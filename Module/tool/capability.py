"""工具所需能力枚举 — 取代字符串 requires，支持能力可用性检测。"""

from enum import Enum


class Capability(Enum):
    DATABASE_READ = "database:read"
    DATABASE_WRITE = "database:write"
    NETWORK = "network"
    WEB_SEARCH = "web_search"
    FILESYSTEM_READ = "filesystem:read"
    FILESYSTEM_WRITE = "filesystem:write"
    PROCESS = "process"
    SCHEDULE = "schedule"
