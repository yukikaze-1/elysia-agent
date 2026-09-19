"""prompt 预算：估算与截断策略（S2-H9 的**唯一执行点**）。

## 为什么这个模块在 `Module/` 而不是 `core/`

两个消费方分属两层：`Module/system_prompt/system_prompt_manager.py`（section 级裁剪）
与 `core/prompt/*`（历史裁剪）。而架构门禁 R1 禁止 `Module/` 生产代码依赖 `core/`
（实测：把它放 core 会让 `make check-arch` 红），故共享判据留在 Module 侧，
core 反向引用（core → Module 是允许的方向）。

## 为什么需要

组装后的 prompt 此前**没有任何总长/预算/截断**（`grep max_prompt|prompt_budget|
context_window` 在 core/ 与 Module/llm/ 下 0 命中）。超 context 不是被截断而是变成
provider **400**，而 400 不在可重试集合里 ⇒ 该会话对该角色**永久失败**，日志还只显示
`GENERATION_PROVIDER_UNAVAILABLE`。触发面很现成：`save_memory` 与
`LongTermMemory.insert` 都不校验长度，角色档案上限本身很大（corePersonality 12000 +
relationshipStyle 6000 + …），空数据下 system prompt 已 ≈6.5K 字符。

## 预算从哪来

`providers.yaml::llm.<provider>.context_window`（模型上下文，单位 token）→ 乘
`CONTEXT_HEADROOM_RATIO` 留余量（输出 token、估算误差都吃这份余量）。
未声明时用 `DEFAULT_CONTEXT_WINDOW`（保守值）并打一次 warning。

**Batch 17 语义（不变）**：`context_window` = **effective runtime window**；
Ollama 侧同一个值经 native `/api/chat` 的 `options.num_ctx` 下发。本模块只管预算，
不知道也不得知道 `num_ctx` / `/api/ps` / 显存。

## 截断优先级（owner 2026-09-16 裁定）

    F1/F2 永不截断 → C4 记忆逐条截 → 历史由旧到新裁

- **system prompt 侧**（`SystemPromptManager`）：先减 C4 记忆条数，再按
  `DROPPABLE_SECTIONS` 顺序整节丢弃；`PROTECTED_SECTIONS` 里的节**永不**丢
  （冲突规则/输出格式/TTS 标记/人格/关系/说话风格/主动性——它们是"她是谁、
  怎么说话"的契约，丢了就不是同一个角色了）。
- **历史侧**（`MessageBuilder`）：**由旧到新逐条丢**（丢到装得下为止）。
- **当前用户消息永不丢**：它是这一轮的输入，丢了等于这一轮没发生。

### 为什么历史侧不需要"成对"约束（一条实测过的负结论）

报告曾要求"assistant `tool_calls` 与 `role=tool` 结果必须成对丢弃，否则拆开就是 400"。
**实测不成立**：`_history_message_content` 对 `role="tool"` 直接返回 None（不发射），
assistant 消息也只带 `content`+工具摘要文本、**不带 `tool_calls` 键**——工具流量被
汇总成 `"[已调用: web_search]"` 这样的一行文本进 prompt。故 messages 里既没有
`role="tool"` 也没有 `tool_calls`，不存在可拆的对。

这个前提**不是靠记性维持**：`core/prompt/tests/test_prompt_budget.py::TestToolTrafficPremise`
把它钉成断言——哪天有人开始发射工具流量，那条用例会红，并在消息里指回本节
（届时必须实现成对截断）。写一个"永远配不到对"的成对逻辑才是真风险：它看起来像守卫，
实际上一次都不会触发。

## 估算的边界（Batch 18 / KB-12 重写：诚实声明）

**没有 tokenizer，也不会有**：不引入 `tiktoken`/`transformers`/`tokenizers`，不下载
vocab，不在热路径问 provider 要 token 数（§59–§63）。故这里给的是
**有界近似（bounded approximation）**，不是精确计数：

    estimate ≈ actual × [1/M, T]      按 **resolved model 选定的 profile** 保证

- `M`（不得低估的倍数）：硬下限 **0.75**，拟合目标 **0.80**，实测最坏 **0.794**。
  为什么下限是 0.75：chat 侧只有 `budget = window × 0.75` 这一层保护——预算判
  "装得下"而真实 prompt 超窗时，provider 是 **HTTP 200 + 静默截断**（Batch 17 实测；
  Ollama 从不为超窗报 400）。故**低估 > 25% 就是实质 false accept**。
- `T`（最坏高估倍数）：profile 相关（见下表）。高估不是"安全"：它会过早裁掉
  记忆/历史、并让预览误拒（§9）。故 T 也是被约束的量，本模块报告它。

### 两个 profile（按 resolved model 的**单一**判定点选择）

| profile | 覆盖 | 依据（真 tokenizer） | M | T |
|---|---|---|---|---|
| `cjk_opt` | 模型名含 `qwen` / `deepseek` | Qwen2.5-3B（HF）+ Ollama `prompt_eval_count` 实测、DeepSeek-V3 tokenizer | 0.79 | **1.26** |
| `generic` | 其余一切（含未知/`custom`） | Llama-3-8B、Mistral-7B-v0.3、`o200k_base`、`cl100k_base` | 0.80 | **1.71** |

为什么必须按模型分 profile（一条**被证伪过的**单公式假设）：同一段中文在不同
tokenizer 下每字符 token 数相差 **3.6×**（DeepSeek-V3 0.58 / Qwen2.5 0.64 /
Llama-3 0.86 / o200k 0.84 / Mistral 1.14 / cl100k 1.21 / p50k 2.12）。用单一系数
同时满足"不低估任何一家"，最坏高估会被顶到 **1.72–1.83×**（Batch 18 用线性规划实测，
见 `docs/review/2026-09-19-Batch18-*` 执行记录）。分 profile 后，本仓**默认的
`deepseek/*`** 与本地 `ollama/qwen2.5*` 拿到紧界（≤1.22），未知模型拿到保守回退。

### 系数怎么来的（不是手调 magic number）

9 个字符类 × 2 个 profile，由 `tests/fixtures/token_corpus/` 的签入语料对
**7 个权威 tokenizer** 做 min-max 线性规划求得（min 最大化安全裕度 / 目标为最小化
最坏高估），并在 holdout 语料上验证。改系数必须重跑该流程，否则
`test_token_corpus_contract.py` 的契约断言会红。

### 明确不在本批范围

- **图片 token**：`image_url` 仍按固定 1024 计（未验证的上界，独立 finding）。
- **输出/推理 token**：由 `CONTEXT_HEADROOM_RATIO` 的 25% 覆盖，本模块只估输入。
- 精确 tokenizer（依赖/体积/离线可用性门槛，见执行记录 §dependency）。
"""

from __future__ import annotations

import json
import logging
import math
from typing import Any

logger = logging.getLogger(__name__)

# 未声明 context_window 时的保守默认（token）。取 32K：足以装下 system prompt +
# 若干轮历史，又不至于在 8K/16K 的本地模型上必然超限。
DEFAULT_CONTEXT_WINDOW: int = 32768

# 预算占上下文窗口的比例：留 25% 给输出 token 与估算误差。
CONTEXT_HEADROOM_RATIO: float = 0.75

# system prompt 最多吃掉预算的这一份（其余留给历史 + 当前消息 + 工具定义）。定 0.5 的理由：
# F1/F2/C1 是身份与格式契约必须完整，而它们在空数据下已占 ~3.2K token；给一半
# 意味着"记忆再多也不会把历史挤没"。
SYSTEM_PROMPT_BUDGET_SHARE: float = 0.5

# 每条消息的固定开销（`<|im_start|>` / 分隔符 / `<|im_end|>` 等特殊 token）——
# **不含 role 名与内容**（那两项分别由 `estimate_tokens(role)` 与内容项计入）。
# 实测（Ollama 0.24.0 + qwen2.5:3b，交替 role 的 N 条消息对单条 system 的边际成本）：
# 含 role 与 1-token 内容时 ≈ 5.9/条 ⇒ 扣掉内容与 role ≈ 4。取 4 而非拟合到 4.9
# 的余量留给 OpenAI 风格（≈3–4/条）：4 + role ≈ 5.5–6 是**上界**取向。
_PER_MESSAGE_OVERHEAD_TOKENS: int = 4

# ---------------------------------------------------------------------------
# 字符类（9 类）
# ---------------------------------------------------------------------------

# 统一表意文字（含扩展 A / 兼容区）+ 谚文音节与字母。
_CJK_IDEO_RANGES: tuple[tuple[int, int], ...] = (
    (0x3400, 0x4DBF),    # CJK 扩展 A
    (0x4E00, 0x9FFF),    # CJK 基本区
    (0xF900, 0xFAFF),    # CJK 兼容表意
    (0xAC00, 0xD7AF),    # 谚文音节
    (0x1100, 0x11FF),    # 谚文字母
)

# 假名（日文）单独计：它比汉字合并得更狠（实测 ~2.1 字符/token vs 汉字 ~1.5）。
_KANA_RANGES: tuple[tuple[int, int], ...] = (
    (0x3040, 0x30FF),    # 平假名 / 片假名
)

# 中日韩标点 + 全角形式（U+FF00–FFEF 覆盖全角标点与全角字母数字）。
_CJK_SYMBOL_RANGES: tuple[tuple[int, int], ...] = (
    (0x3000, 0x303F),    # CJK 标点
    (0xFF00, 0xFFEF),    # 全角形式
)

# emoji：主区 + 杂项符号 + 装饰符号 + 变体选择符(16) + ZWJ + 区域指示符。
# ZWJ/VS16 单列进同一类，是因为它们只出现在 emoji 序列里，按"emoji 序列的一个
# 码位"计价与真实计费同向（一个 ZWJ 家庭 emoji 是 5–7 个码位、2–4 个 token）。
_EMOJI_RANGES: tuple[tuple[int, int], ...] = (
    (0x1F000, 0x1F2FF),  # 麻将/多米诺/牌面
    (0x1F300, 0x1FAFF),  # 主 emoji 区（含扩展）
    (0x2600, 0x27BF),    # 杂项符号 + 装饰符号
    (0x2B00, 0x2BFF),    # 杂项符号与箭头
    (0xFE0F, 0xFE0F),    # 变体选择符 16
    (0x200D, 0x200D),    # 零宽连接符
    (0x1F1E6, 0x1F1FF),  # 区域指示符（国旗）
)

_NEWLINE_CHARS = frozenset(" \t\n\r\v\f")

# ---------------------------------------------------------------------------
# profile 系数：token / 字符。见模块 docstring「系数怎么来的」。
# ---------------------------------------------------------------------------

_CLASSES: tuple[str, ...] = (
    "word", "digits", "punct", "space", "cjk", "kana", "cjksym", "emoji", "other",
)

_TOKEN_WEIGHTS: dict[str, dict[str, float]] = {
    # Qwen2.5 / DeepSeek-V3 家族：中文压缩率高（~1.5–1.7 字符/token），数字逐位切分。
    "cjk_opt": {
        "word": 0.2220, "digits": 0.9452, "punct": 0.5766, "space": 0.2330,
        "cjk": 0.5800, "kana": 0.5279, "cjksym": 1.3308, "emoji": 1.5397,
        "other": 0.2883,
    },
    # 其余一切（Llama / Mistral / OpenAI BPE / 未知）：保守回退，T=1.66。
    "generic": {
        "word": 0.2707, "digits": 1.3302, "punct": 0.7018, "space": 0.2108,
        "cjk": 1.1235, "kana": 0.7508, "cjksym": 1.7809, "emoji": 2.6110,
        "other": 0.7970,
    },
}

_PROFILE_CJK_OPT = "cjk_opt"
_PROFILE_GENERIC = "generic"
_DEFAULT_PROFILE = _PROFILE_GENERIC

# 归入 `cjk_opt` 的模型名子串（小写匹配，**唯一的模型判定点**）。
# 只登记**有权威 tokenizer 证据**的家族；新增家族必须同时更新语料契约测试。
# 工具段（tools=[...]）的结构化估算常量——见 `estimate_tools_tokens`。
# 实测（qwen2.5:3b，native /api/chat，对同一请求带/不带 tools 的 prompt_eval_count 差）：
#   工具段整体框架（第一个工具之前的部分）≈ 77 → 取 100（保守）
#   同构工具边际成本 ≈ 45/个（骨架 ≈45 − 该工具自身内容）→ 工具骨架取 10，
#     其余落在"参数骨架 + 内容"两栏
#   参数边际成本 ≈ 15/个 → 参数骨架取 12
#   在仓内 4 组真实/合成 schema 上复核：est/actual ∈ [0.97, 1.24]
_TOOLS_SECTION_BOILERPLATE_TOKENS: int = 100
_TOOL_FRAME_TOKENS: int = 10
_TOOL_PARAM_FRAME_TOKENS: int = 12

_CJK_OPT_NAME_MARKERS: tuple[str, ...] = ("qwen", "deepseek")


def profile_for_model(model: str | None) -> str:
    """把 resolved model 映射到估算 profile（**唯一**的模型判定点）。

    刻意做成一个集中函数而不是散落的 `if "qwen" in model`：§67 要的是
    "判据只有一份"，而不是"不许判"。未知/None 一律回退 `generic`
    （保守 profile）——自定义 provider 不得因为"认不出"而变得不可用或不可信。

    Args:
        model: `provider/model` 形式的已解析模型 id，或 None。

    Returns:
        `_TOKEN_WEIGHTS` 的键（`cjk_opt` / `generic`）。
    """
    if not model:
        return _DEFAULT_PROFILE
    # 只按 **模型段** 匹配，不看 provider 段：`custom/qwen2.5-7b` 也该拿到紧界。
    name = model.rpartition("/")[2].lower()
    if any(marker in name for marker in _CJK_OPT_NAME_MARKERS):
        return _PROFILE_CJK_OPT
    return _DEFAULT_PROFILE


def _classify(char: str) -> str:
    """单个码位 → 字符类名（纯函数，无 locale/归一化依赖）。"""
    if char in _NEWLINE_CHARS:
        return "space"
    if "a" <= char <= "z" or "A" <= char <= "Z" or char == "_":
        return "word"
    if "0" <= char <= "9":
        return "digits"
    code = ord(char)
    if code < 128:
        return "punct"
    if any(lo <= code <= hi for lo, hi in _EMOJI_RANGES):
        return "emoji"
    if any(lo <= code <= hi for lo, hi in _CJK_SYMBOL_RANGES):
        return "cjksym"
    if any(lo <= code <= hi for lo, hi in _KANA_RANGES):
        return "kana"
    if any(lo <= code <= hi for lo, hi in _CJK_IDEO_RANGES):
        return "cjk"
    return "other"


def estimate_tokens(text: str, *, model: str | None = None) -> int:
    """估算一段文本的 token 数（有界近似，见模块 docstring 的合同）。

    单趟 O(n) 分类 + 加权求和；纯函数、确定性、不修改输入、不做 Unicode 归一化。

    Args:
        text: 待估算文本。
        model: 已解析模型 id（`provider/model`）。None → 保守的 `generic` profile。

    Returns:
        估算 token 数（空串为 0，非空至少 1）。
    """
    if not text:
        return 0
    weights = _TOKEN_WEIGHTS[profile_for_model(model)]
    counts: dict[str, int] = dict.fromkeys(_CLASSES, 0)
    for char in text:
        counts[_classify(char)] += 1
    total = 0.0
    for name in _CLASSES:
        n = counts[name]
        if n:
            total += n * weights[name]
    # 向上取整：绝不让"几百字符"这类小量被取整抹成 0（短提示词也因此不为 0）。
    return max(1, math.ceil(total))


def _content_tokens(content: Any, model: str | None) -> int:
    """OpenAI message content 的 token 估算（str 或多模态块列表）。"""
    if isinstance(content, str):
        return estimate_tokens(content, model=model)
    if isinstance(content, list):
        total = 0
        for block in content:
            if not isinstance(block, dict):
                total += estimate_tokens(str(block), model=model)
                continue
            block_type = block.get("type")
            if block_type == "text":
                total += estimate_tokens(str(block.get("text", "")), model=model)
            elif block_type == "image_url":
                # 图片按固定上界计（真实值取决于分辨率，OpenAI 口径约 85–1105 token）。
                # **未验证的近似**：本批只保证文本估算的合同（独立 finding）。
                total += 1024
            else:
                total += estimate_tokens(str(block), model=model)
        return total
    return estimate_tokens(str(content), model=model)


def _count_schema_properties(params: Any) -> int:
    """递归统计 JSON Schema 的属性个数（嵌套 `properties` 也算）。"""
    if not isinstance(params, dict):
        return 0
    props = params.get("properties")
    if not isinstance(props, dict):
        return 0
    total = 0
    for value in props.values():
        total += 1
        if isinstance(value, dict):
            total += _count_schema_properties(value)
    return total


def _tool_content_text(spec: dict) -> str:
    """把一个工具定义里**逐工具变化**的文本抽出来（骨架不在此列，见常量说明）。"""
    fn = spec.get("function") if isinstance(spec, dict) else None
    if not isinstance(fn, dict):
        return json.dumps(spec, ensure_ascii=False) if not isinstance(spec, str) else spec
    parts: list[str] = [str(fn.get("name") or ""), str(fn.get("description") or "")]
    params = fn.get("parameters")
    props = params.get("properties") if isinstance(params, dict) else None
    if isinstance(props, dict):
        for pname, pspec in props.items():
            parts.append(str(pname))
            if isinstance(pspec, dict):
                parts.append(str(pspec.get("description") or ""))
                for key in ("enum", "default"):
                    if key in pspec:
                        parts.append(json.dumps(pspec[key], ensure_ascii=False))
    if isinstance(params, dict):
        required = params.get("required")
        if isinstance(required, list):
            parts.extend(str(item) for item in required)
    return " ".join(parts)


def estimate_tools_tokens(tools: list[dict] | None, *, model: str | None = None) -> int:
    """估算 provider request 里 `tools=[...]` 那一份的 token 数。

    为什么必须计它：实测本仓 12 个内置工具的 schema 在 qwen2.5:3b 上是 **1126
    token**——占 6144 预算的 18%、占 8192 窗口的 14%。它们随每次请求发出，
    此前**完全不在估算与预算之内**（只靠 25% 余量兜着），这是与 KB-12 同根的
    false accept 面：用户消息 + 巨大 schema 时预算判"装得下"，真实 prompt 却超窗
    → provider 静默截断。

    **为什么不是 `json.dumps` 之后直接跑文本估算**：工具 schema 的骨架
    （`"type":"function"` / `"parameters"` / `"properties"` / `"required"` …）
    逐工具重复，真实 BPE 在第一次之后几乎不再为它付费，而线性文本模型每次都全额计。
    实测差 1.55×（12 个工具：线性估 1748 / 真值 1126）。故这里按**结构**计：
    一段固定框架 + 每工具骨架 + 每参数骨架 + 只有"逐工具变化的文本"走文本模型。

    常量来自 qwen2.5:3b 上的受控探针（N 个同构工具 / 递增描述 / 递增参数），
    是**上界**取向；跨 provider 的 tool prompt 框架无法离线复刻，故仍是近似。

    Args:
        tools: OpenAI function-calling 格式的工具定义列表（None/空 → 0）。
        model: 已解析模型 id（决定变化文本用哪个 profile）。

    Returns:
        估算 token 数。
    """
    if not tools:
        return 0
    try:
        total = _TOOLS_SECTION_BOILERPLATE_TOKENS
        for spec in tools:
            if not isinstance(spec, dict):
                total += _TOOL_FRAME_TOKENS + estimate_tokens(str(spec), model=model)
                continue
            raw_fn = spec.get("function")
            fn: dict = raw_fn if isinstance(raw_fn, dict) else {}
            raw_params = fn.get("parameters")
            params: dict = raw_params if isinstance(raw_params, dict) else {}
            total += _TOOL_FRAME_TOKENS
            total += _TOOL_PARAM_FRAME_TOKENS * _count_schema_properties(params)
            total += estimate_tokens(_tool_content_text(spec), model=model)
        return total
    except (TypeError, ValueError, AttributeError):  # 畸形 schema 绝不让组装崩掉
        return estimate_tokens(str(tools), model=model)


def estimate_messages_tokens(
    messages: list[dict],
    *,
    model: str | None = None,
    tools: list[dict] | None = None,
) -> int:
    """估算整份 **provider request 输入**的 token 数（messages + tools）。

    Batch 18 起 `tools` 纳入估算：`provider request = messages + tools`，只算前者
    会系统性漏掉后者（见 `estimate_tools_tokens`）。

    Args:
        messages: OpenAI 格式的消息列表。
        model: 已解析模型 id（决定 profile）。
        tools: 同一次请求的工具定义（None → 不计）。

    Returns:
        估算 token 数。
    """
    total = estimate_tools_tokens(tools, model=model)
    for msg in messages:
        total += _PER_MESSAGE_OVERHEAD_TOKENS
        total += estimate_tokens(str(msg.get("role", "")), model=model)
        total += _content_tokens(msg.get("content"), model)
        tool_calls = msg.get("tool_calls")
        if tool_calls:
            total += estimate_tokens(str(tool_calls), model=model)
    return total


def effective_context_window(context_window: int | None) -> int:
    """上下文窗口的**有效值**（token）：未声明/非法时用保守默认。

    "未声明 ≠ 无上限"是本模块的前提——调用方必须对 None 取保守默认，否则
    "没配 = 不截断"会退化成"超 context 变 provider 400"的原始缺陷。

    非法值（0 / 负数）与未声明同视：`providers.yaml` 是手写元数据，`0` 只可能是
    "没填"，而负数若原样参与换算会得到一个退化的极小预算（把 prompt 裁到只剩骨架
    却不报错）。两者都回退到保守默认，属既有兼容策略（unknown → 保守 fail-open）。

    之所以单独成一个函数：**"窗口的有效值"与"预算"是两个不同的量**。只要有人
    按窗口做判定（如预览请求"是否装得下"的本地校验），它必须与预算取同一个回退
    值；把这条规则抄第二遍就会漂移（一处改成 16K、另一处还是 32K）。
    """
    if context_window is None or context_window <= 0:
        return DEFAULT_CONTEXT_WINDOW
    return context_window


def budget_from_context_window(context_window: int | None) -> int:
    """上下文窗口 → prompt 预算（token）。未声明窗口时用保守默认。"""
    return max(1, int(effective_context_window(context_window) * CONTEXT_HEADROOM_RATIO))


def system_prompt_budget(prompt_budget_tokens: int) -> int:
    """system prompt 的子预算（总预算的一份，见 `SYSTEM_PROMPT_BUDGET_SHARE`）。"""
    return max(1, int(prompt_budget_tokens * SYSTEM_PROMPT_BUDGET_SHARE))
