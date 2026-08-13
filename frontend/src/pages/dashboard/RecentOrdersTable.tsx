import { Link } from "react-router-dom";
import { name } from "../../data/payload";
import type { DashboardOrder, PayloadDict } from "../../data/payload";
import { int, money } from "../../lib/format";
import { ROUTES } from "../../routes";
import type { OrderRow } from "./model";

interface RecentOrdersTableProps {
  rows: OrderRow[];
  orders: DashboardOrder[];
  dict: PayloadDict;
  onOpen: (orderIndex: number) => void;
}

function createdTime(order: DashboardOrder) {
  return (order.createdAt || order.date).replace("T", " ").slice(0, 16);
}

export function RecentOrdersTable({ rows, orders, dict, onOpen }: RecentOrdersTableProps) {
  return (
    <table className="orders-table recent-orders-table">
      <thead>
        <tr>
          <th>建立时间</th>
          <th>采购单号</th>
          <th>状态</th>
          <th>采购员</th>
          <th>供应商</th>
          <th className="n">商品行</th>
          <th className="n">采购数量</th>
          <th className="n">采购金额</th>
          <th>快捷操作</th>
        </tr>
      </thead>
      <tbody>
        {rows.map((row) => {
          const order = orders[row.orderIndex];
          return (
            <tr
              key={order.no}
              tabIndex={0}
              onClick={() => onOpen(row.orderIndex)}
              onKeyDown={(event) => {
                if (event.key === "Enter") onOpen(row.orderIndex);
              }}
            >
              <td className="created-at">{createdTime(order)}</td>
              <td className="no">{order.no}</td>
              <td>
                <span className="chip">{order.confirmed ? "已确认" : "待审核"}</span>
              </td>
              <td>{name(dict.buyers, order.buyer)}</td>
              <td>{name(dict.suppliers, order.supplier)}</td>
              <td className="n">{int(row.lineCount)}</td>
              <td className="n">{int(row.qty)}</td>
              <td className="n">{money(row.amount)}</td>
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
