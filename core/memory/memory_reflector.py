"""MemoryReflector — 定期深度审查 LTM，合并相似、修正过时、删除无意义记忆。

多条件触发（轮次 / 新增记忆数），embedding 聚类分组 + LLM 批量审查。

生命周期:
- save_state() → SessionMemory.save() 调用，返回 MemoryReflectorState
- restore_state(state) → SessionMemory.load() 调用，从 MemoryReflectorState 恢复计数器
- 计数器通过 session.save() 自动持久化，不再需要 flush()
"""

import asyncio
import json
import logging
import re
import time as _time
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import ValidationError

from core.schema.memory_reflector import MemoryReflectorState, ReflectorActions

if TYPE_CHECKING:
    from core.memory.long_term import LongTermMemory
    from core.memory.session import SessionMemory
    from core.adapter.llm_adapter import LLMAdapter

logger = logging.getLogger(__name__)

# MemoryReflector prompt — 文件路径，运行时加载
REFLECTOR_PROMPT_PATH = Path("prompt/MemoryReflector.j2")
_REFLECTOR_PROMPT: str | None = None


def _get_reflector_prompt() -> str:
    global _REFLECTOR_PROMPT
    if _REFLECTOR_PROMPT is not None:
        return _REFLECTOR_PROMPT
    if REFLECTOR_PROMPT_PATH.exists():
        _REFLECTOR_PROMPT = REFLECTOR_PROMPT_PATH.read_text(encoding="utf-8")
    else:
        _REFLECTOR_PROMPT = (
            "Review these long-term memories:\n{memories}\n\n"
            "Last run summary: {last_summary}\n\n"
            "Output merges/updates/deletes as JSON."
        )
    return _REFLECTOR_PROMPT


class MemoryReflector:
    """定期深度去重记忆。

    生命周期:
    - save_state(): SessionMemory.save() 调用，返回 MemoryReflectorState 供持久化
    - restore_state(state): SessionMemory.load() 调用，从 MemoryReflectorState 恢复计数器
    - check_and_reflect(): 在 _after_response() 中调用，检查触发条件
    """

    # 触发阈值
    ROUND_THRESHOLD = 50      # N 轮对话
    NEW_MEMORY_THRESHOLD = 30  # N 条新记忆
    CLUSTER_SIZE = 20          # 每组记忆数（上限 30）

    def __init__(
        self,
        llm: "LLMAdapter",
        memory: "LongTermMemory",
        session: "SessionMemory",
    ):
        self._llm = llm
        self._memory = memory
        self._session = session
        self._lock = asyncio.Lock()
        self._round_count = 0
        self._new_memory_count = 0
        self._last_summary: dict = {}

    # ---- 计数器 ----

    def increment_round(self) -> None:
        """每轮对话后 +1（在 _after_response 中调用）。"""
        self._round_count += 1

    def increment_new_memories(self, count: int) -> None:
        """consolidation 有新记忆后 +N。"""
        self._new_memory_count += count

    # ---- 触发检查 ----

    def _should_run(self) -> bool:
        """检查是否满足触发条件。"""
        return (
            self._round_count >= self.ROUND_THRESHOLD
            or self._new_memory_count >= self.NEW_MEMORY_THRESHOLD
        )

    async def check_and_reflect(self) -> dict | None:
        """在 _after_response() 中调用。满足条件则执行反射。返回执行摘要或 None。"""
        if not self._should_run():
            return None

        if self._lock.locked():
            logger.debug("MemoryReflector skipped — previous run still active")
            return None

        async with self._lock:
            return await self._do_reflect()

    # ---- 核心逻辑 ----

    async def _do_reflect(self) -> dict:
        """执行一次完整的去重周期。"""
        all_entries = self._memory.get_all()
        if not all_entries:
            self._reset_counters()
            return {"ran_at": _time.time(), "merges": 0, "updates": 0, "deletes": 0}

        # 1. 聚类分组
        groups = self._cluster_entries(all_entries)

        total_merges = 0
        total_updates = 0
        total_deletes = 0

        # 2. 逐组 LLM 审查
        for group in groups:
            try:
                actions = await self._review_group(group)
                m, u, d = await self._execute_actions(actions)
                total_merges += m
                total_updates += u
                total_deletes += d
            except Exception:
                logger.debug("MemoryReflector: group review failed", exc_info=True)

        # 3. 记录摘要 + 重置计数器
        summary = {
            "ran_at": _time.time(),
            "merges": total_merges,
            "updates": total_updates,
            "deletes": total_deletes,
        }
        self._last_summary = summary
        self._reset_counters()

        if total_merges or total_updates or total_deletes:
            logger.info(
                "MemoryReflector: %d merges, %d updates, %d deletes",
                total_merges, total_updates, total_deletes,
            )
        return summary

    def _cluster_entries(self, entries) -> list[list]:
        """将条目按相似度聚类分组，每组 ≤ CLUSTER_SIZE 条。

        首选 embedding KMeans，降级按 memory_type 分组 + oversized 拆分。
        """
        n = len(entries)
        if n <= self.CLUSTER_SIZE:
            return [entries]

        # 首选：embedding KMeans（要求全部条目都有 embedding，避免索引越界）
        embeddings = [e.embedding for e in entries if e.embedding]
        if len(embeddings) == len(entries):  # 100% 有 embedding
            try:
                from sklearn.cluster import KMeans
                import numpy as np
                n_clusters = max(1, (n + self.CLUSTER_SIZE - 1) // self.CLUSTER_SIZE)
                X = np.array(embeddings)
                kmeans = KMeans(n_clusters=n_clusters, random_state=42, n_init=10)
                labels = kmeans.fit_predict(X)
                groups: dict[int, list] = {}
                for i, e in enumerate(entries):
                    groups.setdefault(int(labels[i]), []).append(e)
                # 确保每组 ≤ CLUSTER_SIZE（KMeans 可能不均匀）
                result = []
                for g in groups.values():
                    if len(g) > self.CLUSTER_SIZE * 2:
                        # 二次拆分
                        for i in range(0, len(g), self.CLUSTER_SIZE):
                            result.append(g[i:i + self.CLUSTER_SIZE])
                    else:
                        result.append(g)
                return result
            except Exception:
                logger.debug("KMeans clustering failed, falling back to type-based")

        # 降级：按 memory_type 分组 + oversized 拆分
        by_type: dict[str, list] = {}
        for e in entries:
            by_type.setdefault(e.memory_type, []).append(e)
        result = []
        for type_entries in by_type.values():
            for i in range(0, len(type_entries), self.CLUSTER_SIZE):
                result.append(type_entries[i:i + self.CLUSTER_SIZE])
        return result

    async def _review_group(self, group) -> ReflectorActions:
        """LLM 审查一组记忆，返回 ReflectorActions。"""
        entries_json = [
            {"id": e.id, "type": e.memory_type, "content": e.text,
             "poignancy": e.poignancy, "timestamp": e.timestamp}
            for e in group
        ]
        prompt = _get_reflector_prompt().replace(
            "{memories}", json.dumps(entries_json, ensure_ascii=False, indent=2),
        ).replace(
            "{last_summary}", json.dumps(self._last_summary, ensure_ascii=False),
        )
        result = await self._llm.think(
            [{"role": "user", "content": prompt}],
            extra_body={"thinking": {"type": "disabled"}},
        )
        return self._parse_actions(result.text)

    @staticmethod
    def _parse_actions(text: str) -> ReflectorActions:
        """解析 LLM 输出为 ReflectorActions。

        不可解析的文本或 schema 校验失败 → raise ValueError，
        由 _do_reflect 的 per-group except 捕获后跳过该组（下轮重试）。
        {} 或部分键是合法输入（字段默认空列表）。
        """
        text = text.strip()
        if text.startswith("```"):
            lines = text.split("\n")
            text = "\n".join(lines[1:]) if len(lines) > 1 else text
            if text.endswith("```"):
                text = text[:-3]
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            match = re.search(r"\{.*\}", text, re.DOTALL)
            if match is None:
                raise ValueError("MemoryReflector: LLM 输出无法解析为 JSON")
            try:
                data = json.loads(match.group())
            except json.JSONDecodeError:
                raise ValueError("MemoryReflector: LLM 输出无法解析为 JSON")
        try:
            return ReflectorActions.model_validate(data)
        except ValidationError as e:
            logger.warning("MemoryReflector: actions 校验失败: %s", e)
            raise ValueError(f"actions 校验失败: {e}") from e

    async def _execute_actions(self, actions: ReflectorActions) -> tuple[int, int, int]:
        """执行 actions（merge → update → delete）。返回 (merges, updates, deletes) 计数。"""
        merges = updates = deletes = 0

        for m in actions.merges:
            try:
                ok = await self._memory.update_text(
                    m.ids[0], m.merged_content,
                    poignancy=m.poignancy,
                    memory_type=m.type.value if m.type else None,
                )
                if not ok:
                    # 目标 id 不存在（LLM 幻觉）——合并内容未写入，
                    # 绝不能 mark_absorbed 隐藏真实条目
                    logger.warning("MemoryReflector merge 目标不存在: %s", m.ids[0])
                    continue
                for absorbed_id in m.ids[1:]:
                    self._memory.mark_absorbed(absorbed_id)
                merges += 1
            except Exception:
                logger.debug("MemoryReflector merge failed for %s", m.ids[0], exc_info=True)

        for u in actions.updates:
            try:
                if await self._memory.update_text(u.id, u.new_content):
                    updates += 1
            except Exception:
                logger.debug("MemoryReflector update failed for %s", u.id, exc_info=True)

        for d in actions.deletes:
            try:
                if await self._memory.delete(d.id):
                    deletes += 1
            except Exception:
                logger.debug("MemoryReflector delete failed for %s", d.id, exc_info=True)

        return merges, updates, deletes

    # ---- 生命周期 ----

    def _reset_counters(self) -> None:
        self._round_count = 0
        self._new_memory_count = 0

    def save_state(self) -> MemoryReflectorState:
        """返回当前计数器状态，供 SessionMemory.save() 持久化。"""
        return MemoryReflectorState(
            mr_round_count=self._round_count,
            mr_new_memory_count=self._new_memory_count,
            mr_last_summary=self._last_summary,
        )

    def restore_state(self, state: MemoryReflectorState) -> None:
        """从 MemoryReflectorState 恢复计数器和上次执行摘要。"""
        self._round_count = state.mr_round_count
        self._new_memory_count = state.mr_new_memory_count
        self._last_summary = state.mr_last_summary
