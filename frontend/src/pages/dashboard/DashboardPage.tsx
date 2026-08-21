import { useEffect, useMemo, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { TopBar } from "../../components/TopBar";
import { LoadFailed, Loading } from "../../components/PageState";
import { usePayload } from "../../hooks/usePayload";
import { decodeDashboard } from "../../data/payload";
import type { DashboardData } from "../../data/payload";
import { cssVar, int, money, pct } from "../../lib/format";
import { ROUTES } from "../../routes";
import { ChartCard } from "./ChartCard";
import { Rail } from "./Rail";
import { RecentOrdersTable } from "./RecentOrdersTable";
import { OrderDrawer } from "./OrderDrawer";
import { TooltipProvider } from "./charts/Tooltip";
import { TrendChart } from "./charts/TrendChart";
import { HBarChart } from "./charts/HBarChart";
import { RecvChart } from "./charts/RecvChart";
import { OpenMixChart } from "./charts/OpenMixChart";
import { SizeHeatmap } from "./charts/SizeHeatmap";
import { ETA_OPTIONS } from "./tiers";
import {
  PRESETS,
  applyFilters,
  buildOrderRows,
  byDim,
  byPeriod,
  makeEtaDays,
  openMix,
  sizeMatrix,
  totals as computeTotals,
  yearBounds,
} from "./model";
import type { DashFilters } from "./model";
import "./dashboard.css";

const RECENT_ORDER_LIMIT = 20;

export default function DashboardPage() {
  const [params, setParams] = useSearchParams();
  const year = params.get("year");
  const { data, error, loading, refreshing, reload } = usePayload<DashboardData>("/api/dashboard", year, decodeDashboard);

  if (loading && !data) return <Loading />;
  if (error && !data) return <LoadFailed message={error} onRetry={reload} />;
  if (!data) return <LoadFailed message="接口没有返回数据。" onRetry={reload} />;
  return (
    <TooltipProvider>
      <Dashboard data={data} year={year} refreshing={refreshing} onYear={(next) => setParams({ year: next })} />
    </TooltipProvider>
  );
}

function sortedOptions(values: string[] | undefined) {
  return (values ?? [])
    .map((label, index) => ({ index, label }))
    .sort((a, b) => a.label.localeCompare(b.label, "zh-CN"));
}

function Dashboard({ data, year, refreshing, onYear }: { data: DashboardData; year: string | null; refreshing?: boolean; onYear: (year: string) => void }) {
  const { dict, meta, orders } = data;
  const today = meta.today || meta.maxDate;

  const years = useMemo(() => {
    const available = meta.availableYears?.length
      ? meta.availableYears
      : [...new Set(orders.map((order) => order.date.slice(0, 4)).filter(Boolean))];
    return available.slice().sort().reverse();
  }, [meta.availableYears, orders]);
  const activeYear = year && years.includes(year)
    ? year
    : (meta.selectedYear && years.includes(meta.selectedYear)
      ? meta.selectedYear
      : (years[0] ?? today.slice(0, 4)));

  const [filters, setFilters] = useState<DashFilters>(() => {
    const [from, to] = yearBounds(activeYear, meta.maxDate);
    return { preset: "year", from, to, status: "", cat: "", buyer: "", query: "", eta: "" };
  });
  const [queryDraft, setQueryDraft] = useState("");
  const [granularity, setGranularity] = useState<"day" | "week" | "month">("month");
  const [sizeMode, setSizeMode] = useState(1);
  const [openedOrder, setOpenedOrder] = useState<number | null>(null);

  useEffect(() => {
    const [from, to] = yearBounds(activeYear, meta.maxDate);
    setFilters({ preset: "year", from, to, status: "", cat: "", buyer: "", query: "", eta: "" });
    setQueryDraft("");
  }, [activeYear, meta.maxDate]);

  useEffect(() => {
    const timer = window.setTimeout(() => {
      setFilters((current) => ({ ...current, query: queryDraft }));
    }, 160);
    return () => window.clearTimeout(timer);
  }, [queryDraft]);

  const etaDays = useMemo(() => makeEtaDays(today), [today]);
  const slice = useMemo(() => applyFilters(data, filters, activeYear, etaDays), [data, filters, activeYear, etaDays]);
  const totals = useMemo(() => computeTotals(slice, orders), [slice, orders]);

  const trend = useMemo(() => byPeriod(slice.rows, orders, granularity), [slice.rows, orders, granularity]);
  const monthly = useMemo(() => byPeriod(slice.rows, orders, "month"), [slice.rows, orders]);
  const cats = useMemo(
    () => byDim(slice.rows, (line) => line.cat, dict.cats).sort((a, b) => b.amount - a.amount),
    [slice.rows, dict.cats],
  );
  const recv = useMemo(
    () => byDim(slice.rows, (line) => line.cat, dict.cats).sort((a, b) => b.qty - a.qty).slice(0, 6),
    [slice.rows, dict.cats],
  );
  const buyers = useMemo(
    () => byDim(slice.rows, (line) => orders[line.order].buyer, dict.buyers).sort((a, b) => b.amount - a.amount),
    [slice.rows, orders, dict.buyers],
  );
  const sizes = useMemo(() => sizeMatrix(slice.rows, dict, sizeMode), [slice.rows, dict, sizeMode]);
  const mix = useMemo(() => openMix(slice.rows, today), [slice.rows, today]);
  const recentOrders = useMemo(
    () => buildOrderRows(slice.rows, orders, dict, etaDays, "created", -1).slice(0, RECENT_ORDER_LIMIT),
    [slice.rows, orders, dict, etaDays],
  );

  const patch = (next: Partial<DashFilters>) => {
    setFilters((currentFilters) => ({ ...currentFilters, ...next }));
  };

  function setEta(key: string) {
    const next = filters.eta === key ? "" : key;
    patch({ eta: next });
  }

  function resetFilters() {
    const [from, to] = yearBounds(activeYear, meta.maxDate);
    setFilters({ preset: "year", from, to, status: "", cat: "", buyer: "", query: "", eta: "" });
    setQueryDraft("");
  }

  const catTotal = cats.reduce((sum, item) => sum + item.amount, 0);
  const mixTotal = mix.reduce((sum, part) => sum + part.value, 0);
  const etaLabel = ETA_OPTIONS.find((option) => option.k === filters.eta)?.label ?? "";

  const sub = (
    <>
      数据源 <b title={meta.warning ?? undefined}>{meta.source}</b> · 查询于 {meta.databaseNow || meta.generated}
      {meta.syncedAt ? ` · 最近同步 ${meta.syncedAt}` : ""} · 业务日期 {meta.minDate} ~ {meta.maxDate} ·{" "}
      {int(meta.orders)} 单 / {int(meta.rows)} 行明细
      {refreshing ? " · 正在更新…" : ""}
      {meta.warning ? <> · <b className="critical">{meta.warning}</b></> : null}
    </>
  );

  return (
    <>
      <TopBar title="采购看板" sub={sub} />

      <div className="filters" role="group" aria-label="全局筛选">
        <div className="field">
          <label htmlFor="f-year">统计年度</label>
          <select id="f-year" value={activeYear} onChange={(event) => onYear(event.target.value)}>
            {years.map((item) => (
              <option key={item} value={item}>
                {item} 年
              </option>
            ))}
          </select>
        </div>
        <div className="field">
          <label id="lb-range">采购日期</label>
          <div className="seg" role="group" aria-labelledby="lb-range">
            {PRESETS.map((preset) => (
              <button
                key={preset.k}
                type="button"
                aria-pressed={filters.preset === preset.k}
                onClick={() => patch({ preset: preset.k })}
              >
                {preset.label}
              </button>
            ))}
          </div>
        </div>
        {filters.preset === "custom" ? (
          <div className="field">
            <label htmlFor="date-from">自定义区间</label>
            <div style={{ display: "flex", gap: 6, alignItems: "center" }}>
              <input
                id="date-from"
                type="date"
                min={yearBounds(activeYear, meta.maxDate)[0]}
                max={yearBounds(activeYear, meta.maxDate)[1]}
                value={filters.from}
                onChange={(event) => patch({ from: event.target.value || yearBounds(activeYear, meta.maxDate)[0] })}
              />
              <span style={{ color: "var(--text-muted)" }}>→</span>
              <input
                type="date"
                min={yearBounds(activeYear, meta.maxDate)[0]}
                max={yearBounds(activeYear, meta.maxDate)[1]}
                value={filters.to}
                onChange={(event) => patch({ to: event.target.value || yearBounds(activeYear, meta.maxDate)[1] })}
              />
            </div>
          </div>
        ) : null}
        <div className="field">
          <label htmlFor="f-eta">到货期限</label>
          <select id="f-eta" value={filters.eta} onChange={(event) => setEta(event.target.value)}>
            {ETA_OPTIONS.map((option) => (
              <option key={option.k} value={option.k}>
                {option.label}
              </option>
            ))}
          </select>
        </div>
        <div className="field">
          <label htmlFor="f-status">状态</label>
          <select id="f-status" value={filters.status} onChange={(event) => patch({ status: event.target.value })}>
            <option value="">全部</option>
            <option value="1">已确认</option>
            <option value="0">待审核</option>
          </select>
        </div>
        <div className="field">
          <label htmlFor="f-cat">品类</label>
          <select id="f-cat" value={filters.cat} onChange={(event) => patch({ cat: event.target.value })}>
            <option value="">全部品类</option>
            {sortedOptions(dict.cats).map((option) => (
              <option key={option.index} value={option.index}>
                {option.label}
              </option>
            ))}
          </select>
        </div>
        <div className="field">
          <label htmlFor="f-buyer">采购员</label>
          <select id="f-buyer" value={filters.buyer} onChange={(event) => patch({ buyer: event.target.value })}>
            <option value="">全部采购员</option>
            {sortedOptions(dict.buyers).map((option) => (
              <option key={option.index} value={option.index}>
                {option.label}
              </option>
            ))}
          </select>
        </div>
        <div className="field">
          <label htmlFor="f-q">搜索</label>
          <input
            id="f-q"
            type="search"
            placeholder="单号 / 商品 / 编码"
            value={queryDraft}
            onChange={(event) => setQueryDraft(event.target.value)}
          />
        </div>
        <button type="button" className="reset" onClick={resetFilters}>
          清空筛选
        </button>
        <div className="slice-note">
          当前切片 {int(totals.orders)} 单 / {int(totals.lines)} 行 · {slice.from} → {slice.to}
          {filters.eta ? ` · 到货期限 ${etaLabel}` : ""}
        </div>
      </div>

      <main className="layout">
        <Rail totals={totals} range={[slice.from, slice.to]} series={monthly} />

        <div className="cards">
          <ChartCard
            eyebrow="金额 · 元"
            title="采购金额走势"
            note="按采购单的下单日期归集"
            span={6}
            controls={
              <div className="mini-seg" role="group" aria-label="时间粒度">
                {(["day", "week", "month"] as const).map((option) => (
                  <button
                    key={option}
                    type="button"
                    aria-pressed={granularity === option}
                    onClick={() => setGranularity(option)}
                  >
                    {option === "day" ? "日" : option === "week" ? "周" : "月"}
                  </button>
                ))}
              </div>
            }
            chart={(width) => <TrendChart data={trend} granularity={granularity} width={width} />}
            table={{
              columns: [
                { label: granularity === "month" ? "月份" : granularity === "week" ? "周起始" : "日期" },
                { label: "采购金额", n: true },
                { label: "采购数量", n: true },
                { label: "采购单", n: true },
              ],
              rows: trend.map((point) => [point.key, int(point.amount), int(point.qty), point.orders]),
            }}
          />

          <ChartCard
            eyebrow="金额 · 元"
            title="品类采购金额"
            span={2}
            chart={(width) => (
              <HBarChart
                width={width}
                labelWidth={92}
                items={cats.map((item) => ({
                  name: item.name,
                  value: item.amount,
                  tip: [
                    { name: "采购金额", value: `${money(item.amount)} 元`, color: cssVar("--series-1") },
                    { name: "采购数量", value: `${int(item.qty)} 件` },
                    { name: "明细行", value: `${int(item.lines)} 行` },
                  ],
                }))}
              />
            )}
            table={{
              columns: [{ label: "品类" }, { label: "采购金额", n: true }, { label: "采购数量", n: true }, { label: "占比", n: true }],
              rows: cats.map((item) => [item.name, int(item.amount), int(item.qty), pct(item.amount, catTotal)]),
            }}
          />

          <ChartCard
            eyebrow="数量 · 件"
            title="各品类入库进度"
            span={2}
            legend={
              <div className="legend">
                <span className="item">
                  <i className="swatch" style={{ background: "var(--series-1)" }} />
                  已入库
                </span>
                <span className="item">
                  <i className="swatch" style={{ background: "var(--track)" }} />
                  待入库
                </span>
              </div>
            }
            chart={(width) => <RecvChart items={recv} width={width} />}
            table={{
              columns: [
                { label: "品类" },
                { label: "采购数量", n: true },
                { label: "已入库", n: true },
                { label: "待入库", n: true },
                { label: "入库率", n: true },
              ],
              rows: recv.map((item) => [
                item.name,
                int(item.qty),
                int(item.inQty),
                int(item.qty - item.inQty),
                pct(item.inQty, item.qty),
              ]),
            }}
          />

          <ChartCard
            eyebrow="金额 · 元"
            title="采购员金额 Top 8"
            span={2}
            chart={(width) => (
              <HBarChart
                width={width}
                labelWidth={104}
                items={buyers.slice(0, 8).map((item) => ({
                  name: item.name,
                  value: item.amount,
                  tip: [
                    { name: "采购金额", value: `${money(item.amount)} 元`, color: cssVar("--series-1") },
                    { name: "采购单", value: `${item.orders} 单` },
                    { name: "采购数量", value: `${int(item.qty)} 件` },
                  ],
                }))}
              />
            )}
            table={{
              columns: [{ label: "采购员" }, { label: "采购金额", n: true }, { label: "采购单", n: true }, { label: "采购数量", n: true }],
              rows: buyers.map((item) => [item.name, int(item.amount), item.orders, int(item.qty)]),
            }}
          />

          <ChartCard
            eyebrow="数量占比 · %"
            title="尺码曲线"
            note="每行按该商品自身采购量归一，看的是码段结构而非绝对值"
            span={4}
            controls={
              <div className="mini-seg" role="group" aria-label="尺码体系">
                {[
                  { m: 1, label: "服装号型" },
                  { m: 2, label: "鞋码" },
                  { m: 3, label: "字母码" },
                ].map((option) => (
                  <button
                    key={option.m}
                    type="button"
                    aria-pressed={sizeMode === option.m}
                    onClick={() => setSizeMode(option.m)}
                  >
                    {option.label}
                  </button>
                ))}
              </div>
            }
            legend={
              <div className="scale-legend">
                <span>占该商品比例</span>
                <span>低</span>
                <span className="ramp">
                  {Array.from({ length: 8 }, (_, level) => (
                    <i key={level} style={{ background: `var(--seq-${level})` }} />
                  ))}
                </span>
                <span>高（≥32%）</span>
              </div>
            }
            chart={(width) => <SizeHeatmap matrix={sizes} mode={sizeMode} width={width} />}
            table={{
              columns: [
                { label: sizeMode === 1 ? "商品（号型）" : sizeMode === 3 ? "商品（字母码）" : "商品（鞋码）" },
                ...sizes.cols.map((col) => ({ label: col, n: true })),
                { label: "合计", n: true },
              ],
              rows: sizes.rows.map((row) => [
                row.name,
                ...sizes.cols.map((col) => {
                  const value = row.cells.get(col) ?? 0;
                  return value ? int(value) : "—";
                }),
                int(row.total),
              ]),
            }}
          />

          <ChartCard
            eyebrow="数量 · 件"
            title="待入库构成"
            note={<>以 {today}（数据最新日期）判定是否逾期</>}
            span={2}
            chart={(width) => <OpenMixChart parts={mix} width={width} />}
            table={{
              columns: [{ label: "到货排期" }, { label: "待入库数量", n: true }, { label: "占比", n: true }, { label: "口径" }],
              rows: mix.map((part) => [part.name, int(part.value), pct(part.value, mixTotal), part.note]),
            }}
          />

          <section className="card span-6">
            <div className="card-head">
              <div>
                <p className="eyebrow">最近建立 · 采购单</p>
                <h2>最近采购单</h2>
                <div className="note">按采购单建立时间从新到旧；点行查看商品，或快捷生成采购合同</div>
              </div>
              <div className="ctrl">
                <span className="note">最近 {int(recentOrders.length)} 单</span>
                <Link
                  className="btn ledger-link"
                  to={activeYear ? `${ROUTES.ledger}?year=${encodeURIComponent(activeYear)}` : ROUTES.ledger}
                >
                  查看交期台账 →
                </Link>
              </div>
            </div>
            <div className="tbl" style={{ maxHeight: "none" }}>
              <RecentOrdersTable
                rows={recentOrders}
                orders={orders}
                dict={dict}
                onOpen={setOpenedOrder}
              />
            </div>
          </section>
        </div>
      </main>

      <OrderDrawer
        order={openedOrder === null ? null : orders[openedOrder]}
        lines={openedOrder === null ? [] : slice.rows.filter((line) => line.order === openedOrder)}
        dict={dict}
        etaDays={etaDays}
        onClose={() => setOpenedOrder(null)}
      />
    </>
  );
}
