"""ILA Dashboard package."""

from __future__ import annotations

from ila import DASHBOARD_THEMES, DEFAULT_DASHBOARD_THEME

# 支持的页面主题 (与顶层 ila.DASHBOARD_THEMES 保持一致)
AVAILABLE_THEMES: tuple[str, ...] = DASHBOARD_THEMES
DEFAULT_THEME: str = DEFAULT_DASHBOARD_THEME

# 纳管对象分页——每页条数
DEFAULT_PAGE_SIZE: int = 10

# 版本历史分页——每页条数
VERSION_HISTORY_PAGE_SIZE: int = 10

# 主题展示名 (中文标签)
_THEME_LABELS: dict[str, str] = {
    "dark": "暗色",
    "light": "亮色",
    "ocean": "海洋",
    "sepia": "复古",
    "grassland": "草原",
    "starry": "星空",
}


def get_themes() -> list[dict[str, str]]:
    """返回可用主题列表，每项为 {id, name}."""
    return [{"id": theme, "name": _THEME_LABELS.get(theme, theme)} for theme in AVAILABLE_THEMES]


def is_valid_theme(theme: str) -> bool:
    """判断给定主题是否受支持."""
    return theme in AVAILABLE_THEMES


def resolve_theme(theme: str | None) -> str:
    """解析主题: 为空或不受支持时回退到默认主题."""
    if theme and is_valid_theme(theme):
        return theme
    return DEFAULT_THEME


__all__ = [
    "AVAILABLE_THEMES",
    "DEFAULT_THEME",
    "DEFAULT_PAGE_SIZE",
    "VERSION_HISTORY_PAGE_SIZE",
    "get_themes",
    "is_valid_theme",
    "resolve_theme",
]
