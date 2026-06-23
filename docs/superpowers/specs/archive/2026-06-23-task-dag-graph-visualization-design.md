# TaskSystem Debug DAG 图可视化

> 将 debug 页面"任务"标签中的 ASCII DAG 替换为 vis-network 交互式有向图。

## 目标

TaskSystem 的 Planner 产出 DAG（有向无环图）节点和边，当前 `renderDAG()` 用 ASCII 字符拼接展示，不直观。替换为 CDN 引入的 vis-network，几行配置出图，节点颜色/图标反映执行状态，支持实时更新。

## 不改的范围

- `Module/task_system/` — 不做任何改动
- `Module/task_system/debug_api.py` — 数据格式已含 `nodes` + `edges`，无需改
- SSE 协议 — `task_new` / `task_update` / `node_update` / `task_done` 事件不变
- System 标签 Scheduler 的 Task Tree — 保持原有 JSON 展示

## 技术选型

**vis-network**（vis.js 的独立网络图模块）：~200KB（gzipped ~60KB），内置 hierarchical layout，一条 `<script>` 从 CDN 引入，零构建配置。

CDN 地址：`https://unpkg.com/vis-network@9.1.6/dist/vis-network.min.js`

## 数据流

```
TaskDebugAPI._collect_snapshot()
  → SSE push (task_new / task_update / node_update / task_done)
  → 前端 renderAllTasks() / updateTaskCard() / updateNodeInCard()
  → 每个 task card 内的 <div class="task-dag">
      ├ 旧: ASCII text (renderDAG)
      └ 新: vis.Network 实例
```

## 节点样式映射

节点状态 → 视觉：

| NodeStatus | 颜色 | 边框 |
|-----------|------|------|
| pending | `#9e9e9e`（灰） | dashed |
| running | `#42a5f5`（蓝） | solid |
| success | `#66bb6a`（绿） | solid |
| failed | `#ef5350`（红） | solid |
| unrecoverable | `#c62828`（深红） | solid |
| skipped | `#e0e0e0`（浅灰） | dashed |
| waiting_approval | `#ffa726`（橙） | dashed |
| cancelled | `#bdbdbd`（灰） | dotted |

- 节点标签：`node_id`（截断到 20 字符），hover tooltip 显示完整 ID + summary + attempt
- 节点形状：`box`（圆角矩形），宽度自适应标签长度
- 边：灰色实线箭头 `from → to`，线宽 1px，`arrows: "to"`

## 布局

```javascript
const DAG_OPTIONS = {
  layout: { hierarchical: { direction: "UD", levelSeparation: 80, nodeSpacing: 50, sortMethod: "directed" } },
  edges: { arrows: "to", color: "#aaa", width: 1 },
  nodes: { shape: "box", font: { size: 11, color: "#ccc" }, margin: 6, widthConstraint: { maximum: 160 } },
  physics: false,               // 关闭物理模拟，严格 hierarchical
  interaction: { dragNodes: false, zoomView: false },
};
```

方向 `UD`（上→下），层级间距 80px，节点间距 50px，`sortMethod: "directed"` 按拓扑排序自动分层。

## 容器尺寸

每个 task card 的 DAG 区域：
- 高度：`Math.min(400, Math.max(150, nodes.length * 60))` px
- 宽度：100%（填充 card 宽度）
- 超过 400px 高度时 card 内部滚动

## 实时更新策略

**关键约定**：SSE 数据字段名是 `node_id`，vis.js 要求 `id`。`initDAG()` 做映射，后续更新函数也必须做映射。

| SSE 事件 | vis.js 操作 |
|----------|-----------|
| `task_new` | 创建新 card → `new vis.Network(container, {nodes, edges}, DAG_OPTIONS)` |
| `node_update` | `network.body.data.nodes.update({id: d.node_id, color: ..., borderWidth: ...})` |
| `task_update` | 把 `newNodes` 的 `node_id` 映射为 `id`，edges 从 `dagInstances` 取 → `network.setData({nodes, edges})` |
| `task_done` | card 保持最终状态渲染，移入已完成区；`dagInstances.delete(taskId)` |

每个活跃 task card 在 `dagInstances` 中维护 `{ network, nodes, edges }`：

```javascript
const dagInstances = new Map();  // taskId → { network, nodes: DataSet, edges: Array }

function initDAG(container, taskId, nodes, edges) {
  // SSE 字段 node_id → vis.js 字段 id
  const items = nodes.map(n => ({ id: n.node_id, label: truncate(n.node_id, 20),
    color: STATUS_COLORS[n.status] || "#9e9e9e",
    title: `${n.node_id}\n${n.summary || ""}\nattempt: ${n.attempt}` }));
  const data = { nodes: new vis.DataSet(items), edges: new vis.DataSet(edges.map(e => ({ from: e[0], to: e[1] }))) };
  const network = new vis.Network(container, data, DAG_OPTIONS);
  dagInstances.set(taskId, { network, nodes: data.nodes, edges });  // 保存 edges 供 task_update 使用
  return network;
}
```

**node_update 实现**（注意 `id: d.node_id` 映射）：

```javascript
function updateNodeInCard(taskId, nodeId, newStatus, attempt) {
  const inst = dagInstances.get(taskId);
  if (!inst) return;
  const color = STATUS_COLORS[newStatus] || "#9e9e9e";
  inst.nodes.update({ id: nodeId, color: color, title: `${nodeId}\nattempt: ${attempt}` });
}
```

**task_update 实现**（nodes 映射 + edges 从缓存取）：

```javascript
function updateTaskCard(taskId, newStatus, newNodes) {
  const inst = dagInstances.get(taskId);
  if (!inst) return;
  // node_id → id 映射
  const items = newNodes.map(n => ({ id: n.node_id, label: truncate(n.node_id, 20),
    color: STATUS_COLORS[n.status] || "#9e9e9e",
    title: `${n.node_id}\n${n.summary || ""}\nattempt: ${n.attempt}` }));
  inst.network.setData({ nodes: items, edges: inst.edges.map(e => ({ from: e[0], to: e[1] })) });
  inst.nodes = inst.network.body.data.nodes;  // setData 创建新 DataSet，必须同步引用，否则后续 node_update 写到旧数据集
  // 更新元信息
  const card = document.getElementById('task-card-' + taskId);
  if (card) {
    card.className = 'task-card status-' + newStatus;
    const statusEl = card.querySelector('.task-status');
    if (statusEl) { statusEl.textContent = newStatus.toUpperCase(); statusEl.className = 'task-status status-' + newStatus; }
  }
}
```

**SSE 重连清理**（snapshot handler 开头）：

```javascript
taskES.addEventListener('snapshot', function(e) {
  dagInstances.clear();  // 清理旧的 vis.Network 实例，防止内存泄漏
  const tasks = JSON.parse(e.data);
  renderAllTasks(tasks);
  // ...
});
```

## 实现范围

### 改动文件

| 文件 | 改动 | 说明 |
|------|------|------|
| `static/debug.html` | ~80 行改动 | 加 CDN `<script>` + 替换 `renderDAG` 等 4 个函数 |

### 新增函数（均在 debug.html 内）

- `initDAG(container, taskId, nodes, edges)` — 创建 vis.Network 实例，存入 dagInstances
- `STATUS_COLORS` — 常量，status → color 映射
- `DAG_OPTIONS` — 常量，vis-network 分层布局配置
- `dagInstances` — `Map<taskId, { network, nodes, edges }>` 全局引用

### 删除函数

- `renderDAG()` — 旧 ASCII DAG 渲染
- `findMergeNodes()` — 辅助函数，不再需要
- `statusIcon()` — vis.js 用颜色+shape 替代 emoji

### 修改函数

- `insertTaskCardDOM()` — 容器准备好后调 `initDAG()` 替代 `renderDAG()`
- `updateTaskCard()` — 调 `network.setData()` 替代 `dagEl.textContent = renderDAG(...)`
- `updateNodeInCard()` — 调 `data.nodes.update()` 替代文本行替换
- `moveTaskToDone()` — `dagInstances.delete(taskId)` 清理 Network 实例，DOM 移入已完成区。图保持最终状态渲染，不再响应任何更新事件（此时 `dagInstances` 中已无该 taskId，后续 `node_update`/`task_update` 为 no-op）

### 不变

- `connectTasksSSE()` — SSE 事件监听映射不变
- `renderAllTasks()` / `insertTaskCard()` / `updateTasksStats()` — 流程不变
- 所有 CSS 样式 — 卡片结构不变

## 测试

1. 启动 agent：`python app.py --server`
2. 触发一个多节点任务（通过聊天让 Elysia 同时做 web_search + web_fetch）
3. 打开 `/debug` → 任务标签 → 确认 DAG 以 vis-network 分层图展示
4. 观察节点状态变化（pending → running → success/failed）→ 确认颜色实时更新
5. 任务完成 → 确认图冻结并移入"已完成"区
6. 多个任务并发 → 确认每张 card 独立维护各自 Network 实例

## 验收标准

- [ ] DAG 展示为 vis-network 交互式有向图（不再是 ASCII 文字）
- [ ] 节点颜色正确反映 NodeStatus（8 种状态各有区分）
- [ ] 布局为自上而下 hierarchical，节点不重叠
- [ ] SSE 实时更新正常（node_update 改颜色，task_update 刷新图，task_done 冻结）
- [ ] 任务完成后图冻结不可拖拽，移入已完成区
- [ ] 无 console 报错，无内存泄漏（dagInstances 在 task_done 时清理）
