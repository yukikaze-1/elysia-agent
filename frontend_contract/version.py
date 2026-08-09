"""传输/协议版本常量（契约单源，P7）。

Task 6（`resolve_protocol_version`）用 `TRANSPORT_VERSION` 作握手头可用值；
`ServerInfoMessage.protocol_version` 默认值用 `PROTOCOL_VERSION`——运行时不再手写
`"2"`/`"2.0"` 字面量。
"""

TRANSPORT_VERSION = "2"      # 握手头 X-Protocol-Version 可用值（Electron 照抄即合法）
PROTOCOL_VERSION = "2.0"     # 仅展示（server_info 下发），不可用于握手头
