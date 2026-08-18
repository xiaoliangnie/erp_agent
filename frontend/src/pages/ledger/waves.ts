/*
 * 跟单三档催办口径（前端显示侧）。
 *
 * 权威实现在 backend/delivery_reminders.py 的 profile=followup，
 * 被交期台账页、Agent 工具和钉钉推送共用。这里只做展示。
 * 改档位边界必须同时改后端、README 的口径章节和 tests/test_delivery_reminders.py。
 */

export type WaveKey = "overdue" | "d3" | "d10" | "far" | "none";
/** 表格里另有「已入库完」这一类，它不是提醒档位。 */
export type OrderWave = WaveKey | "done";

export interface Wave {
  k: WaveKey;
  seq: string;
  label: string;
  short: string;
  /** 触发日相对交期的偏移；-1 表示逾期档（交期次日起逐日追）。 */
  off: number | null;
  cssVar: string;
  note: string;
  rank: number;
}

export const WAVES: Wave[] = [
  { k: "overdue", seq: "第 3 档", label: "已逾期", short: "逾期", off: -1, cssVar: "--tier-overdue", note: "交期已过仍未入库完，逐日追", rank: 0 },
  { k: "d3", seq: "第 2 档", label: "剩 ≤3 天", short: "≤3天", off: 3, cssVar: "--tier-d3", note: "交期前 3 天内，确认发货", rank: 1 },
  { k: "d10", seq: "第 1 档", label: "剩 ≤10 天", short: "≤10天", off: 10, cssVar: "--tier-d10", note: "交期前 10 天内，确认排产/发货计划", rank: 2 },
  { k: "far", seq: "", label: "暂不提醒", short: ">10天", off: null, cssVar: "--tier-far", note: "距交期 10 天以上，还没进提醒窗", rank: 3 },
  { k: "none", seq: "", label: "未排期", short: "未排期", off: null, cssVar: "--tier-none", note: "没有交期，先补日期才催得动", rank: 4 },
];

export const WAVE_BY_KEY = new Map<WaveKey, Wave>(WAVES.map((wave) => [wave.k, wave]));

/** 前端档位 → 后端催办 bucket，与 `delivery_reminders.FOLLOWUP_WAVES` 对齐。 */
export const WAVE_TO_BUCKET: Partial<Record<WaveKey, "overdue" | "d3" | "d10">> = {
  overdue: "overdue",
  d3: "d3",
  d10: "d10",
};

/** 需要发提醒的三档，最急优先。 */
export const URGENT: WaveKey[] = ["overdue", "d3", "d10"];
const URGENT_SET = new Set<string>(URGENT);
export const isUrgent = (wave: string): boolean => URGENT_SET.has(wave);

/** 时间轴按第 1→3 档排；档位卡是最急优先，两者顺序不同是有意的。 */
export const TIMELINE: Wave[] = ["d10", "d3", "overdue"].map((key) => WAVE_BY_KEY.get(key as WaveKey)!);

export function waveOfDays(days: number | null): WaveKey {
  if (days === null) return "none";
  if (days < 0) return "overdue";
  if (days <= 3) return "d3";
  if (days <= 10) return "d10";
  return "far";
}

export function daysText(days: number | null): string {
  if (days === null) return "未排期";
  if (days < 0) return `逾期 ${-days} 天`;
  if (days === 0) return "今天到期";
  if (days === 1) return "明天到期";
  return `剩 ${days} 天`;
}

export const waveRank = (wave: string): number => WAVE_BY_KEY.get(wave as WaveKey)?.rank ?? 9;

export interface PlannedWave {
  wave: Wave;
  /** 该档的触发日（天序号）。 */
  day: number;
}

/**
 * 三档的触发日：第 n 档 = 交期 − off，逾期档是交期次日。
 * 没有交期就排不出来，返回 null —— 这时一档也发不出去。
 */
export function planWaves(etaDay: number | null): PlannedWave[] | null {
  if (etaDay === null) return null;
  return TIMELINE.map((wave) => ({ wave, day: etaDay + (wave.off === -1 ? 1 : -wave.off!) }));
}

/** 档位标签：有次序的写「第 n 档 · 标签」。 */
export function waveLabel(wave: OrderWave): string {
  if (wave === "done") return "已入库完";
  const found = WAVE_BY_KEY.get(wave);
  if (!found) return wave;
  return found.seq ? `${found.seq} · ${found.label}` : found.label;
}
