import { dayIso, pct } from "../../lib/format";
import { name } from "../../data/payload";
import type { PayloadDict } from "../../data/payload";
import type { LedgerOrder, WaveStamp } from "./model";
import { WAVE_BY_KEY, planWaves } from "./waves";

const HEAD = [
  "采购员", "提醒波次", "剩余天数", "交期", "交期来源", "下次提醒日", "采购单号", "采购日期",
  "供应商ID", "主要商品", "款数", "品类", "采购数量", "入库数量", "待入库", "入库率", "状态", "仓储方", "外部单号",
];

const quote = (value: unknown): string => {
  const text = value == null ? "" : String(value);
  return /[",\n]/.test(text) ? `"${text.replace(/"/g, '""')}"` : text;
};

/** 一行一个采购单：给到人、给到期限、给到下一次该什么时候催。 */
export function exportReminderCsv(
  orders: LedgerOrder[],
  stamps: Map<number, WaveStamp>,
  dict: PayloadDict,
  today: number,
  baselineDate: string,
): void {
  const lines = [HEAD.join(",")];
  for (const order of orders) {
    const stamp = stamps.get(order.index);
    if (!stamp) continue;
    const wave = stamp.done ? { seq: "", label: "已入库完" } : WAVE_BY_KEY.get(stamp.wave as never)!;
    const plan = planWaves(order.etaDay);
    const next = plan ? (plan.find((step) => today < step.day) ?? null) : null;
    lines.push(
      [
        name(dict.buyers, order.buyer),
        (wave.seq ? `${wave.seq} · ` : "") + wave.label,
        stamp.left === null ? "" : stamp.left,
        order.eta || "",
        order.etaSource || "",
        next ? dayIso(next.day) : stamp.done || !plan ? "" : "已到最后一波，逐日追",
        order.no,
        order.date,
        name(dict.suppliers, order.supplier),
        order.product,
        order.productCount,
        order.category,
        order.qty,
        order.inQty,
        order.pending,
        pct(order.inQty, order.qty),
        order.confirmed ? "已确认" : "待审核",
        name(dict.warehouses, order.warehouse),
        order.externalNo || "",
      ]
        .map(quote)
        .join(","),
    );
  }

  // 带 BOM，Excel 打开中文才不乱码。
  const blob = new Blob([`\uFEFF${lines.join("\r\n")}`], { type: "text/csv;charset=utf-8" });
  const link = document.createElement("a");
  link.href = URL.createObjectURL(blob);
  link.download = `催办清单_${baselineDate}.csv`;
  link.click();
  setTimeout(() => URL.revokeObjectURL(link.href), 1000);
}
