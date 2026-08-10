"""长期记忆——内存兜底，可选 Milvus + embedding。

启动快速：仅尝试连接 Milvus。Embedding 在首次 insert/search 时懒加载，
带短超时避免阻塞。
"""

import asyncio
import json
import logging
import math
import os
import time as _time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from paths import ProjectPath

from core.memory.embedding_provider import EmbeddingProvider
from core.schema.memory import MemoryType
from core.schema.types import MemoryId

if TYPE_CHECKING:
    from core.storage.memory_repo import MemoryRepository

logger = logging.getLogger(__name__)


@dataclass
class MemoryEntry:
    id: MemoryId = MemoryId("")         # 唯一标识（insert 时自动生成）
    text: str = ""
    embedding: list[float] = field(default_factory=list, repr=False)
    memory_type: MemoryType = MemoryType.NOTE   # Fact/Preference/Event/Plan/Note（构造时 coerce 归一）
    poignancy: int = 5             # 重要性 1-10
    timestamp: float = 0.0         # Unix 时间戳
    metadata: dict = field(default_factory=dict)
    _milvus_pk: int | None = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        """构造时归一化 memory_type（dataclass 无自动校验，直接赋值会绕过）。"""
        self.memory_type = MemoryType.coerce(self.memory_type)

    @property
    def dimension(self) -> int:
        """向量维度。"""
        return len(self.embedding)


class LongTermMemory:
    """长期记忆，可选 Milvus 向量检索 + embedding。

    优雅降级：
    - 有 Milvus → 向量存 Milvus，语义 ANN 检索
    - 无 Milvus → 内存列表 + 字符重叠检索
    - 有 embedding → insert 时自动嵌入以提升检索质量
    - 无 embedding → 纯文本存储，子串检索

    Embedding 首次使用时懒加载（不在连接时），带 10s 超时避免阻塞启动。
    """

    def __init__(self, max_entries: int = 10000, max_age_days: int = 90,
                 embed_timeout: float = 10.0,
                 enable_eviction: bool = True,
                 repo: "MemoryRepository | None" = None,
                 pending_syncs_file: str | Path | None = None):
        """初始化长期记忆。

        Args:
            max_entries: 最大条目数，超限时触发驱逐。
            max_age_days: 最大保留天数（当前未使用，保留供后续实现）。
            embed_timeout: 嵌入后端加载超时（秒）。
            enable_eviction: 是否启用数量上限驱逐。
            repo: MemoryRepository 实例，用于 Milvus 操作。如果为 None，在 connect() 时自动创建。
            pending_syncs_file: 未同步条目 journal 路径（测试注入），默认
                ProjectPath.MEMORY_PENDING_SYNCS。
        """
        self._entries: list[MemoryEntry] = []
        self._repo: "MemoryRepository | None" = repo
        self._embedding_provider = EmbeddingProvider(embed_timeout)
        self._dirty_ids: set[MemoryId] = set()  # 未同步到 Milvus 的条目 ID
        self._max_entries = max_entries
        self._max_age_days = max_age_days
        self._absorbed_ids: set[MemoryId] = set()  # 被 merge 吸收但尚未 delete 的条目 ID
        self._enable_eviction = enable_eviction
        self._write_lock = asyncio.Lock()     # 保护所有修改操作的并发锁
        # B5/P1-12：离线删除的持久 tombstone（entry_id → milvus pk）——重连/重启后
        # 清理旧行，防止删除意图丢失、旧数据复活。
        self._pending_deletes: dict[MemoryId, int] = {}
        self._load_pending_deletes()
        # review 复核 #6：未同步条目的持久 journal——Milvus 离线 insert 的记忆此前
        # 仅在进程内 _dirty_ids，进程退出即丢失；本 journal 重启/重连后恢复补写。
        self._pending_syncs_file_path = pending_syncs_file
        self._load_pending_syncs()

    async def connect(self) -> bool:
        """连接 Milvus（快速）。嵌入推迟到首次使用时加载。

        Returns:
            True 表示 Milvus 可用，False 表示不可用（降级到纯内存模式）。
        """
        if self._repo is None:
            from core.storage.memory_repo import MemoryRepository
            self._repo = MemoryRepository()
        result = await self._repo.connect()
        if result:
            loaded = await self._load_from_milvus()
            if loaded:
                logger.info("LongTermMemory: loaded %d entries from Milvus", loaded)
            # B5/P1-12：重连后清理离线删除的 tombstone（防止删除意图丢失/旧数据复活）
            await self._apply_pending_deletes()
            return True
        else:
            return False

    def _pending_deletes_file(self) -> str:
        """pending deletes journal 路径。"""
        return str(ProjectPath.MEMORY_PENDING_DELETES.resolve())

    def _load_pending_deletes(self) -> None:
        """启动时从 journal 加载离线删除 tombstone（损坏/缺失忽略）。"""
        try:
            path = self._pending_deletes_file()
            if not Path(path).is_file():
                return
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            self._pending_deletes = {
                MemoryId(str(eid)): int(pk)
                for eid, pk in (data or {}).items()
            }
        except Exception:
            logger.warning("读取 memory pending-deletes journal 失败", exc_info=True)
            self._pending_deletes = {}

    def _save_pending_deletes(self) -> None:
        """持久化离线删除 tombstone（原子写）。"""
        try:
            path = self._pending_deletes_file()
            if not self._pending_deletes:
                # 空集合 → 删除/清空 journal（避免陈旧 tombstone 误删已重建条目）
                if Path(path).is_file():
                    os.remove(path)
                return
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({str(k): v for k, v in self._pending_deletes.items()}, f)
            os.replace(tmp, path)
        except Exception:
            logger.warning("写入 memory pending-deletes journal 失败", exc_info=True)

    # ---- 未同步条目 journal（review 复核 #6）----

    def _pending_syncs_file(self) -> str:
        """pending syncs journal 路径。"""
        return str(self._pending_syncs_file_path or ProjectPath.MEMORY_PENDING_SYNCS.resolve())

    def _load_pending_syncs(self) -> None:
        """启动时从 journal 加载未同步条目（进程退出/崩溃前 Milvus 写入失败的记忆）。

        与 `_pending_deletes` 对称：损坏/缺失忽略，重启后恢复补写不丢离线记忆。
        """
        try:
            path = self._pending_syncs_file()
            if not Path(path).is_file():
                return
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            existing_ids = {e.id for e in self._entries}
            for eid, doc in (data or {}).items():
                if eid in existing_ids:
                    continue
                entry = MemoryEntry(
                    id=MemoryId(eid),
                    text=doc.get("text", ""),
                    embedding=list(doc.get("embedding") or []),
                    memory_type=doc.get("memory_type", "Note"),
                    poignancy=int(doc.get("poignancy", 5)),
                    timestamp=float(doc.get("timestamp", 0.0)),
                    metadata=dict(doc.get("metadata") or {}),
                )
                self._entries.append(entry)
                existing_ids.add(eid)
                self._dirty_ids.add(entry.id)
        except Exception:
            logger.warning("读取 memory pending-syncs journal 失败", exc_info=True)

    def _save_pending_syncs(self) -> None:
        """持久化未同步条目（原子写；空集合删除 journal）。

        只序列化仍存在且带 embedding 的脏条目——同步成功的条目落盘后从 journal 移除，
        防止重启后重复补写。
        """
        try:
            path = self._pending_syncs_file()
            entries: dict[str, dict[str, object]] = {}
            for eid in self._dirty_ids:
                entry = self._get_by_id(eid)
                if entry is None or not entry.embedding:
                    continue
                entries[str(eid)] = {
                    "text": entry.text,
                    "embedding": entry.embedding,
                    "memory_type": entry.memory_type.value,
                    "poignancy": entry.poignancy,
                    "timestamp": entry.timestamp,
                    "metadata": entry.metadata,
                }
            if not entries:
                if Path(path).is_file():
                    os.remove(path)
                return
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(entries, f)
            os.replace(tmp, path)
        except Exception:
            logger.warning("写入 memory pending-syncs journal 失败", exc_info=True)

    async def _apply_pending_deletes(self) -> None:
        """重连/重启后清理离线删除的 Milvus 旧行（尽力而为，失败保留待下次）。"""
        if self._repo is None or not self._pending_deletes:
            return
        for eid, pk in list(self._pending_deletes.items()):
            try:
                await self._repo.delete([pk])
                self._pending_deletes.pop(eid, None)
            except Exception:
                logger.debug("pending delete 重试失败 entry=%s pk=%s", eid, pk, exc_info=True)
        self._save_pending_deletes()

    async def _load_from_milvus(self) -> int:
        """从 Milvus 恢复已有条目到内存。

        B5/P1-12：按 entry_id 幂等去重——连续 connect（/restart）不重复 append。

        Returns:
            成功加载的条目数量。加载失败返回 0。
        """
        if self._repo is None:
            return 0
        try:
            results = await self._repo.query(
                filter_expr="id >= 0",
                output_fields=["id", "entry_id", "text", "memory_type", "poignancy", "timestamp"],
                limit=self._max_entries,
            )
        except Exception as e:
            logger.warning("LongTermMemory: 从 Milvus 加载失败: %s", e)
            return 0

        from core.utils import new_memory_id

        existing_ids = {e.id for e in self._entries}
        loaded = 0
        for row in results:
            pk = row.get("id")
            # 优先使用存储的 entry_id，兼容无 entry_id 字段的旧数据
            entry_id = row.get("entry_id", "")
            if not entry_id or not isinstance(entry_id, str):
                entry_id = new_memory_id()
            eid = MemoryId(entry_id)
            if eid in existing_ids:
                # review 复核 #6：journal 已含该条目（此前未同步）但 Milvus 端已有行
                # （同步完成与 journal 清除之间崩溃）→ 补 pk 并从 dirty 移除，避免重复补写。
                for existing in self._entries:
                    if existing.id == eid:
                        if existing._milvus_pk is None:
                            existing._milvus_pk = pk
                            self._dirty_ids.discard(eid)
                            self._save_pending_syncs()
                        break
                continue  # 已加载——不重复（连续 connect 不倍增）
            entry = MemoryEntry(
                id=eid,
                text=row.get("text", ""),
                embedding=[],  # 不加载向量 — 太大，搜索走 Milvus
                memory_type=row.get("memory_type", "Note"),
                poignancy=int(row.get("poignancy", 5)),
                timestamp=float(row.get("timestamp", 0.0)),
                _milvus_pk=pk,
            )
            self._entries.append(entry)
            existing_ids.add(eid)
            loaded += 1

        # 启动时驱逐超出上限的条目
        if loaded:
            await self._evict_overflow()

        return loaded

    async def health_check(self) -> bool:
        """尝试重连 Milvus。

        Returns:
            True 表示已连接或重连成功，False 表示不可用。
        """
        if self._repo is not None:
            return await self._repo.health_check()
        return False

    # ---- Write ----

    async def insert(self, text: str, **metadata: object) -> None:
        """插入一条记忆。如果有嵌入后端则自动嵌入。

        Args:
            text: 记忆文本。
            **metadata: 可选元数据，支持 memory_type (或 type)、
                poignancy (1-10)、timestamp (Unix 时间戳)、source 等关键字。
        """
        embedding = await self._embedding_provider.embed(text)

        async with self._write_lock:
            from core.utils import new_memory_id
            # 在副本上操作，避免修改调用方传入的 dict
            meta: dict[str, Any] = dict(metadata)
            # 接受 'type' 作为 'memory_type' 的别名
            memory_type = meta.pop("memory_type", meta.pop("type", "Note"))
            entry = MemoryEntry(
                id=new_memory_id(),
                text=text,
                embedding=embedding,
                memory_type=memory_type,
                poignancy=int(meta.pop("poignancy", 5)),
                timestamp=float(meta.pop("timestamp", _time.time())),
                metadata=meta,
            )

            if self._repo is not None and embedding:
                try:
                    pk_list = await self._repo.insert(
                        vectors=[embedding],
                        metadata=[{
                            "entry_id": entry.id,
                            "text": text,
                            "memory_type": entry.memory_type.value,
                            "poignancy": entry.poignancy,
                            "timestamp": entry.timestamp,
                            **meta,
                        }],
                    )
                    if pk_list and pk_list[0] is not None:
                        entry._milvus_pk = int(pk_list[0])
                        logger.debug("LongTermMemory insert: entry_id=%s, pk=%s", entry.id, pk_list[0])
                    else:
                        # B5/P1-12：无 durable PK 不能视为成功——标记脏，健康后同步
                        self._dirty_ids.add(entry.id)
                except Exception as e:
                    logger.warning("Milvus insert failed: %s, falling back to in-memory", e)
                    self._dirty_ids.add(entry.id)
            elif self._repo is None:
                self._dirty_ids.add(entry.id)
            if entry.id in self._dirty_ids:
                # review 复核 #6：离线/无 PK → 持久 journal，防进程退出丢失待同步记忆
                self._save_pending_syncs()

            self._entries.append(entry)
            if self._enable_eviction:
                await self._evict_overflow()

    async def _evict_overflow(self) -> int:
        """数量上限驱逐：超过 max_entries 时保留最新的 N 条。

        在每次 insert() 后调用，保持内存列表 + Milvus 健康。

        Returns:
            驱逐的条目数量。未超限时返回 0。
        """
        removed = 0
        if len(self._entries) <= self._max_entries:
            return removed

        # 按时间戳倒序，保留最新的 max_entries 条
        self._entries.sort(key=lambda e: e.timestamp, reverse=True)
        overflow = len(self._entries) - self._max_entries
        evicted = self._entries[-overflow:]
        evicted_ids = {e.id for e in evicted}
        evicted_pks = [e._milvus_pk for e in evicted if e._milvus_pk is not None]
        self._entries = self._entries[:self._max_entries]
        removed = overflow

        # 同步清理 Milvus
        if self._repo is not None and evicted_pks:
            try:
                deleted = await self._repo.delete(evicted_pks)
                if deleted < len(evicted_pks):
                    # B5/P1-12：部分/全部未删（离线）→ 记 tombstone 待重连清理
                    for e in evicted:
                        if e._milvus_pk is not None:
                            self._pending_deletes[e.id] = e._milvus_pk
                    self._save_pending_deletes()
            except Exception:
                # B5/P1-12：驱逐删除失败 → tombstone
                for e in evicted:
                    if e._milvus_pk is not None:
                        self._pending_deletes[e.id] = e._milvus_pk
                self._save_pending_deletes()

        # 同步清理 dirty_ids
        if evicted_ids:
            self._dirty_ids -= evicted_ids
            self._save_pending_syncs()  # review 复核 #6：journal 同步移除被驱逐的待同步条目

        if removed:
            logger.info("Long-term memory: 驱逐 %d 条超限条目 (剩余 %d)",
                       removed, len(self._entries))
        return removed

    def _get_by_id(self, entry_id: MemoryId) -> MemoryEntry | None:
        """通过 ID 查找条目。

        Args:
            entry_id: 条目唯一标识。

        Returns:
            找到的 MemoryEntry，未找到返回 None。
        """
        for e in self._entries:
            if e.id == entry_id:
                return e
        return None

    def get_all(self) -> list[MemoryEntry]:
        """返回全部活跃条目的副本。

        Returns:
            排除已吸收（absorbed）条目后的 MemoryEntry 列表。
        """
        return [e for e in self._entries if e.id not in self._absorbed_ids]

    async def mark_absorbed(self, entry_id: MemoryId) -> None:
        """标记条目已被 merge 吸收，同步清理 Milvus + 内存。

        Args:
            entry_id: 被吸收的条目 ID。
        """
        self._absorbed_ids.add(entry_id)
        async with self._write_lock:
            entry = self._get_by_id(entry_id)
            if entry is None:
                return
            if entry._milvus_pk is not None and self._repo is not None:
                try:
                    deleted = await self._repo.delete([entry._milvus_pk])
                    if deleted < 1:
                        self._pending_deletes[entry_id] = entry._milvus_pk
                        self._save_pending_deletes()
                except Exception:
                    # B5/P1-12：吸收条目删除失败 → tombstone 待重连清理
                    self._pending_deletes[entry_id] = entry._milvus_pk
                    self._save_pending_deletes()
            # 从 _entries 移除，释放内存
            self._entries = [e for e in self._entries if e.id != entry_id]
            self._dirty_ids.discard(entry_id)
            self._save_pending_syncs()  # review 复核 #6：journal 移除已吸收的待同步条目

    async def sync_dirty_entries(self) -> int:
        """尝试将脏条目同步到 Milvus。

        由 _db_health_loop 在 Milvus 从故障恢复时调用——Milvus 写入失败的记忆
        此前仅在内存（_milvus_pk=None，重启后丢失），恢复后补写并建立向量索引。

        Returns:
            成功同步的条目数。
        """
        async with self._write_lock:
            if self._repo is None or not self._dirty_ids:
                return 0
            synced = 0
            for eid in list(self._dirty_ids):
                entry = self._get_by_id(eid)
                # review 复核 #6：取得 _write_lock 防并发幽灵写——mark_absorbed/delete
                # 不能与补写竞态（删除后仍插回一条，重启后记忆复活）。eid 已吸收也跳过。
                if entry is None or not entry.embedding or eid in self._absorbed_ids:
                    continue
                try:
                    pk_list = await self._repo.insert(
                        vectors=[entry.embedding],
                        metadata=[{
                            "entry_id": entry.id,
                            "text": entry.text,
                            "memory_type": entry.memory_type.value,
                            "poignancy": entry.poignancy,
                            "timestamp": entry.timestamp,
                            **entry.metadata,
                        }],
                    )
                    if pk_list and pk_list[0] is not None:
                        entry._milvus_pk = int(pk_list[0])
                        self._dirty_ids.discard(eid)
                        synced += 1
                    # B5/P1-12：空 PK（离线）→ 保留 dirty 待下次健康轮询重试，不计成功
                except Exception:
                    logger.debug("Sync entry %s to Milvus failed", eid, exc_info=True)
            if synced:
                # review 复核 #6：同步成功的条目从 journal 移除（防重启重复补写）
                self._save_pending_syncs()
                logger.info("Synced %d dirty entries to Milvus", synced)
            return synced

    # ---- Delete ----

    async def update_text(
        self, entry_id: MemoryId, new_text: str,
        poignancy: int | None = None,
        memory_type: str | None = None,
    ) -> bool:
        """更新记忆文本（内存 + Milvus 重建）。

        Args:
            entry_id: 要更新的条目 ID。
            new_text: 新的记忆文本。
            poignancy: 新的重要性分数（1-10），None 表示不更新。
            memory_type: 新的记忆类型，None 表示不更新。

        Returns:
            True 表示找到并更新了条目，False 表示条目不存在。
        """
        async with self._write_lock:
            for i, e in enumerate(self._entries):
                if e.id == entry_id:
                    e.text = new_text
                    if poignancy is not None:
                        e.poignancy = poignancy
                    if memory_type is not None:
                        e.memory_type = MemoryType.coerce(memory_type)
                    e.timestamp = _time.time()
                    if self._repo is not None and e._milvus_pk is not None:
                        old_pk = e._milvus_pk
                        emb = await self._embedding_provider.embed(new_text)
                        if not emb:
                            # embedding 后端不可用——标记脏，等 sync_dirty_entries 重试，
                            # 否则旧向量与新文本绑定参与 rerank、重启后旧文本静默回滚
                            self._dirty_ids.add(entry_id)
                        else:
                            try:
                                pk_list = await self._repo.insert(
                                    vectors=[emb],
                                    metadata=[{
                                        "entry_id": e.id,
                                        "text": new_text,
                                        "memory_type": e.memory_type.value,
                                        "poignancy": e.poignancy,
                                        "timestamp": e.timestamp,
                                    }],
                                )
                                if pk_list:
                                    e._milvus_pk = int(pk_list[0])
                                    logger.info("LongTermMemory update_text: entry_id=%s, old_pk=%s, new_pk=%s",
                                                entry_id, old_pk, pk_list[0])
                                    # 新向量插入成功后才删除旧向量
                                    try:
                                        await self._repo.delete([old_pk])
                                    except Exception:
                                        logger.debug("Milvus delete old pk failed for update %s", entry_id, exc_info=True)
                                else:
                                    # 插入返回空 → 标记脏，保留旧向量
                                    self._dirty_ids.add(entry_id)
                            except Exception:
                                logger.debug("Embedding/insert failed for update %s", entry_id, exc_info=True)
                                self._dirty_ids.add(entry_id)
                    if entry_id in self._dirty_ids:
                        self._save_pending_syncs()  # review 复核 #6：journal 同步未补写的更新
                    return True
            return False

    async def delete(self, entry_id: MemoryId) -> bool:
        """删除单条记忆（内存 + Milvus）。

        Args:
            entry_id: 要删除的条目 ID。

        Returns:
            True 表示找到并删除了条目，False 表示条目不存在。
        """
        async with self._write_lock:
            for i, e in enumerate(self._entries):
                if e.id == entry_id:
                    self._entries.pop(i)
                    self._absorbed_ids.discard(entry_id)
                    self._dirty_ids.discard(entry_id)
                    self._save_pending_syncs()  # review 复核 #6：journal 移除已删的待同步条目
                    if self._repo is not None and e._milvus_pk is not None:
                        logger.info("LongTermMemory delete: entry_id=%s, milvus_pk=%s", entry_id, e._milvus_pk)
                        try:
                            deleted = await self._repo.delete([e._milvus_pk])
                            if deleted < 1:
                                # B5/P1-12：repo 离线/无行删除 → 记持久 tombstone 待重连清理
                                self._pending_deletes[entry_id] = e._milvus_pk
                                self._save_pending_deletes()
                        except Exception:
                            # B5/P1-12：删除失败 → tombstone，防旧行重启复活
                            self._pending_deletes[entry_id] = e._milvus_pk
                            self._save_pending_deletes()
                    return True
            return False

    async def clear_all(self) -> int:
        """清空所有记忆（内存 + Milvus 重建 collection）。可独立调用，无需先 connect()。

        Returns:
            清除的条目总数。
        """
        async with self._write_lock:
            count = len(self._entries)
            logger.info("LongTermMemory: clearing all %d entries", count)
            self._entries.clear()
            self._absorbed_ids.clear()
            self._dirty_ids.clear()
            self._save_pending_syncs()  # review 复核 #6：清空 journal（空集合删除文件）

            if self._repo is not None:
                # 已有 repo → 直接操作
                await self._repo.clear_all()
            else:
                # 无已有 repo → 自行创建临时 repo
                from core.storage.memory_repo import MemoryRepository
                try:
                    temp_repo = MemoryRepository()
                except Exception:
                    # MemoryRepository 创建失败（极端情况）→ 静默降级
                    return count
                try:
                    await temp_repo.connect()
                    await temp_repo.clear_all()
                except Exception:
                    # Milvus 不可达时静默降级：内存已清空，返回 count
                    pass
                finally:
                    await temp_repo.close()
            return count

    # ---- Read ----

    async def search(
        self,
        query_text: str = "",
        query_embedding: list[float] | None = None,
        top_k: int = 5,
        memory_type: str | None = None,
    ) -> list[MemoryEntry]:
        """向量搜索 + rerank（相似度 + 重要性 + 新鲜度）。

        Args:
            query_text: 查询文本，用于向量化搜索。
            query_embedding: 查询向量，提供时跳过文本向量化。
            top_k: 返回的最大结果数。
            memory_type: 按记忆类型过滤（如 "Fact"、"Preference"）。

        Returns:
            按综合分数降序排列的 MemoryEntry 列表。
        """
        backend = await self._embedding_provider.ensure(wait=False)

        if not self._entries and self._repo is None:
            return []

        candidates: list[MemoryEntry] = []
        distances: dict[int, float | None] = {}  # id(entry) → L2 distance 或 None（fallback 标记）

        # 尝试 Milvus——取 top_k * 4 作为 rerank 候选池
        if self._repo is not None and await self._repo.health_check():
            vec = query_embedding
            if vec is None and query_text and backend is not None:
                try:
                    vec = await backend.embed_query(query_text)
                except Exception:
                    logger.debug("Embedding failed for search", exc_info=True)

            if vec:
                try:
                    raw = await self._repo.search(
                        vector=vec,
                        top_k=min(top_k * 4, 20),
                        output_fields=["id", "text", "memory_type", "poignancy", "timestamp"],
                    )
                    # 从 Milvus 结果构建 PK→distance + text→distance（fallback）映射
                    pk_to_distance: dict[int, float] = {}
                    text_fb: dict[str, float] = {}
                    for r in raw:
                        pk = r.get("id")
                        text = r.get("text", "")
                        d = float(r.get("distance", r.get("score", 1.0)))
                        if pk is not None:
                            pk_to_distance[pk] = d
                        else:
                            text_fb[text] = d

                    # 将 Milvus 结果与内存条目匹配：优先 PK，文本兜底
                    for e in self._entries:
                        if e._milvus_pk is not None and e._milvus_pk in pk_to_distance:
                            candidates.append(e)
                            distances[id(e)] = pk_to_distance[e._milvus_pk]
                        elif e._milvus_pk is None and e.text in text_fb:
                            candidates.append(e)
                            distances[id(e)] = text_fb[e.text]
                            logger.debug("LongTermMemory: entry matched via text fallback (no PK)")
                except Exception as e:
                    logger.warning("Milvus search failed: %s", e)

        # 从搜索结果中排除已吸收条目
        candidates = [e for e in candidates if e.id not in self._absorbed_ids]

        # Milvus 无候选时回退到内存搜索
        fb_scores: dict[int, float] = {}
        if not candidates:
            fallback_results = self._search_fallback(
                query_text, min(top_k * 2, len(self._entries))
            )
            # 从兜底结果中排除已吸收条目
            fallback_results = [(s, e) for s, e in fallback_results if e.id not in self._absorbed_ids]
            fb_scores = {id(e): s for s, e in fallback_results}
            for fb_score, entry in fallback_results:
                candidates.append(entry)
                distances[id(entry)] = None  # flag for rerank to use fb_score

        if not candidates:
            return []

        # 重排序：0.5 × 相似度 + 0.3 × 重要性 + 0.2 × 新鲜度
        now = _time.time()
        scored: list[tuple[float, MemoryEntry]] = []
        for entry in candidates:
            # 相似度：Milvus 用 1/(1+L2_distance)，否则用兜底重叠分数
            dist = distances.get(id(entry), 0.0)
            if dist is not None:
                sim = 1.0 / (1.0 + dist)
            else:
                sim = fb_scores.get(id(entry), 0.3)

            poi = min(entry.poignancy, 10) / 10.0  # 归一化到 [0,1]

            age_days = (now - entry.timestamp) / 86400 if entry.timestamp > 0 else 30
            rec = math.exp(-0.1 * age_days)  # 指数衰减，半衰期约 6.9 天

            score = 0.5 * sim + 0.3 * poi + 0.2 * rec
            scored.append((score, entry))

        # 按 memory_type 过滤（如指定）——try_coerce 大小写不敏感，未知值不命中
        if memory_type:
            mt = MemoryType.try_coerce(memory_type)
            scored = [(s, e) for s, e in scored if mt is not None and e.memory_type == mt]

        scored.sort(key=lambda x: x[0], reverse=True)
        return [e for _, e in scored[:top_k]]

    def _search_fallback(self, query: str, top_k: int) -> list[tuple[float, MemoryEntry]]:
        """In-memory 文本重叠搜索，供 rerank 使用。

        Args:
            query: 查询文本。
            top_k: 返回的最大结果数。

        Returns:
            (score, entry) 元组列表，按分数降序排列。
        """
        if not query:
            return [(0.0, e) for e in self._entries[-top_k:]] if self._entries else []

        query_lower = query.lower()
        scored: list[tuple[float, MemoryEntry]] = []
        for e in self._entries:
            text_lower = e.text.lower()
            raw = sum(1 for ch in query_lower if ch != " " and ch in text_lower)
            if raw > 0:
                # 相对查询长度归一化重叠分数
                score = raw / max(len(query.strip()), 1)
                scored.append((score, e))
        scored.sort(key=lambda x: x[0], reverse=True)
        return scored[:top_k]

    # ---- Lifecycle ----

    @property
    def count(self) -> int:
        """当前活跃条目总数。"""
        return len(self._entries)

    async def close(self) -> None:
        """关闭长期记忆：取消嵌入加载、关闭 Milvus 连接。"""
        self._embedding_provider.cancel()
        if self._repo:
            await self._repo.close()
