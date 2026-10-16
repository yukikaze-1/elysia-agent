"""capabilities 构建与广播 — 服务运行时状态下发前端。

CORE-MOD-P1-01 从 core/input/capabilities.py 迁入 core/services。

嵌套 per-service 形状：available=false 时 model 为 null（不读 None.model，
避免 AttributeError 与"显示选中不可用模型"）。契约单源：构造
`CapabilitiesMessage` 模型 + `wire_dict()` 序列化（ts_null 补显式 null）。

**能力读取不得杀死协议帧**（2026-09-27 修复）：`model` 是
``Module/{tts,stt}/protocol.py`` 的后端协议成员，读取统一走 ``_backend_model()``
——不满足协议的后端（缺 ``model``）只让**该字段**降级为 null 并打 warning，
不允许把 ``server_info``/``capabilities`` 这类**协议必须帧**整体炸掉。
历史缺陷：`agent.tts.model` 裸读 + 建连发送点的 `except Exception: logger.debug`
⇒ 一个缺 ``model`` 的测试替身让 v2 客户端**永久收不到 server_info**，
而日志里只有一行 debug（见 docs/electron-backend-protocol-v2.md §建连帧）。

**这一层是运行期韧性，不是契约强制**（2026-09-28 分层）：它保证"坏 backend 真的
漏进运行时"时协议帧仍然可达；"backend 不许违反协议"由静态检查 + contract test
负责（``Module/_common/protocol_contract.py``、``Module/{tts,stt}/tests/test_*_contract.py``）。
`_backend_model()` 的四种情形（缺成员 / 空串 / 非字符串 / getter 自己抛）以**不同
日志级别**区分，见其 docstring 的判据表——getter 抛异常走 ERROR + traceback，
不会被当成"缺成员"静默降级。
"""

import logging
from typing import TYPE_CHECKING

from Module.output import WebSocketChannel
from frontend_contract import (
    CapabilitiesMessage,
    ServerInfoFeatures,
    ServerInfoMessage,
    ServiceCapability,
)
from frontend_contract.version import PROTOCOL_VERSION

if TYPE_CHECKING:
    from core.agent import Agent
    from core.agent.server import AgentServer

logger = logging.getLogger(__name__)


def _declares_model(backend: object) -> bool:
    """``backend`` 是否**静态声明**了 ``model`` 成员（类 MRO 或实例 ``__dict__``）。

    只做静态查找，**不触发** property getter——这正是它与
    ``getattr(obj, "model", default)`` 的区别所在（后者会把 getter 内部抛出的
    AttributeError 也变成 default，见 `_backend_model` 的判据表）。
    """
    if any("model" in vars(klass) for klass in type(backend).__mro__):
        return True
    instance_dict = getattr(backend, "__dict__", None)
    return isinstance(instance_dict, dict) and "model" in instance_dict


def _backend_model(backend: object, service: str) -> str | None:
    """读后端 ``model`` 协议成员——取不到就让**该字段**降级为 null，但绝不静默。

    ## 为什么不能让它抛（这一层是运行期韧性，不是契约强制）

    调用方（``build_capabilities`` / ``build_server_info``）的返回值直接决定
    `capabilities`/``server_info`` 这一帧能否上 wire。协议帧的可达性**不能**依赖
    后端元数据是否可读——那是「一个字段缺失 → 整帧消失」的故障放大。2026-09-27 的
    真实缺陷正是这条：测试替身没实现 ``model``，``server_info`` 整帧消失，而日志里
    只有一行 debug。

    ## 但它**不是**"backend 可以不守协议"的许可证

    四种情形在这里被刻意区分，且**日志级别不同**——"契约违规"与"真实实现 bug"
    不会被同一行日志掩盖：

    | 情形 | 返回 | 日志 |
    |------|------|------|
    | 成员**不存在**（duck-typed 替身漏写 / 后端真没实现） | ``None`` | **WARNING**：指名后端与服务，要求补齐实现 |
    | ``model`` 返回 ``""`` | ``""`` | 无——ABC 的默认实现在 ``_model`` 未设时就是空串，属**合法**值 |
    | ``model`` 返回非 ``str``（含 ``None``） | ``None`` | **WARNING**：与协议 ``-> str`` 不符 |
    | getter **自己抛异常** | ``None`` | **ERROR + exc_info**：这是实现 bug，不是"缺成员" |

    最后一行的判据有历史踩坑：``getattr(obj, "model", default)`` 会把「成员不存在」
    与「property getter 内部抛 AttributeError」**都**变成 default，于是 getter 的真
    bug 被误报成"后端没实现协议成员"。所以这里先直接取（单参 ``getattr``，异常照抛），
    只在 ``AttributeError`` 时用 `_declares_model()` 静态判定到底是哪一种。

    **开发/测试期的严格性不在这里**——违反协议要在 contract test 里失败，见
    ``Module/_common/protocol_contract.py`` 与
    ``Module/{tts,stt}/tests/test_*_contract.py``。本函数只负责"一个坏 backend
    真的漏进运行时"时的最小降级。

    Args:
        backend: TTS/STT 后端实例，或 None（服务未挂载）。
        service: 服务名（"tts"/"stt"），仅用于告警文案。

    Returns:
        完整模型标识（``provider/model``）；后端缺失、未实现该协议成员或读不到
        合法字符串时 None。**不打印任何密钥/完整配置**——只有服务名与后端**类型名**。
    """
    if backend is None:
        return None
    backend_kind = type(backend).__name__
    try:
        raw = getattr(backend, "model")
    except AttributeError:
        if _declares_model(backend):
            # 声明了却取不到 ⇒ getter 自己抛了 AttributeError：真实实现 bug，不是缺成员
            logger.error(
                "%s 后端的 .model getter 抛了 AttributeError（backend=%s）——"
                "这是实现错误，不是「缺协议成员」；能力帧的 model 字段降级为 null",
                service.upper(), backend_kind, exc_info=True,
            )
        else:
            logger.warning(
                "%s 后端未实现 .model（Module/%s/protocol.py 要求的协议成员，backend=%s）"
                "——能力帧的 model 字段降级为 null；请补齐后端实现",
                service.upper(), service, backend_kind,
            )
        return None
    except Exception:
        logger.error(
            "%s 后端的 .model getter 抛异常（backend=%s）——这是实现错误；"
            "能力帧的 model 字段降级为 null",
            service.upper(), backend_kind, exc_info=True,
        )
        return None
    if isinstance(raw, str):
        return raw
    logger.warning(
        "%s 后端的 .model 返回非字符串 %s（backend=%s）——Module/%s/protocol.py 声明 "
        "``-> str``；能力帧的 model 字段降级为 null",
        service.upper(), type(raw).__name__, backend_kind, service,
    )
    return None


def build_capabilities(agent: "Agent | AgentServer") -> CapabilitiesMessage:
    """构建嵌套 per-service capabilities 契约模型。

    Args:
        agent: Agent 或 AgentServer mixin（只读公开属性 + config；
            运行时经 Agent 组合，属性实际由 Agent 提供）。

    Returns:
        ``CapabilitiesMessage``（type=capabilities + live2d_static_image +
        services），每个 service 为 ``ServiceCapability{available, enabled, model, default}``。
    """

    def svc(available: bool, enabled: bool, model: str | None, default: str) -> ServiceCapability:
        # model: available=False 时置 None → wire_dict 经 TsNull 补显式 null
        return ServiceCapability(
            available=available,
            enabled=enabled,
            model=model if available else None,
            default=default,
        )

    cfg = agent.config
    return CapabilitiesMessage(
        live2d_static_image=cfg.live2d.static_image,
        services={
            "llm": svc(True, True, agent.llm.model, cfg.llm.model),
            "stt": svc(agent.stt is not None, agent.stt_enabled,
                       _backend_model(agent.stt, "stt"), cfg.stt.model),
            "tts": svc(agent.tts is not None, agent.tts_enabled,
                       _backend_model(agent.tts, "tts"), cfg.tts.model),
        },
    )


def build_server_info(agent: "Agent", connection_id: str | None = None) -> ServerInfoMessage:
    """构建 v2 server_info（protocol_version/features/services，取代 capabilities）。

    `features` 严禁超前声明——每个字段必须如实反映后端当前真实能力：
    结构化聊天/多会话/Desktop/转写分别在批次 2/3/4/6 实现后置 true，批次 1 全 false。
    `services` 复用与 capabilities 相同的 per-service 形状，Electron 设置面板共用渲染。
    `connection_id`（批次 4，BE-042 附件持有者校验闭环）：v2 + electron 建连时
    ws_handler 传入握手生成的 conn_id → `server_info.connectionId`；非 electron/普通
    v2 传 None → wire 缺省省略（None → exclude_none，v1 类型字节不变）。

    Args:
        agent: Agent 实例（只读公开属性 + config）。
        connection_id: 桌面客户端连接 ID（仅 v2 + electron 建连时非 None）。

    Returns:
        ``ServerInfoMessage``（type=server_info + protocol_version + features + services）。
    """
    from core.version import build_server_version, get_build_sha
    from core.storage.idempotency import get_store

    def svc(available: bool, enabled: bool, model: str | None, default: str) -> ServiceCapability:
        return ServiceCapability(
            available=available,
            enabled=enabled,
            model=model if available else None,
            default=default,
        )

    cfg = agent.config
    return ServerInfoMessage(
        # P7 单源：协议版本显式取 PROTOCOL_VERSION（frontend_contract.version）——
        # 运行时不再手写 "2.0" 字面量，改协议版本只改 version.py + make gen-types
        protocol_version=PROTOCOL_VERSION,
        server_version=build_server_version(),
        build_sha=get_build_sha(),  # 批次 7：build SHA 独立上 wire（前端展示/反馈定位）
        features=ServerInfoFeatures(
            structured_generation=True,  # 批次 2 已实现
            multi_conversation=True,     # 批次 3 已实现
            desktop_runtime=True,        # 批次 4 已实现（D-s，spec §8）
            dictation=True,        # 批次 6 已实现（BE-062）
            voice_recording=True,  # 批次 6（BE-060）
            voice_realtime=True,   # 批次 6（BE-061）
            media_upload=True,     # 批次 7 已实现（POST /api/uploads）
            idempotency_durable=get_store().durable,  # D6c：true=跨重启持久化 / false=内存降级窗口
            character_profiles=getattr(agent, "character_profiles", False),
            character_state_stream=True,  # 阶段 G: character.state.changed 事件已实现
        ),
        services={
            "llm": svc(True, True, agent.llm.model, cfg.llm.model),
            "stt": svc(agent.stt is not None, agent.stt_enabled,
                       _backend_model(agent.stt, "stt"), cfg.stt.model),
            "tts": svc(agent.tts is not None, agent.tts_enabled,
                       _backend_model(agent.tts, "tts"), cfg.tts.model),
        },
        connectionId=connection_id,
    )


async def broadcast_capabilities(agent: "Agent | AgentServer") -> None:
    """服务状态变化（switch_*/set_service 成功）后向 v1 WebSocketChannel 重播。

    不复制 switch_model_result 仅回请求方的模式——遍历 ``agent.output.channels``，
    过滤 ``WebSocketChannel``（agent 无条件注册 ConsoleChannel，无 ws），
    对每个 WebSocketChannel 调公开 ``send_json``（2026-09-12 前该方法为私有
    `_send_json`，本函数当时只能标注 `# noqa: SLF001` 穿透）。
    **批次 8c（F15）版本门控**：仅重播 protocol_version < 2 的通道——v1 浏览器
    通道（无头）恒 1 → 保留重播；v2 Electron 建连已收 server_info（services 同形状
    取代 capabilities），跳过避免双轨下发。

    Args:
        agent: Agent 或 AgentServer mixin（switch_*/set_service 内以 self 调用）。
    """
    try:
        payload = build_capabilities(agent)
        for ch in agent.output.channels:
            if isinstance(ch, WebSocketChannel) and getattr(ch, "protocol_version", 1) < 2:
                await ch.send_json(payload.wire_dict())
    except Exception:
        # 与 per-channel 发送失败（send_json 内部已记录）区分：这里捕获的是
        # build_capabilities 回归或 list-change——需 warning 级别才可见
        logger.warning("capabilities 重播失败", exc_info=True)
