"""MemoryConsolidator — 从对话中提取关键事实。

每次 assistant 回复后，运行轻量提取以抓取计划、偏好、事实与决策，
存入长期记忆供日后召回。

与认知循环使用同一个 LLM 后端（或 mock）。
"""

import asyncio
import json
import logging
import re
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

    def __init__(self, llm: MemoryLLM, memory: ScopedLongTermMemory | LongTermMemory,
                 character_name: str = "Elysia",
                 user_name: str = "用户", buffer_threshold: int | None = None):
        """初始化记忆整合器。

        Args:
            llm: 记忆域 LLM 窄接口（MemoryLLM），用于提取和比对记忆。
            memory: 长期记忆后端。
            character_name: 角色名，用于格式化对话文本。
            user_name: 用户名，用于格式化对话文本。
            buffer_threshold: 缓冲阈值，攒够此数量的消息后触发提取。
                默认使用 BUFFER_THRESHOLD (6)。
        """
        self._llm = llm
        self._memory = memory
        self._character_name = character_name
        self._user_name = user_name
        self._history: list[dict] = []  # recent consolidation results
        self._buffer: list[tuple[str, str]] = []  # (user_msg, assistant_msg) 缓冲
        self._threshold = buffer_threshold if buffer_threshold is not None else self.BUFFER_THRESHOLD
        self._consolidate_lock = asyncio.Lock()

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

        if len(self._buffer) < self._threshold:
            return []

        if self._consolidate_lock.locked():
            logger.debug("Consolidation skipped — previous run still active")
            return []

        async with self._consolidate_lock:
            if len(self._buffer) < self._threshold:
                return []  # 已被并发 consolidate 处理
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
            batch = self._buffer
            self._buffer = []
            return await self._do_consolidate(batch)

    async def _do_consolidate(self, batch: list[tuple[str, str]]) -> list[ExtractedMemory]:
        """执行实际的 LLM 提取和存储（需持有 consolidate_lock；batch 已 detach）。

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

        logger.debug("Consolidation: 开始提取 (%d turns)", len(turns))

        prompt = _get_extraction_prompt().replace(
            "{character_name}", self._character_name,
        ).replace(
            "{user_name}", self._user_name,
        ).replace(
            "{conversation}", conversation_text[-2000:],
        )

        try:
            result = await self._llm.think([{"role": "user", "content": prompt}],
                extra_body={"thinking": {"type": "disabled"}})
            items = _parse_extraction(result.text)
        except Exception:
            # 解析失败（含 _JsonArrayParseError）→ batch 重新入队待下轮重试，
            # 整合机会不静默丢失（S9）。warning 可见 LLM 输出异常。
            logger.warning("Consolidation extraction failed（requeue batch 待重试）",
                           exc_info=True)
            self._buffer = batch + self._buffer
            return []

        # batch 已在锁内 detach——不再 clear self._buffer（B5/P1-11：不清掉并发新消息）
        if items:
            try:
                inserted = await self._compare_and_resolve(items)
                logger.debug("Consolidation compared: %d items → %d inserted",
                             len(items), inserted)
            except Exception:
                # 存储失败（含 compare 回退 insert 仍失败）→ batch 重新入队待重试
                logger.warning("Consolidation 存储失败（requeue batch 待重试）", exc_info=True)
                self._buffer = batch + self._buffer
                return []

        for item in items:
            self._history.append(item.model_dump())
        if len(self._history) > 20:
            self._history = self._history[-20:]

        return items

    async def _compare_and_resolve(self, new_items: list[ExtractedMemory]) -> int:
        """将新提取的记忆与已有记忆比对，执行 insert/update/merge/skip。

        Args:
            new_items: 新提取的记忆列表。

        Returns:
            实际新增（insert）的记忆数。失败时回退为全部 insert。
        """
        if not new_items or not self._memory.get_all():
            # 无已有记忆，全部直接 insert
            for item in new_items:
                await self._memory.insert(
                    item.content,
                    memory_type=item.type.value,
                    poignancy=item.poignancy,
                    source="consolidation",
                )
            return len(new_items)

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

            # 3. 执行 — merge → update → insert
            inserted = 0
            for action in actions:
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

            return inserted

        except Exception:
            logger.debug("Consolidation compare failed, falling back to insert all",
                         exc_info=True)
            # 回退：全部 insert
            for item in new_items:
                await self._memory.insert(
                    item.content,
                    memory_type=item.type.value,
                    poignancy=item.poignancy,
                    source="consolidation",
                )
            return len(new_items)
