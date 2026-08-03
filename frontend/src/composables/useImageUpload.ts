import { useChatStore } from '@/stores/chatStore'
import type { ImageUploadError, ImageUploadResponse } from '@/types/protocol'

/**
 * 共享图片上传 + 拖拽处理逻辑。
 * 用于 InputBar（textarea 拖拽上传）和 MessageList（聊天区拖拽上传）。
 *
 * @param dropTarget 可选 — 返回拖拽时需要切换 class 的 DOM 元素。
 *   传入时 onDragEnter 会添加 'drag-over' class，onDragLeave/onDrop 会移除。
 */
export function useImageUpload(dropTarget?: () => HTMLElement | null) {
  const chat = useChatStore()

  // dragCounter 避免子元素间的 dragenter/dragleave 闪烁
  let dragCounter = 0

  async function uploadImage(file: File) {
    const form = new FormData()
    form.append('image', file)
    try {
      const resp = await fetch('/api/images', { method: 'POST', body: form })
      if (!resp.ok) {
        const err = await resp.json().catch(() => ({ error: '' })) as ImageUploadError
        chat.addStatusMessage('图片上传失败: ' + (err.error || resp.status))
        return
      }
      const data: ImageUploadResponse = await resp.json()
      chat.previewImages.push({ url: data.url, mime: data.mime })
    } catch (e) {
      chat.addStatusMessage('图片上传失败: ' + (e as Error).message)
    }
  }

  function onDragOver(e: DragEvent) {
    e.preventDefault()
  }

  function onDragEnter(e: DragEvent) {
    e.preventDefault()
    dragCounter++
    dropTarget?.()?.classList.add('drag-over')
  }

  function onDragLeave(e: DragEvent) {
    e.preventDefault()
    dragCounter--
    if (dragCounter <= 0) {
      dragCounter = 0
      dropTarget?.()?.classList.remove('drag-over')
    }
  }

  function onDrop(e: DragEvent) {
    e.preventDefault()
    dragCounter = 0
    dropTarget?.()?.classList.remove('drag-over')
    const file = e.dataTransfer?.files[0]
    if (file && file.type.startsWith('image/')) uploadImage(file)
  }

  return { uploadImage, onDragOver, onDragEnter, onDragLeave, onDrop }
}
