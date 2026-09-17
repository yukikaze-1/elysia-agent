"""应用级配置 dataclass — 经 Module.config.ConfigLoader 加载。"""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from Module.config import ConfigField, Range


@dataclass
class AgentConfig:
    character: str = ConfigField(default="character/elysia/config.yaml")
    user_name: str = ConfigField(default="妖梦")
    max_pending_messages: int = ConfigField(default=32)


@dataclass
class LLMConfig:
    model: str = ConfigField(default="deepseek/deepseek-chat")


@dataclass
class MemoryConfig:
    session_max_messages: int = ConfigField(default=20)
    ltm_max_entries: int = ConfigField(default=10000)
    ltm_max_age_days: int = ConfigField(default=90)
    ltm_enable_eviction: bool = ConfigField(default=True)
    ltm_embed_timeout: float = ConfigField(default=10.0)


@dataclass
class ServerConfig:
    # 默认绑定回环地址；远程暴露用 env SERVER_HOST=0.0.0.0 覆盖（开发期远程测试，
    # scripts/start.sh 无需改 config.yaml 即可远程启动，配 ALLOW_INSECURE_REMOTE
    # 或 AGENT_ACCESS_TOKEN 通过 bind 安全检查）。
    host: str = ConfigField(default="127.0.0.1", env="SERVER_HOST")
    port: int = ConfigField(default=8765, constraints=[Range(1, 65535)])
    auth_token: str = ConfigField(default="", env="SERVER_AUTH_TOKEN", secret=True)
    # 产品访问 token（批次 1）：护 /ws + 业务 /api/* + /uploads/*；与 SERVER_AUTH_TOKEN
    # （admin/debug）权限域分离。secret=True 防止写入 YAML/git。
    access_token: str = ConfigField(default="", env="AGENT_ACCESS_TOKEN", secret=True)
    # 停用 v1 浏览器聊天（2026-08-11）：serve_chat 默认关 → / 404；/debug 仪表盘
    # 独立保留（拆耦合）。SERVE_CHAT=1 可逆恢复浏览器聊天。
    serve_chat: bool = ConfigField(default=False, env="SERVE_CHAT")
    max_ws_connections: int = ConfigField(default=64)
    # D6b：公开入口限流（类别 → rpm，0 关闭；单进程语义，不假装全局限流）。
    # 覆盖 plan Task D6b：连接/消息（generation）/provider test/upload/search/工具。
    rate_limit_ws_per_min: int = ConfigField(default=120, env="RATE_LIMIT_WS_PER_MIN")
    rate_limit_messages_per_min: int = ConfigField(default=120, env="RATE_LIMIT_MESSAGES_PER_MIN")
    rate_limit_provider_test_per_min: int = ConfigField(default=30, env="RATE_LIMIT_PROVIDER_TEST_PER_MIN")
    rate_limit_upload_per_min: int = ConfigField(default=60, env="RATE_LIMIT_UPLOAD_PER_MIN")
    rate_limit_search_per_min: int = ConfigField(default=60, env="RATE_LIMIT_SEARCH_PER_MIN")
    rate_limit_tools_per_min: int = ConfigField(default=120, env="RATE_LIMIT_TOOLS_PER_MIN")
    # 业务请求体上限（app 级 client_max_size + payload 中间件 Content-Length 预检）。
    # 批次 7 提到 64MB：既有 8MB 会把 10MB 图片 / 通用上传聚合体截住（I3）。
    max_request_bytes: int = ConfigField(default=64 * 1024 * 1024)
    image_max_mb: int = ConfigField(default=10)
    image_allowed_types: list[str] = ConfigField(default_factory=lambda: ["image/png", "image/jpeg", "image/gif", "image/webp"])
    # 附件签名密钥（批次 7，spec §3.4）：HMAC 签名 /api/attachments/{id} 下载 URL。
    # 空 = 未配置（fail-closed：AGENT_ACCESS_TOKEN 配置而 SIG 未配时省略 signedUrl）。
    attachment_sig_secret: str = ConfigField(default="", env="ATTACHMENT_SIG_SECRET", secret=True)

    @property
    def rate_limits(self) -> dict[str, int]:
        """D6b 限流规则（类别 → rpm；0 关闭该类别）。create_app 消费。"""
        return {
            "ws": self.rate_limit_ws_per_min,
            "messages": self.rate_limit_messages_per_min,
            "provider_test": self.rate_limit_provider_test_per_min,
            "upload": self.rate_limit_upload_per_min,
            "search": self.rate_limit_search_per_min,
            "tools": self.rate_limit_tools_per_min,
        }


@dataclass
class TTSConfig:
    enabled: bool = ConfigField(default=False)
    model: str = ConfigField(default="gpt-sovits/default")
    rate_limit_rpm: int = ConfigField(
        default=20,
        env="TTS_RATE_LIMIT_RPM",
        help="MiniMax 每分钟最大合成请求数（滑动窗口），0 关闭限速",
    )


@dataclass
class STTConfig:
    enabled: bool = ConfigField(default=False)
    model: str = ConfigField(default="sensevoice/sense-voice")


@dataclass
class WakeWordConfig:
    enabled: bool = ConfigField(default=False)


@dataclass
class Live2DConfig:
    """Live2D 看板娘配置"""
    static_image: str = ConfigField(default="", env="LIVE2D_STATIC_IMAGE")


@dataclass
class VisionConfig:
    """视觉模型配置 — 供 describe_image 工具使用。"""
    model: str = ConfigField(default="mimo/mimo-v2.5")


@dataclass
class DatabaseConfig:
    """数据库连接配置 — MongoDB + Milvus。"""
    mongodb_uri: str = ConfigField(
        default="mongodb://localhost:27017",
        env="MONGODB_URI",
        help="MongoDB 连接串（如 mongodb://localhost:27017）",
    )
    milvus_uri: str = ConfigField(
        default="http://localhost:19530",
        env="MILVUS_URI",
        help="Milvus 服务端点（如 http://localhost:19530）",
    )
    milvus_token: str = ConfigField(
        default="",
        secret=True,
        env="MILVUS_TOKEN",
        help="Milvus 认证令牌",
    )
    db_name: str = ConfigField(default="default", help="MongoDB 数据库名")


@dataclass
class AppConfig:
    agent: AgentConfig = ConfigField(default_factory=AgentConfig)
    llm: LLMConfig = ConfigField(default_factory=LLMConfig)
    memory: MemoryConfig = ConfigField(default_factory=MemoryConfig)
    server: ServerConfig = ConfigField(default_factory=ServerConfig)
    tts: TTSConfig = ConfigField(default_factory=TTSConfig)
    stt: STTConfig = ConfigField(default_factory=STTConfig)
    wake_word: WakeWordConfig = ConfigField(default_factory=WakeWordConfig)
    live2d: Live2DConfig = ConfigField(default_factory=Live2DConfig)
    vision: VisionConfig = ConfigField(default_factory=VisionConfig)
    database: DatabaseConfig = ConfigField(default_factory=DatabaseConfig)


def apply_config_overrides(config: AppConfig, overrides: Mapping[str, Any]) -> None:
    """把**显式**覆盖写进一个**已加载**的 config 对象（点路径 → 属性）。

    F4（Batch 1.6）：这是 CLI 显式覆盖的**唯一施加点**的实现。缺陷背景：
    `app.py` 把 `--host/--port/--tts/--stt/--character` 写进 config 后交给
    `run_agent_service`，而后者**每一轮开头**都
    `ConfigLoader(AppConfig, yaml_path=…).load()` 整个换掉 config ⇒ 5 个 flag 在
    server 模式**全部静默失效**（实测 `--port 9999` 仍绑 8765），而 `--cli`/`--check`
    两条不经 `run_agent_service` 的路径反而生效。修法是"每轮重载之后重新施加覆盖"，
    而不是"首轮不重载"——后者会破坏"/restart 重读 YAML"这条既有契约
    （`core/agent/tests/test_restart_rebuild.py` 钉着它）。

    **优先级（本批裁定并测试）**：`显式 CLI 覆盖 > env > YAML > dataclass 默认`。
    前两层之间的顺序沿用的是**现状语义**：`app.py` 一直是在 `ConfigLoader.load()`
    （内部按 defaults → YAML → env 覆盖）**之后**写这几个字段的，`--cli` / `--check`
    模式下实测也是 CLI 赢 env。本函数把同一顺序搬到了重载之后，顺序没有改变。

    Args:
        config: 已加载的 config（`AppConfig` 或同构对象——测试里的 `SimpleNamespace`
            同样可用，本函数只做属性寻址与赋值）。
        overrides: `{点路径: 值}`，如 `{"server.port": 9999}`。

    Raises:
        AttributeError: 路径上的段不存在（拼错的字段名不该静默失效——那正是本缺陷的
            形态：覆盖"应用了"却没人读）。
    """
    for path, value in overrides.items():
        target: Any = config
        *parents, leaf = path.split(".")
        for name in parents:
            if not hasattr(target, name):
                raise AttributeError(f"配置覆盖路径 {path!r} 不存在：缺少 {name!r}")
            target = getattr(target, name)
        if not hasattr(target, leaf):
            raise AttributeError(f"配置覆盖路径 {path!r} 不存在：缺少 {leaf!r}")
        setattr(target, leaf, value)
