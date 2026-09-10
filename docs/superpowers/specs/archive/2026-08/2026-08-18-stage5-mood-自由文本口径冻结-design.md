# 阶段 5（Live2D）后端配套：`generation.completed.mood` 自由文本口径冻结（B-1）

**文档版本：** v0.2
**文档状态：** 已评审（code-review 1 轮，4 Minor 消化，无 Critical/Important）
**日期：** 2026-08-18
**仓库：** `/home/yomu/elysia-agent`
**上游评估：** `docs/review/archive/2026-08/2026-08-18-阶段5-Live2D后端配套清单.md`（B-1【P0·文档必须补】）
**后端基线：** `main@e348091fd5d8759abb1182bd8e86a9bd4cf456e8`
**前端对照：** elysia-frontend 阶段 5 前端批次 A/B/C（`live2d.directive` 前端契约超集已冻结，B-2 项）
**前置决策：** 会话讨论定案 D0-D5（全部按推荐定案）

---

## 1. 背景

阶段 5（Live2D）前端批次 A/B/C 已完成，P0 是前端本地渲染能力；后端复核结论为**无阻塞性
代码缺口**。唯一「P0·文档必须补」项是 B-1：冻结 `generation.completed.mood` 的自由文本口径。

- 后端 `generation.completed.mood` 实为 LLM `<mood>` 标签的自由文本（常见值：`开心`、`平静`、
  `难过`、`惊讶` 等），**不是英文枚举**。前端阶段 5 按本地映射表消费该字段；联调复核发现前端
  目前只认英文码，已列入前端整改清单。
- 为避免未来后端或契约层误把 `mood` 收敛成英文枚举、再次造成跨仓漂移，后端必须冻结口径：
  **`mood` 为自由文本/中文标签，非枚举；客户端负责本地归一映射，未知值安全忽略。**
- B-2（`live2d.directive` 后端指令帧）由清单定案为 **P1 可选项，第一轮真实联调不实现**——本批
  记录该决策，不实现。
- B-3（启动后端确认基线）/B-4（联调后回填）为联调时/后环境与文档动作，**不入本代码批**，写入
  执行记录后续衔接。

## 2. 目标

- **B-1**：`frontend_contract/ws_message.py` 的 `GenerationCompletedMessage.mood` description
  改为自由文本口径；`docs/electron-backend-protocol-v2.md` 冻结 `mood` 非枚举语义（§2.1 字段说明、
  §5 完成 bullet、§7 expression 抑制、§1 建连 mood）。
- 重新生成三产物（`contract.generated.ts` / `contract.schema.json` / `contract.raw.ts`），
  `make check-types` 全绿。
- **不新增枚举、不改变 wire 字段类型、不新增 wire 消息/字段/错误码**——纯描述/文档变更。
- **golden 帧零 churn**：golden 是 wire 帧（无 description），`scripts/gen_contract_frames.py
  --check` 不受影响。
- **B-2 定案 defer** 记录在案；**B-3/B-4** 写入执行记录后续衔接。
- 已通过联调链路（阶段 4 真实联调验收、voice 三模式、TTS/Profile）不得回归。

## 3. 现状（事实基线）

### 3.1 契约模型 `frontend_contract/ws_message.py`

| 行 | 现状 |
|---|---|
| `:421` | `GenerationCompletedMessage.mood: Optional[str] = Field(default=None, description="心情（<mood> 标签落位），可缺省")`——**B-1 目标行** |
| `:209` | `SessionMessageItem.mood`「情绪标签，可缺省」（会话历史项，同源自由文本） |
| `:456` | `MessageMessage.mood`「情绪标签，可缺省」（v1 完整消息推送） |

> D2 定案：只动 `:421`（`GenerationCompletedMessage.mood`）；`:209`/`:456` 为 v1/历史路径，按
> 报告 scope 记为「同源但不动」，避免扩大契约 churn。

### 3.2 mood 数据链（核实「自由文本非枚举」成立，且有更强证据）

| 环节 | 位置 | 事实 |
|---|---|---|
| 生产 | `core/cognitive/streaming.py:36-69` `_apply_mood` | 从 LLM `<mood>` 标签原样提取自由文本（`开心`/`平静`…），支持「文本\|强度」，写入 `character_state.mood` |
| 兜底 | `core/personality/emotion.py:82-101` `analyze_user`（文本关键词兜底，`finalize.py:56-65` `_apply_final_mood_fallback` 调用） | LLM 未出 `<mood>` 时按关键词写**英文 keyword**（`happy`/`sad`…）到 `state.mood`——字段既可能中文自由文本、也可能英文关键词，**更强化「非枚举」口径**。注：`analyze_user_stt`（`:115-129`）仅推 STT 表情、**不**写 `state.mood`，非本条路径 |
| 落位 | `core/cognitive/finalize.py:192-196` | `generation.completed` 的 `mood` 读 `target.character_state.mood`（目标会话），`target is None` 时读 `engine.state.mood` |

### 3.3 生成产物与 description 发射

| 产物 | 机制 | 现状行 |
|---|---|---|
| `frontend/src/types/contract.generated.ts` | `frontend_contract/gen_ts.py:86-88` 把 description 发射为 JSDoc | `:432-433`（`/** 心情（<mood> 标签落位），可缺省 */`） |
| `frontend_contract/contract.schema.json` | `frontend_contract/gen_schema.py:62` `TypeAdapter.json_schema()` 原生产出 description | `:5313` / `:9888` |
| `frontend_contract/contract.raw.ts` | `gen_ts.generate_raw_ts`（同 JSDoc 机制） | `:436-437` |

**结论：改 description 必 churn 三产物**（报告主张成立）。golden 帧是 wire 帧（无 description）→
零 churn。基线 `python -m frontend_contract --check` 此刻已绿（已实测）。

### 3.4 golden 帧已实证中文自由文本

`scripts/gen_contract_frames.py:120/220` 用 `mood="开心"`/`"平静"`；`docs/archive/frontend-handoff-v2/traces/`
的 `ws-v2-chat.jsonl`/`ws-v2-session.jsonl` 含 `"mood"`。**wire 层本来就是中文自由文本**——冻结口径
与现状一致，无行为改动。

### 3.5 协议文档 `docs/electron-backend-protocol-v2.md`（mood 相关落点）

| 行 | 现状 | 说明 |
|---|---|---|
| `:15` | 「→ `expression`（当前心情）」 | §1 建连帧，同为角色 mood 自由文本（与 `generation.completed.mood` 同一数据来源，读活动/默认会话 `character_state.mood`；多会话下与目标会话 mood 可能不同——既有语义，非本批引入；报告命名 §1） |
| `:36` | sample `"mood":"…"` | §2 事件序列样例（wire 示例，通用占位，无需改） |
| `:49` | `generation.completed` 行「…`mood`、`completeness`…」 | §2.1 事件表——**权威字段说明落点** |
| `:115` | 「**完成**：`generation.completed` 是唯一终态（携带 usage/model/mood）」 | §5 完成 bullet——prose 字段说明 |
| `:138` | 「`expression` **抑制**（心情由 `generation.completed.mood` 落位 + 建连 mood 推送）」 | §7 v2 通道——报告 §7 命名处 |

> 报告「§1/§2 的 generation.completed 字段说明」引用略不精确：§1 无该字段说明表，mood 实际落点
> 如上 4 处。本 spec 钉精确插入点（§1:15、§2.1 表格后、§5:115、§7:138）。

### 3.6 现有测试与门禁

| 项 | 现状 | B-1 影响 |
|---|---|---|
| `frontend_contract/tests/test_ws_message.py:148` | `mood="开心"` 断言 wire 值 | 不 assert description 字符串 → 描述改动不破测试 |
| 全仓 description 断言 | 无任何测试断言 description | 无测试影响 |
| `make check-types` 三子项 | 基线绿（已实测 `--check`） | 改后需 `make gen-types` 再生成 + `--check` 验证 |
| golden `--check` | 零 churn | wire 帧无 description → 不受影响 |

## 4. 方案（D0-D5 定案）

### 4.1 契约描述冻结（`frontend_contract/ws_message.py:421`）

```python
mood: Optional[str] = Field(default=None, description="心情自由文本（如 `开心`/`平静`），非枚举；前端本地归一，未知值忽略")
```

- 新 description 无 `*/` 序列（JSDoc 安全）、无引号/换行（JSON schema 安全）；含反引号在 JSDoc 注释
  与 schema 字符串值内均为合法字符。
- ruff 无 E501 行宽规则（`ruff.toml` 仅 select TID252/ANN001-003）→ 无 lint 红线。
- 改后运行 `make gen-types`（`python -m frontend_contract`，再生成 ts/schema/raw_ts 三产物）。

### 4.2 文档口径冻结（`docs/electron-backend-protocol-v2.md`，4 处插入/扩展）

| # | 落点 | 改动 |
|---|---|---|
| 1 | `:15` §1 建连帧 | 「`expression`（当前心情，**自由文本非枚举**，客户端本地归一映射，未知值忽略）」 |
| 2 | §2.1 事件表后（`:42-50` 表格**之后**，即 line 50 后插入，勿插表内） | 新增 `>` 注记：「`generation.completed.mood` 为**自由文本/中文标签，非枚举**（LLM `<mood>` 标签原样落位，如 `开心`/`平静`）；客户端负责本地归一映射，未知值安全忽略（阶段5联调口径，2026-08-18）」 |
| 3 | `:115` §5 完成 bullet | 「（携带 usage/model/mood；**`mood` 为自由文本非枚举**——LLM `<mood>` 标签原样落位，客户端本地归一，未知值忽略，阶段5联调口径）」 |
| 4 | `:138` §7 expression 抑制行 | 「（心情由 `generation.completed.mood` 落位 + 建连 mood 推送；**`mood` 为自由文本非枚举**，客户端本地归一，未知值忽略）」 |

统一注记后缀「阶段5联调口径，2026-08-18」，与阶段 4 的「阶段4真实联调修订」注记风格一致。

### 4.3 决策表

| 决策 | 定案 | 理由 |
|---|---|---|
| D0 批范围 | **A 只做 B-1** | B-2 报告自定 P1 可选项第一轮联调不实现；B-3/B-4 是联调时/后环境与文档动作，不入代码批 |
| D1 描述措辞 | **报告原文**（含 `开心`/`平静` 反引号） | 已核验 JSDoc/JSON 双安全；与前端整改清单口径一致 |
| D2 其它 mood 字段 | **只动 `GenerationCompletedMessage.mood`** | `SessionMessageItem.mood:209` / `MessageMessage.mood:456` 同源但为 v1/历史路径，严格跟随报告验收 scope，不扩大契约 churn |
| D3 文档插入点 | **4 处钉死**（§1:15 / §2.1 表后 / §5:115 / §7:138） | 报告「§1/§2」引用不精确，按真实落点全覆盖；漏插风险 = 口径未全冻结 |
| D4 测试 | **不新增单元测试；验证=门禁 + grep** | 描述改动无行为语义；check-types 三子项 + golden 零 churn 即验证；补 grep 断言新口径出现在 doc + 三产物 |
| D5 流程 | **goal-mode 批次**（spec/plan 单 commit → SDD 执行 → 门禁 → 验收） | 与 BE-S4-RUN-P0-01 同构的 review 整改批；执行记录承载 B-2/B-3/B-4 决策 |

**纪律**：golden、`Makefile`、`providers.yaml` 零改动；`frontend_contract/` 只动 `ws_message.py:421`
一个 description（不新增/删除/重命名模型与字段）；历史文档不动。

## 5. 测试计划

本批为描述/文档变更，**无行为语义**。测试策略 = 门禁即验证：

### 5.1 Task 1：契约描述冻结 + 再生成（`frontend_contract/ws_message.py` + 三产物）

- 改 `:421` description → 运行 `make gen-types` 再生成三产物 → `make check-types` 三子项全绿。
- 定向验证：grep 新口径「心情自由文本」在 `contract.generated.ts` / `contract.raw.ts`（JSDoc）与
  `contract.schema.json`（description 字段）均出现；旧描述「心情（<mood> 标签落位）」在生成产物
  中零命中。
- golden `--check` 退出 0（零 churn）。

### 5.2 Task 2：文档口径冻结（`docs/electron-backend-protocol-v2.md`）

- 4 处插入/扩展（§4.2 表）。
- 定向验证：grep「自由文本/中文标签，非枚举」或「自由文本非枚举」在 protocol-v2.md 命中 ≥3 处
  （§2.1 表后 / §5 / §7，§1 用「自由文本非枚举」措辞）；旧措辞「情绪标签」「可缺省」在 doc 中
  mood 上下文无新增。

## 6. 门禁

- `make test-quick`（unit+service，含 frontend_contract 契约测试）、`make test-normal`（排除 slow）。
- `make lint`（ruff All passed）、`make pyright`（0 errors，Elysia env）。
- `make check-types`（`frontend_contract --check` + `gen_contract_frames --check` 零 churn +
  `check_error_codes.py` 41 码）。
- 已通过联调链路回归：阶段 4 真实联调验收（R1-R4）、voice 三模式、TTS/Profile、多会话。

## 7. 验收（完成定义）

- `GenerationCompletedMessage.mood` description = 「心情自由文本（如 `开心`/`平静`），非枚举；
  前端本地归一，未知值忽略」；三产物再生成后 `make check-types` 全绿（golden 零 churn + 41 码）。
- protocol-v2.md 在 §1 建连 mood、§2.1 字段说明、§5 完成 bullet、§7 expression 抑制 4 处均冻结
  「mood 自由文本非枚举」口径；旧「心情（<mood> 标签落位）」措辞从生成产物移除。
- B-2（`live2d.directive`）定案 defer 已记录；B-3（启动后端基线）/B-4（联调后回填）写入执行记录
  后续衔接。
- 不新增枚举、不改变 wire 字段类型、不新增 wire 消息/字段/错误码；`Makefile`/`providers.yaml`/
  golden 零改动；历史文档零触碰。
- **标记**：本批可标「阶段 5 已开发/自动化已验证（B-1 口径冻结）」；「阶段 5 完整真实联调通过」
  须待前端阶段 5 批次 + Windows Electron 真实复验后由联调作业推进（B-3/B-4）。

## 8. 风险

| 风险 | 缓解 |
|---|---|
| 改 description 未同步再生成 → check-types 漂移红 | Task 1 步骤强制 `make gen-types` 后再 `--check`；门禁双验证 |
| 报告「§1/§2」引用不精确导致漏插 | §4.2 钉死 4 处插入点（§1:15 / §2.1 / §5 / §7），Task 2 grep 验证 ≥3 命中 |
| 未来契约层误收敛 mood 为枚举 → 跨仓漂移 | 冻结口径写入 contract description + 文档双保险；golden 帧已是中文值，再生成自证 |
| description 内反引号/斜杠破坏产物 | 已核验无 `*/`/引号/换行序列；JSDoc 与 JSON schema 均安全 |
| 前端 Live2D 消费 mood 依赖未同步 | 前端整改清单已列（本地归一映射）；本批只冻结后端口径，不涉前端代码 |

## 9. 决策点记录（会话定案 D0-D5）

D0 只做 B-1（B-2 defer，B-3/B-4 后续衔接）；D1 描述用报告原文；D2 只动 `GenerationCompletedMessage.mood`；
D3 文档 4 处插入点钉死；D4 不新增测试（门禁 + grep 即验证）；D5 goal-mode 批次。全部按推荐定案，
无用户推翻。

## 10. Review 记录

code-review 1 轮（general-purpose agent）结论 **With fixes**（无 Critical/Important，4 Minor 全消化）：

- **MIN-1**（§4.1）「长度 93 字符」数字错误（description 实为 36 字符）——改为只保留「ruff 无 E501 规则」论证，删除数字。
- **MIN-2**（§3.2）「STT 情绪检测路径」归因错误——写英文 keyword 的是**文本关键词兜底** `analyze_user`（emotion.py:82-101，`finalize.py:56-65` 调用），`analyze_user_stt`（:115-129）仅推 STT 表情不写 mood；§3.2 表已重写，实质结论（mood 可含英文 keyword，强化非枚举）不变。
- **MIN-3**（§4.2 #2）插入点「:49-50」歧义（表内插会破 markdown 表格）——改为「§2.1 事件表后（line 50 后插入，勿插表内）」。
- **MIN-4**（§4.2 #1 + §3.5 :15）「与 generation.completed.mood 同源」在多会话下不精确（建连 expression 读活动/默认会话、generation mood 读目标会话，可分歧，既有语义）——§4.2 #1 改为仅口径措辞，§3.5 补多会话注记。

评审另实测三子门禁基线全绿（`--check` + golden + 41 码），与 spec §3.3/§6 主张一致。
