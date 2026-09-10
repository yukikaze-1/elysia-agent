# Input 层拆分 批次 A：错误信封下沉 errors.py（解绕环）

**文档版本：** v0.1
**文档状态：** 待评审
**日期：** 2026-08-14
**仓库：** `/home/yomu/elysia-agent`
**上游评估：** 会话记录「core/input 内聚性评估」（2026-08-14）问题 3
**前置决策：** AskUserQuestion 敲定 Q4（server.py 纯装配）+ Q3（错误信封→errors.py）

---

## 1. 背景

core/input 内聚性评估确认三个问题之一：跨切错误信封 `_protocol_error_response` 错放在
`core/input/server.py`，被 **97 处**跨模块调用，全部走函数级 `# 懒导入绕环`，制造两对
运行时循环：

```text
server.py ──(顶层 import)──→ api_routes / debug_routes（注册）
     ↑                              │
     └──(懒导入绕环)←── api_routes (16 处 _protocol_error_response)
     └──(懒导入绕环)←── debug_routes/{character,archive,ltm,__init__} (70 处)
```

懒导入虽然运行期无死锁，但：
- 掩盖真实依赖方向（错误信封是 input 层公共设施，被 4 个模块消费，却寄生在 server.py）；
- 函数内反复 `from core.input.server import ...` 是绕环的产物，一旦信封移走即可恢复
  正常顶层 eager import；
- 是后续批次 D（server.py 纯装配）的前置：server.py 纯化需先卸下信封职责。

## 2. 目标

- 消除 `server↔api_routes`、`server↔debug_routes` 两对懒导入绕环；
- 错误信封归位到专属模块，成为 input 层公共设施；
- **零行为变更**（纯移动 + import 路径更新，函数逻辑不变）。

## 3. 现状（事实基线）

| 项 | 值 |
|---|---|
| 函数 | `_protocol_error_response(code: str, message: str, status: int = 401, retryable: bool = False) -> web.Response` |
| 定义位置 | `core/input/server.py:150-166`（1 处定义 + 11 处 server 内部调用） |
| 依赖 | `frontend_contract.rest.ProtocolError`、aiohttp `web.json_response`、`uuid`（均非 server.py 内部符号；`ProtocolError` 仅被本函数使用） |
| 总出现 | 98 次：`debug_routes/character.py` 28、`archive.py` 26、`api_routes.py` 16、`ltm.py` 14、`server.py` 12、`debug_routes/__init__.py` 2 |
| 调用点 | **61 处**（不含定义、不含懒导入行）：character 14、api_routes 15、archive 13、ltm 7、server 11、`__init__` 1 |
| 懒导入行 | **36 行**：character 14、archive 13、ltm 7、api_routes 1、`__init__` 1（server 无——定义于本地） |
| 绕环 import | `api_routes` + `debug_routes` 均函数内 `from core.input.server import _protocol_error_response`（懒导入，36 行） |

## 4. 方案

### 4.1 新建 `core/input/errors.py`

从 `server.py:150-166` 整体移动 `_protocol_error_response`（函数体零改动），模块自包含：

```python
"""input 层统一错误信封响应构造器。

_protocol_error_response 原寄生于 core/input/server.py，被 api_routes/debug_routes 经
函数级懒导入绕环访问（2026-08-14 批次 A 下沉）。errors.py 仅依赖 frontend_contract +
aiohttp，不依赖 server.py——调用方改顶层 eager import 即可消除绕环。
"""
```

顶部 import：`from frontend_contract.rest import ProtocolError` + `from aiohttp import web`
+ `import uuid`。

### 4.2 调用点改顶层 eager import

| 文件 | 改动 |
|---|---|
| `core/input/server.py` | 删除本地定义（150-166）与顶部 `:28` 的 `ProtocolError` import（移走后仅剩死引用）；顶部 `from core.input.errors import _protocol_error_response`（errors 无环，server 顶部 eager 安全）；内部 11 处调用不变 |
| `core/input/api_routes.py` | 顶部 `from core.input.errors import _protocol_error_response`；删除函数内 **1 处**懒导入行（15 处调用不变） |
| `core/input/debug_routes/character.py` | 顶部 import；删除函数内 **14 处**懒导入行（14 处调用不变） |
| `core/input/debug_routes/archive.py` | 顶部 import；删除函数内 **13 处**懒导入行（13 处调用不变） |
| `core/input/debug_routes/ltm.py` | 顶部 import；删除函数内 **7 处**懒导入行（7 处调用不变） |
| `core/input/debug_routes/__init__.py` | 顶部 import；删除函数内 **1 处**懒导入行（1 处调用不变） |

调用点函数名保持 `_protocol_error_response` 不变（最小 diff）。

## 5. 决策记录

| 决策 | 选择 | 理由 |
|---|---|---|
| D-A1 是否公开化函数名 | **保持私有名 `_protocol_error_response`** | 本批核心是解绕环；改名会让 97 处调用 + review diff 膨胀，属附加语义清理，留作可选后续 |
| D-A2 模块命名 | `core/input/errors.py` | Q4 敲定时「信封→errors.py」；语义明确（错误信封专属） |
| D-A3 调用点 import 方式 | **顶部 eager import** | errors.py 无环，懒导入是绕环产物，应随移走消除；行为等价（import 时机提前但模块无副作用） |

## 6. 波及面

- 新增：`core/input/errors.py`
- 修改：`core/input/server.py`、`core/input/api_routes.py`、`core/input/debug_routes/{character,archive,ltm,__init__}.py`
- 测试：`core/input/tests/` 全目录经 grep 核实**无任何 mock `_protocol_error_response`**（均走 HTTP 断言信封行为），移动后零测试改动
- **无**：契约、wire 行为、错误码（函数逻辑不变）

## 7. 验收标准

1. `make test-quick`（unit+service 子集）全绿 + `make test-full`（全量 3548 passed 基线）全绿；
2. `grep -rn "from core.input.server import _protocol_error_response" core/ --include="*.py"` 为空；
3. `grep -rn "from core.input.server import" core/input/ --include="*.py"`（排除 tests/）为空——生产代码不再有 server 懒导入，`server↔api_routes`、`server↔debug_routes` 绕环消除；
4. ruff + pyright 全绿（`core/input/errors.py` 纳入门禁）。

## 8. 风险

- **低**：纯移动 + import 路径更新，函数逻辑零改动；3548 测试为行为锁定安全网；
- 注意点：server.py 内部 11 处调用（+1 处定义）改从 errors import 后，确认 `_protocol_error_response`
  不再在 server.py 作用域内定义（避免 shadow）；`:28` 的 `ProtocolError` import 需随定义删除（死代码）；ruff 可能对顶部新 import 顺序报 isort
  （本仓未启用 isort 排序，忽略）。

## 9. 后续衔接

- 批次 D（server.py 纯装配）将以本批的 `errors.py` 为中间件错误响应基础；
- 若后续希望公开化，可将 `_protocol_error_response` → `protocol_error_response` 作为
  独立小批次（改名 61 处调用，机械替换）。

## 10. Review 记录（2026-08-14）

- code-review 第 1 轮（自洽性 agent）：6 issues 全部修复——调用点计数改「总出现 98 / 调用 61 /
  懒导入 36」三列（§3）、server 内部 11 处调用 + 1 处定义、`server.py:28` 的 `ProtocolError`
  死 import 随定义删除（§4.2）、验收 1 明确 test-full 全量基线（§7）、验收 3 落成可执行 grep
  （§7）、测试段收紧为零改动核实（§6）。
- **第 2/3/4/5 个 review agents（事实核对 ×2、风险遗漏 ×2）在本环境均未在 10 分钟内返回（卡住），
  未获得其独立发现。** spec review 判定基于第 1 轮 agent（覆盖自洽 + 部分事实）+
  主审查对代码的自我事实核对（调用点计数逐文件验证、死 import 核实、`core/agent/server.py`
  的 `create_app` import 为合法入口引用不在绕环范围、`errors.py` 依赖面零环）。异常记录于此
  （goal 执行记录），后续批次 review 采用更轻量/单 agent 模式。
