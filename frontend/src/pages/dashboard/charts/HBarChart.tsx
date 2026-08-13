import { cssVar, money } from "../../../lib/format";
import { barPathH } from "./geometry";
import { useHoverable } from "./Tooltip";
import type { TipRow } from "./Tooltip";

export interface HBarItem {
  name: string;
  value: number;
  tip: TipRow[];
}

interface HBarChartProps {
  items: HBarItem[];
  width: number;
  labelWidth?: number;
  format?: (value: number) => string;
}

const BAND = 30;
const VALUE_W = 62;

/** 横向条形：单一色相，标签在轴上，数值在条端。 */
export function HBarChart({ items, width, labelWidth = 96, format = money }: HBarChartProps) {
  const hoverable = useHoverable();
  if (!items.length) return <div className="empty">无数据</div>;

  const labelW = Math.min(labelWidth, Math.round(width * 0.38));
  const barH = Math.min(18, BAND - 12);
  const height = items.length * BAND + 6;
  const inner = Math.max(10, width - labelW - VALUE_W - 8);
  const max = Math.max(...items.map((item) => item.value)) || 1;

  return (
    <svg viewBox={`0 0 ${width} ${height}`} height={height}>
      {items.map((item, index) => {
        const y = index * BAND + 4;
        const barWidth = Math.max(2, (inner * item.value) / max);
        return (
          <g key={item.name}>
            <text
              x={labelW - 10}
              y={y + barH / 2}
              fill={cssVar("--text-secondary")}
              fontSize={11.5}
              textAnchor="end"
              dominantBaseline="middle"
            >
              {item.name}
            </text>
            <g {...hoverable(item.name, item.tip)}>
              <path d={barPathH(labelW, y, barWidth, barH, 4)} fill={cssVar("--series-1")} />
              {/* 透明命中区：条太短时也要点得到 */}
              <rect x={labelW} y={y - 6} width={Math.max(barWidth, 24)} height={barH + 12} fill="transparent" />
            </g>
            <text
              x={labelW + barWidth + 8}
              y={y + barH / 2}
              fill={cssVar("--text-primary")}
              fontSize={11}
              fontWeight={600}
              dominantBaseline="middle"
              className="num"
            >
              {format(item.value)}
            </text>
          </g>
        );
      })}
    </svg>
  );
}
