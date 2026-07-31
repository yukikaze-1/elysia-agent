# Debug 页面改进设计 — 2026-06-01

> 范围: `static/debug.html` (246 行单文件)
> 方式: 增量修补，不改架构

---

## 背景

debug 页面轮询 `/debug/api` 展示系统内部状态。通过交叉对比前端渲染和后端 `DebugInfo` dataclass (27 字段)，发现 3 个数据缺口、2 个 bug、5 个 UX 问题。

---

## 改进清单

### 数据补全

**1. `session_params` 展示**
Memory 卡片新增一行，格式化显示会话参数字典。后端已返回此字段但前端从未渲染。

**2. `llm_mock` 死代码移除 + 正确检测**
`debug.html:108` 检查 `info.llm_mock`，但 `DebugInfo` dataclass 无此字段。改为检查 `info.llm_model` 字符串是否包含 `"mock"`（大小写不敏感）。

**3. 工具参数 description**
Tools 卡片中每个 `.param-tag` 目前只显示 name + type + required。后端每个 parameter 都有 `description` 字段，加为 `title` 属性，hover 时显示。

### Bug 修复

**4. XSS 防护**
以下后端值直接拼入 HTML 未 escape:
- `info.fsm_state`, `info.fsm_previous` (System 卡片)
- `info.llm_model` (System 卡片)
- `info.currently_executing` (Tools 卡片顶部 banner)
- 工具 `t.name`, `t.description`, 参数 `p.name`, `p.type` (Tools 卡片)
- `t.name`, `t.args`, `t.result`, `t.error` (Tool History)
- `c.type`, `c.content` (Consolidation)
- `m.role`, `m.content` (Session Recent)
- `e.metadata?.type`, `e.text` (Long-term Memory)

修复方式: 所有动态值外层加 `esc()`。

**5. 轮询失败退避**
当前: 固定 `setInterval(poll, 2000)`，失败不减速。
改为: 成功 → 2s 间隔。连续失败 → 2s → 4s → 8s → max 30s。成功后重置。

### UX 优化

**6. 暂停/恢复**
header 加"暂停"按钮。点击暂停 → `clearInterval(timer)`，按钮变"恢复"。点击恢复 → 立即 poll 一次 + 重新 setInterval。

**7. 减少闪烁**
`render()` 开头浅比较 `info` 关键字段（`fsm_state`, `processing`, `currently_executing`, `timing_stats`）与上次数据。全相同则只更新 `#updated` 时间戳，跳过 DOM 重建。

**8. 导出按钮**
header 加"复制 JSON"按钮。点击 → `navigator.clipboard.writeText(JSON.stringify(lastInfo, null, 2))`。按钮反馈"已复制"2 秒。

**9. 消息可展开**
`session_recent` 和 `lt_recent` 的截断文本改为 `class="truncated"` + `onclick` 展开/收起。初始显示 90 字符，点击展开全文。

**10. 响应式**
加 `@media (max-width: 700px)` 媒体查询，grid 切换为单列。

---

## 不纳入

- `timing_history` 原始 100 条记录的时间线图（需要图表库或 canvas，过度设计）
- WebSocket 实时推送替代轮询（需要后端改动）
- debug 页面模块化拆分（用户选择增量修补）

---

## 验证

```bash
# 启动服务
python app.py --server

# 浏览器打开 /debug
# 1. 确认所有卡片正常渲染
# 2. session_params 显示在 Memory 卡片
# 3. hover 工具参数 → 显示 description
# 4. 缩小窗口 → 单列布局
# 5. 点击暂停 → 轮询停止，按钮变"恢复"
# 6. 点击恢复 → 恢复轮询
# 7. 点击复制 JSON → 剪贴板有完整数据
# 8. 点击截断消息 → 展开/收起
# 9. 断开连接 → 退避重试
```
