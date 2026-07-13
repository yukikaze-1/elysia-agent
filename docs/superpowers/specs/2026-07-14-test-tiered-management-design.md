# 测试分层管理 — 设计 spec

## 背景

当前项目 63 个测试文件、1558 个测试，全量跑 ~22 分钟。`pytest.ini` 只有一个 `integration` marker。没有分层/分组策略，改了代码不知道该跑哪些测试、全量跑太久、测试散落在逐步增长的历史文件中。

## 目标

1. **分层运行** — 日常改完代码秒级验证（~30s），提交前中等验证（~5min），推之前全量
2. **改动定位** — 改了 `core/` 的某个文件，能快速确定要跑哪些测试
3. **质量清理** — 逐测试审查，清理过时/无效测试

## 四层 markers

在 `pytest.ini` 注册 4 个 markers：

```ini
[pytest]
markers =
    unit:        纯函数/类测试，无 I/O，无 Agent 启动，无外部服务 mock
    service:     单模块测试，可能需要 mock，但不启动完整 Agent/不连真实 DB
    integration: 需要真实数据库（MongoDB/Milvus）或外部 API
    slow:        完整管线、端到端、或单测耗时 >1s
```

| 层 | 标准 | 预计占比 | 累计耗时 |
|---|------|----------|----------|
| `unit` | 纯函数/类，无 I/O，无 Agent | ~40% | ~5s |
| `service` | 单模块 + mock | ~40% | ~30s |
| `integration` | 需 DB 或外部服务 | ~15% | ~5min |
| `slow` | 完整 Agent 启动/端到端 | ~5% | ~10min |

## 日常使用

```bash
# 日常开发 — 秒级
pytest -m "unit or service" -q -x --tb=short

# 提交前 — 跑除 slow 外的所有
pytest -m "not slow" -q --tb=short

# 推之前
pytest -q

# 改了 core/memory/ → 只跑相关
pytest core/memory/tests/ -q
```

## Review 标准

每个测试打 marker 时，从 4 个维度审查：

1. **有效性** — 测试是否仍在测有意义的东西？被测代码已删除的残留测试？
2. **正确性** — 断言是否匹配当前代码行为？mock/setup 是否正确？
3. **归属** — 文件名/位置是否合理？`test_phase1/2/3.py` 里的测试是否该拆分？
4. **分层** — 真正的级别是什么？有无不必要的重量级 setup？

### 处理规则

- 过时/无效测试 → 当场删除或标记为后续 repair
- 断言不正确 → 当场修复（小改动）或记录到 todo（需要深入理解的大改动）
- 归属不合理 → 记录到 todo，不在本次迁移中做文件拆分

## 分批实施

分 10 批，每批 commit 一次：

| 批次 | 范围 | 文件数 | 预计 marker 分布 |
|------|------|--------|-------------------|
| 1 | `Module/_common/` + `Module/config/` + `Module/logger/` | 6 | unit |
| 2 | `Module/database/` + `Module/embedding/` | 6 | unit + service |
| 3 | `Module/llm/` + `Module/stt/` + `Module/tts/` | 6 | service |
| 4 | `Module/input/` + `Module/output/` | 4 | unit + service |
| 5 | `Module/system_prompt/` | 6 | unit + integration |
| 6 | `Module/tool/` | 2 | unit + service |
| 7 | `Module/task_system/` | 16 | unit + integration |
| 8 | `core/tools/` | 8 | unit + service |
| 9 | `core/memory/` + `core/personality/` | 6 | unit + service |
| 10 | `core/tests/` | 10 | service + integration + slow |

### 第 1 批试点

先完成 `Module/_common/` + `Module/config/` + `Module/logger/`（6 文件），确认 review 深度和产出一致，再继续。

## 产出

1. **`pytest.ini`** — 注册 4 个 markers
2. **所有测试文件** — 每个测试函数加 `@pytest.mark.xxx` 装饰器
3. **`CLAUDE.md`** — 测试命令改为分层体系
4. **`Makefile`** — 新增 `test-quick`、`test-normal`、`test-full` targets
5. **`todo.md`** — 过时测试、归属不合理、需修复的断言记录
