import { useState } from "react";
import type { ReactNode } from "react";
import { useElementWidth } from "../../hooks/useElementWidth";

export interface Column {
  label: string;
  n?: boolean;
}

interface DataTableProps {
  columns: Column[];
  rows: (string | number)[][];
}

/** 每张图的等价读法：图看形状，表给准确数字。 */
export function DataTable({ columns, rows }: DataTableProps) {
  return (
    <table>
      <thead>
        <tr>
          {columns.map((column, index) => (
            <th key={`${column.label}-${index}`} className={column.n ? "n" : undefined}>
              {column.label}
            </th>
          ))}
        </tr>
      </thead>
      <tbody>
        {rows.map((row, rowIndex) => (
          <tr key={rowIndex}>
            {row.map((cell, cellIndex) => (
              <td key={cellIndex} className={columns[cellIndex]?.n ? "n" : undefined}>
                {cell}
              </td>
            ))}
          </tr>
        ))}
      </tbody>
    </table>
  );
}

interface ChartCardProps {
  eyebrow: string;
  title: string;
  note?: ReactNode;
  span?: 2 | 3 | 4 | 6;
  /** 卡头右侧的额外控件，例如粒度切换。 */
  controls?: ReactNode;
  /** 图下方的图例，切到表视图时一起隐藏。 */
  legend?: ReactNode;
  /** 拿到容器宽度再画图 —— SVG 按实际像素排版。 */
  chart: (width: number) => ReactNode;
  table: { columns: Column[]; rows: (string | number)[][] };
  /** 图表上方的内容，例如档位卡，两个视图都显示。 */
  children?: ReactNode;
}

export function ChartCard({ eyebrow, title, note, span, controls, legend, chart, table, children }: ChartCardProps) {
  const [view, setView] = useState<"chart" | "table">("chart");
  const { ref, width } = useElementWidth<HTMLDivElement>();

  return (
    <section className={`card${span ? ` span-${span}` : ""}`}>
      <div className="card-head">
        <div>
          <p className="eyebrow">{eyebrow}</p>
          <h2>{title}</h2>
          {note ? <div className="note">{note}</div> : null}
        </div>
        <div className="ctrl">
          {controls}
          <div className="mini-seg" role="group" aria-label="显示方式">
            <button type="button" aria-pressed={view === "chart"} onClick={() => setView("chart")}>
              图
            </button>
            <button type="button" aria-pressed={view === "table"} onClick={() => setView("table")}>
              表
            </button>
          </div>
        </div>
      </div>
      {children}
      <div className="plot" ref={ref} hidden={view !== "chart"}>
        {view === "chart" ? chart(width) : null}
      </div>
      {view === "chart" && legend ? legend : null}
      {view === "table" ? (
        <div className="tbl">
          <DataTable columns={table.columns} rows={table.rows} />
        </div>
      ) : null}
    </section>
  );
}
