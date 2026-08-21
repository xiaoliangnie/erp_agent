import { int } from "../../lib/format";

export interface PurchaseDraftLine {
  styleId: string;
  sku: string;
  name: string;
  spec: string;
  qty: number;
  price: number | null;
  supplier: string;
  remark: string;
  lastPoId?: string;
}

export interface PurchaseDraftGroup {
  supplier: string;
  lines: number;
  qty: number;
  missingPrice: number;
}

export interface PurchaseDraft {
  id: string;
  board: string;
  createdAt: string;
  filename: string;
  lines: PurchaseDraftLine[];
  groups: PurchaseDraftGroup[];
  stats: {
    lines: number;
    qty: number;
    suppliers: number;
    missingSupplier: number;
    missingPrice: number;
  };
  notes: string[];
}

interface Props {
  draft: PurchaseDraft;
  onClose: () => void;
}

export function PurchaseDraftDrawer({ draft, onClose }: Props) {
  const fileHref = `/api/purchase-drafts/${draft.id}/file`;
  return (
    <>
      <div className="spu-backdrop" onClick={onClose} />
      <aside className="spu-drawer" role="dialog" aria-labelledby="po-draft-title">
        <header className="spu-drawer-head">
          <div>
            <p className="eyebrow">采购单草稿 · 未写入 ERP</p>
            <h2 id="po-draft-title">{draft.filename}</h2>
            <p className="small">
              {draft.createdAt} · {int(draft.stats.lines)} 行 · {int(draft.stats.qty)} 件 · {int(draft.stats.suppliers)} 家供应商
            </p>
          </div>
          <button type="button" className="btn" onClick={onClose}>关闭</button>
        </header>

        <div className="spu-po-actions">
          <a className="btn primary" href={fileHref}>下载采购单</a>
          <a className="btn" href="/api/purchase-drafts/template">下载空白模板</a>
        </div>

        {draft.notes.map((note) => (
          <p key={note} className="small spu-po-note">{note}</p>
        ))}

        <section>
          <h3>按供应商</h3>
          <table className="spu-po-table">
            <thead>
              <tr>
                <th>供应商</th>
                <th className="num">行数</th>
                <th className="num">数量</th>
              </tr>
            </thead>
            <tbody>
              {draft.groups.map((group) => (
                <tr key={group.supplier}>
                  <td>{group.supplier}{group.missingPrice ? " · 缺单价" : ""}</td>
                  <td className="num">{int(group.lines)}</td>
                  <td className="num">{int(group.qty)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>

        <section>
          <h3>明细预览</h3>
          <table className="spu-po-table">
            <thead>
              <tr>
                <th>供应商</th>
                <th>商品编码</th>
                <th>款式编码</th>
                <th>名称</th>
                <th className="num">数量</th>
                <th className="num">单价</th>
              </tr>
            </thead>
            <tbody>
              {draft.lines.map((line, index) => (
                <tr key={`${line.sku}-${index}`}>
                  <td>{line.supplier || "未对上供应商"}</td>
                  <td className="mono">{line.sku}</td>
                  <td className="mono">{line.styleId}</td>
                  <td className="spu-name" title={line.name}>{line.name}</td>
                  <td className="num">{int(line.qty)}</td>
                  <td className="num">{line.price == null ? "—" : line.price}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>
      </aside>
    </>
  );
}
