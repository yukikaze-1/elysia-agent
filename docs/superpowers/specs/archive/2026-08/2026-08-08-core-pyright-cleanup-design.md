# core/ 生产代码 pyright 报错清理 + 引入 pyright 门禁 — 设计文档

> **来源**：用户整理 IDE 报错清单（2026-08-08，22 文件 ~113 处），拍板「全修 + 引入 pyright 门禁」。
>
> **关键调研发现（两次基线）**：
> - CLI pyright 裸跑 core/ 报 715 errors（494 个在测试文件、14 个是 `reportMissingModuleSource` warning）。
> - 排除测试 + basic 模式 + 关 `reportMissingImports`，**base conda 环境（无 aiohttp）下 67 errors**——
>   但这是**环境假象**：base python 3.13 没有 aiohttp，`web.WebSocketResponse` 松散解析掩盖了 ~25 处。
> - **Elysia env（有 aiohttp，项目真实运行环境）下真实基线 = 92 errors**（core/ 生产代码，~24 文件）。
>   review 复现确认：同一配置，base env 67 / Elysia env 92。**门禁必须锁定 Elysia env**，否则基线漂移、
>   CI 不激活 env 会漏报 25 个真实类型错误。

**Goal**：修清 core/ 生产代码的 pyright 报错（**92 处，Elysia env 基线**），并引入 pyright 门禁
（`pyrightconfig.json` + `make pyright` + 文档），门禁锁定 Elysia env、检 core/ 生产代码 **0 errors**。
**运行时以零变化为主**（绝大多数是类型标注修正；可空点收窄 + StrEnum 收口需声明边界）。

**Architecture**：门禁配置先行（锁 env + 收敛基线到 92），再按根因分类修 92 处。修复分五类：
A（mixin/动态属性不可见）→ 类型标注占位；B（NewType/枚举收口）→ 签名收窄/收口点（含 ValueError 边界声明）；
C（可空未收窄）→ 断言/收窄；D（契约构造/返回类型/aiohttp 泛型）→ 参数对齐/类型修正/cast；E（其他）→ 逐个。

**Tech Stack**：Python 3.12 + pyright 1.1.411（conda env `Elysia`，`pip install pyright`）。pytest（marker=unit/service）。

## Global Constraints

- 生产代码函数参数必须类型注解（ruff ANN001-3；**pyright 门禁将强制更精确的类型**）。
- 禁相对导入（ruff TID252）。代码注释/README 用中文。
- **门禁检 `core/` 生产代码**（用户核心；Module/ 报错留后续，避免 workflow_engine 等复杂报错牵出）。
- **`typeCheckingMode: basic`**（非 strict——standard 会引入 reportIncompatibleMethodOverride 等设计级报错）。
- **排除测试文件**（`**/tests/**`、`**/test_*.py`）——pytest fixture/mock 动态类型，与 ruff ANN 测试豁免一致。
- **关 `reportMissingImports`**（第三方无 stub）。
- **门禁必须在 Elysia env 运行**：`make pyright` 用 `conda run -n $(CONDA_ENV) pyright`（base env 会漏报 25 处）。
- **不因 pyright 用 getattr 退步**——mixin 属性用类型标注声明（与本会话 getattr 审计纪律一致）。
- 完整触及点：`/tmp/pyright-errors-92.txt`（92 行，已导出）。

---

## 现状分析（92 报错按文件 + 根因分组）

### A. mixin/动态属性不可见（类型标注占位，不用 getattr）

| 文件 | 处数 | 属性/问题 | 修法方向 |
|------|------|----------|---------|
| `core/agent/server.py` | 17 | `AgentServer.session`（×11）/`stt_enabled`/`tts_enabled`（×2）/`self` 传 `broadcast_capabilities(agent: Agent)`（×4） | `AgentServer` 类内声明 `session: SessionMemory`/`stt_enabled: bool`/`tts_enabled: bool` 类型占位（运行时来自组合的 Agent @property）；`broadcast_capabilities` 参数放宽为 `"Agent \| AgentServer"` 或内部只用公开属性 |
| `core/agent/_lifecycle_persistence.py:133` | 1 | `_LifecyclePersistence.attachment_repo` | 类内声明 `attachment_repo: AttachmentRepository` 占位 |
| `core/agent/_lifecycle_tools.py:104` | 1 | `OutputChannel._send_json`（私有跨类访问） | 看调用点——若 websocket 特有，改公开方法或类型声明 |
| `core/adapter/tts_adapter.py:310` | 1 | `Task.flush_on_cancel`（asyncio.Task 动态属性，运行时合法） | `# type: ignore[attr-defined]` + 注释 |
| `core/tools/runtime_config/plugin.py:302,305` | 2 | `dict.created_at`（dict 推断 `dict[Unknown,Unknown]`） | 显式 dict 类型标注 |

### B. NewType/枚举收口（含 ValueError 边界声明）

| 文件 | 处数 | 值 → 目标 | 修法方向 |
|------|------|----------|---------|
| `core/input/session_routes.py` | 302,309,351,362 | `str` → `SessionId`（get_by_mongo_id/query_page） | `SessionId(x)` 收口 |
| `core/input/session_routes.py` | 529,536 | `str`（match_info）→ `switch_session(sid: SessionId)` | agent.py:644 签名收窄后，此处 `SessionId(sid)` 收口 |
| `core/input/session_routes.py:652` | 1 | `str` → `Role` | `Role(x)` 收口（**ValueError 边界**，见行为变化） |
| `core/input/session_routes.py:711` | 1 | `Literal['stop','tool_calls'] \| None` → `FinishReason` | `FinishReason(x)` |
| `core/input/voice_sessions.py:328,341,543,589` | 4 | `Literal[...]` → `VoiceEndReason` | `VoiceEndReason(x)` |
| `core/input/voice_sessions.py:519` | 1 | `str` → `MessageId \| None` | `_build_user_input` 参数 `str` → `MessageId`（ID 批次遗留） |
| `core/agent/agent.py:640` | 1 | `int \| str` → `resolve(protocol_version: int)` | 签名收窄为 `int`（**已核实**：所有 resolve_session 调用方传 int——ws_handler:127 派生 int、voice_sessions:524/ws_handler:532,692 传字面量 2） |
| `core/agent/agent.py:644` | 1 | `SessionId \| str` → `switch(sid: SessionId)` | 签名收窄为 `SessionId` + session_routes:529,536 调用点 `SessionId(sid)` 收口（**review 修正**：初稿漏此调用点） |
| `core/input/ws_handler.py:429` | 1 | `str` → `AckStatus` | `AckStatus(x)` |
| `core/cognitive/loop.py:699` | 1 | `SessionId \| None` → `record(session_id: SessionId)` | 收窄（`or` 兜底/断言，查上下文） |
| `core/monitoring/idle_monitor.py:112,129` | 2 | `Literal[...]` → `VoiceReason` | `VoiceReason(x)` |
| `core/input/debug_routes/archive.py:281` | 1 | `str` → `SessionId`（release_session_attachments） | `SessionId(x)` |
| `core/input/session_routes.py:515` | 1 | `str`（match_info）→ `release_session_attachments(agent, sid: SessionId)` | `SessionId(sid)`（同函数第二个调用点，初稿漏） |
| `core/input/api_routes.py:950` | 1 | `Unknown \| Literal['pending']` → `RepoCallStatus` | `RepoCallStatus(x)`（**ValueError 边界**） |

### C. 可空未收窄（逐个判断，潜在 NoneType 风险）

| 文件 | 处数 | 属性/值 | 修法方向 |
|------|------|---------|---------|
| `core/input/ws_handler.py` | ~20 | `ws: WebSocketResponse \| None` 传给非 None 的 `_send_ack`/`_send_voice_error` | **统一方案**：`_send_ack`/`_send_voice_error` 签名改为接受 `WebSocketResponse \| None`，函数内部 `if ws is None: return`（一处改、20 处调用点稳，且语义正确——voice 错误路径 ws 可空）。**review 修正**：初稿写"20 处断言"，一处改签名更优 |
| `core/input/ws_handler.py:719` | 1 | `voice_mgr.commit_recording`（voice_mgr 可空） | 入口 `if voice_mgr is None` 或断言 |
| `core/agent/_lifecycle_services.py:136` | 1 | `set_enabled(self._tts_enabled)`（`_tts_enabled: bool \| None`） | 入口 `if self._tts_enabled is None` 回退 config 或断言（service_state enabled 三态） |
| `core/input/debug_routes/character.py:129,191` | 2 | `result.wire_dict()`（result 推断 None） | 收窄 `if result is None: return` |
| `core/input/session_routes.py:652` | 1 | `msg.role.value`（msg 推断 None） | `getattr` 收窄或断言 |
| `core/input/voice_sessions.py:484,500` | 2 | `sessionId=self._session_id`（`_session_id: str \| None`） | 构造处 `str(x) if x else ""` 或断言（voice session 必已初始化） |
| `core/monitoring/token_tracker.py:79,80,81,126` | 4 | `model/round_num/session_id = None` | **类型标注改 `str \| None`/`int \| None`/`SessionId \| None`**（record 默认 None 合法，标注非 Optional 是 bug） |
| `core/desktop/bridge.py:188` | 1 | `Future \| None` 赋给 `Future` | 断言/收窄 |

### D. 契约构造/返回类型/aiohttp 泛型

| 文件 | 处数 | 值 → 目标 | 修法方向 |
|------|------|----------|---------|
| `core/input/api_routes.py:489,491` | 2 | `TtsVoiceEntry` vs `list[dict]` | append 目标改 `TtsVoiceEntry` 或 `wire_dict()`；`:491` 参数类型 `list[dict]`→`list[TtsVoiceEntry]` |
| `core/input/ws_handler.py:93` | 1 | `list[ClientMessageDict]` → `list[dict]` | `_build_session_items` 参数放宽 |
| `core/input/ws_handler.py:218,222` | 2 | `dict/bool` → `__setitem__(value: str)` | 字典值类型标注 |
| `core/input/session_routes.py:306,320,359,377` | 4 | `tuple[..., tuple[Unknown\|None, int]]` → `tuple[str, int]` | 解包处 `str(x)`/返回类型修正 |
| `core/memory/session.py:168` | 1 | dict → `ClientToolMessage` | TypedDict 构造/断言 |
| `core/input/ssrf.py:129` | 1 | `str \| int` → `_is_blocked_ip(ip: str)` | `str(ip)` 收窄 |
| `core/input/voice_sessions.py:296` | 1 | `(() -> Awaitable[None])` → `Callback` | Callback 类型定义（返回 Coroutine） |
| `core/input/attachment_routes.py:168` | 1 | `FileResponse` 不可赋给 `Response` | 返回类型改 `StreamResponse`（FileResponse 子类）或 cast |
| `core/input/server.py:345` | 1 | `Response` 不可赋给 `WebSocketResponse[Literal[True]]` | cast 或返回类型调整 |
| `core/memory/memory_reflector.py:240` | 1 | `Literal[10]` → `n_init: str` | sklearn KMeans 类型 stub 误报——`# type: ignore[arg-type]` 或显式 `int(10)` |
| `core/tools/time/plugin.py:62` | 1 | `ZoneInfo` → `timezone \| None` | 标注改 `tzinfo \| None` |

### E. 其他（可空迭代/兜底）

| 文件 | 处数 | 规则 | 修法方向 |
|------|------|------|---------|
| `core/tools/vision/plugin.py:202,211` | 2 | reportOptionalIterable | `x or []` 兜底 |

---

## 设计

### 1. pyright 门禁配置（锁定 Elysia env）

新建 `pyrightconfig.json`：

```json
{
  "include": ["core/"],
  "exclude": ["**/tests/**", "**/test_*.py", "**/__pycache__"],
  "pythonVersion": "3.12",
  "typeCheckingMode": "basic",
  "reportMissingImports": false
}
```

`Makefile` 加目标（**`conda run -n $(CONDA_ENV)` 锁 env——base env 会漏报 25 处**）：

```makefile
# 类型检查门禁（pyright，core 生产代码；须在 Elysia env 运行，pyright 已装）
pyright:
	conda run -n $(CONDA_ENV) pyright
```

`CLAUDE.md`「Running tests」加 pyright 命令 + 「Conventions」记录门禁（须 Elysia env）。

### 2. A 类修法（mixin 属性类型标注占位）

- `server.py`：`AgentServer` 类内声明 `session: "SessionMemory"`、`stt_enabled: bool`、`tts_enabled: bool`；
  `broadcast_capabilities` 参数放宽为 `"Agent | AgentServer"`（内部只用公开属性）。
- `_lifecycle_persistence.py`：声明 `attachment_repo` 占位；`_lifecycle_tools.py` 看调用点定；
  `tts_adapter.py` `# type: ignore[attr-defined]`；`runtime_config` 显式 dict 类型。

### 3. B 类修法（NewType/枚举收口）

- `SessionId(x)`/`MessageId(x)`/`Role(x)`/`FinishReason(x)`/`VoiceEndReason(x)`/`VoiceReason(x)`/`AckStatus(x)`/`RepoCallStatus(x)`。
- `agent.py:640` 签名收窄 `int`；`:644` 收窄 `SessionId` + `session_routes.py:529,536` 补 `SessionId(sid)` 收口。
- `voice_sessions.py:519`：`_build_user_input(message_id: MessageId)`。
- **StrEnum 收口 ValueError 边界**：`Role(x)`/`RepoCallStatus(x)` 若 x 非成员值抛 ValueError——
  现状是宽松透传/pydantic 后置校验。**上游已过滤**（session_routes:652 role 先过滤到
  USER/ASSISTANT/TOOL；api_routes:950 doc status 来自 repo 枚举），故实际不触发，但声明为边界。

### 4. C 类修法（可空收窄）

- **ws_handler 统一方案**：`_send_ack`/`_send_voice_error` 签名 `ws: "web.WebSocketResponse | None"` +
  内部 `if ws is None: return`（一处改、20 调用点稳，语义正确）。**review 修正**：初稿"20 处断言"改为签名放宽。
- 其余：入口 `if x is None`/断言/默认值；`token_tracker` 标注改 `X | None`。

### 5. D 类修法

- 契约参数放宽（`list[dict]`→`list[TtsVoiceEntry]` 等）；tuple 解包 `str(x)`；FileResponse→`StreamResponse`；
  server:345 cast；memory_reflector `# type: ignore`；time `tzinfo | None`。

### 6. E 类修法

- `vision/plugin.py` `x or []` 兜底。

### 7. 测试与门禁验证

- 修完后 `conda run -n $(CONDA_ENV) pyright`（Elysia env）应 **0 errors**。
- 全量 `make test-normal` + `make lint` 回归（运行时零变化为主）。

---

## 行为变化（须在最终报告中声明）

1. **运行时以零变化为主**：绝大多数修法是类型标注/收口（NewType 即 str、枚举收窄、契约 pydantic 兼容、cast），
   不改变 wire 字节与运行逻辑。
2. **StrEnum 收口新增 ValueError 边界**：`Role(x)`/`RepoCallStatus(x)` 等若 x 非成员值在**调用点**抛 ValueError
   （现状宽松透传/pydantic 后置）。上游已过滤，实际不触发，但属新增异常路径——声明而非"零变化"。
3. **ws_handler `_send_ack`/`_send_voice_error` 签名放宽可空 + 内部 `if ws is None: return`**——行为对齐：
   可空 ws 时静默跳过（voice 错误路径 ws 恒非 None，无实际差异）；消除 ~20 处潜在 TypeError。
4. **token_tracker.record 标注改 `X | None`**——运行时行为不变，类型更准确。
5. **mixin 属性类型占位**（AgentServer.session 等）——运行时不变，静态可见。
6. **新增 pyright 门禁**：`make pyright`（Elysia env）检 core/ 生产 0 errors；base env 会漏报 25 处（须锁 env）。

## 测试策略

- **门禁**：`conda run -n $(CONDA_ENV) pyright` → 0 errors（核心标准）。
- **回归**：`make test-normal` + `make lint`。
- **重点回归**：core/input（ws_handler/session_routes/voice_sessions/api_routes）、core/agent、
  core/monitoring、core/memory、core/cognitive——B/C/D 改动集中区。

## Out of Scope

- **Module/ 的 pyright 报错**（workflow_engine sub_agent 12 处等）——门禁只检 core/，后续批次。
- **测试文件类型卫生**——排除在门禁外（与 ruff ANN 测试豁免一致）。
- **strict/standard 模式**——basic 起步；reportIncompatibleMethodOverride（ToolPlugin.execute 签名）是设计问题。
- **`reportMissingModuleSource` warning**（import yaml 无 stub）——不阻断。
- 不引入 mypy；不重构 ToolPlugin 参数签名体系。

## 决策记录（用户 /goal 要求记录，最终报告提出）

1. **门禁范围 core/ + basic 模式 + 排除测试**：用户核心关注；Module/ 留后续；basic 避免设计级报错；
   测试豁免与 ruff ANN 一致。
2. **门禁锁定 Elysia env**（review 关键发现）：base env 67 vs Elysia env 92——env 不锁会漏报 25 处真实
   类型错误（ws 可空/attachment FileResponse/archive SessionId 等）。Makefile 用 `conda run -n $(CONDA_ENV)`。
3. **mixin 属性类型占位（不用 getattr）**：与 getattr 审计纪律一致；类型占位是 pyright mixin 标准修法。
4. **ws_handler 可空统一方案**（review 修正）：改 `_send_ack`/`_send_voice_error` 签名接受可空 + 内部
   `if ws is None: return`——一处改、20 调用点稳，优于初稿"20 处断言"。
5. **StrEnum 收口 ValueError 边界声明**（review 修正）：非"零变化"，上游过滤保证实际不触发。
6. **真实 bug 判断**：92 处无确定运行时崩溃；C 类可空是潜在 NoneType 风险（若 None 路径存在则真实），
   ws_handler 签名放宽即修；token_tracker 标注 bug、tts_adapter 动态属性是类型层修正。

## Spec Review 修复记录（code-review 后并入）

| # | Review 发现 | 修复 |
|---|------------|------|
| 1 | 基线 67 是环境假象（base env 无 aiohttp）；Elysia env 真实 92 | 基线改 92；门禁锁 Elysia env（conda run）；分类补全到 92 全覆盖 |
| 2 | A-E 分类只覆盖 63/67，漏 4 处（_lifecycle_services:136、api_routes:491,950、runtime_config:305） | 分类表补全（_lifecycle_services 归 C 可空 enabled 三态、api_routes:491 归 D、:950 归 B、runtime_config:305 归 A） |
| 3 | Makefile 目标硬编码 env 路径，且 `conda run` 与直连基线不同 | 改 `conda run -n $(CONDA_ENV) pyright`（可移植 + 锁 env） |
| 4 | time/plugin.py "待查" 是假占位（:62 ZoneInfo→timezone 可确定） | 补确定修法 `tzinfo \| None`（归 D）；Self-Review 修正 |
| 5 | agent.py:644 收窄破坏 session_routes:536（str 调用存在） | 补 session_routes:529,536 `SessionId(sid)` 收口（归 B） |
| 6 | StrEnum 收口是新增 ValueError 边界，非"零变化" | 行为变化 §2 声明边界 |
| 7 | 重审补 4 处分类缺口（_lifecycle_services:136、session_routes:515、voice_sessions:484,500）；server.py session 明细 ×12→×11 | C/B 表补行；明细修正 |

## Self-Review（已执行）

1. **Spec 覆盖**：92 报错按文件 A-E 全覆盖（A 22 + B 18 + C ~31 + D 16 + E 2 ≈ 92）；
   门禁配置（§1）、修法（§2-6）、验证（§7）、行为声明、决策记录、review 修复记录——全部映射。
2. **占位符扫描**：无 TBD/TODO；time/plugin.py 已确定（`tzinfo | None`）；`_lifecycle_tools.py:104` 标"看调用点"——
   plan 阶段给确切修法（私有 _send_json 跨类访问，需定公开化或声明）。
3. **类型一致性**：A 类型占位、B 收口、C 收窄、D 参数放宽/cast——修法与 pyright 报错类型对应。
4. **已知耦合**：agent.py:644 收窄 ↔ session_routes:529,536 收口（原子）；ws_handler `_send_ack` 签名放宽 ↔
   20 调用点自动兼容；token_tracker 标注 `X | None` ↔ 调用方类型推断；门禁锁 env 是 CI/本地一致的前提。
