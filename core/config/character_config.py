"""Character configuration — loaded from YAML, defines personality/voice/wake-words."""

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

import yaml
from paths import get_project_root


@dataclass
class EmotionConfig:
    thresholds: dict[str, float] = field(default_factory=dict)
    idle_expressions: list[str] = field(default_factory=lambda: ["neutral"])


@dataclass
class ProactiveConfig:
    enabled: bool = False
    interval: float = 300.0
    triggers: list[dict] = field(default_factory=list)


@dataclass
class CharacterConfig:
    name: str = "Agent"
    voice: str = "default"
    user_pronoun: str = "她"
    base_personality_file: str = ""     # 新增：L1 Core 文件路径
    personality: dict[str, object] = field(default_factory=dict)
    emotion: EmotionConfig = field(default_factory=EmotionConfig)
    wake_words: list[str] = field(default_factory=list)
    proactive: ProactiveConfig = field(default_factory=ProactiveConfig)
    base_personality: str = ""          # 新增：L1 Core 文本内容（运行时加载）

    @property
    def traits(self) -> list[str]:
        """性格特质列表。"""
        return cast(list[str], self.personality.get("traits", []))

    @property
    def speaking_style(self) -> str:
        """说话风格描述。"""
        return str(self.personality.get("speaking_style", ""))

    @classmethod
    def from_yaml(cls, path: str | Path) -> "CharacterConfig":
        """从 YAML 文件加载角色配置。

        Args:
            path: YAML 配置文件路径。

        Returns:
            解析后的 CharacterConfig 实例。

        Raises:
            FileNotFoundError: 配置文件不存在。
            PermissionError: 配置文件无读取权限。
        """
        path = Path(path)
        if not path.exists():
            raise FileNotFoundError(f"Character config not found: {path}")
        try:
            raw_text = path.read_text("utf-8")
        except PermissionError:
            raise PermissionError(f"Character config 无读取权限: {path}") from None
        raw = yaml.safe_load(raw_text) or {}
        proactive_raw = raw.get("proactive", {})
        emotion_raw = raw.get("emotion", {})

        # 加载 L1 Core 文件
        base_file = raw.get("base_personality_file", "")
        base_personality = cls._load_base_personality(base_file, raw.get("name", "Agent"), path)

        return cls(
            name=raw.get("name", "Agent"),
            voice=raw.get("voice", "default"),
            user_pronoun=raw.get("user_pronoun", "她"),
            base_personality_file=base_file,
            base_personality=base_personality,
            personality=raw.get("personality", {}),
            emotion=EmotionConfig(
                thresholds=emotion_raw.get("thresholds", {}),
                idle_expressions=emotion_raw.get("idle_expressions", ["neutral"]),
            ),
            wake_words=raw.get("wake_words", []),
            proactive=ProactiveConfig(
                enabled=proactive_raw.get("enabled", False),
                interval=float(proactive_raw.get("interval", 300)),
                triggers=proactive_raw.get("triggers", []),
            ),
        )

    @staticmethod
    def _load_base_personality(base_file: str, name: str, yaml_path: Path) -> str:
        """加载 L1 Core personality 文本。

        路径解析策略：
        1. 相对于 character YAML 所在目录
        2. 回退项目根目录
        3. 未指定时推导 character/{name}_personality.md
        4. 文件不存在 → WARN，返回空串

        Args:
            base_file: YAML 中指定的 base_personality_file 路径，可为空字符串。
            name: 角色名称，用于自动推导 personality 文件路径。
            yaml_path: YAML 配置文件所在路径，用作相对路径解析基准。

        Returns:
            加载的 personality 文本内容；文件不存在时返回空字符串。
        """
        logger = logging.getLogger(__name__)

        candidates = []
        if base_file:
            candidates.append(yaml_path.parent / base_file)          # Step 1: YAML 目录
            candidates.append(get_project_root() / base_file)          # Step 2: 项目根目录

        # Step 3: 自动推导 character/{name}_personality.md
        candidates.append(yaml_path.parent / f"{name}_personality.md")

        for p in candidates:
            if p.exists():
                return p.read_text("utf-8").strip()

        tried = [str(c) for c in candidates]
        if base_file:
            # 显式指定 base_personality_file（正式部署声明）但文件不存在 →
            # 配置错误 fail-fast（O6）。以空 L1 运行会让 C1 无核心性格。
            raise FileNotFoundError(
                f"L1 Core personality 文件不存在（base_personality_file={base_file}，"
                f"tried: {tried}）"
            )
        logger.warning(
            "L1 Core personality 文件不存在 (tried: %s)，C1 不使用 core 文本",
            tried,
        )
        return ""

    def personality_block(self) -> str:
        """Render a compact personality description for prompt injection。

        优先使用 base_personality，降级使用 traits + speaking_style（兼容旧 YAML）。

        Returns:
            紧凑的人格描述文本。
        """
        if self.base_personality:
            return self.base_personality
        traits_str = "、".join(self.traits)
        lines = [f"你是 {self.name}。"]
        if self.traits:
            lines.append(f"性格: {traits_str}")
        if self.speaking_style:
            lines.append(f"说话风格: {self.speaking_style}")
        return "\n".join(lines)
