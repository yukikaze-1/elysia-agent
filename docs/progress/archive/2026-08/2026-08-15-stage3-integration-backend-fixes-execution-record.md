# 阶段 3 真实联调后端整改执行记录（2026-08-15）

## 批次状态

| 项 | 值 |
|---|---|
| 上游需求 | `docs/review/archive/2026-08/2026-08-15-阶段3真实联调后端整改清单.md`（BE-S3-INT-P0-01/P0-02/P1-01） |
| spec | `docs/superpowers/specs/archive/2026-08/2026-08-15-stage3-integration-backend-fixes-design.md`（commit `3dcfce07`） |
| plan | `docs/superpowers/plans/archive/2026-08/2026-08-15-stage3-integration-backend-fixes.md`（commit `63f63d94`） |
| 执行 | subagent（general-purpose）执行 Task 1-5 |
| feature commits | `dc84415c`（契约枚举）、`183818f1`（sticker 搜索）、`ade71a23`（日期 fail-fast）、`6287c05d`（逐文件 kind） |
| 门禁 | test-quick **3581 passed** / test-normal **3689 passed**（基线 3548/3656 + 33 新增，含 final review low-2 补丁）/ ruff All passed / pyright **0 errors** / check-types 全绿 |

## 决策点定案（D1-D9）

| 决策 | 定案 | 理由 |
|---|---|---|
| D1 逐文件配对模型 | **A 邻接配对 + 单 kind 特判** | 前端 `uploadBatch` 经 `sniffKind` 每文件**总是**填 kind → 单 kind part 仅来自旧前端，特判无歧义；kind 未紧邻文件（连续/悬空）→ 400；无 kind 文件合法 → hint=None 魔数判定（仅 kind_count 0/≥2 可达） |
| D2 date_from>date_to | **A 保持空结果 200** | 前端日期选择器不产生倒置，纯防御不扩大范围 |
| D3 category/kind 白名单 | **B 契约层共享枚举**（用户定案） | 消除 handler/repo/upload_routes 三份字符串集合维护陷阱；枚举不被字段引用 → TS 快照零漂移 |
| D4 批次组织 | **A 合并单批次** | 三整改同服务阶段 3 联调，按 P0-01→P1-01→P0-02 顺序 |
| D5 悬空/歧义错误码 | 400 `CONTRACT_VALIDATION` | 与非法 kind 现状一致 |
| D6 kind part 上限 | 100（defense-in-depth） | 邻接+悬空+≤10 文件使实际 kind_count ≤ 11，分支不可达保留兜底 |
| D7 sticker 不进 UploadKind | 保持 4 值 | 贴图走 /api/stickers 不落附件（清单 2.2.6） |
| D8 日期严格格式 | `^\d{4}-\d{2}-\d{2}$` + `fromisoformat` 双重 | 拒紧凑/ISO 周历/ISO datetime/非法日历 |
| D9 契约响应零改动 | SearchMessageItem/AttachmentInfo 字段不变；snippet 空合法 | 清单 2.2.4/2.2.5 已定 |

## Review 记录

- **spec**：code-review 1 轮（general-purpose agent）10 条 issue 全部消化——HIGH-1 前端日期当前发 ISO 半开区间（非自然日）→ §1/§2/§9 补 P1-01 与 FE-S3-INT-P0-01 同步放行时序；MED-1 文件数检查保留；MED-2 邻接判定补「顺序歧义 400」；MED-3 单 kind 特判与 hint=None 语义明确；LOW×4 表述修正。
- **plan**：code-review 1 轮 7 条 issue 全部消化——MED-1 补 DST 边界测试；MED-2 Task 2 回归并入 repo 测试 + 新增 Task 5 全量门禁；LOW×5（失败预期/defense-in-depth/无 kind 测试/魔数落位/D2 回归）。
- **最终 commit review**（Phase 8，1 轮）：general-purpose agent 审 `dc84415c^..6287c05d`，结论 **yes 可验收**（无 high/medium）。两处 low 处理：
  - **low-1** 空 `kind=` 值行为变化——旧代码 falsy 跳过走魔数判定，新代码单 kind 特判统一为 `""` → 400 `CONTRACT_VALIDATION`「未知 kind」。spec 预期（§4.4 步骤 5 非法 kind 400），前端 `uploadBatch` 经 `sniffKind` 恒填非空 kind，无生产回归——**接受不改码**（已核实空串非 None 故合法路径）。
  - **low-2** D1 文档化的「kind_count ≥ 2 时未紧邻 kind 的文件 → hint=None 魔数判定」路径无测试锁定——补 `test_multi_kind_no_hint_file_magic_detected`（`kind,files,files,kind,files` 混合批 → `[image,image,video]`），TestPerFileKind **12 passed**。
  - agent 实证：406 回归 + 32 新测试 + `make check-types` 零漂移 + 既有 4 个关键测试（sticker_400/always_empty/text_400/tz_400）语义未变 + DST 测试有区分力（若日期不做时区换算会误纳入 04:30Z 消息）。
  - **外部约束**：HIGH-1 放行时序——P1-01 日期 fail-fast 须与前端 FE-S3-INT-P0-01 同步上线（后端先行会使当前发 ISO 时间戳的既有日期查询 200→400），属部署顺序非代码缺陷，见 §后续衔接。

## 执行偏差（已解决，非 spec 语义变更）

- **StickerPart 播种需带 `src`**（执行中发现）：plan 测试播种 `StickerPart(sticker_id=...)` 未带 `src`——构造时 pydantic v2 默认 `validate_default=False` 不校验，读回时 `_doc_to_message` 的 `validate_message_legacy_compat` 对显式存在的空 `src` 触发 `_validate_src` 校验失败（`core/schema/utils.py:67`）。修复：测试播种对齐生产构造 `ws_v2.py:243-244`（`src=f"/static/stickers/{id}.png"`），改两处测试种子，不改变任何 spec 语义（sticker 搜索行为/白名单/契约形状均未动）。
- **subagent 可靠**：本批执行 agent（general-purpose）顺畅完成 Task 1-5，未卡住——不同于 2026-08-14 批次 review agents 卡住的记录；spec/plan 两轮 review agent 均按时返回。
- **门禁范围**：执行 agent 跑 test-quick（3580）+ ruff/pyright/check-types；主审查追加 test-normal（3688）确认无 slow/external 意外；验收 agent 复跑 test-quick **3581** / test-normal **3689**（含 final review low-2 补丁后全量计数）。

## 验收结论（Phase 10，subagent 独立验收）

- **结论：本批可标记「已开发/已验证」**（清单 §6 定义）；**不得标记「已真实联调」**——B-02/B-03 真实联调报告与前端跨仓 Stage 3 门禁待前端配套（FE-S3-INT-P0-01/DEP-01/02）落地后另行完成。
- 6 个维度全通过：功能符合性（P0-01/P0-02/P1-01 全部目标行为 + 33 测试）、契约零漂移（枚举不被字段引用，`make check-types` 全绿 + 生成产物零变化）、四门禁（test-quick **3581** / test-normal **3689** / ruff / pyright 0 errors）、文档同步（CLAUDE.md/README/architecture/todo/执行记录）、决策点 D1-D9 可追溯、完成定义逐条核对。
- 验收发现唯一小文档不准（测试计数差 1，low-2 补丁在门禁记录后加入）——已在本节前同步修正（3580→3581、3688→3689、32→33 新增）。

## 后续衔接

- 本批完成后进入 B-02（历史/搜索/来源跳转）与 B-03（附件/多媒体）真实联调准入；
- **P1-01 放行时序**：日期 fail-fast 须与前端 FE-S3-INT-P0-01（改发 `YYYY-MM-DD` 自然日）同步——后端先行会使当前发 ISO 时间戳的前端日期查询 400（前端整改见 `elysia-frontend` 前端清单 FE-S3-INT-P0-01）；
- 前端配套：`HistoryCategory.sticker` 查询模型、逐文件 kind FormData 已冻结（前端清单 FE-S3-INT-DEP-01/02）。
