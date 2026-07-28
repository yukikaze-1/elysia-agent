"""TokenTracker — 统一管理 LLM token 消耗统计、广播和持久化。

Usage:
    tracker = TokenTracker(session_id="abc", output_manager=output, repo=token_repo)
    await tracker.record(usage, model="deepseek/deepseek-chat", round_num=0)
    stats = await tracker.get_session_stats()
    await tracker.flush_pending()
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, asdict
from typing import TYPE_CHECKING

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from Module.output import OutputManager
    from core.schema.types import SessionId
    from core.storage.token_repo import TokenRepository


@dataclass
class TokenUsage:
    """单次 API 调用的 token 消耗记录。"""
    model: str
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    round_num: int
    timestamp: float


class TokenTracker:
    """Token 消耗统计器。

    负责：
    - 内存累计（session 内按模型分类）
    - WebSocket 广播（前端实时显示）
    - MongoDB 持久化（不可用时降级到本地 JSONL 文件）
    """

    _pending_file = os.path.join("data", "token_usage_pending.jsonl")

    @staticmethod
    async def clear_all(repo: "TokenRepository") -> int:
        """清空所有 token_usage 记录。返回删除数量。"""
        return await repo.clear_all()

    def __init__(
        self,
        session_id: SessionId,
        output_manager: "OutputManager",
        repo: "TokenRepository | None" = None,
    ):
        """初始化 TokenTracker。

        Args:
            session_id: 当前会话 ID。
            output_manager: 输出管理器，用于 WebSocket 广播。
            repo: Token 持久化仓库，为 None 时仅使用本地 JSONL 文件。
        """
        self.session_id = session_id
        self._output = output_manager
        self._repo = repo

        # Session 内存累计
        self.session_total: int = 0
        self.per_model: dict[str, int] = {}
        self.current_turn_usages: list[TokenUsage] = []

    def start_turn(self) -> None:
        """新一轮用户对话开始前清空 turn 缓存。"""
        self.current_turn_usages.clear()

    async def record(self, usage: dict[str, int], model: str, round_num: int) -> None:
        """记录一次 API 调用的 token 消耗。

        每轮 LLM 调用完成后由 CognitiveLoop 调用一次。
        自动完成：内存累计、WS 广播、持久化。
        """
        u = TokenUsage(
            model=model,
            prompt_tokens=usage["prompt_tokens"],
            completion_tokens=usage["completion_tokens"],
            total_tokens=usage["total_tokens"],
            round_num=round_num,
            timestamp=time.time(),
        )

        # 1. 内存累计
        self.session_total += u.total_tokens
        self.per_model[model] = self.per_model.get(model, 0) + u.total_tokens
        self.current_turn_usages.append(u)

        # 2. WS 广播（前端实时更新）
        await self._output.token_usage({
            "turn": {
                "rounds": [asdict(r) for r in self.current_turn_usages],
                "turn_total": sum(r.total_tokens for r in self.current_turn_usages),
            },
            "session": {
                "total": self.session_total,
                "by_model": dict(self.per_model),
            },
        })

        # 3. 持久化
        await self._persist(u)

    async def get_session_stats(self) -> dict:
        """获取当前 session 的 token 统计（用于前端重连恢复）。

        优先从 MongoDB 查询，补充 pending 文件中当前 session 的数据。
        如果 MongoDB 不可用，返回内存数据。
        """
        if self._repo is not None:
            try:
                per_model = await self._repo.total_tokens_by_model(self.session_id)
                session_total = sum(per_model.values())

                # 补充 pending 文件中当前 session 的数据
                if os.path.exists(self._pending_file):
                    with open(self._pending_file) as f:
                        for line in f:
                            if not line.strip():
                                continue
                            r = json.loads(line)
                            if r.get("session_id") == self.session_id:
                                m = r["model"]
                                per_model[m] = per_model.get(m, 0) + r["total_tokens"]
                                session_total += r["total_tokens"]

                if session_total > self.session_total:
                    self.session_total = session_total
                    self.per_model = per_model
                else:
                    # 内存是当前进程的 ground truth，取较大值
                    per_model = dict(self.per_model)
                    session_total = self.session_total
                return {"total": session_total, "by_model": per_model}
            except Exception:
                pass
        return {"total": self.session_total, "by_model": dict(self.per_model)}

    async def flush_pending(self) -> None:
        """将本地 pending 文件中的数据批量导入 MongoDB，成功后删除文件。"""
        if self._repo is None:
            return
        if not os.path.exists(self._pending_file):
            return
        try:
            from core.schema.token import TokenUsageEntry
            with open(self._pending_file) as f:
                records = [json.loads(line) for line in f if line.strip()]
            if records:
                entries = [TokenUsageEntry.model_validate(r) for r in records]
                await self._repo.flush(entries)
                logger.info("pending 文件导入完成: %d 条 token 记录已写入 MongoDB", len(records))
            os.remove(self._pending_file)
        except Exception:
            logger.warning("pending 文件导入失败，下次启动重试", exc_info=True)

    async def _persist(self, usage: TokenUsage) -> None:
        """持久化单条记录：MongoDB 优先，失败则写本地 JSONL。"""
        from core.schema.token import TokenUsageEntry
        from core.schema.types import SessionId

        entry = TokenUsageEntry(
            session_id=SessionId(self.session_id),
            model=usage.model,
            prompt_tokens=usage.prompt_tokens,
            completion_tokens=usage.completion_tokens,
            total_tokens=usage.total_tokens,
            round_num=usage.round_num,
        )
        if self._repo is not None:
            try:
                await self._repo.record(entry)
                return
            except Exception:
                logger.debug("MongoDB 不可用，token 记录写入 %s", self._pending_file)
        # 追加到本地 JSONL
        os.makedirs(os.path.dirname(self._pending_file), exist_ok=True)
        doc = entry.model_dump(mode="json")
        with open(self._pending_file, "a") as f:
            f.write(json.dumps(doc, ensure_ascii=False) + "\n")
