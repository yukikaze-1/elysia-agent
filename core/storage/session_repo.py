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

from core.storage.base import Repository, StorageUnavailableError, search_regex_filter
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
    投影层归一后，下游所有键比较都是 str vs str。**注意**：归一不等于可达——缺
    `updated_at` 的文档在 keyset 谓词下仍可能被漏掉（见 todo.md 的登记项）。
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
            await self._db.ensure_index(COLLECTION, [("session_id", 1)])
            await self._db.ensure_index(COLLECTION, [("updated_at", -1)])
            # 批次 8 / P2-01r：keyset 分页的排序是 (updated_at DESC, session_id ASC)，
            # 单键 updated_at 索引无法提供该顺序（真 Mongo 会退化成扫描 + 阻塞排序，
            # 实测 20k 会话 23.3ms/页 vs 有复合索引 1.2ms/页）。新增索引由 ensure_index
            # 幂等覆盖（ARCH-INC-P1-04：不做迁移框架），并已在 schema_drift 登记。
            await self._db.ensure_index(COLLECTION, [("updated_at", -1), ("session_id", 1)])
            logger.info("SessionRepository: MongoDB connected")
            return True
        except Exception as e:
            logger.warning("SessionRepository: MongoDB unavailable (%s)", e)
            self._db = None
            return False

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
        await self._call(
            lambda: db.update_one(
                COLLECTION, {"session_id": session_id}, {"$set": doc}, upsert=True,
            ),
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
        await self._call(
            lambda: db.update_one(
                COLLECTION, {"session_id": session_id}, {"$set": doc}, upsert=True,
            ),
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
        await self._call(
            lambda: db.update_one(
                COLLECTION,
                {"session_id": session_id},
                {"$setOnInsert": {
                    "session_id": session_id,
                    "title": "",
                    "messages": [],
                    "character_binding": normalize_character_binding(character_binding) or {},
                    "updated_at": now_iso(),
                }},
                upsert=True,
            ),
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

        Args:
            limit: 单页最多返回多少条（<=0 返回空列表，不落到后端）。
            after: 上一页最后一条的 `(updated_at, session_id)`；None = 第一页。
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
        if after is not None:
            after_ts, after_sid = after
            filt["$or"] = [
                {"updated_at": {"$lt": after_ts}},
                {"updated_at": after_ts, "session_id": {"$gt": after_sid}},
            ]
        db = self._db
        docs = await self._call(
            lambda: db.find(COLLECTION, filt,
                            sort=[("updated_at", -1), ("session_id", 1)], limit=limit),
            reason="SessionRepository.list_page 查询失败",
        )
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

        Returns:
            会话摘要列表，按更新时间降序，最多 200 条（**上界是诊断成本，不是产品语义**）。

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
        docs = await db.find(COLLECTION, {}, sort=[("updated_at", -1)], limit=200)
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
