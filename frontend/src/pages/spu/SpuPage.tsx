import { useCallback, useEffect, useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import { TopBar } from "../../components/TopBar";
import { Loading, LoadFailed } from "../../components/PageState";
import { publicApi, errorText } from "../../api/client";
import { useLivePoll } from "../../hooks/useLivePoll";
import { useServerClock } from "../../hooks/useServerClock";
import { int } from "../../lib/format";
import { ChannelBoardCard, ChannelWindowsCell, styleChannelWindows } from "./ChannelMix";
import { StyleDrawer } from "./StyleDrawer";
import { ROUTES } from "../../routes";
import type { AnalyzePayload, SpuAnalysis, SpuStyle, SpuSummary } from "./types";
import { sparkValues, turnoverText, wowText } from "./types";
import "./spu.css";

type AlertFilter = "all" | "stockout" | "broken" | "short" | "replenish" | "inWarehouse";
type SortKey = "turnover" | "dailyAvg" | "wow" | "broken" | "sales7" | "sales30" | "replenish";
export type SpuBoard = "apparel" | "baihuo";

const APPAREL_LINES = ["鞋类", "通勤裤", "服装-非通勤裤"] as const;
const TURNOVER_HIGHLIGHT = 35;
const IDLE_ANALYSIS: SpuAnalysis = { status: "idle", text: "", error: "", analyzedAt: "" };
function analyzePrefix(board: SpuBoard): string {
  return board === "baihuo" ? "baihuo-analyze-v1" : "spu-analyze-v1";
}

function cacheDay(computedAt: string): string {
  return computedAt.slice(0, 10);
}

function readLocalAnalysis(styleId: string, day: string, board: SpuBoard = "apparel"): SpuAnalysis | null {
  if (!day) return null;
  try {
    const raw = window.localStorage.getItem(`${analyzePrefix(board)}:${day}:${styleId}`);
    if (!raw) return null;
    const row = JSON.parse(raw) as { text?: string; analyzedAt?: string };
    if (!row.text) return null;
    return {
      status: "done",
      text: row.text,
      error: "",
      analyzedAt: row.analyzedAt || "",
      stale: false,
    };
  } catch {
    return null;
  }
}

function writeLocalAnalysis(styleId: string, day: string, analysis: SpuAnalysis, board: SpuBoard = "apparel") {
  if (!day || !analysis.text || analysis.status === "error") return;
  try {
    window.localStorage.setItem(
      `${analyzePrefix(board)}:${day}:${styleId}`,
      JSON.stringify({ text: analysis.text, analyzedAt: analysis.analyzedAt || "" }),
    );
  } catch {
    /* 配额满了不影响主流程 */
  }
}

function loadLocalDay(day: string, board: SpuBoard = "apparel"): Record<string, SpuAnalysis> {
  const out: Record<string, SpuAnalysis> = {};
  if (!day || typeof window === "undefined") return out;
  const prefix = `${analyzePrefix(board)}:${day}:`;
  try {
    for (let index = 0; index < window.localStorage.length; index += 1) {
      const key = window.localStorage.key(index);
      if (!key || !key.startsWith(prefix)) continue;
      const styleId = key.slice(prefix.length);
      const row = readLocalAnalysis(styleId, day, board);
      if (row) out[styleId] = row;
    }
  } catch {
    return out;
  }
  return out;
}

function analysisFromPayload(payload: AnalyzePayload): SpuAnalysis {
  const text = payload.analysis || "";
  if (!text) return IDLE_ANALYSIS;
  return {
    status: payload.stale ? "stale" : "done",
    text,
    error: "",
    analyzedAt: payload.analyzedAt || "",
    stale: Boolean(payload.stale),
  };
}

/** 出库趋势线。全 0 画平线；峰值点标最大日销。 */
function Sparkline({ values, label = "近30天出库" }: { values: number[]; label?: string }) {
  const width = 120;
  const height = 26;
  const pad = 2;
  if (!values.length) return <span className="small">—</span>;
  const max = Math.max(...values);
  const step = (width - pad * 2) / Math.max(1, values.length - 1);
  const y = (value: number) =>
    max <= 0 ? height - pad : height - pad - (value / max) * (height - pad * 2);
  const points = values.map((value, index) => `${(pad + index * step).toFixed(1)},${y(value).toFixed(1)}`).join(" ");
  const peak = values.indexOf(max);
  return (
    <svg
      className="spu-spark"
      viewBox={`0 0 ${width} ${height}`}
      width={width}
      height={height}
      role="img"
      aria-label={`${label}，峰值 ${Math.round(max)}`}
    >
      <polyline points={points} fill="none" strokeWidth="1.5" />
      {max > 0 ? <circle cx={pad + peak * step} cy={y(max)} r="2" /> : null}
    </svg>
  );
}

function sortValue(item: SpuStyle, key: SortKey): number {
  switch (key) {
    case "turnover":
      // 周转空值（没有日均）排最后
      return item.turnoverDays === null ? Number.POSITIVE_INFINITY : item.turnoverDays;
    case "dailyAvg": return -item.dailyAvg;
    case "wow": return item.wowRatio === null ? Number.POSITIVE_INFINITY : -item.wowRatio;
    case "broken": return -(item.brokenSkus * 1000 + item.shortSkus);
    case "sales7": return -item.sales7;
    case "sales30": return -item.sales30;
    case "replenish": return item.replenishQty === null ? Number.NEGATIVE_INFINITY : -item.replenishQty;
  }
}

export default function SpuPage({ board = "apparel" }: { board?: SpuBoard }) {
  const isBaihuo = board === "baihuo";
  const [summary, setSummary] = useState<SpuSummary | null>(null);
  const [error, setError] = useState("");
  const [line, setLine] = useState("");
  const [supplier, setSupplier] = useState("");
  const [alert, setAlert] = useState<AlertFilter>("all");
  const [keyword, setKeyword] = useState("");
  const [sortKey, setSortKey] = useState<SortKey>("turnover");
  const [selectedId, setSelectedId] = useState("");
  const [analyses, setAnalyses] = useState<Record<string, SpuAnalysis>>({});
  const [uploadNote, setUploadNote] = useState("");
  const [uploading, setUploading] = useState(false);
  const [computing, setComputing] = useState(false);
  const [picked, setPicked] = useState<Record<string, true>>({});
  const [syncing, setSyncing] = useState(false);
  const clock = useServerClock();
  const navigate = useNavigate();

  const load = useCallback(async () => {
    const q = board === "baihuo" ? "?board=baihuo" : "";
    const payload = await publicApi.get<SpuSummary>(`/api/spu/summary${q}`);
    setSummary(payload);
    const day = cacheDay(payload.computedAt || "");
    setAnalyses((current) => {
      const next = { ...loadLocalDay(day, board), ...current };
      for (const [styleId, row] of Object.entries(payload.analyses || {})) {
        if (current[styleId]?.status === "loading") continue;
        const mapped = analysisFromPayload(row);
        if (mapped.text) {
          next[styleId] = mapped;
          writeLocalAnalysis(styleId, day, mapped, board);
        }
      }
      return next;
    });
    return payload;
  }, [board]);

  useEffect(() => {
    load().catch((cause: unknown) => setError(errorText(cause)));
  }, [load]);

  const silentLoad = useCallback(async () => {
    if (computing) return;
    setSyncing(true);
    try {
      await load();
    } catch {
      // 留下上一屏，不把正在看的表清掉。
    } finally {
      setSyncing(false);
    }
  }, [computing, load]);

  useLivePoll(silentLoad, 20_000, Boolean(summary) && !computing);

  const computeNow = useCallback(async () => {
    setComputing(true);
    setUploadNote("正在后台计算，大约一分钟…");
    try {
      const q = board === "baihuo" ? "?board=baihuo" : "";
      await publicApi.post(`/api/spu/refresh${q}`, {});
      for (let step = 0; step < 30; step += 1) {
        await new Promise((resolve) => window.setTimeout(resolve, 3000));
        const payload = await load();
        if (payload.computedAt && !payload.refreshing) {
          setUploadNote("已更新");
          return;
        }
      }
      setUploadNote("还在算，稍后再刷新页面");
    } catch (cause: unknown) {
      setUploadNote(errorText(cause));
    } finally {
      setComputing(false);
    }
  }, [board, load]);

  const uploadPlanSource = useCallback(async (file: File) => {
    setUploading(true);
    setUploadNote(`正在上传「${file.name}」…`);
    try {
      const response = await fetch("/api/spu/plan-source", { method: "POST", body: file });
      const payload = (await response.json()) as { ok: boolean; message?: string; error?: string };
      setUploadNote(payload.ok ? payload.message ?? "已开始重生成" : payload.error ?? `HTTP ${response.status}`);
    } catch (cause: unknown) {
      setUploadNote(errorText(cause));
    } finally {
      setUploading(false);
    }
  }, []);

  const loadAnalysis = useCallback(async (styleId: string, day: string) => {
    const local = readLocalAnalysis(styleId, day, board);
    if (local) {
      setAnalyses((current) => {
        if (current[styleId]?.text) return current;
        return { ...current, [styleId]: local };
      });
    }
    try {
      const q = board === "baihuo" ? "&board=baihuo" : "";
      const result = await publicApi.get<AnalyzePayload>(
        `/api/spu/analyze?styleId=${encodeURIComponent(styleId)}${q}`,
      );
      const mapped = analysisFromPayload(result);
      if (!mapped.text) return;
      writeLocalAnalysis(styleId, day, mapped, board);
      setAnalyses((current) => {
        if (current[styleId]?.status === "loading") return current;
        return { ...current, [styleId]: mapped };
      });
    } catch {
      /* 接口还没起来时保留本机/内存里的分析结果，不要清空 */
    }
  }, [board]);

  const analyze = useCallback(async (styleId: string, force = false) => {
    setAnalyses((current) => {
      const previous = current[styleId];
      return {
        ...current,
        [styleId]: {
          status: "loading",
          text: previous?.text || "",
          error: "",
          analyzedAt: previous?.analyzedAt || "",
        },
      };
    });
    try {
      const result = await publicApi.post<AnalyzePayload>("/api/spu/analyze", { styleId, force, board });
      const mapped = analysisFromPayload(result);
      writeLocalAnalysis(styleId, cacheDay(summary?.computedAt || ""), mapped, board);
      setAnalyses((current) => ({ ...current, [styleId]: mapped }));
    } catch (cause: unknown) {
      setAnalyses((current) => ({
        ...current,
        [styleId]: { status: "error", text: current[styleId]?.text || "", error: errorText(cause) },
      }));
    }
  }, [board, summary?.computedAt]);

  useEffect(() => {
    if (!selectedId) return;
    void loadAnalysis(selectedId, cacheDay(summary?.computedAt || ""));
  }, [selectedId, loadAnalysis, summary?.computedAt]);

  const rows = useMemo(() => {
    if (!summary) return [];
    const text = keyword.trim().toLowerCase();
    const filtered = summary.styles.filter((item) => {
      if (line && item.categoryLine !== line) return false;
      if (alert === "stockout" && !item.stockout) return false;
      if (alert === "broken" && item.brokenSkus <= 0) return false;
      if (alert === "short" && item.shortSkus <= 0) return false;
      if (alert === "replenish" && (item.replenishQty ?? 0) <= 0) return false;
      if (alert === "inWarehouse" && (item.inQty ?? 0) <= 0) return false;
      if (supplier === "__none__" && item.lastSupplier) return false;
      if (supplier && supplier !== "__none__" && item.lastSupplier !== supplier) return false;
      if (text && !item.styleId.toLowerCase().includes(text)
        && !item.name.toLowerCase().includes(text)
        && !(item.lastSupplier || "").toLowerCase().includes(text)) return false;
      return true;
    });
    // 缺货永远排最前，其余按所选指标
    return filtered.sort((a, b) => {
      if (a.stockout !== b.stockout) return a.stockout ? -1 : 1;
      return sortValue(a, sortKey) - sortValue(b, sortKey);
    });
  }, [summary, line, supplier, alert, keyword, sortKey]);

  const selected = useMemo(
    () => (summary ? summary.styles.find((item) => item.styleId === selectedId) ?? null : null),
    [summary, selectedId],
  );

  const pickedIds = useMemo(() => Object.keys(picked), [picked]);
  const visiblePicked = useMemo(
    () => rows.filter((item) => picked[item.styleId]).length,
    [rows, picked],
  );
  const suggestIds = useMemo(
    () => rows.filter((item) => (item.orderQty ?? 0) > 0).map((item) => item.styleId),
    [rows],
  );

  const togglePick = useCallback((styleId: string, on: boolean) => {
    setPicked((current) => {
      const next = { ...current };
      if (on) next[styleId] = true;
      else delete next[styleId];
      return next;
    });
  }, []);

  const pickSuggest = useCallback(() => {
    setPicked((current) => {
      const next = { ...current };
      for (const styleId of suggestIds) next[styleId] = true;
      return next;
    });
  }, [suggestIds]);

  const openPurchase = useCallback(() => {
    if (!pickedIds.length) return;
    const params = new URLSearchParams({ board, ids: pickedIds.join(",") });
    navigate(`${ROUTES.purchase}?${params.toString()}`);
  }, [board, navigate, pickedIds]);

  const supplierOptions = useMemo(() => {
    if (!summary) return [];
    const names = new Set<string>();
    for (const item of summary.styles) {
      if (item.lastSupplier) names.add(item.lastSupplier);
    }
    return [...names].sort((left, right) => left.localeCompare(right, "zh"));
  }, [summary]);

  const lineOptions = useMemo(() => {
    if (isBaihuo) {
      if (!summary) return [];
      const names = new Set<string>();
      for (const item of summary.styles) {
        if (item.categoryLine) names.add(item.categoryLine);
      }
      return [...names].sort((left, right) => {
        if (left === "文创百货") return -1;
        if (right === "文创百货") return 1;
        return left.localeCompare(right, "zh");
      });
    }
    return [...APPAREL_LINES];
  }, [isBaihuo, summary]);

  if (error) {
    return (
      <LoadFailed
        title={isBaihuo ? "读不到自营百货结果表" : "读不到 SPU 结果表"}
        message={error}
        onRetry={() => {
          setError("");
          load().catch((cause: unknown) => setError(errorText(cause)));
        }}
      />
    );
  }
  if (!summary) return <Loading label={isBaihuo ? "正在读取自营百货结果表…" : "正在读取 SPU 结果表…"} />;

  const replenishCount = summary.styles.filter((item) => (item.replenishQty ?? 0) > 0).length;
  const inboundCount = summary.styles.filter((item) => (item.inQty ?? 0) > 0).length;
  const boardWindows = [
    {
      label: "7天",
      online: summary.styles.reduce((sum, item) => sum + (item.sales7Online ?? 0), 0),
      offline: summary.styles.reduce((sum, item) => sum + (item.sales7Offline ?? 0), 0),
    },
    {
      label: "15天",
      online: summary.styles.reduce((sum, item) => sum + (item.sales15Online ?? 0), 0),
      offline: summary.styles.reduce((sum, item) => sum + (item.sales15Offline ?? 0), 0),
    },
    {
      label: "30天",
      online: summary.styles.reduce((sum, item) => sum + (item.sales30Online ?? 0), 0),
      offline: summary.styles.reduce((sum, item) => sum + (item.sales30Offline ?? 0), 0),
    },
  ];
  const cards: { k: AlertFilter; label: string; value: number; note: string; tone: string }[] = [
    {
      k: "all",
      label: "进表款数",
      value: summary.styleCount,
      note: isBaihuo ? "标签「自营百货」，剔清仓/淘汰/有升级" : "自营 × 三品类线，剔清仓/淘汰/有升级",
      tone: "",
    },
    {
      k: "stockout",
      label: "缺货",
      value: summary.stockoutCount,
      note: isBaihuo ? "周转 < 30；日均=月销量/30" : "周转 < 30 天",
      tone: "critical",
    },
    ...(isBaihuo
      ? [
          { k: "replenish" as const, label: "需补货", value: replenishCount, note: isBaihuo ? "日均×30 − 总库存 > 0" : "日均×60 − 总库存 > 0", tone: "warning" },
          { k: "inWarehouse" as const, label: "进货仓待上架", value: inboundCount, note: "已到仓，不进总库存", tone: "warning" },
        ]
      : [
          { k: "broken" as const, label: "有断码", value: summary.brokenStyleCount, note: "任一 SKU 实际库存 < 1", tone: "warning" },
          { k: "short" as const, label: "有缺码", value: summary.shortStyleCount, note: "SKU 库存撑不过 7 天销量", tone: "warning" },
        ]),
  ];

  const clockBit = clock.ready ? `现在 ${clock.now}` : "";
  const syncBit = syncing ? "正在同步" : "表格自动更新";
  const sub = summary.computedAt
    ? [clockBit, `数据时点 ${summary.computedAt}`, "每天 09:00 自动重算", syncBit].filter(Boolean).join(" · ")
    : isBaihuo
      ? "结果表还没有数据，点「现在计算」或等每日 09:00"
      : "结果表还没有数据，等每日 09:00 重算或先跑一次 run_spu_alerts";

  return (
    <>
      <TopBar title={isBaihuo ? "自营百货" : "鞋服 SPU 总表"} sub={sub} />
      <main className="spu">
        <section className="spu-cards">
          {cards.map((card) => (
            <button
              key={card.k}
              type="button"
              className={`spu-card ${card.tone}`}
              aria-pressed={alert === card.k}
              onClick={() => setAlert(card.k)}
            >
              <span className="eyebrow">{card.label}</span>
              <span className="spu-card-v num">{int(card.value)}</span>
              <span className="spu-card-n">{card.note}</span>
            </button>
          ))}
          <ChannelBoardCard windows={boardWindows} />
        </section>

        <section className="spu-controls">
          <div className="seg" role="group" aria-label="品类线">
            <button type="button" aria-pressed={line === ""} onClick={() => setLine("")}>全部</button>
            {lineOptions.map((name) => (
              <button key={name} type="button" aria-pressed={line === name} onClick={() => setLine(name)}>
                {name}
              </button>
            ))}
          </div>
          <div className="seg" role="group" aria-label="排序">
            {(
              (isBaihuo
                ? [
                    ["turnover", "周转最紧"],
                    ["dailyAvg", "日均最高"],
                    ["sales30", "30天最热"],
                    ["replenish", "建议最多"],
                  ]
                : [
                    ["turnover", "周转最紧"],
                    ["broken", "断码最多"],
                    ["sales7", "近7天最热"],
                    ["wow", "环比最猛"],
                    ["replenish", "建议最多"],
                  ]) as [SortKey, string][]
            ).map(([key, label]) => (
              <button key={key} type="button" aria-pressed={sortKey === key} onClick={() => setSortKey(key)}>
                {label}
              </button>
            ))}
          </div>
          <select
            className="spu-supplier-filter"
            aria-label="供应商"
            value={supplier}
            onChange={(event) => setSupplier(event.target.value)}
          >
            <option value="">全部供应商</option>
            <option value="__none__">无供应商</option>
            {supplierOptions.map((name) => (
              <option key={name} value={name}>{name}</option>
            ))}
          </select>
          <input
            type="search"
            placeholder={isBaihuo ? "搜商品编码 / 商品名称 / 供应商" : "搜款式编码 / 品名 / 供应商"}
            value={keyword}
            onChange={(event) => setKeyword(event.target.value)}
          />
          <span className="small num">{rows.length} 款</span>
          <button type="button" className="btn" disabled={computing} onClick={() => void computeNow()}>
            {computing ? "计算中…" : "现在计算"}
          </button>
          {isBaihuo ? null : (
            <label className={`btn spu-upload ${uploading ? "is-busy" : ""}`}>
              {uploading ? "上传中…" : "上传订货表"}
              <input
                type="file"
                accept=".xlsx"
                hidden
                disabled={uploading}
                onChange={(event) => {
                  const file = event.target.files?.[0];
                  event.target.value = "";
                  if (file) void uploadPlanSource(file);
                }}
              />
            </label>
          )}
          {uploadNote ? <span className="small">{uploadNote}</span> : null}
          <button type="button" className="btn" disabled={!suggestIds.length} onClick={pickSuggest}>
            勾选建议下单
          </button>
          <button type="button" className="btn" disabled={!pickedIds.length} onClick={() => setPicked({})}>
            清空勾选
          </button>
          <button
            type="button"
            className="btn primary"
            disabled={!pickedIds.length}
            onClick={openPurchase}
          >
            {`建立采购单${visiblePicked ? `（${visiblePicked}）` : ""}`}
          </button>
        </section>

        <section className="spu-table-wrap">
          <table className="spu-table">
            <thead>
              <tr>
                <th className="spu-check">
                  <input
                    type="checkbox"
                    checked={Boolean(suggestIds.length && suggestIds.every((id) => picked[id]))}
                    onChange={(event) => {
                      if (event.target.checked) pickSuggest();
                      else setPicked({});
                    }}
                    aria-label="勾选当前筛选里有建议下单的款"
                  />
                </th>
                <th>{isBaihuo ? "商品编码" : "款式编码"}</th>
                <th>{isBaihuo ? "商品名称" : "品名"}</th>
                <th>供应商</th>
                <th>品类线</th>
                <th className="num">周转天数</th>
                {isBaihuo ? null : (
                  <>
                    <th className="num">断码</th>
                    <th className="num">缺码</th>
                  </>
                )}
                <th className="num">日均</th>
                <th>近30天趋势</th>
                {isBaihuo ? null : (
                  <>
                    <th className="num">近7天</th>
                    <th className="num">7~14天</th>
                    <th className="num">周环比</th>
                  </>
                )}
                {isBaihuo ? (
                  <>
                    <th className="num">7天</th>
                    <th className="num">14天</th>
                    <th className="num">30天</th>
                  </>
                ) : (
                  <th className="num">60天</th>
                )}
                <th className="spu-ch-col" title="7 / 15 / 30 天线上 / 线下，按店铺设置分组">线上 / 线下</th>
                <th className="num">总库存</th>
                <th className="num" title="已到进货仓未上架，不进总库存">进货仓</th>
                <th className="num" title={isBaihuo ? "日均 × 30 − 总库存" : "日均 × 60 − 总库存"}>补货建议</th>
                <th className="num" title="补货建议向上取整：有起订量按起订量倍数，否则按 10">建议下单</th>
                <th>备注</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((item) => (
                <tr
                  key={item.styleId}
                  className={`spu-row ${item.stockout ? "is-stockout" : ""} ${picked[item.styleId] ? "is-picked" : ""}`}
                  onClick={() => setSelectedId(item.styleId)}
                >
                  <td className="spu-check" onClick={(event) => event.stopPropagation()}>
                    <input
                      type="checkbox"
                      checked={Boolean(picked[item.styleId])}
                      onChange={(event) => togglePick(item.styleId, event.target.checked)}
                      aria-label={`勾选 ${item.styleId}`}
                    />
                  </td>
                  <td className="mono">{item.styleId}</td>
                  <td className="spu-name" title={item.name}>{item.name}</td>
                  <td className="spu-supplier" title={item.lastSupplier || ""}>{item.lastSupplier || "—"}</td>
                  <td>{item.categoryLine}</td>
                  <td className={`num ${item.turnoverDays !== null && item.turnoverDays < TURNOVER_HIGHLIGHT ? "t-warn" : ""}`}>
                    {item.stockout ? <span className="spu-flag">缺货</span> : null}
                    {turnoverText(item.turnoverDays)}
                  </td>
                  {isBaihuo ? null : (
                    <>
                      <td className={`num ${item.brokenSkus > 0 ? "t-bad" : ""}`}>{item.brokenSkus || ""}</td>
                      <td className={`num ${item.shortSkus > 0 ? "t-warn" : ""}`}>{item.shortSkus || ""}</td>
                    </>
                  )}
                  <td className="num">{item.dailyAvg.toFixed(1)}</td>
                  <td>
                    <Sparkline
                      values={sparkValues(item, board)}
                      label="近30天出库"
                    />
                  </td>
                  {isBaihuo ? null : (
                    <>
                      <td className="num">{int(item.sales7)}</td>
                      <td className="num">{int(item.salesPrev7)}</td>
                      <td className={`num ${item.wowRatio === null ? "" : item.wowRatio >= 0 ? "t-up" : "t-down"}`}>
                        {wowText(item.wowRatio)}
                      </td>
                    </>
                  )}
                  {isBaihuo ? (
                    <>
                      <td className="num">{int(item.sales7)}</td>
                      <td className="num">{int(item.sales14 ?? 0)}</td>
                      <td className="num">{int(item.sales30)}</td>
                    </>
                  ) : (
                    <td className="num">{int(item.sales60)}</td>
                  )}
                  <td className="spu-ch-col">
                    <ChannelWindowsCell windows={styleChannelWindows(item)} />
                  </td>
                  <td
                    className="num"
                    title={`实际 ${int(item.qty)} · 占有 ${int(item.occupy)} · 在途 ${int(item.inbound)}`}
                  >
                    {int(item.onHand)}
                  </td>
                  <td
                    className={`num ${(item.inQty ?? 0) > 0 ? "t-up" : ""}`}
                    title={(item.inQty ?? 0) > 0 ? "已到进货仓，不进总库存，先别按建议再下单" : "进货仓为空"}
                  >
                    {(item.inQty ?? 0) > 0 ? int(item.inQty ?? 0) : ""}
                  </td>
                  <td className="num">{item.replenishQty === null ? "—" : int(item.replenishQty)}</td>
                  <td className="num spu-order">
                    {item.orderQty === null || item.orderQty <= 0 ? "—" : int(item.orderQty)}
                    {item.moq ? <span className="spu-moq">起订{int(item.moq)}</span> : null}
                  </td>
                  <td className="small">{item.remark}</td>
                </tr>
              ))}
            </tbody>
          </table>
          {rows.length === 0 ? <div className="empty">当前筛选没有款式</div> : null}
        </section>

        {selected ? (
          <StyleDrawer
            style={selected}
            computedAt={summary.computedAt}
            analysis={analyses[selected.styleId] ?? IDLE_ANALYSIS}
            onAnalyze={analyze}
            onClose={() => setSelectedId("")}
            board={board}
          />
        ) : null}
      </main>
    </>
  );
}
