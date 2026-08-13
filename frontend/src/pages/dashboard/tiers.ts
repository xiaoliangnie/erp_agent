/*
 * 采购看板的到货预警档位。
 *
 * 剩余天数 = 最早预计到货日 − 今天；档位互斥，正好对上 T-20 / T-10 / T-1 / 逾期四波。
 * 注意这里走的是「预计到货日」，交期台账走的是与供应商约定的交期，两套口径不同是有意的。
 */

export type TierKey = "overdue" | "d1" | "d10" | "d20" | "far" | "none";

export interface Tier {
  k: TierKey;
  label: string;
  short: string;
  cssVar: string;
  note: string;
  rank: number;
}

export const TIERS: Tier[] = [
  { k: "overdue", label: "已逾期", short: "逾期", cssVar: "--tier-overdue", note: "预计到货日已过，超时未到", rank: 0 },
  { k: "d1", label: "剩 0–1 天", short: "0–1天", cssVar: "--tier-d1", note: "今明两天到期，T-1 提醒", rank: 1 },
  { k: "d10", label: "剩 2–10 天", short: "2–10天", cssVar: "--tier-d10", note: "十日内到期，T-10 提醒", rank: 2 },
  { k: "d20", label: "剩 11–20 天", short: "11–20天", cssVar: "--tier-d20", note: "二十日内到期，T-20 提醒", rank: 3 },
  { k: "far", label: "剩 20 天以上", short: ">20天", cssVar: "--tier-far", note: "尚未进入提醒窗口", rank: 4 },
  { k: "none", label: "未排期", short: "未排期", cssVar: "--tier-none", note: "没有填预计到货日期，催不出期限", rank: 5 },
];

export const TIER_BY_KEY = new Map<TierKey, Tier>(TIERS.map((tier) => [tier.k, tier]));

/** 需要发提醒的四档。 */
const URGENT_SET = new Set<string>(["overdue", "d1", "d10", "d20"]);
export const isUrgent = (tier: string | null): boolean => tier != null && URGENT_SET.has(tier);

export function tierOfDays(days: number | null): TierKey {
  if (days === null) return "none";
  if (days < 0) return "overdue";
  if (days <= 1) return "d1";
  if (days <= 10) return "d10";
  if (days <= 20) return "d20";
  return "far";
}

export function daysText(days: number | null): string {
  if (days === null) return "未排期";
  if (days < 0) return `逾期 ${-days} 天`;
  if (days === 0) return "今天到期";
  return `剩 ${days} 天`;
}

/** 到货期限筛选项：空 = 不限；open = 全部待入库；其余是单个档位。 */
export const ETA_OPTIONS: { k: string; label: string }[] = [
  { k: "", label: "不限" },
  { k: "open", label: "全部待入库" },
  ...TIERS.map((tier) => ({ k: tier.k as string, label: tier.label })),
];
