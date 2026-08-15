"""HTTP 服务绑定安全守卫 — 回环判定 + 非安全远程绑定拦截/告警。

从 core.input.server 迁出（CORE-MOD-P1-01）：绑定安全是通用运行时策略，
不应只归传输层所有；core.input.server 与 AgentServer 共用本模块。
"""

import logging
import os

logger = logging.getLogger(__name__)


def is_loopback_host(host: str) -> bool:
    """host 是否为回环地址（127.0.0.1 / localhost / ::1）。

    产品/dev 模式判定依据：回环 → 本地 dev（幂等可内存降级）；非回环 → 产品
    （幂等 Mongo 离线 fail-closed）。与 D6a bind 守卫同一判定。
    """
    return host in ("127.0.0.1", "localhost", "::1")


def assert_secure_bind(host: str, access_token: str, auth_token: str = "") -> None:
    """D6a：非回环绑定 + 无产品认证 token + 未显式 ALLOW_INSECURE_REMOTE → 拒绝启动。

    fail-closed：默认配置无法匿名监听非回环地址。仅以下之一放行：
    - host 是 loopback（127.0.0.1 / localhost / ::1）；
    - 已配置 access_token（AGENT_ACCESS_TOKEN）——业务 /api、/ws、/uploads 的
      唯一鉴权；SERVER_AUTH_TOKEN（管理面）**不**单独放行远程绑定（review 复核
      #1：仅配管理 token 时业务端点仍匿名可访问）；
    - 显式设 ALLOW_INSECURE_REMOTE=1（仅本地开发开关，日志/health 明确 insecure）。

    Raises:
        RuntimeError: fail-closed 条件成立。
    """
    if os.getenv("ALLOW_INSECURE_REMOTE", "") in ("1", "true", "True", "yes"):
        return
    if is_loopback_host(host):
        return
    if access_token:
        return
    raise RuntimeError(
        f"拒绝启动：服务绑定非回环地址 {host!r} 且未配置产品认证 token。\n"
        "  - 远程部署：设置 AGENT_ACCESS_TOKEN（护 /ws /api/* /uploads/*，唯一放行凭据），"
        "建议同时配 SERVER_AUTH_TOKEN（管理面），使用 HTTPS/WSS；\n"
        "  - 本地开发：改用 127.0.0.1 绑定，或显式设置 ALLOW_INSECURE_REMOTE=1 并知悉风险。"
    )


def warn_if_unsecured(host: str, access_token: str, auth_token: str = "") -> None:
    """非回环绑定 + 未配置认证 token → 显著告警；仅配置一侧 → 提示同时配置。

    本函数只告警、不拒绝启动——拒绝分支由 `assert_secure_bind` 承担（fail-closed）：
    默认配置 host=127.0.0.1 走回环放行，非回环绑定在无 AGENT_ACCESS_TOKEN 且未显式
    设 ALLOW_INSECURE_REMOTE=1 时启动即被拒；SERVER_AUTH_TOKEN 单独配置**不**放行
    远程绑定（管理面 token 不护业务端点，review 复核 #1）。故本函数满足 PRD「绑定
    非回环地址且未启用认证时应拒绝启动或输出显著阻塞告警」的"或"分支——仅对未被
    `assert_secure_bind` 拒绝（loopback / AGENT_ACCESS_TOKEN / ALLOW_INSECURE_REMOTE=1
    任一放行）的非回环绑定输出显著告警，仅配置一侧 → 提示同时配置。

    Args:
        host: 服务绑定地址。
        access_token: 产品 access token（AGENT_ACCESS_TOKEN），空 = 未启用认证。
        auth_token: 管理面 token（SERVER_AUTH_TOKEN），空 = 未启用管理认证。
    """
    if is_loopback_host(host):
        return
    if auth_token and access_token:
        return
    if access_token or auth_token:
        logger.warning(
            "\n"
            "==================================================================\n"
            "  安全建议：服务绑定非回环地址 %r 且仅配置了部分认证。\n"
            "  远程部署建议同时配置 AGENT_ACCESS_TOKEN（产品 API /ws /uploads）与\n"
            "  SERVER_AUTH_TOKEN（管理面 /shutdown /debug/api），二者缺一会导致\n"
            "  部分端点匿名可访问。\n"
            "==================================================================",
            host,
        )
        return
    logger.error(
        "\n"
        "==================================================================\n"
        "  安全警告：服务绑定非回环地址 %r 且未配置产品认证。\n"
        "  当前所有 /ws、/api/*、/uploads/* 匿名可访问，仅适用于本机开发。\n"
        "  远程部署请设置环境变量 AGENT_ACCESS_TOKEN，并使用 HTTPS/WSS。\n"
        "==================================================================",
        host,
    )
