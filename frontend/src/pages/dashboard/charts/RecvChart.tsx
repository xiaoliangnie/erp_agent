import { cssVar, int, pct } from "../../../lib/format";
import { barPathH } from "./geometry";
import { useHoverable } from "./Tooltip";
import type { DimPoint } from "../model";

interface RecvChartProps {
  items: DimPoint[];
  width: number;
}

const BAND = 40;
const BAR_H = 14;
const RATE_W = 46;

/** 入库仪表：同色系深浅两段，直接标注完成率。 */
export function RecvChart({ items, width }: RecvChartProps) {
  const hoverable = useHoverable();
  if (!items.length) return <div className="empty">无数据</div>;

  const labelW = Math.min(88, Math.round(width * 0.34));
  const height = items.length * BAND + 4;
  const inner = Math.max(10, width - labelW - RATE_W - 10);
  const max = Math.max(...items.map((item) => item.qty)) || 1;

  return (
    <svg viewBox={`0 0 ${width} ${height}`} height={height}>
      {items.map((item, index) => {
        const y = index * BAND + 6;
        const total = Math.max(2, (inner * item.qty) / max);
        const done = item.qty > 0 ? (total * item.inQty) / item.qty : 0;
        return (
          <g key={item.k}>
            <text
              x={labelW - 10}
              y={y + BAR_H / 2}
              fill={cssVar("--text-secondary")}
              fontSize={11.5}
              textAnchor="end"
              dominantBaseline="middle"
            >
              {item.name}
            </text>
            <g
              {...hoverable(item.name, [
                { name: "已入库", value: `${int(item.inQty)} 件`, color: cssVar("--series-1") },
                { name: "待入库", value: `${int(item.qty - item.inQty)} 件`, color: cssVar("--track") },
                { name: "入库率", value: pct(item.inQty, item.qty) },
              ])}
            >
              <path d={barPathH(labelW, y, total, BAR_H, 4)} fill={cssVar("--track")} />
              {/* 2px 表面色留白把两段分开，而不是描边 */}
              {done > 1 ? (
                <path d={barPathH(labelW, y, Math.max(1, done - 2), BAR_H, 4)} fill={cssVar("--series-1")} />
              ) : null}
              <rect x={labelW} y={y - 8} width={Math.max(total, 24)} height={BAR_H + 16} fill="transparent" />
            </g>
            <text
              x={width - 2}
              y={y + BAR_H / 2}
              fill={cssVar("--text-primary")}
              fontSize={11}
              fontWeight={600}
              textAnchor="end"
              dominantBaseline="middle"
              className="num"
            >
              {pct(item.inQty, item.qty)}
            </text>
            <text
              x={labelW}
              y={y + BAR_H + 11}
              fill={cssVar("--text-muted")}
              fontSize={10}
              dominantBaseline="middle"
              className="num"
            >
              {`采购 ${int(item.qty)} 件 · 已入库 ${int(item.inQty)}`}
            </text>
          </g>
        );
      })}
    </svg>
  );
}
