"""MemoryConsolidator — 从对话中提取关键事实。

每次 assistant 回复后，运行轻量提取以抓取计划、偏好、事实与决策，
存入长期记忆供日后召回。

与认知循环使用同一个 LLM 后端（或 mock）。
"""

import asyncio
import json
import logging
import re
import time
from typing import Any

from pydantic import TypeAdapter, ValidationError

from core.schema.memory import (
    ExtractedMemory,
    CompareAction,
    MergeAction,
    UpdateAction,
    InsertAction,
)

from paths import ProjectPath
from core.memory.long_term import LongTermMemory
from core.memory.scoped_long_term import ScopedLongTermMemory
from core.memory.llm_protocol import MemoryLLM

logger = logging.getLogger(__name__)

# Extraction prompt — 文件路径，运行时加载
_EXTRACTION_PATH = ProjectPath.PROMPTS.resolve() / "ExtractMemory.j2"
_EXTRACTION_PROMPT: str | None = None


def _get_extraction_prompt() -> str:
    global _EXTRACTION_PROMPT
    if _EXTRACTION_PROMPT is not None:
        return _EXTRACTION_PROMPT
    if _EXTRACTION_PATH.exists():
        _EXTRACTION_PROMPT = _EXTRACTION_PATH.read_text(encoding="utf-8")
    else:
        _EXTRACTION_PROMPT = (
            "从以下对话中提取值得长期记住的关键信息。\n\n"
            "对话:\n{conversation}\n\n"
            "返回 JSON 数组。没有则返回 []。"
        )
    return _EXTRACTION_PROMPT

# Consolidation 比对 prompt — 文件路径，运行时加载
_COMPARE_PROMPT: str | None = None


def _get_compare_prompt() -> str:
    global _COMPARE_PROMPT
    if _COMPARE_PROMPT is not None:
        return _COMPARE_PROMPT
    path = ProjectPath.PROMPTS.resolve() / "ConsolidationCompare.j2"
    if path.exists():
        _COMPARE_PROMPT = path.read_text(encoding="utf-8")
    else:
        _COMPARE_PROMPT = (
            "Compare these new memory items:\n{new_items}\n\n"
            "With existing related memories:\n{existing_items}\n\n"
            "For each new item, output: insert/update/merge/skip as JSON array."
        )
    return _COMPARE_PROMPT


# TypeAdapter 提为模块级单例（Pydantic 官方建议），避免每次解析重复创建
_compare_actions_adapter = TypeAdapter(list[CompareAction])


class _JsonArrayParseError(ValueError):
    """无法从 LLM 输出中解析出 JSON 数组。

    与「合法空数组 []」区分：[] 是成功解析出的空结果（返回 [] 不抛），
    解析失败抛本异常——调用方据此保留整合缓冲，避免整合机会静默丢失（S9）。
    """


def _extract_json_array(text: str) -> list[dict]:
    """纯文本→list[dict]。去掉 markdown fence → json.loads → regex fallback。

    Args:
        text: LLM 输出的原始文本，可能包含 markdown 代码块。

    Returns:
        解析出的 dict 列表；合法空数组返回空列表。

    Raises:
        _JsonArrayParseError: 无法解析出 JSON 数组时抛出。
    """
    text = text.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        text = "\n".join(lines[1:]) if len(lines) > 1 else text
        if text.endswith("```"):
            text = text[:-3]
    text = text.strip()
    if not text:
        raise _JsonArrayParseError("提取输出为空")
    try:
        items = json.loads(text)
        if isinstance(items, list):
            return items
        raise _JsonArrayParseError("JSON 顶层不是数组")
    except json.JSONDecodeError:
        match = re.search(r"\[.*\]", text, re.DOTALL)
        if match:
            try:
                items = json.loads(match.group())
                if isinstance(items, list):
                    return items
            except json.JSONDecodeError:
                pass
    raise _JsonArrayParseError("无法从文本中解析出 JSON 数组")


def _parse_extraction(text: str) -> list[ExtractedMemory]:
    """解析 LLM 提取输出 (ExtractMemory.j2) → list[ExtractedMemory]。

    逐条 .model_validate()——一个坏元素只跳过该项，不影响同批次其他合规项。
    .model_validate() 确保非 dict 元素统一抛 ValidationError（而非 TypeError）。

    Args:
        text: LLM 提取输出的原始文本。

    Returns:
        合规的 ExtractedMemory 列表，不合规的元素被跳过。
    """
    items = _extract_json_array(text)
    result: list[ExtractedMemory] = []
    for item in items:
        try:
            result.append(ExtractedMemory.model_validate(item))
        except ValidationError as e:
            logger.warning("跳过不合规的提取记忆: %s，原始数据: %s", e, item)
    return result


def _parse_compare_actions(text: str, expected_count: int | None = None) -> list[CompareAction]:
    """解析 LLM 比对输出 (ConsolidationCompare.j2) → list[CompareAction]。

    整批 all-or-nothing 校验。校验失败 raise ValueError——调用方
    _compare_and_resolve() 的 except Exception 捕获后走 insert-all 回退，
    确保一个坏元素不导致静默丢记忆。

    Args:
        text: LLM 比对输出的原始文本。
        expected_count: 期望的 actions 数量，与 new_items 一致。
            数量不符时 raise ValueError（如 LLM 返回空数组 → 回退，避免静默丢记忆）。

    Returns:
        解析出的 CompareAction 列表。

    Raises:
        ValueError: 校验失败或数量不符时抛出。
    """
    items = _extract_json_array(text)
    try:
        actions = _compare_actions_adapter.validate_python(items)
    except ValidationError as e:
        raise ValueError(f"比对 actions 校验失败: {e}") from e
    if expected_count is not None and len(actions) != expected_count:
        raise ValueError(
            f"比对 actions 数量不符: 期望 {expected_count}，实际 {len(actions)}"
        )
    return actions


class MemoryConsolidator:
    """从对话中提取并存储关键事实。"""

    BUFFER_THRESHOLD = 6  # 攒够 N 条消息再调 LLM 提取
    #: 缓冲（含 requeue 前插）的**硬上界**（S3-13）。requeue 走 `batch + self._buffer`
    #: 前插且无上界：LLM 持续失败时缓冲无界增长，而提取 prompt 只取 `[-2000:]`——
    #: **最旧的轮次永远不会进入 prompt**，却仍在每轮触发一次注定失败的 LLM 调用。
    #: 超界时从最旧开始丢，并**如实记录丢弃条数**（丢弃是可见事件，不是静默行为）。
    MAX_BUFFER_TURNS = 200

    def __init__(self, llm: MemoryLLM, memory: ScopedLongTermMemory | LongTermMemory,
                 character_name: str = "Elysia",
                 user_name: str = "用户", buffer_threshold: int | None = None,
                 pending_key: str | None = None):
        """初始化记忆整合器。

        Args:
            llm: 记忆域 LLM 窄接口（MemoryLLM），用于提取和比对记忆。
            memory: 长期记忆后端。
            character_name: 角色名，用于格式化对话文本。
            user_name: 用户名，用于格式化对话文本。
            buffer_threshold: 缓冲阈值，攒够此数量的消息后触发提取。
                默认使用 BUFFER_THRESHOLD (6)。
            pending_key: 待整合缓冲 journal 的键（生产传 instance_id；None → 不落盘，
                测试与一次性 consolidator 用默认 None）。构造时读回上次进程留下的
                缓冲——**决策 #10：关机时最后 <6 轮不做提取，改为落盘 + 下次启动补提**。
        """
        self._llm = llm
        self._memory = memory
        self._character_name = character_name
        self._user_name = user_name
        self._history: list[dict] = []  # recent consolidation results
        self._buffer: list[tuple[str, str]] = []  # (user_msg, assistant_msg) 缓冲
        self._dropped_turns = 0                    # 因上界丢弃的轮数（S3-13，可见指标）
        self._threshold = buffer_threshold if buffer_threshold is not None else self.BUFFER_THRESHOLD
        self._consolidate_lock = asyncio.Lock()
        # 最近一次运行的状态（debug 面板 / 日志）——区分「没跑」/「在跑」/「跑了提取 0 条」/「跑失败」
        self._running: bool = False
        self._last_run_at: float | None = None
        self._last_turns: int = 0
        self._last_extracted: int = 0
        self._last_inserted: int = 0
        self._last_error: str = ""
        # 待整合缓冲持久化（#10）：构造即读回上次进程的残留缓冲，并把
        # `_pending_restored` 置真——下一次 consolidate 跳过阈值立刻补提一次
        # （否则"补提"要等用户再聊够 6 轮才发生，而用户可能根本不再聊）。
        self._pending_key = pending_key
        self._pending_restored = False
        if pending_key:
            restored = self._pending_store().get(pending_key)
            if restored:
                self._buffer = list(restored)
                self._pending_restored = True
                logger.info(
                    "Consolidator 读回上次进程未整合的 %d 轮对话（待补提）", len(restored))

    def _pending_store(self):
        """待整合缓冲 journal store（延迟 import：避免 memory 包内循环导入）。"""
        from core.memory.consolidator_pending import get_store

        return get_store()

    def save_pending(self) -> None:
        """把当前缓冲落盘（#10）。

        何时调用：① 关机 flush 之后（无论成败——成功则缓冲已空、journal 被清掉，
        超时/失败则 requeue 后的缓冲被写下来）；② 任何 requeue 失败路径
        （见 `_requeue`），使"提取失败"在**进程意外退出**时也不丢。
        """
        if not self._pending_key:
            return
        self._pending_store().put(self._pending_key, list(self._buffer))

    def has_pending_restore(self) -> bool:
        """是否有上次进程遗留、尚未补提的缓冲（启动补提路径据此决定是否 spawn）。"""
        return self._pending_restored and bool(self._buffer)

    def set_user_name(self, user_name: str) -> None:
        """投影本轮身份的有效称呼（P0-2：preferredAddress 优先，否则 displayName）。

        记忆提取/比对的 {user_name} 占位符与对话标签据此使用用户当前的自我称呼，
        不再固定为启动时的 config.agent.user_name。回合由 _process_lock 串行，
        无并发写。
        """
        if user_name:
            self._user_name = user_name

    async def consolidate(self, user_message: str, assistant_response: str) -> list[ExtractedMemory]:
        """缓冲对话，攒够阈值后调 LLM 提取。

        如果已有 consolidation 正在运行则跳过本轮（防止并发 LLM 调用）。

        B5/P1-11：锁内原子 detach 当前 batch——LLM 等待期间的新消息进入新 buffer，
        不被本轮 clear() 误删；提取/存储失败时 batch 重新入队（requeue）待重试。

        Args:
            user_message: 用户消息文本。
            assistant_response: 助手回复文本。

        Returns:
            本次提取的 ExtractedMemory 列表，可能为空。
        """
        if not assistant_response.strip() or not user_message.strip():
            return []

        self._buffer.append((user_message, assistant_response))

        # #10：上次进程遗留的缓冲在**下一次对话**就补提，不等攒满阈值——否则
        # "补提"要求用户再聊够 6 轮，而他可能再也不聊（残留缓冲无限期躺在内存/盘上）。
        restored = self._pending_restored
        if len(self._buffer) < self._threshold and not restored:
            self.save_pending()   # 轮次落盘：进程被 kill（无 shutdown 钩子）也不丢
            return []

        if self._consolidate_lock.locked():
            logger.debug("Consolidation skipped — previous run still active")
            self.save_pending()
            return []

        async with self._consolidate_lock:
            if len(self._buffer) < self._threshold and not restored:
                return []  # 已被并发 consolidate 处理
            # 补提标志只在**真的走到提取**时消费：抢锁失败/已被并发处理时保留标志，
            # 让下一次对话继续尝试（否则标志丢一次，补提就要再等攒够阈值）。
            self._pending_restored = False
            # 原子 detach 当前 batch——之后 append 的新消息进新 buffer
            batch = self._buffer
            self._buffer = []
            return await self._do_consolidate(batch)

    async def flush(self) -> list[ExtractedMemory]:
        """强制整合缓冲中的对话轮次，不等待阈值。

        Called on shutdown to avoid losing unconsolidated messages.

        Returns:
            提取的 ExtractedMemory 列表，缓冲为空或 LLM 失败时为空。
        """
        if not self._buffer:
            return []

        if self._consolidate_lock.locked():
            logger.debug("Flush skipped — consolidation already in progress")
            return []

        async with self._consolidate_lock:
            if not self._buffer:
                return []
            self._pending_restored = False
            batch = self._buffer
            self._buffer = []
            return await self._do_consolidate(batch)

    async def _do_consolidate(self, batch: list[tuple[str, str]]) -> list[ExtractedMemory]:
        """执行实际的 LLM 提取和存储（需持有 consolidate_lock；batch 已 detach）。

        无论结果如何都会留下**一条 INFO** 说明「跑了、几轮、提取几条、入库几条」。
        此前提取到 0 条时全程零日志（`compared` 行在 `if items:` 内且是 DEBUG），
        面板上「Buffer 0/6 + 历史空」与「根本没跑」「跑挂了」完全无法区分——
        实测有人发了 6 轮陪聊消息（"嘿嘿"/"❤️"），提取 0 条是**正确答案**，
        却因为毫无痕迹而被合理怀疑成故障（2026-09-12）。

        Args:
            batch: 本批待整合的 (user_msg, assistant_msg) 列表。

        Returns:
            提取并比对后的 ExtractedMemory 列表，LLM 失败时为空。
        """
        # 合并缓冲中的多轮对话为一段文本
        turns: list[str] = []
        for u, a in batch:
            turns.append(f"{self._user_name}: {u[:300]}")
            turns.append(f"{self._character_name}: {a[:500]}")
        conversation_text = "\n".join(turns)

        prompt = _get_extraction_prompt().replace(
            "{character_name}", self._character_name,
        ).replace(
            "{user_name}", self._user_name,
        ).replace(
            "{conversation}", conversation_text[-2000:],
        )

        # `_running` 的置位放在所有可能抛异常的**纯准备**代码之后——
        # 这样置位与 `_finish_run` 之间只剩下面两段各自 except 兜底的代码，
        # 不会出现"标志位卡在 True"的状态。
        logger.debug("Consolidation: 开始提取 (%d turns)", len(turns))
        self._running = True
        self._last_turns = len(turns)

        try:
            result = await self._llm.think([{"role": "user", "content": prompt}],
                extra_body={"thinking": {"type": "disabled"}})
            items = _parse_extraction(result.text)
        except BaseException as e:
            # 解析失败（含 _JsonArrayParseError）→ batch 重新入队待下轮重试，
            # 整合机会不静默丢失（S9）。warning 可见 LLM 输出异常。
            #
            # **必须是 BaseException（S2-A1）**：关停 flush 走
            # `asyncio.wait_for(flush(), timeout=5.0)`，超时 → 取消 → `CancelledError`
            # 是 BaseException 子类，原先绕过 `except Exception` → **requeue 不执行、
            # 这批对话永久丢失**，而日志只有一句 "flush timed out"，完全不提数据丢失。
            logger.warning("Consolidation extraction failed（requeue batch 待重试）",
                           exc_info=True)
            self._requeue(batch)
            self._finish_run(extracted=0, inserted=0, error=f"提取失败: {e}")
            if not isinstance(e, Exception):
                raise        # 取消/中断语义不吞——缓冲已还原，交给调用方处理
            return []

        # batch 已在锁内 detach——不再 clear self._buffer（B5/P1-11：不清掉并发新消息）
        inserted = 0
        if items:
            try:
                inserted = await self._compare_and_resolve(items)
                logger.debug("Consolidation compared: %d items → %d inserted",
                             len(items), inserted)
            except BaseException as e:
                # 存储失败（含 compare 回退 insert 仍失败）→ batch 重新入队待重试。
                # 同 S2-A1：取消也必须还原缓冲（`_compare_and_resolve` 内部有
                # 嵌入/向量库 await，同样可能被关停预算掐断）。
                logger.warning("Consolidation 存储失败（requeue batch 待重试）", exc_info=True)
                self._requeue(batch)
                self._finish_run(extracted=len(items), inserted=0,
                                 error=f"存储失败: {e}")
                if not isinstance(e, Exception):
                    raise
                return []

        for item in items:
            self._history.append(item.model_dump())
        if len(self._history) > 20:
            self._history = self._history[-20:]

        self._finish_run(extracted=len(items), inserted=inserted)
        # **无条件** INFO：提取 0 条也是有效结果，必须与「没跑」「跑挂了」可区分
        logger.info("Consolidation: %d turns → 提取 %d 条，入库 %d 条",
                    self._last_turns, len(items), inserted)
        # #10：本批已消化 → journal 里属于它的部分该消失（若 LLM 等待期间又来了新
        # 轮次，写下去的是那些新轮次——正是"仍未整合"的准确集合）。
        self.save_pending()
        return items

    def _requeue(self, batch: list[tuple[str, str]]) -> None:
        """把失败的 batch 前插回缓冲，并施加**硬上界**（S3-13）。

        requeue 的语义是"这轮没成功，下次再试"。但持续失败时它会无界增长：每轮
        `batch + self._buffer` 前插，而提取 prompt 只取末尾 2000 字符——**最旧的轮次
        永远进不了 prompt**，却仍在每轮触发一次注定失败的 LLM 调用。

        超界时从**队首（最旧）**开始丢，并记录丢弃条数（丢弃必须是可见事件）。
        为什么丢最旧而不是最新：提取 prompt 只取末尾 2000 字符——**最旧的内容本来
        就进不了 prompt**，丢掉它不会损失任何"本来会被提取"的信息，只是停止为它
        反复付一次注定失败的 LLM 调用。代价是 requeue 的那批（前插 → 位于队首）
        在长期故障时会被先丢掉，即"重试"在极端积压下让位于"保住新内容"——
        这是有意的取舍，不是遗漏。

        Args:
            batch: 本轮失败、待重试的 (user_msg, assistant_msg) 列表。
        """
        self._buffer = batch + self._buffer
        overflow = len(self._buffer) - self.MAX_BUFFER_TURNS
        if overflow > 0:
            self._buffer = self._buffer[overflow:]
            logger.warning(
                "Consolidation 缓冲超过上界 %d——丢弃最旧 %d 轮（累计丢弃 %d 轮）",
                self.MAX_BUFFER_TURNS, overflow, self._dropped_turns + overflow,
            )
            self._dropped_turns += overflow
        # #10：requeue 之后立刻落盘。**这是达到阈值之后的唯一落盘点**——未达阈值时
        # 由 append 路径写，但一旦攒够阈值 batch 就被 detach 出 buffer，此后只有
        # "失败重入队"（本处）与"成功消化"（_do_consolidate 末）两个同步点。少了本处，
        # 磁盘上留的是 detach 前那份（内容上少一批、且不带本轮的丢弃修正）。
        self.save_pending()

    def _finish_run(self, *, extracted: int, inserted: int, error: str = "") -> None:
        """记录本次运行结果（debug 面板 / 日志用）。"""
        self._running = False
        self._last_run_at = time.time()
        self._last_extracted = extracted
        self._last_inserted = inserted
        self._last_error = error

    def stats(self) -> dict[str, Any]:
        """运行状态快照——供 debug 面板消费，避免消费方穿透 ``_buffer``/``_history``。

        Returns:
            dict: 缓冲/阈值/历史 + 最近一次运行的轮数、提取数、入库数、错误与时间戳。
        """
        return {
            "buffer_size": len(self._buffer),
            "max_buffer_turns": self.MAX_BUFFER_TURNS,
            "dropped_turns": self._dropped_turns,
            "threshold": self._threshold,
            "history": [dict(h) for h in self._history[-5:]],
            "running": self._running,
            "last_run_at": self._last_run_at,
            "last_turns": self._last_turns,
            "last_extracted": self._last_extracted,
            "last_inserted": self._last_inserted,
            "last_error": self._last_error,
        }

    async def _compare_and_resolve(self, new_items: list[ExtractedMemory]) -> int:
        """将新提取的记忆与已有记忆比对，执行 insert/update/merge/skip。

        Args:
            new_items: 新提取的记忆列表。

        Returns:
            实际新增（insert）的记忆数。失败时回退为全部 insert。
        """
        if not new_items or not self._memory.get_all():
            # 无已有记忆，全部直接 insert
            return await self._insert_all(new_items)

        try:
            # 1. 搜索相关已有记忆
            related = await self._memory.search(
                query_text=" ".join(item.content for item in new_items),
                top_k=10,
            )
            related_dicts = [
                {"id": e.id, "type": e.memory_type, "content": e.text,
                 "poignancy": e.poignancy, "timestamp": e.timestamp}
                for e in related
            ]

            # 2. LLM 比对
            prompt_template = _get_compare_prompt()
            compare_prompt = prompt_template.replace(
                "{character_name}", self._character_name,
            ).replace(
                "{user_name}", self._user_name,
            ).replace(
                "{new_items}", json.dumps(
                    [item.model_dump() for item in new_items],
                    ensure_ascii=False, indent=2,
                ),
            ).replace(
                "{existing_items}", json.dumps(related_dicts, ensure_ascii=False, indent=2),
            )
            result = await self._llm.think(
                [{"role": "user", "content": compare_prompt}],
                extra_body={"thinking": {"type": "disabled"}},
            )
            actions = _parse_compare_actions(result.text, expected_count=len(new_items))
        except Exception:
            # 比对阶段失败：**此时一条动作都没执行**，回退 insert-all 是安全的
            # （不会重复应用）。S3-12：这条回退原先与"执行动作"共用一个 try，
            # 于是"执行到第 2 条时失败"也会走这里——已 insert 的重复插入、
            # 已 merge/update 的二次应用。日志也从 debug 提到 warning：
            # 这是**数据重复**级别的降级，不该在默认级别看不见。
            logger.warning(
                "Consolidation 比对失败，回退为全部插入（%d 条）", len(new_items),
                exc_info=True)
            return await self._insert_all(new_items)

        # 3. 执行 — merge → update → insert（**逐条兜底**：单条失败不影响其余，
        #    更不触发"整批重跑"）
        inserted = 0
        failed = 0
        for idx, action in enumerate(actions):
            try:
                if isinstance(action, MergeAction):
                    await self._memory.update_text(
                        action.target_id, action.merged_content,
                        poignancy=action.poignancy,
                        memory_type=action.type.value if action.type else None,
                    )
                elif isinstance(action, UpdateAction):
                    await self._memory.update_text(action.target_id, action.new_content)
                elif isinstance(action, InsertAction):
                    await self._memory.insert(
                        action.content,
                        memory_type=action.type.value,
                        poignancy=action.poignancy,
                        source="consolidation",
                    )
                    inserted += 1
                # SkipAction — 无操作
            except Exception:
                failed += 1
                logger.warning(
                    "Consolidation 第 %d/%d 条动作执行失败（其余继续，不回退重跑）: %s",
                    idx + 1, len(actions), action.__class__.__name__, exc_info=True)
        if failed:
            logger.warning("Consolidation: %d/%d 条动作未生效", failed, len(actions))
        return inserted

    async def _insert_all(self, new_items: list[ExtractedMemory]) -> int:
        """全部直接插入（无比对可用时的降级路径）。"""
        for item in new_items:
            await self._memory.insert(
                item.content,
                memory_type=item.type.value,
                poignancy=item.poignancy,
                source="consolidation",
            )
        return len(new_items)
