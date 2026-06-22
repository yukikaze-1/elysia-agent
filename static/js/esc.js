/** HTML 转义工具——统一实现，chat.js/settings.js/debug.html 共用。 */
export function esc(s) {
  const d = document.createElement('div');
  d.textContent = s ?? '';
  return d.innerHTML;
}
