"""流式结构化块过滤器 — 阻止代码/markdown 块进入 TTS。

从 cognitive_loop.py 提取。_BlockFilter 在 LLM 流式响应中
实时过滤 <|markdown|> 和 <|code|> 标记，确保 TTS 只朗读对话文本。
build_parts_with_blocks 在流式结束后从完整文本中解析块结构，
用于 session 存储和前端渲染。
"""

from __future__ import annotations

import re

# ---- 正则常量 ----

_BLOCK_PATTERN: re.Pattern = re.compile(
    r'<\|markdown\|>(.*?)</\|markdown\|>|'
    r'<\|code\|(\w+)?>(.*?)</\|code\|>',
    re.DOTALL,
)

_BLOCK_START_RE: re.Pattern = re.compile(r'<\|(markdown|code)(?:\|(\w*))?\|?>')

# 最长标记 "</|markdown|>" 为 13 字符，流式处理时保留末尾这么多以防截断
_BLOCK_LOOKBEHIND = 13


# ---- BlockFilter ----

class BlockFilter:
    """流式过滤结构化块标记，阻止代码/markdown 内容进入 TTS。

    用法:
        bf = BlockFilter()
        for chunk in llm_stream:
            clean = bf.feed(chunk)   # 返回可安全送 TTS/分句的文本
            ...
        trailing = bf.flush()        # 清空缓冲区
    """

    def __init__(self) -> None:
        self._buf = ""
        self._depth = 0

    def feed(self, chunk: str) -> str:
        """喂入一个 chunk，返回可以去 TTS/分句的干净文本。"""
        self._buf += chunk
        out: list[str] = []
        i = 0

        while i < len(self._buf):
            if self._depth > 0:
                # 在块内 — 找结束标记
                end_md = self._buf.find("</|markdown|>", i)
                end_code = self._buf.find("</|code|>", i)
                candidates = []
                if end_md >= 0:
                    candidates.append(end_md)
                if end_code >= 0:
                    candidates.append(end_code)
                if candidates:
                    end_pos = min(candidates)
                    end_len = (
                        len("</|markdown|>")
                        if self._buf.startswith("</|markdown|>", end_pos)
                        else len("</|code|>")
                    )
                    i = end_pos + end_len
                    self._depth -= 1
                else:
                    # 没有结束标记 — 保留末尾以防截断
                    keep = min(len(self._buf) - i, _BLOCK_LOOKBEHIND)
                    i = len(self._buf) - keep
                    break
            else:
                # 不在块内 — 找开始标记
                m = _BLOCK_START_RE.search(self._buf, i)
                if m:
                    out.append(self._buf[i:m.start()])
                    i = m.end()
                    self._depth += 1
                else:
                    # 没有开始标记 — 安全地输出大部分，保留末尾防截断
                    safe = len(self._buf) - _BLOCK_LOOKBEHIND
                    if safe > i:
                        out.append(self._buf[i:safe])
                        i = safe
                    break

        self._buf = self._buf[i:]
        return "".join(out)

    def flush(self) -> str:
        """清空缓冲区，返回剩余的未被标记包裹的文本。"""
        result = self._buf
        self._buf = ""
        self._depth = 0
        # 去掉可能残留的未闭合标记
        cleaned = _BLOCK_START_RE.sub("", result)
        return cleaned


# ---- 块解析 ----

def build_parts_with_blocks(
    acc_text: str, sentences: list[str],
) -> tuple[list[dict], dict[str, list[int]]]:
    """从 LLM 输出文本中解析结构化块标记，构建 parts 和 content_type。

    对话文本的句子作为 text parts，标记包裹的内容作为 markdown/code parts，
    追加在句子之后。

    Args:
        acc_text: LLM 输出的完整文本（含块标记）
        sentences: 已分句的纯对话文本列表

    Returns:
        (parts, content_type)
        parts: [{"content": str, "language"?: str}, ...]
        content_type: {"markdown": [idx, ...], "code": [idx, ...]}
    """
    parts: list[dict] = [{"content": s} for s in sentences]
    content_type: dict[str, list[int]] = {"markdown": [], "code": []}

    if "<|markdown|>" not in acc_text and "<|code|" not in acc_text:
        return parts, content_type

    for m in _BLOCK_PATTERN.finditer(acc_text):
        md_content = m.group(1)
        if md_content is not None:
            content_type["markdown"].append(len(parts))
            parts.append({"content": md_content.strip()})
        else:
            lang = m.group(2) or ""
            code_content = m.group(3)
            content_type["code"].append(len(parts))
            p: dict = {"content": code_content.strip()}
            if lang:
                p["language"] = lang
            parts.append(p)

    return parts, content_type
