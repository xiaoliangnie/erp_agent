import { useEffect, useState } from "react";
import { dayIso, dayNumber, int } from "../../lib/format";
import { ChannelCompare, ShopSources, styleChannelWindows } from "./ChannelMix";
import type { SpuAnalysis, SpuStyle } from "./types";
import { monthlySalesBaihuo, sparkValues, turnoverText, wowText } from "./types";

interface StyleDrawerProps {
  style: SpuStyle;
  computedAt: string;
  analysis: SpuAnalysis;
  onAnalyze: (styleId: string, force?: boolean) => void;
  onClose: () => void;
  board?: "apparel" | "baihuo";
}

/** 逐日出库的日期轴：结果表按「计算日的昨天」往回铺，最后一个点是昨天。 */
function dailyDates(computedAt: string, count: number): string[] {
  const base = computedAt.slice(0, 10);
  if (!/^\d{4}-\d{2}-\d{2}$/.test(base)) return Array(count).fill("");
  const anchor = dayNumber(base);
  return Array.from({ length: count }, (_, index) => dayIso(anchor - (count - index)));
}

/** 放大版趋势图：网格 + 悬停十字线，鼠标落点显示日期与出库量。 */
function TrendChart({ values, dates }: { values: number[]; dates: string[] }) {
  const width = 540;
  const height = 170;
  const pad = { left: 34, right: 10, top: 12, bottom: 22 };
  const [hover, setHover] = useState<number | null>(null);
  const innerW = width - pad.left - pad.right;
  const innerH = height - pad.top - pad.bottom;
  const max = Math.max(...values, 1);
  const step = innerW / Math.max(1, values.length - 1);
  const x = (index: number) => pad.left + index * step;
  const y = (value: number) => pad.top + innerH - (value / max) * innerH;
  const points = values.map((value, index) => `${x(index).toFixed(1)},${y(value).toFixed(1)}`).join(" ");
  const gridValues = [0, 0.5, 1].map((ratio) => Math.round(max * ratio));
  const first = dates[0] ?? "";
  const last = dates[dates.length - 1] ?? "";

  const onMove = (event: React.MouseEvent<SVGSVGElement>) => {
    const rect = event.currentTarget.getBoundingClientRect();
    const viewX = ((event.clientX - rect.left) / rect.width) * width;
    const index = Math.round((viewX - pad.left) / step);
    setHover(index >= 0 && index < values.length ? index : null);
  };

  const tip = hover === null ? null : (() => {
    const boxW = 108;
    const px = x(hover);
    // 靠右侧时气泡翻到左边，避免出画
    const boxX = px + boxW + 8 > width - pad.right ? px - boxW - 8 : px + 8;
    return { px, boxX, boxY: Math.max(2, y(values[hover]) - 34) };
  })();

  return (
    <svg
      className="spu-trend"
      viewBox={`0 0 ${width} ${height}`}
      role="img"
      aria-label="逐日出库"
      onMouseMove={onMove}
      onMouseLeave={() => setHover(null)}
    >
      {gridValues.map((value) => (
        <g key={value}>
          <line x1={pad.left} x2={width - pad.right} y1={y(value)} y2={y(value)} className="grid" />
          <text x={pad.left - 5} y={y(value) + 3} textAnchor="end" className="axis">{int(value)}</text>
        </g>
      ))}
      <polyline points={points} fill="none" strokeWidth="1.6" className="line" />
      {values.map((value, index) => (
        <circle
          key={index}
          cx={x(index)}
          cy={y(value)}
          r={hover === index ? 4 : 2.4}
          className={hover === index ? "dot is-hover" : "dot"}
        />
      ))}
      {hover !== null && tip ? (
        <g className="spu-tip">
          <line x1={tip.px} x2={tip.px} y1={pad.top} y2={pad.top + innerH} className="cross" />
          <rect x={tip.boxX} y={tip.boxY} width={108} height={30} rx={5} className="tip-box" />
          <text x={tip.boxX + 8} y={tip.boxY + 12} className="tip-date">{dates[hover]}</text>
          <text x={tip.boxX + 8} y={tip.boxY + 25} className="tip-value">出库 {int(values[hover])} 件</text>
        </g>
      ) : null}
      <text x={pad.left} y={height - 6} className="axis">{first.slice(5)}</text>
      <text x={width - pad.right} y={height - 6} textAnchor="end" className="axis">{last.slice(5)}</text>
    </svg>
  );
}

export function StyleDrawer({
  style, computedAt, analysis, onAnalyze, onClose, board = "apparel",
}: StyleDrawerProps) {
  const [showTable, setShowTable] = useState(false);
  const daily = sparkValues(style, board);
  const dates = dailyDates(computedAt, daily.length);
  const isBaihuo = board === "baihuo";
  const monthly = style.monthlySales
    ?? monthlySalesBaihuo(style.sales7, style.sales15 ?? 0, style.sales30);

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  const metrics: { label: string; value: string; tone?: string }[] = [
    { label: "周转天数", value: turnoverText(style.turnoverDays), tone: style.stockout ? "bad" : "" },
    { label: "日均", value: style.dailyAvg.toFixed(1) },
    ...(isBaihuo
      ? [
          { label: "7天", value: int(style.sales7) },
          { label: "14天", value: int(style.sales14 ?? 0) },
          { label: "15天", value: int(style.sales15 ?? 0) },
          { label: "30天", value: int(style.sales30) },
          { label: "月销量", value: monthly.toFixed(1) },
        ]
      : [
          { label: "近7天", value: int(style.sales7) },
          { label: "7~14天", value: int(style.salesPrev7) },
          { label: "周环比", value: wowText(style.wowRatio), tone: (style.wowRatio ?? 0) < 0 ? "bad" : "up" },
          { label: "60天", value: int(style.sales60) },
        ]),
    { label: "实际库存", value: int(style.qty) },
    { label: "订单占有", value: int(style.occupy) },
    { label: "采购在途", value: int(style.inbound) },
    {
      label: "进货仓",
      value: int(style.inQty ?? 0),
      tone: (style.inQty ?? 0) > 0 ? "up" : "",
    },
    { label: "总库存", value: int(style.onHand) },
    ...(isBaihuo
      ? []
      : [
          { label: "断码 SKU", value: style.brokenSkus ? int(style.brokenSkus) : "0", tone: style.brokenSkus ? "bad" : "" },
          { label: "缺码 SKU", value: style.shortSkus ? int(style.shortSkus) : "0", tone: style.shortSkus ? "warn" : "" },
        ]),
    { label: "补货建议", value: style.replenishQty === null ? "—" : int(style.replenishQty) },
    {
      label: style.moq ? `建议下单（起订${int(style.moq)}）` : "建议下单",
      value: style.orderQty === null || style.orderQty <= 0 ? "—" : int(style.orderQty),
      tone: "up",
    },
  ];

  return (
    <>
      <div className="spu-backdrop" onClick={onClose} />
      <aside className="spu-drawer" role="dialog" aria-label={`${style.styleId} 明细`}>
        <header className="spu-drawer-head">
          <div>
            <div className="mono">{style.styleId}</div>
            <h2>{style.name || "（无品名）"}</h2>
            <div className="small">
              {style.lastSupplier ? `${style.lastSupplier} · ` : ""}
              {style.categoryLine}
              {isBaihuo
                ? (style.skuCount > 1 ? ` · ${int(style.skuCount)} 个规格` : "")
                : ` · ${int(style.skuCount)} 个 SKU`}
              {style.stockout ? <span className="spu-flag">缺货</span> : null}
              {style.remark ? ` · ${style.remark}` : ""}
            </div>
          </div>
          <button type="button" className="btn" onClick={onClose}>关闭</button>
        </header>

        <>
          <ChannelCompare windows={styleChannelWindows(style)} />
          <ShopSources shops={style.saleShops ?? []} />
        </>

        <section>
          <p className="eyebrow">近 30 天逐日出库 · 件</p>
          <TrendChart values={daily} dates={dates} />
          <button type="button" className="btn spu-toggle" onClick={() => setShowTable((current) => !current)}>
            {showTable ? "收起数据" : "查看逐日数据"}
          </button>
          {showTable ? (
            <div className="spu-daily">
              {daily.map((value, index) => (
                <div key={index} className="spu-daily-row">
                  <span className="mono">{dates[index]}</span>
                  <span className="num">{int(value)}</span>
                </div>
              ))}
            </div>
          ) : null}
        </section>

        <section>
          <p className="eyebrow">关键指标</p>
          <div className="spu-metrics">
            {metrics.map((item) => (
              <div key={item.label} className="spu-metric">
                <span className="spu-metric-k">{item.label}</span>
                <span className={`spu-metric-v num ${item.tone ?? ""}`}>{item.value}</span>
              </div>
            ))}
          </div>
          {isBaihuo ? (
            <p className="small spu-formula">
              月销量 = (7天×4 + 15天×2 + 30天) / 3，日均 = 月销量 / 30。
              补货建议 = 日均 × 30 − 总库存。
              建议下单 = 把补货建议向上取整：有起订量按起订量倍数，否则按 10；补货建议 ≤ 0 显示 —。
              总库存 = 实际 − 占有 + 采购在途，进货仓不进总库存。
            </p>
          ) : null}
        </section>

        <section>
          <div className="spu-analyze-head">
            <p className="eyebrow">模型分析</p>
            <button
              type="button"
              className="btn"
              disabled={analysis.status === "loading" || analysis.status === "done"}
              onClick={() => onAnalyze(style.styleId)}
            >
              {analysis.status === "loading"
                ? "分析中…"
                : analysis.status === "done"
                  ? "今日已分析"
                  : analysis.status === "stale"
                    ? "重新分析"
                    : "进行分析"}
            </button>
            {analysis.status === "done" ? (
              <button type="button" className="btn" onClick={() => onAnalyze(style.styleId, true)}>
                重跑
              </button>
            ) : null}
          </div>
          {analysis.status === "error" ? <div className="spu-analyze-error">{analysis.error}</div> : null}
          {analysis.status === "stale" && analysis.analyzedAt ? (
            <div className="spu-analyze-stale">
              上次分析于 {analysis.analyzedAt}，已过期，请重新分析。
            </div>
          ) : null}
          {analysis.status === "done" && analysis.analyzedAt ? (
            <div className="small">分析于 {analysis.analyzedAt}。规则改了可点重跑。</div>
          ) : null}
          {analysis.text ? (
            <div className="spu-analyze-text">{analysis.text}</div>
          ) : analysis.status === "idle" ? (
            <div className="small">
              {isBaihuo
                ? "对照 7/15/30 天、同品类和库存结构判断跟不跟补货建议，建议必须写理由。当天分析过会留在这里。"
                : "对照同季节同品类、近30天形态和库存结构判断跟不跟补货建议，建议必须写理由。当天分析过会留在这里。"}
            </div>
          ) : null}
        </section>
      </aside>
    </>
  );
}
