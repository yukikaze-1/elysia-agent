"""Character configuration — loaded from YAML, defines personality/voice/wake-words."""

import logging
from dataclasses import dataclass, field
from pathlib import Path

import yaml


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
        return self.personality.get("traits", [])  # type: ignore[return-value]

    @property
    def speaking_style(self) -> str:
        return str(self.personality.get("speaking_style", ""))

    @classmethod
    def from_yaml(cls, path: str | Path) -> "CharacterConfig":
        path = Path(path)
        if not path.exists():
            raise FileNotFoundError(f"Character config not found: {path}")
        raw = yaml.safe_load(path.read_text("utf-8")) or {}
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
        """加载 L1 Core personality 文本。路径解析策略：
        1. 相对于 character YAML 所在目录
        2. 回退项目根目录
        3. 未指定时推导 character/{name}_personality.txt
        4. 文件不存在 → WARN，返回空串
        """
        logger = logging.getLogger(__name__)

        candidates = []
        if base_file:
            candidates.append(yaml_path.parent / base_file)          # Step 1: YAML 目录
            candidates.append(Path(".") / base_file)                 # Step 2: 项目根目录

        # Step 3: 自动推导 character/{name}_personality.txt
        candidates.append(yaml_path.parent / f"{name}_personality.txt")

        for p in candidates:
            if p.exists():
                return p.read_text("utf-8").strip()

        logger.warning(
            "L1 Core personality 文件不存在 (tried: %s)，C1 不使用 core 文本",
            [str(c) for c in candidates]
        )
        return ""

    def personality_block(self) -> str:
        """Render a compact personality description for prompt injection。
        优先使用 base_personality，降级使用 traits + speaking_style（兼容旧 YAML）。
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
