# Input 层拆分 批次 C：api_routes 按领域拆 5 文件

**文档版本：** v0.1（初稿，待评审）
**文档状态：** 待评审
**日期：** 2026-08-14
**仓库：** `/home/yomu/elysia-agent`
**上游评估：** 会话记录「core/input 内聚性评估」（2026-08-14）问题 2
**前置决策：** AskUserQuestion Q1（**5 文件按领域聚合**）+ Q3（**helper 随业务下移 + 公共进 _common**）

---

## 1. 背景

`core/input/api_routes.py`（1182 行，全仓第 3）把 **8 组互不相关路由**塞进一个
`register_api_routes(app)` 函数（245-1182，约 940 行），14 个 handler 全是**嵌套闭包**：

- Provider 管理（4）：`providers_{list,save,delete,test}_handler`
- TTS 声音（2）：`tts_voices_handler`、`tts_preview_handler`
- 图片上传（1）：`image_upload_handler`
- 通用上传 + 表情（2）：`upload_handler`、`stickers_handler`
- Desktop 工具（3）：`tools_registry_handler`、`tools_call_status_handler`、`desktop_upload_handler`
- Chat API + 消息归档（2）：`chat_api_handler`、`messages_handler`

闭包无法独立 import/单测；对比 `session_routes.py`（854 行）用**模块级 handler + 末尾 register** 模式，
明显更优。内嵌闭包 helper（`_tool_error`/`_tool_metadata_to_contract`/`_desktop_call_to_status`）进一步恶化。

## 2. 目标

- `api_routes.py` 按领域拆 **5 文件**（Q1 敲定），沿用 session_routes 模块级 handler 模式；
- 共享 helper 归位（Q3）：各路由组自带私有 helper，跨组共享进 `_common.py`；
- `api_routes.py` 退役删除（8 组路由迁走后）；
- **零行为变更**（3548 测试锁定）。

## 3. 现状（事实基线）

| 项 | 值 |
|---|---|
| handler | 14 个（全为 register_api_routes 内嵌套闭包） |
| 路由组 | 8 组：providers(4) / tts(2) / image(1) / uploads+stickers(2) / desktop-tools(3) / chat+messages(2) |
| 共享 helper | `_sign_url`（被 attachment_routes import）、`_archive_to_client`/`_archive_msg_to_contract`（被 session_routes + debug_routes/archive import）、`_PREVIEW_TEXT`（被测试 import） |
| 私有 helper | `_strip_secret_keys`、`_custom_profile_in_use`、`_run_usable_test`、`_tool_error`、`_tool_metadata_to_contract`、`_desktop_call_to_status` |
| 出口 | `register_api_routes`（被 server import）；上述共享 helper |

## 4. 方案

### 4.1 拆 5 文件（模块级 handler + 末尾 `register_<group>_routes(app)`）

| 新文件 | 内容 | handler 数 |
|---|---|---|
| `core/input/provider_routes.py` | providers list/save/delete/test | 4 |
| `core/input/tts_routes.py` | voices + preview | 2 |
| `core/input/upload_routes.py` | images + uploads + stickers | 3 |
| `core/input/tools_routes.py` | tools registry/call-status + desktop upload | 3 |
| `core/input/chat_routes.py` | chat + messages | 2 |

### 4.2 共享 helper 归位（Q3）

| helper | 归属 | 理由 |
|---|---|---|
| `_sign_url` | `upload_routes.py`（attachment_routes 改从它 import） | 签名/上传域 |
| `_archive_to_client`/`_archive_msg_to_contract` | `chat_routes.py` 或 `_common.py`（被 session_routes + debug_routes 共享 → 视跨组引用定） | 归档转换跨组共享 |
| `_PREVIEW_TEXT` | `tts_routes.py`（测试 import 路径更新） | TTS 域 |
| `_strip_secret_keys`/`_custom_profile_in_use`/`_run_usable_test` | `provider_routes.py` | Provider 私有 |
| `_tool_error` | `_common.py`（**provider + tools 共用**——跨组共享，Q3） | 统一错误信封 |
| `_tool_metadata_to_contract`/`_desktop_call_to_status` | `tools_routes.py` | Desktop 私有 |

### 4.3 退役 `api_routes.py`

5 文件拆完后删除 `api_routes.py`（或仅保留 `register_api_routes` 转调 5 个子注册？——**直接删除**，
server.py 改调 5 个 `register_<group>_routes(app)`；`_archive_to_client` 等共享 helper 归位后无出口残留）。

## 5. 决策记录

| 决策 | 选择 | 理由 |
|---|---|---|
| D-C1 拆分边界 | **5 文件按领域聚合**（Q1 敲定） | provider/tts/upload/tools/chat 边界清晰；避免 8 组一对一拆出的单 handler 空壳 |
| D-C2 handler 形式 | **模块级函数 + 末尾 register**（对齐 session_routes） | 可独立 import/单测；区别于现有闭包 |
| D-C3 api_routes 去留 | **直接删除**（server 改调 5 个子注册） | 拆完无残留价值；`register_api_routes` 转调会增加一层无意义间接 |
| D-C4 `_archive_to_client` 归属 | 视共享面定：仅 session_routes+debug 用 → `_common.py`（跨组共享） | Q3 原则：跨业务共享进 _common |

## 6. 波及面

- 新增：`provider_routes.py`/`tts_routes.py`/`upload_routes.py`/`tools_routes.py`/`chat_routes.py`
- 修改：`server.py`（`register_api_routes` → 5 个注册）、`attachment_routes.py`/`session_routes.py`/
  `debug_routes/archive.py`（共享 helper import 路径）、相关测试（`_PREVIEW_TEXT`/`_archive_*` import）
- 删除：`api_routes.py`
- **无**：wire 行为、路由路径、契约、错误码

## 7. 验收标准

1. `make test-quick` 全绿 + `make test-full`（3548 基线）全绿；
2. 路由表等价：`/api/providers` 等 14 个端点全部保留（测试覆盖的端点逐一验证）；
3. `core/input/api_routes.py` 删除，无残留 import（grep 为空）；
4. ruff + pyright 全绿（5 新文件纳入门禁）。

## 8. 风险

- **中**：14 个闭包 handler 改模块级函数——函数体零改动，但闭包捕获的变量（如 `app`、外部状态）
  需检查；`register_api_routes` 内 handler 若捕获外层变量，拆出时需改为参数传递或模块级依赖；
- 共享 helper 归位要逐一核对所有 import 方（grep `from core.input.api_routes import`）；
- 测试 mock `_archive_to_client`/`_PREVIEW_TEXT` 的路径需同步。

## 9. 后续衔接

- 批次 D（server.py 纯装配）以批次 A（errors.py）+ 本批（server 改调 5 注册）为基础；
- 与本批 B（ws_handler）独立，可并行推进。
