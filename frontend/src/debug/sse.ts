/**
 * SSE over fetch（S2-F3，2026-09-12）。
 *
 * 为什么不用 `EventSource`：它在协议层面**无法设置请求头**，而管理面凭据必须走
 * `Authorization`（见 `auth.ts` 与 `docs/electron-backend-security.md` §1.3——
 * 凭据绝不进 URL 查询参数）。用 `fetch` + `ReadableStream` 手工消费 SSE 即可带着
 * 头，代价是自动重连要自己写（`TasksTab.vue` 保留原有的指数退避）。
 */

import { debugFetch } from './auth'

/** 解析一个 SSE 事件块（块之间以空行分隔）。无 data 行的块（如纯注释）返回 null。 */
export function parseSseBlock(raw: string): { event: string; data: string } | null {
  let event = 'message'
  const data: string[] = []
  for (const line of raw.split('\n')) {
    if (!line || line.startsWith(':')) continue       // `: keepalive` 等注释行
    const colon = line.indexOf(':')
    const field = colon === -1 ? line : line.slice(0, colon)
    let value = colon === -1 ? '' : line.slice(colon + 1)
    if (value.startsWith(' ')) value = value.slice(1) // 规范：冒号后单个空格是分隔符
    if (field === 'event') event = value
    else if (field === 'data') data.push(value)
  }
  return data.length ? { event, data: data.join('\n') } : null
}

/**
 * 消费一个 SSE 流直到结束/出错；正常结束与异常都以抛错收场，调用方据此重连。
 *
 * @param onOpen 响应头到达且状态码 OK 时回调一次（调用方据此置「已连接」）。
 */
export async function readSseStream(
  url: string,
  onEvent: (event: string, data: string) => void,
  signal: AbortSignal,
  onOpen?: () => void,
): Promise<void> {
  const res = await debugFetch(url, {
    signal,
    headers: { Accept: 'text/event-stream' },
  })
  if (!res.ok) throw new Error(`HTTP ${res.status}`)
  if (!res.body) throw new Error('响应无 body，无法读取 SSE 流')
  onOpen?.()

  const reader = res.body.pipeThrough(new TextDecoderStream()).getReader()
  let buf = ''
  try {
    for (;;) {
      const { value, done } = await reader.read()
      if (done) break
      buf = (buf + value).replace(/\r\n/g, '\n')
      let idx: number
      while ((idx = buf.indexOf('\n\n')) >= 0) {
        const block = buf.slice(0, idx)
        buf = buf.slice(idx + 2)
        const evt = parseSseBlock(block)
        if (evt) onEvent(evt.event, evt.data)
      }
    }
  } finally {
    reader.cancel().catch(() => { /* 流已关闭 */ })
  }
  throw new Error('SSE 流已结束')
}
