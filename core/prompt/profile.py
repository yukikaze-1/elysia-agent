"""ProfileConfig — YAML profile 加载。"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass
class ProfileConfig:
    """定义"用哪些 section、用哪个变体"。

    Attributes:
        name: profile 名称
        description: 人类可读说明
        enabled: section 引用列表，如 ["identity", "identity@soft", "sensory"]
    """

    name: str
    description: str
    enabled: list[str] = field(default_factory=list)

    @property
    def enabled_set(self) -> set[str]:
        """纯 section 名集合（去掉 @variant 后缀）。

        用于 render() 中快速判断 section_name in prof.enabled_set。
        """
        result: set[str] = set()
        for ref in self.enabled:
            if "@" in ref:
                result.add(ref.split("@", 1)[0])
            else:
                result.add(ref)
        return result

    @classmethod
    def from_yaml(cls, path: str | Path) -> "ProfileConfig":
        """从 YAML 文件加载 profile。

        Args:
            path: profile YAML 文件路径

        Returns:
            ProfileConfig 实例
        """
        raw = yaml.safe_load(Path(path).read_text("utf-8")) or {}
        name = raw.get("name", "")
        description = raw.get("description", "")
        enabled = raw.get("enabled", [])
        if not isinstance(enabled, list):
            enabled = []
        return cls(name=name, description=description, enabled=enabled)
