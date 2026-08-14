import { useCallback, useEffect, useRef, useState } from "react";
import { TopBar } from "../../components/TopBar";
import { errorText, exchangeApi } from "../../api/client";
import { useCredentials } from "../../hooks/useCredentials";
import { newId } from "../../lib/id";
import type { ExchangeJob, ExchangeOrderItems, ExchangeOrderSearch, ExchangePolicy, ExchangeProduct, ExchangeStatus } from "./types";
import { JobCard } from "./JobCard";
import "./exchange.css";

const POLL_MS = 3000;

/** 把粘进来的一坨订单号切开：空白、半角/全角逗号和分号都当分隔符。 */
function parseOids(raw: string): string[] {
  return [...new Set(raw.split(/[\s,，;；]+/).map((item) => item.trim()).filter(Boolean))];
}

export default function ExchangePage() {
  const { credentials, update, remember, filled } = useCredentials("exchange");
  const [connected, setConnected] = useState(false);
  const [status, setStatus] = useState<ExchangeStatus | null>(null);
  const [jobs, setJobs] = useState<ExchangeJob[]>([]);
  const [products, setProducts] = useState<ExchangeProduct[]>([]);
  const [openId, setOpenId] = useState("");
  const [message, setMessage] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [productQuery, setProductQuery] = useState("");
  const [orderQuery, setOrderQuery] = useState("");
  const [orderSearch, setOrderSearch] = useState<ExchangeOrderSearch | null>(null);
  const [orderLoading, setOrderLoading] = useState(false);
  const [orderError, setOrderError] = useState("");
  const [selectedOids, setSelectedOids] = useState<string[]>([]);
  const [policy, setPolicy] = useState<ExchangePolicy | null>(null);
  const [sourceProduct, setSourceProduct] = useState<ExchangeProduct | null>(null);
  const [orderItems, setOrderItems] = useState<ExchangeOrderItems | null>(null);
  const [form, setForm] = useState({ source: "", target: "", oids: "" });
  const [pollStale, setPollStale] = useState(false);

  // 轮询回调里要读最新凭证，又不想因为凭证变化重启定时器。
  const credentialsRef = useRef(credentials);
  credentialsRef.current = credentials;

  const refresh = useCallback(async () => {
    const auth = credentialsRef.current;
    if (!auth.token.trim()) return;
    const [next, list] = await Promise.all([
      exchangeApi.get<ExchangeStatus>("/api/exchange/status", auth),
      exchangeApi.get<{ jobs: ExchangeJob[] }>("/api/exchange/jobs", auth),
    ]);
    setStatus(next);
    setJobs(list.jobs);
    setPollStale(false);
  }, []);

  const loadProducts = useCallback(async (query: string) => {
    const auth = credentialsRef.current;
    if (!auth.token.trim()) return;
    const params = new URLSearchParams({ limit: "200", q: query });
    const found = await exchangeApi.get<{ products: ExchangeProduct[] }>(
      `/api/exchange/products?${params}`,
      auth,
    );
    setProducts(found.products);
  }, []);

  const loadOrders = useCallback(async (query: string) => {
    const auth = credentialsRef.current;
    if (!auth.token.trim()) return;
    setOrderLoading(true);
    setOrderError("");
    try {
      const params = new URLSearchParams({ limit: "50", q: query });
      const found = await exchangeApi.get<ExchangeOrderSearch>(`/api/exchange/orders?${params}`, auth);
      setOrderSearch(found);
    } catch (error) {
      setOrderError(errorText(error));
      throw error;
    } finally {
      setOrderLoading(false);
    }
  }, []);

  const loadOrderItems = useCallback(async (oids: string[]) => {
    const auth = credentialsRef.current;
    if (!auth.token.trim()) return;
    const params = new URLSearchParams();
    oids.forEach((oid) => params.append("o_id", oid));
    setOrderItems(await exchangeApi.get<ExchangeOrderItems>(`/api/exchange/order-items?${params}`, auth));
  }, []);

  const connect = useCallback(async () => {
    if (!filled) throw new Error("请填写 Token 和操作人姓名");
    remember(credentials);
    setMessage("");

    // 订单选择器直接读取本地镜像库，不依赖 ERP Worker、任务队列或商品接口。
    // 这些辅助接口中的任意一个暂时失败时，仍应允许用户选单和查看订单商品。
    const results = await Promise.allSettled([
      loadOrders(""),
      refresh(),
      loadProducts(""),
      exchangeApi.get<ExchangePolicy>("/api/exchange/policy", credentials).then(setPolicy),
    ]);
    const succeeded = results.some((result) => result.status === "fulfilled");
    setConnected(succeeded);
    if (!succeeded) {
      const firstFailure = results.find((result): result is PromiseRejectedResult => result.status === "rejected");
      throw firstFailure?.reason ?? new Error("换货页面连接失败");
    }

    const auxiliaryFailures = results.slice(1).filter((result) => result.status === "rejected").length;
    if (results[0].status === "rejected") {
      setMessage(`订单数据源加载失败：${errorText(results[0].reason)}`);
    } else if (auxiliaryFailures) {
      setMessage("订单库已连接；ERP 状态或商品资料暂时不可用，可稍后重新连接。");
    }
  }, [credentials, filled, loadOrders, loadProducts, refresh, remember]);

  // 只对页面打开时已有的凭证自动连接。不要在用户刚输入 Token 第一个字符时抢先发起认证。
  const autoConnectStoredCredentials = useRef(filled);
  const bootstrapped = useRef(false);
  useEffect(() => {
    if (bootstrapped.current || !autoConnectStoredCredentials.current) return;
    bootstrapped.current = true;
    connect().catch((error: unknown) => setMessage(errorText(error)));
  }, [connect]);

  useEffect(() => {
    if (!connected) return;
    const timer = window.setInterval(() => {
      refresh().catch(() => {
        setPollStale(true);
      });
    }, POLL_MS);
    return () => window.clearInterval(timer);
  }, [connected, refresh]);

  // SKU 输入防抖联想。
  useEffect(() => {
    if (!connected) return;
    const timer = window.setTimeout(() => {
      loadProducts(productQuery).catch(() => undefined);
    }, 300);
    return () => window.clearTimeout(timer);
  }, [connected, productQuery, loadProducts]);

  useEffect(() => {
    if (!connected || !form.source.trim()) {
      setSourceProduct(null);
      return;
    }
    const source = form.source.trim();
    const special = policy?.specialMappings.find((item) => item.sourceSku === source);
    if (special) {
      setSourceProduct({ sku: source, styleCode: special.sourceStyle, name: special.name });
      loadProducts(special.targetStyle).catch(() => undefined);
      return;
    }
    const timer = window.setTimeout(async () => {
      try {
        const params = new URLSearchParams({ limit: "20", q: source });
        const found = await exchangeApi.get<{ products: ExchangeProduct[] }>(`/api/exchange/products?${params}`, credentialsRef.current);
        const exact = found.products.find((item) => item.sku === source) ?? null;
        setSourceProduct(exact);
        if (exact?.styleCode) await loadProducts(exact.styleCode);
      } catch { setSourceProduct(null); }
    }, 250);
    return () => window.clearTimeout(timer);
  }, [connected, form.source, loadProducts, policy]);

  useEffect(() => {
    if (!connected) return;
    const timer = window.setTimeout(() => {
      loadOrders(orderQuery).catch(() => undefined);
    }, 300);
    return () => window.clearTimeout(timer);
  }, [connected, loadOrders, orderQuery]);

  const effectiveOids = [...new Set([...selectedOids, ...parseOids(form.oids)])];
  useEffect(() => {
    if (!connected) return;
    if (!effectiveOids.length) {
      setOrderItems(null);
      return;
    }
    const timer = window.setTimeout(() => loadOrderItems(effectiveOids).catch(() => undefined), 250);
    return () => window.clearTimeout(timer);
  }, [connected, effectiveOids.join("|"), loadOrderItems]);

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    setMessage("");
    const source = form.source.trim();
    const target = form.target.trim();
    const oids = [...new Set([...selectedOids, ...parseOids(form.oids)])];
    try {
      if (source === target) throw new Error("源 SKU 与目标 SKU 不能相同");
      if (!oids.length) throw new Error("必须填写明确的内部订单号 o_id");
      setSubmitting(true);
      const resolveProduct = async (sku: string) => {
        const params = new URLSearchParams({ limit: "20", q: sku });
        const found = await exchangeApi.get<{ products: ExchangeProduct[] }>(
          `/api/exchange/products?${params}`, credentials,
        );
        return found.products.find((item) => item.sku === sku);
      };
      const special = policy?.specialMappings.find((item) => item.sourceSku === source);
      const [resolvedSource, targetProduct] = await Promise.all([
        special ? Promise.resolve({ sku: source, styleCode: special.sourceStyle, name: special.name }) : resolveProduct(source),
        resolveProduct(target),
      ]);
      if (!resolvedSource || !targetProduct) {
        throw new Error("源商品和目标商品必须从候选中选择并解析成明确 SKU；可用 SKU、款式编码、名称或规格搜索。");
      }
      if (special && !special.targetSkus.includes(target)) {
        throw new Error(`${source} 只能更换为维护的 ${special.targetSkus.length} 个目标 SKU。`);
      }
      if (!special && resolvedSource.styleCode !== targetProduct.styleCode) {
        throw new Error(`其他商品只能在同一款式内换货：${resolvedSource.styleCode || "未知"} ≠ ${targetProduct.styleCode || "未知"}`);
      }
      await exchangeApi.post(
        "/api/exchange/jobs",
        {
          rules: {
            strategy: "direct",
            replacements: [{
              from: source,
              to: target,
              sourceStyle: resolvedSource.styleCode ?? "",
              targetStyle: targetProduct.styleCode ?? "",
            }],
          },
          targets: { o_ids: oids, limit: 500 },
          operator: credentials.operator.trim(),
        },
        credentials,
        { headers: { "Idempotency-Key": newId() } },
      );
      setForm((current) => ({ ...current, oids: "" }));
      setSelectedOids([]);
      await refresh();
    } catch (error) {
      setMessage(errorText(error));
    } finally {
      setSubmitting(false);
    }
  }

  async function act(jobId: string, action: "confirm" | "cancel") {
    if (
      action === "cancel" &&
      !window.confirm("取消该换货任务？未执行的试算和确认都会作废。")
    ) {
      return;
    }
    if (
      action === "confirm" &&
      !window.confirm("确认按当前 dry-run 清单执行真实换货？该操作会修改 ERP 订单，且不会自动重试。")
    ) {
      return;
    }
    try {
      await exchangeApi.post(`/api/exchange/jobs/${jobId}/${action}`, { operator: credentials.operator.trim() }, credentials);
      setOpenId(jobId);
      await refresh();
    } catch (error) {
      setMessage(errorText(error));
    }
  }

  const online = (status?.onlineWorkers ?? 0) > 0;

  return (
    <>
      <TopBar
        title="订单 SKU 换货"
        sub={connected && !online ? "ERP Worker 离线，任务不会执行" : "dry-run 试算，人工确认后才动 ERP"}
      />
      <main className="exchange-layout">
        {connected && pollStale ? (
          <div className="notice exchange-stale" role="status">
            状态可能过时，轮询失败。页面仍显示上一次结果，请检查网络后等待自动刷新。
          </div>
        ) : null}
        {connected && !online ? (
          <div className="notice exchange-offline" role="alert">
            ERP Worker 离线，换货任务不会执行。请打开已登录聚水潭的浏览器油猴脚本。
          </div>
        ) : null}
        <section className="panel">
          <div className="panel-head">
            <strong>新建换货任务</strong>
          </div>
          <p className="small" style={{ margin: "6px 0 16px" }}>
            页面只提交规则和明确订单号。ERP Worker 会先读取真实订单并生成 dry-run，不会立即换货。
          </p>

          <div className="credentials">
            <div className="credentials-grid">
              <input
                type="password"
                autoComplete="off"
                placeholder="EXCHANGE_API_TOKEN"
                value={credentials.token}
                onChange={(event) => update({ token: event.target.value })}
              />
              <input
                autoComplete="off"
                placeholder="操作人姓名"
                value={credentials.operator}
                onChange={(event) => update({ operator: event.target.value })}
              />
              <button
                type="button"
                className="btn"
                onClick={() => {
                  setMessage("");
                  connect().catch((error: unknown) => setMessage(errorText(error)));
                }}
              >
                连接
              </button>
            </div>
            <div className="small" style={{ marginTop: 7 }}>
              Token 仅保存在当前标签页 sessionStorage，不写入页面或本地文件。
            </div>
          </div>

          <div className="statusline" style={{ margin: "14px 0" }}>
            <span className={`dot ${online ? "online" : "offline"}`} />
            <span className="small">
              {!connected
                ? "尚未连接"
                : online
                  ? `${status?.onlineWorkers} 个 ERP Worker 在线`
                  : "ERP Worker 离线，任务不会执行"}
            </span>
          </div>

          <form onSubmit={submit}>
            <div className="notice exchange-policy">
              <b>系统强制规则</b>
              {policy ? (
                <>
                  {policy.specialMappings.map((item) => (
                    <div key={item.sourceSku}>
                      源 SKU 为 {item.sourceSku} 时，只能选择已维护的 {item.targetSkus.length} 个目标 SKU
                      {item.name ? `（${item.name}）` : ""}。
                    </div>
                  ))}
                  <div>
                    {policy.specialMappings.length
                      ? "其他商品只能在相同款式编码内换货。"
                      : "所有商品只能在相同款式编码内换货。"}
                  </div>
                </>
              ) : (
                <div>连接后读取换货规则。未连接时仍按同款式限制提交，服务端会再校验一遍。</div>
              )}
            </div>
            <div className="field order-picker">
              <label htmlFor="order-search">第 1 步：从订单库选择订单</label>
              <input
                id="order-search"
                placeholder="搜索内部订单号、平台单号、店铺或买家"
                value={orderQuery}
                disabled={!connected || !orderSearch?.configured}
                onChange={(event) => setOrderQuery(event.target.value)}
              />
              {orderSearch?.configured ? (
                <>
                  <div className="small order-source-note">
                    已选 {selectedOids.length} 单
                  </div>
                  <div className="order-results">
                    {orderSearch.orders.length ? orderSearch.orders.map((order) => (
                      <label key={order.oId}>
                        <input type="checkbox" checked={selectedOids.includes(order.oId)} onChange={(event) => {
                          setSelectedOids((current) => event.target.checked
                            ? [...new Set([...current, order.oId])]
                            : current.filter((value) => value !== order.oId));
                        }} />
                        <span>
                          <b className="mono">{order.oId}</b>
                          <small>{[order.platformOrderNo, order.shopName, order.buyer, order.status, order.orderDate].filter(Boolean).join(" · ")}</small>
                        </span>
                      </label>
                    )) : <div className="empty">没有匹配订单</div>}
                  </div>
                </>
              ) : (
                <div className="notice order-source-unavailable">
                  <div>
                    {orderLoading
                      ? "正在检查订单数据源…"
                      : orderError
                        ? `订单数据源加载失败：${orderError}`
                        : orderSearch?.message ?? (connected ? "订单数据源暂不可用。" : "请先填写凭证并点击连接。")}
                  </div>
                  {!orderLoading && filled ? (
                    <button
                      type="button"
                      className="btn order-source-retry"
                      onClick={() => loadOrders(orderQuery).then(() => setConnected(true)).catch(() => undefined)}
                    >
                      重试订单数据源
                    </button>
                  ) : null}
                </div>
              )}
            </div>
            <div className="field">
              <label htmlFor="oids">手工输入 o_id（数据源未接入时的测试入口）</label>
              <textarea
                id="oids"
                rows={6}
                placeholder={"订单库接入前可在这里填写；每行一个，也支持逗号分隔\n10012345\n10012346"}
                value={form.oids}
                onChange={(event) => setForm((current) => ({ ...current, oids: event.target.value }))}
              />
            </div>
            <div className="field order-item-picker">
              <label htmlFor="source">第 2 步：从所选订单中选择源商品</label>
              {orderSearch?.configured ? (
                orderItems?.configured ? (
                  <select id="source" required value={form.source} onChange={(event) => {
                    const item = orderItems.items.find((row) => row.sku === event.target.value) ?? null;
                    setSourceProduct(item);
                    setForm((current) => ({ ...current, source: event.target.value, target: "" }));
                  }}>
                    <option value="">请选择订单中的 SKU</option>
                    {orderItems.items.map((item) => (
                      <option key={item.sku} value={item.sku}>
                        {`${item.sku} · ${item.name} ${item.properties || ""} · 覆盖 ${item.orderCount}/${orderItems.selectedOrderCount} 单`}
                      </option>
                    ))}
                  </select>
                ) : <div className="notice">{orderItems?.message ?? "请先选择订单。"}</div>
              ) : (
                <input id="source" list="exchange-products" required
                  placeholder="订单库未接入：测试时手工选择源 SKU"
                  value={form.source} onChange={(event) => {
                    setProductQuery(event.target.value);
                    setForm((current) => ({ ...current, source: event.target.value, target: "" }));
                  }} />
              )}
            </div>
            <div className="field" style={{ margin: "13px 0" }}>
              <label htmlFor="target">第 3 步：选择目标 SKU</label>
              <input id="target" list="exchange-products" required disabled={!form.source.trim()}
                placeholder="系统只显示符合业务规则的目标 SKU"
                value={form.target} onChange={(event) => {
                  setProductQuery(event.target.value);
                  setForm((current) => ({ ...current, target: event.target.value }));
                }} />
            </div>
            <datalist id="exchange-products">
              {products.filter((product) => {
                const special = policy?.specialMappings.find((item) => item.sourceSku === form.source.trim());
                if (special) return special.targetSkus.includes(product.sku);
                return !sourceProduct?.styleCode || product.styleCode === sourceProduct.styleCode;
              }).map((product) => (
                <option key={product.sku} value={product.sku}>
                  {`${product.styleCode ? `[${product.styleCode}] ` : ""}${product.name} ${product.properties ?? ""}`.trim()}
                </option>
              ))}
            </datalist>
            <div className="notice">
              ERP 最终按 SKU 执行。系统会在页面、服务端和 dry-run 三层校验上述规则；必须核对商品和数量后再确认。
            </div>
            <button type="submit" className="btn primary" disabled={submitting || !connected}>
              创建 dry-run 任务
            </button>
            {message ? (
              <div className="status error" role="alert">
                {message}
              </div>
            ) : null}
          </form>
        </section>

        <section className="panel">
          <div className="panel-head">
            <strong>任务与预演</strong>
            <small>页面每 3 秒刷新</small>
          </div>
          <p className="small" style={{ margin: "6px 0 16px" }}>
            ERP 标签页离线时，试算任务约 5 分钟后会退回队列重试；已开始改 ERP 的任务超时后标为中断，不会自动重投。
          </p>
          <div className="jobs">
            {!connected ? (
              <div className="empty">输入 Token 后查看任务</div>
            ) : jobs.length === 0 ? (
              <div className="empty">还没有换货任务</div>
            ) : (
              jobs.map((job) => (
                <JobCard
                  key={job.id}
                  job={job}
                  open={openId === job.id}
                  onToggle={() => setOpenId(openId === job.id ? "" : job.id)}
                  onAct={act}
                />
              ))
            )}
          </div>
        </section>
      </main>
    </>
  );
}
