import { cssVar, int } from "../../lib/format";
import type { LedgerOrder, WaveStamp } from "./model";
import type { OrderWave } from "./waves";
import { WAVES, isUrgent } from "./waves";

interface TierCardsProps {
  orders: LedgerOrder[];
  stamps: Map<number, WaveStamp>;
  selected: string;
  showDone: boolean;
  onSelect: (wave: string) => void;
}

interface Bucket {
  count: number;
  qty: number;
  buyers: Set<number>;
  first: string;
}

const DONE_CARD = {
  k: "done" as const,
  seq: "",
  label: "已入库完",
  cssVar: "--tier-done",
  note: "没有待入库量，不进提醒",
};

/**
 * 档位卡自己不受档位筛选约束 —— 否则选中一档，其他档全归零，就没法在档位之间跳。
 * 传进来的 orders 必须是「除档位以外的筛选都已生效」的那份。
 */
export function TierCards({ orders, stamps, selected, showDone, onSelect }: TierCardsProps) {
  const buckets = new Map<string, Bucket>();
  const keys: string[] = [...WAVES.map((wave) => wave.k), "done"];
  for (const key of keys) buckets.set(key, { count: 0, qty: 0, buyers: new Set(), first: "" });

  for (const order of orders) {
    const stamp = stamps.get(order.index);
    if (!stamp) continue;
    const bucket = buckets.get(stamp.wave);
    if (!bucket) continue;
    bucket.count += 1;
    bucket.qty += order.pending;
    bucket.buyers.add(order.buyer);
    if (order.eta && (!bucket.first || order.eta < bucket.first)) bucket.first = order.eta;
  }

  const cards: { k: OrderWave; seq: string; label: string; cssVar: string; note: string }[] = [
    ...WAVES.map((wave) => ({ k: wave.k as OrderWave, seq: wave.seq, label: wave.label, cssVar: wave.cssVar, note: wave.note })),
  ];
  if (showDone) cards.push(DONE_CARD);

  return (
    <div className="tiers">
      {cards.map((card) => {
        const bucket = buckets.get(card.k)!;
        return (
          <button
            key={card.k}
            type="button"
            className="tier"
            aria-pressed={selected === card.k}
            style={{ borderLeftColor: cssVar(card.cssVar) }}
            onClick={() => onSelect(card.k)}
          >
            <div className="t-k">
              {card.seq ? <span className="seq">{card.seq}</span> : null}
              <span>{card.label}</span>
            </div>
            <div className="t-v">
              {int(bucket.count)}
              <small>单</small>
            </div>
            <div className="t-s">
              {int(bucket.qty)} 件待入库 · {bucket.buyers.size} 个采购员
            </div>
            <div className="t-n">
              {bucket.first && isUrgent(card.k) ? `${card.note}｜最早交期 ${bucket.first}` : card.note}
            </div>
          </button>
        );
      })}
    </div>
  );
}
