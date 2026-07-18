"""MemoryConsolidator — extracts key facts from conversations.

After each assistant response, runs a lightweight extraction to pull out
plans, preferences, facts, and decisions. These are stored in long-term
memory for future recall.

Uses the same LLM backend as the cognitive loop (or mock).
"""

import asyncio
import json
import logging
import re
from pathlib import Path

from pydantic import TypeAdapter, ValidationError

from core.schema.memory import (
    ExtractedMemory,
    CompareAction,
    MergeAction,
    UpdateAction,
    InsertAction,
)

from .memory.long_term import LongTermMemory
from .llm_adapter import LLMAdapter

logger = logging.getLogger(__name__)

# Extraction prompt — 文件路径，运行时加载
EXTRACTION_PROMPT_PATH = Path("prompt/ExtractMemory.j2")
_EXTRACTION_PROMPT: str | None = None


def _get_extraction_prompt() -> str:
    global _EXTRACTION_PROMPT
    if _EXTRACTION_PROMPT is not None:
        return _EXTRACTION_PROMPT
    if EXTRACTION_PROMPT_PATH.exists():
        _EXTRACTION_PROMPT = EXTRACTION_PROMPT_PATH.read_text(encoding="utf-8")
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
    path = Path("prompt/ConsolidationCompare.j2")
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


def _extract_json_array(text: str) -> list[dict]:
    """纯文本→list[dict]。去掉 markdown fence → json.loads → regex fallback。"""
    text = text.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        text = "\n".join(lines[1:]) if len(lines) > 1 else text
        if text.endswith("```"):
            text = text[:-3]
    try:
        items = json.loads(text)
        if isinstance(items, list):
            return items
    except json.JSONDecodeError:
        match = re.search(r"\[.*\]", text, re.DOTALL)
        if match:
            try:
                items = json.loads(match.group())
                if isinstance(items, list):
                    return items
            except json.JSONDecodeError:
                pass
    return []


def _parse_extraction(text: str) -> list[ExtractedMemory]:
    """解析 LLM 提取输出 (ExtractMemory.j2) → list[ExtractedMemory]。

    逐条 .model_validate()——一个坏元素只跳过该项，不影响同批次其他合规项。
    .model_validate() 确保非 dict 元素统一抛 ValidationError（而非 TypeError）。
    """
    items = _extract_json_array(text)
    result: list[ExtractedMemory] = []
    for item in items:
        try:
            result.append(ExtractedMemory.model_validate(item))
        except ValidationError as e:
            logger.warning("跳过不合规的提取记忆: %s，原始数据: %s", e, item)
    return result


def _parse_compare_actions(text: str) -> list[CompareAction]:
    """解析 LLM 比对输出 (ConsolidationCompare.j2) → list[CompareAction]。

    整批 all-or-nothing 校验。校验失败 raise ValueError——调用方
    _compare_and_resolve() 的 except Exception 捕获后走 insert-all 回退，
    确保一个坏元素不导致静默丢记忆。
    """
    items = _extract_json_array(text)
    try:
        return _compare_actions_adapter.validate_python(items)
    except ValidationError as e:
        raise ValueError(f"比对 actions 校验失败: {e}") from e


class MemoryConsolidator:
    """Extracts and stores key facts from conversations."""

    BUFFER_THRESHOLD = 6  # 攒够 N 条消息再调 LLM 提取

    def __init__(self, llm: LLMAdapter, memory: LongTermMemory, character_name: str = "Elysia",
                 user_name: str = "用户", buffer_threshold: int | None = None):
        self._llm = llm
        self._memory = memory
        self._character_name = character_name
        self._user_name = user_name
        self._history: list[dict] = []  # recent consolidation results
        self._buffer: list[tuple[str, str]] = []  # (user_msg, assistant_msg) 缓冲
        self._threshold = buffer_threshold if buffer_threshold is not None else self.BUFFER_THRESHOLD
        self._consolidate_lock = asyncio.Lock()

    async def consolidate(self, user_message: str, assistant_response: str) -> list[dict]:
        """缓冲对话，攒够阈值后调 LLM 提取。返回本次提取的条目（可能为空）。

        如果已有 consolidation 正在运行则跳过本轮（防止并发 LLM 调用）。
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
            return await self._do_consolidate()

    async def flush(self) -> list[dict]:
        """Force consolidation of buffered turns regardless of threshold.

        Called on shutdown to avoid losing unconsolidated messages.
        Returns extracted items (may be empty if buffer was empty or LLM fails).
        """
        if not self._buffer:
            return []

        if self._consolidate_lock.locked():
            logger.debug("Flush skipped — consolidation already in progress")
            return []

        async with self._consolidate_lock:
            return await self._do_consolidate()

    async def _do_consolidate(self) -> list[dict]:
        """执行实际的 LLM 提取和存储（需持有 consolidate_lock）。"""
        # 合并缓冲中的多轮对话为一段文本
        turns: list[str] = []
        for u, a in self._buffer:
            turns.append(f"{self._user_name}: {u[:300]}")
            turns.append(f"{self._character_name}: {a[:500]}")
        conversation_text = "\n".join(turns)
        self._buffer.clear()

        prompt = _get_extraction_prompt().format(
            conversation=conversation_text[:2000],
            character_name=self._character_name,
            user_name=self._user_name,
        )

        try:
            result = await self._llm.think([{"role": "user", "content": prompt}],
                extra_body={"thinking": {"type": "disabled"}})
            items = self._parse_result(result.text)
        except Exception:
            logger.debug("Consolidation extraction failed", exc_info=True)
            return []

        if items:
            inserted = await self._compare_and_resolve(items)
            logger.debug("Consolidation compared: %d items → %d inserted", len(items), inserted)

        for item in items:
            self._history.append({
                "type": item.get("type", "?"),
                "content": item.get("content", ""),
                "poignancy": item.get("poignancy", 5),
            })
        if len(self._history) > 20:
            self._history = self._history[-20:]

        return items

    @staticmethod
    def _parse_result(text: str) -> list[dict]:
        """Parse LLM output as JSON array. Robust to markdown wrapping."""
        text = text.strip()
        # Strip markdown code fences
        if text.startswith("```"):
            lines = text.split("\n")
            text = "\n".join(lines[1:]) if len(lines) > 1 else text
            if text.endswith("```"):
                text = text[:-3]
        try:
            items = json.loads(text)
            if isinstance(items, list):
                return items
        except json.JSONDecodeError:
            # Try to find JSON array inside text
            import re
            match = re.search(r"\[.*\]", text, re.DOTALL)
            if match:
                try:
                    items = json.loads(match.group())
                    if isinstance(items, list):
                        return items
                except json.JSONDecodeError:
                    pass
        return []

    async def _compare_and_resolve(self, new_items: list[dict]) -> int:
        """将新提取的记忆与已有记忆比对，执行 insert/update/merge/skip。
        返回实际新增的记忆数。失败时回退：全部 insert。"""
        if not new_items or not self._memory.get_all():
            # 无已有记忆，全部直接 insert
            for item in new_items:
                content = item.get("content", "").strip()
                if content:
                    await self._memory.insert(
                        content,
                        memory_type=item.get("type", "note"),
                        poignancy=int(item.get("poignancy", 5)),
                        source="consolidation",
                    )
            return len(new_items)

        try:
            # 1. 搜索相关已有记忆
            related = await self._memory.search(
                query_text=" ".join(item.get("content", "") for item in new_items),
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
                "{new_items}", json.dumps(new_items, ensure_ascii=False, indent=2),
            ).replace(
                "{existing_items}", json.dumps(related_dicts, ensure_ascii=False, indent=2),
            )
            result = await self._llm.think(
                [{"role": "user", "content": compare_prompt}],
                extra_body={"thinking": {"type": "disabled"}},
            )
            actions = self._parse_result(result.text)

            # 3. 预校验
            if not self._validate_actions(actions, new_items):
                raise ValueError("Action validation failed")

            # 4. 执行 — merge → update → insert
            inserted = 0
            for action in actions:
                act = action.get("action", "insert")
                if act == "merge":
                    target_id = action.get("target_id", "")
                    merged = action.get("merged_content", "")
                    if target_id and merged:
                        await self._memory.update_text(
                            target_id, merged,
                            poignancy=action.get("poignancy"),
                            memory_type=action.get("type"),
                        )
                elif act == "update":
                    target_id = action.get("target_id", "")
                    new_content = action.get("new_content", "")
                    if target_id and new_content:
                        await self._memory.update_text(target_id, new_content)
                elif act == "insert":
                    content = action.get("content", "").strip()
                    if content:
                        await self._memory.insert(
                            content,
                            memory_type=action.get("type", "note"),
                            poignancy=int(action.get("poignancy", 5)),
                            source="consolidation",
                        )
                        inserted += 1
                # skip — 无操作

            return inserted

        except Exception:
            logger.debug("Consolidation compare failed, falling back to insert all",
                         exc_info=True)
            # 回退：全部 insert
            for item in new_items:
                content = item.get("content", "").strip()
                if content:
                    await self._memory.insert(
                        content,
                        memory_type=item.get("type", "note"),
                        poignancy=int(item.get("poignancy", 5)),
                        source="consolidation",
                    )
            return len(new_items)

    @staticmethod
    def _validate_actions(actions: list[dict], new_items: list[dict]) -> bool:
        """预校验 LLM 输出的 actions 批次。返回 True 表示通过。"""
        if not isinstance(actions, list):
            return False
        for action in actions:
            act = action.get("action", "")
            if act not in ("insert", "update", "merge", "skip"):
                return False
            if act in ("update", "merge") and not action.get("target_id"):
                return False
        return True
