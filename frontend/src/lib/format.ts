/** 数字与日期口径的显示格式，看板和台账共用一份。 */

export const int = (value: number): string => Math.round(value).toLocaleString("zh-CN");

/** 紧凑金额：亿 / 万 / 元。 */
export function money(value: number): string {
  const magnitude = Math.abs(value);
  if (magnitude >= 1e8) return `${(value / 1e8).toFixed(2)} 亿`;
  if (magnitude >= 1e4) return `${(value / 1e4).toFixed(magnitude >= 1e6 ? 0 : 1)} 万`;
  return int(value);
}

export const pct = (part: number, whole: number): string =>
  whole > 0 ? `${((part / whole) * 100).toFixed(1)}%` : "—";

/** 读 CSS 变量取色，保证图表配色只有令牌一个来源。 */
export const cssVar = (name: string): string =>
  getComputedStyle(document.documentElement).getPropertyValue(name).trim();

const DAY_MS = 86_400_000;

/** ISO 日期 → 天序号。用 UTC 避免本地时区把日期推前后一天。 */
export const dayNumber = (iso: string): number =>
  Date.UTC(+iso.slice(0, 4), +iso.slice(5, 7) - 1, +iso.slice(8, 10)) / DAY_MS;

export const dayIso = (value: number): string => new Date(value * DAY_MS).toISOString().slice(0, 10);

export function ellipsis(text: string, max: number): string {
  return text.length > max ? `${text.slice(0, max - 1)}…` : text;
}
