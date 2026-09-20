/**
 * KB-56 回归：`chatStore` 历史分页的**会话归属**不变量。
 *
 * Finding（Batch 19 CONFIRMED-机制，Batch 20 复核后**加重**）：`loadMoreMessages` 打的是
 * **全库** `GET /api/messages`（跨会话 compat 端点，无会话过滤），而 WS 回放项**不含
 * `_id`** ⇒ 首页游标恒空 ⇒ 首页恒为"全局最新 50 条"。更糟的是全库端点返回的项**带
 * `_id`**（`Message.mongo_id` 的 wire 别名），旧代码会把它 latch 成游标 ⇒ 之后持续沿着
 * **跨会话归档**往回翻，污染是无界的。
 *
 * 本文件驱动**真实 store**（真 Pinia + 真 fetch 替身），断言四条不变量：
 * - **SI1** 会话域异步响应只能改它**发起时**捕获的那个会话；
 * - **SI2** 切会话不会重定向已在途的请求；
 * - **SI3** 陈旧响应可以丢弃，但**绝不能**污染别的会话；
 * - **SI4** 结论与响应到达顺序无关。
 *
 * 替身保真度（重要）：回放帧按**真实契约** `SessionMessageItem` 构造——它**没有**
 * `_id`（合约里根本没这个字段）；历史页按**真实契约** `MessagesPageResponse` 构造
 * ——`cursor` 是响应的顶层字段，页内项带 `_id`。
 */

import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'

import { useChatStore, type ChatMessage } from './chatStore'

// ---- 环境替身 ----

function installLocalStorage(): void {
  const store = new Map<string, string>()
  vi.stubGlobal('localStorage', {
    getItem: (k: string) => (store.has(k) ? store.get(k)! : null),
    setItem: (k: string, v: string) => void store.set(k, String(v)),
    removeItem: (k: string) => void store.delete(k),
    clear: () => store.clear(),
    key: (i: number) => Array.from(store.keys())[i] ?? null,
    get length() {
      return store.size
    },
  })
}

interface ServerMsg {
  message_id: string
  role: string
  timestamp: string
  parts: { content: string }[]
  _id: string
}

/** 历史页里的一条消息（`rest_contract.Message`：带 `_id`）。 */
function pageMsg(id: string, content: string, mongoId: string, role = 'user'): ServerMsg {
  return {
    message_id: id,
    role,
    timestamp: '2026-09-20T00:00:00+00:00',
    parts: [{ content }],
    _id: mongoId,
  }
}

/**
 * 回放帧里的一条消息（`SessionMessageItem`：**没有** `_id`，也没有 `session_id`）。
 * 刻意逐字对齐契约形状——带 `_id` 的替身会掩盖"游标必须来自会话域响应"这一事实。
 */
function replayMsg(id: string, content: string, role = 'user') {
  return {
    message_id: id,
    role,
    timestamp: '2026-09-20T00:00:00+00:00',
    parts: [{ content }],
  }
}

interface PageBody {
  messages: ServerMsg[]
  has_more: boolean
  total: number
  cursor: string | null
}

function page(
  messages: ServerMsg[],
  opts: { hasMore?: boolean; total?: number; cursor?: string | null } = {},
): PageBody {
  return {
    messages,
    has_more: opts.hasMore ?? messages.length > 0,
    total: opts.total ?? messages.length,
    cursor: opts.cursor !== undefined ? opts.cursor : (messages[messages.length - 1]?._id ?? null),
  }
}

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'content-type': 'application/json' },
  })
}

/** 一次可控的 fetch 响应：由测试决定何时 resolve（复现"在途 + 切会话"）。 */
function deferred<T>() {
  let resolve!: (v: T) => void
  const promise = new Promise<T>((res) => {
    resolve = res
  })
  return { promise, resolve }
}

interface FetchCall {
  path: string
  sessionId: string | null
  before: string | null
  limit: string | null
}

type Route = (call: FetchCall) => Response | Promise<Response>

/** 安装 fetch 替身：按 URL 记录调用并按会话 id 路由响应。 */
function installFetch(
  routes: Record<string, Route | PageBody>,
  globalFallback?: PageBody,
) {
  const calls: FetchCall[] = []
  const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
    const parsed = new URL(String(input), 'http://localhost')
    const m = parsed.pathname.match(/^\/api\/sessions\/([^/]+)\/messages$/)
    const call: FetchCall = {
      path: parsed.pathname,
      sessionId: m ? decodeURIComponent(m[1]) : null,
      before: parsed.searchParams.get('before'),
      limit: parsed.searchParams.get('limit'),
    }
    calls.push(call)
    if (!m) {
      // 全库端点：本 store **绝不允许**再打它（KB-56 的注入源）。
      // 默认返回空页；需要证明"打了它就会串会话"的用例可注入 `globalFallback`
      // ——那是**真实** `/api/messages` 的形状（跨会话、带 `_id`）。
      return jsonResponse(globalFallback ?? { messages: [], has_more: false, total: 0 })
    }
    const route = routes[call.sessionId!]
    if (!route) {
      return jsonResponse({ error: 'SESSION_NOT_FOUND' }, 404)
    }
    return typeof route === 'function' ? await route(call) : jsonResponse(route)
  })
  vi.stubGlobal('fetch', fetchMock)
  return { calls, fetchMock }
}

function contents(store: ReturnType<typeof useChatStore>): string[] {
  return (store.messages as ChatMessage[]).map((m) =>
    typeof m.content === 'string' ? m.content : JSON.stringify(m.content),
  )
}

/** 模拟 WS `session` 回放帧（`useWebSocket` 的调用形状）。 */
function replay(store: ReturnType<typeof useChatStore>, sessionId: string, msgs: unknown[]): void {
  store.restoreFromServer(msgs, undefined, sessionId)
}

beforeEach(() => {
  installLocalStorage()
  setActivePinia(createPinia())
})

// ---- SI-T1 / SI-T2：跨会话污染 ----

describe('KB-56 SI-T1：A 的"加载更多"在切到 B 之后返回', () => {
  it('A 的历史页绝不进入 B 的列表', async () => {
    const store = useChatStore()
    const gate = deferred<Response>()
    installFetch({
      sess_A: () => gate.promise,
      sess_B: page([pageMsg('b1', 'B live', 'id_b1')]),
    })

    replay(store, 'sess_A', [replayMsg('a9', 'A live')])
    expect(store.activeSessionId).toBe('sess_A')

    const loading = store.loadMoreMessages() // 在途：归属 sess_A
    // 切到 B（B 自己的回放到达）
    replay(store, 'sess_B', [replayMsg('b1', 'B live')])
    expect(store.activeSessionId).toBe('sess_B')
    const before = contents(store)

    // A 的响应现在才回来
    gate.resolve(jsonResponse(page([pageMsg('a1', 'A older secret', 'id_a1')], { total: 9 })))
    await loading

    expect(contents(store)).toEqual(before) // B 保持纯净
    expect(contents(store).join('|')).not.toContain('A older secret')
    expect(store.earliestMessageId).toBeNull() // 陈旧响应不得推进 B 的游标
  })
})

describe('KB-56 SI-T2：A→B→C 乱序返回', () => {
  it('每条历史页只进入自己的会话；A/B 的迟到响应都不污染 C', async () => {
    const store = useChatStore()
    const gateA = deferred<Response>()
    const gateB = deferred<Response>()
    installFetch(
      {
        sess_A: () => gateA.promise,
        sess_B: () => gateB.promise,
        sess_C: page([pageMsg('c1', 'C live', 'id_c1')]),
      },
      // 真实全库端点形状（跨会话污染源）：若被测代码打了它，这里就会把别会话的
      // 消息交出去——正是 KB-56 的注入机制。
      page([pageMsg('z9', 'OTHER SESSION LEAK', 'id_z9')], { hasMore: true, total: 99 }),
    )

    replay(store, 'sess_A', [replayMsg('a9', 'A live')])
    const loadingA = store.loadMoreMessages()

    replay(store, 'sess_B', [replayMsg('b9', 'B live')])
    const loadingB = store.loadMoreMessages()

    replay(store, 'sess_C', [replayMsg('c1', 'C live')])
    const beforeC = contents(store)

    // 乱序：B 先回，A 后回（顺序不应影响结论）
    gateB.resolve(jsonResponse(page([pageMsg('b1', 'B older', 'id_b1')], { total: 9 })))
    await loadingB
    expect(contents(store)).toEqual(beforeC)

    gateA.resolve(jsonResponse(page([pageMsg('a1', 'A older', 'id_a1')], { total: 9 })))
    await loadingA

    const text = contents(store).join('|')
    expect(text).not.toContain('A older')
    expect(text).not.toContain('B older')
    expect(text).not.toContain('OTHER SESSION LEAK') // 绝不走全库端点
    expect(contents(store)).toEqual(beforeC)
  })

  it('迟到的**错误**响应同样不得改动会话状态', async () => {
    const store = useChatStore()
    const gateA = deferred<Response>()
    installFetch({
      sess_A: () => gateA.promise,
      sess_B: page([pageMsg('b1', 'B live', 'id_b1')]),
    })

    replay(store, 'sess_A', [replayMsg('a9', 'A live')])
    const loading = store.loadMoreMessages()
    replay(store, 'sess_B', [replayMsg('b1', 'B live')])
    const hasMoreBefore = store.hasMoreMessages

    gateA.resolve(new Response('boom', { status: 500 }))
    await loading

    expect(contents(store)).toEqual(['B live'])
    expect(store.hasMoreMessages).toBe(hasMoreBefore)
  })
})

// ---- SI-T3：同会话多页 ----

describe('KB-56 SI-T3：同会话连续翻页', () => {
  it('翻两页都进同一会话，游标按响应的 cursor 推进', async () => {
    const store = useChatStore()
    const { calls } = installFetch({
      sess_A: (call) => {
        if (call.before === null) {
          return jsonResponse(
            page([pageMsg('a5', 'A p1a', 'id_a5'), pageMsg('a4', 'A p1b', 'id_a4')], {
              total: 9,
              cursor: 'id_a4',
            }),
          )
        }
        if (call.before === 'id_a4') {
          return jsonResponse(page([pageMsg('a3', 'A p2a', 'id_a3')], { total: 9, cursor: 'id_a3' }))
        }
        throw new Error(`意外的 before 游标: ${call.before}`)
      },
    })

    replay(store, 'sess_A', [replayMsg('a9', 'A live'), replayMsg('a8', 'A live2')])

    expect(await store.loadMoreMessages()).toBe(true)
    expect(store.earliestMessageId).toBe('id_a4')
    expect(await store.loadMoreMessages()).toBe(true)
    expect(store.earliestMessageId).toBe('id_a3')

    // 消息顺序：更早的在前面（页面按 _id 降序返回，逐页 prepend ⇒ 时间正序）
    expect(contents(store)).toEqual(['A p2a', 'A p1a', 'A p1b', 'A live', 'A live2'])
    // 全部请求都打会话域端点，且带上了正确的 before 游标
    expect(calls.map((c) => c.sessionId)).toEqual(['sess_A', 'sess_A'])
    expect(calls.map((c) => c.before)).toEqual([null, 'id_a4'])
    expect(calls.map((c) => c.limit)).toEqual(['50', '50'])
  })

  it('绝不调用全库端点 /api/messages', async () => {
    const store = useChatStore()
    const { calls } = installFetch({
      sess_A: page([pageMsg('a3', 'A p1', 'id_a3')]),
    })
    replay(store, 'sess_A', [replayMsg('a9', 'A live')])
    await store.loadMoreMessages()
    expect(calls.some((c) => c.sessionId === null)).toBe(false)
    expect(calls.every((c) => c.path === '/api/sessions/sess_A/messages')).toBe(true)
  })
})

// ---- SI-T4 / SI-T5：失败与正常路径 ----

describe('KB-56 SI-T4/T5：失败路径与正常路径', () => {
  it('失败请求不推进游标、不改 has_more、不插消息', async () => {
    const store = useChatStore()
    installFetch({
      sess_A: () => jsonResponse({ error: 'STORAGE_UNAVAILABLE' }, 503),
    })
    replay(store, 'sess_A', [replayMsg('a9', 'A live')])

    expect(await store.loadMoreMessages()).toBe(false)
    expect(contents(store)).toEqual(['A live'])
    expect(store.earliestMessageId).toBeNull()
    expect(store.hasMoreMessages).toBe(true)
  })

  it('会话身份未知时拒绝翻页（绝不退回全库端点）', async () => {
    const store = useChatStore()
    const { calls } = installFetch({})
    replay(store, '', [replayMsg('a9', 'A live')]) // 回放不带会话 id
    expect(store.activeSessionId).toBeNull()

    expect(await store.loadMoreMessages()).toBe(false)
    expect(calls).toHaveLength(0)
    expect(contents(store)).toEqual(['A live'])
  })

  it('当前会话的正常翻页照常工作（回归防线）', async () => {
    const store = useChatStore()
    const { calls } = installFetch({ sess_A: page([pageMsg('a1', 'A older', 'id_a1')]) })
    replay(store, 'sess_A', [replayMsg('a9', 'A live')])

    expect(await store.loadMoreMessages()).toBe(true)
    expect(contents(store)).toEqual(['A older', 'A live'])
    // 回放项不带 `_id` ⇒ 首页**不带** before（游标由响应接手）
    expect(calls[0].path).toBe('/api/sessions/sess_A/messages')
    expect(calls[0].limit).toBe('50')
    expect(calls[0].before).toBeNull()
  })
})

// ---- SI-T6 / SI-T7：既有行为不回归 ----

describe('KB-56 SI-T6/T7：去重、顺序与 live+history 合并', () => {
  it('与会话窗口重叠的 message_id 被去重（既有行为不回归）', async () => {
    const store = useChatStore()
    installFetch({
      sess_A: page([pageMsg('a9', 'A live', 'id_a9'), pageMsg('a1', 'A older', 'id_a1')]),
    })
    replay(store, 'sess_A', [replayMsg('a9', 'A live')])

    expect(await store.loadMoreMessages()).toBe(true)
    expect(contents(store)).toEqual(['A older', 'A live']) // a9 未重复
  })

  it('历史页在途时到达的新 live 消息不被覆盖，历史页插在其前', async () => {
    const store = useChatStore()
    const gate = deferred<Response>()
    installFetch({ sess_A: () => gate.promise })

    replay(store, 'sess_A', [replayMsg('a9', 'A live')])
    const loading = store.loadMoreMessages()
    // live 推送（同会话）在历史页之前到达
    store.appendDelta('newer live')

    gate.resolve(jsonResponse(page([pageMsg('a1', 'A older', 'id_a1')], { total: 9 })))
    await loading

    expect(contents(store)).toEqual(['A older', 'A live', 'newer live'])
  })

  it('同一会话的 A→B→A：世代闸门仍判定为陈旧响应', async () => {
    const store = useChatStore()
    const gateA = deferred<Response>()
    installFetch(
      {
        sess_A: () => gateA.promise,
        sess_B: page([pageMsg('b1', 'B live', 'id_b1')]),
      },
      page([pageMsg('z9', 'OTHER SESSION LEAK', 'id_z9')], { hasMore: true, total: 99 }),
    )

    replay(store, 'sess_A', [replayMsg('a9', 'A live')])
    const loading = store.loadMoreMessages()
    replay(store, 'sess_B', [replayMsg('b1', 'B live')])
    replay(store, 'sess_A', [replayMsg('a9', 'A live')]) // 切回 A

    gateA.resolve(jsonResponse(page([pageMsg('a1', 'A older', 'id_a1')], { total: 9 })))
    await loading

    // 该响应属于**上一个** A 世代（A→B→A 已使归属换代）：不得插进新 A 的列表。
    // 只用 `activeSessionId === 'sess_A'` 的判据在这里会误判为"同会话"⇒ 必须靠世代票。
    expect(contents(store)).toEqual(['A live'])
    expect(contents(store).join('|')).not.toContain('OTHER SESSION LEAK')
    expect(store.earliestMessageId).toBeNull()
  })
})
