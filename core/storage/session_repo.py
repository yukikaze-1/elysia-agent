"""SessionRepository — MongoDB sessions 集合的 Repository 实现。"""

import logging
from dataclasses import dataclass
from typing import Sequence

from Module.database.documentdb import DocumentDBBackend, DocumentDBFactory
from core.character.binding import CharacterBinding, normalize_character_binding
from core.schema.utils import now_iso
from core.schema.adapters.message_adapter import validate_message_legacy_compat
from core.schema.character import CharacterState
from core.schema.memory_reflector import MemoryReflectorState
from core.schema.message import Message, MessageRole
from core.schema.part import TextPart
from core.schema.service_state import ServiceState
from core.schema.types import IsoTimestamp, SessionId

from core.storage.base import (
    Repository,
    StorageUnavailableError,
    is_duplicate_key,
    search_regex_filter,
)
from core.storage.character_write_guard import (
    count_assistant_without_character,
    warn_missing_character_snapshot,
)
from core.storage.destructive_guard import require_confirm

logger = logging.getLogger(__name__)
COLLECTION = "sessions"

CONFIRM_TOKEN = "sessions"
"""破坏性 `clear_all` 的确认令牌（稳定资源名——理由见 core/storage/destructive_guard.py）。"""

AUTO_TITLE_MAX_LEN = 20
"""自动标题最大长度（首条 UserMessage 文本截取字符数）。"""

SESSION_ID_INDEX = "session_id_1"
"""`session_id` 唯一约束的规范索引名（Mongo 按键自动命名，与声明键一致）。"""

_LIST_ALL_LIMIT = 200
"""`list_all` 的窗口上界（诊断成本上界，不是产品语义——见该方法 docstring）。"""

ORDERED_TS_FILTER: dict = {
    "updated_at": {"$type": "string", "$gt": "", "$not": {"$type": "array"}},
}
"""**有序桶**判据：`updated_at` 是**非空字符串标量**（排序键可用且与投影一致）。

为什么三个条件缺一不可（真库 mongo:8 实测，`tmp/b16/main/probe_array_bucket.py`）：

  - `$gt: ""` —— 类型括号（type bracketing）让它只匹配字符串里字典序大于空串的值；
  - `$type: "string"` —— 补上"字段必须**真的存在**且是字符串"这一半（`$gt` 对缺失
    字段也不匹配，但显式写出来语义更完整，也让 fake 端无歧义）；
  - `$not: {"$type": "array"}` —— **承重条件**：Mongo 的比较/`$type` 算子会**遍历
    数组元素**，于是 `updated_at: ["1990-…","2040-…"]` 这样的文档**既**匹配
    `$type: "string"` **也**匹配 `$gt: ""`（按最大元素排序）——但投影层
    （`_summary_from_doc`）把它归一成 Sentinel `""`。若不排除数组，"有序桶文档
    投影成 Sentinel"就会与"游标里的 Sentinel 表示 legacy 桶"撞车：游标一旦落在
    数组文档上，后续请求会被误判为"已在 legacy 桶"而**跳过整个有序桶**
    （静默漏项 + 重复 + `has_more` 撒谎）。排除数组后本判据恰为"非空字符串标量"，
    "存储侧排序键 == 投影侧排序键"对**每一篇**文档成立（这是游标能当位置用的前提）。

真库计划实测：加这三个条件后正常路径仍是 `IXSCAN(updated_at_-1_session_id_1)` +
索引序、`docsExamined == nReturned == limit`（与非多键数据集上的旧谓词逐项相同）。
"""

LEGACY_TS_CLAUSE: dict = {"$nor": [ORDERED_TS_FILTER]}
"""**legacy 桶**子句：`ORDERED_TS_FILTER` 的**真补集**（顶层 `$nor`，含字段缺失）。

判据必须真的是补集，否则两桶拼起来覆盖不了整个集合——这正是数组型 `updated_at`
踩到的坑（`{"$not": {"$gt": ""}}` 不匹配"数组里含非空字符串"的文档，于是它既不在
有序桶、也不在 legacy 桶 ⇒ 不可达）。

`$nor` 的代价：它拿不到索引范围（真库实测：整条 `session_id` 索引扫描
`keysExamined == 集合大小`、无阻塞排序），而旧写法 `$not: {$gt: ""}` 能拿到精确
indexBounds（`[MaxKey,"{}"] ∪ ["",MinKey]`）。两者只在**有序桶取尽之后**才被调用
（即一次遍历的最后一页，或小数据集上的每次请求），因此用 O(N) 索引扫描换
"补集正确性"是划算的；**有序桶（正常路径）的计划不受任何影响**。

这类文档没有可用的排序键，投影层把它们的 `updated_at` 归一为 `""`（Sentinel），
因此它们在契约序中**排在所有有序记录之后**，桶内以 `session_id ASC` 定序。
**不伪造时间戳**：桶内顺序只依赖持久化的 `session_id`，与"何时被读到"无关
（禁止 `now()` / 随机值 / 首次读取时间做排序 authority）。
"""

INCOMPLETE_CHARACTER_BINDING_FILTER: dict = {
    "$or": [
        {"character_binding.character_id": {"$exists": False}},
        {"character_binding.character_id": {"$in": [None, ""]}},
        {"character_binding.instance_id": {"$exists": False}},
        {"character_binding.instance_id": {"$in": [None, ""]}},
    ],
}
"""character_binding 缺失/不完整的会话过滤条件（多角色数据完整性自检用）。

覆盖三种缺口：键不存在（`$exists: False` 对 `{}` 空对象同样命中——点号路径不存在）、
值为 null、值为空串。`character_profiles=true` 时前端以「每条会话都带角色绑定」为
前提 fail-closed（整页拒绝），故启动期需能统计这类记录。
"""


def derive_session_title(messages: Sequence[Message]) -> str:
    """从首条 UserMessage 的文本内容派生自动标题（前 20 字符）。

    幂等派生：仅在 session 文档无显式 title 时由 save() 调用，
    首条 UserMessage 无文本（如纯图片）时返回空串。

    Args:
        messages: 消息序列。

    Returns:
        派生标题，可能为空串。
    """
    for m in messages:
        if getattr(m, "role", None) != MessageRole.USER:
            continue
        text = "".join(p.content for p in getattr(m, "content", ()) if isinstance(p, TextPart))
        text = text.strip()
        return text[:AUTO_TITLE_MAX_LEN]
    return ""


def _warn_session_identity_not_enforced(reason: str) -> None:
    """把"`session_id` 唯一性未强制"这一降级状态**显著**报出来（不静默）。

    KB-16 的部署策略是"可用性优先"：库里有重复文档时不建唯一索引、也不删任何
    数据，应用照常启动。代价是逻辑主键在存储层不再唯一——这个代价必须看得见，
    处置步骤也必须写清楚（本仓不做自动删除/自动合并：重复文档可能各自带着不同的
    消息、标题、归档关联，"取更新时间最新的那个"是猜 winner）。
    """
    logger.warning(
        "\n"
        "==================================================================\n"
        "  SessionRepository：`session_id` **唯一性未强制**（可用性优先）\n"
        "    原因  : %s\n"
        "    影响  : 同一 session_id 对应多份文档，而各读路径**命中不同的那一份**：\n"
        "            `load`/`get_summary` 取 natural order 的第一篇、列表按\n"
        "            `updated_at DESC` 取另一篇（`repo.list_page`/`list_all` 不去重，\n"
        "            debug 面板会显示两条同 ID；只有 `SessionManager.list` 去重），\n"
        "            `rename` 只改一篇且不改 `updated_at`（列表里可能看不到新名字），\n"
        "            只有 `delete` 是 `delete_many`（两份一起删）\n"
        "    处置  : 人工确认并清理重复文档后**重启**——数据干净后 connect()\n"
        "            会自动把该索引升级为唯一（本方法只在数据干净时升级，\n"
        "            绝不自动删数据、也不猜谁是赢家）\n"
        "==================================================================",
        reason,
    )


def _same_key_session_id_index(live: list[dict]) -> tuple[str, bool] | None:
    """库中键模式恰为 `{session_id: 1}` 的索引 → `(名字, 是否唯一)`；没有则 None。

    **按键模式而不是名字识别**：`ensure_index` 的规范名是 `session_id_1`，但操作者
    完全可能手工建过**非规范名**的同键索引（如 `session_id_unique`，Mongo 允许同键
    不同选项并存——真库实测）。只认规范名会对它视而不见，转而去建第二个同键索引
    （真库抛 code 85），再退回非唯一 ⇒ 留下两个同键索引 + 每次 connect 都注定重试
    失败 + `schema_drift` 永久报 option_conflict。
    """
    for idx in live:
        key = idx.get("key")
        if key is not None and dict(key) == {"session_id": 1}:
            return str(idx.get("name") or ""), bool(idx.get("unique", False))
    return None


def _is_session_identity_conflict(exc: BaseException) -> bool:
    """该 E11000 是否**确实**是本集合的 `session_id` 唯一约束（§60）。

    为什么不能"任何 DuplicateKeyError 都当身份冲突"：集合将来可能出现别的唯一
    索引，盲目吞掉会把无关的约束违规伪装成"并发同键收敛"。故要求驱动给出约束
    上下文（`details.keyPattern`）且它**恰好**是 `{"session_id": 1}`；拿不到
    上下文时一律不认（宁可让异常冒泡，也不猜）。
    """
    if not is_duplicate_key(exc):
        return False
    details = getattr(exc, "details", None)
    if not isinstance(details, dict):
        return False
    return details.get("keyPattern") == {"session_id": 1}


@dataclass
class SessionSummary:
    """会话摘要信息，用于列表展示。"""
    session_id: SessionId
    message_count: int
    updated_at: IsoTimestamp
    title: str = ""
    active: bool = False  # 是否为当前活动会话（由 SessionManager.list 填充）
    character_binding: CharacterBinding | None = None


def _summary_from_doc(d: dict) -> SessionSummary:
    """sessions doc → `SessionSummary`（四个读路径的唯一投影点）。

    **为什么收成一个函数**（批次 8 / P2-01r）：列表窗口读（`list_all`）、分页读
    （`list_page`）、单文档读（`get_summary`）、批量读（`summaries_by_ids`）必须
    对同一条 doc 给出**同一个**摘要——各写一份投影，任何一份漏字段（如
    `character_binding`）都会变成"某一页少了字段"这类逐页漂移。

    `updated_at` 强制收成 `str`：它是分页的**排序键**，历史/损坏文档可能缺字段或为
    null，而 `str` 与 `None` 比较会抛 `TypeError`（在归并里表现为"整个列表静默退化"）。
    投影层归一后，下游所有键比较都是 str vs str。

    **归一值 `""` 是 Sentinel，不是"空时间戳"**（批次 16 / KB-15）：它表示"该文档
    没有可用的排序键"，在契约序里排最后、桶内按 `session_id ASC` 定序。存储层的
    `list_page` 用同一个判据把文档分桶（`ORDERED_TS_FILTER` / `LEGACY_TS_CLAUSE`），
    因此**存储顺序 == 投影顺序 == 游标语义**，这类文档同样可达（修前它们会被
    keyset 谓词静默漏掉，或把游标写成解码不了的空时间戳）。
    """
    raw_ts = d.get("updated_at")
    return SessionSummary(
        session_id=SessionId(d.get("session_id", "")),
        message_count=len(d.get("messages", [])),
        updated_at=raw_ts if isinstance(raw_ts, str) else "",
        title=d.get("title", ""),
        character_binding=normalize_character_binding(d.get("character_binding")),
    )


class SessionRepository(Repository):
    """会话存储 — MongoDB sessions 集合。"""

    def __init__(self, db: DocumentDBBackend | None = None, *,
                 database_name: str = "default",
                 base_url: str | None = None,
                 require_assistant_character: bool = False):
        """初始化会话存储。

        Args:
            db: 可选的文档数据库后端，为 None 时需调用 connect() 建立连接。
            database_name: MongoDB 数据库名称，默认 "default"。
            base_url: MongoDB 连接串，为 None 时从 MONGODB_URI 环境变量读取。
            require_assistant_character: `character_profiles` 能力开启时置 True——
                save() 落库内嵌 assistant 消息若缺角色身份快照，发显著告警（仍写入，
                见 core/storage/character_write_guard.py 的设计取舍）。
        """
        self._db: DocumentDBBackend | None = db
        self._database_name = database_name
        self._base_url = base_url
        self._injected_db = db is not None
        self._require_assistant_character = require_assistant_character

    async def connect(self) -> bool:
        """建立 MongoDB 连接并创建索引（sessions 无 Mongo TTL——删除走显式级联删除）。

        索引**引导**分两段：普通索引直接 `ensure_index`（幂等只增）；`session_id`
        唯一约束走 `_ensure_session_id_index()`——它是一次**选项升级**（库里普遍
        已存在非唯一的 `session_id_1`），需要先探测重复数据、必要时 drop 重建，
        且任何失败都不得让仓储不可用（见该方法的纪律清单）。

        Returns:
            True 表示连接成功，False 表示连接失败。
        """
        if self._db is not None:
            # 已注入 db（测试内存 fake）——视为已连接，不覆盖为真实 Mongo。
            return True
        try:
            self._db = DocumentDBFactory.create(
                f"mongodb/{self._database_name}",
                base_url=self._base_url,
            )
            await self._db.ensure_index(COLLECTION, [("updated_at", -1)])
            # 批次 8 / P2-01r：keyset 分页的排序是 (updated_at DESC, session_id ASC)，
            # 单键 updated_at 索引无法提供该顺序（真 Mongo 会退化成扫描 + 阻塞排序，
            # 实测 20k 会话 23.3ms/页 vs 有复合索引 1.2ms/页）。新增索引由 ensure_index
            # 幂等覆盖（ARCH-INC-P1-04：不做迁移框架），并已在 schema_drift 登记。
            await self._db.ensure_index(COLLECTION, [("updated_at", -1), ("session_id", 1)])
            # 批次 16 / KB-16：session_id 是**逻辑主键**（全仓按它直读/改名/删除/
            # 摘要/级联），唯一性必须在存储层强制——不是一行 unique=True（见方法）。
            await self._ensure_session_id_index()
            logger.info("SessionRepository: MongoDB connected")
            return True
        except Exception as e:
            logger.warning("SessionRepository: MongoDB unavailable (%s)", e)
            self._db = None
            return False

    async def _probe_duplicate_session_ids(self) -> tuple[bool, str]:
        """探测 `session_id` 重复（决定唯一索引可不可以建）。

        Returns:
            `(是否可以建唯一索引, 人类可读的原因/重复样本)`。检测失败时返回
            `(False, 原因)`——**不**把「查不到」当作「无重复」（S8 纪律 1）：
            检测失败时若照建唯一索引，真库会以 `DuplicateKeyError` 拒绝，
            升级路径已经把非唯一索引 drop 掉，集合会退化成**没有任何
            `session_id` 索引**（真库实测：build 失败后只剩 `_id_`）。
        """
        assert self._db is not None
        pipeline = [
            {"$group": {"_id": "$session_id", "n": {"$sum": 1}}},
            {"$match": {"n": {"$gt": 1}}},
            {"$limit": 5},
        ]
        try:
            dup_docs = list(await self._db.aggregate(COLLECTION, pipeline))
        except Exception as e:
            return False, f"重复检测失败：{type(e).__name__}: {e}"
        if not dup_docs:
            return True, ""
        # 只报**数量**，不输出会话内容（数据审计纪律：不泄漏用户数据）。
        return False, f"存在 {len(dup_docs)}{'+' if len(dup_docs) >= 5 else ''} 组重复 session_id"

    async def _ensure_session_id_index(self) -> None:
        """建立 `session_id` **唯一**约束（可用性优先，S8 同族纪律）。

        为什么不是一行 `ensure_index(COLLECTION, [("session_id", 1)], unique=True)`：

        1. **Mongo 不能原地改索引选项**。库里普遍已有非唯一 `session_id_1`
           （2026-07-16 起就声明着）；同键请求 unique 会抛
           `OperationFailure`(86 IndexKeySpecsConflict)——真库实测。而它在
           `connect()` 的同一个 `try` 里会被吞成"仓储不可用"（`_db = None`），
           即**整个 sessions 域 503**。故先 `list_indexes()` 比选项，
           需要升级时**显式 `drop_index()` 后重建**（`drop_index` 是本仓唯一的
           索引删除出路，这是它在生产代码里的第一个调用点）。
        2. **有重复数据时绝不建唯一索引，也绝不 drop 现有索引**：真库实测
           "已有重复 + build unique" → `DuplicateKeyError`(11000)，而 drop 已经发生
           ⇒ 集合只剩 `_id_`。检测到重复 ⇒ 保持现状 + 显著告警（可用性优先）。
           唯一性据此**降级为显式**：库侧由运行时 `schema_drift` 的
           `option_conflicts` 报 error，启动日志同时给出处置命令。
        3. **检测失败 ≠ 无重复**（`_probe_duplicate_session_ids` 纪律）。
        4. **索引引导不得毁掉可用性**：本方法**不抛**——建索引失败只告警；
           `connect()` 仍返回 True（对比：让异常冒泡会把一次索引故障升级成
           "MongoDB 不可用"，正是 S2-B1 的故障形态）。
        5. **幂等**：已建成的唯一索引直接返回；重复 `connect()`（含健康重连）
           不产生任何变更——**幂等判定在昂贵的重复探测之前**（探测是全集合
           `$group`：真库实测 20k 文档 71ms / 100k 351ms，健康重连每轮都会
           `connect()`，不该为"已经对了"的库反复付这个代价）。
        6. **按**键模式**而不是名字**识别现状（`_same_key_session_id_index`）：
           操作者可能手工建过非规范名的同键索引。
        """
        assert self._db is not None
        db = self._db
        try:
            live = await db.list_indexes(COLLECTION)
        except Exception:
            # 后端违约抛异常（而非按协议返回 None）时同样按"状态未知"处理：
            # 本方法承诺不抛，`connect()` 的可信度不该取决于后端的异常风格。
            logger.warning("SessionRepository：list_indexes 抛异常，跳过 session_id "
                           "唯一约束保障（保持现状，下次 connect() 重试）", exc_info=True)
            return
        if live is None:
            # S2-B1 三态：查询失败 ≠ 没有索引。状态未知时不做任何索引变更
            # （create_index 在同名不同选项时会抛，drop 更不该在未知状态执行）。
            logger.warning(
                "SessionRepository：list_indexes 查询失败，跳过 session_id 唯一约束保障"
                "（保持现状，下次 connect() 重试）")
            return
        existing = _same_key_session_id_index(live)
        if existing is not None and existing[1]:
            if existing[0] != SESSION_ID_INDEX:
                logger.warning(
                    "SessionRepository：`session_id` 唯一约束由**非规范名**索引 %s 提供"
                    "（规范名 %s）——约束已生效，本方法不做任何变更；"
                    "但 schema_drift 会把规范名报成 missing、把该索引报成 orphan"
                    "（人工处置：drop_index('%s') 后重启，由本方法重建规范名索引）",
                    existing[0], SESSION_ID_INDEX, existing[0])
            return  # 已建成唯一约束（幂等：重复 connect() 不做任何变更）
        desired_unique, reason = await self._probe_duplicate_session_ids()
        if not desired_unique:
            # 库里有重复 ⇒ 唯一约束**不强制**（可用性优先）。这是显式降级：
            # 启动日志 + 运行时 schema_drift 的 option_conflicts 都会报出来。
            _warn_session_identity_not_enforced(reason)
            if existing is None:
                # 现状连非唯一索引都没有（上次升级中途失败留下的形态）：补回来，
                # 保证 session_id 至少还有索引可用。
                try:
                    await db.ensure_index(COLLECTION, [("session_id", 1)])
                except Exception:
                    logger.error("SessionRepository：补建非唯一 session_id 索引失败",
                                 exc_info=True)
            return
        if existing is not None:
            # 数据干净且选项不同 ⇒ 升级。Mongo 不支持原地改选项（真库实测 code 86），
            # 只能先删后建；drop 失败（状态未知/权限不足）就保持现状，绝不硬来。
            if not await db.drop_index(COLLECTION, existing[0]):
                logger.warning(
                    "SessionRepository：drop_index(%s) 未成功，跳过唯一约束升级"
                    "（保持现状，下次 connect() 重试）", existing[0])
                return
        try:
            await db.ensure_index(COLLECTION, [("session_id", 1)], unique=True)
        except Exception:
            logger.error(
                "SessionRepository：session_id 唯一索引创建失败——仓储保持可用，"
                "唯一性未强制；下次 connect() 会重试", exc_info=True)
            # 唯一建不出来时退回非唯一：宁可降级唯一性，也不让 session_id
            # 完全没有索引（升级路径已经把旧的删掉了）。
            try:
                await db.ensure_index(COLLECTION, [("session_id", 1)])
            except Exception:
                logger.error("SessionRepository：session_id 非唯一索引恢复也失败",
                             exc_info=True)

    async def close(self) -> None:
        """关闭数据库连接，释放资源。"""
        if self._db:
            await self._db.close()
            self._db = None

    async def health_check(self) -> bool:
        """健康检查：真实 ping（P1-05），失败时重连并重建索引。

        Returns:
            True 表示可用，False 表示不可用。
        """
        return await self._health_check()

    async def _upsert_session(self, filter_expr: dict, update: dict, *,
                              reason: str) -> None:
        """`{session_id}` upsert，并把并发同键冲突**收敛**到既有文档上（§58/§59/§60）。

        `session_id` 唯一索引（`_ensure_session_id_index`）下，三条写路径的
        "先 find 再 upsert"仍可能同时走插入分支。真库实测（20 并发 × 5 轮 ×
        create/ensure_exists/save/load+create）：服务端会把 loser 的插入
        **自动重试为 update**，因此正常路径**看不到异常**（0 例外、最终恒 1 篇）。

        本方法是那条路径的兜底，三条纪律：

        - `non_fatal=is_duplicate_key`：即使冲突冒出来也不 `_mark_unavailable()`
          判死仓储（那会把一次正常并发升级成全站 sessions 503，直到下一轮健康重连）。
        - 只对**经确认的** `session_id` 约束冲突收敛（`_is_session_identity_conflict`）；
          别的唯一约束异常原样冒泡，不猜。
        - 收敛 = 重跑同一个 upsert：此时文档已存在 ⇒ 走 update 分支，而
          `$set` / `$setOnInsert` 都不修改 `session_id` ⇒ **不可能再次冲突**
          （最多重试一次，无循环）。
        """
        assert self._db is not None
        db = self._db

        async def _run_once() -> None:
            await self._call(
                lambda: db.update_one(COLLECTION, filter_expr, update, upsert=True),
                reason=reason, non_fatal=is_duplicate_key,
            )

        try:
            await _run_once()
        except Exception as exc:
            if not _is_session_identity_conflict(exc):
                raise
            logger.info(
                "SessionRepository：并发同键 upsert 冲突，收敛到既有 session 文档（%s）",
                filter_expr.get("session_id"))
            await _run_once()

    async def save(self, session_id: SessionId, messages: Sequence[Message],
                   character_state: CharacterState,
                   reflector_state: MemoryReflectorState | None = None,
                   service_state: ServiceState | None = None,
                   character_binding: object = None) -> None:
        """保存或更新 session。

        Args:
            session_id: 会话 ID。
            messages: 消息列表。
            character_state: 角色状态。
            reflector_state: 记忆反射器状态，为 None 时写入空对象。
            service_state: 服务运行时状态，为 None 时写入空对象。
            character_binding: 会话绑定的角色实例（架构稿 §15.3），为 None 时写入空对象。

        Raises:
            StorageUnavailableError: MongoDB 未连接（B2/P1-01——禁止静默
                return 被 caller 当作成功；caller 捕获后落 JSONL fallback）。
        """
        if self._db is None:
            raise StorageUnavailableError(
                "SessionRepository 未连接（MongoDB 不可用），session 未持久化")
        db = self._db
        # P3-3 写入期兜底：内嵌 assistant 缺角色快照 → 显著告警（仍写入；见 guard 模块）。
        if self._require_assistant_character:
            missing = count_assistant_without_character(messages)
            if missing:
                warn_missing_character_snapshot(
                    collection=COLLECTION, session_id=str(session_id),
                    missing=missing, total=len(messages))
        serialized = [m.model_dump(mode="json") for m in messages]
        doc = {
            "session_id": session_id,
            "messages": serialized,
            "character_state": character_state.model_dump(mode="json"),
            "reflector_state": reflector_state.model_dump(mode="json") if reflector_state else {},
            "service_state": service_state.model_dump(mode="json") if service_state else {},
            "character_binding": normalize_character_binding(character_binding) or {},
            "updated_at": now_iso(),
        }
        # 标题语义：显式 title（create/rename 写入）必须保留；
        # 仅当现有 doc 无 title 且 messages 非空时派生自动标题（幂等，无额外调用点）。
        existing = await self._call(
            lambda: db.find(COLLECTION, {"session_id": session_id}, limit=1),
            reason="SessionRepository.save 查询失败",
        )
        existing_title = existing[0].get("title", "") if existing else ""
        doc["title"] = existing_title if existing_title else derive_session_title(messages)
        await self._upsert_session(
            {"session_id": session_id}, {"$set": doc},
            reason="SessionRepository.save 写入失败",
        )

    async def create(
        self,
        session_id: SessionId,
        title: str = "",
        character_binding: object = None,
    ) -> None:
        """创建会话（写入 title 与空消息体）。

        create 后 title 由 save() 保留（save 不覆盖非空 title 除非显式）。

        Args:
            session_id: 会话 ID。
            title: 会话标题，默认空串（可由后续 save 自动派生）。

        Raises:
            StorageUnavailableError: MongoDB 未连接（B2/P1-01）。
        """
        if self._db is None:
            raise StorageUnavailableError(
                "SessionRepository 未连接（MongoDB 不可用），session 未创建")
        db = self._db
        doc = {
            "session_id": session_id,
            "title": title,
            "messages": [],
            "character_binding": normalize_character_binding(character_binding) or {},
            "updated_at": now_iso(),
        }
        await self._upsert_session(
            {"session_id": session_id}, {"$set": doc},
            reason="SessionRepository.create 写入失败",
        )

    async def ensure_exists(
        self,
        session_id: SessionId,
        character_binding: object = None,
    ) -> bool:
        """确保会话文档存在（$setOnInsert——绝不覆盖既有文档字段）。

        clean-start 会话发现（BE-P1-05）：eager default 会话启动时 best-effort 落库。
        与 create() 的 `$set`（会重置 messages 等字段）不同，`$setOnInsert` 只在新
        插入时写入——已有文档（含 messages/标题）恒不被触碰，重启不清空 LLM 上下文
        窗口。

        为什么先 find 再 upsert：后端 update_one 只暴露 modified_count（D6-d 已核实
        不暴露 upserted_id），且 `$setOnInsert` 命中已有文档时 modified_count=0，与
        真正插入的 0 无法区分。先 find 使「是否新插入」的布尔语义在真实/内存后端
        都成立。find→upsert 窗口内的并发插入由 $setOnInsert 兜底（不覆盖既有文档），
        仅返回值可能轻微乐观（视作本次新插入）——best-effort 启动路径可接受。

        Args:
            session_id: 会话 ID。

        Returns:
            True 表示本次新插入（此前不存在）；False 表示已存在（未修改）。

        Raises:
            StorageUnavailableError: MongoDB 未连接（B2/P1-01——与 create/load 同语义）。
        """
        if self._db is None:
            raise StorageUnavailableError(
                "SessionRepository 未连接（MongoDB 不可用），无法确保会话存在")
        db = self._db
        existing = await self._call(
            lambda: db.find(COLLECTION, {"session_id": session_id}, limit=1),
            reason="SessionRepository.ensure_exists 查询失败",
        )
        if existing:
            return False
        await self._upsert_session(
            {"session_id": session_id},
            {"$setOnInsert": {
                "session_id": session_id,
                "title": "",
                "messages": [],
                "character_binding": normalize_character_binding(character_binding) or {},
                "updated_at": now_iso(),
            }},
            reason="SessionRepository.ensure_exists 写入失败",
        )
        return True

    async def rename(self, session_id: SessionId, title: str) -> bool:
        """重命名会话。

        Args:
            session_id: 会话 ID。
            title: 新标题。

        Returns:
            True 表示会话存在且已更新；False **仅**表示"会话不存在"。

        Raises:
            StorageUnavailableError: MongoDB 离线。

        #45/#8 同族（写路径对齐）：此前离线也返回 `False`，与"会话不存在"共用同一个
        返回值——调用方无法区分，只能二选一地解释。现与同仓 `save`/`create` 对齐：
        离线 **raise**，`False` 只留一种含义。需要"离线也继续"的调用方（如
        `SessionManager.rename` 的进程内摘要兜底）必须**显式**捕获——容忍要看得见。
        """
        if self._db is None:
            raise StorageUnavailableError(
                "SessionRepository 未连接——rename 无法执行，不得静默当作'会话不存在'")
        db = self._db
        result = await self._call(
            lambda: db.update_one(
                COLLECTION, {"session_id": session_id}, {"$set": {"title": title}},
                upsert=False,
            ),
            reason="SessionRepository.rename 写入失败",
            # 写路径纪律（与 `_upsert_session` 同）：**任何**唯一约束冲突都是业务
            # 冲突而不是存储故障。不带 `non_fatal` 时 `_call` 会 `_mark_unavailable()`
            # 判死整个仓储（直到下一轮健康重连），而 `SessionManager.rename` 还会把
            # 它吞成"改名成功"⇒ 用户看到 200、随后整个 sessions 域 503。
            # 冲突本身仍**原样冒泡**（不猜、不吞）——今天 sessions 上没有第二个唯一
            # 索引，这条路不可达；装护栏是为了它将来仍然只是"一次失败的改名"。
            non_fatal=is_duplicate_key,
        )
        return result > 0

    async def load(self, session_id: SessionId
                   ) -> tuple[
                       list[Message], CharacterState, MemoryReflectorState,
                       ServiceState, CharacterBinding | None,
                   ] | None:
        """加载 session。

        Args:
            session_id: 会话 ID。

        Returns:
            (消息列表, 角色状态, 记忆反射器状态, 服务运行时状态, 角色绑定) 元组，
            不存在返回 None。

        Raises:
            StorageUnavailableError: MongoDB 未连接（B2/P1-01——区分「不可用」
                与「not found」；caller 捕获后走 JSONL fallback）。
        """
        if self._db is None:
            raise StorageUnavailableError(
                "SessionRepository 未连接（MongoDB 不可用），session 无法加载")
        db = self._db
        results = await self._call(
            lambda: db.find(COLLECTION, {"session_id": session_id}, limit=1),
            reason="SessionRepository.load 查询失败",
        )
        if not results:
            return None
        doc = results[0]
        msgs_raw = doc.get("messages", [])
        for m in msgs_raw:
            m.pop("_id", None)
        messages = [validate_message_legacy_compat(m) for m in msgs_raw]
        cs_data = doc.get("character_state", {})
        cs = CharacterState.model_validate(cs_data) if cs_data else CharacterState(session_id=session_id)
        rs_data = doc.get("reflector_state", {})
        rs = MemoryReflectorState.model_validate(rs_data) if rs_data else MemoryReflectorState()
        ss_data = doc.get("service_state", {})
        ss = ServiceState.model_validate(ss_data) if ss_data else ServiceState()
        cb_data = normalize_character_binding(doc.get("character_binding"))
        return messages, cs, rs, ss, cb_data

    async def delete(self, session_id: SessionId) -> int:
        """删除指定会话。

        Args:
            session_id: 会话 ID。

        Returns:
            删除的会话数（0 = 不存在，幂等）。

        Raises:
            StorageUnavailableError: MongoDB 未连接（B3/P1-03——离线删除不得
                静默返回 0 被 caller 当成功，否则重启后会话复活）。
        """
        if self._db is None:
            raise StorageUnavailableError(
                "SessionRepository 未连接（MongoDB 不可用），session 未删除")
        db = self._db
        result = await self._call(
            lambda: db.delete_many(COLLECTION, {"session_id": session_id}),
            reason="SessionRepository.delete 删除失败",
        )
        logger.info("SessionRepository delete: session_id=%s, deleted=%d", session_id, result)
        return result

    async def list_page(self, *, limit: int,
                        after: tuple[str, str] | None = None,
                        search: str = "") -> list[SessionSummary]:
        """**keyset 分页**读取一页会话摘要（存储层分页的唯一入口）。

        这是 `GET /api/sessions` 的取数原语（批次 8 / P2-01r）：排序与谓词与设计
        文档 `docs/superpowers/specs/archive/2026-08/2026-08-04-electron-backend-batch3-multi-session.md`
        §4「列表游标（keyset）」逐字一致——

          - 排序 `updated_at DESC, session_id ASC`（**tie-break 下推到存储**：
            只按 updated_at 排序时，时间戳相同的记录在页边界上可能被重复或漏掉）；
          - 下一页谓词 `(updated_at < U) OR (updated_at = U AND session_id > S)`；
          - `search` 对 title 做大小写不敏感子串匹配（`$regex`，转义 + 长度上限
            统一走 `search_regex_filter`；超长按**无匹配**处理，与消息/角色搜索同口径）；
          - `limit` 是**单页上限**，与数据集大小无关——调用方必须用返回的最后一条
            记录继续翻页，直到拿不满一页为止。**不得**用"预取全量再切片"替代
            （那正是 P2-01r：`limit=200` 预取把单页上限变成了数据集上限）。

        ## 批次 16 / KB-15：排序键的**全域**定义（两个桶）

        修前本方法把排序/谓词直接压在**原始** `updated_at` 上，而投影层
        （`_summary_from_doc`）把"非字符串 / 缺失 / null"归一成 `""` 再交给游标。
        存储侧的排序键与**对外暴露的**排序键因此不一致，后果有二（真库 E3 实测）：

          1. **静默漏项**：缺/`null` 的文档在 `updated_at: {$lt: <str>}` 下**永不匹配**
             （类型括号），排在末尾却谁也叫不到它 ⇒ 只有"一页装得下"时才露过面；
          2. **游标毒化**：BSON Date 型的 `updated_at` 排在字符串**之前**（BSON 序），
             投影却把它归成 `""` ⇒ 它若落在页边界，`next_cursor` 会是
             `base64("|sid")`，而解码端拒绝空时间戳 ⇒ **下一页直接 400**，
             该页之后的全部记录不可达。

        现在把排序域写成两个**显式**桶，二者合起来覆盖集合中的每一篇文档：

        | 桶 | 判据 | 排序键 | 桶内顺序 |
        |---|---|---|---|
        | 有序 | `ORDERED_TS_FILTER`（非空字符串**标量**） | `updated_at` | `updated_at DESC, session_id ASC` |
        | legacy | `LEGACY_TS_CLAUSE`（**真补集**：缺失/null/非字符串/空串/数组/对象…） | Sentinel `""` | `session_id ASC` |

        两个桶拼接后的顺序与投影层给出的契约序（`_precedes` / `_after_cursor`：
        `updated_at DESC, session_id ASC`，`""` 最小）**逐项相等**——这正是游标
        能当"位置"用的前提。legacy 桶只在有序桶取不满一页时才被查询（每请求至多
        两次 `find`，仍是 O(limit)，没有回到"预取全量"）。

        Args:
            limit: 单页最多返回多少条（<=0 返回空列表，不落到后端）。
            after: 上一页最后一条的 `(updated_at, session_id)`；None = 第一页。
                `after[0] == ""` 表示"已进入 legacy 桶"（Sentinel 只由该桶产生）。
            search: title 子串（大小写不敏感）；空串不过滤。

        Returns:
            至多 `limit` 条摘要，按契约顺序排列。

        Raises:
            StorageUnavailableError: MongoDB 未连接（与 `list_all` 同一 fail-closed 口径）。
        """
        if self._db is None:
            raise StorageUnavailableError(
                "SessionRepository 未连接（MongoDB 不可用），会话分页无法判定")
        if limit <= 0:
            return []
        filt: dict = {}
        if search:
            clause = search_regex_filter(search)
            if clause is None:
                # 超长搜索词：**不能忽略过滤**（忽略会返回全部会话，比空结果更危险）。
                logger.warning("会话搜索词超长（%d 字符）——按无匹配处理", len(search))
                return []
            filt["title"] = clause
        db = self._db
        docs: list[dict] = []
        # 有序桶：首页，或游标停在有序桶内（`updated_at` 非空）时继续读它。
        if after is None or after[0]:
            ordered = dict(filt)
            ordered.update(ORDERED_TS_FILTER)
            if after is not None:
                after_ts, after_sid = after
                ordered["$or"] = [
                    {"updated_at": {"$lt": after_ts}},
                    {"updated_at": after_ts, "session_id": {"$gt": after_sid}},
                ]
            docs = await self._call(
                lambda: db.find(COLLECTION, ordered,
                                sort=[("updated_at", -1), ("session_id", 1)], limit=limit),
                reason="SessionRepository.list_page 查询失败",
            )
            if len(docs) >= limit:
                return [_summary_from_doc(d) for d in docs]
        # legacy 桶：有序桶已取尽（或游标已在桶内）——按 `session_id ASC` 续读。
        # 排序键**只用** session_id：这些文档没有可用的 updated_at，若仍按
        # `(updated_at DESC, session_id ASC)` 排，桶内顺序会由 BSON 类型序决定
        # （Date > String > Number > Null），与投影出来的 `""` 序不一致。
        legacy = dict(filt)
        legacy.update(LEGACY_TS_CLAUSE)
        if after is not None and not after[0]:
            legacy["session_id"] = {"$gt": after[1]}
        remaining = limit - len(docs)
        if remaining > 0:
            docs.extend(await self._call(
                lambda: db.find(COLLECTION, legacy, sort=[("session_id", 1)],
                                limit=remaining),
                reason="SessionRepository.list_page 查询失败",
            ))
        return [_summary_from_doc(d) for d in docs]

    async def get_summary(self, session_id: SessionId) -> SessionSummary | None:
        """按会话 ID 读**单条**摘要（不经过任何列表窗口）。

        详情/重命名路径（`GET|PATCH /api/sessions/{id}`）此前复用一个"最多 200 条"
        的列表视图，于是第 201+ 条会话的详情拿到 None、由路由兜底成 `updatedAt: ""`
        ——同一 root cause 的第二个出口（批次 8 / P2-01r）。单文档读与窗口无关。

        Args:
            session_id: 会话 ID。

        Returns:
            摘要；会话不存在返回 None。

        Raises:
            StorageUnavailableError: MongoDB 未连接（离线 ≠ 不存在）。
        """
        if self._db is None:
            raise StorageUnavailableError(
                "SessionRepository 未连接（MongoDB 不可用），会话摘要无法判定")
        db = self._db
        docs = await self._call(
            lambda: db.find(COLLECTION, {"session_id": session_id}, limit=1),
            reason="SessionRepository.get_summary 查询失败",
        )
        return _summary_from_doc(docs[0]) if docs else None

    async def summaries_by_ids(
        self, session_ids: Sequence[SessionId],
    ) -> dict[SessionId, SessionSummary]:
        """批量读摘要（按 session_id `$in`）——进程内覆盖层的持久键/字段来源。

        `SessionManager` 的列表合并需要知道"某个本地摘要对应的持久记录是否存在、
        其持久 `updated_at`/`message_count`/绑定是什么"：覆盖层的**有效排序键**是
        `max(持久键, 本地键)`（改过名的会话要被"抬"到最前），只靠本地值会把记录
        摆错位置；反之若把覆盖层整条从结果里剔除，它会**丢失**。一次 `$in` 查询
        （ID 数 = 进程内写过的会话数）替代逐条 `find`。

        Args:
            session_ids: 待查会话 ID 序列（去重后查询；空序列不发查询）。

        Returns:
            `{session_id: 摘要}`，只含真实存在的记录。

        Raises:
            StorageUnavailableError: MongoDB 未连接（离线 ≠ 不存在）。
        """
        if self._db is None:
            raise StorageUnavailableError(
                "SessionRepository 未连接（MongoDB 不可用），覆盖层摘要无法判定")
        ids = list(dict.fromkeys(str(s) for s in session_ids))
        if not ids:
            return {}
        db = self._db
        docs = await self._call(
            lambda: db.find(COLLECTION, {"session_id": {"$in": ids}}, limit=len(ids)),
            reason="SessionRepository.summaries_by_ids 查询失败",
        )
        return {SessionId(str(d.get("session_id", ""))): _summary_from_doc(d) for d in docs}

    async def list_all(self) -> list[SessionSummary]:
        """列出最近会话摘要（**有界诊断读**，≤200 条）。

        ⚠️ **用户列表不走这里**（批次 8 / P2-01r）：本方法只服务 debug 面板
        （`GET /debug/api/sessions`——无分页参数、按"全部会话"展示）。用户可见的
        会话列表走 `list_page()`；把本方法的 200 当成分页取数，正是 P2-01r 的成因：
        单页上限变成了数据集上限，第 201+ 条不可达。

        排序域与 `list_page` **同一套桶**（批次 16 / KB-15）：先按
        `updated_at DESC, session_id ASC` 取有序桶，不足 200 时再按
        `session_id ASC` 补 legacy 桶。修前这里只 `sort=updated_at DESC`，
        于是一篇 BSON Date 型的坏文档会**占掉有序窗口的一个名额**并排在最前
        （BSON 序把 Date 排在字符串之前），而它在投影里根本没有排序键。

        Returns:
            会话摘要列表，按更新时间降序（无排序键的 legacy 记录排在最后，
            桶内 `session_id ASC`），最多 200 条（**上界是诊断成本，不是产品语义**）。

        Raises:
            StorageUnavailableError: 数据库未连接（决策 #8：本仓储整仓读 fail-closed
                ——离线时不得把「读不到」报成「没有会话」）。

        `SessionManager` 的降级路径已捕获该异常并显式降级为进程内摘要 + warning；
        这是**有意的降级点**，不是让本方法返回空的理由。
        """
        if self._db is None:
            raise StorageUnavailableError(
                "SessionRepository 未连接（MongoDB 不可用），会话列表无法判定")
        db = self._db
        limit = _LIST_ALL_LIMIT
        docs = await db.find(
            COLLECTION, ORDERED_TS_FILTER,
            sort=[("updated_at", -1), ("session_id", 1)], limit=limit)
        remaining = limit - len(docs)
        if remaining > 0:
            docs.extend(await db.find(
                COLLECTION, LEGACY_TS_CLAUSE, sort=[("session_id", 1)], limit=remaining))
        return [_summary_from_doc(d) for d in docs]

    async def clear_all(self, *, confirm: str) -> int:
        """清空全部会话（**破坏性且不可逆**，要求类型化确认）。

        Args:
            confirm: 必须等于 `CONFIRM_TOKEN`（破坏性操作的目标资源确认）。

        Returns:
            删除的会话数。

        Raises:
            DestructiveConfirmRequired: confirm 不匹配。
            StorageUnavailableError: 数据库未连接（此前静默返回 0，调用方会
                误以为已清空，现改 fail-closed）。
        """
        require_confirm(
            target=CONFIRM_TOKEN, confirm=confirm, operation="清空全部会话",
        )
        if self._db is None:
            raise StorageUnavailableError(
                f"SessionRepository.clear_all: {COLLECTION} 未连接，拒绝静默成功",
            )
        result = await self._db.delete_many(COLLECTION, {})
        logger.warning("SessionRepository clear_all: deleted=%d sessions", result)
        return result

    async def count_incomplete_character_binding(self) -> int:
        """统计 character_binding 缺失/不完整的会话数（多角色完整性自检）。

        数据库侧 count，不拉取文档。用于启动期自检告警：`character_profiles=true`
        时这类记录会让前端对会话列表整页拒绝。

        Returns:
            命中的会话数。

        Raises:
            StorageUnavailableError: MongoDB 未连接（区分「0 条缺口」与「未扫描」——
                调用方据此标记自检未执行，而非误报数据健康）。
        """
        if self._db is None:
            raise StorageUnavailableError(
                "SessionRepository 未连接（MongoDB 不可用），无法执行角色绑定完整性自检")
        return await self._db.count(COLLECTION, INCOMPLETE_CHARACTER_BINDING_FILTER)
