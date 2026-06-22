# TaskSystem Debug DAG 图可视化 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将 debug 页面"任务"标签的 ASCII DAG 替换为 vis-network 交互式有向图。

**Architecture:** 引入 vis-network CDN，新增 `initDAG()` 创建图实例 + `dagInstances` Map 管理生命周期，重写 `insertTaskCardDOM()` / `updateTaskCard()` / `updateNodeInCard()` / `moveTaskToDone()` 4 个函数，删除 `renderDAG()` / `findMergeNodes()` / `statusIcon()` 3 个旧函数。后端完全不动。

**Tech Stack:** vis-network 9.1.6 (CDN), vanilla JavaScript

## Global Constraints

- 改动范围仅 `static/debug.html`
- 后端 `Module/task_system/` 和 `debug_api.py` 完全不动
- SSE 协议不变（`task_new` / `task_update` / `node_update` / `task_done`）
- SSE 数据字段名是 `node_id`，vis.js 要求 `id`——每次数据交互必须做映射
- `dagInstances` 结构：`Map<taskId, { network, nodes: DataSet, edges: Array }>`
- `setData` 后必须同步 `inst.nodes = inst.network.body.data.nodes`
- snapshot handler 开头必须 `dagInstances.clear()`
- 项目前端原则：零构建步骤、零外部框架依赖——CDN 脚本是唯一例外

---

### Task 1: 引入 vis-network CDN + 新增常量 + initDAG + truncate 工具函数

**Files:**
- Modify: `static/debug.html` — 在 `<script>` 标签前加 CDN `<script>`；在 `connectTasksSSE()` 上方新增 truncate / STATUS_COLORS / DAG_OPTIONS / dagInstances / initDAG

**Interfaces:**
- Produces:
  - `truncate(s, n)` — 字符串截断，`s.length <= n ? s : s.slice(0, n-1) + "…"`
  - `STATUS_COLORS` — `{ pending: "#9e9e9e", running: "#42a5f5", success: "#66bb6a", failed: "#ef5350", unrecoverable: "#c62828", skipped: "#e0e0e0", waiting_approval: "#ffa726", cancelled: "#bdbdbd" }`
  - `DAG_OPTIONS` — vis-network 分层布局配置对象（物理模拟关闭、交互关闭）
  - `dagInstances` — `new Map()` 全局变量
  - `initDAG(container, taskId, nodes, edges)` — 创建 vis.Network，存入 dagInstances

- [ ] **Step 1: 在 `<script>` 标签前添加 vis-network CDN**

找到 `static/debug.html` 的 `<script>` 标签（约 line 297），在其上方插入：

```html
<script src="https://unpkg.com/vis-network@9.1.6/dist/vis-network.min.js"></script>
```

然后在该 `<script>` 内部最前面（`connectTasksSSE` 等函数之前）添加新代码。

- [ ] **Step 2: 添加 truncate 工具函数和常量**

在 `// ═══════════════════════════════════════════════════════════════════` 注释块之后、`let taskES = null;` 之前插入：

```javascript
// ── 工具函数 ──

function truncate(s, n) {
  if (!s) return '';
  return s.length <= n ? s : s.slice(0, n - 1) + '…';
}

// ── vis-network 配置 ──

const STATUS_COLORS = {
  pending: '#9e9e9e',
  running: '#42a5f5',
  success: '#66bb6a',
  failed: '#ef5350',
  unrecoverable: '#c62828',
  skipped: '#e0e0e0',
  waiting_approval: '#ffa726',
  cancelled: '#bdbdbd',
};

const DAG_OPTIONS = {
  layout: { hierarchical: { direction: 'UD', levelSeparation: 80, nodeSpacing: 50, sortMethod: 'directed' } },
  edges: { arrows: 'to', color: '#aaa', width: 1 },
  nodes: { shape: 'box', font: { size: 11, color: '#ccc' }, margin: 6, widthConstraint: { maximum: 160 } },
  physics: false,
  interaction: { dragNodes: false, zoomView: false },
};

const dagInstances = new Map();  // taskId → { network, nodes: DataSet, edges: Array }

// ── DAG 图初始化 ──

function initDAG(container, taskId, nodes, edges) {
  // SSE 字段 node_id → vis.js 字段 id
  const items = nodes.map(function(n) {
    return {
      id: n.node_id,
      label: truncate(n.node_id, 20),
      color: STATUS_COLORS[n.status] || '#9e9e9e',
      title: n.node_id + '\n' + (n.summary || '') + '\nattempt: ' + (n.attempt || 0),
    };
  });
  const edgeItems = (edges || []).map(function(e) { return { from: e[0], to: e[1] }; });
  const data = { nodes: new vis.DataSet(items), edges: new vis.DataSet(edgeItems) };
  const network = new vis.Network(container, data, DAG_OPTIONS);
  dagInstances.set(taskId, { network: network, nodes: data.nodes, edges: edges || [] });
  return network;
}
```

- [ ] **Step 3: 验证页面能正常加载**

启动 agent，打开 `/debug`，确认：
- 浏览器 Console 无 JS 报错
- `vis` 全局对象存在（在 Console 输入 `typeof vis` 回车 → `"object"`）
- `dagInstances` 为空 Map（尚无活跃任务）

Run:
```bash
# 启动 agent 后访问 http://localhost:8765/debug
# 打开浏览器 Console，输入: typeof vis
# 预期输出: "object"
```

- [ ] **Step 4: Commit**

```bash
git add static/debug.html
git commit -m "feat(debug): 引入 vis-network CDN + STATUS_COLORS/DAG_OPTIONS/initDAG"
```

---

### Task 2: 重写 SSE 事件处理函数，接入 vis-network

**Files:**
- Modify: `static/debug.html` — 重写 `insertTaskCardDOM()` / `updateTaskCard()` / `updateNodeInCard()` / `moveTaskToDone()`，修改 `snapshot` handler，删除 `renderDAG()` / `findMergeNodes()` / `statusIcon()`

**Interfaces:**
- Consumes: `dagInstances`, `initDAG()`, `STATUS_COLORS`, `truncate()` (from Task 1)
- Produces: 重写后的 `insertTaskCardDOM()` / `updateTaskCard()` / `updateNodeInCard()` / `moveTaskToDone()` — 签名不变，调用方（`connectTasksSSE` / `insertTaskCard` / `renderAllTasks`）无需改动

- [ ] **Step 1: 删除旧的 DAG 渲染函数**

删除以下 3 个函数（约 lines 1064–1123）：
- `findMergeNodes(edges)` — lines 1064–1070
- `renderDAG(nodes, edges)` — lines 1072–1111
- `statusIcon(status)` — lines 1113–1123

保留它们上方的 `// ── DAG 渲染 ──` 注释块和下方的 `// ── 渲染 ──` 注释块。

- [ ] **Step 2: 重写 insertTaskCardDOM()**

替换函数体（约 lines 1153–1173）：

```javascript
function insertTaskCardDOM(container, t) {
  var existing = document.getElementById('task-card-' + t.task_id);
  if (existing) existing.remove();
  var progress = 0;
  var nodes = t.nodes || [];
  if (nodes.length > 0) {
    var done = nodes.filter(function(n) { return n.status === 'success' || n.status === 'skipped'; }).length;
    progress = Math.round(done / nodes.length * 100);
  }
  var dagId = 'dag-' + t.task_id;
  var height = Math.min(400, Math.max(150, nodes.length * 60));
  var html = '<div class="task-card status-' + t.status + '" id="task-card-' + t.task_id + '">' +
    '<div class="task-header">' +
      '<span class="task-id">' + t.task_id + '</span>' +
      '<span class="task-status status-' + t.status + '">' + t.status.toUpperCase() + '</span>' +
    '</div>' +
    '<div class="task-meta">Replan: ' + (t.plan_version - 1) + '/' + t.plan_version +
    ' | 节点: ' + (nodes.length) + ' | 队列: ' + (t.mailbox_depth || 0) + '</div>' +
    '<div class="task-progress-bar"><div class="fill" style="width:' + progress + '%"></div></div>' +
    '<div class="task-dag" id="' + dagId + '" style="height:' + height + 'px;"></div>' +
  '</div>';
  container.insertAdjacentHTML('afterbegin', html);

  // 创建 vis-network 图
  if (nodes.length > 0) {
    initDAG(document.getElementById(dagId), t.task_id, nodes, t.edges || []);
  }
}
```

关键变更：
- 去掉 `data-edges` HTML 属性（edges 由 `dagInstances` 管理）
- `<div class="task-dag">` 改为带 `id="dag-{task_id}"` 和固定 `height` 的容器
- 末尾调用 `initDAG()` 创建 vis-network 图

- [ ] **Step 3: 重写 updateTaskCard()**

替换函数体（约 lines 1175–1199）：

```javascript
function updateTaskCard(taskId, newStatus, newNodes) {
  // 1. 更新 vis-network 图
  var inst = dagInstances.get(taskId);
  if (inst && newNodes) {
    var items = newNodes.map(function(n) {
      return {
        id: n.node_id,
        label: truncate(n.node_id, 20),
        color: STATUS_COLORS[n.status] || '#9e9e9e',
        title: n.node_id + '\n' + (n.summary || '') + '\nattempt: ' + (n.attempt || 0),
      };
    });
    inst.network.setData({ nodes: items, edges: inst.edges.map(function(e) { return { from: e[0], to: e[1] }; }) });
    inst.nodes = inst.network.body.data.nodes;  // setData 创建新 DataSet，必须同步引用
  }

  // 2. 更新 card DOM 元信息
  var card = document.getElementById('task-card-' + taskId);
  if (!card) return;
  var statusEl = card.querySelector('.task-status');
  if (statusEl) {
    statusEl.textContent = newStatus.toUpperCase();
    statusEl.className = 'task-status status-' + newStatus;
  }
  card.className = 'task-card status-' + newStatus;

  // 3. 更新进度条
  if (newNodes && newNodes.length > 0) {
    var done = newNodes.filter(function(n) { return n.status === 'success' || n.status === 'skipped'; }).length;
    var progress = Math.round(done / newNodes.length * 100);
    var fill = card.querySelector('.task-progress-bar .fill');
    if (fill) fill.style.width = progress + '%';
  }
}
```

- [ ] **Step 4: 重写 updateNodeInCard()**

替换函数体（约 lines 1201–1215）：

```javascript
function updateNodeInCard(taskId, nodeId, newStatus, attempt) {
  var inst = dagInstances.get(taskId);
  if (!inst) return;
  var color = STATUS_COLORS[newStatus] || '#9e9e9e';
  var title = nodeId + '\nattempt: ' + (attempt || 0);
  inst.nodes.update({ id: nodeId, color: color, title: title });
}
```

关键：`id` 不是 `node_id`——vis.js DataSet.update() 用 `id` 字段定位。

- [ ] **Step 5: 重写 moveTaskToDone()**

替换函数体（约 lines 1217–1230）：

```javascript
function moveTaskToDone(taskId, status, error) {
  dagInstances.delete(taskId);  // 清理 Network 实例，后续 node_update/task_update 自然 no-op

  var card = document.getElementById('task-card-' + taskId);
  if (!card) return;
  card.remove();
  var doneList = document.getElementById('tasks-done-list');
  var msg = status.toUpperCase();
  if (error) msg += ' — ' + error;
  var row = document.createElement('div');
  row.className = 'task-done-row';
  row.innerHTML = '<b>' + taskId + '</b>: ' + msg;
  doneList.insertBefore(row, doneList.firstChild);
  document.getElementById('tasks-done-section').style.display = '';
  updateTasksStats();
}
```

关键变更：函数开头 `dagInstances.delete(taskId)` ——图保持最终状态渲染直到 DOM 被移除，之后任何 `node_update`/`task_update` 事件因 `dagInstances.get(taskId)` 返回 `undefined` 而静默跳过。

- [ ] **Step 6: 在 snapshot handler 中添加 dagInstances.clear()**

修改 `connectTasksSSE()` 内的 `snapshot` event listener（约 line 1016）：

```javascript
taskES.addEventListener('snapshot', function(e) {
  dagInstances.clear();  // SSE 重连/首次连接时清理旧 Network 实例，防止内存泄漏
  const tasks = JSON.parse(e.data);
  renderAllTasks(tasks);
  document.getElementById('tasks-stats').innerHTML =
    '运行中: ' + tasks.filter(function(t) { return t.status === 'running'; }).length +
    ' | SSE: <span style="color:#66bb6a">●已连接</span>';
  taskReconnectDelay = 1000;
});
```

- [ ] **Step 7: 全文检查清单**

确认以下项：
- [ ] debug.html 中不再存在 `renderDAG` / `findMergeNodes` / `statusIcon` 字符串
- [ ] `data-edges` HTML 属性不再出现（edges 由 dagInstances 管理）
- [ ] 所有 vis.js 数据交互中，SSE 的 `node_id` 均映射为 `id`
- [ ] `setData` 调用后紧跟 `inst.nodes = inst.network.body.data.nodes`
- [ ] `moveTaskToDone` 开头调用 `dagInstances.delete(taskId)`

Run:
```bash
grep -n 'renderDAG\|findMergeNodes\|statusIcon\|data-edges' /home/yomu/Agent/static/debug.html
# 预期输出: (no matches)
```

- [ ] **Step 8: 手动验证（端到端）**

```bash
conda activate Elysia
python app.py --server
```

1. 打开 `http://localhost:8765/debug` → 切换到"任务"标签
2. 打开 `http://localhost:8765/` → 聊天页面，发送一个需要多步骤的任务（例如"帮我在网上搜一下 Python 3.13 的新特性，然后抓取第一个结果的页面内容"）
3. 切回 debug 页面 → 任务标签 → 确认：
   - DAG 显示为 vis-network 分层有向图（节点用圆角矩形，边带箭头）
   - 节点颜色随状态变化（pending 灰 → running 蓝 → success 绿）
   - 多个任务并发时，每张 card 的图独立运行
4. 等待任务完成 → 确认 card 移入"已完成"区，图消失
5. 检查浏览器 Console → 无报错

- [ ] **Step 9: Commit**

```bash
git add static/debug.html
git commit -m "feat(debug): TaskSystem DAG 替换为 vis-network 交互式有向图"
```

---

## 自审清单

实施完后对照 spec 确认：

| spec 要求 | Task | 状态 |
|----------|------|------|
| DAG 展示为 vis-network 交互式有向图 | Task 2 Step 2 | — |
| 节点颜色正确反映 8 种 NodeStatus | Task 1 Step 2 (STATUS_COLORS) | — |
| 布局为自上而下 hierarchical | Task 1 Step 2 (DAG_OPTIONS) | — |
| node_update 改颜色 | Task 2 Step 4 | — |
| task_update 刷新图 + 进度条 | Task 2 Step 3 | — |
| task_done 冻结并移入已完成区 | Task 2 Step 5 | — |
| SSE 重连清理 dagInstances | Task 2 Step 6 | — |
| setData 后同步 inst.nodes | Task 2 Step 3 | — |
| node_id → id 映射 | Task 1 Step 2 + Task 2 Step 3/4 | — |
| 后端完全不动 | 全 Task | — |
