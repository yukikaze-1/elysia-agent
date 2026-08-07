"""parts 构建 — 从句子列表 + 块列表交错出 TextPart + content_type。

替代旧组件按正则重解析块列表的路径：
不再扫描 acc_text，改由调用方（协调器）传入已按 `after_sentence_idx` 定位的
块列表。输出形状不变——TextPart 列表 + content_type 索引，session 存储/归档
不受影响（spec 迁移计划 step 4）。

对齐设计文档 `docs/superpowers/specs/2026-08-01-stream-parser-refactor-design.md`
「事件面」一节 after_sentence_idx 交错语义与测试策略 #9/#12。
"""

from __future__ import annotations

from collections.abc import Iterable

from core.schema.part import TextPart
from core.text.tags import strip_display_hidden
from core.text.tokenizer import Token, TokenKind


# 块引用：(kind, content, lang, after_sentence_idx)
#   kind               = "markdown" | "code"（块类型）
#   content            = 块内容（已去 <|code|>/``` 标记，tokenizer 产出的原文）
#   lang               = 语言标记（围栏信息串首 token / <|code|lang>；显示路径用）
#   after_sentence_idx = 块闭合时已"完成"的句子数——含 min_chunk 暂留的短句
#                        与 max_buffer_length 强制切分发出的句子（对齐 splitter
#                        completed_count）。块应插在第 after_sentence_idx 个
#                        句子之后；语义见 build_parts 文档。
BlockRef = tuple[str, str, str, int]


def build_parts(
    all_sentences: list[str],
    blocks: list[BlockRef],
) -> tuple[list[TextPart], dict[str, list[int]]]:
    """按 after_sentence_idx 把块交错进句子序列，构建 TextPart + content_type。

    Args:
        all_sentences: 本轮全部句子（含内嵌 PAUSE/STAGE_CN 的原文文本，
            来自 splitter 的已发出句子列表）。
        blocks: 块列表，按 token 出现顺序。

    Returns:
        (parts, content_type) 元组：
        - parts: TextPart 列表，句子与块交错排列。
        - content_type: {"markdown": [...], "code": [...]}，
          值为对应类型块在 parts 中的索引列表（恒含两键）。

    交错语义（对齐 spec「事件面」+ 测试策略 #9）：
    - 块插入点 = 第 after_sentence_idx 个句子之后（after_sentence_idx=0 →
      首句之前；≥ len(all_sentences) → 追加末尾）。
    - 每块的位置相对**原始句子列表**计算，不做插入后重索引：多个块落在同一
      插入点时，按 blocks 列表顺序排列（同槽块 = 按闭合顺序）。
    - 块内容对齐旧 `_parse_blocks` 的 `.strip()` 语义（去首尾空白，内部结构保留）。

    显示剥离（对齐 spec「显示路径」）：持久化 TextPart 是前端气泡/历史的消息文本，
    经 `strip_display_hidden` 剥 PAUSE `<#x#>` + 英文语气词 `(sighs)`（STAGE_CN 保留）。
    SENTENCE 事件（供 TTS）由 splitter 直接产出、保留 PAUSE/语气词——两条路径分离。
    """
    content_type: dict[str, list[int]] = {"markdown": [], "code": []}

    if not blocks:
        return (
            [
                TextPart(content=strip_display_hidden(sentence))
                for sentence in all_sentences
            ],
            content_type,
        )

    n = len(all_sentences)
    # slots[k] = 插在"第 k 个句子之后"的块（k=0 → 首句之前；k=n → 末尾）。
    # 钳制 after_sentence_idx 到 [0, n]：≥ n 追加末尾；负数防御（正常为计数 ≥ 0，
    # 但负数会落入 Python 负索引，静默错插，故钳到 0 = 首句之前）。
    slots: list[list[BlockRef]] = [[] for _ in range(n + 1)]
    for block in blocks:
        slots[min(max(block[3], 0), n)].append(block)

    parts: list[TextPart] = []

    # after_sentence_idx=0 的块（闭合时无已发出句子）落在首句之前。
    for kind, content, _lang, _after in slots[0]:
        content_type[kind].append(len(parts))
        parts.append(TextPart(content=content.strip()))

    for k in range(n):
        parts.append(TextPart(content=strip_display_hidden(all_sentences[k])))
        for kind, content, _lang, _after in slots[k + 1]:
            content_type[kind].append(len(parts))
            parts.append(TextPart(content=content.strip()))

    return parts, content_type


def rebuild_public_text(tokens: Iterable[Token]) -> str:
    """按 token 流顺序重建 public_text（去 META，保留 PAUSE/STAGE_CN/块内容）。

    对齐 spec 279 行「SPEECH + PAUSE + STAGE_CN + 代码块内容，无 META」：
    - SPEECH 文本原样；
    - PAUSE `<#0.5#>` 原样保留（public_text 只去 META 与代码块标记）；
    - STAGE_CN（中文舞台指示）原样保留；
    - CODE/MARKDOWN 块内容（tokenizer 已去标记）按 token 序插回原文位置——
      句中块如 `看这个<|code|>x=1</|code|>示例` 重建为原文顺序；
    - META（<mood>/<inner_voice>）剔除。

    与 build_parts 的 after_sentence_idx 交错是两条独立路径：本函数按 token
    原文位置重建，供 analyze_assistant / log_conversation / after_response。
    排除 META（mood/inner_voice）+ ANGLE_EN（尖括号违规格式，全链路丢弃）。
    PAUSE 与 STAGE_EN（圆括号语气词）保留——与 PAUSE 同为 TTS 标记，供分析文本。
    """
    return "".join(
        tok.text
        for tok in tokens
        if tok.kind not in (TokenKind.META, TokenKind.ANGLE_EN)
    )
