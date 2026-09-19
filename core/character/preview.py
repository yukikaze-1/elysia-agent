"""CharacterPreviewPort — 无状态角色预览推理口（BE-STAGEH-03）。

预览只做「按 draft 组装 prompt → 一次 LLM 调用」的纯推理。绝对零副作用边界：
不建/激活 session，不建 CharacterInstance，不写 message/generation archive，不读
或写会话历史/LTM/relationship/演化 state，不推进 definition revision 或 instance
stateVersion，不触发 reflector/compression/mood/TTS/audio/expression/Live2D，不调用
任何工具，也不改变 Agent 当前活动会话/角色。

用量不并入任何 conversation token 统计——预览结果不带 usage。

## 预览请求必须装得下 resolved 模型的窗口（F8 / Batch 7）

预览是**第二条 prompt 组装路径**（chat 走 `core/cognitive/loop.py::_build_turn_prompt`），
两者必须用同一个窗口权威与同一个预算换算，否则小窗口 provider 上只有一条路径会
400：预览此前不传 `prompt_budget_tokens`，于是 `system + userMessage` 原样发出，
`ollama`（`providers.yaml` 声明 8192）上一段稍长的预览输入就必然超窗。

三条语义（不变量 PPW-1..PPW-4）：

1. **预算取自 resolved runtime model**：`self._llm.model`（热切换后即时跟手），
   经 `LLMFactory.context_window` 取声明窗口——不读 config/bootstrap 模型，也不
   自带第二份窗口表（Batch 5：能力真相靠近 registry）。
2. **能裁进窗口就必须能预览**：预算裁剪沿用 chat 的同一实现
   （`PromptAssembler.build_messages(prompt_budget_tokens=…)` → section/历史裁剪），
   不新增预览专用默认值。
3. **裁完仍装不下 → 本地拒绝，不发已知必 400 的请求**：抛
   `PreviewPromptTooLargeError`（服务层映射为 400 `CHARACTER_VALIDATION`）。
   判定阈值是**窗口**而不是预算：预算含 25% 输出余量，保护节（身份/格式契约）
   自身就可能超过它——只要请求真的装得下，就不该以"预算没达标"为由拒绝预览。

**不设 preview 专属输出上限**：chat 与预览都不设 `max_tokens`（provider 默认），
输出余量由 `CONTEXT_HEADROOM_RATIO` 预留；仓库没有 model 级 output cap metadata，
本批不新造。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from core.adapter.llm_adapter import LLMAdapter
    from core.character.runtime_profile import RuntimeCharacterProfile
    from core.profile.user_identity import UserIdentity

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PreviewReply:
    """一次预览推理的归一化结果。"""

    text: str
    model: str | None = None


class PreviewPromptTooLargeError(RuntimeError):
    """预览 prompt 连 resolved 模型的上下文窗口都装不下。

    **本地判定**：抛出时尚未发出任何 provider 请求（一次用户预览仍最多一次
    generation 请求）。服务层把它映射成非重试的 400，而不是"provider 暂时不可用"
    的 503——这个条件不会因重试而消失。
    """


class CharacterPreviewPort(Protocol):
    """窄接口：只接收已编译 profile + 用户身份快照 + 一条输入。"""

    async def generate(
        self,
        *,
        profile: "RuntimeCharacterProfile",
        user_identity: "UserIdentity",
        user_message: str,
    ) -> PreviewReply: ...


class PromptCharacterPreviewAdapter:
    """用一次性 PromptAssembler（无会话/无记忆）组装 prompt，经 LLMAdapter 单次推理。

    每次调用新建 assembler——PromptAssembler 是有状态对象（set_character 会改写
    section），复用生产实例会把预览角色泄漏进真实会话的 prompt。
    """

    def __init__(self, llm: "LLMAdapter"):
        self._llm = llm

    async def generate(
        self,
        *,
        profile: "RuntimeCharacterProfile",
        user_identity: "UserIdentity",
        user_message: str,
    ) -> PreviewReply:
        from Module.llm.registry import LLMFactory
        from Module.system_prompt.budget import (
            budget_from_context_window,
            effective_context_window,
            estimate_messages_tokens,
        )
        from core.prompt.prompt_assembler import PromptAssembler

        assembler = PromptAssembler(
            character=profile.config,
            session=None,      # 无会话历史
            long_term=None,    # 无 LTM 检索（C4 空渲染）
        )
        assembler.enable_preview_mode()
        assembler.set_character(profile.config.name, profile.config.name, profile)
        # 窗口权威 = resolved runtime model（热切换后即时跟手）。与 chat 同源同式：
        # `core/cognitive/loop.py::_build_turn_prompt` 也是这么取预算的。
        model_id = self._llm.model
        window = LLMFactory.context_window(model_id)
        # P0-2：有效称呼与生产回合同规则——preferredAddress 优先，否则 displayName。
        address = (user_identity.preferredAddress or "").strip() or (
            user_identity.displayName or "用户")
        messages = await assembler.build_messages(
            user_message=user_message,
            user_name=address,
            user_pronoun_override=user_identity.pronouns,
            user_timezone_override=user_identity.timezone,
            prompt_budget_tokens=budget_from_context_window(window),
            model_id=model_id,
        )
        # PPW-4：裁完仍装不下 → 本地拒绝。发送一个已知必失败的请求只会把确定性的
        # 输入问题伪装成"provider 暂时不可用"（503 + retryable）。
        #
        # 边界语义（Challenger H）：判据是 `估算输入 >= 窗口` ⇒ 拒绝。取 `>=` 而非 `>`
        # 是因为预览的**产品目的是拿到一句回复**——输入占满整个窗口时已经没有留给
        # 输出的余量（本仓 chat 与预览都不发 max_tokens，余量全靠 provider 默认 +
        # 预算的 25% headroom）。`== 窗口` 属于"装得下但生成不出东西"，同样不该发。
        #
        # **判据的诚实边界（Batch 18 更新）**：估算器是**有界近似**（不是 provider
        # tokenizer），误差带按 resolved model 的 profile 声明在
        # `Module/system_prompt/budget.py` 的 docstring 里（6 个 profile，
        # 实测 est/actual 下限 ≥0.78、上限 1.25–2.68 视家族而定）。
        # ⇒ 本判据的两侧都**不是绝对的**，别再写成"绝不发出真实超窗的请求"
        # （Batch 18 Challenger 已证伪那句）：
        #   * 低估侧：合同外的文字（婆罗米系/东南亚/希伯来/阿拉伯等）与 `repeat`
        #     类不享受下限保证 —— 它们由保守固定系数覆盖（只高估）或如实登记；
        #   * 高估侧：真实输入落在窗口的 [1/T, 1] 区间时仍会被本地拒（T 越小越窄，
        #     例如 `qwen` 的 T=1.25 ⇒ 最坏 20% 的窗口宽度）。
        # 这条判据的价值是"把**大部分**必失败请求挡在发出去之前"，不是精确计数。
        estimated_input = estimate_messages_tokens(messages, model=model_id)
        effective_window = effective_context_window(window)
        if estimated_input >= effective_window:
            logger.warning(
                "角色预览输入超出模型窗口，本地拒绝（不请求 provider）："
                "model=%s window=%d 估算输入=%d",
                model_id, effective_window, estimated_input)
            raise PreviewPromptTooLargeError(
                f"预览内容超出模型 {model_id} 的上下文窗口"
                f"（估算 {estimated_input} >= {effective_window} token）"
            )
        result = await self._llm.think(messages)   # tools=None → 不挂任何工具
        return PreviewReply(text=result.text or "", model=self._llm.model)
