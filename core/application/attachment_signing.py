"""附件签名 URL 的签发与验证（应用用例层，ARCH-INC-P1-02）。

**为什么把它从 route 文件里搬出来**：签发逻辑原本在 `core/input/upload_routes.py::_sign_url`，
验证逻辑在 `core/input/attachment_routes.py::_download_handler` 里与 HTTP 关注点交织
（取 query、判 access_token、构造错误响应）。三者（TTL 常量、hex 正则、时间窗口、
HMAC 比较、fail-open/fail-closed 策略）分散在两个文件里，只能靠**起 aiohttp 服务**
才能测——而这是安全边界，最该能脱离传输层单测。

本模块只做规则，不做传输：不 import aiohttp、不构造响应、不认识 HTTP 状态码。
错误码是**契约层的码**（`frontend_contract/errors.py` 已登记），由调用方映射到状态码。

**校验序（spec §3.3，D3 重排——签名前置，防存在性枚举）**：
① sig 形态 → ③ exp 窗口 → ④ HMAC → （② 记录存在性由调用方查库，不在这里）
"""

from __future__ import annotations

import hashlib
import hmac
import re
import time
from collections.abc import Callable
from dataclasses import dataclass

#: 签名有效期（秒）。exp 必须落在 [now, now + TTL]。
SIGNATURE_TTL_SECONDS = 86400

#: sig 形态：十六进制串（长度放宽到 128，兼容未来换算法）。
_SIG_RE = re.compile(r"^[0-9a-f]{1,128}$")

#: 错误码（契约层已登记；调用方负责映射 HTTP/WS 状态码）。
CODE_INVALID_SIGNATURE = "INVALID_SIGNATURE"
CODE_SIGNATURE_EXPIRED = "SIGNATURE_EXPIRED"
CODE_SIGNATURE_DISABLED = "ATTACHMENT_SIGNATURE_DISABLED"


@dataclass(frozen=True)
class VerifyOutcome:
    """验证结果。`ok=False` 时 `code`/`message` 可直接进错误信封。"""

    ok: bool
    code: str = ""
    message: str = ""

    def __bool__(self) -> bool:
        return self.ok


_OK = VerifyOutcome(ok=True)


class AttachmentSigningService:
    """HMAC 短签的签发/验证单源。

    三种运行态由两个配置项决定（策略原样保留，未改语义）：

    | access_token | sig_secret | 行为 |
    |---|---|---|
    | 配 | 配 | 正常签发 + 验证 |
    | 配 | 空 | **fail-closed**：下载直接拒绝（`ATTACHMENT_SIGNATURE_DISABLED`）|
    | 空 | 任意 | **fail-open**：不校验签名，仅查记录存在性（本地开发）|
    """

    def __init__(self, access_token: str = "", sig_secret: str = "",
                 *, ttl_seconds: int = SIGNATURE_TTL_SECONDS,
                 clock: Callable[[], float] = time.time) -> None:
        """初始化。

        Args:
            access_token: 产品 token（决定是否启用签名校验）。
            sig_secret: HMAC 密钥。
            ttl_seconds: 签名有效期；验证窗口同样用它（签发/验证必须同一常量，
                否则会出现"签出来立刻过期"这类只在生产暴露的错配）。
            clock: 取当前时间（秒），测试注入用。
        """
        self._access_token = access_token or ""
        self._sig_secret = sig_secret or ""
        self._ttl = int(ttl_seconds)
        self._clock = clock

    # ---- 运行态 ----

    @property
    def disabled(self) -> bool:
        """配了 token 却没配密钥 → 必须拒绝（不能静默降级成不校验）。"""
        return bool(self._access_token) and not self._sig_secret

    @property
    def verify_enabled(self) -> bool:
        """是否真的校验签名（fail-open 时为 False）。"""
        return bool(self._access_token) and bool(self._sig_secret)

    # ---- 签发 ----

    def sign(self, attachment_id: str) -> tuple[str, int]:
        """签发下载 URL。

        Args:
            attachment_id: 附件 ID。

        Returns:
            (url, expires_unix)。url 为站内相对路径。
        """
        if not self._sig_secret:
            raise ValueError("无法签发：未配置 ATTACHMENT_SIG_SECRET")
        expires_unix = int(self._clock()) + self._ttl
        sig = self._compute(attachment_id, expires_unix)
        return (f"/api/attachments/{attachment_id}?sig={sig}&exp={expires_unix}",
                expires_unix)

    def _compute(self, attachment_id: str, expires_unix: int) -> str:
        return hmac.new(self._sig_secret.encode(),
                        f"{attachment_id}:{expires_unix}".encode(),
                        hashlib.sha256).hexdigest()

    # ---- 验证 ----

    def verify(self, attachment_id: str, sig: str, exp_raw: str) -> VerifyOutcome:
        """校验签名（顺序：形态 → 时间窗口 → HMAC）。

        Args:
            attachment_id: 路径里的附件 ID。
            sig: query 里的签名。
            exp_raw: query 里的过期时刻（原样字符串，本方法负责解析）。

        Returns:
            VerifyOutcome。fail-open（未配 token）时恒为 ok —— 调用方仍须做
            记录存在性校验。
        """
        if not self.verify_enabled:
            return _OK
        if not _SIG_RE.fullmatch(sig or ""):
            return VerifyOutcome(False, CODE_INVALID_SIGNATURE, "缺少或非法签名")
        try:
            exp = int(exp_raw or "")
        except ValueError:
            exp = -1
        now = int(self._clock())
        if exp < now or exp > now + self._ttl:
            return VerifyOutcome(False, CODE_SIGNATURE_EXPIRED, "签名已过期")
        expected = self._compute(attachment_id, exp)
        # compare_digest 防时序侧信道
        if not hmac.compare_digest(sig, expected):
            return VerifyOutcome(False, CODE_INVALID_SIGNATURE, "签名校验失败")
        return _OK

    def disabled_outcome(self) -> VerifyOutcome:
        """fail-closed 早退结果（配了 token 却没配密钥）。"""
        return VerifyOutcome(False, CODE_SIGNATURE_DISABLED, "附件签名未配置")


def service_for_agent(agent: object) -> AttachmentSigningService:
    """按 Agent 的服务端配置构造服务（组合根之外的唯一装配点）。

    Args:
        agent: 具备 `config.server` 的 Agent（或测试替身）。
    """
    cfg = getattr(getattr(agent, "config", None), "server", None)
    return AttachmentSigningService(
        access_token=getattr(cfg, "access_token", "") or "",
        sig_secret=getattr(cfg, "attachment_sig_secret", "") or "",
    )
