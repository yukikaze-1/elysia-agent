"""UserIdentityStore — 用户身份存取（Mongo 权威 + 进程内只读缓存）。

BE-STAGEH-02：Mongo `user_identity_profiles` 是权威事实源（`UserIdentityRepository`）；
本 store 只持有读缓存，供同步读路径（`get()`）使用。无 repo 装配（单测/显式本地
模式）时降级为内存 + 可选 JSON 文件。

P0-3（fail-closed）：
- 装配了 repo 时**绝不**伪造本地 profile——`connect()` 装载失败即 `get()` 返回
  None、`is_available` 为 False，依赖身份的业务（`/api/me` 写、preview、会话创建、
  generation）一律 fail-closed 返回稳定公开错误；
- `health_check()` 供数据库恢复协调器探测；Mongo 恢复后 `connect()` 重新装载权威
  档案并原子替换缓存（不覆盖 Mongo 中更高 revision 的文档）。

N-11（failure truth 归属，Batch 4）：装载失败是**本 store 的事实**，不是 `connect()`
调用方的日志行——`connect()` 仍按既有契约返回 None（调用链据此 fail-closed，不 raise
到 `Agent.start()` 引起整体回滚），但失败原因由本对象持有（`last_error`，只读、进程内、
**绝不进 HTTP**）。这样「当前身份可用吗」与「上一次为什么不可用」两个问题都有权威答主，
而不是只有一行 warning。判定可用性的权威仍是 `is_available`——**没有**第二套 ready 标志。

## 两个谓词回答两个不同问题（勿合并）

| 谓词 | 问题 | 权威输入 |
|---|---|---|
| `is_available` | 依赖身份的产品路径**现在能不能服务**？ | `_cached is not None` 且 repo 已连接 |
| `health_check()` | 权威库**现在通不通**（恢复协调器探针）？ | `repo.health_check()` 真实 ping（会重连） |

「ping 通」**不**蕴含「身份已装载」：库里文档不合域模型时 repo 连得好好的，而
`_cached` 恒为 None。故 `/ready` 的 identity 门禁必须读 `is_available`，读
`health_check()` 会假绿（N-1）。

## 冲突元数据的权威（N-3 / Batch 6）

CAS 冲突时「现在到第几版」的**唯一权威**是 Mongo 文档的 `revision` 字段，由
repository 在冲突发生点回读并随 `UserIdentityUpdateOutcome.current_revision` 带上来
（见 `core/storage/user_identity_repo.py`）。本 store 的 `_cached` 是**读缓存，不是
权威**：多 writer 场景下它按定义就是陈旧的，409 的 `details.currentRevision`
**绝不**从它取值。同一道理禁止 `expected + 1`（可能跳过多次写入）。
"""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path

from core.profile.user_identity import (
    UserIdentity,
    UserIdentityUpdateOutcome,
    LOCAL_DEFAULT_PRONOMS,
    LOCAL_DEFAULT_USER_ID,
)
from core.storage.base import StorageUnavailableError
from core.storage.user_identity_repo import UserIdentityRepository

logger = logging.getLogger(__name__)


class UserIdentityStore:
    """UserIdentity 存取（Mongo 权威 + 进程内缓存）。"""

    def __init__(self, *, storage_path: str | None = None,
                 repo: UserIdentityRepository | None = None):
        self._storage_path = Path(storage_path) if storage_path else None
        self._repo = repo
        self._cached: UserIdentity | None = None
        self._lock = threading.RLock()
        # N-11：上一次失败装载的异常类型名（None = 无失败记录）。只存类型名——
        # 完整消息可能含连接串/文件路径，而本属性虽是进程内诊断仍不放任扩散。
        self._last_error: str | None = None

    # ---- 权威性判定 ----

    @property
    def is_authoritative(self) -> bool:
        """是否装配了权威 repo（生产 Agent 装配路径）。"""
        return self._repo is not None

    @property
    def is_available(self) -> bool:
        """权威身份是否**当前可用**（依赖身份的产品路径能否服务）。非权威模式恒 True。"""
        if self._repo is None:
            return True
        return self._cached is not None and self._repo.is_connected

    @property
    def last_error(self) -> str | None:
        """最近一次**未被后续成功状态消解**的装载/读取失败的异常类型名；只读诊断。

        语义（三条，写清楚是为了防止被当成第二套可用性判据）：
        1. **不是可用性判据**：它记的是"最近有没有失败过"，不是"现在能不能服务"。
           判可用性一律用 `is_available`；反向也不成立——读路径可服务不等于写路径
           可服务（`is_available` 只看 repo 句柄与缓存，写入仍真查库）；
        2. **会被消解**：成功装载（`connect()`）、成功 bootstrap（本地模式落盘）、
           成功读回缓存文件（`get()`）都会清空它——失败历史不得把当前判定钉死；
        3. **不出 HTTP**：`/ready` 只报布尔可用性，raw 异常/类型名不外泄
           （Batch 4 §22/§50）。
        """
        with self._lock:
            return self._last_error

    # ---- 生命周期 ----

    async def connect(self, *, user_name: str) -> UserIdentity | None:
        """建连 repo 并装载权威 profile；首次空库原子 bootstrap。

        权威模式装载失败**不**降级为本地 profile——返回 None 并清空缓存，业务侧
        据此 fail-closed。无 repo 时走本地 bootstrap。

        N-11：失败**不** raise（调用方 `_start_persistence` / `_recover_dependency`
        按 None 判定并保留降级进程；raise 会打穿到 `Agent.start()` 造成整体回滚），
        但失败事实由本 store 持有——`is_available` False + `last_error` 可查，
        成功装载时清空。

        Returns:
            装载到的权威身份；权威模式不可用时为 None。
        """
        repo = self._repo
        if repo is None:
            return self.bootstrap(user_name=user_name)
        try:
            if not await repo.connect():
                raise StorageUnavailableError("UserIdentityRepository 未连接")
            loaded = await repo.get(LOCAL_DEFAULT_USER_ID)
            if loaded is None:
                seed = UserIdentity(
                    displayName=user_name, pronouns=LOCAL_DEFAULT_PRONOMS)
                loaded = await repo.bootstrap_if_absent(seed)
            with self._lock:
                self._cached = loaded
                self._last_error = None
            return loaded
        except Exception as exc:
            with self._lock:
                # 不变量：**同一次加锁内**同时落"不可用"与"失败记录"，不依赖调用方去读
                # 日志——日志可能被吞、被降级、被采样，状态不会。两者分开写会留下一个
                # 跨线程可观察窗口（失败已记录而陈旧身份仍在服务，Challenger 加试 3）。
                self._record_error(exc)
                self._cached = None
            logger.error(
                "用户身份权威装载失败——身份不可用（fail-closed，不伪造本地档案）",
                exc_info=True)
            return None

    async def health_check(self) -> bool:
        """权威 repo 可达性（恢复协调器探针）。无 repo（本地模式）视为 ok。"""
        repo = self._repo
        if repo is None:
            return True
        try:
            return bool(await repo.health_check())
        except Exception:
            return False

    async def close(self) -> None:
        if self._repo is not None:
            await self._repo.close()

    # ---- 读 ----

    def get(self) -> UserIdentity | None:
        with self._lock:
            if self._repo is not None:
                # 权威模式：只信 Mongo 装载结果——绝不回退 JSON 文件或内存伪造。
                return self._cached
            if self._cached is not None:
                return self._cached
            if self._storage_path and self._storage_path.exists():
                self._cached = self._load_from_file()
            return self._cached

    def placeholder(self, user_name: str) -> UserIdentity:
        """构造期占位身份——固定单用户 userId，不缓存、不持久化。

        仅供 Agent 构造期的 scope/owner 常量使用（`usr_local_default` 是 V1 单用户
        常量）。权威档案必须由 `connect()` 从 Mongo 装载；`get()` 在装载成功前恒为
        None。本地模式（无 repo）下等价于 `bootstrap()`。
        """
        with self._lock:
            if self._cached is not None:
                return self._cached
        return self.bootstrap(user_name=user_name)

    # ---- 写 ----

    def bootstrap(self, user_name: str) -> UserIdentity:
        """本地 bootstrap——仅无 repo 的本地/单测模式（写入内存 + 可选 JSON 文件）。

        权威模式（repo 已装配）下**不**缓存、不落盘，只返回占位身份：生产装配路径
        的真实身份只能来自 `connect()` 的 Mongo 装载，装载失败即 fail-closed。
        """
        with self._lock:
            if self._repo is not None:
                return UserIdentity(
                    userId=LOCAL_DEFAULT_USER_ID,
                    displayName=user_name,
                    pronouns=LOCAL_DEFAULT_PRONOMS,
                )
            if self._cached is not None:
                return self._cached
            if self._storage_path and self._storage_path.exists():
                self._cached = self._load_from_file()
                if self._cached is not None:
                    return self._cached
            self._cached = UserIdentity(displayName=user_name, pronouns=LOCAL_DEFAULT_PRONOMS)
            self._persist(self._cached)
            # 降级**成功**：新建身份已落盘（覆盖损坏缓存），服务可用 → 清空失败记录。
            # 降级失败**没有**这条路（`_persist` 抛错会冒泡给调用方），故本地模式
            # 不存在「报了成功其实不可用」的空档。
            self._clear_error()
            return self._cached

    async def save(self, identity: UserIdentity) -> UserIdentity:
        """写前 touch（updatedAt + revision++）。无 repo 时写本地文件。"""
        with self._lock:
            identity.touch()
            self._cached = identity
            self._persist(identity)
            return identity

    async def update_if_revision(self, expected_revision: int,
                                 updates: dict[str, object]) -> UserIdentityUpdateOutcome:
        """CAS 更新：仅当当前 revision == expected_revision 时应用并递增。

        Updates 白名单为 displayName/preferredAddress/pronouns/locale/timezone/
        defaultCharacterId/initialized；不接收 userId/revision/createdAt/updatedAt。

        Returns:
            `UserIdentityUpdateOutcome`：应用成功 → `identity` 为写后身份（并刷新本
            store 缓存）；冲突 → `identity is None`，`current_revision` = **权威库
            现值**（N-3 / Batch 6：不再回落到进程内缓存，也不做 `expected + 1`）。
            调用方据此映射 409 `details.currentRevision`；`None` 表示权威现值未知
            （记录不存在），此时必须**省略**该字段而不是伪造。

        冲突**不刷新缓存**：409 是报告失败，不是隐式重试或静默 refresh（Batch 6 §38）；
        缓存新鲜度是另一个问题（E1），本批不解决。

        repo 已装配时以 Mongo 为权威（CAS 原子）；权威身份不可用 → fail-closed
        （StorageUnavailableError → 503），不写本地造成「档案已更新」的假象。
        冲突后的权威回读发生在 repository 内（冲突产生点携带真相），回读失败同样
        以 StorageUnavailableError 冒泡——绝不回退成陈旧缓存值。
        """
        normalized = self._normalize_updates(updates)
        repo = self._repo
        if repo is not None:
            if not self.is_available:
                raise StorageUnavailableError("用户身份权威存储不可用")
            outcome = await repo.update_if_revision(
                LOCAL_DEFAULT_USER_ID, expected_revision, normalized)
            if outcome.identity is not None:
                with self._lock:
                    self._cached = outcome.identity
            return outcome
        return self._update_local(expected_revision, normalized)


    # ---- 内部 ----

    def _record_error(self, exc: BaseException) -> None:
        """记录一次失败装载/读取的**类型名**。

        只留类型名：完整消息可能含连接串/路径，而 `last_error` 是进程内诊断，
        不该成为 secret 的第二个落点（Batch 4 §22）。

        自己持锁（`RLock` 可重入）——调用方 `connect()` / `_load_from_file()` 已在锁内，
        但本方法不依赖该前提，单独调用同样安全。
        """
        with self._lock:
            self._last_error = type(exc).__name__

    def _clear_error(self) -> None:
        """清空失败记录（成功装载 / 成功 bootstrap / 成功读回文件时调用）。

        自己持锁（`RLock` 可重入），语义与 `_record_error` 对称。
        """
        with self._lock:
            self._last_error = None

    @staticmethod
    def _normalize_updates(updates: dict[str, object]) -> dict[str, object]:
        """白名单过滤 + initialized 只能置 true（C3 fix：未知字段跳过而非 setattr）。"""
        updatable = (
            "displayName", "preferredAddress", "pronouns", "locale",
            "timezone", "defaultCharacterId", "initialized",
        )
        normalized: dict[str, object] = {}
        for key, value in updates.items():
            if key not in updatable:
                continue
            normalized[key] = True if key == "initialized" else value
        return normalized

    def _update_local(self, expected_revision: int,
                      updates: dict[str, object]) -> UserIdentityUpdateOutcome:
        """无 repo 时的内存 + JSONL CAS（单进程）。

        本地模式下**缓存就是权威**（没有第二个 writer），故冲突时 `current_revision`
        取当前缓存值——这不是"用缓存冒充权威"，而是该模式下唯一的权威来源。
        """
        with self._lock:
            cur = self.get()
            if cur is None:
                return UserIdentityUpdateOutcome()
            if cur.revision != expected_revision:
                return UserIdentityUpdateOutcome(current_revision=cur.revision)
            new = cur.model_copy(deep=True)
            for key, value in updates.items():
                setattr(new, key, value)
            new.touch()
            self._cached = new
            self._persist(new)
            return UserIdentityUpdateOutcome(identity=new)

    def _load_from_file(self) -> UserIdentity | None:
        """读本地 JSON 缓存文件；解析失败 → None（**刻意**不 raise，见下）。

        这个 catch 是 **DEGRADATION_CATCH（load-bearing）**，不是 bug-masking：
        两个调用方（`get()` / `bootstrap()`）都在无 repo 的本地模式下工作，
        `bootstrap()` 失败后仍会产出合法内存身份并 `_persist` 覆盖损坏文件
        ——删掉它会把本地 `GET /api/me` 变成 500，而那不是本批要修的语义。
        N-11 只要求失败**留下可查状态**：故此处记录 `last_error`（可用性由
        `bootstrap()` 的成功/失败决定，不是由"文件读没读到"决定）。
        """
        if self._storage_path is None:
            return None
        try:
            data = json.loads(self._storage_path.read_text("utf-8"))
            loaded = UserIdentity.model_validate(data)
        except Exception as exc:
            self._record_error(exc)
            logger.warning("UserIdentity 文件解析失败，重新 bootstrap：%s", self._storage_path)
            return None
        # 读成功即"当前状态可信" → 清空旧失败记录（§21：失败历史不得钉死当前判定）
        self._clear_error()
        return loaded

    def _persist(self, identity: UserIdentity) -> None:
        if self._storage_path is None:
            return
        self._storage_path.parent.mkdir(parents=True, exist_ok=True)
        self._storage_path.write_text(
            identity.model_dump_json(), encoding="utf-8")
