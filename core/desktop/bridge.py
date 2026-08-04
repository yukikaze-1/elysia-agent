"""DesktopToolBridge — Desktop 工具桥（锁 + future + 状态机 + 断连规则）。

批次 4（BE-041/043）：桌面工具「请求 → 幂等去重 → 定向投递 → 结果回传」的核心并发组件。
配套调用注册表 `DesktopCallRepository`（core/storage/desktop_call_repo.py，Task 3）；
wire 帧经契约层 `ToolRequestMessage`/`ToolStatusMessage`（frontend_contract，Task 1）。

职责（spec §4.2/§4.3）：
- **幂等去重（锁内完成）**：`_locks.setdefault((tool_name, arguments_hash), Lock())`
  串行化同参并发——锁内查 `_dedup` 索引（`dict[tuple[str,str], call_id]`），pending 命中
  复用 callId + await 原 future，不二次投递（防重复弹窗，BE-043）。offline/expired
  不参与去重（断连后原弹窗不可恢复，重试即新调用）。
- **先登记后投递（顺序不可颠倒）**：`self._pending[call_id] = future` 必须先于
  `deliver(tool.request)`；否则 Electron 极速回 status 时 `handle_status` 在登记前执行
  → 结果丢弃、future 挂超时。投递失败清理登记。
- **进程内 future 终态唯一**：`_pending[call_id]` 为 request_and_wait 的唤醒源；所有
  resolve 路径经 `if not future.done()` 防护——超时/取消/结果到达竞态不抛
  InvalidStateError；future 只 resolve 一次。
- **篡改拒绝（立即 resolve，不挂超时）**：wire `toolName/toolVersion/runtime/paramHash`
  与登记 `tool_name/tool_version/runtime/arguments_hash` 不一致 → 锁内 mark failed
  （CALL_IDEMPOTENCY_CONFLICT）+ 立即 resolve 明确失败态（TOOL_ERROR，message=
  "参数与请求不一致，已拒绝"）+ return False。
- **终态守卫**：已终态 call 的晚到 status 仅更新 repo result/error 字段（repo mark 终态
  语义：返回 False 但字段已写——**不能把 mark False 当「什么都没发生」**），不重复
  resolve（`if not future.done()` 防护）。
- **断连规则（D-o）**：`on_client_disconnect` → repo 标 offline + `_connected=False`；
  **pending（未接手）call 立即 resolve UNAVAILABLE**（回合及时收尾）；**accepted/running
  call 不 resolve**（Electron 可能已副作用，future 挂起到超时或晚到结果——重连后重传
  result，`offline → succeeded` 闭环）；重连不自动重投执行请求。
- **target_connection 填充**：`request_and_wait` 组装 call doc 必须填
  `target_connection=self._connection_id`（Task 8 附件持有者校验依赖）。

敏感纪律（D-p）：本模块 logger 只记 callId/状态/字段名，不打印 arguments 值。
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timedelta
from typing import Any, Awaitable, Callable

from Module.tool.protocol import ErrorType, MediaItem, ToolResult
from core.schema.utils import now_iso
from core.storage.desktop_call_repo import (
    STATUS_CANCELLED,
    STATUS_EXPIRED,
    STATUS_FAILED,
    STATUS_OFFLINE,
    STATUS_PENDING,
    STATUS_REJECTED,
    STATUS_SUCCEEDED,
    _ACTIVE_STATES,
    DesktopCallRepository,
)
from frontend_contract.ws_message import ToolRequestMessage, ToolStatusMessage, ToolStatusEnum

logger = logging.getLogger(__name__)

# 篡改拒绝错误码（spec §9 错误目录，CALL_IDEMPOTENCY_CONFLICT 语义在桥内落地）
_TAMPER_CODE = "CALL_IDEMPOTENCY_CONFLICT"


class DesktopToolBridge:
    """Desktop 工具桥 — 幂等去重 + future 终态唯一 + 状态机 + 断连规则。

    Args:
        repo: Task 3 的 DesktopCallRepository（进程内缓存读权威）。
        timeout: 单次调用超时秒数（默认 120s，与 desktop 插件 safety.request_timeout 对齐）。
    """

    def __init__(self, repo: DesktopCallRepository, timeout: float = 120.0) -> None:
        self._repo = repo
        self._timeout = timeout
        # 进程内 await 等待：call_id → future（request_and_wait 的唤醒源）
        self._pending: dict[str, asyncio.Future] = {}
        # 锁：tuple（幂等去重 per (tool, hash)）+ str（按 call_id 状态闩）两种键共存
        self._locks: dict[Any, asyncio.Lock] = {}
        # 幂等去重索引：(tool_name, arguments_hash) → call_id
        self._dedup: dict[tuple[str, str], str] = {}
        self._connected: bool = False
        self._connection_id: str | None = None
        # 投递回调：agent 层注入（Task 6），定位 electron channel → _send_json(tool.request)
        self._deliver: Callable[[dict], Awaitable[None]] | None = None

    # ---- 生命周期 ----

    def is_available(self) -> bool:
        """桌面客户端是否在线（同步——D-j 由 ToolAdapter.definitions 过滤消费）。"""
        return self._connected

    async def connect_db(self) -> bool:
        """建立 MongoDB 连接 + 全量 reload 入进程内缓存（_start_persistence 挂点）。"""
        return await self._repo.connect()

    async def health_check(self) -> bool:
        """桥的健康状态：桌面客户端是否可用（repo 离线降级不视为桥不可用）。"""
        return self._connected

    def bind_deliver(self, deliver: Callable[[dict], Awaitable[None]]) -> None:
        """绑定投递回调（Task 6 _start_tool_context 注入）。"""
        self._deliver = deliver

    # ---- 连接状态（ws_handler 握手/断开挂点） ----

    async def on_client_connect(self, connection_id: str) -> None:
        """Electron（X-Client-Platform: electron + v2）连接握手：标 connected。"""
        self._connected = True
        self._connection_id = connection_id

    async def on_client_disconnect(self, connection_id: str) -> None:
        """目标连接断开：repo 标 offline + 未接手 call 立即 resolve UNAVAILABLE。

        断连规则（spec §4.2 / D-o）：
        - pending（未收到 accepted/running，Electron 未接手）→ 立即 resolve UNAVAILABLE
          （Agent 回合及时收尾，不冻结 120s；此时无副作用已发生）。
        - accepted/running（Electron 已接手，可能已副作用）→ 不 resolve，future 挂起到
          超时或晚到结果（重连后重传 result，offline → 终态闭环）。
        - 重连不自动重投执行请求（防重复副作用，BE-043）。
        """
        if self._connection_id == connection_id:
            self._connected = False
            self._connection_id = None
        # 快照未接手（pending）call → 立即 resolve UNAVAILABLE
        for call_id, future in list(self._pending.items()):
            if future.done():
                self._pending.pop(call_id, None)
                continue
            doc = await self._repo.get(call_id)
            if doc is None or doc.get("target_connection") != connection_id:
                continue
            if doc.get("status") != STATUS_PENDING:
                continue  # accepted/running 已接手：挂起到超时或晚到结果
            if not future.done():
                future.set_result(ToolResult.fail(
                    name=doc.get("tool_name") or "",
                    error="桌面客户端已断开",
                    error_type=ErrorType.UNAVAILABLE,
                ))
            self._pending.pop(call_id, None)
            self._dedup.pop(
                self._dedup_key(doc.get("tool_name"), doc.get("arguments_hash")), None)
        # repo 标 offline（未接手 + 已接手的 pending/accepted/running 全部 → offline）
        await self._repo.mark_offline_by_connection(connection_id)

    # ---- 调用生命周期 ----

    async def request_and_wait(self, *, call: dict) -> ToolResult:
        """发起桌面工具调用并等待结果（幂等去重 + 先登记后投递 + 超时）。

        Args:
            call: 调用 dict（core/tools/desktop/plugin.py _delegate 组装），含
                tool_name/tool_version/runtime/arguments/arguments_hash/risk_level/
                permission_required/approval_authority。

        Returns:
            按 status 组装的 ToolResult（spec §4.3）。
        """
        tool_name = call.get("tool_name") or ""
        arguments_hash = call.get("arguments_hash") or ""
        key = self._dedup_key(tool_name, arguments_hash)
        lock = self._locks.setdefault(key, asyncio.Lock())

        async with lock:
            # ① 幂等去重（锁内完成）：pending 命中复用 callId，不二次投递
            existing_id = self._dedup.get(key)
            if existing_id is not None:
                existing = await self._repo.get(existing_id)
                future = self._pending.get(existing_id)
                if (existing is not None
                        and existing.get("status") in _ACTIVE_STATES
                        and future is not None):
                    # 复用原 future（锁内 await——同参并发串行化为一次投递）
                    result = await self._await_with_timeout(future)
                    if result is None:
                        # 超时：清理登记 + mark expired（幂等——首方可能已标）
                        self._pending.pop(existing_id, None)
                        self._dedup.pop(key, None)
                        await self._repo.mark(existing_id, status=STATUS_EXPIRED)
                        return self._timeout_result(tool_name)
                    self._pending.pop(existing_id, None)
                    self._dedup.pop(key, None)
                    return result
                # 已终态/offline（或 future 异常缺失）：不参与去重，清除后按新调用处理
                self._dedup.pop(key, None)

            # ② 锁内建 call + repo.upsert（pending, expires_at=now+timeout）+ _dedup 登记
            call_id = "call_" + uuid.uuid4().hex[:12]
            now = now_iso()
            doc = {
                **call,
                "call_id": call_id,
                "status": STATUS_PENDING,
                "target_connection": self._connection_id,
                "created_at": now,
                "expires_at": self._expires_iso(now, self._timeout),
                "updated_at": now,
                "result": None,
                "error": None,
            }
            await self._repo.upsert(doc)
            self._dedup[key] = call_id
            future: asyncio.Future = asyncio.get_running_loop().create_future()

            # ③ 先登记 future 再投递（顺序不可颠倒，防极速 result 竞态）
            self._pending[call_id] = future
            try:
                if self._deliver is None:
                    raise RuntimeError("deliver 回调未绑定")
                await self._deliver(self._build_request_frame(doc))
            except Exception as e:  # noqa: BLE001 — 投递失败属预期路径，兜底清理
                logger.warning("DesktopToolBridge 投递失败 (call=%s): %s", call_id, e)
                self._pending.pop(call_id, None)
                self._dedup.pop(key, None)
                await self._repo.mark(
                    call_id, status=STATUS_FAILED,
                    error={"code": "DESKTOP_UNAVAILABLE", "message": "桌面工具请求投递失败"})
                return ToolResult.fail(
                    name=tool_name, error="桌面客户端不可用", error_type=ErrorType.UNAVAILABLE)

        # ④ 锁外 await 结果（超时由 _await_with_timeout 返回 None 表达）
        result = await self._await_with_timeout(future)
        if result is None:
            # ⑤ 超时：锁内 mark expired + 清理登记
            async with lock:
                self._pending.pop(call_id, None)
                self._dedup.pop(key, None)
                await self._repo.mark(call_id, status=STATUS_EXPIRED)
            return self._timeout_result(tool_name)

        # ⑥ 结果回传（handle_status/cancel_call 已 resolve future + 更新 repo）
        self._pending.pop(call_id, None)
        self._dedup.pop(key, None)
        return result

    async def handle_status(self, status: dict) -> bool:
        """处理 Client→Server tool.status（校验 + 篡改拒绝 + 终态守卫 + resolve）。

        Args:
            status: wire 帧（camelCase）或已解析的 ToolStatusMessage。

        Returns:
            True 表示已处理（含终态晚到仅记录）；False 表示拒绝（call 未知或篡改）。
        """
        msg = status if isinstance(status, ToolStatusMessage) else self._parse_status(status)
        if msg is None:
            return False
        call_id = msg.callId
        doc = await self._repo.get(call_id)
        if doc is None:
            logger.warning("DesktopToolBridge tool.status 未知 callId=%s", call_id)
            return False

        # 篡改拒绝（BE-043）：wire toolName/toolVersion/runtime/paramHash 与登记一致
        if (msg.toolName != doc.get("tool_name")
                or msg.toolVersion != doc.get("tool_version")
                or msg.runtime != doc.get("runtime")
                or msg.paramHash != doc.get("arguments_hash")):
            return await self._reject_tampering(call_id, doc)

        # accepted/running → 仅更新 repo（不 resolve）
        if msg.status in (ToolStatusEnum.ACCEPTED, ToolStatusEnum.RUNNING):
            await self._repo.mark(call_id, status=msg.status.value)
            return True

        # 终态分支：result/error/rejected/cancelled → resolve
        repo_status, result_field, error_field, tool_result = self._map_terminal(msg)
        # mark：终态守卫由 repo 处理——终态晚到返回 False 但 result/error 字段已写
        # （不能把 mark False 当「什么都没发生」）；offline → 终态合法（重传闭环）
        await self._repo.mark(
            call_id, status=repo_status, result=result_field, error=error_field)
        # resolve（if not done 防 InvalidStateError；已终态晚到不重复 resolve）
        self._resolve_future(call_id, tool_result)
        return True

    async def cancel_call(self, call_id: str) -> None:
        """interrupt/回合取消：锁内 mark cancelled + future resolve(CANCELLED)。"""
        lock = self._locks.setdefault(call_id, asyncio.Lock())
        async with lock:
            doc = await self._repo.get(call_id)
            await self._repo.mark(call_id, status=STATUS_CANCELLED)
            self._resolve_future(call_id, ToolResult.fail(
                name=(doc or {}).get("tool_name") or "",
                error="客户端取消了该调用",
                error_type=ErrorType.CANCELLED,
            ))
            self._pending.pop(call_id, None)

    async def get_call(self, call_id: str) -> dict | None:
        """按 callId 查询调用文档（BE-043 状态查询；不存在返回 None）。"""
        return await self._repo.get(call_id)

    async def pending_calls(self) -> list[dict]:
        """重连重放/审计用：pending/offline 且未超时的调用（offline 为「可重传结果」准终态）。

        注：接口签名定为 async（repo 为 async 读）；Task 6/8 调用时需 await。
        """
        now = now_iso()
        result: list[dict] = []
        for call_id in list(self._pending.keys()):
            doc = await self._repo.get(call_id)
            if doc is None:
                continue
            if (doc.get("status") in (STATUS_PENDING, STATUS_OFFLINE)
                    and (doc.get("expires_at") or "") > now):
                result.append(doc)
        return result

    async def save_all(self) -> None:
        """no-op 预留：repo 即时 upsert、无内存脏集可 flush（review 修正）。"""
        return None

    # ---- 内部工具 ----

    @staticmethod
    def _dedup_key(tool_name: Any, arguments_hash: Any) -> tuple[str, str]:
        """幂等去重索引键：tool_name + arguments_hash（None 归一化为空串）。"""
        return (tool_name or "", arguments_hash or "")

    @staticmethod
    def _expires_iso(start_iso: str, timeout: float) -> str:
        """由起始 ISO 时间 + 超时秒数计算 expires_at（UTC ISO）。"""
        start = datetime.fromisoformat(start_iso)
        return (start + timedelta(seconds=timeout)).isoformat()

    def _build_request_frame(self, doc: dict) -> dict:
        """组装 tool.request 投递帧（契约模型 → wire_dict，Task 1 契约单源）。"""
        msg = ToolRequestMessage(
            callId=doc["call_id"],
            toolName=doc["tool_name"],
            toolVersion=doc["tool_version"],
            runtime=doc["runtime"],
            arguments=doc.get("arguments") or {},
            reason=doc.get("reason"),
            riskLevel=doc.get("risk_level") or "safe",
            permissionRequired=bool(doc.get("permission_required")),
            approvalAuthority=doc.get("approval_authority") or "client",
            paramHash=doc.get("arguments_hash") or "",
            expiresAt=doc.get("expires_at") or "",
            requestId=doc.get("request_id"),
        )
        return msg.wire_dict()

    @staticmethod
    def _parse_status(status: dict) -> ToolStatusMessage | None:
        """解析 tool.status wire 帧为契约模型；解析失败返回 None。"""
        try:
            return ToolStatusMessage(**status)
        except Exception as e:  # noqa: BLE001 — 帧格式非法属预期拒绝路径
            logger.warning("DesktopToolBridge tool.status 解析失败: %s", e)
            return None

    async def _await_with_timeout(self, future: asyncio.Future) -> ToolResult | None:
        """await 结果；超时返回 None。

        用 asyncio.shield 包住共享 future——wait_for 超时不取消 future：dedup 复用方与
        首方共同 await 同一 future，取消会让复用方收到 CancelledError。
        """
        try:
            return await asyncio.wait_for(asyncio.shield(future), self._timeout)
        except asyncio.TimeoutError:
            return None

    def _resolve_future(self, call_id: str, tool_result: ToolResult) -> None:
        """resolve future（if not done 防护——future 只 resolve 一次，防 InvalidStateError）。"""
        future = self._pending.get(call_id)
        if future is not None and not future.done():
            future.set_result(tool_result)

    @staticmethod
    def _timeout_result(tool_name: str) -> ToolResult:
        return ToolResult.fail(
            name=tool_name, error="桌面工具调用超时", error_type=ErrorType.TIMEOUT)

    async def _reject_tampering(self, call_id: str, doc: dict) -> bool:
        """篡改拒绝（BE-043）：锁内 mark failed + 立即 resolve 明确失败态 + return False。

        立即 resolve 而非挂超时——Electron 副作用已发生但结果被拒，Agent 及时收到拒绝。
        """
        tool_name = doc.get("tool_name") or ""
        lock = self._locks.setdefault(call_id, asyncio.Lock())
        async with lock:
            await self._repo.mark(
                call_id, status=STATUS_FAILED,
                error={"code": _TAMPER_CODE, "message": "参数与请求不一致，已拒绝"})
            self._resolve_future(call_id, ToolResult.fail(
                name=tool_name,
                error="参数与请求不一致，已拒绝",
                error_type=ErrorType.TOOL_ERROR,
            ))
            logger.warning("DesktopToolBridge 篡改拒绝 (call=%s)", call_id)
            return False

    @staticmethod
    def _map_terminal(msg: ToolStatusMessage) -> tuple[str, dict | None, dict | None, ToolResult]:
        """终态 wire status → (repo 枚举, result 字段, error 字段, ToolResult)。spec §4.3。"""
        tool_name = msg.toolName
        if msg.status == ToolStatusEnum.RESULT:
            result_field = {
                "output": msg.output,
                "attachments": [
                    {"url": a.url, "mime_type": a.mimeType, "label": a.label}
                    for a in (msg.attachments or [])
                ],
                "execution_summary": msg.executionSummary,
            }
            images, files = DesktopToolBridge._split_attachments(msg.attachments)
            content = msg.output or ""
            llm_summary = msg.executionSummary or content
            tool_result = ToolResult.ok(
                name=tool_name, content=content, duration_ms=0.0,
                llm_summary=llm_summary, images=images, files=files)
            return STATUS_SUCCEEDED, result_field, None, tool_result
        if msg.status == ToolStatusEnum.ERROR:
            error_field = {
                "code": (msg.error or {}).get("code"),
                "message": (msg.error or {}).get("message"),
            }
            message = (msg.error or {}).get("message") or "桌面工具执行出错"
            tool_result = ToolResult.fail(
                name=tool_name, error=message, error_type=ErrorType.TOOL_ERROR)
            return STATUS_FAILED, None, error_field, tool_result
        if msg.status == ToolStatusEnum.REJECTED:
            tool_result = ToolResult.fail(
                name=tool_name, error="用户拒绝了该工具调用", error_type=ErrorType.CANCELLED)
            return STATUS_REJECTED, None, None, tool_result
        # CANCELLED
        tool_result = ToolResult.fail(
            name=tool_name, error="客户端取消了该调用", error_type=ErrorType.CANCELLED)
        return STATUS_CANCELLED, None, None, tool_result

    @staticmethod
    def _split_attachments(attachments: list | None) -> tuple[list[MediaItem], list[MediaItem]]:
        """附件 → images（image/*，聊天 ImagePart 消费）与 files（其余）。"""
        images: list[MediaItem] = []
        files: list[MediaItem] = []
        for att in attachments or []:
            item = MediaItem(url=att.url, mime_type=att.mimeType, label=att.label)
            if (att.mimeType or "").startswith("image/"):
                images.append(item)
            else:
                files.append(item)
        return images, files
