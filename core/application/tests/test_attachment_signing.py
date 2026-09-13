# 测试文件：core/application/tests/test_attachment_signing.py
"""附件签名用例层单测（ARCH-INC-P1-02）。

**这一层要买的东西就是本文件**：签名规则（TTL / 时间窗口 / HMAC / fail-open 与
fail-closed 判定）原本一半在 `upload_routes._sign_url`、一半交织在
`attachment_routes._download_handler` 里，只能**起 aiohttp 服务**才测得到——而它是
安全边界。现在全部可脱离传输层、注入时钟、精确到秒地断言。

不重复 HTTP 层的覆盖（状态码映射仍由 `core/input/tests/test_attachment_download.py`
端到端保证）；这里测的是**规则本身**与两类此前无法直接测的边界。
"""

import hashlib
import hmac

import pytest

from core.application.attachment_signing import (
    CODE_INVALID_SIGNATURE,
    CODE_SIGNATURE_DISABLED,
    CODE_SIGNATURE_EXPIRED,
    SIGNATURE_TTL_SECONDS,
    AttachmentSigningService,
    service_for_agent,
)

pytestmark = pytest.mark.unit

SECRET = "test-secret-not-a-credential"


class _Clock:
    """可控时钟：让时间窗口断言精确到秒，而不是靠 sleep。"""

    def __init__(self, now: float = 1_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def _svc(*, access_token: str = "token", sig_secret: str = SECRET,
         clock=None) -> AttachmentSigningService:
    return AttachmentSigningService(access_token, sig_secret,
                                    clock=clock or _Clock())


# ═══════════════════════════════════════════════════════════════════
# 运行态：三种配置组合
# ═══════════════════════════════════════════════════════════════════

def test_both_configured_is_normal_mode():
    s = _svc()
    assert s.disabled is False and s.verify_enabled is True


def test_token_without_secret_is_fail_closed():
    """配了 token 却没配密钥 → 必须拒绝，不能静默降级成"不校验"。"""
    s = _svc(sig_secret="")
    assert s.disabled is True and s.verify_enabled is False
    assert s.disabled_outcome().code == CODE_SIGNATURE_DISABLED


def test_no_token_is_fail_open():
    """未配 token → 下载不校验（本地开发语义）。"""
    s = _svc(access_token="")
    assert s.disabled is False and s.verify_enabled is False
    assert s.verify("att_1", "", "").ok is True


# ═══════════════════════════════════════════════════════════════════
# 签发 ↔ 验证往返
# ═══════════════════════════════════════════════════════════════════

def test_sign_then_verify_roundtrip():
    s = _svc()
    url, exp = s.sign("att_1")
    assert url.startswith("/api/attachments/att_1?sig=")
    sig = url.split("sig=")[1].split("&")[0]
    assert s.verify("att_1", sig, str(exp)).ok is True


def test_exp_is_exactly_ttl_ahead():
    """签发与验证共用同一 TTL 常量——错配会表现为"签出来立刻过期"。"""
    clock = _Clock(1_000_000.0)
    s = _svc(clock=clock)
    _, exp = s.sign("att_1")
    assert exp == 1_000_000 + SIGNATURE_TTL_SECONDS


def test_sign_requires_secret():
    with pytest.raises(ValueError):
        _svc(sig_secret="").sign("att_1")


# ═══════════════════════════════════════════════════════════════════
# 验证：三类失败
# ═══════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("sig", ["", "NOT-HEX", "../../etc/passwd", "a" * 129])
def test_malformed_sig_rejected(sig):
    """形态先判：非法 hex / 超长（compare_digest 的 DoS 防护）一律 INVALID_SIGNATURE。"""
    out = _svc().verify("att_1", sig, "9999999999")
    assert out.ok is False and out.code == CODE_INVALID_SIGNATURE


def test_expired_when_past():
    clock = _Clock()
    s = _svc(clock=clock)
    _, exp = s.sign("att_1")
    clock.now = exp + 1                      # 过期一秒
    out = s.verify("att_1", s._compute("att_1", exp), str(exp))   # noqa: SLF001
    assert out.code == CODE_SIGNATURE_EXPIRED


def test_expired_when_too_far_in_future():
    """上界同样必要：否则攻击者用一个远期 exp 造出长期有效签名。"""
    clock = _Clock()
    s = _svc(clock=clock)
    far = int(clock.now) + SIGNATURE_TTL_SECONDS + 1
    out = s.verify("att_1", s._compute("att_1", far), str(far))   # noqa: SLF001
    assert out.code == CODE_SIGNATURE_EXPIRED


def test_expiry_window_is_inclusive_at_both_ends():
    """边界是闭区间：exp == now 与 exp == now+TTL 都必须通过。"""
    clock = _Clock()
    s = _svc(clock=clock)
    for exp in (int(clock.now), int(clock.now) + SIGNATURE_TTL_SECONDS):
        sig = s._compute("att_1", exp)       # noqa: SLF001
        assert s.verify("att_1", sig, str(exp)).ok is True, exp


def test_non_integer_exp_rejected_as_expired():
    out = _svc().verify("att_1", "a" * 64, "not-a-number")
    assert out.code == CODE_SIGNATURE_EXPIRED


def test_signature_bound_to_attachment_id():
    """签名绑定具体附件：换 ID 即失效（否则一个签名能下全部附件）。"""
    s = _svc()
    _, exp = s.sign("att_1")
    sig = s._compute("att_1", exp)           # noqa: SLF001
    assert s.verify("att_1", sig, str(exp)).ok is True
    assert s.verify("att_2", sig, str(exp)).code == CODE_INVALID_SIGNATURE


def test_tampered_sig_rejected():
    s = _svc()
    _, exp = s.sign("att_1")
    good = s._compute("att_1", exp)          # noqa: SLF001
    bad = ("0" if good[0] != "0" else "1") + good[1:]
    assert s.verify("att_1", bad, str(exp)).code == CODE_INVALID_SIGNATURE


# ═══════════════════════════════════════════════════════════════════
# 与手算 HMAC 对齐（防止"自己跟自己一致"的空转测试）
# ═══════════════════════════════════════════════════════════════════

def test_matches_independent_hmac_computation():
    """用**独立手算**的 HMAC 验证签名值——只做 sign→verify 往返的话，算法写错
    也会自洽通过。"""
    clock = _Clock(1_700_000_000.0)
    s = _svc(clock=clock)
    url, exp = s.sign("att_42")
    sig = url.split("sig=")[1].split("&")[0]
    expected = hmac.new(SECRET.encode(), f"att_42:{exp}".encode(),
                        hashlib.sha256).hexdigest()
    assert sig == expected
    assert exp == 1_700_000_000 + SIGNATURE_TTL_SECONDS


# ═══════════════════════════════════════════════════════════════════
# 装配
# ═══════════════════════════════════════════════════════════════════

def test_service_for_agent_reads_server_config():
    class _Server:
        access_token = "tok"
        attachment_sig_secret = "sec"

    class _Agent:
        config = type("C", (), {"server": _Server()})()

    s = service_for_agent(_Agent())
    assert s.verify_enabled is True


def test_service_for_agent_tolerates_missing_config():
    """测试替身/半初始化 agent 不得让探针炸掉——缺配置按 fail-open 处理。"""
    s = service_for_agent(object())
    assert s.disabled is False and s.verify_enabled is False
