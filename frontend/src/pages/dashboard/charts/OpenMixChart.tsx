import { cssVar, int, pct } from "../../../lib/format";
import { useHoverable } from "./Tooltip";
import type { MixPart } from "../model";

interface OpenMixChartProps {
  parts: MixPart[];
  width: number;
}

const BAR_H = 26;
const GAP = 2;

/** 待入库构成：一条整体堆叠条 + 逐档直接标注。 */
export function OpenMixChart({ parts, width }: OpenMixChartProps) {
  const hoverable = useHoverable();
  const total = parts.reduce((sum, part) => sum + part.value, 0);
  if (total <= 0) return <div className="empty">当前切片已全部入库</div>;

  let cursor = 0;
  const segments = parts
    .filter((part) => part.value > 0)
    .map((part) => {
      const segWidth = (width * part.value) / total;
      const x = cursor;
      cursor += segWidth;
      return { part, x, width: segWidth };
    });

  return (
    <>
      <svg viewBox={`0 0 ${width} ${BAR_H}`} height={BAR_H}>
        <clipPath id="openmix-clip">
          <rect x={0} y={0} width={width} height={BAR_H} rx={4} />
        </clipPath>
        <g clipPath="url(#openmix-clip)">
          {segments.map((segment) => (
            <g
              key={segment.part.name}
              {...hoverable(segment.part.name, [
                { name: "待入库", value: `${int(segment.part.value)} 件`, color: segment.part.color },
                { name: "占待入库", value: pct(segment.part.value, total) },
              ])}
            >
              <rect
                x={segment.x}
                y={0}
                width={Math.max(1, segment.width - GAP)}
                height={BAR_H}
                fill={segment.part.color}
              />
            </g>
          ))}
        </g>
      </svg>

      <div style={{ marginTop: 12 }}>
        {parts.map((part) => (
          <div key={part.name} className="ledger-row" style={{ padding: "9px 0" }}>
            <span className="k">
              <i
                style={{
                  width: 9,
                  height: 9,
                  borderRadius: 3,
                  display: "inline-block",
                  marginRight: 7,
                  background: part.color,
                }}
              />
              {part.name + (part.risk && part.value > 0 ? " ⚠" : "")}
            </span>
            <span className="v" style={{ color: part.risk && part.value > 0 ? cssVar("--critical") : undefined }}>
              {int(part.value)}
              <small>件 · {pct(part.value, total)}</small>
            </span>
          </div>
        ))}
        <div className="hero-sub" style={{ marginTop: 8 }}>
          合计待入库 {int(total)} 件
        </div>
      </div>
    </>
  );
}
