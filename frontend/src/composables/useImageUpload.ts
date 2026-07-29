import { useChatStore } from '@/stores/chatStore'

/**
 * 共享图片上传 + 拖拽处理逻辑。
 * 用于 InputBar（textarea 拖拽上传）和 MessageList（聊天区拖拽上传）。
 *
 * @param dropTarget 可选 — 返回拖拽时需要切换 class 的 DOM 元素。
 *   传入时 onDragOver 会添加 'drag-over' class，onDragLeave/onDrop 会移除。
 */
export function useImageUpload(dropTarget?: () => HTMLElement | null) {
  const chat = useChatStore()

  async function uploadImage(file: File) {
    const form = new FormData()
    form.append('image', file)
    try {
      const resp = await fetch('/api/images', { method: 'POST', body: form })
      if (!resp.ok) {
        const err = await resp.json().catch(() => ({}))
        chat.addStatusMessage('图片上传失败: ' + (err.error || resp.status))
        return
      }
      const { url, mime } = await resp.json()
      chat.previewImages.push({ url, mime })
    } catch (e) {
      chat.addStatusMessage('图片上传失败: ' + (e as Error).message)
    }
  }

  function onDragOver(e: DragEvent) {
    e.preventDefault()
    dropTarget?.()?.classList.add('drag-over')
  }

  function onDragLeave(e: DragEvent) {
    e.preventDefault()
    dropTarget?.()?.classList.remove('drag-over')
  }

  function onDrop(e: DragEvent) {
    e.preventDefault()
    dropTarget?.()?.classList.remove('drag-over')
    const file = e.dataTransfer?.files[0]
    if (file && file.type.startsWith('image/')) uploadImage(file)
  }

  return { uploadImage, onDragOver, onDragLeave, onDrop }
}
