/** SVG 排版算术。图表都是手写 SVG，没有图表库。 */

/** 一端圆角、基线端平角的条形 —— 横向。 */
export function barPathH(x: number, y: number, w: number, h: number, r: number): string {
  const radius = Math.max(0, Math.min(r, w, h / 2));
  return (
    `M${x},${y} H${x + w - radius} A${radius},${radius} 0 0 1 ${x + w},${y + radius} ` +
    `V${y + h - radius} A${radius},${radius} 0 0 1 ${x + w - radius},${y + h} H${x} Z`
  );
}

/** 一端圆角、基线端平角的条形 —— 纵向（自下而上）。 */
export function barPathV(x: number, yTop: number, w: number, h: number, r: number): string {
  const radius = Math.max(0, Math.min(r, w / 2, h));
  return (
    `M${x},${yTop + h} V${yTop + radius} A${radius},${radius} 0 0 1 ${x + radius},${yTop} ` +
    `H${x + w - radius} A${radius},${radius} 0 0 1 ${x + w},${yTop + radius} V${yTop + h} Z`
  );
}

/** 好看的刻度：1 / 2 / 2.5 / 5 / 10 的整数倍。 */
export function niceTicks(max: number, count = 4): number[] {
  if (max <= 0) return [0];
  const raw = max / count;
  const magnitude = 10 ** Math.floor(Math.log10(raw));
  const step = [1, 2, 2.5, 5, 10].map((factor) => factor * magnitude).find((value) => value >= raw) ?? 10 * magnitude;
  const ticks: number[] = [];
  for (let value = 0; value <= max * 1.0001; value += step) ticks.push(value);
  if (ticks[ticks.length - 1] < max) ticks.push(ticks[ticks.length - 1] + step);
  return ticks;
}

/** 该日期所在周的周一。 */
export function isoMonday(iso: string): string {
  const date = new Date(`${iso}T00:00:00`);
  const weekday = (date.getDay() + 6) % 7;
  date.setDate(date.getDate() - weekday);
  return date.toISOString().slice(0, 10);
}

export function shiftDays(iso: string, days: number): string {
  const date = new Date(`${iso}T00:00:00`);
  date.setDate(date.getDate() + days);
  return date.toISOString().slice(0, 10);
}

export const monthDay = (iso: string): string => iso.slice(5).replace("-", "/");
