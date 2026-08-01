"""工具错误分类 — 从 Module/_common 重导出（保持 core.schema.error_type 兼容）。"""
from Module._common.error_type import ErrorType

__all__ = ["ErrorType"]
