# 前端 — Vue 3 + Vite + Pinia + TypeScript

Elysia Agent 的 Web 聊天界面 + Debug 仪表盘。

## 架构

```
frontend/
├── index.html                  # 聊天页面 HTML 入口
├── debug.html                  # Debug 页面 HTML 入口（Vite 多页）
├── vite.config.ts              # Vite 配置（代理 :8765 + 多入口）
├── package.json
├── tsconfig.json
└── src/
    ├── main.ts                 # 聊天应用入口（Pinia + marked + hljs）
    ├── App.vue                 # 根组件：全局快捷键 + approval 透传
    │
    ├── types/protocol.ts       # 契约类型 re-export（单源 frontend_contract → contract.generated.ts，ServerMessage 判别联合 38 成员；勿手改）
    │
    ├── stores/                 # Pinia 状态管理
    │   ├── connectionStore.ts  # WebSocket 连接 + 指数退避重连
    │   ├── chatStore.ts        # 消息列表 + delta 流式追加 + TTS 排队 + 历史加载
    │   ├── audioStore.ts       # TTS 缓冲/播放 + 麦克风激活
    │   ├── settingsStore.ts    # 设置项 + localStorage 同步 + 模型切换
    │   ├── toolsStore.ts       # 工具插件列表
    │   └── debugStore.ts       # Debug 轮询 + Tab 状态 + 分页
    │
    ├── composables/            # 可复用逻辑
    │   ├── useWebSocket.ts     # WS 消息路由（类型安全的 ServerMessage 分发）
    │   ├── useAbortController.ts # AbortController 封装（请求取消）
    │   ├── useImageUpload.ts   # 共享图片上传 + 拖拽处理（InputBar + MessageList 复用）
    │   └── useLightbox.ts      # 全局图片灯箱状态
    │
    ├── components/
    │   ├── layout/AppLayout.vue          # 三栏布局
    │   ├── character/                    # 左侧角色面板
    │   │   ├── CharacterPanel.vue
    │   │   ├── AvatarDisplay.vue         # Live2D / 静态图
    │   │   └── CharStatus.vue            # 状态点 + 心情 + 模型名
    │   ├── chat/                         # 消息渲染
    │   │   ├── MessageList.vue           # 消息列表（TransitionGroup + 滚动加载）
    │   │   ├── ChatMessage.vue           # 单条消息路由
    │   │   ├── ChatBubble.vue            # 气泡内容（文本/markdown + XSS 消毒）
    │   │   ├── ToolCard.vue              # 工具调用卡片
    │   │   ├── InnerVoice.vue            # 内心独白气泡
    │   │   ├── TokenLabel.vue            # Token 统计
    │   │   ├── TimeDivider.vue           # 时间分隔符
    │   │   └── TypingIndicator.vue       # 打字指示器
    │   ├── input/
    │   │   ├── InputBar.vue              # 输入框 + 附件 + 麦克风 + 发送
    │   │   └── PreviewBar.vue            # 图片预览条
    │   ├── panels/
    │   │   ├── SettingsPanel.vue         # 设置面板（模型选择 + 重启/关机）
    │   │   └── ToolsPanel.vue            # 工具管理面板
    │   ├── ui/
    │   │   ├── ToggleSwitch.vue          # 开关组件（aria 完整）
    │   │   ├── ApprovalDialog.vue        # HITL 审批弹窗
    │   │   └── ModelSelector.vue         # 模型选择下拉框
    │   └── shared/
    │       ├── Lightbox.vue              # 图片灯箱（v-if 响应式控制）
    │       └── CodeBlock.vue             # 代码块（highlight.js + 复制）
    │
    ├── styles/
    │   ├── variables.css                 # 全局 CSS 变量
    │   └── message-layout.css            # 共享消息布局样式（ChatMessage + TypingIndicator）
    │
    ├── utils/
    │   ├── escape.ts                     # HTML 转义工具
    │   └── format.ts                     # 数字格式化（fmt: 1.0k）
    │
    └── debug/                            # 独立 Debug 应用（Vite 多入口）
        ├── main.ts                       # 入口 + Pinia
        ├── App.vue                       # Tab shell（概览 | 数据库 | 任务）+ 凭据闸门
        ├── auth.ts                       # 管理面凭据：sessionStorage + debugFetch（唯一允许裸 fetch 的地方）
        ├── sse.ts                        # SSE over fetch（EventSource 设不了 Authorization 头）
        ├── types.ts                      # DebugData 等 TS 接口
        └── components/
            ├── TokenGate.vue             # 凭据闸门（仅后端 401 时出现）
            ├── OverviewTab.vue           # 16 面板 2 列 grid
            ├── DatabaseTab.vue           # 容器，切换时加载子面板
            ├── LtmPanel.vue              # LTM CRUD（搜索防抖 + CRUD + 清空）
            ├── SessionPanel.vue          # 会话管理
            ├── ArchivePanel.vue          # 消息归档（搜索防抖 + 翻页）
            ├── TasksTab.vue              # SSE（fetch 流式）+ 指数退避重连
            ├── TaskCard.vue              # 任务卡片：vis-network DAG（实时更新）
            ├── shared/
            │   ├── PaginationBar.vue     # 分页导航（offset 语义）
            │   ├── ConfirmModal.vue      # 确认弹窗（Escape + 自动聚焦）
            │   └── ExpandableText.vue    # 文本截断（Unicode 安全）
            └── modals/
                ├── PromptViewer.vue      # LLM Prompt 详情
                ├── ResponseViewer.vue    # LLM Response 详情
                ├── VersionHistory.vue    # L2/C6 版本历史 + contentType 监听
                └── ImageLightbox.vue     # 图片全屏灯箱（加载/错误状态）
```

## 开发

```bash
cd frontend
npm install
npm run dev       # → Vite dev server :5173（自动代理 WS + API 到 :8765）
```

## 构建

```bash
npm run build     # → 构建产物输出到 ../static/（index.html + debug.html + assets/）
```

## 技术要点

### WebSocket 消息流

```
connectionStore.onmessage → _onMessage
→ useWebSocket.handleMessage（ServerMessage 类型收窄 + switch）
→ 各 store action（appendDelta, setMood, updateCapabilities 等）
```

### 安全

- **XSS 防护**：`ChatBubble.vue` 中 `marked.parse()` 输出经 DOMPurify 消毒后再通过 `v-html` 注入
- **WebSocket 类型安全**：消息从 `as any` 迁移为 `ServerMessage` 判别联合类型收窄

### Store 设计

- `connectionStore`：WebSocket 生命周期 + 指数退避重连（retryCount 正确递增）
- `chatStore` ↔ `audioStore` 存在循环依赖（延迟初始化解决），双向交互文档化在 store 头部注释
- `settingsStore`：`updateCapabilities()` action 封装能力标志更新；`switchModel()` 检查 send 返回值

### 共享模块

| 模块 | 使用者 |
|------|--------|
| `composables/useImageUpload.ts` | InputBar + MessageList |
| `utils/format.ts` | CharStatus + TokenLabel |
| `styles/message-layout.css` | ChatMessage + TypingIndicator |

### 无障碍（a11y）

- `ToggleSwitch`: `role="switch"` + `aria-checked` + `aria-label` + 键盘事件
- `TokenLabel`: `role="button"` + `tabindex="0"` + 键盘事件
- `ConfirmModal`: Escape 关闭 + 自动聚焦确认按钮
- `ExpandableText`: Unicode 安全截断（`[...text]` 替代 `slice`）

## 相关文档

- [项目 CLAUDE.md](../CLAUDE.md) — 完整架构文档
- [前后端契约单源 frontend_contract](../frontend_contract/README.md) — 类型唯一权威来源（生成 contract.generated.ts）
- [前端代码审查 TODO](../docs/archive/frontend_todo.md) — 已知问题清单及修复状态（已归档，剩余活项并入 [todo.md](../todo.md)）
- [后端 Input 层 README（REST 路由索引）](../core/input/README.md)
- [WebSocket 协议](../core/input/ws_handler.py)
