"""Agent 生命周期 — WorkflowEngine + mailbox 启停。"""
from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any, Callable, Coroutine

from Module._common.task_scope import AsyncTaskScope, cancel_and_drain
from Module.workflow_contract.schema import WorkflowId
from core.agent._types import RunState
from core.schema.event import TaskResultData

if TYPE_CHECKING:
    from core.monitoring.event_bus import EventBus
    from core.cognitive import CognitiveLoop
    from core.adapter.tool_adapter import ToolAdapter
    from Module.workflow_engine.workflow_engine import WorkflowEngine
    from core.cognitive.delegation import DelegationPreparer
    from core.adapter.llm_adapter import LLMAdapter
    from core.schema.types import IsoTimestamp
    from core.agent._types import _BackgroundTaskEntry
    from Module.llm.protocol import LLMBackend

logger = logging.getLogger(__name__)

#: 终态事件类型（与 `Module/workflow_engine/mailbox.py::_TERMINAL_EVENTS` 同一集合的
#: 字符串形态）。终态事件 → 呈现文本的唯一实现点是 `terminal_payload()`——轮询排空、
#: WAL 硬杀补投两条路径共用它，避免同一条结果出现两种措辞。
TERMINAL_KINDS = frozenset({"completed", "failed", "cancelled"})

#: 硬杀（SIGKILL / 掉电 / OOM）后 WAL 里"只有派发记录、没有终态记录"的任务措辞。
#: 与优雅停机的"任务因服务重启被中断，未产生结果"（见 `_stop_mailbox`）区分：
#: 那条能确定是正常退出/`/restart`，这条只说明进程是异常终止的。
INTERRUPTED_BY_HARD_KILL_TEXT = "任务因服务进程异常终止被中断，未产生结果"


def terminal_payload(kind: str, data: dict) -> tuple[str, bool]:
    """终态事件 → (呈现给用户的文本, is_error)。

    **唯一的措辞实现点**：`drain_mailboxes_once`（正常/停机排空）与
    `_replay_task_result_wal`（硬杀补投）都调它。两处各写一份必然漂移——本项目反复
    出现的「两条路径不对称」缺陷族。

    Args:
        kind: 终态事件类型值（completed / failed / cancelled）。
        data: 终态事件的原始 data。

    Returns:
        (文本, is_error)；未知 kind → ("", False)。
    """
    if kind == "completed":
        return str(data.get("result", "")), False
    if kind == "failed":
        return f"任务执行失败: {data.get('error', '未知错误')}", True
    if kind == "cancelled":
        return f"任务已取消: {data.get('reason', '用户取消')}", True
    return "", False


# ══════════════════════════════════════════════════════════════════
# 终态结果的投递状态机 —— Issue B 的不可丢失不变量（唯一定义点）
# ══════════════════════════════════════════════════════════════════
#
# 一条 `TaskResultData` 的生命周期只有这几个状态：
#
#   persisted    已落库（`background_task_results`，写库时 `delivered=False`）
#   queued       已 `bus.post` 进 EventBus，**无人**持有它的所有权
#   accepted     消费方（`_handle_task_result`）已 spawn 出承载它的宣告回合——
#                所有权转移完成。**只有到这一步才允许写 `delivered=True`**
#   consumed     宣告回合已真正读过 `task_result_context`（进程内，不可观测）
#   delivered    库中 `delivered=True`（= accepted 已持久化，跨重启不再补投）
#   WAL removed  本地 WAL 副本已清（落库成功后立即发生，见 `_persist_task_result`）
#
# 允许的迁移：
#   persisted → queued → accepted → consumed → delivered
#   persisted → WAL removed                 落库成功后清 WAL 副本（库为权威）
#   queued    → queued                      中断 drain 后**原位重投**（搬移，不是复制）
#
# **禁止的迁移**（Issue B 的原缺陷正是它）：
#
#   WAL removed ∧ delivered=True ∧ ¬accepted
#
#   ——"库说已投递、WAL 副本已清，而消费方从未取得所有权"。此时这条结果
#      WAL / EventBus / 消费方**三处皆无**，用户永久且静默地失去它。
#   派生的禁止项：在 `persisted` 那一步就写 `delivered=True`（把 durable ack 挂在
#   **入队**上）。入队只证明"进了队列"，不证明"有人接手"——`_state == RUNNING`
#   是"消费者协程活着"这个**假设**，不是"这条结果已被认领"这个不变量。
#
# 不变量（每一刻都必须成立）：一条 `TaskResultData` 至少在**一处**——
# WAL / 仓储、EventBus 队列、消费方——是活的。
#
# 因此 durable ack 的唯一授予点是 `_handle_task_result` 中 spawn 成功之后；
# `drain_mailboxes_once` 与 `_redeliver_pending_task_results` **只投递、不确认**。


class _LifecycleTaskSystem:
    """WorkflowEngine + mailbox 生命周期 mixin。"""

    if TYPE_CHECKING:
        _llm: LLMAdapter
        _tool_adapter: ToolAdapter
        _bus: EventBus
        _task_system: WorkflowEngine | None
        _task_llm_backend: LLMBackend | None
        _delegation_preparer: DelegationPreparer | None
        _cognitive: CognitiveLoop
        _user_name: str
        _register_background_task: Callable[[str, Any, str, IsoTimestamp], None]
        _mailbox_task: asyncio.Task[None] | None
        _lifecycle_tasks: AsyncTaskScope
        _poll_mailboxes: Callable[[], Coroutine[Any, Any, None]]
        _background_tasks: dict[WorkflowId, _BackgroundTaskEntry]
        _background_task_result_repo: Any
        _task_result_wal: Any
        _state: "RunState"

    #: 任务终态结果的一次 Mongo 读写预算（#6）。目标 = Mongo 驱动（外部 I/O，仓库内
    #: 没有更内层的预算）→ 按"叶子预算"登记在 `scripts/check_timeout_budget.py`。
    #: **为什么必须有界**：upsert/投递标记跑在**关停路径**上，而驱动默认
    #: `serverSelectionTimeoutMS` 是 30s——"Mongo 网络黑洞（连不上但也没报错）"会让
    #: 一条落库把停机拖住 30s，用户看到的是"关不掉"。2s 远超本地 Mongo 的单次写
    #: （毫秒级），超时即降级为"本次不落库"（只记 warning，投递不受影响）。
    _TASK_RESULT_STORE_TIMEOUT = 2.0

    def _bg_result_repo(self) -> Any:
        """终态结果仓储（#6）；未接线（单测替身 / 从未建连）→ None（只投递不落库）。

        只读 `_background_task_result_repo` 这**一个**字段、不走 `agent.background_task_repo`
        属性：关停路径上绝不能触发懒构造（那会在停机中途新开一条 Mongo 连接）。
        """
        return getattr(self, "_background_task_result_repo", None)

    def _wal(self) -> Any:
        """本地终态 WAL（#6 硬杀窗口）；未接线（单测替身）→ None。

        与 `_bg_result_repo` 同样只读单一字段：`terminal_sink` 要**同步**调用，
        不能在里面做懒构造（惰性 import/建对象会把一次落盘变成不可预期的耗时）。
        """
        return getattr(self, "_task_result_wal", None)

    def _record_terminal_wal(self, task_id: str, event: str, data: dict) -> None:
        """`push_mailbox_event` 的同步 sink（#6 硬杀窗口）。

        **必须是同步的普通方法**（不是 coroutine）：engine 在推终态事件的那一行直接
        调用它，靠"调用返回时记录已 fsync 到盘"来消掉硬杀窗口。写失败由 WAL 自己
        吞掉并计数（见 `TaskResultWAL.failure_count`），不影响任务终态推送。
        """
        wal = self._wal()
        if wal is None:
            return
        wal.record_terminal(task_id, event, data)

    async def _persist_task_result(
        self, data: TaskResultData, *, status: str, delivered: bool,
    ) -> bool:
        """把一条终态结果落库，返回是否落库成功（失败只记日志，绝不抛）。

        **为什么落库不能失败即中断投递**：跨重启不丢是"更好"，而"当下就告诉用户"
        是"必须"——Mongo 离线时宁可只丢跨重启保障，也不能连本次投递一起丢。
        故失败降级为 warning（日志里有明确的一行，不是静默）。

        Args:
            data: 终态事件数据（消费侧看到的同一条）。
            status: completed / failed / cancelled / interrupted。
            delivered: 写库时的投递标记。**Issue B 之后生产调用方一律传 False**
                （`drain_mailboxes_once` / `_replay_task_result_wal` / `_stop_mailbox`
                的 interrupted 落库）：durable ack 的唯一授予点是消费侧的
                `_handle_task_result`（spawn 成功后 `mark_delivered`）。
                参数保留是因为仓储（`upsert_pending`）仍支持写入期置位，
                删它与本次修复无关（见报告的 follow-up）。

        Returns:
            True 表示已落库。
        """
        repo = self._bg_result_repo()
        if repo is None:
            return False
        try:
            await asyncio.wait_for(
                repo.upsert_pending(
                    task_id=data.task_id,
                    result=data.result,
                    original_intent=data.original_intent,
                    dispatched_at=data.dispatched_at,
                    status=status,
                    is_error=data.is_error,
                    delivered=delivered,
                ),
                timeout=self._TASK_RESULT_STORE_TIMEOUT)
            # #6 硬杀窗口：库里已有这条 = WAL 里的副本冗余，清掉。
            # 放在 upsert 成功之后（同步，中间没有 await）——反过来的话，"清了 WAL
            # 但落库失败"就会两头都没有。真出现"落库成功、清理前崩溃"也无害：启动
            # replay 会因"库里有记录"跳过它（见 `_replay_task_result_wal`）。
            wal = self._wal()
            if wal is not None:
                wal.forget([str(data.task_id)])
            return True
        except Exception:
            logger.warning(
                "后台任务终态落库失败（本次投递不受影响；跨重启保障降级）task_id=%s",
                data.task_id, exc_info=True)
            return False

    async def _mark_task_result_delivered(self, task_id: Any) -> None:
        """标记终态结果已投递给活跃 Agent（失败只记日志——最坏是下次启动重复投递）。"""
        repo = self._bg_result_repo()
        if repo is None:
            return
        try:
            await asyncio.wait_for(
                repo.mark_delivered(task_id),
                timeout=self._TASK_RESULT_STORE_TIMEOUT)
        except Exception:
            logger.warning(
                "后台任务终态标记已投递失败（下次启动可能重复投递）task_id=%s",
                task_id, exc_info=True)

    async def _start_task_system(self) -> None:
        """创建 WorkflowEngine + DelegationPreparer + HITLNotifier，注入 CognitiveLoop。"""
        from Module.workflow_engine.workflow_engine import WorkflowEngine
        from core.cognitive.delegation import DelegationPreparer
        from Module.workflow_contract.infra import AgentInfra, resolve_task_system_model
        from Module.workflow_contract import ConstraintConfig

        # F1（Batch 1.6）：工作流的 LLM 资源**归工作流自己所有**。
        # 修复前这里取的是 `self._llm.get_raw_client()`——主 LLM 适配器当前后端的
        # raw client。而 `LLMAdapter.switch_model()` 会在热切换时把**换下来的旧
        # backend 后台 close 掉**（`core/adapter/llm_adapter.py` 的
        # `_closing_backends.try_spawn(close_meth())`），于是切换之后工作流手里的
        # 就是一个已关闭的对象：任何后台任务 LLM 调用抛
        # `APIConnectionError ← RuntimeError: Cannot send a request, as the client
        # has been closed.`（Batch 1.5 探针实测 `is_closed()` False → True）。
        # 那是**资源生命周期所有权错误**（A 创建/关闭、B 长期持有），不是接缝问题。
        # 建不起来（provider 配置缺欠 / custom 条目解析不出 / runtime != backend）
        # 一律降级为 None——**绝不抛**：本方法是 `_STARTUP_STEPS` 的一步，抛出去会经
        # `AgentLifecycle.start()` 回滚整只 Agent，把"某个 provider 配置缺欠"升级成
        # "整个应用起不来"（正是 Issue C 修过的形态）。
        # 模型名只解析一次：建 backend 用的名字与引擎请求里 `model=` 用的名字
        # 必须**是同一个值**（不是"两次调用同一个函数"）。
        task_model_name = resolve_task_system_model()
        task_llm = None
        try:
            task_llm = self._build_task_llm_backend(task_model_name)
        except Exception:
            logger.warning(
                "工作流 task LLM 构建失败——后台任务委派不可用（submit_task 不注册）；"
                "Agent 其余功能不受影响", exc_info=True)
        self._task_llm_backend = task_llm
        raw_llm = task_llm.raw_client if task_llm is not None else None

        _ts_infra = AgentInfra(
            llm=raw_llm,  # type: ignore[arg-type]
            milvus=None,
            http=None,
            tool_registry=self._tool_adapter,
            constraints=ConstraintConfig(),
            # 模型名与上面那个 backend 用的**同一个**值（`resolve_task_system_model`
            # 是 env `TASK_SYSTEM_MODEL` 的唯一读取点，不在本文件再读一次 env）。
            llm_model=task_model_name,
            # #6：终态结果持久化面（engine 侧可读、Agent 侧读写；停机落库 + 启动补投）
            background_task_results=self._bg_result_repo(),
            # #6 硬杀窗口：终态**同步**落盘 sink（engine 挂在每个 mailbox 上）
            terminal_result_sink=self._record_terminal_wal,
        )

        from core.hitl import EventBusHITLNotifier

        _hitl = EventBusHITLNotifier(self._bus)
        self._task_system = WorkflowEngine(infra=_ts_infra, hitl_notifier=_hitl)
        await self._task_system.start()
        logger.info("WorkflowEngine started")
        # 注入 WorkflowEngine 到 ToolContext，供 get_runtime_config 查询活跃任务
        self._tool_adapter.tool_context.services["_task_system"] = self._task_system
        # Issue C：委派准备器复用 engine 那**同一个** client（不读 provider 环境变量、
        # 不自建第二份连接），于是"委派可用性"与"引擎可用性"不可能分歧。
        # F1 之后这个 client 是工作流自持的 task LLM，而不是主 LLM 的借出品。
        # 运行时交不出可用 client → 用**既有**的"不可用"表示（`None`）：
        # `CognitiveLoop` 据此不把 submit_task 注册给 LLM（`core/cognitive/loop.py`
        # 的 `_delegation_preparer is not None` 判据），调用点返回显式文案
        # （`core/cognitive/tools.py` 的 submit_task guard）。引擎自身照常启动，
        # 可选/必需关系不变。
        if raw_llm is None:
            self._delegation_preparer = None
            logger.warning(
                "DelegationPreparer 未创建：工作流的 task LLM 不可用"
                "（模型=%s，构建失败或主 provider 的 runtime != backend）——"
                "后台任务委派不可用（submit_task 不注册）；"
                "Agent 其余功能不受影响", task_model_name)
        else:
            self._delegation_preparer = DelegationPreparer(_ts_infra)

        # 注入到 CognitiveLoop
        from core.cognitive.user_profile import UserProfile
        self._cognitive.configure(
            task_system=self._task_system,
            delegation_preparer=self._delegation_preparer,
            user_profile=UserProfile(display_name=self._user_name),
            on_background_task=self._register_background_task,
        )

    def _build_task_llm_backend(self, task_model_name: str) -> "LLMBackend | None":
        """建一个**工作流自己拥有**的 task LLM backend（F1）。

        Args:
            task_model_name: 裸模型名（`resolve_task_system_model()` 的结果，由调用方
                解析一次后传入——建 backend 的名字与引擎请求里的 `model=` 必须是同一个值）。

        连接参数（base_url / api_key / 出站守卫）按**主 LLM 的模型标识**解析，模型名
        用 `TASK_SYSTEM_MODEL`（裸名）——两边拼成 `"<主 provider>/<task 模型名>"`。
        这么拼的理由：

        - `TASK_SYSTEM_MODEL` 的契约是**裸模型名**（它被原样塞进 API 的 `model=`，
          见 `AgentInfra.llm_model` 与 Batch 1.5 的不变式测试），编不出自己的
          `provider/` 前缀；provider 只能从主模型取。
        - `custom/<name>` 的 base_url/api_key 挂在**条目**上，裸模型名对不上任何条目，
          所以参数必须按主模型标识解析（`_resolve_switch_params` 顺带完成 SSRF 校验
          与出站守卫工厂注入——**复用**既有的那一处实现，不抄第四份）。
        - 复用 `LLMFactory.create` 也意味着 `supports_thinking` / `supports_prefix`
          与主 provider 同源（`Module/config/providers.yaml`）。

        **返回 None 表示"工作流没有可用的 LLM"**（与 `raw_client=None` 同一个语义），
        调用方据此走既有降级路径——本方法自己**不吞**异常，由调用方统一收口
        （便于测试从构造函数注入失败）。

        Returns:
            可用的 `LLMBackend`；主 provider 的 runtime 不是 backend 时返回 None。

        Raises:
            Exception: provider 配置缺欠（缺 base_url / 未知 provider / custom 条目
                解析失败）——由 `_start_task_system` 统一降级。
        """
        from Module.llm import LLMFactory
        from core.agent.server import _resolve_switch_params

        main_model_id = self._llm.model
        provider = main_model_id.split("/", 1)[0] if "/" in main_model_id else main_model_id
        task_model_id = f"{provider}/{task_model_name}"

        params, runtime, http_client_factory = _resolve_switch_params("llm", main_model_id)
        if runtime != "backend":
            # desktop/local Profile 由 Electron 执行，后端不代理（与 switch_llm 同判据）
            logger.warning(
                "主 LLM Profile %s 的 runtime=%s，后台不由后端执行——"
                "工作流 task LLM 不创建", main_model_id, runtime)
            return None
        if http_client_factory is not None:
            params["http_client_factory"] = http_client_factory
        backend = LLMFactory.create(task_model_id, **params)
        logger.info(
            "工作流 task LLM: %s（provider 取自**此刻**的主模型 %s——热切换不影响已建好的"
            "这一个，重启后按当时的主模型重建）；分配/释放归工作流启停"
            "（_start_task_system / _stop_task_system）",
            task_model_id, main_model_id)
        return backend

    def _start_mailbox_polling(self) -> None:
        """启动后台任务 mailbox 轮询（独立 asyncio.Task）。"""
        self._mailbox_task = self._lifecycle_tasks.spawn(
            self._poll_mailboxes(), name="agent-mailbox")

    # ---- mailbox 排空与投递（唯一实现点）----

    async def drain_mailboxes_once(self) -> int:
        """排空所有活跃后台任务的 mailbox 并投递终态事件，返回投递条数。

        **轮询路径（`_poll_mailboxes`）与关停路径（`_stop_mailbox`）共用本方法**。
        两条路径各写一份必然漂移——本项目反复出现的「两条路径不对称」缺陷族。

        S2-A4：为什么关停路径也必须调它。`stop()` 第一行就把 `_state` 置 STOPPED，
        轮询循环条件随即为假；而 `WorkflowEngine.stop()` 排在关停步骤的后段，中间
        隔着 drain_cognitive + 4 个 flush + db_health。该窗口内完成/失败/取消的后台
        任务，其终态事件落在 mailbox 里**无人读**，而重启路径会新建 WorkflowEngine、
        旧 mailbox 一起丢弃——用户永远等不到「任务完成」唤醒。

        mailbox 本身是**有界保留**队列（终态事件优先保留，只淘汰最旧的 progress），
        所以"晚一点统一读"不会丢事件，只要求读的时候注册表还在。

        #6 + Issue B：本方法**只做两件事**——① 落库（`delivered=False`，**无条件**）、
        ② 投递进总线。它**不**授予 durable ack：那条 ack 的唯一授予点是消费侧的
        `_handle_task_result`（spawn 成功 = 所有权转移完成），见文件顶部的投递状态机。

        为什么 `delivered` 不再耦合 `_state == RUNNING`（Issue B / B-1）：那条判据
        把"消费者协程活着"当成"这条结果已被认领"。二者不是一回事——`bus.post` 返回
        时队列里可能马上就被 INTERRUPT 插队并 drain 掉（INTERRUPT=0 越过 NORMAL=2），
        而 `_persist_task_result` 落库成功即清 WAL 副本。于是"库里 delivered=True +
        WAL 已清 + 无人消费"三处皆无是可达的，用户永久静默地失去终态结果。
        关停路径现在也**自动**正确：它的 post 进的是死总线，没人消费 → 记录保持
        `delivered=False` → 下次启动补投（不再依赖任何一条路径上读 `_state`）。

        是 async 方法：落库要 await。落库失败不阻断投递（见 `_persist_task_result`）。
        """
        delivered = 0
        for task_id, info in list(self._background_tasks.items()):
            mailbox = info.mailbox
            if mailbox is None:
                continue
            try:
                while True:
                    # 两种 mailbox 协议都要容忍，且语义不同：`asyncio.Queue` 空时
                    # **抛 QueueEmpty**，`WorkflowMailbox`（生产实现）空时**返回
                    # None**。只处理一种会让另一种走进外层 except 刷假 warning。
                    try:
                        event = mailbox.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                    if event is None:
                        break
                    data = event.data or {}
                    kind = event.type.value
                    if kind not in TERMINAL_KINDS:
                        continue  # progress / approval_needed 等非终态：不投递、不 pop
                    # 措辞走唯一实现点（与硬杀补投共用）——两处各写一份必然漂移。
                    text, is_error = terminal_payload(kind, data)
                    payload = TaskResultData(
                        task_id=task_id,
                        result=text,
                        original_intent=info.intent,
                        dispatched_at=info.dispatched_at,
                        is_error=is_error,
                    )
                    status = kind
                    if kind == "failed":
                        logger.warning(
                            "后台任务失败: %s — %s",
                            info.intent, data.get('error', '未知错误'),
                        )
                    elif kind == "cancelled":
                        # E3：取消事件改推 CANCELLED——消费侧须 pop 条目 + 呈现结果，
                        # 否则 _background_tasks 条目永久驻留（直到下次 start 清理）。
                        logger.info(
                            "后台任务已取消: %s — %s",
                            info.intent, data.get('reason', '用户取消'),
                        )
                    # Batch 1.7（Challenger C4）：**条目在这里就摘掉**，早于下面那个
                    # `await _persist_task_result`。终态事件已经从 mailbox 取走、所有权
                    # 归本方法，再把它留在 `_background_tasks` 里只会制造一个危险窗口：
                    # 停机时轮询被 `cancel_and_drain` 掐断（恰好在 await 上）→ 条目仍在
                    # → `_stop_mailbox` 的 stranded 分支把**真实终态覆写**成
                    # `interrupted`（"任务因服务重启被中断，未产生结果"），接着启动
                    # replay 又把 `interrupted` 当权威、`wal.forget()` 删掉真正的终态。
                    # 摘除与"取走事件"之间**没有任何 await**，所以这个窗口结构上不存在。
                    # 落库失败也不丢：`_persist_task_result` 只在 upsert 成功后清 WAL，
                    # 所以那一刻终态仍在 WAL 里，下次启动 replay 会补回（库为权威）。
                    self._background_tasks.pop(task_id, None)
                    # #6：先落库再投递（"投了但没人消费"因此在磁盘上可判定）。
                    # delivered 恒 False：本方法只负责入队，投递确认在消费侧授予。
                    await self._persist_task_result(
                        payload, status=status, delivered=False)
                    self._bus.post(payload)
                    delivered += 1
                    break  # task 已终态，处理下一个 task
            except Exception:
                # 处理单个事件时的意外错误（event.data 访问 / TaskResultData
                # 构造 / bus.post 等）——不中断整体排空，但必须留日志诊断。
                logger.warning(
                    "mailbox 投递事件异常（task_id=%s）", task_id, exc_info=True,
                )
        return delivered

    async def _stop_mailbox(self) -> None:
        """停止后台任务 mailbox 轮询。

        S2-A4：顺序是 **① 同步排空并投递 → ② 取消轮询任务 → ③ 清注册表**。
        - `stop_task_system` 已在本步之前完成（`_SHUTDOWN_STEPS` 顺序），所以此刻
          所有后台任务都已终态、终态事件都已写入各自 mailbox；
        - 轮询任务早在 `stop()` 入口 `_state` 置 STOPPED 时就停转了，停机窗口内到达
          的事件只有这一次同步排空能读到；
        - 注册表**必须**留到本步才清——在 `stop()` 入口清会让上面这次排空无条目可查
          （这正是 S2-A4 的原缺陷）。

        #6 / Issue B：本步的投递落进的是**死总线**（`_process_events` 是关停第一步就
        被取消的），所以那些事件只落库不送达——由下次启动的补投真正送给用户。
        这个结论现在**不依赖任何 `_state` 判据**：`drain_mailboxes_once` 一律写
        `delivered=False`，而死总线里没人消费 → 记录自然留在待补投集合里（Issue B
        之前这里靠 `consumer_alive` 手工判定，与"入队 ≠ 被认领"是两回事）。
        仍未终态的任务（引擎已停，终态事件不会再有了）也**落库为 interrupted**：
        否则"用户等一个永远不会来的结果"只留在日志里，重启后连日志都没有。
        """
        delivered = await self.drain_mailboxes_once()
        if delivered:
            logger.info(
                "mailbox 收尾排空：%d 条后台任务终态事件已落库待补投（停机窗口内完成）",
                delivered)
        await cancel_and_drain(
            self._mailbox_task, timeout=3.0, label="agent-mailbox")
        self._mailbox_task = None
        # 仍未终态的任务：终态事件不会再有了（引擎已停）。不留静默丢失——逐条记
        # warning，并落库为 interrupted（#6：跨重启不丢，启动时告知用户"被重启打断"）。
        stranded = [(tid, e.intent) for tid, e in self._background_tasks.items()]
        if stranded:
            logger.warning(
                "停机时仍有 %d 个后台任务未见终态——本次进程内不会再投递"
                "（/restart 会新建 WorkflowEngine，旧任务与 mailbox 一并丢弃）: %s",
                len(stranded), stranded)
            for task_id, intent in stranded:
                entry = self._background_tasks[task_id]
                interrupted = TaskResultData(
                    task_id=task_id,
                    result="任务因服务重启被中断，未产生结果",
                    original_intent=intent,
                    dispatched_at=entry.dispatched_at,
                    is_error=True,
                )
                await self._persist_task_result(
                    interrupted, status="interrupted", delivered=False)
        self._background_tasks.clear()

    async def _redeliver_pending_task_results(self) -> None:
        """启动补投（#6）：把上一进程未送达的后台任务终态重新投进总线。

        为什么必须在**启动**做：关停路径的排空只能把事件投进死总线（见 `_stop_mailbox`），
        而那些事件是用户唯一能得到的"任务结果"。补投后由 `_handle_task_result` 唤醒
        认知循环，用户下次看会话就能看到结果（回复同样落归档）。

        Issue B（B-1）：本方法**只 post，不 mark**。在入队那一刻写 `delivered=True`
        与在 `drain_mailboxes_once` 里那样写是同一个谎——入队只证明"进了队列"。
        投递确认由消费侧的 `_handle_task_result`（spawn 成功之后）授予：
        - 消费成功 → 记录转 `delivered=True`，不再待补投（不会每次启动重复告知）；
        - 消费前进程又挂了 → 记录仍是 `delivered=False`，下次启动再补投一次
          （at-least-once：宁可重复，不可丢失）。

        读失败（Mongo 离线）→ 记 warning 后返回，记录仍在库里，下轮启动再补投。
        """
        repo = self._bg_result_repo()
        if repo is None:
            return
        try:
            pending = await asyncio.wait_for(
                repo.list_pending(), timeout=self._TASK_RESULT_STORE_TIMEOUT)
        except Exception:
            logger.warning("启动补投后台任务终态失败（记录仍在库中，下次启动再试）",
                           exc_info=True)
            return
        if not pending:
            return
        for doc in pending:
            payload = TaskResultData(
                task_id=doc.get("task_id", ""),
                result=doc.get("result", ""),
                original_intent=doc.get("original_intent", ""),
                dispatched_at=str(doc.get("dispatched_at") or ""),
                is_error=bool(doc.get("is_error", False)),
            )
            self._bus.post(payload)
            # Issue B（B-1）：**不**在这里 mark_delivered——投递确认属于消费侧。
            # 若在这里标记，"已入队"又会冒充"已认领"（原缺陷的同一根因）。
        logger.info("启动补投 %d 条后台任务终态（上次进程未送达）", len(pending))

    async def _replay_task_result_wal(self) -> None:
        """把本地 WAL 里的候选终态补进仓储（#6 硬杀窗口），必须在补投之前。

        **它补的是哪一段**：`background_task_results` 的落库点在 mailbox 轮询上
        （0.5s 周期）。进程在"终态已产生、还没被排空落库"这一窗口内被硬杀（SIGKILL /
        掉电 / OOM），优雅停机一行代码都跑不到，那条终态就只能靠本地 WAL 找回来。
        WAL 在**终态产生的那一刻**同步 fsync（见 `Module/workflow_engine/mailbox.py`
        的 `_wal_terminal`），所以窗口被压到"任务终态与 fsync 之间"。

        三类候选：
        - 有终态记录 → 按该终态补（措辞走 `terminal_payload`，与正常排空同一实现点）；
        - 只有派发记录 → 进程死在任务中途，补 `interrupted`（措辞见常量）；
        - **库里已有记录 → 跳过**（Mongo 是权威；这条判据同时消掉"落库成功但 WAL
          还没清理"残留导致的重复告知）。

        失败语义：仓储不可用（Mongo 离线）/ 单条判重失败 → 记 warning 后**保留 WAL
        并停止**（下轮启动再试）。绝不"假装补过了"——那才会真的丢。
        """
        wal = self._wal()
        if wal is None:
            return
        candidates = wal.replay()
        if not candidates:
            return
        repo = self._bg_result_repo()
        if repo is None:
            # 没有仓储可补 → 保留 WAL。进程若一直不接存储，WAL 会一直留着（它是
            # 唯一副本，不能删）。
            return
        done: list[str] = []
        already: int = 0
        for cand in candidates:
            task_id = str(cand["task_id"])
            try:
                existing = await asyncio.wait_for(
                    repo.get(task_id), timeout=self._TASK_RESULT_STORE_TIMEOUT)
            except Exception:
                logger.warning(
                    "启动 WAL 补录：查库失败，保留 WAL 待下次启动（task_id=%s）",
                    task_id, exc_info=True)
                break
            if existing is not None:
                done.append(task_id)  # 库是权威：库里已有 = WAL 副本冗余
                already += 1
                continue
            event = cand.get("event")
            if isinstance(event, str) and event in TERMINAL_KINDS:
                text, is_error = terminal_payload(event, cand.get("data") or {})
                status = event
            else:
                text, is_error = INTERRUPTED_BY_HARD_KILL_TEXT, True
                status = "interrupted"
            payload = TaskResultData(
                # WAL 里是裸 str（磁盘上的 JSON），core 边界按 NewType 收口
                # （`docs/code-style-guide.md`：ID 在边界处包一层）。
                task_id=WorkflowId(task_id),
                result=text,
                original_intent=str(cand.get("original_intent") or ""),
                dispatched_at=str(cand.get("dispatched_at") or ""),
                is_error=is_error,
            )
            if await self._persist_task_result(payload, status=status, delivered=False):
                done.append(task_id)
            else:
                break  # 落库失败 → 保留这条（及后续）待下次启动
        if done:
            wal.forget(done)
            logger.info(
                "启动 WAL 补录 %d 条后台任务终态（进程上次非正常终止）"
                "——其中 %d 条库中已有，仅清理 WAL 副本；%d 条为本轮补录",
                len(done), already, len(done) - already)

    # 外层预算必须 ≥ `WorkflowEngine.stop()` 的**整条最坏路径**，不是它的单个
    # `timeout` 参数。S2-A3 的缺陷正是外层 5.0s < 内层 10.0s：只要有任意任务仍在
    # RUNNING（后台任务引擎的常态），外层必然先超时，把 stop() 从中间掐断，于是
    # 后续 `task.cancel()`、孤儿工具收尾、兜底清理**全部不执行**，运行中的任务在
    # 已关闭的 ToolAdapter/LLM/Mongo 上继续 await，semaphore 也永不释放。
    #
    # 内部算术（`Module/workflow_engine/workflow_engine.py::stop`）：
    #   等任务自然完成 10s + cancel 后二次等待 3s + 孤儿工具 3s + cancel 落地 1s
    #   ≈ 17s
    # 取 20s 留余量。**改内层任何一段都要同步改这里**——两条预算是一组不变量。
    _TASK_SYSTEM_STOP_BUDGET = 20.0

    #: F1（Batch 1.6）：工作流自持的 task LLM backend 的一次关闭预算。它是**叶子**——
    #: 目标是 `OpenAICompatBackend.close()`（→ `AsyncOpenAI.close()`），仓库内没有更
    #: 内层的预算；与 `_stop_llm_and_tts` 关主 LLM 用的是同一个 3s 量级。超时只记
    #: warning：关不掉的连接池由进程退出回收，不能把停机拖在这里。
    _TASK_LLM_CLOSE_BUDGET = 3.0

    async def _stop_task_system(self) -> None:
        """停止 WorkflowEngine，再关工作流**自己拥有**的 task LLM。

        ## 为什么关闭放在 `finally`（Challenger H1 的修复点）

        「顺序」与「一定发生」是两件事。顺序上必须**先** `WorkflowEngine.stop()`
        （它先等在途任务落定，内部最坏 ≈17s）**再**关 task LLM，否则会掐掉正在跑的
        请求。但"先"不等于"到达"：`Agent.stop()` 按 CR-02 逐步吞掉每个步骤的
        `BaseException` 继续往下走，所以只要 `WorkflowEngine.stop()` 抛出
        **TimeoutError 以外**的东西（`RuntimeError` / `CancelledError`——后者正是
        用户再按一次 Ctrl+C 的形态），控制流就跳过了关闭语句：backend 泄漏，
        `_task_llm_backend` 还留在身上，下一次 `start()` 直接把引用覆盖掉 ⇒
        **永久泄漏**（修复前工作流不拥有任何 LLM 资源，故这个洞不存在）。

        因此：**先取走并置 None**（所有权当场转移给本方法），关闭放 `finally`——
        正常 / 超时 / 抛错 / 被取消四种出口都关得到，且异常语义原样向上传（不吞
        `CancelledError`，那是 `Agent.stop()` 的职责）。
        """
        task_llm, self._task_llm_backend = self._task_llm_backend, None
        try:
            if self._task_system:
                try:
                    await asyncio.wait_for(
                        self._task_system.stop(),
                        timeout=self._TASK_SYSTEM_STOP_BUDGET)
                except asyncio.TimeoutError:
                    # 超时说明 stop() 自身卡住（内部预算已用尽仍未返回）——留可见痕迹，
                    # 但不改流程：_SHUTDOWN_STEPS 会继续走后面的清理。
                    logger.error(
                        "WorkflowEngine stop 超时（>%.0fs，超过其内部最坏路径）——"
                        "后台任务未收尾，semaphore 可能未释放",
                        self._TASK_SYSTEM_STOP_BUDGET)
        finally:
            # F1（Batch 1.6）：关闭**工作流自己拥有**的 task LLM。
            # 取到局部变量并立刻置 None ⇒ 本方法可能被调两次（关停步骤是幂等的），
            # 而"恰好关一次"是所有权的一部分（Test F1-4 钉住 close 计数 == 1）。
            if task_llm is not None:
                try:
                    await asyncio.wait_for(
                        task_llm.close(), timeout=self._TASK_LLM_CLOSE_BUDGET)
                except asyncio.TimeoutError:
                    logger.warning(
                        "工作流 task LLM 关闭超时（>%.0fs）", self._TASK_LLM_CLOSE_BUDGET)
                except Exception:
                    logger.warning("工作流 task LLM 关闭失败", exc_info=True)
        # 注：**不**在这里关闭主 LLM 的 client——它是 `LLMAdapter` 的资源，由
        # `stop_llm_tts` 步骤统一关闭（本步在它之前）。在这里关会掐掉停机序列后续
        # 步骤（scheduler / tool_adapter 收尾）仍在用的连接池。
        # `/restart` 也不因此泄漏：`Agent.start()` 首行 `_llm.reconnect()` 重建**主**
        # client，而重启会重新走 `_start_task_system` → 建一个全新的 task LLM。
