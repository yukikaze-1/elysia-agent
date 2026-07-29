/**
 * 格式化数字：>= 1000 显示为 1.0k 形式。
 * 用于 Token 计数等场景。
 */
export function fmt(n: number): string {
  if (n >= 1000) return (n / 1000).toFixed(1) + 'k'
  return String(n)
}
