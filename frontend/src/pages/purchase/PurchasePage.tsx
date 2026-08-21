import { useCallback, useEffect, useMemo, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { DateField, datePart } from "../../components/DateField";
import { TopBar } from "../../components/TopBar";
import { errorText, publicApi } from "../../api/client";
import { useServerClock } from "../../hooks/useServerClock";
import { ROUTES } from "../../routes";
import "./purchase.css";

interface DraftLine {
  styleId: string;
  sku: string;
  name: string;
  spec: string;
  qty: number;
  price: number | null;
  supplier?: string;
  supplierId?: string;
  remark: string;
  orderQty?: number | null;
  lastQty?: number | null;
}

interface DraftHeader {
  seller: string;
  sellerId: string;
  purchaserName: string;
  paymentMethod: string;
  wmsCoId: string;
  wmsCoName: string;
  poDate: string;
  arriveDate: string;
  taxRate: string | number;
  remark: string;
  invoiceType: string;
}

interface PurchaseDraft {
  id: string;
  board: string;
  createdAt: string;
  filename: string;
  lines: DraftLine[];
  notes: string[];
  header: DraftHeader;
  options: {
    warehouses: { id: string; name: string }[];
    payments: { id: string; name: string }[];
    suppliers?: {
      seller: string;
      sellerId: string;
      legalName: string;
      invoiceType: string;
      invoiceLabel: string;
      taxRate: string | number;
      invoiceRates?: Record<string, number>;
      settlement: string;
      paymentMethod?: string;
    }[];
    purchasers?: { id: string; name: string }[];
    taxRates?: number[];
    invoiceRates?: Record<string, number>;
  };
  supplierNote?: string;
  poId?: string;
  contract?: { ok: boolean; error?: string; page?: string; fileName?: string } | null;
  writesErp?: boolean;
}

const INVOICE_LABELS: Record<string, string> = {
  no_invoice: "不开票",
  normal_invoice: "普票",
  special_invoice: "专票",
};

const INVOICE_DEFAULT_RATES: Record<string, number> = {
  no_invoice: 0,
  normal_invoice: 0,
  special_invoice: 13,
};

function taxForInvoice(
  invoice: string,
  seller: string,
  draft: PurchaseDraft | null,
) {
  const chosen = (draft?.options.suppliers || []).find((item) => item.seller === seller);
  const fromSupplier = chosen?.invoiceRates?.[invoice];
  if (fromSupplier !== undefined && fromSupplier !== null) return fromSupplier;
  const fromDraft = draft?.options.invoiceRates?.[invoice];
  if (fromDraft !== undefined) return fromDraft;
  return INVOICE_DEFAULT_RATES[invoice] ?? 0;
}

function taxRateOptions(draft: PurchaseDraft | null, header: DraftHeader) {
  const rates = new Set<number>(draft?.options.taxRates || [0, 1, 3, 6, 9, 13]);
  if (header.taxRate !== "" && header.taxRate != null && !Number.isNaN(Number(header.taxRate))) {
    rates.add(Number(header.taxRate));
  }
  return [...rates].sort((left, right) => left - right);
}

function money(value: number | null | undefined) {
  if (value == null || Number.isNaN(Number(value))) return "";
  return Number(value).toFixed(2);
}

export default function PurchasePage() {
  const [params] = useSearchParams();
  const board = params.get("board") === "baihuo" ? "baihuo" : "apparel";
  const ids = useMemo(
    () => (params.get("ids") || "").split(",").map((item) => item.trim()).filter(Boolean),
    [params],
  );
  const [draft, setDraft] = useState<PurchaseDraft | null>(null);
  const [header, setHeader] = useState<DraftHeader | null>(null);
  const [lines, setLines] = useState<DraftLine[]>([]);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState<"load" | "save" | "submit" | "">("");
  const [confirming, setConfirming] = useState(false);
  const [result, setResult] = useState<PurchaseDraft | null>(null);
  const [poDateDirty, setPoDateDirty] = useState(false);
  const clock = useServerClock();

  useEffect(() => {
    if (!ids.length) {
      setError("从鞋服或自营百货勾选商品后再进来");
      return;
    }
    setBusy("load");
    setError("");
    publicApi.post<PurchaseDraft>("/api/purchase-drafts", { board, styleIds: ids })
      .then((payload) => {
        setDraft(payload);
        setHeader({ ...payload.header, poDate: datePart(payload.header.poDate) });
        setLines(payload.lines);
      })
      .catch((cause: unknown) => setError(errorText(cause)))
      .finally(() => setBusy(""));
  }, [board, ids]);

  useEffect(() => {
    if (poDateDirty || confirming || result || !clock.today) return;
    setHeader((current) => {
      if (!current || datePart(current.poDate) === clock.today) return current;
      return { ...current, poDate: clock.today };
    });
  }, [clock.today, confirming, poDateDirty, result]);

  const body = useMemo(() => ({ header, lines }), [header, lines]);

  const patchHeader = useCallback((key: keyof DraftHeader, value: string) => {
    setHeader((current) => current ? { ...current, [key]: value } : current);
  }, []);

  const patchLine = useCallback((index: number, key: keyof DraftLine, value: string) => {
    setLines((current) => current.map((line, i) => {
      if (i !== index) return line;
      if (key === "qty") return { ...line, qty: Number(value) || 0 };
      if (key === "price") return { ...line, price: value === "" ? null : Number(value) };
      return { ...line, [key]: value };
    }));
  }, []);

  const qtyTotal = lines.reduce((sum, line) => sum + (Number(line.qty) || 0), 0);
  const amountTotal = lines.reduce((sum, line) => sum + (Number(line.qty) || 0) * (Number(line.price) || 0), 0);
  const supplierNote = useMemo(() => {
    const chosen = (draft?.options.suppliers || []).find((item) => item.seller === header?.seller);
    if (chosen) {
      return [
        chosen.legalName || chosen.seller,
        chosen.invoiceLabel,
        chosen.sellerId ? `编码 ${chosen.sellerId}` : "",
      ].filter(Boolean).join(" · ");
    }
    return draft?.supplierNote || "";
  }, [draft, header?.seller]);

  async function save() {
    if (!draft || !header) return;
    setBusy("save");
    setError("");
    try {
      const payload = await publicApi.post<PurchaseDraft>(`/api/purchase-drafts/${draft.id}`, body);
      setDraft(payload);
      setHeader(payload.header);
      setLines(payload.lines);
    } catch (cause: unknown) {
      setError(errorText(cause));
    } finally {
      setBusy("");
    }
  }

  async function submit() {
    if (!draft || !header) return;
    setBusy("submit");
    setError("");
    try {
      const payload = await publicApi.post<PurchaseDraft>(`/api/purchase-drafts/${draft.id}/confirm`, body);
      setResult(payload);
      setConfirming(false);
    } catch (cause: unknown) {
      setError(errorText(cause));
    } finally {
      setBusy("");
    }
  }

  const title = board === "baihuo" ? "自营百货建采购单" : "鞋服建采购单";

  return (
    <>
      <TopBar
        title={title}
        sub={[
          clock.ready ? `现在 ${clock.now}` : "",
          "数量参考上次采购，空数量不写入",
          "采购日期选年月日，写入时带上当下时分秒",
        ].filter(Boolean).join(" · ")}
      />
      <main className="purchase-page">
        <p className="small purchase-lead">
          鞋服同款列出该款式编码下全部商品编码。数量、单价、备注参考上次采购，没填数量的行不写入采购单。
          供应商编号、票种、税率和合同一样走本机供应商管理表；采购员从历史采购署名里选。
          付款与合同同一套：3/7、发货前付款、到仓后付款、月度结算。确认后才写入 ERP，默认待审核。
        </p>
        {error ? <div className="purchase-error">{error}</div> : null}
        {busy === "load" ? <div className="empty">正在按勾选商品组草稿…</div> : null}
        {result ? (
          <section className="erp-card">
            <h2>已写入聚水潭</h2>
            <p>采购单号 <strong>{result.poId}</strong></p>
            {result.contract?.ok ? (
              <p>合同已生成：{result.contract.fileName}</p>
            ) : (
              <p>合同还没出：{result.contract?.error || "镜像可能还没刷到这张单"}</p>
            )}
            <p className="toolbar">
              {result.contract?.page ? <Link className="btn primary" to={result.contract.page}>去采购合同页</Link> : null}
              <Link className="btn" to={board === "baihuo" ? ROUTES.baihuo : ROUTES.spu}>回看板</Link>
            </p>
          </section>
        ) : null}
        {header && !result ? (
          <>
            <section className="erp-card">
              <div className="erp-section-title">采购单主信息</div>
              <div className="erp-grid">
                <label>采购单号<input value="自动生成" disabled /></label>
                <label className="must">供应商
                  <select
                    value={header.seller}
                    onChange={(event) => {
                      const seller = event.target.value;
                      const chosen = (draft?.options.suppliers || []).find((item) => item.seller === seller);
                      setHeader((current) => {
                        if (!current) return current;
                        if (!chosen) {
                          return { ...current, seller, sellerId: seller ? current.sellerId : "" };
                        }
                        return {
                          ...current,
                          seller: chosen.seller,
                          sellerId: chosen.sellerId || current.sellerId,
                          invoiceType: chosen.invoiceType || current.invoiceType,
                          taxRate: taxForInvoice(
                            chosen.invoiceType || current.invoiceType,
                            chosen.seller,
                            draft,
                          ),
                          paymentMethod: chosen.paymentMethod || current.paymentMethod,
                        };
                      });
                    }}
                  >
                    <option value="">请选择供应商</option>
                    {header.seller && !(draft?.options.suppliers || []).some((item) => item.seller === header.seller) ? (
                      <option value={header.seller}>{header.seller}（最近采购，主数据未维护）</option>
                    ) : null}
                    {(draft?.options.suppliers || []).map((item) => (
                      <option key={item.seller} value={item.seller}>
                        {item.seller}{item.sellerId ? ` · ${item.sellerId}` : ""}
                      </option>
                    ))}
                  </select>
                </label>
                <label className="must">供应商编号
                  <input value={header.sellerId} onChange={(event) => patchHeader("sellerId", event.target.value)} />
                </label>
                <label className="must">采购日期
                  <DateField
                    value={header.poDate}
                    today={clock.today}
                    onChange={(value) => {
                      setPoDateDirty(true);
                      patchHeader("poDate", value);
                    }}
                  />
                </label>
                <label className="must">采购员
                  <select value={header.purchaserName} onChange={(event) => patchHeader("purchaserName", event.target.value)}>
                    <option value="">请选择采购员</option>
                    {header.purchaserName && !(draft?.options.purchasers || []).some((item) => item.id === header.purchaserName) ? (
                      <option value={header.purchaserName}>{header.purchaserName}</option>
                    ) : null}
                    {(draft?.options.purchasers || []).map((item) => (
                      <option key={item.id} value={item.id}>{item.name}</option>
                    ))}
                  </select>
                </label>
                <label className="must">仓储方
                  <select
                    value={header.wmsCoId}
                    onChange={(event) => {
                      const warehouse = (draft?.options.warehouses || []).find((item) => item.id === event.target.value);
                      setHeader((current) => current ? {
                        ...current,
                        wmsCoId: event.target.value,
                        wmsCoName: warehouse?.name || current.wmsCoName,
                      } : current);
                    }}
                  >
                    {(draft?.options.warehouses || []).map((item) => (
                      <option key={item.id} value={item.id}>{item.name}</option>
                    ))}
                  </select>
                </label>
                <label className="must">付款方式
                  <select value={header.paymentMethod} onChange={(event) => patchHeader("paymentMethod", event.target.value)}>
                    <option value="">请选择</option>
                    {(draft?.options.payments || []).map((item) => (
                      <option key={item.id} value={item.id}>{item.name}</option>
                    ))}
                  </select>
                </label>
                <label>到货日期
                  <DateField
                    value={header.arriveDate}
                    today={clock.today}
                    onChange={(value) => patchHeader("arriveDate", value)}
                  />
                </label>
                <label>税率%
                  <select
                    value={header.taxRate === "" || header.taxRate == null ? "" : String(header.taxRate)}
                    onChange={(event) => patchHeader("taxRate", event.target.value)}
                  >
                    <option value="">请选择税率</option>
                    {taxRateOptions(draft, header).map((rate) => (
                      <option key={String(rate)} value={String(rate)}>
                        {rate}%{Number(rate) === Number(taxForInvoice(header.invoiceType, header.seller, draft)) ? "（票种默认）" : ""}
                      </option>
                    ))}
                  </select>
                </label>
                <label>备注
                  <input value={header.remark} onChange={(event) => patchHeader("remark", event.target.value)} />
                </label>
                <label>合同票种
                  <select
                    value={header.invoiceType}
                    onChange={(event) => {
                      const invoiceType = event.target.value;
                      setHeader((current) => current ? {
                        ...current,
                        invoiceType,
                        taxRate: taxForInvoice(invoiceType, current.seller, draft),
                      } : current);
                    }}
                  >
                    {Object.entries(INVOICE_LABELS).map(([id, name]) => (
                      <option key={id} value={id}>
                        {name}（默认 {draft?.options.invoiceRates?.[id] ?? INVOICE_DEFAULT_RATES[id]}%）
                      </option>
                    ))}
                  </select>
                </label>
              </div>
              {supplierNote ? <p className="small purchase-supplier-note">{supplierNote}</p> : null}
            </section>
            <section className="erp-card">
              <div className="erp-section-title">采购明细</div>
              <table className="erp-items">
                <thead>
                  <tr>
                    <th>图</th>
                    <th>名称 | 款式编码 | 商品编码 | 颜色规格</th>
                    <th>上次采购</th>
                    <th>数量</th>
                    <th>单价</th>
                    <th>金额</th>
                    <th>备注</th>
                  </tr>
                </thead>
                <tbody>
                  {lines.map((line, index) => (
                    <tr key={`${line.sku}-${index}`} className={Number(line.qty) > 0 ? "" : "is-zero"}>
                      <td className="erp-pic" />
                      <td>
                        <div>{line.name || "—"}</div>
                        <div className="small">{line.styleId} / {line.sku} / {line.spec || "—"}</div>
                      </td>
                      <td className="num">{line.lastQty ?? "—"}</td>
                      <td>
                        <input
                          className="erp-qty"
                          value={line.qty}
                          onChange={(event) => patchLine(index, "qty", event.target.value)}
                        />
                      </td>
                      <td>
                        <input
                          className="erp-qty"
                          value={line.price ?? ""}
                          onChange={(event) => patchLine(index, "price", event.target.value)}
                        />
                      </td>
                      <td className="num">{money((Number(line.qty) || 0) * (Number(line.price) || 0))}</td>
                      <td>
                        <input
                          value={line.remark}
                          onChange={(event) => patchLine(index, "remark", event.target.value)}
                        />
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
              <div className="erp-sum">
                <span>商品总数量: {qtyTotal}</span>
                <span>商品总金额: ￥{money(amountTotal)}</span>
              </div>
            </section>
            <div className="erp-bar">
              <Link className="btn" to={board === "baihuo" ? ROUTES.baihuo : ROUTES.spu}>关闭</Link>
              <button type="button" className="btn" disabled={busy !== ""} onClick={() => void save()}>保存草稿</button>
              <button type="button" className="btn primary" disabled={busy !== ""} onClick={() => setConfirming(true)}>保存</button>
            </div>
            {draft?.notes?.length ? (
              <ul className="small purchase-notes">{draft.notes.map((note) => <li key={note}>{note}</li>)}</ul>
            ) : null}
          </>
        ) : null}

        {confirming && header ? (
          <div className="erp-mask" role="dialog" aria-modal="true" aria-label="确认写入聚水潭">
            <div className="erp-dialog">
              <div className="erp-dialog-title">确认写入聚水潭采购单</div>
              <p className="small">下面按 ERP 手工下单页排。数量为空的码不写入。确认后调用页面同一套保存，默认<strong>待审核</strong>，不自动审核生效。</p>
              <div className="erp-preview">
                <div>供应商：{header.seller}（{header.sellerId}）</div>
                <div>采购员：{header.purchaserName}</div>
                <div>仓储方：{header.wmsCoName}</div>
                <div>付款方式：{(draft?.options.payments || []).find((item) => item.id === header.paymentMethod)?.name || header.paymentMethod}</div>
                <div>采购日期：{datePart(header.poDate)}（写入时带上当下时分秒）</div>
                <div>票种 / 税率：{INVOICE_LABELS[header.invoiceType] || header.invoiceType} · {header.taxRate === "" || header.taxRate == null ? "—" : `${header.taxRate}%`}</div>
                <div>备注：{header.remark || "—"}</div>
              </div>
              <table className="erp-items">
                <thead>
                  <tr>
                    <th>商品编码</th>
                    <th>数量</th>
                    <th>单价</th>
                    <th>金额</th>
                  </tr>
                </thead>
                <tbody>
                  {lines.filter((line) => Number(line.qty) > 0).map((line) => (
                    <tr key={line.sku}>
                      <td>{line.sku}<div className="small">{line.name}</div></td>
                      <td className="num">{line.qty}</td>
                      <td className="num">{money(line.price)}</td>
                      <td className="num">{money((Number(line.qty) || 0) * (Number(line.price) || 0))}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
              <div className="erp-bar">
                <button type="button" className="btn" disabled={busy === "submit"} onClick={() => setConfirming(false)}>关闭</button>
                <button type="button" className="btn primary" disabled={busy === "submit"} onClick={() => void submit()}>
                  {busy === "submit" ? "正在写入 ERP…" : "确认保存"}
                </button>
              </div>
            </div>
          </div>
        ) : null}
      </main>
    </>
  );
}
