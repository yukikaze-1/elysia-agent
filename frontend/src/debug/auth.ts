/**
 * Debug 仪表盘的管理面凭据（S2-F3，2026-09-12）。
 *
 * 背景：`/debug/api/*` 归管理面，`_check_auth` **只认 SERVER_AUTH_TOKEN**
 * （CR-03：产品 token 不授予管理面）。而此前这个前端**不携带任何凭据**——
 * 于是只要配了 `SERVER_AUTH_TOKEN`，整个数据面 401，仪表盘退化成空壳。
 *
 * 方案要点：
 * - token 由页面提示输入，存 `sessionStorage`（**不是** localStorage：关掉标签页
 *   即失效，缩小残留窗口）；
 * - **绝不放进 URL**——`docs/electron-backend-security.md` §1.3 明确要求凭据走
 *   Authorization 头，不进查询参数（查询参数会进访问日志/Referer/浏览器历史）；
 * - **绝不落日志**：本模块不 console.log token，错误信息只带 HTTP 状态码；
 * - 401 是唯一判据：不预设「后端是否需要 token」——没配 token 的后端会直接放行，
 *   此时闸门根本不出现（回环本地开发零打扰）。
 */

import { ref } from 'vue'

const STORAGE_KEY = 'elysia.debug.adminToken'

/** 最近一次 /debug/api 请求是否被 401 拒绝（驱动凭据闸门显隐）。 */
export const authRejected = ref(false)

/** 是否持有一个非空 token（仅用于按钮文案，不代表它一定有效）。 */
export const hasToken = ref(false)

function readStored(): string {
  try {
    return sessionStorage.getItem(STORAGE_KEY) ?? ''
  } catch {
    return ''   // 隐私模式 / 存储被禁——降级为「本次会话手工输入」
  }
}

let token = readStored()
hasToken.value = token !== ''

/** 写入 token（空串等价于清除）；返回是否非空。 */
export function setToken(value: string): boolean {
  token = value.trim()
  hasToken.value = token !== ''
  try {
    if (token) sessionStorage.setItem(STORAGE_KEY, token)
    else sessionStorage.removeItem(STORAGE_KEY)
  } catch {
    /* 存不下就只留在内存里 */
  }
  return hasToken.value
}

/** 清除 token（闸门上的「清除」按钮）。 */
export function clearToken(): void {
  setToken('')
}

/**
 * 与 `fetch` 同签名的包装：给 `/debug/api` 请求补上 Bearer 头，并据响应维护
 * `authRejected`。
 *
 * 为什么统一走这里而不是各处直接 fetch：凭据是**横切**关注点，漏掉任何一处调用
 * 都会表现为一个 401 页签。`frontend/src/debug/**` 与 `stores/debugStore.ts` 里
 * 不允许出现裸 `fetch(`，由 `scripts/check_debug_auth.py` 守。
 */
export async function debugFetch(
  input: RequestInfo | URL,
  init: RequestInit = {},
): Promise<Response> {
  const headers = new Headers(init.headers)
  if (token) headers.set('Authorization', `Bearer ${token}`)
  const res = await fetch(input, { ...init, headers })
  if (res.status === 401) authRejected.value = true
  else if (res.ok) authRejected.value = false
  return res
}
