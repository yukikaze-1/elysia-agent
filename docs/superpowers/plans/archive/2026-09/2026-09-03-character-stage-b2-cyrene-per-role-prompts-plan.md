# 阶段 B-2：Cyrene 完整角色 + Per-Role 模板机制 + 辅助 Prompt 参数化 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 引入 per-role 模板机制，新建完整 Cyrene 角色，辅助 Prompt 去 Elysia 化，翻转 Stage A 泄漏测试的 C2/C3 角色风格词。Elysia 现网渲染 byte-for-byte 不变。

**Architecture:** `PromptAssembler` 按 `character.name.lower()` 拼 `templates/{role}/cX.j2` 路径（存在用角色模板，否则回退共享）。C2/C3/C7 per-role；F2/F3 保持共享（♪/她是格式示例）。辅助 Prompt 用占位符（`{character_name}`/`{user_name}`）或去 Elysia 化。Cyrene 内容由 subagent 参照 `character/cyrene/reference/` 生成。

**Tech Stack:** Python 3.12 + Jinja2 + Pydantic + PyYAML + pytest（conda Elysia env）。

**Spec:** `docs/superpowers/specs/archive/2026-09/2026-09-03-character-stage-b2-cyrene-per-role-prompts-design.md`

## Global Constraints

- **Elysia byte-for-byte 不变**：`test_elysia_snapshot_unchanged`（Elysia 渲染 == `system_prompt_snapshot_elysia_cf4bc008.txt`）必须保持绿。`elysia_config` 渲染不得改变。
- **禁相对导入**：`from core.xxx` / `from Module.xxx`，不用 `from .xxx`（`make lint` TID252，`__init__.py`/`__main__.py` 豁免）。
- **生产代码函数参数必须类型注解**（`make lint` ANN001/002/003；测试文件豁免）。
- **conda env**：`conda run -n Elysia` 跑测试/pyright。
- **测试 marker**：新测试必须加 `unit`/`service`（无 I/O 用 unit，mock 用 service）。
- **README 中文**、代码注释中文（docstring 同理，中英混合可接受）。
- **Cyrene 内容创作指南**（spec §3）：Cyrene 的 C2/C3/C7 + speaking_style 必须不含 spec §3.3 禁止词（`普遍之爱`/`制造暧昧`/`初识`/`傲娇`/`今天不想振作也没有关系`/`Elysia`/`爱莉`/`妖梦` 等），且体现 spec §3.2 差异（爱观=铭记/陪伴；不喜欢告别）。
- **`test_snapshot_matches_current`（cold 快照 `e793a782`）不得破坏**：cold 走回退共享 C2/C3（Elysia 原文），cold 渲染保持现状（含 Elysia 词），快照不变。
- **`_refresh_c6` 首刷**读 `character.speaking_style`（`or _C6_L1_DEFAULT`），`compression.py:48` annotation 收紧为 `CharacterConfig`。
- **不改 `speaking_style.py` 的 `_C6_L1_DEFAULT` 常量**（Elysia L1 种子保留）。

---

### Task 1: `PromptAssembler` 加 `_resolve_template` 选路 + per-role 模板目录

**Files:**
- Modify: `core/prompt/prompt_assembler.py:37`（`_SECTION` 后加 `from pathlib import Path`；构造 section 用 `_resolve_template`）
- Create: `Module/system_prompt/templates/elysia/c2_relationship.j2`（复制共享）
- Create: `Module/system_prompt/templates/elysia/c3_emotion.j2`（复制共享）
- Create: `Module/system_prompt/templates/elysia/c7_proactive.j2`（复制共享）
- Create: `Module/system_prompt/templates/cyrene/c2_relationship.j2`（占位，Task 4 填充）
- Create: `Module/system_prompt/templates/cyrene/c3_emotion.j2`（占位，Task 4 填充）
- Create: `Module/system_prompt/templates/cyrene/c7_proactive.j2`（占位，Task 4 填充）
- Test: `core/prompt/tests/test_prompt_assembler.py`（补充）

**Interfaces:**
- Consumes: `PromptAssembler.__init__` 的 `character: CharacterConfig`（已有 `self._character`）。
- Produces: `PromptAssembler._resolve_template(name: str) -> str`——返回角色目录路径或共享路径。

- [ ] **Step 1: 写失败测试**

```python
# core/prompt/tests/test_prompt_assembler.py (追加)
from core.prompt.tests.conftest import cold_config, elysia_config, make_assembler, mock_long_term, mock_session  # noqa


def test_resolve_template_uses_role_dir(elysia_config, mock_long_term, mock_session):
    """角色名有角色目录时用 per-role 路径。"""
    from core.prompt.prompt_assembler import PromptAssembler
    a = PromptAssembler(elysia_config, mock_session, mock_long_term)
    path = a._resolve_template("c2_relationship.j2")
    assert "elysia" in path and path.endswith("c2_relationship.j2")


def test_resolve_template_falls_back_to_shared(cold_config, mock_long_term, mock_session):
    """无角色目录时回退共享路径。"""
    from core.prompt.prompt_assembler import PromptAssembler
    a = PromptAssembler(cold_config, mock_session, mock_long_term)
    path = a._resolve_template("c2_relationship.j2")
    assert "/templates/c2_relationship.j2" in path
```

- [ ] **Step 2: 运行测试确认失败**

Run: `conda run -n Elysia python -m pytest core/prompt/tests/test_prompt_assembler.py -v`
Expected: FAIL — `PromptAssembler` 无 `_resolve_template` 属性。

- [ ] **Step 3: 实现 `_resolve_template` + 构造 section 用选路**

```python
# core/prompt/prompt_assembler.py —— 加 import
from pathlib import Path  # 顶部 import 区

# 类内加方法
def _resolve_template(self, name: str) -> str:
    """按角色名解析模板路径：角色目录存在则用，否则回退共享。"""
    role_dir = f"{_SECTION}/{self._character.name.lower()}/{name}"
    if Path(role_dir).exists():
        return role_dir
    return f"{_SECTION}/{name}"
```

构造 section 处改用选路（C2/C3/C7 + C6 用 character.speaking_style 不选路；C6 仍共享模板 + 变量）：

```python
# sections 里改这三处
self._emotion_section = EmotionSection(self._resolve_template("c3_emotion.j2"), session)
RelationshipSection(self._resolve_template("c2_relationship.j2"), character.name),
ProactiveSection(self._resolve_template("c7_proactive.j2")),
```

- [ ] **Step 4: 复制 Elysia 字节相同副本**

```bash
# 复制共享模板到 elysia/（字节相同）
cp Module/system_prompt/templates/c2_relationship.j2 Module/system_prompt/templates/elysia/c2_relationship.j2
cp Module/system_prompt/templates/c3_emotion.j2 Module/system_prompt/templates/elysia/c3_emotion.j2
cp Module/system_prompt/templates/c7_proactive.j2 Module/system_prompt/templates/elysia/c7_proactive.j2
```

- [ ] **Step 5: 运行测试确认通过**

Run: `conda run -n Elysia python -m pytest core/prompt/tests/test_prompt_assembler.py core/prompt/tests/test_system_prompt_snapshot.py -v`
Expected: PASS（选路/回退通过；Elysia 快照因字节相同不变）。

- [ ] **Step 6: Commit**

```bash
git add core/prompt/prompt_assembler.py Module/system_prompt/templates/elysia/ Module/system_prompt/templates/cyrene/ core/prompt/tests/test_prompt_assembler.py docs/superpowers/plans/archive/2026-09/2026-09-03-character-stage-b2-cyrene-per-role-prompts-plan.md
git commit -m "feat(stage-b2): _resolve_template per-role 选路 + elysia/cyrene 模板目录骨架"
```

---

### Task 2: `templates/elysia/*` 字节相同校验测试

**Files:**
- Test: `Module/system_prompt/tests/unit/test_sections/test_boundary.py`（追加）
- Test: `core/prompt/tests/test_system_prompt_snapshot.py`（补充 `test_elysia_templates_byte_identical`）

**Interfaces:**
- Consumes: `templates/elysia/*` 与 `templates/*`（Task 1 创建）。
- Produces: 字节相同校验测试——保证 Elysia 副本与共享原版不漂移。

- [ ] **Step 1: 写失败测试**

```python
# core/prompt/tests/test_system_prompt_snapshot.py (追加)
def test_elysia_templates_byte_identical():
    """templates/elysia/* 与共享原版字节相同（Elysia byte-for-byte 保护）。"""
    base = Path("Module/system_prompt/templates")
    for name in ["c2_relationship.j2", "c3_emotion.j2", "c7_proactive.j2"]:
        shared = (base / name).read_bytes()
        elysia = (base / "elysia" / name).read_bytes()
        assert elysia == shared, f"{name} elysia 副本 != 共享原版"
```

- [ ] **Step 2: 运行确认通过（Task 1 已复制字节相同）**

Run: `conda run -n Elysia python -m pytest core/prompt/tests/test_system_prompt_snapshot.py::test_elysia_templates_byte_identical -v`
Expected: PASS（Task 1 用 `cp` 复制字节相同）。

- [ ] **Step 3: Commit**

```bash
git add core/prompt/tests/test_system_prompt_snapshot.py
git commit -m "test(stage-b2): templates/elysia 字节相同校验（byte-for-byte 保护）"
```

---

### Task 3: Cyrene CharacterConfig + personality.md + speaking_style

**Files:**
- Create: `character/cyrene/config.yaml`（name/user_pronoun/wake_words/emotion/proactive/`personality.speaking_style`）
- Create: `character/cyrene/personality.md`（L1 Core，来自 spec §3 角色内核，参照 `reference/`）
- Test: `core/prompt/tests/conftest.py`（补 `cyrene_config` 夹具）
- Test: `core/prompt/tests/test_system_prompt_snapshot.py`（补 `test_cyrene_config_speaking_style_nonempty`）

**Interfaces:**
- Consumes: `CharacterConfig.from_yaml`（`core/config/character_config.py`）。
- Produces: `cyrene_config` 夹具（`CharacterConfig`，name="Cyrene"）；`character/cyrene/config.yaml` 可加载。

- [ ] **Step 1: 写失败测试**

```python
# core/prompt/tests/test_system_prompt_snapshot.py (追加)
def test_cyrene_config_speaking_style_nonempty():
    """Cyrene config.yaml 的 speaking_style 非空（per-role C6 有内容）。"""
    from core.config.character_config import CharacterConfig
    cfg = CharacterConfig.from_yaml(Path("character/cyrene/config.yaml"))
    assert cfg.speaking_style.strip(), "Cyrene speaking_style 不能为空"
```

- [ ] **Step 2: 运行测试确认失败**

Run: `conda run -n Elysia python -m pytest core/prompt/tests/test_system_prompt_snapshot.py::test_cyrene_config_speaking_style_nonempty -v`
Expected: FAIL — `character/cyrene/config.yaml` 不存在（FileNotFoundError）。

- [ ] **Step 3: 建 Cyrene config.yaml**

```yaml
# ============================================================
# Cyrene 角色配置（昔涟，《崩坏：星穹铁道》翁法罗斯「记忆」命途少女）
# ============================================================
# 被 PersonalityEngine、EmotionEngine、IdleMonitor、TTS、STT 消费
# 性格描述加载自 personality.md（L1 Core）

# ---- 基本标识 ----
name: Cyrene              # 角色名（全局引用）
user_pronoun: 你          # 用户人称代词（注入 system prompt）

# ---- 语音（待启用） ----
voice: cyrene_v2          # [占位] TTS voice ID

# ---- 性格 ----
base_personality_file: personality.md           # L1 Core 文本（相对于本文件目录）

personality:
  speaking_style: |-
    [Cyrene 的 C6 说话风格——由 subagent 参照 reference 生成，见 Task 4 Step 3 示例。须不含 §3.3 禁止词，体现 spec §3.2 差异。]

# ---- 外观（待启用） ----
appearance:
  height: "162cm"
  hair: "粉色长发"
  eyes: "粉红色"

# ---- 情绪引擎 ----
emotion:
  thresholds:
    excitement: 0.6
    sadness: 0.3

# ---- 主动行为（IdleMonitor 驱动） ----
proactive:
  enabled: false
  interval: 300
  triggers:
    - type: silence_timeout
      duration: 30

# ---- 唤醒词 ----
wake_words:
  - Cyrene
  - 昔涟
```

> ⚠️ **Task 3 Step 3 的 speaking_style 用占位符**，Task 4 Step 3 由 subagent 填充实际内容。**Task 3 结束时该字段是占位符**——但 `test_cyrene_config_speaking_style_nonempty` 会红（占位符也是非空字符串，`cfg.speaking_style.strip()` 非空 → 通过）。**若要 Task 3 独立通过，占位符本身算非空。**

- [ ] **Step 4: 建 Cyrene personality.md（L1 Core 骨架）**

```markdown
# Cyrene 角色人格（L1 Core）

[Cyrene 的 L1 人格——由 subagent 参照 character/cyrene/reference/*.md 生成，见 Task 4 Step 3。]
[须体现 spec §3.1 角色内核：爱=铭记/理解/陪伴/创造未来；外显活泼甜美、内里温柔坚韧；不喜欢告别。]
```

- [ ] **Step 5: conftest 补 cyrene_config 夹具**

```python
# core/prompt/tests/conftest.py (追加)
@pytest.fixture
def cyrene_config() -> CharacterConfig:
    """构造 Cyrene 正角色 CharacterConfig（speaking_style 从 config.yaml 读）。"""
    from core.config.character_config import CharacterConfig
    return CharacterConfig.from_yaml(Path("character/cyrene/config.yaml"))
```

- [ ] **Step 6: 运行测试确认通过**

Run: `conda run -n Elysia python -m pytest core/prompt/tests/test_system_prompt_snapshot.py::test_cyrene_config_speaking_style_nonempty -v`
Expected: PASS（speaking_style 占位符非空）。

- [ ] **Step 7: Commit**

```bash
git add character/cyrene/ core/prompt/tests/conftest.py core/prompt/tests/test_system_prompt_snapshot.py
git commit -m "feat(stage-b2): Cyrene config.yaml + personality.md + speaking_style（骨架）"
```

---

### Task 4: Cyrene per-role C2/C3/C7 内容创作（subagent 生成）

**Files:**
- Modify: `Module/system_prompt/templates/cyrene/c2_relationship.j2`（Task 1 占位 → 填充 Cyrene 关系观）
- Modify: `Module/system_prompt/templates/cyrene/c3_emotion.j2`（Task 1 占位 → 填充 Cyrene 情绪响应）
- Modify: `Module/system_prompt/templates/cyrene/c7_proactive.j2`（Task 1 占位 → 填充 Cyrene 主动性）
- Modify: `character/cyrene/config.yaml`（Task 3 占位 → 填充实际 speaking_style）
- Modify: `character/cyrene/personality.md`（Task 3 骨架 → 填充完整 L1）
- Test: `core/prompt/tests/test_system_prompt_leak.py`（新增 Cyrene 渲染无 Elysia 角色词——但**依赖 Task 7 全面翻转**，此处先加基础断言）

**Interfaces:**
- Consumes: spec §3.1 角色内核 + §3.2 与 Elysia 差异 + §3.3 禁止词门禁 + `character/cyrene/reference/*.md`。
- Produces: Cyrene 的 C2/C3/C7 per-role 模板 + speaking_style + personality.md（不含 §3.3 禁止词，体现 §3.2 差异）。

- [ ] **Step 1: subagent 参照 reference 生成 Cyrene C2/C3/C7 内容**

派 subagent，输入：`character/cyrene/reference/*.md`（3 份昔涟角色分析）+ spec §3 创作指南。输出：
- `templates/cyrene/c2_relationship.j2`：Cyrene 关系观（爱=铭记/理解/陪伴/创造未来；不喜欢告别；阶段标签用「初遇/相伴/铭记」而非「初识/熟悉/信任/亲密」）。**wrapper `<relationship>...</relationship>` 保留**（与其他角色模板结构一致）。
- `templates/cyrene/c3_emotion.j2`：Cyrene 情绪响应（活泼甜美，但会收起笑意变轻而坚定；用「共情/牺牲」替代「傲娇」；应对话术不含 `今天不想振作也没有关系`）。**wrapper `<emotion>...</emotion>` 保留**。
- `templates/cyrene/c7_proactive.j2`：Cyrene 主动性（活泼俏皮但重「记住故事」；冷却机制不含 Elysia 原文）。**wrapper `<proactive>...</proactive>` 保留**。
- `character/cyrene/config.yaml` speaking_style：Cyrene C6（活泼甜美、直呼名字/温柔昵称、不用「五模式」框架、不用 ♪ 硬编码）。
- `character/cyrene/personality.md`：完整 L1（spec §3.1 内核）。

**subagent 验收规约**（写完必须自检）：
1. 内容不含 spec §3.3 禁止词：`Elysia`/`爱莉`/`爱莉希雅`/`卡莲`/`妖梦`/`普遍之爱`/`制造暧昧`/`初识`/`傲娇`/`今天不想振作也没有关系`/`俏皮灵动`/`明快`/`可爱的女孩子`/`漂亮的孩子`/`小家伙`/`亲爱的`/`特别的客人`/`勇敢的人`/`五模式切换`/`轻松模式`/`认真模式`/`神秘模式`/`告别模式`/`愤怒模式`。
2. 体现 §3.2 差异：爱观=铭记/陪伴 vs 普遍之爱；告别观=不喜欢告别 vs 告别模式。
3. C2/C3/C7 模板保留 `{{ user_name }}`/`{{ user_pronoun }}` 占位符（Elysia 模板同款），不硬编码 `她`。
4. 模板 wrapper 标签 `<relationship>`/`<emotion>`/`<proactive>` 保留，结构与其他角色一致。
5. **speaking_style 特殊规约**（⚠️ MINOR-7）：Cyrene 的 `speaking_style` 用**自定结构**（非「五模式切换/轻松模式/认真模式/告别模式/愤怒模式」框架）；用**「甜美」/「漂亮」替代「可爱」**（`可爱` 是 Elysia FIXED_PERSONA 词，避免翻转误判）；不用 `♪` 硬编码（Cyrene 语气更偏 `~`）；人称用直呼名字/温柔昵称/呀/呢（非 Elysia 的「可爱的女孩子」等）。
6. **辅助 prompt 去 Elysia 化**（§2.3 机制 Y 涉及，但 Cyrene 内容是 per-role 模板——注意 `俏皮灵动`/`成熟冷静`/`柔软直接` 只在 `CharacterReflector.j2` 辅助 prompt，**Cyrene 的 C2/C3/C7 模板不含这些词**；subagent 只需确保 Cyrene per-role 模板不含 §3.2 Elysia 风格词）。

- [ ] **Step 2: Cyrene 模板结构对齐断言（写测试）**

```python
# core/prompt/tests/test_system_prompt_snapshot.py (追加)——基础结构断言
def test_cyrene_per_role_templates_exist():
    """templates/cyrene/ 下 C2/C3/C7 模板存在且含 wrapper。"""
    base = Path("Module/system_prompt/templates/cyrene")
    for name, tag in [("c2_relationship.j2", "relationship"),
                      ("c3_emotion.j2", "emotion"),
                      ("c7_proactive.j2", "proactive")]:
        f = base / name
        assert f.exists(), f"{name} 不存在"
        content = f.read_text("utf-8")
        assert f"<{tag}>" in content and f"</{tag}>" in content
```

- [ ] **Step 3: 运行测试确认通过**

Run: `conda run -n Elysia python -m pytest core/prompt/tests/test_system_prompt_snapshot.py::test_cyrene_per_role_templates_exist -v`
Expected: PASS（subagent 生成后模板存在含 wrapper）。

- [ ] **Step 4: Commit**

```bash
git add Module/system_prompt/templates/cyrene/ character/cyrene/
git commit -m "feat(stage-b2): Cyrene per-role C2/C3/C7 内容 + 完整 L1/speaking_style"
```

---

### Task 5: 辅助 Prompt 参数化（MemoryReflector/ConsolidationCompare/ExtractMemory）

**Files:**
- Modify: `core/prompts/MemoryReflector.j2`（`Elysia`/`妖梦` → `{character_name}`/`{user_name}` 占位符）
- Modify: `core/prompts/ConsolidationCompare.j2`（`Elysia`/`妖梦` → `{character_name}`/`{user_name}`）
- Modify: `core/prompts/ExtractMemory.j2`（line 40 `妖梦` 反例 → `{user_name}`；line 3 俏皮描述去 Elysia 化）
- Modify: `core/memory/memory_reflector.py:69`（**`__init__` 加 `character_name`/`user_name` 参数**——⚠️ 当前无这两个字段，BLOCKING；存 `self._character_name`/`self._user_name`）+ `:258`（`replace()` 传值）
- Modify: `core/agent/agent.py:237`（**`MemoryReflector(...)` 调用点传 `character_name=self._character.name, user_name=self._user_name`**）
- Modify: `core/memory/consolidator.py:340`（**compare-prompt `replace()` 也加 `{character_name}`/`{user_name}`**——⚠️ 当前只替换 `{new_items}`/`{existing_items}`，CORRECTNESS）
- Modify: `core/memory/consolidator.py:271/340`（`replace()` 传 `{character_name}`/`{user_name}`）
- Test: `core/memory/tests/test_consolidator.py`、`core/memory/tests/test_memory_reflector.py`（追加占位符替换断言）

**Interfaces:**
- Consumes: `memory_reflector.py`/`consolidator.py` 的 `_get_*_prompt()`（读 `.j2` + `replace()`）。
- Produces: 辅助 Prompt 渲染无 `Elysia`/`妖梦` 字面；`{character_name}`/`{user_name}` 被替换（不复现 `{`）。

- [ ] **Step 1: 写失败测试**

```python
# core/memory/tests/test_memory_reflector.py (追加)
def test_reflector_prompt_placeholder_replaced():
    """MemoryReflector prompt 的 {character_name}/{user_name} 被替换，无 Elysia/妖梦。"""
    from core.memory.memory_reflector import _get_reflector_prompt
    p = _get_reflector_prompt()
    assert "Elysia" not in p and "妖梦" not in p, "MemoryReflector 仍含 Elysia/妖梦"
    assert "{character_name}" in p and "{user_name}" in p


def test_reflector_prompt_no_elysia_render():
    """MemoryReflector 渲染后无 Elysia/妖梦（replace 已传 character_name/user_name）。"""
    from core.memory.memory_reflector import _get_reflector_prompt
    p = _get_reflector_prompt().replace(
        "{character_name}", "Cyrene",
    ).replace(
        "{user_name}", "用户",
    ).replace(
        "{memories}", "[]",
    ).replace(
        "{last_summary}", "{}",
    )
    assert "Elysia" not in p and "妖梦" not in p
```

```python
# core/memory/tests/test_consolidator.py (追加)
def test_consolidation_prompt_no_elysia():
    """ConsolidationCompare/ExtractMemory prompt 无 Elysia/妖梦。"""
    from core.memory.consolidator import _get_compare_prompt, _get_extraction_prompt
    for p in [_get_compare_prompt(), _get_extraction_prompt()]:
        assert "Elysia" not in p and "妖梦" not in p, "consolidation prompt 仍含 Elysia/妖梦"
```

- [ ] **Step 2: 运行测试确认失败**

Run: `conda run -n Elysia python -m pytest core/memory/tests/test_memory_reflector.py core/memory/tests/test_consolidator.py -v`
Expected: FAIL — `Elysia`/`妖梦` 仍在 prompt 中。

- [ ] **Step 3: 改 MemoryReflector.j2 / ConsolidationCompare.j2 占位符**

`core/prompts/MemoryReflector.j2`：
- line 1：`你是 Elysia 的长期记忆管理专家。` → `你是 {character_name} 的长期记忆管理专家。`
- line 3：`Elysia 是妖梦的女性 AI 伴侣。...` → `{character_name} 是 {user_name} 的女性 AI 伴侣。...`
- line 39/40/83/111/168/201：`妖梦` → `{user_name}`

`core/prompts/ConsolidationCompare.j2`：同样把 `Elysia`→`{character_name}`，`妖梦`→`{user_name}`。

`core/prompts/ExtractMemory.j2`：
- line 3：`性格轻松时俏皮、认真时冷静、亲密时柔软而直接` → 去 Elysia 化（`{character_name} 是你（{user_name}）的陪伴者`或通用）
- line 40：`妖梦因为连续加班...` → `{user_name} 因为连续加班...`

- [ ] **Step 4: 改 `memory_reflector.py` `__init__` + `_review_group` replace + `agent.py` 传值**

**4a. `memory_reflector.py:69` `__init__` 加 `character_name`/`user_name` 参数**（⚠️ 当前无这两个字段，须添加）：

```python
def __init__(
    self,
    llm: MemoryLLM,
    memory: "LongTermMemory",
    session: "SessionMemory",
    character_name: str = "Elysia",  # 新增
    user_name: str = "妖梦",          # 新增
):
    ...
    self._character_name = character_name  # 新增
    self._user_name = user_name            # 新增
```

**4b. `memory_reflector.py:258` `_review_group` `replace()` 传值**：

```python
prompt = _get_reflector_prompt().replace(
    "{character_name}", self._character_name,
).replace(
    "{user_name}", self._user_name,
).replace(
    "{memories}", json.dumps(entries_json, ensure_ascii=False, indent=2),
).replace(
    "{last_summary}", json.dumps(self._last_summary, ensure_ascii=False),
)
```

**4c. `agent.py:237` 调用点传值**（⚠️ 关键——否则默认值 `"Elysia"`/`"妖梦"` 泄漏给 Cyrene）：

```python
self._memory_reflector = MemoryReflector(
    llm=self._llm,
    memory=self._long_term,
    session=self._session_manager.active,
    character_name=self._character.name,  # 新增
    user_name=self._user_name,            # 新增
)
```

**4d. `consolidator.py:340` compare-prompt `replace()` 加 `{character_name}`/`{user_name}`**（⚠️ 当前只替换 `{new_items}`/`{existing_items}`）：

```python
compare_prompt = prompt_template.replace(
    "{character_name}", self._character_name,
).replace(
    "{user_name}", self._user_name,
).replace(
    "{new_items}", json.dumps(...),
).replace(
    "{existing_items}", json.dumps(...),
)
```

> ⚠️ **`consolidator.py` 的 `_character_name`/`_user_name` 已存在**（line 189/190，`MemoryConsolidator.__init__` 已存），无需改构造。仅 compare-prompt 需补 replace。

- [ ] **Step 5: 运行测试确认通过**

Run: `conda run -n Elysia python -m pytest core/memory/tests/test_memory_reflector.py::test_reflector_prompt_placeholder_replaced core/memory/tests/test_memory_reflector.py::test_reflector_prompt_no_elysia_render core/memory/tests/test_consolidator.py::test_consolidation_prompt_no_elysia -v`
Expected: PASS（占位符替换，无 Elysia/妖梦）。

- [ ] **Step 7: Commit**

```bash
git add core/prompts/MemoryReflector.j2 core/prompts/ConsolidationCompare.j2 core/prompts/ExtractMemory.j2 core/memory/memory_reflector.py core/memory/consolidator.py core/agent/agent.py core/memory/tests/
git commit -m "refactor(stage-b2): 辅助 prompt 参数化（占位符替换 Elysia/妖梦）+ MemoryReflector 接 character_name/user_name"
```

---

### Task 6: CharacterReflector/SpeakingStyleEdit 去 Elysia 化 + compression _refresh_c6

**Files:**
- Modify: `core/prompts/CharacterReflector.j2:3`（`俏皮灵动...` → 通用心理结构）
- Modify: `core/prompts/SpeakingStyleEdit.j2:15,19`（`五模式`/`明快` → 通用）
- Modify: `core/personality/compression.py:48,269`（`character_config: object` → `CharacterConfig`；`_refresh_c6` 读 `_character.speaking_style`）
- Test: `core/personality/tests/test_compression.py`（**新建**——当前不存在，勿 append）
- **Modify: `core/personality/tests/test_prompt_leak_auxiliary.py`**（⚠️ BLOCKING：`test_speaking_style_edit_leak_forced_structure`/`test_speaking_style_edit_leak_immutable_color` 断言 `五模式切换`/`♪`/`明快` `in`，Task 6 移除后红 → 翻转这两个为 `not in`；`test_character_reflector_leak_pronoun` 的 `她 in` 保留）

**Interfaces:**
- Consumes: `CharacterReflector.j2`/`SpeakingStyleEdit.j2`（Jinja2 `pm.render`；`compression.py` 的 `_refresh_c6` 裸构造 `SpeakingStyleSection`）。
- Produces: CharacterReflector/SpeakingStyleEdit 渲染无 `俏皮灵动`/`明快`；Cyrene `_refresh_c6` 首刷读 `character.speaking_style`（不取 `_C6_L1_DEFAULT`）。

- [ ] **Step 1: 写失败测试**

```python
# core/personality/tests/test_compression.py (追加)
def test_refresh_c6_uses_character_speaking_style():
    """Cyrene _refresh_c6 首刷读 _character.speaking_style，不取 _C6_L1_DEFAULT。"""
    from unittest.mock import AsyncMock, MagicMock
    from pathlib import Path

    from core.config.character_config import CharacterConfig
    from core.personality.compression import CompressionManager
    from Module.system_prompt import PromptManager
    from paths import ProjectPath

    cfg = CharacterConfig.from_yaml(Path("character/cyrene/config.yaml"))
    # 关键断言：_refresh_c6 首刷用的 SpeakingStyleSection 应取 cfg.speaking_style 而非 _C6_L1_DEFAULT
    state = MagicMock()
    state.speaking_style_c6 = ""  # 首刷路径
    pm = PromptManager(str(ProjectPath.PROMPTS.resolve()))
    cm = CompressionManager(
        llm=AsyncMock(), state=state, character_name="Cyrene",
        l1_core=cfg.base_personality, pm=pm,
        version_repo=None, character_config=cfg,
    )
    # 间接验证：mock 内部 SpeakingStyleSection 构造，断言收到 speaking_style=cfg.speaking_style
    with patch("core.personality.compression.SpeakingStyleSection") as MockSec:
        MockSec.return_value.render = AsyncMock(return_value=cfg.speaking_style)
        # 直接调 _refresh_c6（需 mock self._state.speaking_style_c6 为空）
        # → 断言 MockSec 用 speaking_style=cfg.speaking_style 构造
        ...
```

> ⚠️ **`CompressionManager` 签名**（`compression.py:39-67`）：`llm, state, character_name, l1_core, pm, version_repo, character_config`（全 keyword-only，无 `engine`/`character_state`）。**无 `engine` 参数**——`state` 是 `CharacterState`。测试构造必须用这 7 个参数名。`_refresh_c6` 用 `self._state`（CharacterState），首刷读 `self._state.speaking_style_c6`。

- [ ] **Step 2: 核对 `CompressionManager.__init__` 签名 + `_refresh_c6` 现裸构造**

```bash
sed -n '39,67p' core/personality/compression.py
sed -n '258,290p' core/personality/compression.py
```
确认：`__init__` 参数为 `llm/state/character_name/l1_core/pm/version_repo/character_config`；`_refresh_c6` 首刷 `SpeakingStyleSection(...)` 无 `speaking_style` 参数。

- [ ] **Step 3: 改 CharacterReflector.j2 / SpeakingStyleEdit.j2 去 Elysia 化**

`core/prompts/CharacterReflector.j2:3`：
```text
你是感知自己状态的 {{ character_name }}：轻松时俏皮灵动，认真时成熟冷静，亲密时柔软直接。
→
你是感知自己状态的 {{ character_name }}，你的情绪与表达随角色而变化。
```

`core/prompts/SpeakingStyleEdit.j2`：
- line 15：`保持原有结构（五模式切换、声线、称呼、♪规则、夸奖、调情、心情影响）` → `保持原有结构，与角色说话风格一致`
- line 19：`语调始终是明快、柔和、自信、亲昵。这是不可变的底色` → `语调与你的性格底色一致，这是不可变的底色`

- [ ] **Step 4: 改 compression.py `_refresh_c6` + annotation**

```python
# compression.py:18-22 —— TYPE_CHECKING 块加 CharacterConfig import
if TYPE_CHECKING:
    from core.config.character_config import CharacterConfig  # 新增

# compression.py:48 —— 收紧 annotation
character_config: CharacterConfig,  # 不再用 object

# compression.py:268 —— _C6_L1_DEFAULT 并入函数内 import
from Module.system_prompt.sections.speaking_style import SpeakingStyleSection, _C6_L1_DEFAULT

# compression.py:258-274 —— _refresh_c6 首刷读 _character.speaking_style
if not current:
    try:
        from Module.system_prompt.sections.speaking_style import SpeakingStyleSection, _C6_L1_DEFAULT
        speaking_style = self._character.speaking_style or _C6_L1_DEFAULT
        sec = SpeakingStyleSection(
            str(ProjectPath.SYSTEM_PROMPT_TEMPLATES.resolve() / "c6_speaking_style.j2"),
            speaking_style=speaking_style,  # 关键：传 character.speaking_style
        )
        sec.set_context(user_name="")
        current = await sec.render()
    except Exception:
        logger.warning("CompressionManager: C6 默认模板渲染失败，跳过刷新")
        return
```

> ⚠️ **`_C6_L1_DEFAULT` 导入**：`_refresh_c6` 内已有函数级 `from Module.system_prompt.sections.speaking_style import SpeakingStyleSection`（line 268），`_C6_L1_DEFAULT` 并入同一 import（而非模块级）。
> **`CharacterConfig` annotation**：`compression.py` 的 TYPE_CHECKING 块（line 18-22）当前只导 `LLMAdapter/CharacterState/StateHistoryEntry/CharacterVersionRepository/PromptManager`，**需加 `from core.config.character_config import CharacterConfig`**，否则 pyright 对 `self._character.speaking_style` 仍报 `object` 无属性。

- [ ] **Step 5: 新建 `core/personality/tests/test_compression.py`（完整版测试）**

```python
# core/personality/tests/test_compression.py (新建，非 append)
"""CompressionManager._refresh_c6 首刷读 character.speaking_style（B-2）。"""
import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from core.config.character_config import CharacterConfig
from core.personality.compression import CompressionManager
from Module.system_prompt import PromptManager
from paths import ProjectPath


def _make_cm(character_config):
    state = MagicMock()
    state.speaking_style_c6 = ""  # 首刷路径
    pm = PromptManager(str(ProjectPath.PROMPTS.resolve()))
    return CompressionManager(
        llm=AsyncMock(), state=state, character_name=character_config.name,
        l1_core=character_config.base_personality, pm=pm,
        version_repo=None, character_config=character_config,
    ), state


def test_refresh_c6_uses_character_speaking_style():
    """Cyrene _refresh_c6 首刷读 character.speaking_style（非 _C6_L1_DEFAULT）。"""
    cfg = CharacterConfig.from_yaml(Path("character/cyrene/config.yaml"))
    assert cfg.speaking_style.strip(), "前置：Cyrene speaking_style 非空"
    cm, state = _make_cm(cfg)
    # mock 内部 SpeakingStyleSection，捕获构造参数
    from Module.system_prompt.sections.speaking_style import SpeakingStyleSection
    orig = SpeakingStyleSection
    captured = {}
    def fake_init(self, template_path, speaking_style=None):
        captured["speaking_style"] = speaking_style
        orig.__init__(self, template_path, speaking_style if speaking_style is not None else orig_default())
    import Module.system_prompt.sections.speaking_style as ss_mod
    ss_mod.SpeakingStyleSection = fake
    try:
        asyncio.run(cm._refresh_c6())
    finally:
        ss_mod.SpeakingStyleSection = orig
    # 断言首刷用 Cyrene 的 speaking_style（非 _C6_L1_DEFAULT）
    assert captured.get("speaking_style") == cfg.speaking_style
    assert captured.get("speaking_style") != _C6_L1_DEFAULT
```

> ⚠️ **测试实现注解**：`_refresh_c6` 内 `from ... import SpeakingStyleSection` 是**函数级本地 import**，patch `Module.system_prompt.sections.speaking_style.SpeakingStyleSection`（模块路径）才拦截；patch `compression.SpeakingStyleSection` 无效（函数读的是 section 模块的类）。上述 `fake_init` 用模块级替换 + 捕获构造参数。实现时按此思路写完整断言。

- [ ] **Step 6: 运行测试确认通过**

Run: `conda run -n Elysia python -m pytest core/personality/tests/test_compression.py -v`
Expected: PASS（`_refresh_c6` 读 character.speaking_style；Cyrene 首刷用 Cyrene 风格）。

- [ ] **Step 7: 运行相关测试确认无回归（含翻转 auxiliary）**

Run: `conda run -n Elysia python -m pytest core/personality/tests/test_compression.py core/personality/tests/test_prompt_leak_auxiliary.py -v`
Expected: PASS——`test_compression.py` 新测试通过；`test_prompt_leak_auxiliary.py` 的 `test_speaking_style_edit_leak_forced_structure`/`test_speaking_style_edit_leak_immutable_color` **已翻转**（Step 8）。

- [ ] **Step 8: 翻转 `test_prompt_leak_auxiliary.py`（Stage A 辅助 prompt 泄漏退出门禁）**

```python
# core/personality/tests/test_prompt_leak_auxiliary.py —— 翻转两个 SpeakingStyleEdit 断言
def test_speaking_style_edit_leak_forced_structure(mock_engine...):
    result = _render_speaking_style_edit(...)
    # 翻转：五模式切换 / ♪ 不再出现（SpeakingStyleEdit.j2 去 Elysia 化）
    assert "五模式切换" not in result
    assert "♪" not in result

def test_speaking_style_edit_leak_immutable_color(...):
    result = _render_speaking_style_edit(...)
    # 翻转：明快、柔和、自信、亲昵 不再出现
    assert "明快、柔和、自信、亲昵" not in result
```

> ⚠️ `test_character_reflector_leak_pronoun` 的 `她 in` **保留**（`她` 仍在 CharacterReflector.j2:82/95，是示例第三方——B-1 Ruling 6，非 Elysia 泄漏）。

- [ ] **Step 9: Commit**

```bash
git add core/prompts/CharacterReflector.j2 core/prompts/SpeakingStyleEdit.j2 core/personality/compression.py core/personality/tests/test_compression.py core/personality/tests/test_prompt_leak_auxiliary.py
git commit -m "fix(stage-b2): CharacterReflector/SpeakingStyleEdit 去 Elysia 化 + compression _refresh_c6 读 character.speaking_style + 辅助 prompt 泄漏测试翻转"
```

---

### Task 7: 泄漏测试翻转 + 快照守门维护（B-2 退出门禁）

**Files:**
- Modify: `core/prompt/tests/test_system_prompt_leak.py`（拆分：Cyrene 角色词 `not in` / 共享格式词 `in` / cold 回退 `in` / Elysia `in`）
- Modify: `core/prompt/tests/conftest.py`（确保 `cyrene_config` 已加，Task 3）
- Test: `core/prompt/tests/test_system_prompt_snapshot.py`（确认 Elysia 快照 byte 不变；cold 快照不变）

**Interfaces:**
- Consumes: `cyrene_config` 夹具（Task 3）+ Cyrene per-role 模板（Task 4）+ `_resolve_template` 选路（Task 1）。
- Produces: 泄漏测试三组断言绿——Cyrene 角色词无、共享格式词有、cold 回退有；Elysia 快照 byte 不变。

- [ ] **Step 1: 拆分 `test_system_prompt_leak.py`（用 `cyrene_config` + `elysia_config` + `cold_config`）**

关键改动：
- `test_leak_tone_marker`（原 cold 断言 `♪ in`）→ 保留为 **cold/Elysia 断言 `♪ in`**（共享 F2 协议）。
- `test_leak_speaking_mode_structure`/`test_leak_fixed_persona`（elysia_config，断言 `in`）→ 保留（byte-for-byte）。
- `test_leak_relationship_structure`（原 cold `普遍之爱 in`）→ **Cyrene 渲染断言 `普遍之爱 not in`**。
- `test_leak_relationship_stage`（原 cold `初识 in`）→ **Cyrene 渲染断言 `初识 not in`**。
- `test_leak_emotion_model`（原 cold `傲娇/今天不想振作 in`）→ **Cyrene 渲染断言 `傲娇 not in`/`今天不想振作也没有关系 not in`**。
- `test_leak_template_body_pronoun`（原 cold `她 in`）→ 保留（F2/F3 共享示例），改明确注释「共享格式词非 Elysia 泄漏」。
- `test_leak_output_format_example`/`test_leak_tts_example`（原 cold `诶嘿`/`她果然` in）→ 保留（F2/F3 共享示例）。
- `test_leak_parameterized_over_scenarios`（原 cold `♪ in`）→ 保留（共享 F2 协议）。

**具体测试代码示例（Cyrene 角色词 not in）：**

```python
@pytest.mark.asyncio
async def test_leak_relationship_structure_cyrene(cyrene_config, cold_engine_mock, mock_long_term, mock_session):
    """Cyrene C2 无 Elysia 关系观（per-role 去 Elysia 化）。"""
    rendered = await render_system_prompt(make_assembler(cyrene_config, cold_engine_mock, mock_long_term, mock_session))
    assert "普遍之爱" not in rendered
    assert "制造暧昧" not in rendered


@pytest.mark.asyncio
async def test_leak_relationship_stage_cyrene(cyrene_config, cold_engine_mock, mock_long_term, mock_session):
    """Cyrene C2 阶段标签不叫初识。"""
    rendered = await render_system_prompt(make_assembler(cyrene_config, cold_engine_mock, mock_long_term, mock_session))
    assert "初识" not in rendered


@pytest.mark.asyncio
async def test_leak_emotion_model_cyrene(cyrene_config, cold_engine_mock, mock_long_term, mock_session):
    """Cyrene C3 无 Elysia 应对话术/傲娇。"""
    rendered = await render_system_prompt(make_assembler(cyrene_config, cold_engine_mock, mock_long_term, mock_session))
    assert "今天不想振作也没有关系" not in rendered
    assert "傲娇" not in rendered
```

**共享格式词保留（Cyrene `in`）：**

```python
@pytest.mark.asyncio
async def test_shared_format_words_remain(cyrene_config, cold_engine_mock, mock_long_term, mock_session):
    """共享格式词（♪/她/诶嘿/她果然）仍在 Cyrene 渲染中——F2/F3 共享协议。"""
    rendered = await render_system_prompt(make_assembler(cyrene_config, cold_engine_mock, mock_long_term, mock_session),
                                          tts_provider="minimax")
    assert "♪" in rendered
    assert "她" in rendered
    assert "诶嘿~被你发现啦" in rendered
    assert "她果然很聪明呢" in rendered
```

- [ ] **Step 2: 运行泄漏测试确认**

Run: `conda run -n Elysia python -m pytest core/prompt/tests/test_system_prompt_leak.py -v`
Expected: 全部通过（Cyrene 角色词 not in；共享格式词 in；Elysia in；cold 回退 in）。

- [ ] **Step 3: 运行快照守门确认 Elysia/cold 不变**

Run: `conda run -n Elysia python -m pytest core/prompt/tests/test_system_prompt_snapshot.py -v`
Expected: PASS——`test_elysia_snapshot_unchanged`（Elysia byte 不变）+ `test_snapshot_matches_current`（cold 快照不变）+ `test_elysia_templates_byte_identical`（elysia 副本字节相同）。

- [ ] **Step 4: 运行全量相关测试**

Run: `conda run -n Elysia python -m pytest -m "unit or service" -q --tb=short`
Expected: PASS（无回归）。

- [ ] **Step 5: 门禁**

Run: `make lint`（ruff TID252 + ANN 门禁）
Expected: PASS。

Run: `conda run -n Elysia pyright`
Expected: 0 errors（含 compression.py annotation 收紧后）。

- [ ] **Step 6: Commit**

```bash
git add core/prompt/tests/test_system_prompt_leak.py core/prompt/tests/test_system_prompt_snapshot.py core/prompt/tests/conftest.py
git commit -m "test(stage-b2): 泄漏测试翻转——Cyrene 角色词 not in / 共享格式词 in / cold 回退 in；Elysia 快照 byte 不变"
```

---

## 任务依赖与执行顺序

```
Task 1 (_resolve_template + 模板目录骨架) ──→ Task 2 (bytes 校验)
   │
   ├──→ Task 3 (Cyrene config + personality 骨架) ──→ Task 4 (Cyrene per-role 内容 subagent 生成)
   │                                                      │
   │                                                      ├──→ 供 Task 7 用
   │                                                      │
   └──→ Task 6 (CharacterReflector/SpeakingStyleEdit 去 Elysia + compression _refresh_c6)
              （Task 6 依赖 Task 3——其测试用 from_yaml(character/cyrene/config.yaml)；
                不依赖 Task 4，可并行于 Task 4）
Task 5 (辅助 prompt 参数化) —— 独立，可与 Task 1 并行
Task 7 (泄漏测试翻转) —— 依赖 Task 1/3/4/6 全部完成后执行
```

**可并行**：Task 1 与 Task 5 独立（不同文件）；Task 6 与 Task 4 独立（Task 6 依赖 Task 3 但不依赖 Task 4 的最终内容）。**Task 7 须最后**（依赖所有 per-role 内容 + 选路 + 去 Elysia 化完成）。

## 验收（B-2 完成标准，对应 spec §7）

- [ ] `make lint` 通过
- [ ] `make pyright` 通过（`conda run -n Elysia pyright`）
- [ ] `make test-quick` 通过（`conda run -n Elysia python -m pytest -m "unit or service" -q -x --tb=short`）
- [ ] `test_elysia_snapshot_unchanged` 通过（Elysia byte-for-byte 不变）
- [ ] `test_elysia_templates_byte_identical` 通过（elysia/* == 共享原版）
- [ ] Cyrene 角色风格词 `not in`（`普遍之爱`/`制造暧昧`/`初识`/`傲娇`/`今天不想振作`）通过
- [ ] 共享格式词 `in`（`♪`/`她`/`诶嘿`/`她果然`）通过
- [ ] cold 回退 `in`（Elysia 风格底线）通过
- [ ] 辅助 prompt 渲染无 `Elysia`/`妖梦`（Task 5）
- [ ] CharacterReflector/SpeakingStyleEdit 无 `俏皮灵动`/`明快`（Task 6）
- [ ] `compression.py::_refresh_c6` 读 character.speaking_style（Cyrene 首刷不取 `_C6_L1_DEFAULT`）
- [ ] `character/cyrene/config.yaml` + `personality.md` + `personality.speaking_style` 存在非空
- [ ] `templates/cyrene/c2/c3/c7` 存在且含 wrapper



