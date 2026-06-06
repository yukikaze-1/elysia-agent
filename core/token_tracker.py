"""TokenTracker — 统一管理 LLM token 消耗统计、广播和持久化。

Usage:
    tracker = TokenTracker(session_id="abc", output_manager=output, document_db=db)
    await tracker.record(usage, model="deepseek/deepseek-chat", round_num=0)
    stats = await tracker.get_session_stats()
    await tracker.flush_pending()
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, asdict
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from Module.output import OutputManager


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

    def __init__(
        self,
        session_id: str,
        output_manager: "OutputManager",
        document_db=None,
    ):
        self.session_id = session_id
        self._output = output_manager
        self._db = document_db

        # Session 内存累计
        self.session_total: int = 0
        self.per_model: dict[str, int] = {}
        self.current_turn_usages: list[TokenUsage] = []

    def start_turn(self) -> None:
        """新一轮用户对话开始前清空 turn 缓存。"""
        self.current_turn_usages.clear()

    async def record(self, usage: dict, model: str, round_num: int) -> None:
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
        if self._db is not None:
            try:
                docs = await self._db.find(
                    "token_usage",
                    {"session_id": self.session_id},
                    limit=10000,
                )
                per_model: dict[str, int] = {}
                session_total = 0
                for doc in docs:
                    model = doc["model"]
                    total = doc["total_tokens"]
                    per_model[model] = per_model.get(model, 0) + total
                    session_total += total

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
        if self._db is None:
            return
        if not os.path.exists(self._pending_file):
            return
        try:
            with open(self._pending_file) as f:
                records = [json.loads(line) for line in f if line.strip()]
            if records:
                await self._db.insert_many("token_usage", records)
            os.remove(self._pending_file)
        except Exception:
            pass  # 下次再试

    async def _persist(self, usage: TokenUsage) -> None:
        """持久化单条记录：MongoDB 优先，失败则写本地 JSONL。"""
        doc = {**asdict(usage), "session_id": self.session_id}
        if self._db is not None:
            try:
                await self._db.insert_one("token_usage", doc)
                return
            except Exception:
                pass  # 降级到文件
        # 追加到本地 JSONL
        os.makedirs(os.path.dirname(self._pending_file), exist_ok=True)
        doc.pop("_id", None)  # PyMongo insert_one 会原地注入 ObjectId，JSON 不可序列化
        with open(self._pending_file, "a") as f:
            f.write(json.dumps(doc, ensure_ascii=False) + "\n")
