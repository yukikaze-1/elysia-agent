import { ref } from 'vue'

/** 全局 lightbox 状态 — 任意组件调用 open(url) 即可全屏查看图片 */
const lightboxImage = ref<string | null>(null)

export function useLightbox() {
  function open(url: string) {
    lightboxImage.value = url
  }
  function close() {
    lightboxImage.value = null
  }
  return { lightboxImage, open, close }
}
