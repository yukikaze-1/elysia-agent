"""Agent —— 将 L0/L1/L2 各组件装配成一个可运行的 Agent。

用法:
    agent = Agent(config=config)
    await agent.start()
    await agent.process_message("你好")       # 单轮对话
    await agent.run_repl()                   # 交互式 REPL
    await agent.stop()

组件依赖图（自顶向下）:
    AppConfig ──┬── CharacterConfig ──┐
                ├── LLMAdapter ──────┼── CharacterEngine ──┐
                ├── MemoryConfig ────┤                     │
                └── ServerConfig     │                     │
                                     │                     │
    Repos ───────┬── SessionManager  │                     │   (批次 3: multi-session 池, active → SessionMemory)
                 ├── LongTermMemory  │                     │
                 └── VersionRepo ────┘                     │
                                                           │
    EventBus ──────────────────┐                           │
    AgentFSM ──────────────────┤                           │
    ToolAdapter ───────────────┤                           │
    PromptAssembler ───────────┤                           │
    MemoryConsolidator ────────┤                           │
    MemoryReflector ───────────┤                           │
                               ├── CognitiveLoop ──────────┤
    TokenTracker ──────────────┤                           │
    PromptRecorder ────────────┤                           │
    ResponseRecorder ──────────┤                           │
    TTSBackend ────────────────┤                           │
    WorkflowEngine ────────────┤                           │
    DelegationPreparer ────────┤                           │
                               │                           │
                               └── IdleMonitor ────────────┘
                               │
                               └── ToolCallDispatcher

    两阶段初始化：__init__ 创建 22 个组件；start() 在拿到 session_id 后再创建 8 个
    (token_tracker, tts, task_system 等)。交叉装配经 start() 中的
    CognitiveLoop.configure() 完成。
"""

from __future__ import annotations

import asyncio
import time
import os
from dataclasses import asdict
import logging
from typing import Any, TYPE_CHECKING, Awaitable, Callable

if TYPE_CHECKING:
    from core.monitoring.token_tracker import TokenTracker
    from core.debug.prompt_recorder import PromptRecorder
    from core.debug.response_recorder import ResponseRecorder
    from Module.workflow_engine.mailbox import WorkflowMailbox
    from Module.stt.protocol import STTBackend
    from core.status import AgentStatus, CognitivePhaseController
    from core.character.definition import CharacterDefinition
    from core.character.instance import CharacterInstance
    from core.character.revision import CharacterRevision

from Module.output import OutputManager, ConsoleChannel
from Module.workflow_contract.schema import WorkflowId
from Module._common.task_scope import AsyncTaskScope
from Module.llm.protocol import LLMBackend

from core.agent._types import RunState, _PendingMessage, _BackgroundTaskEntry
from core.agent.lifecycle import AgentLifecycle
from core.agent.server import AgentServer
from core.cognitive.loop import DEFAULT_INSTANCE_ID

from core.config import AppConfig
from core.monitoring.event_bus import EventBus, EventPriority, EventType
from core.status.agent_fsm import AgentFSM, AgentState
from core.utils import set_request_id, get_request_id
from core.config import CharacterConfig
from core.memory.session_manager import SessionManager
from core.memory.long_term import LongTermMemory
from core.memory.scope import MemoryScope
from core.prompt.prompt_assembler import PromptAssembler
from core.adapter import ToolAdapter
from core.tools import ALL_PLUGINS
from core.monitoring.idle_monitor import IdleMonitor
from core.adapter.llm_adapter import LLMAdapter
from core.memory.consolidator import MemoryConsolidator
from core.personality import CharacterEngine
from core.personality.protocol import CharacterEngineProtocol
from core.memory.memory_reflector import MemoryReflector
from core.character.registry import CharacterRuntimeRegistry
from core.character.runtime import CharacterRuntime
from core.character.runtime_loader import CharacterRuntimeSourceLoader
from core.character.runtime_profile import compile_runtime_profile
from core.character.service import CharacterService, CharacterStateConflictError
from core.character.catalog import CharacterCatalog
from core.character.preview import PromptCharacterPreviewAdapter
from core.storage.character_instance_repo import CharacterInstanceRepository
from core.storage.character_repo import CharacterRepository
from core.storage.character_revision_repo import CharacterRevisionRepository
from core.storage.character_version_repo import CharacterVersionRepository
from core.schema.types import IsoTimestamp, MessageId, SessionId
from core.schema.utils import now_iso
from core.schema.character import CharacterRuntimeState
from core.profile.profile_store import UserIdentityStore
from core.storage.user_identity_repo import UserIdentityRepository
from core.schema.event import (
    InterruptData, UserInputData, TaskResultData, StateChangeData, SystemTickData,
)
from Module.embedding import EmbeddingFactory
from core.tool_runtime import ToolCallDispatcher
from core.storage.session_repo import SessionRepository
from core.storage.background_task_repo import BackgroundTaskResultRepository
from core.storage.task_result_wal import TaskResultWAL
from core.storage.message_repo import MessageRepository
from core.storage.token_repo import TokenRepository
from core.storage.memory_repo_v2 import MemoryRepositoryV2
from core.storage.desktop_call_repo import DesktopCallRepository
from core.storage.attachment_repo import AttachmentRepository
from core.storage.idempotency_repo import IdempotencyRepository
from core.storage.attachment_release import release_session_attachments
from core.desktop.bridge import DesktopToolBridge
from paths import ProjectPath

logger = logging.getLogger(__name__)


class Agent(AgentLifecycle, AgentServer):
    """由 L0/L1/L2 各组件装配而成的完整 Agent。"""

    def __init__(self, *, config: AppConfig, config_path: str | None = None,
                 prefer_config_model: bool = False):
        """初始化 Agent 并创建所有 L0/L1/L2 组件。

        Args:
            config: 应用配置（AppConfig 实例）。
            config_path: 配置文件路径，用于 /restart 时重新加载配置。
            prefer_config_model: 本次启动**以 config 的 llm.model 为准**（忽略持久化的
                运行期模型选择，并把它写回）。由 `run_agent_service` 在"重启且
                YAML 里的模型变了"时置位——否则 `_apply_service_state` 会用旧选择把
                用户刚改的模型切回去（S2-A5 第二半）。
        """
        self._config = config
        self._config_path = config_path
        self._prefer_config_model = prefer_config_model
        self._user_name = config.agent.user_name
        # Task 7（B1）：用户身份存储——启动用 config.agent.user_name 建 bootstrap profile，
        # 供 _prepare_turn 读 pronouns（全局唯一「她」）。
        # BE-STAGEH-02：Mongo user_identity_profiles 为权威——repo 在 __init__ 装配，
        # Agent.start() 经 _start_persistence 装载（重启不再回到 bootstrap）。
        self._user_identity_repo = UserIdentityRepository(
            base_url=config.database.mongodb_uri)
        self._user_identity_store = UserIdentityStore(repo=self._user_identity_repo)
        # P0-3：权威模式下构造期**不**伪造本地档案——这里只取固定单用户 userId 的
        # 占位（scope/owner 常量用）。真实身份由 start() 经 connect() 从 Mongo 装载；
        # 装载失败则身份不可用，业务侧 fail-closed。
        self._user_identity = self._user_identity_store.placeholder(user_name=self._user_name)

        # ---- L0: Infrastructure (from Module/) ----
        self._output = OutputManager()
        self._output.add_channel(ConsoleChannel())

        # ---- Character ----
        self._character = CharacterConfig.from_yaml(config.agent.character)

        # ---- Storage Repositories ----
        db_cfg = config.database
        # P3-3：写入期角色完整性兜底随能力开关开启——character_profiles=true 时
        # assistant 消息缺角色快照会在两个落库边界发显著告警（而非静默落库）。
        _require_char = self.character_profiles
        self._session_repo = SessionRepository(
            database_name=db_cfg.db_name,
            base_url=db_cfg.mongodb_uri,
            require_assistant_character=_require_char,
        )
        self._message_repo = MessageRepository(
            database_name=db_cfg.db_name,
            base_url=db_cfg.mongodb_uri,
            require_assistant_character=_require_char,
        )
        self._token_repo = TokenRepository(
            database_name=db_cfg.db_name,
            base_url=db_cfg.mongodb_uri,
        )
        # #6（决策 #6）：后台任务终态结果仓储——"停机落库 + 启动补投"的持久面。
        # 与 token/message repo 同模式：此处只构造，建连在 _start_persistence。
        self._background_task_result_repo = BackgroundTaskResultRepository(
            database_name=db_cfg.db_name,
            base_url=db_cfg.mongodb_uri,
        )
        # #6 硬杀窗口：终态结果的本地 WAL（同步 fsync；补的是"终态已产生、仓储还没
        # 落库"的那 ≤1 个轮询周期被硬杀的部分）。无连接概念，构造即可用。
        self._task_result_wal = TaskResultWAL()
        self._memory_repo = MemoryRepositoryV2(
            base_url=db_cfg.milvus_uri,
            token=db_cfg.milvus_token or None,
        )
        self._version_repo = CharacterVersionRepository(
            base_url=db_cfg.mongodb_uri,
        )
        # #21b：generation 仓储改为**在 SessionManager 之前显式构造**——会话删除的
        # 级联清单要引用它，而原先它藏在 `GenerationRegistry()` 内部（构造点在本函数
        # 更后面），清单拿不到实例。仓储实例仍只此一个，注册表经构造注入复用。
        from core.storage.generation_repo import GenerationRepository
        self._generation_repo = GenerationRepository(
            database_name=db_cfg.db_name,
            base_url=db_cfg.mongodb_uri,
        )
        # 批次 4 Desktop 调用注册表（desktop_calls 集合；Mongo 离线降级为进程内缓存）
        self._desktop_call_repo = DesktopCallRepository(
            database_name=db_cfg.db_name,
            base_url=db_cfg.mongodb_uri,
        )
        # 批次 7：附件仓库——lazy 初始化（attachment_repo property 首访时构造，
        # start() 建连；Mongo 离线附件操作 fail-closed）
        self._attachment_repo: AttachmentRepository | None = None
        # D6c：幂等 claim 仓库——lazy 初始化（idempotency_repo property 首访构造，
        # _start_persistence 建连；产品模式 Mongo 离线幂等 fail-closed）
        self._idempotency_repo: IdempotencyRepository | None = None

        # ---- L2: Memory ----
        # 批次 3 (multi-session)：SessionManager 取代单一 self._session——
        # 缓存池 + 活动指针，eager 构造 default 空实例。session property 直通 active。
        self._session_manager = SessionManager(
            repo=self._session_repo,
            msg_repo=self._message_repo,
            max_messages=config.memory.session_max_messages,
            # #21b：会话域集合的级联删除清单——**唯一接线点**。这些集合都按
            # session_id 分区，删会话时漏掉任何一个都会留下永久不可达的孤儿行。
            # 覆盖守卫：core/memory/tests/test_session_cascade.py 断言"所有带
            # delete_by_session 的仓储都在这里"，新增集合忘接线即红。
            cascade_repos=[
                ("token_usage", self._token_repo),
                ("generations", self._generation_repo),
                ("character_versions", self._version_repo),
            ],
        )
        # review 复核 #5 D2：会话删除提交后释放该会话归档消息的附件 refs（失败由
        # manager journal 重试）。lazy 闭包——delete/retry 时才解引用 self 的 repo。
        self._session_manager.set_attachment_cleanup(
            lambda sid: release_session_attachments(self, sid))
        self._long_term = LongTermMemory(
            max_entries=config.memory.ltm_max_entries,
            max_age_days=config.memory.ltm_max_age_days,
            embed_timeout=config.memory.ltm_embed_timeout,
            enable_eviction=config.memory.ltm_enable_eviction,
            repo=self._memory_repo,
        )

        # ---- L2: Tools ----
        # 批次 4（review 修正，构造顺序 critical）：DesktopToolBridge 必须先于
        # ToolAdapter 实例化——① 构造绑定注入（definitions() D-j 过滤）；② _start_persistence
        # 早于 _STARTUP_STEPS/_start_tool_context，bridge 必须已存在供 connect_db()。
        self._desktop_bridge = DesktopToolBridge(repo=self._desktop_call_repo)
        self._tool_adapter = ToolAdapter(
            ALL_PLUGINS,
            session_id="",
            user_name=self._user_name,
            config=self._config,
            hitl_broadcast=self._output,
            desktop_bridge=self._desktop_bridge,
        )

        # ---- L1: Cognitive ----
        self._bus = EventBus()
        #: 未登记事件类型计数（S3-23）：debug/日志用它暴露
        #: 「发布方与消费方类型表不一致」，而不是静默丢弃。
        self._unhandled_events = 0
        self._fsm = AgentFSM()
        self._llm = LLMAdapter(model=config.llm.model)

        # ---- 性格 + 情绪引擎（M3: engine per-instance + RuntimeRegistry）----
        # Task 11（A3 修复）：不再 eager 构造全局 _character_engine——改注册 runtime
        # registry + 默认会话 binding（→ 内置 Elysia instance）。engine 经 registry 按
        # instance 懒建：回合边界由 _prepare_turn 解析（registry.get → runtime.engine），
        # 公开只读接口 agent.character_engine 同样经 registry 解析当前 instance。
        # 默认 instance 需恒 pre-seed（PromptAssembler 快照路径 + 默认会话 binding），
        # 因此在此构造 default runtime + engine 并注册入 registry。
        from core.character.builtin_source import BuiltinCharacterSource

        self._runtime_registry = CharacterRuntimeRegistry()
        self._instance_repo = CharacterInstanceRepository(
            base_url=db_cfg.mongodb_uri,
        )
        # 默认会话 binding（内置 Elysia）——M3：binding 不再指 session.character_state，
        # 而是 instance 身份；_prepare_turn/character_engine 经此解析 runtime。
        self._default_binding: dict = {
            "character_id": "chr_builtin_elysia",
            "instance_id": "chi_builtin_elysia",
        }
        self._builtin_source = BuiltinCharacterSource()
        # Character REST and runtime loading share one repository/service graph.
        # Separate instances previously made a just-created custom definition
        # invisible to the Agent's runtime factory until another connection cycle.
        self._character_repo = CharacterRepository(base_url=db_cfg.mongodb_uri)
        self._character_revision_repo = CharacterRevisionRepository(
            base_url=db_cfg.mongodb_uri
        )
        self._character_catalog = CharacterCatalog(source=self._builtin_source)
        self._character_service = CharacterService(
            repo=self._character_repo,
            rev_repo=self._character_revision_repo,
            catalog=self._character_catalog,
            user_identity_store=self._user_identity_store,
            instance_repo=self._instance_repo,
            runtime_registry=self._runtime_registry,
            # BE-STAGEH-03：无状态预览口——一次性 prompt 组装 + 单次 LLM 推理
            preview_port=PromptCharacterPreviewAdapter(self._llm),
        )
        self._runtime_source_loader = CharacterRuntimeSourceLoader(
            characters=self._character_service,
            instances=self._instance_repo,
            builtins=self._builtin_source,
            owner_user_id=self._user_identity.userId,
        )

        def _assemble_runtime(
            definition: "CharacterDefinition",
            revision: "CharacterRevision",
            instance: "CharacterInstance",
        ) -> CharacterRuntime:
            """从已解析的精确 revision 构造一个 instance-scoped runtime。"""
            profile = compile_runtime_profile(definition, revision)
            character_config = profile.config
            # Task 7（记忆 v2 隔离）：scoped 三组件——memory_view（ScopedLongTermMemory）
            # + per-runtime consolidator/reflector。scope 按 instance 固定，view 复用同一
            # LongTermMemory（_long_term），但读写全部作用域过滤。
            scope = MemoryScope(
                user_id=self._user_identity.userId,
                character_instance_id=instance.instanceId,
            )
            memory_view = self._long_term.for_scope(scope)
            rt = CharacterRuntime(
                instance=instance,
                definition=definition,
                revision=revision,
                user_identity=self._user_identity,
                memory_view=memory_view,
                profile=profile,
                consolidator=MemoryConsolidator(
                    llm=self._llm,
                    memory=memory_view,
                    character_name=character_config.name,
                    user_name=self._user_name,
                    # #10（决策 #10）：未整合缓冲按 instance 落盘，下次启动补提。
                    # key 用 instance_id（不落文件名 → 无路径穿越面、不会降级串档）。
                    pending_key=instance.instanceId,
                ),
                # MemoryReflector 的 session 原用于 save_state() 计数器持久化；per-runtime
                # 后计数持久化改经 runtime.state（spec §2.6），构造不依赖 session——传 None。
                reflector=MemoryReflector(
                    llm=self._llm,
                    memory=memory_view,
                    session=None,
                    character_name=character_config.name,
                    user_name=self._user_name,
                ),
            )
            # M3（C1 修复）：每个 runtime 都接 persist_fn——否则 registry 经此构造出的
            # 非 default instance runtime 的 _persist_fn=None，flush() no-op，状态永不落库。
            # persist_fn 捕获本 runtime 自身 instance（不再硬编码 default_runtime.instance），
            # 并走 CharacterInstanceRepository.cas_state_version（I1：CAS 路径激活）。
            # 注：MemoryReflector 计数器 restore 由 _start_persistence 在 state 加载后恢复
            # （此处 _make_runtime 构造时 instance.state 为 fresh，不触发 restore——见 _lifecycle_persistence.py）。
            rt.set_persist_fn(self._persist_runtime_factory(rt))
            return rt

        def _make_runtime(instance_id: str) -> CharacterRuntime:
            """Synchronously construct only a trusted file-backed built-in."""
            if not instance_id.startswith("chi_builtin_"):
                raise RuntimeError(
                    f"custom runtime {instance_id} 必须通过异步存储 loader 构造"
                )
            role_name = instance_id.removeprefix("chi_builtin_")
            return _assemble_runtime(*self._builtin_source.load(role_name))

        async def _load_runtime(instance_id: str) -> CharacterRuntime:
            """Load an exact built-in/custom source and assemble its runtime."""
            source = await self._runtime_source_loader.load(instance_id)
            return _assemble_runtime(*source)

        def _make_engine(runtime: CharacterRuntime) -> CharacterEngine:
            """从 runtime 构造 CharacterEngine（M3: 每 instance 一个 engine，state 权威 =
            runtime.state，与 runtime.state 共享同一引用）。
            """
            profile = runtime.profile or compile_runtime_profile(
                runtime.definition, runtime.revision
            )
            runtime.profile = profile
            character_config = profile.config
            return CharacterEngine(
                llm=self._llm,
                state=runtime.state,
                character_name=character_config.name,
                user_name=self._user_name,
                base_personality=character_config.base_personality,
                character_config=character_config,
                output=self._output,
                version_repo=self._version_repo,
                instance_id=runtime.instance.instanceId,
                character_revision=runtime.revision.revision,
            )

        self._make_runtime = _make_runtime
        self._load_runtime = _load_runtime
        self._make_engine = _make_engine

        # 默认 instance（内置 Elysia）：构造 runtime + engine 并注册入 registry。
        # PromptAssembler 快照路径需要默认 persona engine（保留）。
        # 先建 runtime 再以其 state 建 engine——engine.state 与 runtime.state 共享同一
        # 引用（_make_engine 传 runtime.state）；次序颠倒会令 engine 挂到被丢弃 runtime 的 state。
        default_runtime = self._runtime_registry.get(
            self._default_binding["instance_id"],
            lambda: _make_runtime(self._default_binding["instance_id"]),
        )
        default_engine = _make_engine(default_runtime)
        default_runtime.bind_engine(default_engine)
        self._default_engine = default_engine
        # 默认会话 binding 指向内置 Elysia instance（M3 权威）。
        self._session_manager.set_character_binding(
            self._session_manager.active_id,
            self._default_binding,
        )

        self._prompt = PromptAssembler(
            self._character, self._session_manager.active, self._long_term,
            personality_engine=self._default_engine,
        )

        self._tts = None  # 在 _start_tts() 中创建

        self._stt: STTBackend | None = None  # STT 后端 — stt_transcribe() 读取，支持热切换

        self._stt_emotion_state: dict = {"emotion": "NEUTRAL", "events": []}  # STT 情绪/事件状态
        self._stt_ok = False  # STT 连通性标志 — 在 run_server() 中设置

        # Task 7（记忆 v2 隔离）：consolidator/memory_reflector 不再构造全局单例——
        # per-runtime 化后由 _make_runtime 构造挂 CharacterRuntime（registry 解析）。
        # loop 的 _consolidator/_memory_reflector 保持 None，仅作 after_response fallback。

        # ---- Token 统计 ----
        self._token_tracker: "TokenTracker | None" = None  # 在 start() 中创建（需要 session_id）
        self._prompt_recorder: "PromptRecorder | None" = None   # 在 start() 中创建
        self._response_recorder: "ResponseRecorder | None" = None # 在 start() 中创建

        # 批次 3（review 修复，兼容#2）：generation 生命周期持久化注册表由 Agent 持有，
        # 经构造注入 loop（loop 不再自建）——agent 与 loop 是同一实例；生命周期启动时
        # 对 registry 建连（connect），此后 register/mark 真正落库（不再全量 no-op）。
        from core.cognitive.generation_registry import GenerationRegistry
        # 复用上面那个实例（唯一仓储实例；级联清单与本注册表指向同一个对象）
        self._generation_registry = GenerationRegistry(repo=self._generation_repo)

        from core.cognitive import CognitiveLoop  # 函数级导入：打破循环导入
        self._cognitive = CognitiveLoop(
            llm=self._llm,
            fsm=self._fsm,
            event_bus=self._bus,
            output=self._output,
            prompt=self._prompt,
            session_resolver=lambda c, p: self.resolve_session(c, p),  # 批次 3：按 conversation_id + protocol_version 解析目标会话
            tool_adapter=self._tool_adapter,
            character_engine=self._default_engine,
            tts=self._tts,
            token_tracker=self._token_tracker,
            generation_registry=self._generation_registry,
            # Task 11（M3）：注入 runtime registry + factory——_prepare_turn 经
            # binding → registry.get(instance_id) → runtime.engine 解析当前回合引擎。
            runtime_registry=self._runtime_registry,
            runtime_factory=self._load_runtime,
            engine_factory=self._make_engine,
        )
        self._cognitive.configure(
            # Task 7（B1）：把用户身份存储传给 loop——_prepare_turn 冻结本轮 UserIdentity 快照（pronouns/称呼/时区）
            user_identity_store=self._user_identity_store,
            # _after_response 是独立 task，主 process 返回后仍会保存 SessionMemory；
            # 给它单独一份引用，避免删除后晚到 save upsert 复活幽灵会话。
            session_work_reserver=lambda sid: self._session_manager.reserve_work(sid, 2),
            session_work_releaser=self._session_manager.release_work,
        )

        # ---- Status & Phase Controller（#43：先建，IdleMonitor 共用同一个对象）----
        from core.status import FSMBackedStatus, FSMBackedPhaseController

        self.status: AgentStatus = FSMBackedStatus(self._fsm)

        # IdleMonitor 此前自建一个 `FSMBackedStatus(fsm)`——同一 FSM 上挂着两个状态
        # 包装器（相位一致，但生命周期轴看不见）。改为注入本 Agent 的 status：状态
        # 只有一个持有者，"谁能接活"也只有一个回答者。
        self._idle = IdleMonitor(
            character=self._character,
            fsm=self._fsm,
            event_bus=self._bus,
            status=self.status,
        )

        self.phases: CognitivePhaseController = FSMBackedPhaseController(
            fsm=self._fsm,
            bus=self._bus,
            output=self._output,
            tts_mgr=self._cognitive.tts_manager,
            status=self.status,
        )
        self._cognitive.phases = self.phases
        self._cognitive.status = self.status
        # 批次 3 review 修复（F1-a）：SessionManager.switch 忙时拒绝（生成/TTS 中
        # 不换绑 engine，防 state 竞态）——activate 入口 FSM 门禁的双保险。
        self._session_manager.set_busy_check(lambda: not self.status.is_accepting_input)

        # Worker Pool — 执行 AI 规划的工具子任务 + 后台任务
        #
        # checkpoint 路径走 ProjectPath 而非字面量（2026-09-14 CI 并行专项）：
        # 字面量 `"data/scheduler_checkpoint.json"` 是 CWD 相对路径，**忽略了
        # paths.py 已有的 `ELYSIA_PATH_SCHEDULER_CHECKPOINT` 覆盖**——于是
        # `pytest -n N` 的 N 个 worker 共享同一个文件与同一个 `<path>.tmp`
        # （scheduler.py 用 `<path> + ".tmp"` 做原子写），互相覆盖/删除/串读
        # 彼此 checkpoint。改走 ProjectPath 后根 conftest 的隔离 fixture 才生效。
        self._scheduler = ToolCallDispatcher(
            tool_executor=self._tool_adapter,
            tool_ctx=None,  # 在 start() 中通过 configure() 更新
            num_workers=5,
            checkpoint_path=str(ProjectPath.SCHEDULER_CHECKPOINT.resolve()),
        )

        # #43：`_state` **不再是独立存储**——见下方 `_state` property。
        # 生命周期轴与相位轴现在由 `self.status` 这一个对象持有。
        self._state = RunState.STOPPED
        self._event_task: asyncio.Task | None = None
        self._req_counter: int = 0
        # ARCH-INC-P2-03：后台协程按 owner 收口。
        #   _lifecycle_tasks — Agent 长期循环（event/mailbox/db-health/GC/embed）
        #   _turns           — 每回合认知任务（用户输入 / 主动搭话 / 后台任务唤醒）
        # 两者都由 Agent 独占，生命周期随 Agent 走；不持久化、不进任务中心。
        self._lifecycle_tasks = AsyncTaskScope("agent")
        self._turns = AsyncTaskScope("agent-turns")
        # FIFO 消息排队 — FSM != IDLE 时暂存消息
        self._pending_messages: list[_PendingMessage] = []
        # C1/P1-06：已真正开始执行的 cognitive task 集合——pre-start 任务不被后一
        # handler 取消（第二条消息排队），只在任务真正运行时才允许打断。
        self._started_cognitive_tasks: set[asyncio.Task] = set()

        # ---- WorkflowEngine ----
        self._task_system = None   # WorkflowEngine | None
        # F1（Batch 1.6）：工作流**自己拥有**的 task LLM backend
        # （`Module.llm.protocol.LLMBackend | None`）——`_start_task_system` 建、
        # `_stop_task_system` 关。它不随主 LLM 的热切换变化（修复前工作流借的是
        # 主适配器的 raw client，`switch_model` 会把那个 client 关掉）。
        self._task_llm_backend: LLMBackend | None = None
        self._delegation_preparer = None  # DelegationPreparer | None
        self._background_tasks: dict[WorkflowId, _BackgroundTaskEntry] = {}
        self._mailbox_task: asyncio.Task | None = None
        self._db_health_task: asyncio.Task | None = None
        self._embed_task: asyncio.Task | None = None
        self._attachment_gc_task: asyncio.Task | None = None

    # ---- CharacterRuntime 持久化（M3：per-instance persist_fn 闭合，C1/I1 修复）----

    def _persist_runtime_factory(
        self, runtime: CharacterRuntime,
    ) -> Callable[[CharacterRuntimeState], Awaitable[None]]:
        """构造 runtime 的 persist_fn（C1：捕获该 runtime 自身 instance，非硬编码 default）。

        flush 时把内存 CharacterRuntimeState 经 CharacterInstanceRepository
        .cas_state_version 写回 CharacterInstance.state。只有确认文档不存在时才走
        setOnInsert；CAS 冲突或存储异常必须 fail-closed，禁止用 upsert 覆盖并发状态。
        repo 连接在 _start_persistence（start()）完成，而 flush 在回合边界/shutdown
        触发——故此处经 self._instance_repo 延迟访问，不在此构造时断言已连接。
        """
        async def _persist(state: CharacterRuntimeState) -> None:
            inst = runtime.instance
            repo = self._instance_repo
            expected_version = inst.stateVersion
            new_version = expected_version + 1
            updated_at = now_iso()
            ok = await repo.cas_state_version(
                inst.instanceId,
                expected=expected_version,
                new_state=state,
                new_version=new_version,
            )
            if not ok:
                persisted = await repo.get(inst.instanceId)
                if persisted is not None:
                    raise CharacterStateConflictError(
                        inst.instanceId, persisted.stateVersion
                    )
                candidate = inst.model_copy(deep=True)
                candidate.state = state
                candidate.stateVersion = new_version
                candidate.updatedAt = updated_at
                if not await repo.save_if_absent(candidate):
                    persisted = await repo.get(inst.instanceId)
                    raise CharacterStateConflictError(
                        inst.instanceId,
                        None if persisted is None else persisted.stateVersion,
                    )

            # Advance the in-memory CAS token only after the durable write succeeds.
            inst.state = state
            inst.stateVersion = new_version
            inst.updatedAt = updated_at
            runtime.stateVersion = new_version
        return _persist

    # ---- process_message ----

    async def process_message(self, text: str = "", user_name: str = "",
                               content: list[dict] | None = None,
                               message_id: str | None = None,
                               protocol_version: int = 1,
                               conversation_id: str | None = None,
                               emotion: str = "",
                               events: list[str] | None = None,
                               origin_connection_id: str = "") -> bool:
        """处理单条用户消息，通过管道送入 CognitiveLoop。

        Agent 忙碌时消息入队（FIFO），IDLE 时出队处理。
        显式中断（停止按钮、VAD）仍通过 EventBus INTERRUPT 机制生效。

        Args:
            text: 纯文本消息（兼容旧调用）。
            user_name: 发送者名称，为空时使用配置中的默认用户名。
            content: 多模态内容数组（ChatMessageV2 content），新接口。
            message_id: v2 预分配用户消息 ID（ACK 一致，落库优先用）；v1 无则 None。
            protocol_version: 来源协议版本（1=v1，2=v2），默认 1。
            conversation_id: 批次 3（multi-session）目标会话 ID；v2 的 None 会在
                accepted 前解析并固化为当时活动会话，v1 仍由 loop 解析到 default。
            emotion: 语音 STT 情绪标签；文本消息为空。
            events: 语音 STT 事件标签；文本消息为空。
            origin_connection_id: CR-13 发起连接 ID（v2 握手分配）。透传到工具安全
                管道，使 HITL 高危工具审批只认发起连接；v1/REST 为空串（不绑定）。

        Returns:
            bool: True = 消息已接受（已入队或直发）；False = 消息为空或队列满被丢弃。
                调用方（v2 附件/sticker 路由）据此决定 ACK accepted / QUEUE_FULL rejected。
        """
        message_id_nt = MessageId(message_id) if message_id else None
        conversation_id_nt = SessionId(conversation_id) if conversation_id else None
        user_name = user_name or self._user_name
        if not text.strip() and not content:
            return False

        # CR-08：停机窗口拒收。准入判定原先只读 FSM 投影（`status.is_accepting_input`），
        # 而事件消费者读 `_state`——`stop()` 首行即置 STOPPED、消费者随即退出，
        # 但 FSM 仍显示 accepting，于是整个停机窗口（20 步清理，数秒~数十秒）内新消息
        # 仍被投进**无界** bus 队列并 `return True`：调用方回 ok、v2 还把 accepted 写进
        # 幂等表，同 clientRequestId 重试因而永远重放这个 ACK，重启也补不回来。
        # #43：判定收敛到 `status.accepts_new_turns`（两个轴合一）——修 CR-08 时是在
        # 每个入口各加一次 `_state != RUNNING`，那是"N 处都要记得"；现在只有一个定义点。
        # 注意这里**不能**用 `status.accepts_new_turns`：本入口的语义是"忙则排队"，
        # 而 lifecycle 未就绪要"拒收"——两者是不同处置。合起来问会让相位忙时改判拒收
        # （`test_busy_queue_threads_conversation_id` 就是这条回归）。下面分别问。
        if not self.status.is_service_running:
            logger.warning(
                "Agent 非运行态 (run_state=%s, fsm=%s) — 拒绝新消息，不投递 bus",
                self._state.name, self.status.current_name())
            return False

        # v2 在 ACK accepted 前原子固定实际目标（None 也解析成当时 active）并登记
        # accepted work。删除会话会在同一 SessionManager 生命周期锁下检查引用计数，
        # 因而 EventBus/pending/生成中的消息不会被删除后静默丢弃或改投新 active。
        session_work_reserved = False
        if int(protocol_version) >= 2:
            target = await self._session_manager.reserve_work(conversation_id, protocol_version)
            conversation_id_nt = SessionId(target.session_id)
            session_work_reserved = True

        rid = self.next_request_id()

        self._idle.touch()

        if not self.status.is_accepting_input or self._bus.pending_user_inputs > 0:
            # 排队消息而不是打断当前流程。review 复核 #4：bus 中已有未派发的
            # USER_INPUT（同 tick 突发，FSM 仍 accepting）同样视为 busy——否则多次
            # process_message 都走直接 post 返回 True，事件派发时才做 pre-start 队列
            # 容量检查并静默丢弃（先 ACK 后丢弃）。接受时即做容量检查。
            max_queue = self._config.agent.max_pending_messages
            if len(self._pending_messages) >= max_queue:
                logger.warning("消息队列已满 (%d) — 丢弃消息", max_queue)
                if session_work_reserved and conversation_id_nt is not None:
                    self._session_manager.release_work(conversation_id_nt)
                return False
            logger.debug("Agent busy — 消息排队 (state=%s, queue=%d)",
                       self.status.current_name(), len(self._pending_messages) + 1)
            self._pending_messages.append(_PendingMessage(
                text=text, user_name=user_name, request_id=rid,
                content=content, emotion=emotion, events=list(events or []),
                message_id=message_id_nt,
                conversation_id=conversation_id_nt,
                protocol_version=protocol_version,
                session_work_reserved=session_work_reserved,
                origin_connection_id=origin_connection_id,
            ))
            return True

        try:
            self._bus.post(UserInputData(
                text=text, user_name=user_name, request_id=rid, content=content,
                emotion=emotion, events=list(events or []), message_id=message_id_nt,
                origin_connection_id=origin_connection_id,
                protocol_version=protocol_version, conversation_id=conversation_id_nt,
                session_work_reserved=session_work_reserved))
        except Exception:
            if session_work_reserved and conversation_id_nt is not None:
                self._session_manager.release_work(conversation_id_nt)
            raise
        return True

    # ---- Background task lifecycle ----

    def _register_background_task(self, task_id: WorkflowId, mailbox: WorkflowMailbox, intent: str, dispatched_at: IsoTimestamp) -> None:
        """CognitiveLoop 提交后台任务后调用——注册到轮询列表。

        #6 硬杀窗口：**同步**落一条 WAL 派发记录。它有两个作用——① 让"派了任务但
        进程死了"在磁盘上可判定（启动时补一条 interrupted，而不是让用户永远等）；
        ② 终态记录本身不含 `original_intent`，只有这里能留下它（硬杀补投要靠它告诉
        用户"这是你当时让我做的哪件事"）。

        Args:
            task_id: 后台任务的唯一标识符。
            mailbox: asyncio.Queue，用于接收任务完成/失败事件。
            intent: 任务意图描述。
            dispatched_at: 任务派发时间（ISO 8601 格式）。
        """
        self._background_tasks[task_id] = _BackgroundTaskEntry(
            intent=intent,
            dispatched_at=dispatched_at,
            mailbox=mailbox,
        )
        self._task_result_wal.record_dispatch(str(task_id), intent, str(dispatched_at))

    def purge_message_from_session(self, msg_id: str) -> int:
        """从当前 session 中移除匹配 message_id 的消息。

        Archive 和 session 是独立存储，只删 archive 不改 session 会导致
        LLM prompt 仍然包含已删消息。此方法在 archive 删除时同步清理 session。

        Args:
            msg_id: 要移除的消息 ID。

        Returns:
            实际移除的消息数量（0 或 1）。
        """
        before = len(self.session.messages)
        kept = [m for m in self.session.messages if m.message_id != msg_id]
        purged = before - len(kept)
        if purged:
            self.session.messages.clear()
            for m in kept:
                self.session.messages.append(m)
        return purged

    async def _poll_mailboxes(self):
        """轮询所有活跃后台任务的 mailbox。每 500ms 一次。

        排空与投递的**唯一实现**在
        `_LifecycleTaskSystem.drain_mailboxes_once()`——关停路径
        （`_stop_mailbox`）调的是同一个方法，避免"轮询 vs 关停"两条路径
        各写一份而漂移（S2-A4）。本方法因此只剩"节奏控制"。
        """
        while self._state == RunState.RUNNING:
            await asyncio.sleep(0.5)
            await self.drain_mailboxes_once()

    # ---- Event dispatching ----

    async def _process_events(self) -> None:
        """主事件循环：从 EventBus 消费事件并分发。

        认知处理在独立任务中运行，保证事件循环在 LLM 流式输出期间
        仍能响应 INTERRUPT 事件。
        """
        while self._state == RunState.RUNNING:
            try:
                event = await self._bus.next()
            except RuntimeError:
                break

            data = event.data
            if data is None:
                continue

            # 从事件数据提取 request_id 并设置 ContextVar（修复跨 task 传播）
            rid = getattr(data, "request_id", "")
            if rid:
                set_request_id(rid)

            # S3-03：**per-event 异常边界**。此前任一 handler 抛异常都会冒泡出
            # 本方法，永久杀死这个唯一的事件消费者（`_lifecycle_tools.py` 只在
            # 启动时 spawn 一次，无 watchdog）——表现为 Agent "进程在线但完全不理人"，
            # `_pending_user_inputs` 只增不减。单个事件处理失败不该让整个 Agent 失聪。
            try:
                await self._dispatch_event(data)
            except asyncio.CancelledError:
                raise                      # 取消是控制流，必须继续传播
            except Exception:
                logger.error("事件处理失败（已跳过该事件，消费者继续）type=%s",
                             type(data).__name__, exc_info=True)

    async def _dispatch_event(self, data: Any) -> None:
        """按事件类型分派（S3-03 抽出的方法体，供 per-event 异常边界包裹）。

        S3-23：**补 else 分支**。此前未登记的类型落到函数末尾被**静默丢弃**——
        零日志、零计数，事件发出去了、消费端当没看见。报告把这类"`else` 分支的
        系统性缺失"列为根因簇 RC-7：它让"发出去没人处理"与"处理成功"在观测面上
        完全一样。典型现场就是 `EventBusHITLNotifier` post 的 `hitl_request`：
        `EventData` 判别联合里没有它，于是审批卡片永远不会出现、也没有任何提示。
        """
        if isinstance(data, InterruptData):
            self._handle_interrupt(data)
        elif isinstance(data, UserInputData):
            self._handle_user_input(data)
        elif isinstance(data, SystemTickData):
            self._handle_system_tick(data)
        elif isinstance(data, TaskResultData):
            await self._handle_task_result(data)
        elif isinstance(data, StateChangeData):
            await self._handle_state_change(data)
        else:
            self._unhandled_events += 1
            logger.warning(
                "EventBus: 未登记的事件类型被丢弃 type=%s（共 %d 次）——"
                "发布方与消费方的类型表不一致；若这是新功能，需在 _dispatch_event "
                "登记，否则它在观测面上与「处理成功」无法区分",
                type(data).__name__, self._unhandled_events)

    def _handle_interrupt(self, data: InterruptData) -> None:
        """处理 INTERRUPT 事件：cancel cognitive task，并按类型处置排队事件。

        中断**必须**插队（INTERRUPT=0 越过 STATE_CHANGE=1 / NORMAL=2，见
        `core/monitoring/event_bus.py`）——但插队不等于有资格销毁别人的事件。
        逐类策略（Issue B / B-2，原实现只写"丢弃其余排队事件"）：

        | 排队事件          | 处置                                          |
        |-------------------|-----------------------------------------------|
        | `StateChangeData` | 保留（原位重投）——前端 UI 需要它              |
        | `UserInputData`   | 释放会话 work 后丢弃（用户已取消这条输入）    |
        | `TaskResultData`  | **原位重投**——持久化过的用户可见终态，不可丢  |
        | 其他              | 丢弃（未登记类型，保持既有行为）              |

        为什么 `TaskResultData` 必须重投而不是丢：在 Issue B 的 B-1 之后，"落库成功
        即清 WAL 副本"仍然成立，于是队列里这一条可能就是这条终态在**进程内的全部
        形态**；丢掉它 = 用户永久且静默地等不到后台任务结果（详见
        `_lifecycle_task_system.py` 顶部的投递状态机）。重投**同步**发生在 drain
        循环内（循环里没有 `await`）⇒ 不存在"取出后还没放回就被抢占"的窗口；
        重投是**搬移**不是复制（`drain()` 已把原件取出），所以不会重复消费。

        Args:
            data: 中断事件数据，包含中断来源等信息。
        """
        set_request_id("-")
        logger.info("EventBus: INTERRUPT (%s)", data.source)
        # 批次 3：source 贯通到 loop 的 cancel 路径（mark 拆分由 Task 9 接入）
        self._cognitive.cancel_current(source=data.source)
        for drained in self._bus.drain():
            if isinstance(drained.data, StateChangeData):
                self._bus.post(drained.data, priority=drained.priority,
                               type=EventType.STATE_CHANGE)
            elif isinstance(drained.data, UserInputData):
                self._release_session_work(
                    drained.data.conversation_id,
                    drained.data.session_work_reserved,
                )
            elif isinstance(drained.data, TaskResultData):
                # Issue B（B-2）：后台任务终态是**持久化过**的结果，用户正等着它。
                # priority / type 原样保留（NORMAL / task_result），下一个循环就能消费。
                self._bus.post(drained.data, priority=drained.priority,
                               type=drained.type)
        for pending in self._pending_messages:
            self._release_pending_session_work(pending)
        self._pending_messages.clear()

    def _release_session_work(self, conversation_id: SessionId | None,
                              reserved: bool) -> None:
        """释放一份 v2 accepted work；供完成、排队丢弃、中断和 shutdown 共用。"""
        if reserved and conversation_id is not None:
            self._session_manager.release_work(conversation_id)

    def _release_pending_session_work(self, pending: _PendingMessage | dict) -> None:
        """释放 pending 引用；兼容少量旧测试/迁移代码使用的 dict 队列项。"""
        if isinstance(pending, dict):
            conversation_id = pending.get("conversation_id")
            reserved = bool(pending.get("session_work_reserved", False))
        else:
            conversation_id = pending.conversation_id
            reserved = pending.session_work_reserved
        self._release_session_work(conversation_id, reserved)

    def _handle_user_input(self, data: UserInputData) -> None:
        """处理 USER_INPUT 事件：创建 cognitive task 异步运行。

        Args:
            data: 用户输入事件数据，包含文本、用户名、情感、多模态内容等。
        """
        text = data.text
        user_name = data.user_name
        stt_emotion = data.emotion
        stt_events = data.events
        content = data.content
        rid = data.request_id
        message_id = data.message_id
        protocol_version = data.protocol_version
        logger.debug("EventBus: USER_INPUT 到达 (来源=%s, STT=%s)",
                   "websocket" if stt_emotion else "text", stt_emotion or "无")
        # C1/P1-06：前一条 cognitive task 若尚未真正开始（同 tick 第二条已 accepted 的
        # 消息，pre-start）——不得隐式取消它（否则已 ACK 消息静默丢失且 lease 泄漏），
        # 改为入队等待；只有任务真正在跑时才允许「新输入打断当前生成」。
        prev = self._cognitive.current_task
        if prev is not None and not prev.done() and prev not in self._started_cognitive_tasks:
            max_queue = self._config.agent.max_pending_messages
            if len(self._pending_messages) >= max_queue:
                self._release_session_work(data.conversation_id, data.session_work_reserved)
                return
            logger.debug("Agent pre-start——新消息排队 (queue=%d)",
                       len(self._pending_messages) + 1)
            self._pending_messages.append(_PendingMessage(
                text=data.text, user_name=data.user_name, request_id=data.request_id,
                content=data.content, emotion=data.emotion, events=list(data.events or []),
                message_id=data.message_id,
                conversation_id=data.conversation_id,
                protocol_version=data.protocol_version,
                session_work_reserved=data.session_work_reserved,
                origin_connection_id=data.origin_connection_id,
            ))
            return
        # 取消上一个未完成的 task（仅真正在跑时），防止快速连续事件造成孤儿
        self._cognitive.cancel_current()
        _on_tool_calls = lambda calls: self._scheduler.dispatch_tool_calls(
            request_id=rid, calls=calls,
        )

        async def _run_with_session_lease() -> None:
            # 标记任务已真正开始（进入 coroutine）——pre-start 判定依据
            task = asyncio.current_task()
            if task is not None:
                self._started_cognitive_tasks.add(task)
            await self._cognitive.process(
                text, user_name=user_name,
                stt_emotion=stt_emotion,
                stt_events=stt_events,
                _on_tool_calls=_on_tool_calls,
                content=content,
                message_id=message_id,
                protocol_version=protocol_version,
                conversation_id=data.conversation_id,
                origin_connection_id=data.origin_connection_id,
            )

        def _lease_release_done(task: asyncio.Task) -> None:
            """done callback：唯一 lease 释放点（正常/异常/取消都会触发，恰一次）。

            C1/P1-06：pre-start cancel 不进入 coroutine finally——必须经 done
            callback 释放，否则 work lease 永久泄漏、删除持续 409 busy。释放恰好
            一次（不在 finally 里重复释放，否则会误扣其他在途消息的 lease）。
            """
            self._started_cognitive_tasks.discard(task)
            self._release_session_work(
                data.conversation_id, data.session_work_reserved)

        try:
            task = self._turns.spawn(
                _run_with_session_lease(),
                name=f"cognitive-{text[:20] if text else 'multimodal'}",
            )
        except Exception:
            self._release_session_work(
                data.conversation_id, data.session_work_reserved)
            raise
        task.add_done_callback(_lease_release_done)
        self._cognitive.current_task = task

    def _handle_system_tick(self, data: SystemTickData) -> None:
        """处理 SYSTEM_TICK 事件——silence_timeout 时主动开启话题。

        Args:
            data: 系统定时事件数据，包含触发原因和已过秒数。
        """
        # TODO: time_of_day 事件未处理——producer 在 idle_monitor._check_proactive 中，
        #       此处只消费 silence_timeout。C5/P2-09：未知 proactive kind 不再静默吞——
        #       记 warning 便于后续实现/观测（明确消费行为）。
        if data.reason == "silence_timeout":
            elapsed = data.elapsed or 0
            rid = self.next_request_id()
            set_request_id(rid)
            _on_tool_calls = lambda calls: self._scheduler.dispatch_tool_calls(
                request_id=rid, calls=calls,
            )
            self._cognitive.cancel_current()
            task = self._turns.spawn(
                self._cognitive.process(
                    f"（系统提示：用户已经{int(elapsed)}秒没有说话了，请自然地开启一个话题）",
                    _on_tool_calls=_on_tool_calls,
                ),
                name="cognitive-proactive"
            )
            self._cognitive.current_task = task
        else:
            logger.warning("SYSTEM_TICK 未知 proactive reason=%s（暂未消费）", data.reason)

    async def _handle_task_result(self, data: TaskResultData) -> None:
        """处理 TASK_RESULT 事件——后台任务完成，唤醒 CognitiveLoop。

        Issue B（B-1）：**durable ack 在这里，不在入队处**。`self._turns.spawn(...)`
        成功 = 承载这条结果的宣告回合已被接受（认知循环取得所有权）——只有到这一步
        才把 `delivered=True` 写进库。此前在 `drain_mailboxes_once` 里按
        `_state == RUNNING` 标记，把"消费者协程活着"当成了"这条结果已被认领"，
        而队列里的元素可能在 `bus.post` 返回后立刻被 INTERRUPT 插队并 drain 掉
        （`_handle_interrupt`），于是"库里已投递 + WAL 已清 + 无人消费"三处皆无。

        **spawn 抛异常时刻意不标记**：异常按原样冒泡到 `_process_events` 的
        per-event 异常边界（消费者不死），库里那条保持 `delivered=False`，
        下次启动由 `_redeliver_pending_task_results` 补投。

        由此得到的语义是 **at-least-once**：在"spawn 成功"与"mark 落库"之间崩溃
        （或 Mongo 写失败），下次启动会**重复宣告一次**同一个终态结果。
        这是有意选的取舍——at-most-once（投递确认早于消费）的失败模式是**静默丢失**
        （用户永远等不到结果，且日志干净），而 at-least-once 的失败模式只是**重复
        告知**一次；后者可被用户直接看穿并忽略，前者不可观测。仓库既有的容忍度
        表述见 `_mark_task_result_delivered` 的 docstring（"最坏是下次启动重复投递"）。

        Args:
            data: 任务结果事件数据，包含 task_id、结果文本和原始意图。
        """
        rid = self.next_request_id()
        set_request_id(rid)
        self._cognitive.cancel_current()
        task = self._turns.spawn(
            self._cognitive.process(
                user_message="",
                is_task_result=True,
                task_result_context=data.model_dump(),
            ),
            name=f"task-result-{data.task_id}",
        )
        self._cognitive.current_task = task
        # 所有权已转移（宣告回合被接受）→ 现在才允许写投递确认。
        # 这一行**必须**留在 spawn 之后：放到它前面（或放回入队处）就是 Issue B 原缺陷。
        await self._mark_task_result_delivered(data.task_id)

    async def _handle_state_change(self, data: StateChangeData) -> None:
        """处理 STATE_CHANGE 事件——FSM 状态变更。

        IDLE/IDLE_BACKGROUND_TASK 时检查排队消息（FIFO 出队），
        通知所有输出通道状态变更（前端据此切换动画）。

        Args:
            data: 状态变更事件数据，包含新状态名称。
        """
        state = data.state
        logger.info("FSM → %s", state)
        if state in (AgentState.IDLE, AgentState.IDLE_BACKGROUND_TASK):
            set_request_id("-")
            if self._pending_messages:
                pending = self._pending_messages.pop(0)
                logger.debug("排队消息出队 (%d 条剩余)", len(self._pending_messages))
                try:
                    self._bus.post(UserInputData(
                        text=pending.text,
                        user_name=pending.user_name,
                        request_id=pending.request_id,
                        content=pending.content,
                        emotion=pending.emotion,
                        events=list(pending.events or []),
                        message_id=pending.message_id,  # 批次 2：忙时排队不丢预分配 ID
                        # review 修复（F3/链路#1）：透传 conversation_id + protocol_version——
                        # 否则 v2 排队消息出队后降级为 v1 语义（恒 default），丢失会话归属。
                        conversation_id=pending.conversation_id,
                        protocol_version=pending.protocol_version,
                        session_work_reserved=pending.session_work_reserved,
                        origin_connection_id=pending.origin_connection_id,
                    ))
                except Exception:
                    self._release_session_work(
                        pending.conversation_id, pending.session_work_reserved)
                    raise
        await self._output.notify_state(state)

    # 工具注册在 start() 中经 ToolAdapter.start() 完成

    # ---- 公开只读接口（薄 getter 直通）----
    # 43 个 @property：通往 Agent 组合根持有的子系统，可写性由子系统 API 自管。
    # 批次 3 新增 session_manager / active_session_id / pending_messages。

    @property
    def session(self):
        """当前会话内存（SessionManager 活动会话，switch 后指向新会话）。"""
        return self._session_manager.active

    @property
    def session_manager(self):
        """会话管理器（多会话缓存池 + 活动指针）。"""
        return self._session_manager

    @property
    def active_session_id(self):
        """当前活动会话 ID（SessionId/str）。"""
        return self._session_manager.active_id

    @property
    def pending_messages(self):
        """FIFO 队列中暂存的消息（只读快照视图，busy 时排队未出队）。"""
        return list(self._pending_messages)

    async def resolve_session(self, conversation_id: str | None,
                              protocol_version: int):
        """按协议版本 + conversation_id 解析目标会话并确保加载（直通 SessionManager）。"""
        return await self._session_manager.resolve(conversation_id, protocol_version)

    async def switch_session(self, sid: SessionId):
        """Load the bound runtime before changing the active-session pointer."""
        # Keep Agent's historical internal lazy-switch semantics. Product v2
        # activate routes validate has_session() before calling this method, while
        # tests/embedded callers may intentionally switch to a fresh local session.
        target = await self._session_manager.ensure_loaded(sid)
        binding = target.character_binding or self._default_binding
        instance_id = binding.get("instance_id") or self._default_binding["instance_id"]
        await self.ensure_character_runtime(instance_id)
        switched = await self._session_manager.switch(sid)
        # S2-C3：**切换后重放该会话的服务开关三态**。`ServiceState` 是 SessionMemory 的
        # 字段（每会话持久化），而它原先只在启动步骤被应用一次——切到另一个会话后内存
        # 槽位保持上一个会话的值，前端读 capabilities（取自内存槽位）与服务端持久化值
        # 不一致，"重启后开关回退"。实测：切到 stt_enabled=True 的会话后
        # `agent.stt_enabled` 仍是 False。
        await self._apply_service_enabled()
        return switched

    async def ensure_character_runtime(self, instance_id: str) -> CharacterRuntime:
        """Ensure a built-in/custom runtime and its engine are ready for UI/chat."""
        runtime = await self._runtime_registry.get_or_load(
            instance_id, lambda: self._load_runtime(instance_id)
        )
        runtime.ensure_engine(lambda: self._make_engine(runtime))
        return runtime

    @property
    def long_term(self):
        """长期记忆（Milvus 向量库）。"""
        return self._long_term

    @property
    def message_repo(self):
        """消息归档仓库。"""
        return self._message_repo

    @property
    def session_repo(self):
        """会话仓库。"""
        return self._session_repo

    @property
    def memory_repo(self):
        """长期记忆仓库。"""
        return self._memory_repo

    @property
    def token_repo(self):
        """token 用量仓库。"""
        return self._token_repo

    @property
    def background_task_repo(self):
        """后台任务终态结果仓库（#6：跨重启不丢 + 启动补投）。"""
        return self._background_task_result_repo

    @property
    def attachment_repo(self):
        """附件仓库（fail-closed 存储，懒初始化；首访时构造真实 repo）。"""
        if self._attachment_repo is None:
            db_cfg = self._config.database
            self._attachment_repo = AttachmentRepository(
                database_name=db_cfg.db_name,
                base_url=db_cfg.mongodb_uri,
            )
        return self._attachment_repo

    @property
    def idempotency_repo(self):
        """幂等 claim 仓库（D6c，懒初始化；首访构造真实 repo）。"""
        if self._idempotency_repo is None:
            db_cfg = self._config.database
            self._idempotency_repo = IdempotencyRepository(
                database_name=db_cfg.db_name,
                base_url=db_cfg.mongodb_uri,
            )
        return self._idempotency_repo

    @property
    def tool_adapter(self):
        """工具子系统适配器。"""
        return self._tool_adapter

    @property
    def output(self):
        """多通道输出管理器。"""
        return self._output

    @property
    def llm(self):
        """LLM 适配器。"""
        return self._llm

    @property
    def tts(self):
        """TTS 流管理器（启动后才有，可能为 None）。"""
        return self._tts

    @property
    def tts_manager(self):
        """TTS 流管理器（播放控制，BE-063 audio.cancel 入口）。"""
        return self._cognitive.tts_manager

    @property
    def stt(self):
        """STT 后端（只读，可能为 None）。"""
        return self._stt

    @property
    def stt_enabled(self) -> bool:
        """STT 有效开关——**派生**（#43）。

        唯一权威是当前会话的 `session.service_state.stt_enabled`（三态：None = 用户从未
        显式设置 → 回退 `config.stt.enabled`）。此前还有一个 Agent 内存槽位
        `_stt_enabled` 与它并行，两者靠"记得同步"维持——那正是 S2-C3「四个真相源」里
        的两个。槽位删除后，切会话不需要任何"同步"动作：读的就是新会话的值。
        """
        return self._effective_service_enabled("stt")

    @property
    def tts_enabled(self) -> bool:
        """TTS 有效开关——派生，同 `stt_enabled`（#43）。"""
        return self._effective_service_enabled("tts")

    @property
    def stt_emotion_state(self):
        """STT 语音情绪/事件状态（transcribe 后更新）。"""
        return self._stt_emotion_state

    @property
    def config(self):
        """应用配置。"""
        return self._config

    @property
    def bus(self):
        """事件总线。"""
        return self._bus

    @property
    def cognitive(self):
        """认知循环。"""
        return self._cognitive

    @property
    def generation_registry(self):
        """generation 生命周期注册表（Agent 持有并注入 loop，启动时 connect）。"""
        return self._generation_registry

    @property
    def desktop_bridge(self):
        """Desktop 工具桥（只读——批次 4 BE-041/043）。

        Task 7/8 经此访问：on_client_connect/on_client_disconnect（ws_handler 握手）、
        get_call/pending_calls（REST 状态查询）、request_and_wait（插件 execute 委托）。
        连接/断开后调 `agent.sync_desktop_available()` 同步 ToolManager 的
        desktop 可用性（plugin_list/registry_metadata available 数据源）。
        """
        return self._desktop_bridge

    def sync_desktop_available(self) -> None:
        """同步 desktop 工具可用性到 ToolManager（bridge 连接/断开时调用）。

        公开入口（2026-08-14 模块化修复——原私有 `_sync_desktop_available` 被输入层
        跨模块直调，违反「走公开接口」纪律，公开化后 ws_handler 正常调用）。
        ws_handler 在 `bridge.on_client_connect/disconnect` 后调用本入口，
        把 `bridge.is_available()` 经 `tool_adapter.set_desktop_available`
        写入 `_desktop_available`——plugin_list/registry_metadata 中 desktop 工具
        available 字段的数据源（BE-040 §3.2）。
        """
        self._tool_adapter.set_desktop_available(
            self._desktop_bridge.is_available())

    @property
    def idle(self):
        """空闲监控。"""
        return self._idle

    @property
    def scheduler(self):
        """任务调度器。"""
        return self._scheduler

    @property
    def consolidator(self):
        """当前实例的 memory consolidator（debug 展示用）。

        Task 7（记忆 v2 隔离）：不再返回全局单例——per-runtime 化后经 registry 解析
        default runtime 的 consolidator。注意：不调 registry.get(..., lambda: None)——get
        会把 factory() 结果存入 dict，lambda: None 会污染 _runtimes（后续 _evict_if_needed
        访问 rt.dirty 崩溃）。agent 属性是 _runtime_registry（非 _registry）。
        """
        rt = self._runtime_registry._runtimes.get(DEFAULT_INSTANCE_ID)  # noqa: SLF001
        return rt.consolidator if rt else None

    @property
    def memory_reflector(self):
        """当前实例的长期记忆反思（debug 展示用）。

        Task 7（记忆 v2 隔离）：同 consolidator——经 registry 解析 default runtime。
        不调 registry.get(..., lambda: None)（避免 lambda: None 污染 _runtimes）。
        """
        rt = self._runtime_registry._runtimes.get(DEFAULT_INSTANCE_ID)  # noqa: SLF001
        return rt.reflector if rt else None

    @property
    def character_engine(self) -> "CharacterEngineProtocol":
        """角色引擎（当前 instance）。Task 11（M3）：经 registry 解析，非全局单例。

        返回类型是**接口**（`CharacterEngineProtocol`）而非具体类：Agent 是构造
        `CharacterEngine` 的组合根，但对外只承诺契约——引擎可单独迭代或整体替换，
        消费方（cognitive / character / prompt / debug）不受影响。

        当前活动会话的 binding → instance_id → registry.get(instance_id).engine。
        binding 缺失回退默认内置 Elysia instance。
        """
        binding = self._session_manager.active.character_binding or self._default_binding
        instance_id = binding.get("instance_id") or self._default_binding["instance_id"]
        runtime = self._runtime_registry.get_existing(instance_id)
        if runtime is None:
            # This synchronous debug/read interface cannot perform Mongo I/O.
            # Built-ins can still be materialised from trusted files; custom
            # runtimes are preloaded by start/create/switch/turn preparation.
            runtime = self._runtime_registry.get(
                instance_id, lambda: self._make_runtime(instance_id)
            )
        return runtime.ensure_engine(lambda: self._make_engine(runtime))

    @property
    def character_runtime_registry(self) -> "CharacterRuntimeRegistry":
        """角色运行时注册表(公开只读)。

        阶段 G: REST /api/character-instances/{id}/state 经此解析 instance runtime。
        跨模块消费禁止 agent._xxx,故公开;注册表内部 get_existing 为只读不懒建。
        """
        return self._runtime_registry

    @property
    def character_state_stream(self) -> bool:
        """角色状态流开关(阶段 G,恒 true——本批实现 snapshot/回合边界事件)。

        与 build_server_info 的 ServerInfoFeatures.character_state_stream 保持一致,
        避免 ws_handler/finalize 的发射 gate 与 capabilities 各自硬编码漂移。
        """
        return True

    @property
    def character_profiles(self) -> bool:
        """角色资料能力开关（Stage H runtime/prompt/state 门禁已闭环）。"""
        return True

    @property
    def character_reflector(self):
        """角色反思器（当前 instance engine.reflector）。"""
        return self.character_engine.reflector

    @property
    def character(self):
        """角色配置（L1 Core + config）。"""
        return self._character

    @property
    def character_service(self) -> "CharacterService":
        """角色服务（启动时创建，公开只读）。

        REST 装配（`core/input/server.py` 的 `create_app`）**必须复用**本实例：
        另建一个 `CharacterService()` 会让 `app["character_service"]` 与 Agent 运行时
        加载器持有两套 repository 图，角色改动在两条路径上互相不可见。此前经
        `getattr(agent, "_character_service")` 私有穿透获取。
        """
        return self._character_service

    @property
    def instance_repo(self) -> CharacterInstanceRepository:
        """角色实例仓库（启动时创建，公开只读）。

        同上——REST 角色实例端点与绑定服务须与 Agent 共用同一实例图；此前经
        `getattr(agent, "_instance_repo")` 私有穿透获取。
        """
        return self._instance_repo

    @property
    def token_tracker(self):
        """token 统计（启动时创建）。"""
        return self._token_tracker

    @property
    def prompt_recorder(self):
        """LLM prompt 记录器（启动时创建）。"""
        return self._prompt_recorder

    @property
    def response_recorder(self):
        """LLM 响应记录器（启动时创建）。"""
        return self._response_recorder

    @property
    def user_name(self):
        """注入的用户名。"""
        return self._user_name

    @property
    def user_identity_store(self) -> UserIdentityStore:
        """用户身份存储（Mongo 权威 + 进程内缓存）——探针/恢复与 REST 共用。"""
        return self._user_identity_store

    @property
    def state(self) -> RunState:
        """Agent 运行状态（只读）。"""
        return self._state


    @property
    def started_at(self):
        """启动时间戳（启动时创建）。"""
        return self._started_at

    @property
    def stt_ok(self):
        """STT 可用标志。"""
        return self._stt_ok

    async def stt_transcribe(self, audio: bytes) -> str:
        """STT 转写（公开接口）。读取当前 agent.stt 后端，支持热切换。"""
        backend = self.stt
        if backend is None:
            return ""  # shutdown 窗口 _stop_* 置 None 后 _stop_output 才关 channel
        text = await backend.transcribe(audio)
        self.stt_emotion_state["emotion"] = getattr(backend, "last_emotion", "NEUTRAL")
        self.stt_emotion_state["events"] = list(getattr(backend, "last_events", None) or [])
        return text

    # ---- 状态写方法（引导主循环退出，不做清理）----

    def request_shutdown(self) -> None:
        """请求关机——设 _state = STOPPING，引导主循环退出。"""
        self._state = RunState.STOPPING

    def request_restart(self) -> None:
        """请求重启——设 _state = RESTARTING，引导主循环退出。"""
        self._state = RunState.RESTARTING
