import { int, pct } from "../../lib/format";
import type { SaleShop, SpuStyle } from "./types";

export interface ChannelWindow {
  label: string;
  online: number;
  offline: number;
}

export function styleChannelWindows(style: SpuStyle): ChannelWindow[] {
  return [
    { label: "7天", online: style.sales7Online ?? 0, offline: style.sales7Offline ?? 0 },
    { label: "15天", online: style.sales15Online ?? 0, offline: style.sales15Offline ?? 0 },
    { label: "30天", online: style.sales30Online ?? 0, offline: style.sales30Offline ?? 0 },
  ];
}

export function channelParts(online: number, offline: number) {
  const on = Math.max(0, online);
  const off = Math.max(0, offline);
  const total = on + off;
  return {
    on,
    off,
    total,
    onPct: total > 0 ? on / total : 0,
    offPct: total > 0 ? off / total : 0,
  };
}

function MixBar({ online, offline }: { online: number; offline: number }) {
  const parts = channelParts(online, offline);
  return (
    <div className="spu-mix-bar" aria-hidden>
      <i className="is-online" style={{ width: `${(parts.onPct * 100).toFixed(1)}%` }} />
      <i className="is-offline" style={{ width: `${(parts.offPct * 100).toFixed(1)}%` }} />
    </div>
  );
}

function ChannelWindowLine({ label, online, offline }: ChannelWindow) {
  const parts = channelParts(online, offline);
  if (parts.total <= 0) {
    return (
      <div className="spu-ch-winline">
        <span className="spu-ch-winlabel">{label}</span>
        <div className="spu-mix-bar" aria-hidden />
        <span className="small">—</span>
      </div>
    );
  }
  return (
    <div
      className="spu-ch-winline"
      title={`${label} 线上 ${int(parts.on)}（${pct(parts.on, parts.total)}） / 线下 ${int(parts.off)}（${pct(parts.off, parts.total)}）`}
    >
      <span className="spu-ch-winlabel">{label}</span>
      <MixBar online={parts.on} offline={parts.off} />
      <span className="num is-online">{int(parts.on)}</span>
      <span className="spu-mix-sep">/</span>
      <span className="num is-offline">{int(parts.off)}</span>
    </div>
  );
}

/** 表里一列：7 / 15 / 30 天线上线下。 */
export function ChannelWindowsCell({ windows }: { windows: ChannelWindow[] }) {
  const any = windows.some((win) => win.online + win.offline > 0);
  if (!any) return <span className="small">—</span>;
  return (
    <div className="spu-mix-stack">
      {windows.map((win) => (
        <ChannelWindowLine key={win.label} {...win} />
      ))}
    </div>
  );
}

function ChannelRow({
  side,
  qty,
  total,
  peak,
}: {
  side: "online" | "offline";
  qty: number;
  total: number;
  peak: number;
}) {
  const name = side === "online" ? "线上" : "线下";
  const width = peak <= 0 ? "0%" : `${Math.max(qty > 0 ? 4 : 0, (qty / peak) * 100).toFixed(1)}%`;
  return (
    <div className={`spu-ch-row is-${side}`}>
      <span className="spu-ch-name">{name}</span>
      <div className="spu-ch-track">
        <i className={`spu-ch-fill is-${side}`} style={{ width }} />
      </div>
      <span className="spu-ch-qty num">{int(qty)}</span>
      <span className="spu-ch-share num">{pct(qty, total)}</span>
    </div>
  );
}

/** 抽屉：7 / 15 / 30 天线下对照。 */
export function ChannelCompare({ windows }: { windows: ChannelWindow[] }) {
  return (
    <section className="spu-channel">
      <p className="eyebrow">线上 / 线下出库 · 件</p>
      {windows.map((win) => {
        const parts = channelParts(win.online, win.offline);
        const peak = Math.max(parts.on, parts.off, 1);
        const lead =
          parts.total <= 0
            ? "没有出库"
            : parts.on === parts.off
              ? "两边一样"
              : parts.on > parts.off
                ? "线上更多"
                : "线下更多";
        return (
          <div
            key={win.label}
            className={`spu-ch-win ${win.label.includes("30") ? "is-featured" : ""}`}
          >
            <div className="spu-ch-head">
              <span>{win.label}</span>
              <span className="small">
                合计 {int(parts.total)}
                {parts.total > 0 ? ` · ${lead}` : ""}
              </span>
            </div>
            <ChannelRow side="online" qty={parts.on} total={parts.total} peak={peak} />
            <ChannelRow side="offline" qty={parts.off} total={parts.total} peak={peak} />
          </div>
        );
      })}
    </section>
  );
}

/** 真实出库店铺，不是编的。 */
export function ShopSources({ shops }: { shops: SaleShop[] }) {
  if (!shops.length) {
    return (
      <section>
        <p className="eyebrow">出库店铺</p>
        <div className="small">近 30 天没有对上店铺的出库；未对上的会计入线上。</div>
      </section>
    );
  }
  return (
    <section>
      <p className="eyebrow">出库店铺 · 镜像出库对上的店，不是编的</p>
      <table className="spu-shops">
        <thead>
          <tr>
            <th>店铺</th>
            <th>分组</th>
            <th>渠道</th>
            <th className="num">7天</th>
            <th className="num">15天</th>
            <th className="num">30天</th>
          </tr>
        </thead>
        <tbody>
          {shops.map((shop) => (
            <tr key={`${shop.shopId}:${shop.shopName}`}>
              <td title={shop.shopId ? `店铺ID ${shop.shopId}` : "出库行没有对上店铺"}>
                {shop.shopName || "—"}
              </td>
              <td>{shop.groupName || "—"}</td>
              <td>{shop.channel === "offline" ? "线下" : "线上"}</td>
              <td className="num">{int(shop.qty7 ?? 0)}</td>
              <td className="num">{int(shop.qty15 ?? 0)}</td>
              <td className="num">{int(shop.qty30)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  );
}

/** 看板顶栏：全表 7 / 15 / 30 天线上线下合计，条带铺满卡片。 */
export function ChannelBoardCard({ windows }: { windows: ChannelWindow[] }) {
  return (
    <div className="spu-card spu-card-channel" aria-label="7/15/30天线上线下出库">
      <span className="eyebrow">线上 / 线下 · 7 / 15 / 30 天</span>
      <div className="spu-ch-board">
        {windows.map((win) => {
          const parts = channelParts(win.online, win.offline);
          return (
            <div
              key={win.label}
              className="spu-ch-board-row"
              title={`${win.label} 线上 ${int(parts.on)}（${pct(parts.on, parts.total)}） / 线下 ${int(parts.off)}（${pct(parts.off, parts.total)}）`}
            >
              <span className="spu-ch-winlabel">{win.label}</span>
              <div className="spu-ch-board-track" aria-hidden>
                {parts.total > 0 ? (
                  <>
                    <i className="is-online" style={{ width: `${(parts.onPct * 100).toFixed(1)}%` }} />
                    <i className="is-offline" style={{ width: `${(parts.offPct * 100).toFixed(1)}%` }} />
                  </>
                ) : null}
              </div>
              <span className="num is-online">{parts.total > 0 ? int(parts.on) : "—"}</span>
              <span className="spu-mix-sep">/</span>
              <span className="num is-offline">{parts.total > 0 ? int(parts.off) : "—"}</span>
            </div>
          );
        })}
      </div>
    </div>
  );
}
