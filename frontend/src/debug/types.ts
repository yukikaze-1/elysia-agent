// Debug 页面类型 — 迁移到前后端契约单源（frontend_contract/debug.py 生成）
// 17 个前端接口（TokenRoundItem/DebugTokenStats/TimingStats/.../DebugData）全部由
// contract.generated.ts 提供；debug 页面专用局部类型保留在各自组件内（TaskCard/TasksTab 等）。
// ContentPart → DebugContentPart、TokenStats → DebugTokenStats（与 ws_message 形状不同，
// 按命名策略改名，避免同名触发生成器安全网）。
export * from '../types/contract.generated'
