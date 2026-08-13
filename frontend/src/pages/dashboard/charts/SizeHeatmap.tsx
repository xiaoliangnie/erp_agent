import { cssVar, ellipsis, int } from "../../../lib/format";
import { useHoverable } from "./Tooltip";
import type { SizeRow } from "../model";

interface SizeHeatmapProps {
  matrix: { cols: string[]; rows: SizeRow[] };
  mode: number;
  width: number;
}

const CELL_H = 24;
const GAP = 2;
const HEAD_H = 18;
const TOTAL_W = 62;
/** 每 4.5 个百分点跨一级色阶，最深一级是 ≥32%。 */
const LEVEL_STEP = 0.045;

/** 尺码曲线热力图：行内归一的顺序色阶。 */
export function SizeHeatmap({ matrix, mode, width }: SizeHeatmapProps) {
  const hoverable = useHoverable();
  if (!matrix.rows.length) {
    return <div className="empty">{mode === 1 ? "当前筛选下没有号型商品" : "当前筛选下没有鞋码商品"}</div>;
  }

  const labelW = Math.min(206, Math.round(width * 0.32));
  const gridW = Math.max(40, width - labelW - TOTAL_W);
  const cellW = gridW / matrix.cols.length;
  const height = HEAD_H + matrix.rows.length * (CELL_H + GAP) + 4;
  const flipAt = Number.parseInt(cssVar("--seq-ink-flip"), 10);

  return (
    <svg viewBox={`0 0 ${width} ${height}`} height={height}>
      {matrix.cols.map((col, index) => (
        <text
          key={col}
          x={labelW + cellW * (index + 0.5)}
          y={HEAD_H - 10}
          fill={cssVar("--text-muted")}
          fontSize={10}
          textAnchor="middle"
          dominantBaseline="middle"
          className="num"
        >
          {col}
        </text>
      ))}
      <text x={width} y={HEAD_H - 10} fill={cssVar("--text-muted")} fontSize={10} textAnchor="end" dominantBaseline="middle">
        合计
      </text>

      {matrix.rows.map((row, rowIndex) => {
        const y = HEAD_H + rowIndex * (CELL_H + GAP);
        return (
          <g key={row.name}>
            <text
              x={labelW - 10}
              y={y + CELL_H / 2}
              fill={cssVar("--text-secondary")}
              fontSize={11}
              textAnchor="end"
              dominantBaseline="middle"
            >
              {ellipsis(row.name, Math.floor((labelW - 14) / 11))}
              <title>{row.name}</title>
            </text>
            {matrix.cols.map((col, colIndex) => {
              const value = row.cells.get(col) ?? 0;
              const share = row.total > 0 ? value / row.total : 0;
              const level = value === 0 ? -1 : Math.min(7, Math.floor(share / LEVEL_STEP));
              const fill = level < 0 ? cssVar("--wash") : cssVar(`--seq-${level}`);
              const cell = (
                <>
                  <rect
                    x={labelW + cellW * colIndex + GAP / 2}
                    y={y}
                    width={Math.max(1, cellW - GAP)}
                    height={CELL_H}
                    rx={3}
                    fill={fill}
                  />
                  {/* 只有量得下才写数字，写不下交给悬浮和表视图，绝不裁字 */}
                  {value > 0 && cellW >= 25 ? (
                    <text
                      x={labelW + cellW * (colIndex + 0.5)}
                      y={y + CELL_H / 2}
                      fill={level >= flipAt ? "#fff" : cssVar("--text-primary")}
                      fontSize={cellW < 32 ? 9 : 10}
                      textAnchor="middle"
                      dominantBaseline="middle"
                      className="num"
                    >
                      {`${Math.round(share * 100)}%`}
                    </text>
                  ) : null}
                </>
              );
              return value > 0 ? (
                <g
                  key={col}
                  {...hoverable(row.name, [
                    { name: `${mode === 1 ? "号型 " : "鞋码 "}${col}`, value: `${int(value)} 件`, color: fill },
                    { name: "占该商品", value: `${(share * 100).toFixed(1)}%` },
                  ])}
                >
                  {cell}
                </g>
              ) : (
                <g key={col}>{cell}</g>
              );
            })}
            <text
              x={width}
              y={y + CELL_H / 2}
              fill={cssVar("--text-primary")}
              fontSize={11}
              fontWeight={600}
              textAnchor="end"
              dominantBaseline="middle"
              className="num"
            >
              {int(row.total)}
            </text>
          </g>
        );
      })}
    </svg>
  );
}
