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
from typing import TYPE_CHECKING

from paths import ProjectPath
from core.schema.token import TokenUsage

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from Module.output import OutputManager
    from core.schema.types import SessionId
    from core.storage.token_repo import TokenRepository


class TokenTracker:
    """Token 消耗统计器。

    负责：
    - 内存累计（session 内按模型分类）
    - WebSocket 广播（前端实时显示）
    - MongoDB 持久化（不可用时降级到本地 JSONL 文件）

    ## `_pending_file` 的解析时机（2026-09-27 测试隔离专项修复）

    **路径在实例构造时解析**，不是 `import` 时固化，也不是每次访问重读环境变量。

    旧实现在类体里写 `_pending_file = str(ProjectPath.TOKEN_PENDING.resolve())`——
    该表达式在**模块 import 时**求值一次，于是 `ELYSIA_PATH_TOKEN_PENDING` 被永久固化：

        os.environ["ELYSIA_PATH_TOKEN_PENDING"] = "/tmp/x.jsonl"
        import core.monitoring.token_tracker        # ← 此刻定格
        os.environ["ELYSIA_PATH_TOKEN_PENDING"] = "/tmp/y.jsonl"
        TokenTracker(...)._pending_file             # 仍是 /tmp/x.jsonl（错）

    后果不是"洁癖"：根 `conftest.py :: _isolate_shared_journals` 用 env 把 7 个
    journal（scheduler checkpoint / consolidator pending / task result WAL …）
    重定向到 `tmp_path`，**唯独这一个不能用同一机制**——env 改了也不生效，测试因此
    可以写到仓库真实的 `data/token_usage_pending.jsonl`。修法两条一起上：
    ① 本处改为实例级解析；② 该 env 补进 `_isolate_shared_journals`。

    为什么不做成 `@property` 每次 `os.getenv`：路径是**配置**，不是每次访问都要重新
    决策的量；同一实例的读写必须落在同一个文件（`flush_pending` 读完就 `os.remove`）。
    构造时解析一次既满足"测试能按环境隔离"，又保持"一个实例一个路径"。
    副作用（有意）：同进程内**先构造**的实例不会跟着后续 env 变化——这是延迟无关的
    确定性，而不是缺陷；生产环境变量在进程启动后就固定。
    """

    @staticmethod
    async def clear_all(repo: "TokenRepository") -> int:
        """清空所有 token_usage 记录。返回删除数量。"""
        return await repo.clear_all()

    def __init__(
        self,
        session_id: SessionId,
        output_manager: "OutputManager",
        repo: "TokenRepository | None" = None,
        pending_file: str | None = None,
    ):
        """初始化 TokenTracker。

        Args:
            session_id: 当前会话 ID。
            output_manager: 输出管理器，用于 WebSocket 广播。
            repo: Token 持久化仓库，为 None 时仅使用本地 JSONL 文件。
            pending_file: pending JSONL 路径覆盖（显式注入，优先于环境解析）。
                缺省按**构造时**的 `ELYSIA_PATH_TOKEN_PENDING`（无则
                `data/token_usage_pending.jsonl`）解析。
        """
        self.session_id = session_id
        self._output = output_manager
        self._repo = repo
        # 实例级路径：构造时按当时环境解析一次（见类 docstring）
        self._pending_file = (
            pending_file if pending_file is not None
            else str(ProjectPath.TOKEN_PENDING.resolve())
        )

        # Session 内存累计
        self.session_total: int = 0
        self.per_model: dict[str, int] = {}
        self.current_turn_usages: list[TokenUsage] = []

        # 批次 3（multi-session）：按会话累计。session_total 保持普通 int 属性，
        # record 时同步为最近记录会话累计（debug/info.py、runtime_config 既有读取不破）。
        self._session_totals: dict[SessionId, int] = {}
        self._session_models: dict[SessionId, dict[str, int]] = {}
        self._last_recorded_session: SessionId | None = None

    def start_turn(self) -> None:
        """新一轮用户对话开始前清空 turn 缓存。"""
        self.current_turn_usages.clear()

    async def record(
        self,
        usage: dict,
        model: str | None = None,
        round_num: int | None = None,
        session_id: SessionId | None = None,
    ) -> None:
        """记录一次 API 调用的 token 消耗。

        每轮 LLM 调用完成后由 CognitiveLoop 调用一次。
        自动完成：内存累计、WS 广播、持久化。
        session_id 缺省 → 沿用实例 self.session_id。
        批次 3（multi-session）：累计 _session_totals，并同步 session_total
        为最近记录会话累计（保留普通属性，内部 += 写点不动）。
        """
        model = model or ""
        round_num = round_num or 0
        sid = session_id if session_id is not None else self.session_id
        u = TokenUsage.from_api(
            usage, model=model, session_id=sid, round_num=round_num,
        )

        # 1. 内存累计
        self.session_total += u.total_tokens
        self.per_model[model] = self.per_model.get(model, 0) + u.total_tokens
        self.current_turn_usages.append(u)

        # 1b. 按会话累计；session_total 同步为最近记录会话累计
        self._session_totals[sid] = self._session_totals.get(sid, 0) + u.total_tokens
        session_models = self._session_models.setdefault(sid, {})
        session_models[model] = session_models.get(model, 0) + u.total_tokens
        self._last_recorded_session = sid
        self.session_total = self._session_totals[sid]

        # 2. WS 广播（前端实时更新）
        await self._output.token_usage({
            "turn": {
                "rounds": [r.model_dump(mode="json") for r in self.current_turn_usages],
                "turn_total": sum(r.total_tokens for r in self.current_turn_usages),
            },
            "session": {
                "total": self.session_total,
                # review 修复（批次 C）：by_model 按 sid 取（_session_models），与 total
                # （最近会话累计）一致——此前广播全局 per_model，跨会话累计泄漏。
                # self.per_model 属性保持全局（debug/runtime_config 既有读取兼容）。
                "by_model": dict(self._session_models[sid]),
            },
        })

        # 3. 持久化
        await self._persist(u)

    async def get_session_stats(self, session_id: SessionId | None = None) -> dict:
        """获取指定 session 的 token 统计（用于前端重连恢复）。

        session_id 缺省 → 最近记录会话（兼容 ws_handler.py 零参调用）。
        优先从 MongoDB 查询，补充 pending 文件中对应 session 的数据。
        如果 MongoDB 不可用，返回内存数据。

        批次 3 review 修复（F8）：get_session_stats(sid) **严格按 sid**——Mongo 离线
        且该 sid 无记录时返回 0/空，不泄漏最近记录会话/全局累计。session_total 属性
        仍保持「最近记录会话」语义（debug/runtime_config 兼容），但本方法不再回退它。
        """
        sid = session_id if session_id is not None else (
            self._last_recorded_session or self.session_id
        )
        if self._repo is not None:
            try:
                per_model = await self._repo.total_tokens_by_model(sid)
                session_total = sum(per_model.values())

                # 补充 pending 文件中该 session 的数据
                if os.path.exists(self._pending_file):
                    with open(self._pending_file) as f:
                        for line in f:
                            if not line.strip():
                                continue
                            r = json.loads(line)
                            if r.get("session_id") == sid:
                                m = r["model"]
                                per_model[m] = per_model.get(m, 0) + r["total_tokens"]
                                session_total += r["total_tokens"]

                # F8：调和仅限该 sid 的内存累计——Mongo 是该 sid 的持久化记录，内存
                # 是该 sid 在途未 flush 记录，取较大值；不掺其他会话（原 else 分支
                # 回退 self.per_model/self.session_total 会把最近记录会话累计泄漏给
                # 无记录的新会话，v1 建连 token_stats 显示其他会话数据）。
                mem_total = self._session_totals.get(sid, 0)
                if mem_total > session_total:
                    per_model = dict(self._session_models.get(sid, {}))
                    session_total = mem_total
                return {"total": session_total, "by_model": per_model}
            except Exception:
                # pending 文件读取/解析失败 → 回退内存数据，留 warning 可见
                logger.warning("get_session_stats 读取失败，回退内存数据",
                               exc_info=True)
        # F8：内存回退严格按 sid——无记录 = 0/空，不泄漏最近会话累计
        total = self._session_totals.get(sid, 0)
        per_model = self._session_models.get(sid, {})
        return {"total": total, "by_model": dict(per_model)}

    async def flush_pending(self) -> None:
        """将本地 pending 文件中的数据批量导入 MongoDB，成功后删除文件。

        旧格式行（prompt/completion）validate 失败时跳过且不保留——
        整文件导入成功后删除，与"不兼容直接改名、旧数据自然老化"一致。
        """
        if self._repo is None:
            return
        if not os.path.exists(self._pending_file):
            return
        try:
            records: list[TokenUsage] = []
            skipped = 0
            with open(self._pending_file) as f:
                for line in f:
                    if not line.strip():
                        continue
                    try:
                        records.append(TokenUsage.model_validate(json.loads(line)))
                    except Exception:
                        skipped += 1  # 旧格式行，跳过不保留
            if records:
                await self._repo.flush(records)
                logger.info("pending 文件导入完成: %d 条 token 记录已写入 MongoDB (跳过 %d 条旧格式)",
                            len(records), skipped)
            os.remove(self._pending_file)
        except Exception:
            logger.warning("pending 文件导入失败，下次启动重试", exc_info=True)

    async def _persist(self, usage: TokenUsage) -> None:
        """持久化单条记录：MongoDB 优先，失败则写本地 JSONL。"""
        if self._repo is not None:
            try:
                await self._repo.record(usage)
                return
            except Exception:
                logger.debug("MongoDB 不可用，token 记录写入 %s", self._pending_file)
        # 追加到本地 JSONL（session_id 已在 usage 内）
        os.makedirs(os.path.dirname(self._pending_file), exist_ok=True)
        doc = usage.model_dump(mode="json")
        with open(self._pending_file, "a") as f:
            f.write(json.dumps(doc, ensure_ascii=False) + "\n")
