import { cssVar, int } from "../../lib/format";
import { name } from "../../data/payload";
import type { PayloadDict } from "../../data/payload";
import type { LedgerOrder, WaveStamp } from "./model";
import { WAVES, isUrgent } from "./waves";

interface BuyerBarsProps {
  orders: LedgerOrder[];
  stamps: Map<number, WaveStamp>;
  dict: PayloadDict;
}

interface Row {
  buyer: number;
  qty: number;
  /** 需催量 = 跟单三档合计（10 天内到期 + 已逾期）。 */
  need: number;
  byWave: Record<string, number>;
}

const TOP = 12;

export function BuyerBars({ orders, stamps, dict }: BuyerBarsProps) {
  const grouped = new Map<number, Row>();
  for (const order of orders) {
    const stamp = stamps.get(order.index);
    if (!stamp || stamp.done) continue;
    let row = grouped.get(order.buyer);
    if (!row) {
      row = { buyer: order.buyer, qty: 0, need: 0, byWave: {} };
      grouped.set(order.buyer, row);
    }
    row.qty += order.pending;
    row.byWave[stamp.wave] = (row.byWave[stamp.wave] ?? 0) + order.pending;
    if (isUrgent(stamp.wave)) row.need += order.pending;
  }

  const rows = [...grouped.values()].sort((a, b) => b.need - a.need || b.qty - a.qty).slice(0, TOP);

  return (
    <>
      <div className="buyer-rows">
        {rows.length === 0 ? (
          <div className="empty">当前筛选下没有待入库的单。</div>
        ) : (
          rows.map((row) => {
            const max = Math.max(...rows.map((item) => item.qty));
            const buyerName = name(dict.buyers, row.buyer);
            return (
              <div key={row.buyer} className="buyer-row">
                <div className="bn" title={buyerName}>
                  {buyerName}
                </div>
                <div className="bar" style={{ width: `${((row.qty / max) * 100).toFixed(2)}%` }}>
                  {WAVES.map((wave) => {
                    const qty = row.byWave[wave.k] ?? 0;
                    if (!qty) return null;
                    return (
                      <i
                        key={wave.k}
                        style={{ width: `${((qty / row.qty) * 100).toFixed(3)}%`, background: cssVar(wave.cssVar) }}
                        title={`${buyerName} · ${wave.label}：${int(qty)} 件`}
                      />
                    );
                  })}
                </div>
                <div
                  className="bv"
                  title={`待入库合计 ${int(row.qty)} 件，其中 10 天内到期或已逾期 ${int(row.need)} 件`}
                >
                  {int(row.need)}
                  <small>需催</small>
                </div>
              </div>
            );
          })
        )}
      </div>
      <div className="legend">
        {WAVES.map((wave) => (
          <span key={wave.k} className="item">
            <span className="swatch" style={{ background: cssVar(wave.cssVar) }} />
            <span>{(wave.seq ? `${wave.seq} · ` : "") + wave.label}</span>
          </span>
        ))}
      </div>
    </>
  );
}
