"""Helpers for inserting user-authored character text into trusted templates."""

from html import escape


def escape_authored_prompt(value: str) -> str:
    """Render authored text as data, never as framework/XML-like markup."""
    return escape(value, quote=False)
