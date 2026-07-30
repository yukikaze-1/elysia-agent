import { onUnmounted } from 'vue'

/**
 * 管理 fetch AbortController 生命周期，防止竞态条件和组件卸载后请求回调。
 *
 * 用法：
 *   const { signal } = useAbortController()
 *   // signal() 返回新 signal，自动 abort 上一次请求
 *   const res = await fetch(url, { signal: signal() })
 *
 * - 每次调用 signal() 会 abort 上一个未完成的请求（防竞态）
 * - 组件卸载时自动 abort 当前请求（防卸载后更新）
 */
export function useAbortController() {
  let controller: AbortController | null = null

  /** 返回一个 AbortSignal，同时 abort 上一次由本实例发起的请求。 */
  function signal(): AbortSignal {
    controller?.abort()
    controller = new AbortController()
    return controller.signal
  }

  onUnmounted(() => {
    controller?.abort()
    controller = null
  })

  return { signal }
}
