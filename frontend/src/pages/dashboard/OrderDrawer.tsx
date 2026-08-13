import { useEffect } from "react";
import { cssVar, int, money, pct } from "../../lib/format";
import { name } from "../../data/payload";
import type { DashboardLine, DashboardOrder, PayloadDict } from "../../data/payload";
import { DataTable } from "./ChartCard";
import { TIER_BY_KEY, daysText, isUrgent } from "./tiers";
import { orderDue } from "./model";
import type { EtaDays } from "./model";

interface OrderDrawerProps {
  order: DashboardOrder | null;
  /** 只给当前切片里属于这张单的明细行 —— 抽屉要和筛选一致。 */
  lines: DashboardLine[];
  dict: PayloadDict;
  etaDays: EtaDays;
  onClose: () => void;
}

const LINE_COLUMNS = [
  { label: "商品" },
  { label: "颜色 / 规格" },
  { label: "商品编码" },
  { label: "数量", n: true },
  { label: "已入库", n: true },
  { label: "待入库", n: true },
  { label: "到货期限" },
  { label: "剩余" },
  { label: "单价", n: true },
  { label: "金额", n: true },
];

export function OrderDrawer({ order, lines, dict, etaDays, onClose }: OrderDrawerProps) {
  useEffect(() => {
    if (!order) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [order, onClose]);

  const open = Boolean(order);
  const amount = lines.reduce((sum, line) => sum + line.amount, 0);
  const qty = lines.reduce((sum, line) => sum + line.qty, 0);
  const inQty = lines.reduce((sum, line) => sum + line.inQty, 0);
  const due = orderDue(lines, etaDays);
  const urgent = due.open > 0 && isUrgent(due.tier);

  const meta: [string, string][] = order
    ? [
        ["状态", order.confirmed ? "已确认" : "待审核"],
        ["采购日期", order.date],
        ["审核日期", order.auditDate || "—"],
        ["到货期限", due.open > 0 ? due.due || "未排期" : "—"],
        ["剩余", due.open > 0 ? daysText(due.days) : "已入库"],
        ["采购员", name(dict.buyers, order.buyer)],
        ["供应商 ID", name(dict.suppliers, order.supplier)],
        ["仓储方", name(dict.warehouses, order.warehouse)],
        ["收货", name(dict.addrs, order.address)],
        ["付款方式", name(dict.pays, order.payment)],
        ["外部单号", order.externalNo || "—"],
        ["采购金额", `${money(amount)} 元`],
        ["采购 / 入库", `${int(qty)} / ${int(inQty)} 件`],
        ["待入库", `${int(due.open)} 件`],
        ["入库率", pct(inQty, qty)],
      ]
    : [];

  return (
    <>
      <div className={`scrim${open ? " open" : ""}`} onClick={onClose} />
      <aside
        className={`drawer${open ? " open" : ""}`}
        role="dialog"
        aria-modal="true"
        aria-label="采购单明细"
        aria-hidden={!open}
      >
        {order ? (
          <>
            <div className="drawer-head">
              <div className="row">
                <p className="eyebrow" style={{ alignSelf: "center" }}>
                  采购单
                </p>
                <h3>{order.no}</h3>
                <button type="button" className="close" onClick={onClose}>
                  关闭
                </button>
              </div>
              <div className="meta-grid">
                {meta.map(([key, value], index) => (
                  <div key={key}>
                    <div className="k">{key}</div>
                    <div
                      className="v"
                      style={
                        index === 4 && urgent
                          ? { color: cssVar(TIER_BY_KEY.get(due.tier!)!.cssVar), fontWeight: 600 }
                          : undefined
                      }
                    >
                      {value}
                    </div>
                  </div>
                ))}
              </div>
            </div>
            <div className="drawer-body">
              <h4>商品明细</h4>
              <div style={{ overflowX: "auto" }}>
                <DataTable
                  columns={LINE_COLUMNS}
                  rows={lines.map((line) => {
                    const pending = line.qty - line.inQty;
                    return [
                      name(dict.spus, line.spu),
                      name(dict.colors, line.color) + (line.spec ? ` / ${line.spec}` : ""),
                      line.sku,
                      int(line.qty),
                      int(line.inQty),
                      int(pending),
                      pending > 0 ? line.eta || "未排期" : line.eta || "—",
                      pending > 0 ? daysText(etaDays(line.eta)) : "已入库",
                      line.price.toFixed(2),
                      int(line.amount),
                    ];
                  })}
                />
              </div>
            </div>
          </>
        ) : null}
      </aside>
    </>
  );
}
