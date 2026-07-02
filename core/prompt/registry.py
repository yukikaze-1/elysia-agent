"""BrainPromptRegistry — section 注册、渲染、A/B 分流、热更新。"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from typing import Any

import yaml
from jinja2 import Environment

from .parser import SectionParser
from .profile import ProfileConfig
from .section import (
    MissingContextError,
    ProfileValidationError,
    SectionNotFoundError,
    SectionRenderError,
    PromptSection,
)

logger = logging.getLogger(__name__)


class BrainPromptRegistry:
    """Brain system prompt 的 section 注册、渲染、热更新中心。"""

    def __init__(self, template_dir: str | Path, *, watch: bool = False):
        """加载所有 section、profile、metadata。

        Args:
            template_dir: prompt/brain/ 目录路径
            watch: 是否启动文件监听（Phase 1 暂不实现，保留参数）
        """
        self._base = Path(template_dir)
        self._section_dir = self._base / "sections"
        self._profile_dir = self._base / "profiles"
        self._metadata_path = self._base / "metadata.yaml"
        self._watch = watch

        # Jinja2 环境 — trim_blocks/lstrip_blocks 与 PromptManager 保持一致，
        # 否则黄金对比测试会因空白差异失败
        self._jinja_env = Environment(trim_blocks=True, lstrip_blocks=True)

        # 注册 datetime 过滤器 — memory section 的 {{ mem.timestamp | datetime }} 需要
        def _datetime_filter(value, fmt="%Y-%m-%d %H:%M:%S"):
            if value is None:
                return ""
            from datetime import datetime as dt
            return dt.fromtimestamp(float(value)).strftime(fmt)
        self._jinja_env.filters["datetime"] = _datetime_filter

        # 加载
        self._parser = SectionParser()
        self._sections: dict[str, dict[str, PromptSection]] = {}
        self._profiles: dict[str, ProfileConfig] = {}
        self._order: list[str] = []
        self._ab_experiments: dict[str, dict] = {}
        self._load_all()

        if watch:
            logger.warning("watch=True 暂未实现文件监听（Phase 1 不包含）")

    # ---- 公有 API ----

    def render(self, profile: str = "default", context: dict[str, Any] | None = None) -> str:
        """按 profile 渲染 system prompt。

        Args:
            profile: profile 名称，或 "__session__" 触发 AB 分流
            context: 所有 section 可能用到的参数字典

        Returns:
            完整的 system prompt 字符串

        Raises:
            MissingContextError: context 缺少 section 声明的参数
            SectionRenderError: 某个 section 渲染失败
            ProfileValidationError: profile 不存在
        """
        if context is None:
            context = {}

        if profile == "__session__":
            profile = self.get_profile_for_session(context.get("session_id", ""))

        if profile not in self._profiles:
            raise ProfileValidationError(profile, f"profile '{profile}' 不存在")

        prof = self._profiles[profile]
        parts: list[str] = []

        for name in self._order:
            section_name, variant = self._parse_ref(name)

            if section_name not in prof.enabled_set:
                continue

            section = self._resolve_section(section_name, variant)

            # 只传 section 声明的参数；缺失参数抛出 MissingContextError
            section_ctx: dict[str, Any] = {}
            for param in section.params:
                if param not in context:
                    raise MissingContextError(section=section.name, param=param)
                section_ctx[param] = context[param]

            try:
                rendered = self._jinja_env.from_string(section.template).render(
                    **section_ctx
                )
            except Exception as exc:
                raise SectionRenderError(section=section.name, cause=exc) from exc

            parts.append(rendered)

        return "\n\n".join(parts)

    def reload(self) -> None:
        """重新扫描 sections/ 和 profiles/，原子替换。"""
        try:
            new_sections = self._parser.scan_all(str(self._section_dir))
            new_profiles = self._load_profiles()
            new_order, new_ab = self._load_metadata()
            # 原子替换前先校验
            self._validate_profiles_with(new_sections, new_profiles)
            self._sections = new_sections
            self._profiles = new_profiles
            self._order = new_order
            self._ab_experiments = new_ab
        except ProfileValidationError:
            logger.warning("reload 失败：profile 校验不通过，保持旧 sections", exc_info=True)
        except Exception:
            logger.warning("reload 失败，保持旧 sections 不变", exc_info=True)

    def get_versions(self) -> dict[str, str]:
        """返回 {section_name: default_variant_version} 映射。"""
        result: dict[str, str] = {}
        for name, variants in self._sections.items():
            default = variants.get("")
            if default:
                result[name] = default.version
        return result

    def get_profile_for_session(self, session_id: str) -> str:
        """基于 session hash 选择 AB profile。

        无实验时返回 "default"。
        有实验时按 hash(session_id + exp_name) % 100 分流。
        """
        if not self._ab_experiments:
            return "default"

        for exp_name, cfg in self._ab_experiments.items():
            ratio = float(cfg.get("ratio", 0))
            bucket = int(hashlib.md5((session_id + exp_name).encode()).hexdigest(), 16) % 100
            if bucket < ratio * 100:
                return cfg.get("variant_profile", "default")

        return "default"

    # ---- 私有 ----

    def _load_all(self) -> None:
        """加载所有 section、profile、metadata。"""
        self._sections = self._parser.scan_all(str(self._section_dir))
        self._profiles = self._load_profiles()
        self._order, self._ab_experiments = self._load_metadata()
        self._validate_profiles()

    def _load_profiles(self) -> dict[str, ProfileConfig]:
        """加载 profiles/ 下所有 .yaml 文件。"""
        profiles: dict[str, ProfileConfig] = {}
        if self._profile_dir.is_dir():
            for yaml_path in sorted(self._profile_dir.glob("*.yaml")):
                prof = ProfileConfig.from_yaml(str(yaml_path))
                profiles[prof.name] = prof
        return profiles

    def _load_metadata(self) -> tuple[list[str], dict[str, dict]]:
        """加载 metadata.yaml，返回 (order, ab_experiments)。"""
        if not self._metadata_path.exists():
            raise FileNotFoundError(f"metadata.yaml 未找到: {self._metadata_path}")

        raw = yaml.safe_load(self._metadata_path.read_text("utf-8")) or {}
        order: list[str] = raw.get("order", [])
        ab: dict[str, dict] = raw.get("ab_experiments", {})
        if not isinstance(ab, dict):
            ab = {}
        return order, ab

    def _validate_profiles(self) -> None:
        """校验所有 profile 引用的 section 都存在。"""
        self._validate_profiles_with(self._sections, self._profiles)

    def _validate_profiles_with(
        self, sections: dict[str, dict[str, PromptSection]], profiles: dict[str, ProfileConfig]
    ) -> None:
        """使用给定的 sections 和 profiles 进行校验，供 reload() 复用。"""
        all_section_names = set(sections.keys())
        for prof in profiles.values():
            unknown = prof.enabled_set - all_section_names
            if unknown:
                raise ProfileValidationError(
                    profile=prof.name,
                    detail=f"引用了不存在的 section: {', '.join(sorted(unknown))}",
                )

    def _resolve_section(
        self, section_name: str, variant: str
    ) -> PromptSection:
        """根据 section 名和变体名查找 PromptSection。

        Args:
            section_name: section 名（如 "identity"）
            variant: 变体名（如 "soft"），空字符串表示默认变体

        Returns:
            PromptSection 实例

        Raises:
            SectionNotFoundError: section 或变体不存在
        """
        variants = self._sections.get(section_name)
        if variants is None:
            raise SectionNotFoundError(section_name, str(self._section_dir))

        if variant in variants:
            return variants[variant]

        # 变体不存在——列出可用变体
        available = list(variants.keys())
        raise SectionNotFoundError(
            f"{section_name}@{variant}" if variant else section_name,
            f"可用变体: {available}",
        )

    @staticmethod
    def _parse_ref(ref: str) -> tuple[str, str]:
        """解析 "section@variant" → (section_name, variant)。"""
        if "@" in ref:
            return tuple(ref.split("@", 1))  # type: ignore[return-value]
        return (ref, "")
