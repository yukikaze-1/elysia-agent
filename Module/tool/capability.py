"""工具所需能力枚举 — 取代字符串 requires，支持能力可用性检测。

同时承载 RBACLevel（工具权限级别）——Module/tool 是 canonical home，
契约层 HITLData.rbac_level 从本模块解析。
"""

from enum import Enum, StrEnum


class Capability(Enum):
    DATABASE_READ = "database:read"
    DATABASE_WRITE = "database:write"
    NETWORK = "network"
    WEB_SEARCH = "web_search"
    FILESYSTEM_READ = "filesystem:read"
    FILESYSTEM_WRITE = "filesystem:write"
    PROCESS = "process"
    SCHEDULE = "schedule"


class RBACLevel(StrEnum):
    """工具权限级别。注册时声明，运行时校验。

    Module/tool 声明权限级别，WorkflowEngine 执行时校验。
    契约层 HITLData.rbac_level 从本模块解析。
    """
    READ_ONLY = "read_only"       # 只读，完全自主
    SAFE_WRITE = "safe_write"     # 安全写入（沙箱内），条件自主
    MUTATION = "mutation"         # 外部变更，必须审批
