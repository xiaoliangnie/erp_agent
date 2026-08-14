import { useCallback, useEffect, useMemo, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { TopBar } from "../../components/TopBar";
import { LoadFailed, Loading } from "../../components/PageState";
import { agentApi, errorText } from "../../api/client";
import { useCredentials } from "../../hooks/useCredentials";
import { usePayload } from "../../hooks/usePayload";
import { decodeDelivery } from "../../data/payload";
import type { DeliveryData } from "../../data/payload";
import { dayNumber, int } from "../../lib/format";
import { ROUTES } from "../../routes";
import { BuyerBars } from "./BuyerBars";
import { LedgerTable } from "./LedgerTable";
import { OrderDrawer } from "./OrderDrawer";
import { TierCards } from "./TierCards";
import { buildOrders, stampWaves } from "./model";
import type { LedgerOrder } from "./model";
import { DESC_FIRST, sortOrders } from "./sorting";
import type { SortKey } from "./sorting";
import { exportReminderCsv } from "./csv";
import { isUrgent, WAVE_TO_BUCKET } from "./waves";
import type { WaveKey } from "./waves";
import "./ledger.css";

const PAGE_SIZE = 25;

interface Filters {
  today: string;
  wave: string;
  buyer: string;
  supplier: string;
  status: string;
  from: string;
  to: string;
  query: string;
  pendingOnly: boolean;
}

const emptyFilters = (today: string): Filters => ({
  today,
  wave: "",
  buyer: "",
  supplier: "",
  status: "",
  from: "",
  to: "",
  query: "",
  pendingOnly: true,
});

/** 下拉里按拼音排序，但选中的值仍是字典下标。 */
function sortedOptions(values: string[] | undefined): { index: number; label: string }[] {
  return (values ?? [])
    .map((label, index) => ({ index, label }))
    .sort((a, b) => a.label.localeCompare(b.label, "zh-CN"));
}

export default function LedgerPage() {
  const [params, setParams] = useSearchParams();
  const year = params.get("year");
  const { data, error, loading, reload } = usePayload<DeliveryData>("/api/delivery", year, decodeDelivery);

  if (loading) return <Loading label="正在读取交期数据…" />;
  if (error) return <LoadFailed message={error} onRetry={reload} />;
  if (!data) return <LoadFailed message="接口没有返回数据。" onRetry={reload} />;
  return <Ledger data={data} year={year} onYear={(next) => setParams({ year: next })} />;
}

function Ledger({ data, year, onYear }: { data: DeliveryData; year: string | null; onYear: (year: string) => void }) {
  const { dict, meta } = data;
  const orders = useMemo(() => buildOrders(data), [data]);

  const years = useMemo(() => {
    const available = meta.availableYears?.length
      ? meta.availableYears
      : [...new Set(orders.map((order) => order.date.slice(0, 4)).filter(Boolean))];
    return available.slice().sort().reverse();
  }, [meta.availableYears, orders]);
  const activeYear = year && years.includes(year) ? year : (years[0] ?? "");

  const baseline = meta.today || meta.maxDate;
  const [filters, setFilters] = useState<Filters>(() => emptyFilters(baseline));
  const [queryDraft, setQueryDraft] = useState("");
  const [sortKey, setSortKey] = useState<SortKey>("urgent");
  const [sortDir, setSortDir] = useState(1);
  const [page, setPage] = useState(0);
  const [opened, setOpened] = useState<LedgerOrder | null>(null);
  const [pushNote, setPushNote] = useState("");
  const [pushing, setPushing] = useState(false);
  const { credentials, update, remember, filled } = useCredentials("agent");

  // 换年度是整页重取，筛选跟着回到初始值。
  useEffect(() => {
    setFilters(emptyFilters(baseline));
    setQueryDraft("");
    setPage(0);
  }, [baseline]);

  useEffect(() => {
    const timer = window.setTimeout(() => {
      setFilters((current) => ({ ...current, query: queryDraft.trim().toLowerCase() }));
      setPage(0);
    }, 160);
    return () => window.clearTimeout(timer);
  }, [queryDraft]);

  const patch = useCallback((next: Partial<Filters>) => {
    setFilters((current) => ({ ...current, ...next }));
    setPage(0);
  }, []);

  const stamps = useMemo(() => stampWaves(orders, filters.today), [orders, filters.today]);
  const today = dayNumber(filters.today);

  /** 除档位以外的筛选。档位卡要用这一份，否则选中一档后其他档全归零。 */
  const withoutWave = useMemo(() => {
    return orders.filter((order) => {
      const stamp = stamps.get(order.index);
      if (!stamp) return false;
      if (activeYear && !order.date.startsWith(`${activeYear}-`)) return false;
      if (filters.pendingOnly && stamp.done) return false;
      if (filters.buyer !== "" && order.buyer !== Number(filters.buyer)) return false;
      if (filters.supplier !== "" && order.supplier !== Number(filters.supplier)) return false;
      if (filters.status !== "" && Number(order.confirmed) !== Number(filters.status)) return false;
      if (filters.from && (!order.eta || order.eta < filters.from)) return false;
      if (filters.to && (!order.eta || order.eta > filters.to)) return false;
      if (filters.query && !order.haystack.includes(filters.query)) return false;
      return true;
    });
  }, [orders, stamps, activeYear, filters]);

  const slice = useMemo(() => {
    const filtered = filters.wave
      ? withoutWave.filter((order) => stamps.get(order.index)?.wave === filters.wave)
      : withoutWave;
    return sortOrders(filtered, stamps, dict, sortKey, sortDir);
  }, [withoutWave, stamps, dict, filters.wave, sortKey, sortDir]);

  const pages = Math.max(1, Math.ceil(slice.length / PAGE_SIZE));
  const current = Math.min(page, pages - 1);
  const rows = slice.slice(current * PAGE_SIZE, current * PAGE_SIZE + PAGE_SIZE);

  const needCount = slice.filter((order) => isUrgent(stamps.get(order.index)?.wave ?? "")).length;
  const pendingTotal = slice.reduce((sum, order) => sum + order.pending, 0);

  const waveTotals = useMemo(() => {
    let count = 0;
    let qty = 0;
    for (const order of withoutWave) {
      const stamp = stamps.get(order.index);
      if (stamp && isUrgent(stamp.wave)) {
        count += 1;
        qty += order.pending;
      }
    }
    return { count, qty };
  }, [withoutWave, stamps]);

  const buyerName = filters.buyer === "" ? "" : (dict.buyers[Number(filters.buyer)] ?? "");
  const waveBucket = WAVE_TO_BUCKET[filters.wave as WaveKey];

  async function sendReminders() {
    if (needCount === 0 || pushing) return;
    if (!filled) {
      setPushNote("请先填写 AGENT_API_TOKEN 和与钉钉/采购员一致的姓名，再发送提醒。");
      return;
    }
    const who = buyerName ? `（仅 ${buyerName}）` : "";
    const wave = waveBucket ? `、档位 ${filters.wave}` : "";
    const extra =
      filters.supplier || filters.query || filters.from || filters.to || filters.status
        ? "推送按采购员和档位走后台催办口径，供应商/搜索等其它筛选不会带上。"
        : "";
    if (!window.confirm(`将把当前需催 ${needCount} 单发到钉钉采购群${who}${wave}。${extra}确定发送？`)) {
      return;
    }
    remember(credentials);
    setPushing(true);
    setPushNote("");
    try {
      const result = await agentApi.post<{
        sent?: boolean;
        skipped?: boolean;
        reason?: string;
        today?: string;
        orderCount?: number;
        buyers?: string[];
      }>(
        "/api/agent/reminders/push",
        {
          operator: credentials.operator.trim(),
          today: filters.today,
          buyer: buyerName,
          buckets: waveBucket ? [waveBucket] : undefined,
        },
        credentials,
      );
      if (result.skipped) {
        const already = (result.reason || "").includes("已经推送过");
        setPushNote(
          already
            ? `当日已推（${result.today || filters.today}）。同一批催办成功后不会重复刷群。`
            : result.reason || "今天没有需要催办的采购单。",
        );
        return;
      }
      const count = result.orderCount ?? needCount;
      const people = (result.buyers || []).join("、");
      setPushNote(`已发到钉钉群：${count} 单${people ? ` · ${people}` : ""}。`);
    } catch (error: unknown) {
      setPushNote(errorText(error));
    } finally {
      setPushing(false);
    }
  }

  function sortBy(key: SortKey) {
    if (sortKey === key) {
      setSortDir((current) => -current);
    } else {
      setSortKey(key);
      setSortDir(DESC_FIRST.includes(key) ? -1 : 1);
    }
    setPage(0);
  }

  function resetFilters() {
    setFilters(emptyFilters(baseline));
    setQueryDraft("");
    setSortKey("urgent");
    setSortDir(1);
    setPage(0);
  }

  const sub = (
    <>
      数据源 <b title={meta.warning ?? undefined}>{meta.source}</b> · {int(meta.orders)} 单 / {int(meta.rows)} 行明细
      {meta.etaMin
        ? ` · 交期 ${meta.etaMin} ~ ${meta.etaMax}（${((meta.etaCoverage ?? 0) * 100).toFixed(0)}% 的行有交期）`
        : ""}
    </>
  );

  return (
    <>
      <TopBar title="交期提醒台账" sub={sub} />

      <div className="filters" role="group" aria-label="筛选">
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
          <label htmlFor="f-today">今天 · 提醒基准日</label>
          <input
            id="f-today"
            type="date"
            value={filters.today}
            onChange={(event) => patch({ today: event.target.value || baseline })}
          />
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
          <label htmlFor="f-sup">供应商</label>
          <select id="f-sup" value={filters.supplier} onChange={(event) => patch({ supplier: event.target.value })}>
            <option value="">全部供应商</option>
            {sortedOptions(dict.suppliers).map((option) => (
              <option key={option.index} value={option.index}>
                {option.label}
              </option>
            ))}
          </select>
        </div>
        <div className="field">
          <label htmlFor="f-st">状态</label>
          <select id="f-st" value={filters.status} onChange={(event) => patch({ status: event.target.value })}>
            <option value="">全部</option>
            <option value="1">已确认</option>
            <option value="0">待审核</option>
          </select>
        </div>
        <div className="field">
          <label htmlFor="f-from">交期区间</label>
          <div style={{ display: "flex", gap: 6, alignItems: "center" }}>
            <input id="f-from" type="date" value={filters.from} onChange={(event) => patch({ from: event.target.value })} />
            <span style={{ color: "var(--text-muted)" }}>→</span>
            <input type="date" value={filters.to} onChange={(event) => patch({ to: event.target.value })} />
          </div>
        </div>
        <div className="field">
          <label htmlFor="f-q">搜索</label>
          <input
            id="f-q"
            type="search"
            placeholder="单号 / 商品 / 编码 / 供应商 / 采购员"
            value={queryDraft}
            onChange={(event) => setQueryDraft(event.target.value)}
          />
        </div>
        <label className="check">
          <input
            type="checkbox"
            checked={filters.pendingOnly}
            onChange={(event) => {
              const pendingOnly = event.target.checked;
              patch({ pendingOnly, wave: pendingOnly && filters.wave === "done" ? "" : filters.wave });
            }}
          />
          只看还有待入库的单
        </label>
        <button type="button" className="reset" onClick={resetFilters}>
          清空筛选
        </button>
        <div className="slice-note">
          {activeYear} 年（1 月 1 日起） · 当前切片 {int(slice.length)} 单 · 待入库 {int(pendingTotal)} 件 · 需催{" "}
          {int(needCount)} 单
        </div>
      </div>

      <div className="ledger-wrap">
        <section className="card">
          <div className="card-head">
            <div>
              <p className="eyebrow">采购单 · 按最急的一波归档</p>
              <h2>交期提醒 · 四波</h2>
              <div className="note">
                以 {filters.today} 为今天。交期取 item_delivery_date，该行没填就退到最早预计到货日期；
                一张单按所有待入库行里最早的交期归档，波次取最急的一档。 当前需催 {int(waveTotals.count)} 单 /{" "}
                {int(waveTotals.qty)} 件。
              </div>
            </div>
            <div className="ledger-push">
              <div className="credentials-grid">
                <input
                  type="password"
                  autoComplete="off"
                  placeholder="AGENT_API_TOKEN"
                  value={credentials.token}
                  onChange={(event) => update({ token: event.target.value })}
                />
                <input
                  autoComplete="off"
                  placeholder="钉钉/采购员姓名"
                  value={credentials.operator}
                  onChange={(event) => update({ operator: event.target.value })}
                />
              </div>
              <div className="push-actions">
                <button
                  type="button"
                  className="btn"
                  disabled={needCount === 0 || pushing}
                  onClick={() => void sendReminders()}
                >
                  {pushing ? "发送中…" : "发送提醒"}
                </button>
                <button
                  type="button"
                  className="btn"
                  onClick={() => exportReminderCsv(slice, stamps, dict, today, filters.today)}
                >
                  导出催办清单
                </button>
              </div>
            </div>
          </div>
          {pushNote ? (
            <div className="notice">
              {pushNote}{" "}
              {pushNote.includes("姓名") || pushNote.includes("Token") || pushNote.includes("未在员工绑定") ? (
                <a href={ROUTES.chat}>打开采购助手 →</a>
              ) : null}
            </div>
          ) : null}
          <TierCards
            orders={withoutWave}
            stamps={stamps}
            selected={filters.wave}
            showDone={!filters.pendingOnly}
            onSelect={(wave) => patch({ wave: filters.wave === wave ? "" : wave })}
          />
        </section>

        <section className="card">
          <div className="card-head">
            <div>
              <p className="eyebrow">待入库数量 · 件</p>
              <h2>按采购员的催办量</h2>
              <div className="note">
                每人一条，按提醒波次堆叠。右端是需催量（前四波合计，即 20 天内 + 已逾期）。
              </div>
            </div>
          </div>
          <BuyerBars orders={slice} stamps={stamps} dict={dict} />
        </section>

        <section className="card">
          <div className="card-head">
            <div>
              <p className="eyebrow">明细 · 采购单</p>
              <h2>交期台账</h2>
              <div className="note">点任意一行查看该单的商品明细与四波排期。</div>
            </div>
            <div className="ctrl">
              <span className="note">{int(slice.length)} 单</span>
            </div>
          </div>
          <div className="tbl-scroll">
            <LedgerTable
              rows={rows}
              stamps={stamps}
              dict={dict}
              today={today}
              sortKey={sortKey}
              sortDir={sortDir}
              onSort={sortBy}
              onOpen={setOpened}
            />
          </div>
          {slice.length === 0 ? <div className="empty">当前筛选下没有采购单。</div> : null}
          <div className="pager">
            <button type="button" disabled={current === 0} onClick={() => setPage(current - 1)}>
              上一页
            </button>
            <span className="num">
              {current + 1} / {pages}
            </span>
            <button type="button" disabled={current >= pages - 1} onClick={() => setPage(current + 1)}>
              下一页
            </button>
            <span className="sp" />
            <span className="note">
              {slice.length
                ? `第 ${int(current * PAGE_SIZE + 1)}–${int(Math.min(slice.length, (current + 1) * PAGE_SIZE))} 单`
                : ""}
            </span>
          </div>
        </section>
      </div>

      <OrderDrawer
        order={opened}
        stamp={opened ? (stamps.get(opened.index) ?? null) : null}
        dict={dict}
        today={today}
        onClose={() => setOpened(null)}
      />
    </>
  );
}
