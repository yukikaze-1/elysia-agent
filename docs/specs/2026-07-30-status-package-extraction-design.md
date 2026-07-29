# status.py → core/status/ 包提取

> 2026-07-30 · 设计文档

## 动机

`AgentStatus` / `CognitivePhaseController` 两个 ABC 是纯接口（零依赖），但放在 `core/agent/` 内，消费者要从 agent 包导入与 Agent 无关的接口。提取到独立包 `core/status/`，文件结构直接表达"接口与实现分离、实现可替换"。

## 目标结构

```
core/status/
├── __init__.py       ← 导出 AgentStatus, CognitivePhaseController,
│                        FSMBackedStatus, FSMBackedPhaseController
├── protocol.py       ← AgentStatus (ABC) + CognitivePhaseController (ABC)
│                        纯接口，零依赖（仅 stdlib）
└── fsm.py            ← FSMBackedStatus + FSMBackedPhaseController
                        依赖 AgentFSM, EventBus, OutputManager, StateChangeData
```

`core/agent/status.py` — **删除**。

## 影响范围

### import 路径变更

| 文件 | 当前 import | 改为 |
|------|-----------|------|
| `core/agent/agent.py` | `from .status import AgentStatus, CognitivePhaseController` (TYPE_CHECKING) | `from core.status import AgentStatus, CognitivePhaseController` |
| `core/agent/agent.py` | `from .status import FSMBackedStatus, FSMBackedPhaseController` (runtime) | `from core.status import FSMBackedStatus, FSMBackedPhaseController` |
| `core/agent/__init__.py` | `from .status import AgentStatus, CognitivePhaseController` | `from core.status import AgentStatus, CognitivePhaseController` |
| `core/cognitive/loop.py` | `from ..agent.status import FSMBackedPhaseController` | `from core.status import FSMBackedPhaseController` |
| `core/cognitive/loop.py` | `from ..agent.status import FSMBackedStatus` | `from core.status import FSMBackedStatus` |
| `core/monitoring/idle_monitor.py` | `from core.agent.status import AgentStatus` | `from core.status import AgentStatus` |
| `core/monitoring/idle_monitor.py` | `from core.agent.status import FSMBackedStatus` | `from core.status import FSMBackedStatus` |
| `core/agent/tests/test_status.py` | `from core.agent.status import FSMBackedStatus` 等 ~20 处 | `from core.status import FSMBackedStatus` 等 |

### 文档更新

- `CLAUDE.md` — `status.py` → `status/` package
- `core/README.md` — 路径更新
- `docs/architecture.md` — 路径更新

## 不做

- 不改变任何类名、方法签名或行为
- 不移动 `core/agent/tests/test_status.py`（属于 agent 的测试，测试 FSM-backed 实现）

## 验证

```bash
# 确认旧文件已删除
test -f core/agent/status.py && echo "FAIL" || echo "OK"

# 确认新包存在
test -f core/status/__init__.py && test -f core/status/protocol.py && test -f core/status/fsm.py && echo "OK"

# 确认无残留的旧 import 路径
grep -rn "from.*agent\.status import\|from.*agent.status import" --include="*.py" | grep -v site-packages | grep -v __pycache__
# 预期空输出（桥接不留）

# 全量测试
make test-quick
```
