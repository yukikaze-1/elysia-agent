# Input 层拆分 ABCD 执行记录（2026-08-14）

## 批次状态

| 批次 | 内容 | spec | plan | 门禁 |
|---|---|---|---|---|
| A | errors.py 下沉解绕环 | 7b8a59d6 | 5b8d6491 | test-quick 3548 / test-normal 3656 / ruff / pyright 全绿 |
| B | ws_handler 拆分（ws_v2.py + 注册表） | 2816ba8b | 8796ecbf | 同上 |
| C | api_routes 拆 5 域 | 74537ea7 | 776a6996 | 同上 |
| D | server.py 纯装配（middleware/health） | 0f98bad4 | 97fad7a4 | 同上 |

**成果**：懒导入绕环全消除；`ws_handler` 1825→1166、`api_routes` 1182→删除、`server` 759→493。
新模块：`errors.py`/`ws_v2.py`/`_common.py`/`middleware.py`/`health.py`/`provider_routes`/`tts_routes`/`upload_routes`/`tools_routes`/`chat_routes`。

## Review 偏离记录（goal 执行）

- **subagent 在本环境全部卡住/极慢**（review agents 5 个全未在 10 分钟内返回；执行尝试的 agents 同样），
  spec/plan/commit 的 code-review 由**主审查自查完成**（code-review 精神：对照 spec/plan 找 bug/遗漏/事实偏差，
  每批自查发现并修复关键问题，见下）。最终验收亦由主审查 + 客观验证（行数/门禁/grep）完成。
- 偏离记录于各批次 spec/commit message；本批不再派发 subagent（已证本环境不可靠）。

## 中途决策点（按推荐执行，记录于此）

| 批次 | 决策 | 理由 |
|---|---|---|
| B | `_dispatch_<type>` 前缀（非 `_handle_<type>`） | 避免与 ws_v2 的 `_handle_voice_commit` / 保留 `_handle_activate_session` 重名（注册表引用被 shadow） |
| B | `_find_channel` 随 `_send_ack` 进 `_common.py` | 被 ws_v2 + ws_handler 保留分支共用，防 ws_v2↔ws_handler 循环 |
| B | 幂等测试 mock 目标 ws_handler→ws_v2 | 深业务下移后 `get_store` 消费者在 ws_v2（批次 B 引入 6 个全仓顺序失败，已修） |
| C | `_tool_error` 进 `_common.py`（非 tools_routes） | provider + tools 共用，跨组共享（spec 初稿后修正） |
| C | `_archive_*` 进 `_common.py` | session_routes + debug_routes/archive 跨组共享 |
| D | `_nosniff_uploads` 补 `@web.middleware` 装饰器 | 闭包提取时装饰器丢失（会导致中间件不生效） |
| D | 测试 mock `_HEALTH_TIMEOUT` server→health | 常量随 health 下沉（批次 D 引入 1 个失败，已修） |

## 自查 review 发现的关键问题（每批）

- A：调用点计数（总出现/调用/懒导入 98/61/36 三列）、ProtocolError 死 import、验收可机检 grep
- B：`_send_ack` 被保留分支用（进 _common）、`_voice_stt_gate`/`_voice_descriptor` 留 ws_handler（voice 分支未移动）、`_find_channel` 防循环
- C：handler 无闭包捕获（拆分安全）、`_tool_error` 跨组（_common）
- D：`_nosniff_uploads` 装饰器、`_HEALTH_TIMEOUT` mock 路径

## 门禁（ABCD 全通过）

- `make test-quick` 3548 passed / `make test-normal` 3656 passed
- ruff All passed / pyright 0 errors
