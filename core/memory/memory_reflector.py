"""MemoryReflector — 定期深度审查 LTM，合并相似、修正过时、删除无意义记忆。

多条件触发（轮次 / 新增记忆数），embedding 聚类分组 + LLM 批量审查。
"""

import asyncio
import json
import logging
import re
import time as _time
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.memory.long_term import LongTermMemory
    from core.memory.session import SessionMemory
    from core.llm_adapter import LLMAdapter

logger = logging.getLogger(__name__)

# MemoryReflector prompt — 文件路径，运行时加载
REFLECTOR_PROMPT_PATH = Path("prompt/memoryReflector.j2")
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

    对标 CharacterReflector 的生命周期模式:
    - restore_state(): session.load() 之后从 params 恢复计数器
    - check_and_reflect(): 在 _after_response() 中调用，检查触发条件
    - flush(): shutdown 时持久化计数器到 session params
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
                # 预校验
                if not self._validate_actions(actions):
                    logger.warning("MemoryReflector: action validation failed for group, skipping")
                    continue
                # 执行
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
        self._save_state()

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

    async def _review_group(self, group) -> dict:
        """LLM 审查一组记忆，返回 actions dict。"""
        entries_json = [
            {"id": e.id, "type": e.memory_type, "content": e.text,
             "poignancy": e.poignancy, "timestamp": e.timestamp}
            for e in group
        ]
        prompt = _get_reflector_prompt().format(
            memories=json.dumps(entries_json, ensure_ascii=False, indent=2),
            last_summary=json.dumps(self._last_summary, ensure_ascii=False),
        )
        result = await self._llm.think(
            [{"role": "user", "content": prompt}],
            extra_body={"thinking": {"type": "disabled"}},
        )
        return self._parse_actions(result.text)

    @staticmethod
    def _parse_actions(text: str) -> dict:
        """解析 LLM 输出为 actions dict。"""
        text = text.strip()
        if text.startswith("```"):
            lines = text.split("\n")
            text = "\n".join(lines[1:]) if len(lines) > 1 else text
            if text.endswith("```"):
                text = text[:-3]
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            match = re.search(r"\{.*\}", text, re.DOTALL)
            if match:
                try:
                    return json.loads(match.group())
                except json.JSONDecodeError:
                    pass
        return {"merges": [], "updates": [], "deletes": []}

    @staticmethod
    def _validate_actions(actions: dict) -> bool:
        """预校验 actions 批次。"""
        if not isinstance(actions, dict):
            return False
        for key in ("merges", "updates", "deletes"):
            if key not in actions or not isinstance(actions[key], list):
                return False
        # 检查操作上限
        if len(actions["merges"]) > 5 or len(actions["updates"]) > 5 or len(actions["deletes"]) > 3:
            logger.warning("MemoryReflector: actions exceed per-group limits, rejecting")
            return False
        return True

    async def _execute_actions(self, actions: dict) -> tuple[int, int, int]:
        """执行 actions（merge → update → delete）。返回 (merges, updates, deletes) 计数。"""
        merges = updates = deletes = 0

        for m in actions.get("merges", []):
            ids = m.get("ids", [])
            merged = m.get("merged_content", "")
            if len(ids) >= 2 and merged:
                try:
                    # 更新第一条的内容，标记其余为 absorbed
                    await self._memory.update_text(
                        ids[0], merged,
                        poignancy=m.get("poignancy"),
                        memory_type=m.get("type"),
                    )
                    for absorbed_id in ids[1:]:
                        self._memory.mark_absorbed(absorbed_id)
                    merges += 1
                except Exception:
                    logger.debug("MemoryReflector merge failed for %s", ids[0], exc_info=True)

        for u in actions.get("updates", []):
            uid = u.get("id", "")
            new_content = u.get("new_content", "")
            if uid and new_content:
                try:
                    await self._memory.update_text(uid, new_content)
                    updates += 1
                except Exception:
                    logger.debug("MemoryReflector update failed for %s", uid, exc_info=True)

        for d in actions.get("deletes", []):
            did = d.get("id", "")
            if did:
                try:
                    await self._memory.delete(did)
                    deletes += 1
                except Exception:
                    logger.debug("MemoryReflector delete failed for %s", did, exc_info=True)

        return merges, updates, deletes

    # ---- 生命周期 ----

    def _reset_counters(self) -> None:
        self._round_count = 0
        self._new_memory_count = 0

    def restore_state(self) -> None:
        """从 session.params 恢复计数器和上次执行摘要。"""
        self._round_count = self._session.get_param("mr_round_count", 0)
        self._new_memory_count = self._session.get_param("mr_new_memory_count", 0)
        self._last_summary = self._session.get_param("mr_last_summary", {})

    async def flush(self) -> None:
        """持久化计数器和摘要到 session.params。"""
        self._save_state()

    def _save_state(self) -> None:
        self._session.set_param("mr_round_count", self._round_count)
        self._session.set_param("mr_new_memory_count", self._new_memory_count)
        self._session.set_param("mr_last_summary", self._last_summary)
