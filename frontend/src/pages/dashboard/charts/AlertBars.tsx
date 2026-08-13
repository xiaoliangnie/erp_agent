import { cssVar, ellipsis, int, pct } from "../../../lib/format";
import { TIERS } from "../tiers";
import { useHoverable } from "./Tooltip";
import type { BuyerBucket } from "../model";

interface AlertBarsProps {
  buyers: BuyerBucket[];
  width: number;
}

const BAND = 30;
const BAR_H = 16;
const VALUE_W = 96;
const TOP = 8;

/** 催办清单：每个采购员一条按提醒档堆叠的待入库条，右端直接标注需催量。 */
export function AlertBars({ buyers, width }: AlertBarsProps) {
  const hoverable = useHoverable();
  if (!buyers.length) return <div className="empty">当前切片没有待入库数量</div>;

  const list = buyers.slice(0, TOP);
  const labelW = Math.min(112, Math.round(width * 0.2));
  const height = list.length * BAND + 6;
  const inner = Math.max(10, width - labelW - VALUE_W - 10);
  const max = Math.max(...list.map((buyer) => buyer.qty)) || 1;

  return (
    <svg viewBox={`0 0 ${width} ${height}`} height={height}>
      {list.map((buyer, index) => {
        const y = index * BAND + 4;
        const total = Math.max(2, (inner * buyer.qty) / max);
        let cursor = labelW;
        return (
          <g key={buyer.k}>
            <text
              x={labelW - 10}
              y={y + BAR_H / 2}
              fill={cssVar("--text-secondary")}
              fontSize={11.5}
              textAnchor="end"
              dominantBaseline="middle"
            >
              {ellipsis(buyer.name, Math.floor((labelW - 14) / 12))}
              <title>{buyer.name}</title>
            </text>
            <clipPath id={`alert-clip-${buyer.k}`}>
              <rect x={labelW} y={y} width={total} height={BAR_H} rx={4} />
            </clipPath>
            <g clipPath={`url(#alert-clip-${buyer.k})`}>
              {TIERS.map((tier) => {
                const cell = buyer.cells.get(tier.k);
                if (!cell || cell.qty <= 0) return null;
                const segWidth = (total * cell.qty) / buyer.qty;
                const x = cursor;
                cursor += segWidth;
                return (
                  <g
                    key={tier.k}
                    {...hoverable(`${buyer.name} · ${tier.label}`, [
                      { name: "待入库", value: `${int(cell.qty)} 件`, color: cssVar(tier.cssVar) },
                      { name: "涉及采购单", value: `${cell.orders} 单` },
                      { name: "占该采购员", value: pct(cell.qty, buyer.qty) },
                    ])}
                  >
                    <rect x={x} y={y} width={Math.max(1, segWidth)} height={BAR_H} fill={cssVar(tier.cssVar)} />
                  </g>
                );
              })}
            </g>
            <text
              x={width - 2}
              y={y + BAR_H / 2}
              fill={buyer.urgent > 0 ? cssVar("--critical") : cssVar("--text-muted")}
              fontSize={11}
              fontWeight={600}
              textAnchor="end"
              dominantBaseline="middle"
              className="num"
            >
              {buyer.urgent > 0 ? `需催 ${int(buyer.urgent)}` : "无需催"}
            </text>
          </g>
        );
      })}
    </svg>
  );
}
