/*
 * 四波催办口径（前端显示侧）。
 *
 * 权威实现在 backend/delivery_reminders.py，被 Agent 工具和钉钉推送共用；这里只做展示。
 * 改档位边界必须同时改后端、README 的口径章节和 tests/test_delivery_reminders.py。
 */

export type WaveKey = "w4" | "w3" | "w2" | "w1" | "far" | "none";
/** 表格里另有「已入库完」这一类，它不是提醒档位。 */
export type OrderWave = WaveKey | "done";

export interface Wave {
  k: WaveKey;
  seq: string;
  label: string;
  short: string;
  /** 触发日相对交期的偏移；-1 表示逾期波（交期次日起逐日追）。 */
  off: number | null;
  cssVar: string;
  note: string;
  rank: number;
}

export const WAVES: Wave[] = [
  { k: "w4", seq: "第 4 次", label: "逾期催办", short: "逾期", off: -1, cssVar: "--tier-overdue", note: "交期已过仍未入库完，逐日追", rank: 0 },
  { k: "w3", seq: "第 3 次", label: "T-1", short: "T-1", off: 1, cssVar: "--tier-d1", note: "交期前 1 天，核对物流单号", rank: 1 },
  { k: "w2", seq: "第 2 次", label: "T-10", short: "T-10", off: 10, cssVar: "--tier-d10", note: "交期前 10 天，确认发货计划", rank: 2 },
  { k: "w1", seq: "第 1 次", label: "T-20", short: "T-20", off: 20, cssVar: "--tier-d20", note: "交期前 20 天，确认排产进度", rank: 3 },
  { k: "far", seq: "", label: "暂不提醒", short: ">20天", off: null, cssVar: "--tier-far", note: "距交期 20 天以上，还没进提醒窗", rank: 4 },
  { k: "none", seq: "", label: "未排期", short: "未排期", off: null, cssVar: "--tier-none", note: "没有交期，先补日期才催得动", rank: 5 },
];

export const WAVE_BY_KEY = new Map<WaveKey, Wave>(WAVES.map((wave) => [wave.k, wave]));

/** 前端档位 → 后端催办 bucket，与 `delivery_reminders.WAVES` 对齐。 */
export const WAVE_TO_BUCKET: Partial<Record<WaveKey, "overdue" | "t1" | "t10" | "t20">> = {
  w4: "overdue",
  w3: "t1",
  w2: "t10",
  w1: "t20",
};

/** 需要发提醒的四波，最急优先。 */
export const URGENT: WaveKey[] = ["w4", "w3", "w2", "w1"];
const URGENT_SET = new Set<string>(URGENT);
export const isUrgent = (wave: string): boolean => URGENT_SET.has(wave);

/** 时间轴按第 1→4 次排；档位卡是最急优先，两者顺序不同是有意的。 */
export const TIMELINE: Wave[] = ["w1", "w2", "w3", "w4"].map((key) => WAVE_BY_KEY.get(key as WaveKey)!);

export function waveOfDays(days: number | null): WaveKey {
  if (days === null) return "none";
  if (days < 0) return "w4";
  if (days <= 1) return "w3";
  if (days <= 10) return "w2";
  if (days <= 20) return "w1";
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
  /** 该波的触发日（天序号）。 */
  day: number;
}

/**
 * 四波的触发日：第 n 波 = 交期 − off，逾期波是交期次日。
 * 没有交期就排不出来，返回 null —— 这时四波一个也发不出去。
 */
export function planWaves(etaDay: number | null): PlannedWave[] | null {
  if (etaDay === null) return null;
  return TIMELINE.map((wave) => ({ wave, day: etaDay + (wave.off === -1 ? 1 : -wave.off!) }));
}

/** 档位标签：有次序的写「第 n 次 · 标签」。 */
export function waveLabel(wave: OrderWave): string {
  if (wave === "done") return "已入库完";
  const found = WAVE_BY_KEY.get(wave);
  if (!found) return wave;
  return found.seq ? `${found.seq} · ${found.label}` : found.label;
}
