"""角色性格引擎接口层——消费方依赖的契约，与实现解耦。

## 为什么需要这一层

`CharacterEngine` 是四层性格模型（L1 Core / L2 Self / L3 State / L4 Mood）的
**一种**实现。在此之前，消费方（`core/agent`、`core/cognitive`、`core/character`、
`core/debug`）不只用它的公开方法，还深入其私有成员——`engine._reflector`、
`engine._compression._compression_lock`、`engine._last_awareness_time`、
`engine._version_repo`、`engine._awareness._last_awareness_time`。后果是：
**换一个引擎实现、或引擎单独迭代，都会同时牵动四个包**。

本模块把消费方真正需要的语义收成两个结构化契约：

- `CharacterEngineProtocol` —— 引擎对外能力（渲染 / 事件 / 情绪 / 演化状态生命周期）；
- `CharacterReflectorProtocol` —— 反思子组件的窄面（缓冲、固化引用、复位、遥测）。

引擎内部实现（`engine.py` 的四层模型 + `compression.py` / `awareness.py` /
`emotion.py` / `reflector.py`）不属于契约，可自由重构。

## 为什么用 `Protocol` 而不是 ABC

仓库里 `core/status/protocol.py` 用的是 ABC + 继承，本层**刻意不同**：

- ABC 要求**替换实现必须继承本模块**——而「引擎可能被替换」正是本层存在的理由，
  替换方（或将来的独立包）不该为了接入而反向依赖 `core.personality`；
- `Protocol` 是结构化契约：任何形状相符的实现都能接入，无需继承、无需导入本模块。

代价是**没有实例化期强制**（ABC 少实现一个方法会 `TypeError`，Protocol 不会，
因为结构相符是静态判定）。补偿有两道：

1. `make pyright` 在「具体实现 → 接口类型」的赋值点做结构校验（`Agent.character_engine`
   等 property 的返回类型即接口类型）；
2. `core/tests/test_character_engine_protocol.py` 逐项断言 `CharacterEngine` 满足契约
   ——成员缺失或方法签名漂移即红。

**注意：`CharacterEngine` 刻意不继承本协议。** 若继承，协议里 `...` 的空方法体会
变成静默可用的 no-op 实现——漏实现一个方法反而不会报错，与建立契约的目的相反。
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from core.schema.character import CharacterRuntimeState
    from core.storage.character_version_repo import CharacterVersionRepository


# ── 契约数据类型（prompt section 渲染产物）────────────────────────────────────

@dataclass
class C1Data:
    """C1 Personality section 渲染数据。"""
    core_personality: str
    self_personality: str


@dataclass
class C9Data:
    """C9 SelfPerception section 渲染数据。"""
    awareness_text: str


@dataclass
class C6Data:
    """C6 SpeakingStyle section 渲染数据。"""
    speaking_style_c6: str  # "" 表示使用默认模板


# ── 反思子组件契约 ────────────────────────────────────────────────────────────

@runtime_checkable
class CharacterReflectorProtocol(Protocol):
    """CharacterReflector 的消费方面（窄面）。

    消费方按用途分三类，均不需要知道反思的触发阈值与 LLM 细节：
    ① **固化引用**——`buffer_message(..., reflector=...)` 需要「发起会话的 reflector」
       引用，避免 session switch 后 rebind 写到新会话；
    ② **生命周期**——复位/恢复缓冲与遥测、把用户称呼投影进去；
    ③ **遥测读取**——debug 面板展示 `buffer_size` / `last_*`。
    """

    async def buffer(self, user_msg: str, assistant_msg: str) -> None:
        """缓冲一轮对话；达到阈值时触发反思。"""
        ...

    async def flush(self) -> None:
        """强制触发未完成的反思（shutdown 时调用）。"""
        ...

    def set_user_name(self, user_name: str) -> None:
        """更新 prompt 中使用的用户称呼。"""
        ...

    def reset_state(self) -> None:
        """复位缓冲、上下文与遥测（角色重置用）。"""
        ...

    def restore_from_state(self) -> None:
        """从共享 `CharacterRuntimeState` 恢复缓冲与上下文（会话/实例切换后调用）。"""
        ...

    @property
    def in_flight(self) -> bool:
        """当前是否有反思在途（busy 判定用）。"""
        ...

    @property
    def buffer_size(self) -> int:
        """当前缓冲的对话轮数。"""
        ...

    # ── 遥测（debug 面板读取；实现侧均为只读 property）──

    @property
    def locked(self) -> bool:
        """反思是否正在进行（与 `in_flight` 同源，保留既有属性名）。"""
        ...

    @property
    def last_reflection_at(self) -> float | None:
        """上次反思的时间戳；从未反思过为 None。"""
        ...

    @property
    def last_success(self) -> bool:
        """上次反思是否成功。"""
        ...

    @property
    def last_duration_ms(self) -> float:
        """上次反思耗时（毫秒）。"""
        ...

    @property
    def last_text_len(self) -> int:
        """上次反思产出文本长度。"""
        ...

    @property
    def last_prompt_system(self) -> str:
        """上次反思的 system prompt（debug 查看）。"""
        ...

    @property
    def last_prompt_user(self) -> str:
        """上次反思的 user prompt（debug 查看）。"""
        ...


# ── 引擎契约 ──────────────────────────────────────────────────────────────────

@runtime_checkable
class CharacterEngineProtocol(Protocol):
    """角色性格引擎的消费方契约。

    继承关系**不是**接入前提——形状相符即可（见模块 docstring）。
    实现参考：`core/personality/engine.py::CharacterEngine`。
    """

    #: L1–L4 共享的运行时状态。与 `CharacterRuntime.state` **同一对象**
    #: （engine 构造时注入），因此读它拿到的是实例权威值。
    state: CharacterRuntimeState

    # ── prompt section 数据提供 ──

    def render_c1(self) -> C1Data:
        """产出 C1 Personality section 数据（L1 Core + L2 Self）。"""
        ...

    def render_c9(self) -> C9Data:
        """产出 C9 SelfPerception section 数据（冷却 + 展示次数门控后）。"""
        ...

    def render_c6(self) -> C6Data:
        """产出 C6 SpeakingStyle section 数据。"""
        ...

    # ── 事件与消息 ──

    async def consume_trigger(self, source: str, data: dict) -> None:
        """接收外部触发事件（reflector / event / idle / feedback），更新 L3。"""
        ...

    def buffer_message(
        self,
        user_msg: str,
        assistant_msg: str,
        reflector: CharacterReflectorProtocol | None = None,
    ) -> asyncio.Task | None:
        """把一轮对话交给反思组件缓冲，返回可登记的任务（无 reflector 时为 None）。"""
        ...

    def analyze_user(self, text: str, *, ctx: Any | None = None) -> None:
        """关键词检测用户消息情绪。"""
        ...

    def analyze_user_stt(
        self, emotion_tag: str, events: list[str] | None = None, *,
        ctx: Any | None = None,
    ) -> None:
        """处理 STT 音频情绪事件。"""
        ...

    def analyze_assistant(self, text: str, *, ctx: Any | None = None) -> None:
        """助手回复后衰减情绪强度。"""
        ...

    def sync_reflector_to_runtime_state(self) -> None:
        """把反思缓冲/上下文同步进共享 state（回合边界持久化）。"""
        ...

    async def flush_reflector(self) -> None:
        """等待未完成的反思落定（shutdown 时调用）。"""
        ...

    # ── 演化状态生命周期 ──
    #
    # 这一组是本接口层存在的直接原因：消费方此前必须分别戳
    # `_reflector._reflect_lock` / `_compression._compression_lock` /
    # `_last_awareness_time` / `_awareness._last_awareness_time` 才能完成
    # 「busy 判定」「复位」「恢复」三件事。现在收成语义方法。

    @property
    def reflector(self) -> CharacterReflectorProtocol | None:
        """当前反思组件（用于固化引用后传给 `buffer_message`）。"""
        ...

    @property
    def version_repo(self) -> CharacterVersionRepository | None:
        """L2/C6 版本历史仓库；未装配时为 None。"""
        ...

    def evolution_in_flight(self) -> bool:
        """是否有任何演化动作在途（反思 / L3→L2 压缩）。

        角色替换（reset）用它做快速拒绝——**快速路径，不是唯一防线**：
        领域层的 runtime lease 才是守住不变量的地方。
        """
        ...

    def reset_evolution_state(self) -> None:
        """复位演化状态：反思缓冲/上下文/遥测 + 觉察冷却。

        与 `CharacterRuntimeState` 的字段复位配套使用（角色重置流程）。
        """
        ...

    def restore_evolution_state(self, *, last_awareness_time: float | None = None) -> None:
        """从共享 state 恢复演化状态（实例/session 切换后调用）。"""
        ...
