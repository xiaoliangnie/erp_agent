import { name } from "../../data/payload";
import type { DashboardLine, DashboardOrder, PayloadDict } from "../../data/payload";
import { TIER_BY_KEY } from "./tiers";
import { orderDue } from "./model";
import type { EtaDays } from "./model";

const HEAD = [
  "采购员", "提醒档位", "剩余天数", "到货期限", "采购单号", "状态",
  "供应商ID", "仓储方", "待入库件数", "待入库金额", "明细行数",
];

const quote = (value: unknown): string => {
  const text = String(value ?? "");
  return /[",\n]/.test(text) ? `"${text.replace(/"/g, '""')}"` : text;
};

/** 催办清单导出：一行一个采购单，给到人、给到期限。 */
export function exportAlerts(
  rows: DashboardLine[],
  orders: DashboardOrder[],
  dict: PayloadDict,
  etaDays: EtaDays,
  today: string,
): void {
  const grouped = new Map<number, { lines: DashboardLine[]; amount: number }>();
  for (const line of rows) {
    if (line.qty - line.inQty <= 0) continue;
    let entry = grouped.get(line.order);
    if (!entry) {
      entry = { lines: [], amount: 0 };
      grouped.set(line.order, entry);
    }
    entry.lines.push(line);
    // 待入库金额按单价折算，不能直接用整行金额。
    entry.amount += (line.qty - line.inQty) * line.price;
  }

  const list = [...grouped.entries()]
    .map(([orderIndex, entry]) => ({ orderIndex, ...entry, due: orderDue(entry.lines, etaDays) }))
    .sort((a, b) => a.due.sort - b.due.sort);

  const body = list.map((entry) => {
    const order = orders[entry.orderIndex];
    const tier = entry.due.tier ? TIER_BY_KEY.get(entry.due.tier)!.label : "";
    return [
      name(dict.buyers, order.buyer),
      tier,
      entry.due.days === null ? "未排期" : entry.due.days,
      entry.due.due || "",
      order.no,
      order.confirmed ? "已确认" : "待审核",
      name(dict.suppliers, order.supplier),
      name(dict.warehouses, order.warehouse),
      Math.round(entry.due.open),
      entry.amount.toFixed(2),
      entry.lines.length,
    ];
  });

  const csv = `\uFEFF${[HEAD, ...body].map((row) => row.map(quote).join(",")).join("\r\n")}`;
  const link = document.createElement("a");
  link.href = URL.createObjectURL(new Blob([csv], { type: "text/csv;charset=utf-8" }));
  link.download = `催办清单_${today}.csv`;
  link.click();
  setTimeout(() => URL.revokeObjectURL(link.href), 1000);
}
