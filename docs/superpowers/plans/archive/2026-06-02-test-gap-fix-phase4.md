# Phase 4: 测试缺口修复 — Database / Config / Logger / Prompt 模块

> **Status: COMPLETE** — 2026-06-02, 5 tasks done, 48 tests passing.
> Commits: `a82f501` `aff2faa` `36b7fd8` `6e783a0` `8b4cca1`

**Goal:** 补充 VectorDB/DocumentDB 的协议抽象强制/连接失败/零结果测试 + Config 深度嵌套/畸形 YAML 测试 + Logger 轮转/并发测试 + Prompt 未测试宏/auto_reload 测试

**Architecture:** 各模块追加测试到已有 `test_*.py` 文件

**Tech Stack:** Python 3.13, pytest, asyncio, unittest.mock

**Spec:** [2026-06-02-test-audit-design.md](../specs/2026-06-02-test-audit-design.md) 第十至十三节

---

## File Structure

```
Module/database/vectordb/test_vectordb.py    # 追加: 抽象强制(5方法), index_preset, 空结果, 连接失败
Module/database/documentdb/test_documentdb.py  # 追加: 抽象强制(6方法), 零结果, upsert, 连接失败
Module/config/test_config.py                   # 追加: 约束describe, 深度嵌套, 畸形YAML, Optional[X]
Module/logger/test_logger.py                   # 追加: CRITICAL, get_caller_logger, 轮转, 并发
Module/prompt/test_prompt.py                   # 追加: Utils.j2宏, auto_reload行为, UndefinedError
```

---

### Task 1: Module/database/vectordb/test_vectordb.py 追加 — 协议抽象 / index_preset / 连接失败

**Files:**
- Modify: `Module/database/vectordb/test_vectordb.py` — 追加
- Read first: `Module/database/vectordb/protocol.py`, `Module/database/vectordb/milvus.py`

**Test scenarios:**

#### A. 协议抽象强制——补 5 个缺失方法
已有测试仅验证 `create_collection` 和 `insert`。需追加：
- `drop_collection` — 子类不实现 → 实例化抛 `TypeError`
- `collection_exists` — 同上
- `search` — 同上
- `query` — 同上
- `close` — 同上

#### B. index_preset 非默认值
- `create_collection(..., index_preset="cosine")` → 验证 `metric_type="COSINE"`
- `create_collection(..., index_preset="ip")` → 验证 `metric_type="IP"`
- `create_collection(..., index_preset="hnsw_l2")` → 验证 HNSW 索引参数

#### C. metric_type 显式覆盖
- `create_collection(..., index_preset="default", metric_type="IP")` → 验证 `metric_type` 参数覆盖 preset 默认值

#### D. 空结果 / 无 output_fields
- `search()` mock `client.search()` 返回 `[]` (空列表) → 验证 `results[0] if results else []` 的 else 分支返回 `[]`
- `search()` 不传 `output_fields` → 验证请求参数中不含 `output_fields` key
- `query()` 不传 `output_fields` → 同上
- `query()` 默认 `limit=10000` → 验证未传 limit 时代码使用默认值

#### E. 连接失败
- mock `MilvusClient` 的 `search` 抛 `ConnectionError` → `asyncio.to_thread` 传播异常 → 验证不包装
- `insert` 时连接失败 → 同上
- `create_collection` 时连接失败 → 同上

**运行:** `conda run -n Elysia python -m pytest Module/database/vectordb/test_vectordb.py -v`

---

### Task 2: Module/database/documentdb/test_documentdb.py 追加 — 协议抽象 / 零结果 / 连接失败

**Files:**
- Modify: `Module/database/documentdb/test_documentdb.py` — 追加
- Read first: `Module/database/documentdb/protocol.py`, `Module/database/documentdb/mongodb.py`

**Test scenarios:**

#### A. 协议抽象强制——补 6 个缺失方法
已有测试仅验证 `insert_one`。需追加：
- `insert_many` / `find` / `update_one` / `delete_many` / `count` / `ensure_index` / `close`
→ 每个方法的抽象检查：子类不实现 → 抛 `TypeError`

#### B. find() 默认参数
- `find({})` 不传 sort/skip/limit → 验证生成的 cursor 不调用 `.sort()` / `.skip()` / `.limit()` (mongodb.py:53-57)

#### C. 零结果路径
- `update_one()` → mock `modified_count = 0` → 验证不抛异常
- `delete_many()` → mock `deleted_count = 0` → 验证不抛异常
- `count()` → mock `count_documents` 返回 `0` → 验证返回 `0`
- `find()` → mock `to_list` 返回 `[]` → 验证返回空列表
- `count()` 传 `{}` (空 filter = count all) → 验证 filter 被正确传递

#### D. upsert / kwargs
- `update_one({}, {}, upsert=True)` → 验证 upsert 参数传递到 `update_one()`
- `ensure_index("field", unique=True, sparse=True, expireAfterSeconds=3600)` → 验证所有 kwargs 透传

#### E. _id 字符串转换
- `find()` mock 返回含 `_id: ObjectId(...)` 的文档 → 验证 `_id` 被转换为 `str`

#### F. 连接失败
- mock `AsyncIOMotorClient` 操作抛 `ServerSelectionTimeoutError` → 验证异常传播
- `insert_one` 时连接失败 → 同上
- `close()` 在已关闭的 client 上调用 → 验证不抛异常

**运行:** `conda run -n Elysia python -m pytest Module/database/documentdb/test_documentdb.py -v`

---

### Task 3: Module/config/test_config.py 追加 — 约束 / 深度嵌套 / 畸形 YAML / Optional

**Files:**
- Modify: `Module/config/test_config.py` — 追加
- Read first: `Module/config/validator.py`, `Module/config/loader.py`, `Module/config/field.py`

**Test scenarios:**

#### A. 约束 describe()
- `Range(min=1, max=10).describe()` → 验证返回字符串含 "1" 和 "10"
- `Choices(["a", "b"]).describe()` → 验证返回字符串含 "a" 和 "b"
- `Regex(r"\d+").describe()` → 验证返回字符串含正则模式

#### B. Choices 空列表 —— 构造守卫
- `Choices([])` → 验证 `__init__` 抛 `ValueError`

#### C. Range 单边界
- `Range(min=1).check(0)` → 验证抛 `ConfigConstraintError`，msg 含 ">="
- `Range(max=10).check(11)` → 验证抛 `ConfigConstraintError`，msg 含 "<="
- `Range().check(999)` → `min=max=None` → 验证无约束时不抛异常

#### D. Regex 自定义 message / 非字符串
- `Regex(r"\d+", message="必须为数字").check("abc")` → 验证错误消息是自定义 message
- `Regex(r"\d+").check(123)` → 非字符串 → 验证错误消息不是值的 repr

#### E. 深度嵌套
- 3 层嵌套 dataclass (A → B → C)，YAML 有所有层级的值 → 验证正确合并
- 嵌套 dataclass 的 YAML 值不是 dict（如 scalar）→ 验证不崩溃

#### F. EnvManager Optional[X]
- `Optional[int]` 类型解包：`EnvManager.coerce("42", Optional[int])` → 验证解析为 `int` → 返回 `42`
- `Optional[str]` 类型解包

#### G. 畸形 YAML / root 非 dict
- YAML 文件包含语法错误 → 验证 `yaml.YAMLError` 行为
- YAML 文件 root 是 list → 验证 `_load_yaml` 返回 `{}`
- 多文档 YAML → 验证只取第一个文档

#### H. yaml_key 多级路径
- `yaml_key="A.B.C"` → YAML 文件含 `{A: {B: {C: {...}}}}` → 验证正确读取嵌套
- `yaml_key="A.X"` → `A` 存在但 `X` 不存在 → 验证 fallback 行为

#### I. 空字符串 env var
- `PORT=""` 对 int 字段 → `EnvManager.coerce("", int)` → 验证异常处理
- `MY_KEY=""` 对 secret required 字段 → 验证空字符串是否被视为有效值

**运行:** `conda run -n Elysia python -m pytest Module/config/test_config.py -v`

---

### Task 4: Module/logger/test_logger.py 追加 — CRITICAL / get_caller_logger / 并发

**Files:**
- Modify: `Module/logger/test_logger.py` — 追加
- Read first: `Module/logger/factory.py`

**Test scenarios:**

#### A. CRITICAL 级别
- `LoggerFactory(level="CRITICAL")` → 验证 level 被正确设置
- `lf.get("test")` → `test.critical("msg")` → 验证消息输出
- `set_level("CRITICAL")` → 验证运行时切换

#### B. get_caller_logger()
- 在 `Module/llm/registry.py` 场景模拟：调用 `get_caller_logger()` → 验证返回的 logger name 是调用者所在模块
- 从 `__main__` 调用 → 验证返回 root logger

#### C. 日志轮转行为
- 验证 `TimedRotatingFileHandler` 被创建并配置了 `when="midnight"` 和 `backupCount=30`
- 自定义 `backup_count` → 验证传到 handler

#### D. JSON 格式
- `_JsonFormatter.format(record)` 含 Unicode → `ensure_ascii=False` → 验证中文字符正确输出
- 含双引号/反斜杠/换行的日志消息 → 验证 JSON 合法
- `extra` 字段 → 验证是否被包含在 JSON 中

#### E. 并发
- 多线程同时调用 `lf.get("name")` → 验证 `_created` set 安全、不创建重复 handler
- 多线程同时 `lf.set_level(name, level)` → 验证不崩溃

**运行:** `conda run -n Elysia python -m pytest Module/logger/test_logger.py -v`

---

### Task 5: Module/prompt/test_prompt.py 追加 — 宏 / auto_reload / UndefinedError

**Files:**
- Modify: `Module/prompt/test_prompt.py` — 追加
- Read first: `Module/prompt/manager.py`
- Read: `prompt/Utils.j2`, `prompt/Brain.j2`, `prompt/MicroReflector.j2`, `prompt/MacroReflector.j2`

**Test scenarios:**

#### A. Utils.j2 三个宏——全部零覆盖
- `FormatHistory(recent_conversations)` — 传入模拟对话历史，验证输出格式
- `FormatMicroMemories(memories)` — 传入模拟记忆列表，验证输出
- `FormatMacroMemories(memories)` — 同上

#### B. Brain.j2 中的 4 个未测宏
- `L0SensoryBlockPrompt(sensory)` — 传入模拟感知数据
- `L2MemoryBlockPrompt(memory)` — 传入模拟记忆数据
- `CurrentStatePrompt(state)` — 传入模拟状态数据
- `ToolUsageGuidance()` — 无参数，验证输出非空字符串

#### C. MicroReflector / MacroReflector UserPrompt 宏——零覆盖
- `MicroReflectorUserPrompt(messages)` — 传入模拟消息，验证输出
- `MacroReflectorUserPrompt(messages)` — 同上

#### D. auto_reload 行为验证
- `auto_reload=False` — 渲染模板 → 修改模板文件 → 再次渲染 → 验证输出不变（缓存）
- `auto_reload=True` — 渲染模板 → 修改模板文件 → 再次渲染 → 验证输出更新

#### E. datetime filter 自定义格式
- `{{ ts | datetime('%Y-%m-%d') }}` → 验证格式为 `2026-06-02`
- `{{ ts | datetime('%H:%M:%S') }}` → 验证只输出时间部分
- 非 float 字符串 timestamp `"1717334400.0"` → `float(value)` 转换

#### F. 模板错误 / 空 context
- 模板含 `{{ undefined_var }}` 但未传入 → `jinja2.UndefinedError` → 验证被 `except Exception` 捕获 → 包装为 `RuntimeError` 并记录日志
- `render("template.j2")` 无 kwargs → 模板无变量 → 正常渲染
- `render("template.j2", name=None)` → 变量值为 None → 验证渲染结果

#### G. 空模板文件
- `.j2` 文件内容为空 → `render()` → 验证返回 ""

**运行:** `conda run -n Elysia python -m pytest Module/prompt/test_prompt.py -v`

---

## 验证

```bash
conda run -n Elysia python -m pytest \
  Module/database/vectordb/test_vectordb.py \
  Module/database/documentdb/test_documentdb.py \
  Module/config/test_config.py \
  Module/logger/test_logger.py \
  Module/prompt/test_prompt.py -v
```
