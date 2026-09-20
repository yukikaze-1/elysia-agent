"""MessageBuilder — 纯 prompt 组装。"""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from dataclasses import dataclass

from core.schema.message import Message
from core.schema.part import (
    AudioPart,
    FilePart,
    ImagePart,
    Part,
    StickerPart,
    TextPart,
    VideoPart,
)
from core.schema.tool import ToolCall
from core.schema.adapters.message_adapter import content_to_openai, content_text, parts_from_dicts

logger = logging.getLogger(__name__)


def _format_size(size_bytes: int) -> str:
    """人类可读体积（只在占位文本里用，精度不重要）。"""
    if size_bytes <= 0:
        return ""
    if size_bytes < 1024:
        return f"{size_bytes}B"
    if size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f}KB"
    return f"{size_bytes / (1024 * 1024):.1f}MB"


# 非文本 Part 的占位文案（**唯一定义点**）。S2-H8：转换器只发射 Text/Image，
# sticker/file/audio/video **一个都发不出去** —— 契约又允许"仅贴图/仅附件"的消息
# （content 为空），于是这类消息此前变成 `{"role":"user","content":[]}`：模型收到的
# 是一条语义为空的消息，像"用户什么都没说"一样作答，历史里还会被整条丢弃
# （实测 8 条存量消息只发出 5 条）。这里给模型一个**最小事实**："用户发了东西、是什么"。
#
# 为什么不对齐"把附件内容喂进去"：那需要 provider 侧能力（file/audio 输入）与工具支持，
# 属产品能力而非本修复；本修复只保证**不丢信息**。
#
# 为什么默认不带 attachment_id：它不是任何工具的参数（vision 用 image_index/paths、
# filesystem 用路径），给一个用不上的 ID 只会诱发模型编造工具调用。仅在**没有任何
# 可读标识**（文件名为空、音频/视频）时用它兜底。
_PLACEHOLDER_WORDING: dict[str, tuple[str, str]] = {
    # kind: (当前轮, 历史轮)
    "sticker": ("[用户发了一个贴图：{sid}]", "[用户曾发过一个贴图：{sid}] "),
    "file": ("[用户发送了文件：{name}{size}]", "[用户曾发送文件：{name}{size}] "),
    "audio": ("[用户发送了音频（{mime}{dur}）]", "[用户曾发送音频（{mime}{dur}）] "),
    "video": ("[用户发送了视频（{mime}{dur}）]", "[用户曾发送视频（{mime}{dur}）] "),
}


def _duration_s(duration_ms: int) -> str:
    return f"，时长 {duration_ms / 1000:.1f}s" if duration_ms > 0 else ""


def _media_placeholder(parts: Sequence[Part], *, images_included: bool,
                       historical: bool) -> str:
    """把"转换器发射不出去、但模型必须知道"的 Part 汇总成占位文本（S2-H8）。

    Args:
        parts: 消息的 Part 列表。
        images_included: 图片是否已由 `content_to_openai` 作为 image_url 块发出
            （多模态 provider 为 True）——True 时图片**不再**补占位，否则会重复；
            False 时按既有文案提示模型用 describe_image 查看。
        historical: 历史消息（过去时文案）还是当前消息（现在时 + 可执行提示）。

    Returns:
        占位文本（多条以换行连接）；无需提示时返回空串。
    """
    idx = 1 if historical else 0
    notes: list[str] = []

    images = [p for p in parts if isinstance(p, ImagePart)]
    if images and not images_included:
        if historical:
            notes.append(_format_image_history_hint(len(images)).rstrip())
        elif len(images) == 1:
            notes.append("[用户发送了一张图片。使用 describe_image 工具查看。]")
        else:
            indices = list(range(len(images)))
            notes.append(
                f"[用户发送了{len(images)}张图片。"
                f"使用 describe_image(image_indices={indices}) 一次性查看所有图片。]")

    for p in parts:
        if isinstance(p, StickerPart):
            sid = p.sticker_id or p.src or "未知"
            notes.append(_PLACEHOLDER_WORDING["sticker"][idx].format(sid=sid))
        elif isinstance(p, FilePart):
            name = p.filename or (f"id={p.attachment_id}" if p.attachment_id else "未命名文件")
            size = _format_size(p.size_bytes)
            notes.append(_PLACEHOLDER_WORDING["file"][idx].format(
                name=name, size=f"（{size}）" if size else ""))
        elif isinstance(p, AudioPart):
            notes.append(_PLACEHOLDER_WORDING["audio"][idx].format(
                mime=p.mime.value if hasattr(p.mime, "value") else p.mime,
                dur=_duration_s(p.duration_ms)))
        elif isinstance(p, VideoPart):
            notes.append(_PLACEHOLDER_WORDING["video"][idx].format(
                mime=p.mime.value if hasattr(p.mime, "value") else p.mime,
                dur=_duration_s(p.duration_ms)))

    return "\n".join(notes)


def _history_message_content(msg: Message, *, supports_multimodal: bool) -> dict[str, object] | None:
    """把单条历史消息转换为 OpenAI message content；tool 消息返回 None。"""
    if msg.role == "tool":
        return None

    inner_voice = getattr(msg, "inner_voice", None)
    iv_prefix = f"<inner_voice>{inner_voice}</inner_voice>\n" if inner_voice else ""

    tool_calls = getattr(msg, "tool_calls", None)
    tool_summary = _format_tool_summary(tool_calls) if tool_calls else ""

    parts = msg.content  # list[Part]
    if supports_multimodal:
        msg_content = content_to_openai(list(parts))
        # 多模态：图片已随 image_url 发出，但 sticker/file/audio/video 仍发不出去
        # ——补占位，否则这类历史同样在上下文里"消失"（S2-H8 历史侧）。
        media_note = _media_placeholder(parts, images_included=True, historical=True)
        if media_note:
            if isinstance(msg_content, list):
                msg_content.append({"type": "text", "text": media_note})
            else:
                msg_content = (msg_content + media_note) if msg_content else media_note
        if tool_summary:
            if isinstance(msg_content, list):
                msg_content.append({"type": "text", "text": tool_summary})
            else:
                msg_content = msg_content + tool_summary
        if iv_prefix:
            if isinstance(msg_content, list):
                msg_content.insert(0, {"type": "text", "text": iv_prefix.rstrip()})
            else:
                msg_content = iv_prefix + msg_content
        return {"role": msg.role, "content": msg_content}

    text_only = [p for p in parts if isinstance(p, TextPart)]
    text_content = "".join(p.content for p in text_only)
    # 图片与其它媒体统一走占位（图片沿用既有"曾发送 N 张图片"文案）
    media_note = _media_placeholder(
        parts, images_included=False, historical=True) if msg.role == "user" else ""
    if media_note:
        sep = "" if media_note.endswith(" ") else "\n"
        text_content = f"{media_note}{sep}{text_content}" if text_content else media_note.rstrip()
    if not text_content and not tool_summary:
        return None
    msg_content = (text_content or "") + (tool_summary or "")
    return {"role": msg.role, "content": iv_prefix + msg_content}


def _current_user_message(
    user_content: str | list[dict],
    *,
    supports_multimodal: bool,
) -> dict[str, object] | None:
    """把当前用户消息转换为 OpenAI message；空文本返回 None。"""
    if isinstance(user_content, list):
        user_parts = parts_from_dicts(user_content)
        has_images = any(isinstance(p, ImagePart) for p in user_parts)
        if has_images and not supports_multimodal:
            text_content = content_text(user_parts)
            media_note = _media_placeholder(
                user_parts, images_included=False, historical=False)
            user_text = f"{media_note}\n{text_content}" if text_content else media_note
            return {"role": "user", "content": user_text}
        # 多模态（或本条没有图片）：正常转换 + 为发不出去的块补占位。
        # 关键修复（S2-H8）：sticker/file/audio/video 此前被转换器静默丢弃，
        # 仅含这类块的消息会产出 `content: []`（模型完全看不到）。
        payload = content_to_openai(user_parts)
        media_note = _media_placeholder(
            user_parts, images_included=supports_multimodal, historical=False)
        if not media_note:
            return {"role": "user", "content": payload}
        if isinstance(payload, str):
            text = f"{media_note}\n{payload}" if payload else media_note
            return {"role": "user", "content": text}
        return {"role": "user",
                "content": [*payload, {"type": "text", "text": media_note}]}
    if user_content:
        return {"role": "user", "content": user_content}
    return None


class MessageBuilder:
    """纯 prompt 组装 — 接收 system prompt + history → 输出 messages[]。

    无状态，依赖 core.schema 类型（Message, Part, ToolCall, adapters）。
    """

    def build_messages(
        self,
        system_prompt: str,
        history: list[Message],
        user_content: str | list[dict] | None = None,
        *,
        supports_multimodal: bool = False,
        prompt_budget_tokens: int | None = None,
        model_id: str | None = None,
        tools: list[dict] | None = None,
    ) -> list[dict]:
        """组装完整 messages[]。

        Args:
            system_prompt: 已渲染的 system prompt 字符串
            history: SessionMemory.recent_messages(8) 返回的 Message 列表
            user_content: 当前用户消息（str 或多模态 content list）
            supports_multimodal: LLM 是否支持多模态图片
            prompt_budget_tokens: prompt 总预算（token，S2-H9）。None = 不做预算
                （预览等拿不到模型上下文的调用点）。超预算时**由旧到新丢历史**；
                当前用户消息与 system 永不丢。
            model_id: 已解析模型 id —— 决定估算 profile（Batch 18）。估算权威仍是
                `Module.system_prompt.budget`；这里只透传，不做第二套算法。
            tools: 同一次 provider request 的 `tools=[...]`（Batch 18）。**必须计入
                总量**：它和 messages 一起占窗口，漏算就是 false accept。

        Returns:
            [{"role": "system", "content": system_prompt},
             ...history with inner_voice + tool_summary injection,
             {"role": "user", "content": user_content}]
        """
        messages: list[dict[str, object]] = [{"role": "system", "content": system_prompt}]
        for msg in history:
            entry = _history_message_content(msg, supports_multimodal=supports_multimodal)
            if entry is not None:
                messages.append(entry)

        if user_content is not None:
            entry = _current_user_message(user_content, supports_multimodal=supports_multimodal)
            if entry is not None:
                messages.append(entry)

        if prompt_budget_tokens is not None:
            _apply_prompt_budget(messages, prompt_budget_tokens,
                                 model_id=model_id, tools=tools)
        return messages


def _format_tool_summary(tool_calls: list[ToolCall]) -> str:
    """将工具调用列表格式化为 LLM 上下文摘要。

    Args:
        tool_calls: ToolCall 对象列表。

    Returns:
        str: 格式化后的摘要字符串（如 " [已调用: search(query=...)]"），空列表时返回空字符串。
    """
    if not tool_calls:
        return ""
    names: list[str] = []
    for tc in tool_calls:
        name = tc.function.name
        try:
            parsed = json.loads(tc.function.arguments)
        except (json.JSONDecodeError, TypeError):
            parsed = None
        # arguments 可能是合法 JSON 但非 dict（list/str/int），跳过避免 AttributeError
        args = parsed if isinstance(parsed, dict) else {}
        arg_strs = [f"{k}={v}" for k, v in args.items()]
        if arg_strs:
            names.append(f"{name}({', '.join(arg_strs)})")
        else:
            names.append(name)
    return f" [已调用: {', '.join(names)}]"


def _format_image_history_hint(count: int) -> str:
    """历史用户消息中图片的降级提示。

    Args:
        count: 图片数量。

    Returns:
        str: 降级提示文本（如 "[用户曾发送一张图片] "）。
    """
    if count == 1:
        return "[用户曾发送一张图片] "
    return f"[用户曾发送{count}张图片] "


def _apply_prompt_budget(messages: list[dict], budget_tokens: int, *,
                         model_id: str | None = None,
                         tools: list[dict] | None = None) -> None:
    """超预算时由旧到新丢历史（**原地**修改 messages）。

    位置在**组装完成之后**：只有拿到全部消息才算得准总量（inner_voice 注入、
    工具摘要、媒体占位都会改变长度）。

    Batch 18：总量 = messages **+ tools**（`estimate_messages_tokens` 一次算全），
    因为 provider request 是两者一起发的；只裁 messages 而不计 tools 会系统性漏算。

    不变式：
    - system 恒为 `[0]`、当前用户消息恒为最后一条，两者都不参与丢弃；
    - 由旧到新**逐条**丢（不是整段清空）：丢到装得下为止，尽量保住近轮；
    - 不需要"assistant tool_calls 与 tool 结果成对"——转换器根本不发射工具流量
      （`role="tool"` 返回 None、assistant 不带 `tool_calls` 键），该前提由
      `core/prompt/tests/test_prompt_budget.py::TestToolTrafficPremise` 钉住；
    - 逼近预算（≥80%）给 info 级观测（报告要求"上下文快满了"要有提示），
      真截断给 warning 并记前后估算；截尽历史仍超预算**不静默**——如实报出剩余
      超限量（system 侧的截断由 SystemPromptManager 负责）。
    """
    from Module.system_prompt.budget import estimate_messages_tokens

    total = estimate_messages_tokens(messages, model=model_id, tools=tools)
    if total <= budget_tokens:
        if total >= budget_tokens * 0.8:
            logger.info(
                "prompt 接近预算：估算 %d / 预算 %d token（历史 %d 条）",
                total, budget_tokens, max(0, len(messages) - 2))
        return

    dropped = 0
    while len(messages) > 2 and estimate_messages_tokens(
            messages, model=model_id, tools=tools) > budget_tokens:
        del messages[1]          # 恒删"最旧的历史"（[0]=system，[-1]=当前消息）
        dropped += 1

    after = estimate_messages_tokens(messages, model=model_id, tools=tools)
    logger.warning(
        "prompt 超预算：估算 %d > 预算 %d token → 由旧到新丢弃 %d 条历史"
        "（消息 %d 条，截后估算 %d）",
        total, budget_tokens, dropped, len(messages), after)
    if after > budget_tokens:
        logger.warning(
            "截尽历史后仍超预算 %d token（system prompt 或当前消息自身过长）",
            after - budget_tokens)


# ═══════════════════════════════════════════════════════════════════
# 回合内重预算（KB-77 / Batch 19）
# ═══════════════════════════════════════════════════════════════════
#
# `_apply_prompt_budget` 只覆盖**回合开始时的第一次组装**（system + 历史 + 当前消息）。
# 但 ReAct 的工具轮会**原地**往同一个 `messages` 数组追加内容（assistant tool_calls +
# `role="tool"` 结果 + 轮次标记），而 `CognitiveLoop` 每轮都把整个数组重发给 provider
# ⇒ 第 2..N 轮**完全不在预算内**。实测（Qwen2.5-3b / 8192 窗口 / 6144 预算）：
# 第 1 轮 6123（预算内），追加一条接近单条上限（`max_result_tokens = 2000`）的工具
# 结果后第 2 轮 **8205 > 8192 窗口**，第 5 轮 16533。Ollama 超窗是 **HTTP 200 +
# 静默截断**（Batch 17 实测），故这是 silent loss，不是 provider 报错。
#
# 为什么放在本模块：裁剪优先级必须与 `_apply_prompt_budget` **同一处**（单一权威），
# 且估算仍走 `Module.system_prompt.budget`（TE1：估算只有一个权威）。

# 预算驱动的工具结果截断标记（与 `ToolResult.llm_content` 的既有标记同风格：
# 告诉模型"内容被截了、可以用更精确的参数重试"，而不是让它以为结果就这么短）。
_TOOL_BUDGET_TRUNCATION_NOTICE = "\n[结果超出本轮上下文预算，已截断；如需完整数据请用更精确的参数重试]"
# 二级回退：预算紧到连说明都装不下时用它（仍告诉模型"这里被截了"）
_TOOL_BUDGET_TRUNCATION_MARK = "[已截断]"


@dataclass(frozen=True)
class RoundBudgetOutcome:
    """一次回合内重预算的结果（供调用方留痕/守卫断言）。"""

    budget: int
    before: int
    after: int
    dropped_history: int = 0
    dropped_exchanges: int = 0
    truncated_results: int = 0

    @property
    def trimmed(self) -> bool:
        return bool(self.dropped_history or self.dropped_exchanges or self.truncated_results)

    @property
    def over_budget(self) -> bool:
        """尽最大努力后仍超预算——受保护内容（system/tools/当前消息）自身过长。"""
        return self.after > self.budget


def _message_costs(messages: list[dict], *, model_id: str | None) -> list[int]:
    """逐条消息的估算成本。

    `estimate_messages_tokens` 是**逐条可加**的（每条：固定开销 + role + content +
    tool_calls），故"整份估算 == 逐条之和 + tools 段"。这里利用该性质把裁剪循环从
    O(n²) 次全量估算降到 O(n) 次逐条估算（热路径：每轮工具往返都要跑）。
    """
    from Module.system_prompt.budget import estimate_messages_tokens

    return [estimate_messages_tokens([msg], model=model_id) for msg in messages]


def _entry_cost(entry: dict, model_id: str | None) -> int:
    """单条消息的估算成本（`estimate_messages_tokens` 逐条可加）。"""
    from Module.system_prompt.budget import estimate_messages_tokens

    return estimate_messages_tokens([entry], model=model_id)


def turn_tail_start(messages: list[dict]) -> int:
    """回合内容（当前用户消息及其后的工具往返）在 `messages` 中的起点下标。

    历史里也会有 user 消息，但**最后一条** user 必然是当前这一条——其后的条目只会是
    工具往返与轮次标记（首轮组装把当前消息放在末尾；工具轮只追加 assistant/tool/system）。
    该轮没有用户条目时（空文本多模态）退化为"保护最后一条"。
    """
    for i in range(len(messages) - 1, 0, -1):
        if messages[i].get("role") == "user":
            return i
    return max(1, len(messages) - 1)


def _is_turn_local_system(msg: dict) -> bool:
    """回合内容区里的 system 条目（轮次标记）。

    `MessageBuilder` 只在 `[0]` 放 system，故回合区内的 system 只可能由
    `CognitiveLoop` 在工具轮后追加。**刻意不做文本匹配**——那会把两个模块用一句
    提示词耦合起来，改文案就静默失效。
    """
    return msg.get("role") == "system"


def _exchange_spans(messages: list[dict], protect_from: int) -> list[tuple[int, int]]:
    """回合区内的工具往返组：`(起始下标, 结束下标)`（左闭右开）。

    一组 = 带 `tool_calls` 的 assistant 消息 + **紧随其后**的全部 `role="tool"` 结果
    + 可选的一个轮次标记（system）。判据是结构性的：只有"assistant(tool_calls) 后面
    直接跟至少一条 tool 结果"才算一组——**孤立的 tool 条目永不成组**（宁可不裁，
    也不制造 OpenAI wire 上的孤儿 tool 消息 ⇒ provider 400）。
    """
    spans: list[tuple[int, int]] = []
    i = max(1, protect_from)
    while i < len(messages):
        msg = messages[i]
        if msg.get("role") == "assistant" and msg.get("tool_calls"):
            j = i + 1
            while j < len(messages) and messages[j].get("role") == "tool":
                j += 1
            if j > i + 1:
                if j < len(messages) and _is_turn_local_system(messages[j]):
                    j += 1
                spans.append((i, j))
                i = j
                continue
        i += 1
    return spans


def _shrink_tool_entry(entry: dict, allowance: int, *, model_id: str | None) -> tuple[bool, int]:
    """把一条 `role="tool"` 条目的 content 头部截到约 `allowance` token。

    标记按"信息量递减"三级回退（完整说明 → 短标记 → 空内容）：预算极紧时先牺牲说明
    文字，而不是整条结果的结构——`role="tool"` 与 assistant `tool_calls` 的配对必须
    保住（孤儿 tool 消息会让严格 provider 直接 400）。

    Returns:
        (是否改动, 改动后的单条成本)
    """
    content = entry.get("content")
    if not isinstance(content, str) or not content:
        return False, 0

    cost = _entry_cost(entry, model_id)
    if cost <= allowance:
        return False, cost

    original = content
    for marker in (_TOOL_BUDGET_TRUNCATION_NOTICE, _TOOL_BUDGET_TRUNCATION_MARK, ""):
        # 比例外推给出首猜，再用真实估算收敛（估算按字符类加权，纯比例会有偏差）。
        chars = max(0, int(len(original) * max(0, allowance - 8) / max(1, cost)))
        while True:
            entry["content"] = original[:chars] + marker
            new_cost = _entry_cost(entry, model_id)
            if new_cost <= allowance:
                return True, new_cost
            if chars == 0:
                break
            chars = max(0, int(chars * 0.7))

    # 连"空内容 + 骨架"都装不下：保留最短形态，由调用方如实报 over_budget。
    entry["content"] = ""
    return True, _entry_cost(entry, model_id)


def enforce_round_budget(
    messages: list[dict],
    budget_tokens: int,
    *,
    turn_start: int,
    model_id: str | None = None,
    tools: list[dict] | None = None,
) -> RoundBudgetOutcome:
    """回合内（ReAct 第 2..N 轮）重新施加同一份 canonical 预算（**原地**修改）。

    与 `_apply_prompt_budget` 共用估算权威与"由旧到新"的裁剪优先级，但保护集更宽：
    回合内容（当前用户消息及其后的工具往返）是**这一轮正在发生的事**，丢它等于把
    工具调用变成没发生过。裁剪顺序（从最可丢到最不可丢）：

    1. **历史**：`messages[1:turn_start]`，由旧到新逐条丢（与首轮完全一致）；
    2. **较早的工具往返组**：整组丢（assistant tool_calls + 其 tool 结果 + 轮次标记）
       ——整组丢是为了不产生孤儿 `role="tool"`（那会让严格 provider 直接 400）；
    3. **最新一组里的工具结果**：按"由旧到新"做头部截断（保留配对结构，只改 content），
       并附上截断标记让模型知道可以用更精确的参数重试。

    永不丢：`messages[0]`（system）、当前用户消息、最新工具往返组的 assistant 条目与其
    tool 配对的**结构**。三档用尽仍超预算时**如实**在结果里报出（`over_budget`），
    由调用方留痕——system prompt + tools 自身超预算是首轮就存在的情形，不在本函数职责内。

    Args:
        messages: 当前回合的完整 messages（原地修改）。
        budget_tokens: 与首轮**同一份**预算（`budget_from_context_window(...)`）。
        turn_start: 回合内容起点下标（当前用户消息；无用户条目时退化为 protections 起点）。
        model_id: 已解析模型 id（估算 profile，与首轮同一份）。
        tools: 同一次请求的 tools（不变，只计入总量）。

    Returns:
        `RoundBudgetOutcome`。
    """
    from Module.system_prompt.budget import estimate_tools_tokens

    tools_cost = estimate_tools_tokens(tools, model=model_id)
    costs = _message_costs(messages, model_id=model_id)
    before = tools_cost + sum(costs)
    budget = max(1, budget_tokens)
    if before <= budget:
        return RoundBudgetOutcome(budget=budget, before=before, after=before)
    original = before

    protected_from = min(max(1, turn_start), max(1, len(messages) - 1))
    dropped_history = dropped_exchanges = truncated = 0

    # ① 历史：由旧到新（与首轮同优先级）
    while before > budget and protected_from > 1:
        before -= costs.pop(1)
        del messages[1]
        protected_from -= 1
        dropped_history += 1

    if before > budget:
        protected_from = min(protected_from, max(1, len(messages) - 1))

    # ② 较早的工具往返组：整组丢（保留最新一组）
    while before > budget:
        spans = _exchange_spans(messages, protected_from)
        if len(spans) <= 1:
            break
        start, end = spans[0]
        for idx in range(end - 1, start - 1, -1):
            before -= costs.pop(idx)
            del messages[idx]
            if idx < protected_from:
                protected_from -= 1
        dropped_exchanges += 1

    # ③ 最新一组：截 tool 结果（结构不动，只改 content）
    if before > budget:
        spans = _exchange_spans(messages, protected_from)
        if spans:
            start, end = spans[-1]
            for idx in range(start + 1, end):
                if before <= budget:
                    break
                entry = messages[idx]
                if entry.get("role") != "tool":
                    continue
                allowance = max(0, costs[idx] - (before - budget))
                changed, new_cost = _shrink_tool_entry(entry, allowance, model_id=model_id)
                if changed:
                    before += new_cost - costs[idx]
                    costs[idx] = new_cost
                    truncated += 1

    outcome = RoundBudgetOutcome(
        budget=budget, before=original, after=before,
        dropped_history=dropped_history, dropped_exchanges=dropped_exchanges,
        truncated_results=truncated)
    if outcome.trimmed:
        logger.warning(
            "回合内 prompt 超预算：估算 %d > 预算 %d token → 丢历史 %d 条 / 丢工具往返 %d 组 "
            "/ 截断工具结果 %d 条（截后估算 %d）",
            original, budget, dropped_history, dropped_exchanges, truncated, before)
    if outcome.over_budget:
        logger.error(
            "回合内预算用尽仍超 %d token（system prompt / tools / 最新工具往返自身过长）",
            before - budget)
    return outcome
