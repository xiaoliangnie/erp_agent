import { cssVar, int, money, pct } from "../../lib/format";
import { useElementWidth } from "../../hooks/useElementWidth";
import type { PeriodPoint, Totals } from "./model";

interface RailProps {
  totals: Totals;
  range: [string, string];
  /** 迷你走势用的月度序列，取最后 12 个月。 */
  series: PeriodPoint[];
}

const SPARK_H = 34;

/** 台账栏：一列对齐的账目，而不是一排 KPI 卡。 */
export function Rail({ totals, range, series }: RailProps) {
  const { ref, width } = useElementWidth<HTMLDivElement>(230);
  const [head, unit] = money(totals.amount).split(" ");
  const rate = totals.qty > 0 ? totals.inQty / totals.qty : 0;
  const points = series.slice(-12);

  return (
    <aside className="rail" aria-label="总览">
      <div className="hero-label">
        <p className="eyebrow">采购金额 · 元</p>
        <span
          className="flag"
          style={{ color: totals.pending ? cssVar("--text-primary") : cssVar("--delta-up") }}
        >
          <i className="dot" style={{ background: totals.pending ? cssVar("--warning") : cssVar("--good") }} />
          {totals.pending ? `${totals.pending} 单待审核` : "全部已确认"}
        </span>
      </div>
      <div className="hero">
        {head}
        <span className="unit">{unit ? `${unit}元` : "元"}</span>
      </div>
      <div className="hero-sub">
        {range[0]} 至 {range[1]}
      </div>

      <div className="spark" ref={ref}>
        {points.length > 1 ? <Spark points={points} width={width} /> : null}
      </div>

      <div className="ledger">
        <LedgerRow label="采购单" value={int(totals.orders)} unit="单" />
        <LedgerRow label="明细行" value={int(totals.lines)} unit="行" />
        <LedgerRow label="采购数量" value={int(totals.qty)} unit="件" />
        <div className="ledger-row stack">
          <div className="head">
            <span className="k">已入库</span>
            <span className="v">
              {int(totals.inQty)}
              <small>件</small>
            </span>
          </div>
          <div className="meter" role="img" aria-label="入库进度">
            <i style={{ width: `${(rate * 100).toFixed(1)}%` }} />
          </div>
          <div className="hero-sub" style={{ marginTop: 6 }}>
            入库率 {pct(totals.inQty, totals.qty)} · 待入库 {int(totals.open)} 件
          </div>
        </div>
        <LedgerRow label="待入库" value={int(totals.open)} unit="件" />
        <LedgerRow label="待审核" value={int(totals.pending)} unit="单" />
        <LedgerRow label="供应商" value={int(totals.suppliers)} unit="家" />
      </div>
    </aside>
  );
}

function LedgerRow({ label, value, unit }: { label: string; value: string; unit: string }) {
  return (
    <div className="ledger-row">
      <span className="k">{label}</span>
      <span className="v">
        {value}
        <small>{unit}</small>
      </span>
    </div>
  );
}

/** 迷你走势：最后一段用强调色，其余压灰。 */
function Spark({ points, width }: { points: PeriodPoint[]; width: number }) {
  const max = Math.max(...points.map((point) => point.amount)) || 1;
  const X = (index: number) => (index * (width - 6)) / (points.length - 1) + 3;
  const Y = (value: number) => SPARK_H - 4 - (value / max) * (SPARK_H - 10);
  const path = points.map((point, index) => `${index ? "L" : "M"}${X(index)},${Y(point.amount)}`).join(" ");
  const last = points.length - 1;

  return (
    <>
      <svg viewBox={`0 0 ${width} ${SPARK_H}`} height={SPARK_H} aria-hidden="true">
        <path d={path} fill="none" stroke={cssVar("--deemph")} strokeWidth={2} strokeLinejoin="round" strokeLinecap="round" />
        <path
          d={`M${X(last - 1)},${Y(points[last - 1].amount)} L${X(last)},${Y(points[last].amount)}`}
          fill="none"
          stroke={cssVar("--series-1")}
          strokeWidth={2}
          strokeLinecap="round"
        />
        <circle
          cx={X(last)}
          cy={Y(points[last].amount)}
          r={4}
          fill={cssVar("--series-1")}
          stroke={cssVar("--surface-1")}
          strokeWidth={2}
        />
      </svg>
      <div className="hero-sub">
        近 {points.length} 个月，末点为 {points[last].key}
      </div>
    </>
  );
}
