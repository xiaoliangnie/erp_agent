import { useCallback, useEffect, useRef, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { TopBar } from "../../components/TopBar";
import { errorText, openBlob, publicApi } from "../../api/client";
import { DEFAULT_RATES, INVOICE_LABELS } from "./types";
import type { ContractItem, ContractOptions, InvoiceType, OrderChoice, ProductImageJob } from "./types";
import { GbPicker } from "./GbPicker";
import "./contract.css";

type StatusKind = "" | "ok" | "error";

const MANUAL_PAYMENT = "__manual__";

const isPoId = (value: string) => /^\d+$/.test(value);

function isAbortError(error: unknown): boolean {
  return error instanceof Error && error.name === "AbortError";
}

function parseFiniteNumber(value: string, label: string): number {
  const parsed = Number(value);
  if (!Number.isFinite(parsed)) throw new Error(`${label}不是有效数字。`);
  return parsed;
}

/** 备注首个数字优先，其次维护价，再才是 ERP 单价（票种匹配或内部户）。 */
function priceFor(
  item: ContractItem,
  mode: InvoiceType,
  erpPriceMode: InvoiceType | null,
  useErpPrice = false,
): string {
  if (item.remarkPrice != null) return String(item.remarkPrice);
  const configured = item.prices[mode];
  if (configured != null) return String(configured);
  return useErpPrice || erpPriceMode === mode ? String(item.erpPrice) : "";
}

function paymentPreviewText(
  order: ContractOptions,
  optionKey: string,
  manualText: string,
): string {
  const clause = optionKey === MANUAL_PAYMENT
    ? manualText.trim()
    : (order.paymentOptions ?? []).find((item) => item.key === optionKey)?.text ?? "";
  const lines = [clause, `采购单号${order.purchaseOrderNo}`].filter(Boolean);
  if (!order.supplierInternal) {
    const extras = [
      order.supplierBankAccountName ? `付款账户名：${order.supplierBankAccountName}` : "",
      order.supplierBankName ? `开户行：${order.supplierBankName}` : "",
      order.supplierBankAccount ? `账户：${order.supplierBankAccount}` : "",
    ].filter(Boolean);
    if (extras.length) lines.push(extras.join("    "));
  }
  return lines.join("\n");
}

export default function ContractPage() {
  const [params] = useSearchParams();
  const [query, setQuery] = useState(() => {
    const initial = params.get("po_id") ?? "";
    return isPoId(initial) ? initial : "";
  });
  const [choices, setChoices] = useState<OrderChoice[]>([]);
  const [order, setOrder] = useState<ContractOptions | null>(null);
  const [invoiceType, setInvoiceType] = useState<InvoiceType>("special_invoice");
  const [taxRate, setTaxRate] = useState("13");
  const [prices, setPrices] = useState<Record<string, string>>({});
  const [paymentOption, setPaymentOption] = useState("");
  const [paymentText, setPaymentText] = useState("");
  const [receivingInfo, setReceivingInfo] = useState("");
  const [inspectionExtra, setInspectionExtra] = useState("");
  const [gbSelections, setGbSelections] = useState<Record<string, string>>({});
  const [status, setStatus] = useState<{ text: string; kind: StatusKind }>({ text: "", kind: "" });
  const [previewUrl, setPreviewUrl] = useState("");
  const [busy, setBusy] = useState<"" | "preview" | "generate">("");
  const [imageJob, setImageJob] = useState<ProductImageJob | null>(null);
  const previewRef = useRef<HTMLDivElement>(null);
  const previewUrlRef = useRef("");
  previewUrlRef.current = previewUrl;

  const say = useCallback((text: string, kind: StatusKind = "") => setStatus({ text, kind }), []);

  // 票种走 ref：换票种不该让载单函数换身份，否则下面的防抖 effect 会跟着重跑。
  const invoiceTypeRef = useRef(invoiceType);
  invoiceTypeRef.current = invoiceType;

  useEffect(() => () => {
    if (previewUrlRef.current) URL.revokeObjectURL(previewUrlRef.current);
  }, []);

  /** 任何影响合同内容的改动都让预览失效：下载必须基于已核对过的那一版。 */
  const invalidatePreview = useCallback(() => {
    setPreviewUrl((current) => {
      if (current) {
        URL.revokeObjectURL(current);
        say("合同参数已修改，请重新生成预览。");
      }
      return "";
    });
  }, [say]);

  const applyPrices = useCallback((next: ContractOptions, mode: InvoiceType) => {
    const filled: Record<string, string> = {};
    for (const item of next.items) {
      filled[item.sku] = priceFor(item, mode, next.erpPriceMode, next.useErpPrice);
    }
    setPrices(filled);
  }, []);

  const applyGb = useCallback((next: ContractOptions) => {
    const filled: Record<string, string> = {};
    for (const item of next.items) filled[item.poiId] = item.gbStandard ?? "";
    setGbSelections(filled);
  }, []);

  const loadOrder = useCallback(
    async (poId: string, signal?: AbortSignal) => {
      invalidatePreview();
      if (!poId) {
        setOrder(null);
        setGbSelections({});
        return;
      }
      say("正在读取实时采购信息…");
      try {
        const data = await publicApi.get<ContractOptions>(
          `/api/contracts/options?po_id=${encodeURIComponent(poId)}`,
          { signal },
        );
        setOrder(data);
        const mode = data.erpPriceMode && data.erpPriceMode in INVOICE_LABELS
          ? data.erpPriceMode
          : invoiceTypeRef.current;
        if (mode !== invoiceTypeRef.current) setInvoiceType(mode);
        setTaxRate(String(data.invoiceRates[mode] ?? DEFAULT_RATES[mode]));
        // 有预选就用预选；内部户没预选时退回手输并带出「内部往来」。
        const internalManual = data.supplierInternal && !data.paymentOption;
        setPaymentOption(internalManual ? MANUAL_PAYMENT : data.paymentOption);
        setPaymentText(internalManual ? data.paymentMethod : "");
        setReceivingInfo(data.receivingInfo || "");
        setInspectionExtra("");
        applyPrices(data, mode);
        applyGb(data);
        const missingUnits = data.items.filter((item) => !item.unit).map((item) => item.sku);
        if (!data.supplierMapped) {
          say(data.supplierIssue || "供应商简称尚未维护完整映射，不能生成合同。", "error");
        } else if (missingUnits.length) {
          say(`商品 ${missingUnits.join("、")} 在商品资料里单位为空，请先在聚水潭补单位。`, "error");
        } else {
          say(
            data.supplierInternal
              ? "内部往来户，合同不列收付款信息。请确认票种、税率和单价后预览。"
              : "采购信息已读取，请确认付款方式、票种、税率和各商品单价，然后预览。",
            "ok",
          );
        }
      } catch (error) {
        if (isAbortError(error)) return;
        setOrder(null);
        setGbSelections({});
        say(errorText(error), "error");
      }
    },
    [applyGb, applyPrices, invalidatePreview, say],
  );

  const loadChoices = useCallback(
    async (search: string, signal?: AbortSignal) => {
      try {
        const data = await publicApi.get<{ orders: OrderChoice[] }>(
          `/api/contracts/orders?q=${encodeURIComponent(search)}`,
          { signal },
        );
        setChoices(data.orders);
        if (!search) say("可直接选择最近采购单，也可输入单号、供应商或采购员搜索。");
      } catch (error) {
        if (isAbortError(error)) return;
        say(errorText(error), "error");
      }
    },
    [say],
  );

  /*
   * 搜索防抖：输入停下来就刷新候选单，输入的是纯数字单号时顺带把该单载进来。
   * 从下拉里选一条也走这条路（datalist 选中会触发 input），所以不用等失焦；
   * 首次带 ?po_id= 进来同样命中，不需要另写引导逻辑。
   * 后发先至的响应靠 AbortController 丢弃，避免旧采购单盖住新选择。
   */
  const loadedPoId = useRef("");
  useEffect(() => {
    const search = query.trim();
    const controller = new AbortController();
    const timer = window.setTimeout(() => {
      void loadChoices(search, controller.signal);
      if (isPoId(search) && loadedPoId.current !== search) {
        loadedPoId.current = search;
        void loadOrder(search, controller.signal);
      }
    }, 220);
    return () => {
      window.clearTimeout(timer);
      controller.abort();
    };
  }, [loadChoices, loadOrder, query]);

  function changeInvoiceType(next: InvoiceType) {
    setInvoiceType(next);
    setTaxRate(String(DEFAULT_RATES[next]));
    if (order) applyPrices(order, next);
    invalidatePreview();
  }

  /** 单价和税率必须全部填齐，缺一个就不发请求 —— 后端也会拒。 */
  function requestBody() {
    if (!order) throw new Error("请先选择采购单。");
    const priceOverrides: Record<string, number> = {};
    for (const item of order.items) {
      const value = prices[item.sku];
      if (value === "" || value == null) throw new Error("请填写所有商品的合同单价。");
      priceOverrides[item.sku] = parseFiniteNumber(value, `商品 ${item.sku} 的合同单价`);
    }
    if (taxRate === "") throw new Error("请填写所选票种的税率。");
    const manual = paymentOption === MANUAL_PAYMENT;
    if (!paymentOption) throw new Error("请选择付款方式。");
    if (manual && !paymentText.trim()) throw new Error("请填写付款方式条款。");
    const gbOverrides: Record<string, string> = {};
    for (const item of order.items) {
      gbOverrides[item.poiId] = gbSelections[item.poiId] ?? "";
    }
    return {
      poId: order.purchaseOrderNo,
      invoiceType,
      taxRate: parseFiniteNumber(taxRate, "税率"),
      paymentOption: manual ? "" : paymentOption,
      paymentText: manual ? paymentText.trim() : "",
      receivingInfo: receivingInfo.trim(),
      inspectionExtra: inspectionExtra.trim(),
      priceOverrides,
      gbOverrides,
    };
  }

  async function makePreview() {
    let body;
    try {
      body = requestBody();
    } catch (error) {
      say(errorText(error), "error");
      return;
    }
    setBusy("preview");
    say("正在生成真实合同预览…");
    try {
      const response = await fetch("/api/contracts/preview", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      if (!response.ok) {
        const data = (await response.json()) as { error?: string };
        throw new Error(data.error ?? "预览失败");
      }
      if (previewUrl) URL.revokeObjectURL(previewUrl);
      setPreviewUrl(URL.createObjectURL(await response.blob()));
      say("预览已生成，请核对后下载 Excel。", "ok");
      window.setTimeout(() => previewRef.current?.scrollIntoView({ behavior: "smooth", block: "start" }), 0);
    } catch (error) {
      say(errorText(error), "error");
    } finally {
      setBusy("");
    }
  }

  async function download() {
    let body;
    try {
      body = requestBody();
    } catch (error) {
      say(errorText(error), "error");
      return;
    }
    setBusy("generate");
    say("正在生成采购合同 Excel…");
    try {
      const response = await fetch("/api/contracts/generate", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      if (!response.ok) {
        const data = (await response.json()) as { error?: string };
        throw new Error(data.error ?? "生成失败");
      }
      openBlob(await response.blob(), `采购合同-${order?.purchaseOrderNo}-${INVOICE_LABELS[invoiceType]}.xlsx`);
      say("采购合同已生成并开始下载。", "ok");
    } catch (error) {
      say(errorText(error), "error");
    } finally {
      setBusy("");
    }
  }

  const paymentReady = paymentOption !== "" && (paymentOption !== MANUAL_PAYMENT || paymentText.trim() !== "");
  const missingUnits = order?.items.filter((item) => !item.unit).map((item) => item.sku) ?? [];
  const canPreview = Boolean(order?.supplierMapped) && paymentReady && missingUnits.length === 0 && busy === "";
  const missingImages = order?.items.filter((item) => !item.hasImage).length ?? 0;

  async function syncImages() {
    if (!order || !missingImages) return;
    say("已创建 ERP 商品图片同步任务，请保持已登录的聚水潭订单页和 Worker 在线。", "");
    try {
      const job = await publicApi.post<ProductImageJob>("/api/contracts/images/sync", {
        poId: order.purchaseOrderNo,
      });
      setImageJob(job);
    } catch (error) {
      say(errorText(error), "error");
    }
  }

  useEffect(() => {
    if (!imageJob || !["pending", "syncing"].includes(imageJob.status)) return;
    const timer = window.setInterval(() => {
      publicApi.get<ProductImageJob>(`/api/contracts/images/jobs/${imageJob.id}`)
        .then((next) => {
          setImageJob(next);
          if (next.status === "done" && order) {
            say("ERP 商品图片已同步，正在重新读取采购单。", "ok");
            void loadOrder(order.purchaseOrderNo);
          } else if (next.status === "failed") {
            say(next.error || "部分或全部商品在 ERP 接口中没有图片。", "error");
            if (order) void loadOrder(order.purchaseOrderNo);
          }
        })
        .catch((error: unknown) => say(errorText(error), "error"));
    }, 2000);
    return () => window.clearInterval(timer);
  }, [imageJob, loadOrder, order, say]);
  const metaFields: [string, string | number][] = order
    ? [
        ["采购单号", order.purchaseOrderNo],
        ["下单日期", order.orderDate],
        ["交货日期", order.deliveryDate],
        ["采购员", order.purchaser],
        ["供应商简称", order.supplierShortName],
        ["供方完整名称", order.supplierInternal ? `${order.supplierLegalName || order.supplierShortName}（内部）` : (order.supplierLegalName || "未维护")],
        ["主数据票种", order.supplierInternal ? "内部往来，不列收付款" : (order.supplierInvoiceLabel || "未填写")],
        ["采购数量", order.totalQuantity],
        ["仓库 / 收货信息", order.warehouse || order.receiveAddress],
      ]
    : [];

  return (
    <>
      <TopBar title="采购合同生成" sub="选择实时采购单，预览确认后下载 Excel" />
      <main className="contract-layout">
        <section className="panel">
          <div className="contract-form">
            <div className="field">
              <label htmlFor="po-id">采购单号</label>
              <input
                id="po-id"
                type="search"
                list="po-list"
                autoComplete="off"
                placeholder="搜索采购单号、供应商或采购员"
                value={query}
                onChange={(event) => {
                  const next = event.target.value;
                  setQuery(next);
                  if (!isPoId(next.trim())) {
                    loadedPoId.current = "";
                    setOrder(null);
                    setGbSelections({});
                    setReceivingInfo("");
                    setInspectionExtra("");
                    invalidatePreview();
                  }
                }}
              />
              <datalist id="po-list">
                {choices.map((choice) => (
                  <option
                    key={choice.purchaseOrderNo}
                    value={choice.purchaseOrderNo}
                    label={`${choice.orderDate} · ${choice.supplier || "未知供应商"}${choice.purchaser ? ` · ${choice.purchaser}` : ""}`}
                  />
                ))}
              </datalist>
            </div>
            <div className="field">
              <label htmlFor="invoice-type">价格 / 票种</label>
              <select
                id="invoice-type"
                value={invoiceType}
                onChange={(event) => changeInvoiceType(event.target.value as InvoiceType)}
              >
                <option value="no_invoice">不开票（默认 0%）</option>
                <option value="normal_invoice">普票（默认 0%）</option>
                <option value="special_invoice">专票（默认 13%）</option>
              </select>
            </div>
            <div className="field">
              <label htmlFor="tax-rate">税率（%，可修改）</label>
              <input
                id="tax-rate"
                type="number"
                min="0"
                max="100"
                step="0.01"
                value={taxRate}
                onChange={(event) => {
                  setTaxRate(event.target.value);
                  invalidatePreview();
                }}
              />
            </div>
            <div className="field payment-field">
              <label htmlFor="payment-option">付款方式</label>
              <select
                id="payment-option"
                value={paymentOption}
                onChange={(event) => {
                  setPaymentOption(event.target.value);
                  invalidatePreview();
                }}
              >
                <option value="">请选择付款方式</option>
                {(order?.paymentOptions ?? []).map((option) => (
                  <option key={option.key} value={option.key}>{option.label}</option>
                ))}
                <option value={MANUAL_PAYMENT}>手动输入</option>
              </select>
            </div>
            <button type="button" className="btn primary" disabled={!canPreview} onClick={() => void makePreview()}>
              {busy === "preview" ? "生成中…" : "预览合同"}
            </button>
            <button type="button" className="btn" disabled={!previewUrl || busy !== ""} onClick={() => void download()}>
              下载 Excel
            </button>
          </div>
          {order && paymentOption === MANUAL_PAYMENT ? (
            <div className="payment-manual">
              <label htmlFor="payment-text">付款方式条款（原样写入合同）</label>
              <textarea
                id="payment-text"
                rows={2}
                placeholder="例如：合同签订后支付30%预付款，余款验收合格后结清"
                value={paymentText}
                onChange={(event) => {
                  setPaymentText(event.target.value);
                  invalidatePreview();
                }}
              />
            </div>
          ) : null}
          {order && paymentOption ? (
            <div className="payment-preview">
              写入合同：
              <pre>{paymentPreviewText(order, paymentOption, paymentText)}</pre>
              {order.paymentNote && paymentOption === order.paymentOption ? (
                <em className="hint"> · {order.paymentNote}</em>
              ) : null}
            </div>
          ) : null}
          {order ? (
            <div className="contract-extras">
              <div className="payment-manual">
                <label htmlFor="receiving-info">收货信息（写入表头 B6:H7 与送货地址行，可改）</label>
                <textarea
                  id="receiving-info"
                  rows={3}
                  value={receivingInfo}
                  onChange={(event) => {
                    setReceivingInfo(event.target.value);
                    invalidatePreview();
                  }}
                />
              </div>
              <div className="payment-manual">
                <label htmlFor="inspection-extra">检验标准加行（空则只用默认两条；有内容从 3 起编号）</label>
                <textarea
                  id="inspection-extra"
                  rows={3}
                  placeholder={"例如：外箱完好\n吊牌齐全"}
                  value={inspectionExtra}
                  onChange={(event) => {
                    setInspectionExtra(event.target.value);
                    invalidatePreview();
                  }}
                />
                <div className="inspection-default">
                  默认：{(order.inspectionStandards || "").split("\n").filter(Boolean).join("；")}
                </div>
              </div>
            </div>
          ) : null}
          <div className={`status ${status.kind}`} role="status">
            {status.text}
          </div>
          {order && missingImages ? (
            <div className="notice">
              当前有 {missingImages} 个 SKU 缺少图片。供应链 API 未返回图片时，可通过已登录聚水潭页面的
              采购明细接口同步 `pic300 / pic160 / pic100` 到本地缓存。
              <div className="notice-actions">
                <button
                  type="button"
                  className="btn"
                  disabled={Boolean(imageJob && ["pending", "syncing"].includes(imageJob.status))}
                  onClick={() => void syncImages()}
                >
                  {imageJob && ["pending", "syncing"].includes(imageJob.status) ? "图片同步中…" : "从 ERP 同步图片"}
                </button>
              </div>
            </div>
          ) : null}
          {order ? (
            <div className="contract-meta">
              {metaFields.map(([label, value]) => (
                <div key={label}>
                  <span>{label}</span>
                  <b>{value || "—"}</b>
                </div>
              ))}
            </div>
          ) : null}
        </section>

        <section className="panel">
          <div className="panel-head">
            <strong>商品、执行标准与合同单价</strong>
            <small>国标码是商品条码，与执行标准不是同一列</small>
          </div>
          {!order ? (
            <div className="empty">先选择采购单</div>
          ) : (
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>SKU / 款号</th>
                    <th>国标码</th>
                    <th>商品 / 规格</th>
                    <th>执行标准</th>
                    <th>分类</th>
                    <th>单位</th>
                    <th>交期</th>
                    <th className="n">采购数</th>
                    <th className="n">已入库</th>
                    <th>图片</th>
                    <th className="n">合同单价</th>
                  </tr>
                </thead>
                <tbody>
                  {order.items.map((item) => {
                    const selected = gbSelections[item.poiId] ?? "";
                    const lastBuy = (item.priceHistory ?? [])[0];
                    return (
                    <tr key={item.poiId || item.sku}>
                      <td>
                        {item.sku + (item.styleCode ? ` / ${item.styleCode}` : "")}
                      </td>
                      <td>{item.nationalCode || "—"}</td>
                      <td>
                        {item.name + (item.specification ? ` / ${item.specification}` : "")}
                        {item.remark ? <div className="sku-extra">备注 {item.remark}</div> : null}
                      </td>
                      <td>
                        <GbPicker
                          item={item}
                          selected={selected}
                          onSelect={(picked) => {
                            setGbSelections((currentValue) => ({
                              ...currentValue,
                              [item.poiId]: picked,
                            }));
                            invalidatePreview();
                          }}
                          onError={(text) => say(text, "error")}
                        />
                      </td>
                      <td>{item.category || "—"}</td>
                      <td className={item.unit ? "" : "unit-missing"}>{item.unit || "未维护"}</td>
                      <td>{item.deliveryDate || "—"}</td>
                      <td className="n">{item.quantity}</td>
                      <td className="n">{item.inQuantity}</td>
                      <td title={item.imageError || undefined}>
                        {item.hasImage ? `可用 · ${item.imageSource}` : "待从 ERP 同步"}
                      </td>
                      <td className="n">
                        <input
                          type="number"
                          step="0.01"
                          min="0"
                          className="price-input"
                          value={prices[item.sku] ?? ""}
                          onChange={(event) => {
                            setPrices((current) => ({ ...current, [item.sku]: event.target.value }));
                            invalidatePreview();
                          }}
                        />
                        {item.remarkPrice != null ? (
                          <div className="price-history">备注解析 {item.remarkPrice}</div>
                        ) : null}
                        {lastBuy ? (
                          <div className="price-history">
                            <span title={`${item.priceHistory.length} 次历史采购`}>
                              上次 {lastBuy.price}（{lastBuy.date} · {lastBuy.poId}）
                            </span>
                            <button
                              type="button"
                              className="link-btn"
                              disabled={prices[item.sku] === String(lastBuy.price)}
                              onClick={() => {
                                setPrices((current) => ({ ...current, [item.sku]: String(lastBuy.price) }));
                                invalidatePreview();
                              }}
                            >
                              采用
                            </button>
                          </div>
                        ) : (
                          <div className="price-history muted">无历史采购</div>
                        )}
                      </td>
                    </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          )}
        </section>

        {previewUrl ? (
          <section className="panel" ref={previewRef}>
            <div className="panel-head">
              <strong>合同预览</strong>
              <small>预览确认后才能下载</small>
            </div>
            <div className="preview-frame">
              <img src={previewUrl} alt="采购合同预览" />
            </div>
          </section>
        ) : null}
      </main>
    </>
  );
}
