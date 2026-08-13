import { useRef, useState } from "react";
import { cssVar, int, money } from "../../../lib/format";
import { monthDay, niceTicks } from "./geometry";
import { useTooltip } from "./Tooltip";
import type { PeriodPoint } from "../model";

interface TrendChartProps {
  data: PeriodPoint[];
  granularity: "day" | "week" | "month";
  width: number;
}

const HEIGHT = 258;
const MARGIN = { t: 18, r: 16, b: 30, l: 58 };
const GRAN_LABEL = { month: "月", week: "周", day: "日" };

/** 折线 + 面积 + 十字准星。准星命中最近的 x，读者瞄的是日期而不是 2px 的线。 */
export function TrendChart({ data, granularity, width }: TrendChartProps) {
  const { show, hide } = useTooltip();
  const svgRef = useRef<SVGSVGElement>(null);
  const [crossX, setCrossX] = useState<number | null>(null);

  if (!data.length) return <div className="empty">当前筛选下没有采购记录</div>;

  const inner = Math.max(10, width - MARGIN.l - MARGIN.r);
  const innerHeight = HEIGHT - MARGIN.t - MARGIN.b;
  const ticks = niceTicks(Math.max(...data.map((point) => point.amount)), 4);
  const top = ticks[ticks.length - 1] || 1;
  const X = (index: number) => MARGIN.l + (data.length === 1 ? inner / 2 : (inner * index) / (data.length - 1));
  const Y = (value: number) => MARGIN.t + innerHeight - (value / top) * innerHeight;

  let line = "";
  let area = `M${X(0)},${MARGIN.t + innerHeight} `;
  data.forEach((point, index) => {
    line += `${index ? "L" : "M"}${X(index)},${Y(point.amount)} `;
    area += `L${X(index)},${Y(point.amount)} `;
  });
  area += `L${X(data.length - 1)},${MARGIN.t + innerHeight} Z`;

  // x 轴刻度最多 8 个。
  const step = Math.max(1, Math.ceil(data.length / 8));

  // 直接标注峰值与末点，其余交给准星和表视图。
  let peak = 0;
  data.forEach((point, index) => {
    if (point.amount > data[peak].amount) peak = index;
  });
  const marks = [...new Set([peak, data.length - 1])];

  function locate(event: React.PointerEvent<SVGRectElement>) {
    const box = svgRef.current?.getBoundingClientRect();
    if (!box) return;
    const px = (event.clientX - box.left) * (width / box.width);
    const spacing = inner / Math.max(1, data.length - 1);
    const index = Math.max(0, Math.min(data.length - 1, Math.round((px - MARGIN.l) / spacing)));
    const point = data[index];
    setCrossX(X(index));
    show(event, `${granularity === "week" ? `${point.key} 起` : point.key} · ${GRAN_LABEL[granularity]}`, [
      { name: "采购金额", value: `${money(point.amount)} 元`, color: cssVar("--series-1") },
      { name: "采购数量", value: `${int(point.qty)} 件` },
      { name: "采购单", value: `${point.orders} 单` },
    ]);
  }

  return (
    <svg ref={svgRef} viewBox={`0 0 ${width} ${HEIGHT}`} height={HEIGHT} role="img" aria-label="采购金额走势">
      {ticks.map((tick) => (
        <g key={tick}>
          <line x1={MARGIN.l} x2={MARGIN.l + inner} y1={Y(tick)} y2={Y(tick)} stroke={cssVar("--grid")} strokeWidth={1} />
          <text
            x={MARGIN.l - 9}
            y={Y(tick)}
            fill={cssVar("--text-muted")}
            fontSize={10}
            textAnchor="end"
            dominantBaseline="middle"
            className="num"
          >
            {money(tick)}
          </text>
        </g>
      ))}

      <path d={area} fill={cssVar("--series-1")} fillOpacity={0.1} />
      <path
        d={line}
        fill="none"
        stroke={cssVar("--series-1")}
        strokeWidth={2}
        strokeLinejoin="round"
        strokeLinecap="round"
      />

      {data.map((point, index) =>
        index % step && index !== data.length - 1 ? null : (
          <text
            key={point.key}
            x={X(index)}
            y={MARGIN.t + innerHeight + 15}
            fill={cssVar("--text-muted")}
            fontSize={10}
            textAnchor="middle"
            dominantBaseline="middle"
            className="num"
          >
            {granularity === "month" ? point.key.slice(2) : monthDay(point.key)}
          </text>
        ),
      )}

      <line
        x1={MARGIN.l}
        x2={MARGIN.l + inner}
        y1={MARGIN.t + innerHeight}
        y2={MARGIN.t + innerHeight}
        stroke={cssVar("--axis")}
        strokeWidth={1}
      />

      {marks.map((index) => {
        const point = data[index];
        // 折线从左上落下来时标签走下方，免得压在线上。
        const falling = index > 0 && data[index - 1].amount > point.amount;
        const dy = falling && Y(point.amount) + 20 < MARGIN.t + innerHeight ? 16 : -13;
        return (
          <g key={`mark-${point.key}`}>
            <circle
              cx={X(index)}
              cy={Y(point.amount)}
              r={4.5}
              fill={cssVar("--series-1")}
              stroke={cssVar("--surface-1")}
              strokeWidth={2}
            />
            <text
              x={X(index)}
              y={Y(point.amount) + dy}
              fill={cssVar("--text-primary")}
              fontSize={11}
              fontWeight={650}
              textAnchor={X(index) > MARGIN.l + inner - 54 ? "end" : "middle"}
              dominantBaseline="middle"
              className="num"
            >
              {money(point.amount)}
            </text>
          </g>
        );
      })}

      {crossX !== null ? (
        <line x1={crossX} x2={crossX} y1={MARGIN.t} y2={MARGIN.t + innerHeight} stroke={cssVar("--axis")} strokeWidth={1} />
      ) : null}
      <rect
        x={MARGIN.l}
        y={MARGIN.t}
        width={inner}
        height={innerHeight}
        fill="transparent"
        onPointerEnter={locate}
        onPointerMove={locate}
        onPointerLeave={() => {
          setCrossX(null);
          hide();
        }}
      />
    </svg>
  );
}
