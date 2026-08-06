"""内置表情包注册表（批次 7 Task 6）。

扫描 ``static/stickers/*.png``，sticker_id = 文件名去扩展名（如 ``st_happy.png`` →
``st_happy``）。表情是公共静态资源，走 ``/static/stickers/{id}.png`` 公开路径，
不落 attachments 集合、不 ref_count、不参与 GC（契约层 StickerEntry 注释定案）。

用法：
    from core.input.sticker_registry import get, load_stickers

    load_stickers()          # create_app 启动时调用，扫描并构建注册表
    path = get("st_happy")   # -> Path | None（get 自带懒加载兜底）
"""

from pathlib import Path

from paths import ProjectPath

# 模块级注册表：sticker_id -> 落盘 Path（内存缓存，运行期不重复扫描）
_registry: dict[str, Path] = {}


def load_stickers() -> dict[str, Path]:
    """扫描 static/stickers/ 目录，构建并返回表情注册表。

    幂等：每次调用重建（支持热加载/测试隔离）；目录缺失时返回空 dict 不抛错。

    Returns:
        dict[str, Path]: sticker_id → 落盘 PNG 绝对路径。
    """
    _registry.clear()
    stickers_dir = ProjectPath.STATIC.resolve() / "stickers"
    if not stickers_dir.is_dir():
        return _registry
    for png in sorted(stickers_dir.glob("*.png")):
        _registry[png.stem] = png
    return _registry


def get(sticker_id: str) -> Path | None:
    """按 sticker_id 取落盘路径；未知返回 None。

    未显式调用 load_stickers() 时懒加载一次（测试/独立调用友好）。

    Args:
        sticker_id: 表情 ID（如 "st_happy"）。

    Returns:
        Path | None: 落盘 PNG 路径，未注册返回 None。
    """
    if not _registry:
        load_stickers()
    return _registry.get(sticker_id)
