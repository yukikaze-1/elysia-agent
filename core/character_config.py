"""Character configuration — loaded from YAML, defines personality/voice/wake-words."""

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
    personality: dict[str, object] = field(default_factory=dict)
    emotion: EmotionConfig = field(default_factory=EmotionConfig)
    wake_words: list[str] = field(default_factory=list)
    proactive: ProactiveConfig = field(default_factory=ProactiveConfig)

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
        return cls(
            name=raw.get("name", "Agent"),
            voice=raw.get("voice", "default"),
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

    def personality_block(self) -> str:
        """Render a compact personality description for prompt injection."""
        traits_str = "、".join(self.traits)
        lines = [f"你是 {self.name}。"]
        if self.traits:
            lines.append(f"性格: {traits_str}")
        if self.speaking_style:
            lines.append(f"说话风格: {self.speaking_style}")
        return "\n".join(lines)
