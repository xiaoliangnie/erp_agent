import { Link } from "react-router-dom";
import { cssVar, int, money, pct } from "../../lib/format";
import { name } from "../../data/payload";
import type { DashboardOrder, PayloadDict } from "../../data/payload";
import { ROUTES } from "../../routes";
import { TIER_BY_KEY, daysText, isUrgent } from "./tiers";
import type { OrderRow, OrderSortKey } from "./model";

const COLUMNS: { key: OrderSortKey | "contract"; label: string; n?: boolean; sortable?: boolean }[] = [
  { key: "no", label: "采购单号" },
  { key: "date", label: "采购日期" },
  { key: "due", label: "到货期限" },
  { key: "left", label: "剩余" },
  { key: "st", label: "状态" },
  { key: "buyer", label: "采购员" },
  { key: "cat", label: "主品类" },
  { key: "n", label: "行数", n: true },
  { key: "qty", label: "采购数量", n: true },
  { key: "inq", label: "已入库", n: true },
  { key: "open", label: "待入库", n: true },
  { key: "rate", label: "入库率", n: true },
  { key: "amt", label: "采购金额", n: true },
  { key: "wh", label: "仓储方" },
  { key: "contract", label: "合同", sortable: false },
];

interface OrdersTableProps {
  rows: OrderRow[];
  orders: DashboardOrder[];
  dict: PayloadDict;
  sortKey: OrderSortKey;
  sortDir: number;
  onSort: (key: OrderSortKey) => void;
  onOpen: (orderIndex: number) => void;
}

export function OrdersTable({ rows, orders, dict, sortKey, sortDir, onSort, onOpen }: OrdersTableProps) {
  return (
    <table className="orders-table">
      <thead>
        <tr>
          {COLUMNS.map((column) => {
            const sortable = column.sortable !== false;
            return (
              <th
                key={column.key}
                className={column.n ? "n" : undefined}
                data-sort={sortable ? column.key : undefined}
                tabIndex={sortable ? 0 : undefined}
                onClick={sortable ? () => onSort(column.key as OrderSortKey) : undefined}
                onKeyDown={
                  sortable
                    ? (event) => {
                        if (event.key === "Enter" || event.key === " ") {
                          event.preventDefault();
                          onSort(column.key as OrderSortKey);
                        }
                      }
                    : undefined
                }
              >
                {column.label}
                {sortKey === column.key ? <span className="arrow">{sortDir < 0 ? "↓" : "↑"}</span> : null}
              </th>
            );
          })}
        </tr>
      </thead>
      <tbody>
        {rows.map((row) => {
          const order = orders[row.orderIndex];
          const due = row.due;
          const urgent = due.open > 0 && isUrgent(due.tier);
          return (
            <tr
              key={order.no}
              tabIndex={0}
              onClick={() => onOpen(row.orderIndex)}
              onKeyDown={(event) => {
                if (event.key === "Enter") onOpen(row.orderIndex);
              }}
            >
              <td className="no">{order.no}</td>
              <td>{order.date}</td>
              <td style={{ color: due.open > 0 && !due.due ? cssVar("--text-muted") : undefined }}>
                {due.open > 0 ? due.due || "未排期" : "—"}
              </td>
              <td
                style={{
                  color: urgent ? cssVar(TIER_BY_KEY.get(due.tier!)!.cssVar) : undefined,
                  fontWeight: urgent ? 600 : undefined,
                }}
              >
                {due.open > 0 ? daysText(due.days) + (due.due && due.noDate ? " ＋未排期" : "") : "已入库"}
              </td>
              <td>
                <span
                  className="chip"
                  style={
                    order.confirmed
                      ? { color: cssVar("--text-secondary") }
                      : { color: cssVar("--text-primary"), borderColor: cssVar("--warning") }
                  }
                >
                  {order.confirmed ? "已确认" : "待审核"}
                </span>
              </td>
              <td>{name(dict.buyers, order.buyer)}</td>
              <td>{name(dict.cats, row.cats[0]) + (row.cats.length > 1 ? ` +${row.cats.length - 1}` : "")}</td>
              <td className="n">{int(row.lineCount)}</td>
              <td className="n">{int(row.qty)}</td>
              <td className="n">{int(row.inQty)}</td>
              <td className="n">{int(due.open)}</td>
              <td className="n">{pct(row.inQty, row.qty)}</td>
              <td className="n">{money(row.amount)}</td>
              <td>{name(dict.warehouses, order.warehouse)}</td>
              <td>
                <Link
                  className="contract-action"
                  to={`${ROUTES.contract}?po_id=${encodeURIComponent(order.no)}`}
                  onClick={(event) => event.stopPropagation()}
                >
                  生成合同
                </Link>
              </td>
            </tr>
          );
        })}
      </tbody>
    </table>
  );
}
