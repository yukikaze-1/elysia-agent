# Brain Prompt 模块迁移至 Module/ 实现计划

> **For agentic workers:** 纯搬迁任务，不需要 subagent。直接在本 session 执行。

**Goal:** 将 `core/prompt/` 迁移为 `Module/brain_prompt/`，删除原目录，更新所有 import。

**Architecture:** `git mv` 搬迁源文件 + 测试文件，sed 批量替换 import 路径，删除旧目录，验证 379 tests。

**Tech Stack:** bash, sed, pytest

## Global Constraints

- 模板数据 `prompt/brain/` 不动
- `Module/prompt/` 不动
- 测试内容逐字不变，仅改 import
- `BrainPromptRegistry("prompt/brain")` 路径不变

---

### Task 1: 搬迁 + import 修正 + 验证（单 task，~10 步）

**Files:**
- Create: `Module/brain_prompt/` (5 src + 6 test files, via git mv)
- Delete: `core/prompt/`
- Modify: `core/prompt_assembler.py:18`
- Modify: `CLAUDE.md`
- Modify: `README.md`

- [ ] **Step 1: 创建目标目录 + git mv 源文件**

```bash
mkdir -p Module/brain_prompt/tests/unit Module/brain_prompt/tests/integration

# 源文件
git mv core/prompt/__init__.py Module/brain_prompt/
git mv core/prompt/section.py Module/brain_prompt/
git mv core/prompt/parser.py Module/brain_prompt/
git mv core/prompt/profile.py Module/brain_prompt/
git mv core/prompt/registry.py Module/brain_prompt/

# 测试文件
git mv core/prompt/tests/__init__.py Module/brain_prompt/tests/
git mv core/prompt/tests/unit/__init__.py Module/brain_prompt/tests/unit/
git mv core/prompt/tests/unit/test_section.py Module/brain_prompt/tests/unit/
git mv core/prompt/tests/unit/test_parser.py Module/brain_prompt/tests/unit/
git mv core/prompt/tests/unit/test_profile.py Module/brain_prompt/tests/unit/
git mv core/prompt/tests/unit/test_registry.py Module/brain_prompt/tests/unit/
git mv core/prompt/tests/integration/__init__.py Module/brain_prompt/tests/integration/
git mv core/prompt/tests/integration/test_golden_diff.py Module/brain_prompt/tests/integration/
git mv core/prompt/tests/integration/test_render_full.py Module/brain_prompt/tests/integration/
```

- [ ] **Step 2: 清理空的旧目录**

```bash
rmdir core/prompt/tests/unit core/prompt/tests/integration core/prompt/tests core/prompt 2>/dev/null
rm -rf core/prompt/__pycache__ 2>/dev/null
# 确认:
test ! -d core/prompt/ && echo "OK: core/prompt/ deleted" || echo "FAIL: core/prompt/ still exists"
```

- [ ] **Step 3: 修正生产代码 import — `core/prompt_assembler.py:18`**

旧: `from .prompt.registry import BrainPromptRegistry`
新: `from Module.brain_prompt import BrainPromptRegistry`

```bash
sed -i 's/from \.prompt\.registry import BrainPromptRegistry/from Module.brain_prompt import BrainPromptRegistry/' core/prompt_assembler.py
```

- [ ] **Step 4: 修正测试文件 import**

6 个测试文件，所有 `from core.prompt.xxx import` → `from Module.brain_prompt import`：

```bash
# 所有 "from core.prompt.xxx import YYY" → "from Module.brain_prompt import YYY"
find Module/brain_prompt/tests -name '*.py' -exec sed -i 's/from core\.prompt\.\w\+ import/from Module.brain_prompt import/g' {} +
```

- [ ] **Step 5: 验证 sed 替换完整性**

```bash
# 确认无残留旧 import
grep -rn 'core\.prompt' Module/brain_prompt/tests/ && echo "FAIL: leftover core.prompt imports" || echo "OK: clean"
grep -rn 'core\.prompt' Module/brain_prompt/ --include='*.py' | grep -v __pycache__ && echo "NOTE: check above" || echo "OK: clean"
```

- [ ] **Step 6: 运行新模块测试**

```bash
python -m pytest Module/brain_prompt/tests/ -v
```

Expected: 45 passed (15 parser + 10 registry + 10 section + 4 profile + 2 golden diff + 4 render full)

- [ ] **Step 7: 运行全量回归**

```bash
python -m pytest Module/prompt/test_prompt.py -v
python -m pytest Module/task_system/tests/unit/ -v
python -m pytest core/tests/test_phase3.py -v
```

Expected: 23 + 305 + ~6 = 334 passed, plus 45 brain_prompt = 379 total

- [ ] **Step 8: 更新 CLAUDE.md Module index**

旧: `| core.prompt | Brain Prompt 模块化引擎 | ...` → 改为放在 Module/ 表格中：

```bash
sed -i 's/| `core\.prompt` | Brain Prompt 模块化引擎 | `BrainPromptRegistry`, `SectionParser`, `ProfileConfig` | — |/| `Module.brain_prompt` | Brain Prompt 模块化引擎 | `BrainPromptRegistry`, `SectionParser`, `ProfileConfig` | — |/' CLAUDE.md
```

- [ ] **Step 9: 更新 README.md 项目结构**

```bash
# core/prompt/ → Module/brain_prompt/
sed -i 's|core/prompt/|Module/brain_prompt/|g' README.md
# 项目结构树中移除 core/prompt/ 行，改为 Module/ 下
sed -i 's|├── prompt/             #   Brain Prompt 引擎|├── brain_prompt/       #   Brain Prompt 引擎（SectionParser → Registry, 9 sections, profile/A/B/热更新）|' README.md
```

- [ ] **Step 10: Commit**

```bash
git add -A
git commit -m "refactor: relocate core/prompt/ → Module/brain_prompt/

- Move 5 src + 6 test files from core/prompt/ to Module/brain_prompt/
- Fix imports: from core.prompt.xxx → from Module.brain_prompt
- Delete core/prompt/
- Update CLAUDE.md and README.md"
```
