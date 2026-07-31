# 项目目录整理设计

> 日期: 2026-06-18  
> 范围: 文件位置整理 + 日志清理，不动代码

## 目标

整理项目顶层和 `core/` 中散放的文件，使目录结构一致、清晰。不修改任何 Python 代码。

## 1. `core/` 测试文件归位

将 3 个混在生产代码中的测试文件移入 `core/tests/`：

```
core/test_prompt_recorder.py  →  core/tests/test_prompt_recorder.py
core/test_timing.py           →  core/tests/test_timing.py
core/test_token_tracker.py    →  core/tests/test_token_tracker.py
```

- 这 3 个文件使用 `from core.xxx` 绝对导入，移动后无需修改 import
- 移动后 `core/tests/` 从 7 个 test_*.py 增至 10 个

## 2. 顶层文件归类

| 文件 | 处理 | 原因 |
|------|------|------|
| `code-review-report.md` | → `docs/code-review-report.md` | 属于项目文档 |
| `app_log.txt` | → `log/app.log` | 和已有日志统一管理 |

`todo.md` 保留顶层不动。

## 3. 日志清理

删除 `log/` 下 17 个零字节文件：

```
logger_CloseSet.log, logger_CloseTest.log, logger_concurrent_test.log,
logger_ExactName.log, logger_IntLevel.log, logger_L1.BrainLayer.log,
logger_LoggerA.log, logger_LoggerB.log, logger_NoDup.log,
logger_RuntimeLevel.log, logger_SameName.log, logger_SensorLayer.log,
logger_test_crit_set.log, logger_TestInfo.log, logger_TestModule.log
```

这些是 LoggerFactory 单元测试残留，`log/` 已在 `.gitignore` 中。

## 4. 不变项

所有 `.py` 文件、`pytest.ini`、`Makefile`、`.env`、配置、`ref/`、`data/`、`live2d/`、`prompt/`、`static/`、`character/`、`docs/`、`Module/` 结构均不动。

## 验证

```bash
# 测试文件移动到 core/tests/ 后仍可正常运行
python -m pytest core/tests/test_prompt_recorder.py core/tests/test_timing.py core/tests/test_token_tracker.py -v

# 全文测试无回归
python -m pytest core/tests/ -v
```
