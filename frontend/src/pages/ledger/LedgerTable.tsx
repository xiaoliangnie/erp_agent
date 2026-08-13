import { cssVar, int, money, pct } from "../../lib/format";
import { name } from "../../data/payload";
import type { PayloadDict } from "../../data/payload";
import type { LedgerOrder, WaveStamp } from "./model";
import type { SortKey } from "./sorting";
import { WaveDots } from "./WaveDots";
import { daysText, isUrgent, waveLabel } from "./waves";
import { WAVE_BY_KEY } from "./waves";

const COLUMNS: { key: SortKey | null; label: string; numeric?: boolean }[] = [
  { key: "no", label: "单号" },
  { key: "date", label: "采购日期" },
  { key: "supplier", label: "供应商" },
  { key: null, label: "产品信息" },
  { key: "eta", label: "交期" },
  { key: "left", label: "剩余", numeric: true },
  { key: "qty", label: "采购数量", numeric: true },
  { key: "in", label: "入库数量", numeric: true },
  { key: "pending", label: "待入库", numeric: true },
  { key: "buyer", label: "采购员" },
  { key: "wave", label: "交期提醒" },
];

interface LedgerTableProps {
  rows: LedgerOrder[];
  stamps: Map<number, WaveStamp>;
  dict: PayloadDict;
  today: number;
  sortKey: SortKey;
  sortDir: number;
  onSort: (key: SortKey) => void;
  onOpen: (order: LedgerOrder) => void;
}

function waveColor(wave: string): string {
  if (wave === "done") return cssVar("--tier-done");
  const found = WAVE_BY_KEY.get(wave as never);
  return found ? cssVar(found.cssVar) : cssVar("--text-muted");
}

export function LedgerTable({ rows, stamps, dict, today, sortKey, sortDir, onSort, onOpen }: LedgerTableProps) {
  return (
    <table>
      <thead>
        <tr>
          {COLUMNS.map((column) => (
            <th
              key={column.label}
              className={column.numeric ? "n" : undefined}
              data-sort={column.key ?? undefined}
              onClick={column.key ? () => onSort(column.key!) : undefined}
              aria-sort={column.key === sortKey ? (sortDir > 0 ? "ascending" : "descending") : undefined}
            >
              {column.label}
              {column.key === sortKey ? <span className="arrow">{sortDir > 0 ? "↑" : "↓"}</span> : null}
            </th>
          ))}
        </tr>
      </thead>
      <tbody>
        {rows.map((order) => {
          const stamp = stamps.get(order.index)!;
          const color = waveColor(stamp.wave);
          const supplierName = name(dict.suppliers, order.supplier);
          const tags: string[] = [];
          if (order.eta && order.etaSource !== "交期") tags.push("预计到货");
          if (order.eta && order.etaSpread > 1) tags.push(`${order.etaSpread} 个交期取最早`);

          return (
            <tr key={order.no} onClick={() => onOpen(order)}>
              <td className="no">{order.no}</td>
              <td>{order.date || "—"}</td>
              <td title={`供应商 ID ${supplierName}（数据里没有供应商名称）`}>{supplierName}</td>
              <td>
                <div className="prod">
                  <span
                    className="pn"
                    title={order.product + (order.productCount > 1 ? `（共 ${order.productCount} 款商品）` : "")}
                  >
                    {order.product}
                    {order.productCount > 1 ? <span className="more">+{order.productCount - 1} 款</span> : null}
                  </span>
                  <span className="ps">
                    {order.category} · {order.lines.length} 行 · {int(order.qty)} 件 · {money(order.amount)} 元
                  </span>
                </div>
              </td>
              <td className="eta-cell">
                <span className="d">{order.eta || "—"}</span>
                {tags.length ? <div className="alt">{tags.join(" · ")}</div> : null}
              </td>
              <td className="n">
                <span
                  className="left"
                  style={{ color: isUrgent(stamp.wave) ? color : stamp.done ? cssVar("--text-muted") : undefined }}
                >
                  {daysText(stamp.left)}
                </span>
              </td>
              <td className="n">{int(order.qty)}</td>
              <td className="n">
                <div className="recv">
                  <span>{int(order.inQty)}</span>
                  <div className="meter" title={`入库率 ${pct(order.inQty, order.qty)}`}>
                    <i
                      style={{
                        width: `${(order.qty > 0 ? Math.min(100, (order.inQty / order.qty) * 100) : 0).toFixed(1)}%`,
                        background: stamp.done ? cssVar("--tier-done") : undefined,
                      }}
                    />
                  </div>
                </div>
              </td>
              <td className="n">{int(order.pending)}</td>
              <td>{name(dict.buyers, order.buyer)}</td>
              <td>
                <div className="wave-cell">
                  <span className="chip" style={{ color, borderColor: color }}>
                    {waveLabel(stamp.wave)}
                  </span>
                  <WaveDots etaDay={order.etaDay} today={today} current={stamp.wave} />
                </div>
              </td>
            </tr>
          );
        })}
      </tbody>
    </table>
  );
}
