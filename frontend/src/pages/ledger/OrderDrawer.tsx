import { useEffect } from "react";
import { cssVar, dayIso, dayNumber, int, money, pct } from "../../lib/format";
import { name } from "../../data/payload";
import type { PayloadDict } from "../../data/payload";
import type { LedgerOrder, WaveStamp } from "./model";
import { WAVE_BY_KEY, daysText, isUrgent, planWaves, waveLabel, waveOfDays } from "./waves";

interface OrderDrawerProps {
  order: LedgerOrder | null;
  stamp: WaveStamp | null;
  dict: PayloadDict;
  today: number;
  onClose: () => void;
}

function waveColor(wave: string): string {
  if (wave === "done") return cssVar("--tier-done");
  const found = WAVE_BY_KEY.get(wave as never);
  return found ? cssVar(found.cssVar) : cssVar("--text-muted");
}

export function OrderDrawer({ order, stamp, dict, today, onClose }: OrderDrawerProps) {
  useEffect(() => {
    if (!order) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [order, onClose]);

  const open = Boolean(order && stamp);
  const color = stamp ? waveColor(stamp.wave) : undefined;
  const plan = order ? planWaves(order.etaDay) : null;

  // 明细排序：待入库的排前面，再按交期，再按待入库量。
  const lines = order
    ? order.lines.slice().sort((a, b) => {
        const pendA = a.qty - a.inQty;
        const pendB = b.qty - b.inQty;
        const etaA = a.deliveryDate || a.eta || "\uFFFF";
        const etaB = b.deliveryDate || b.eta || "\uFFFF";
        return Number(pendB > 0) - Number(pendA > 0) || (etaA < etaB ? -1 : etaA > etaB ? 1 : 0) || pendB - pendA;
      })
    : [];

  const meta: [string, string][] =
    order && stamp
      ? [
          ["采购日期", order.date || "—"],
          ["状态", order.confirmed ? "已确认" : "待审核"],
          ["采购员", name(dict.buyers, order.buyer)],
          ["供应商 ID", name(dict.suppliers, order.supplier)],
          ["交期", (order.eta || "—") + (order.etaSource && order.etaSource !== "交期" ? "（预计到货日）" : "")],
          ["剩余", daysText(stamp.left)],
          ["采购数量", `${int(order.qty)} 件`],
          ["入库数量", `${int(order.inQty)} 件（${pct(order.inQty, order.qty)}）`],
          ["待入库", `${int(order.pending)} 件`],
          ["采购金额", `${money(order.amount)} 元`],
          ["仓储方", name(dict.warehouses, order.warehouse)],
          ["外部单号", order.externalNo || "—"],
        ]
      : [];

  return (
    <>
      <div className={`scrim${open ? " open" : ""}`} onClick={onClose} />
      <aside className={`drawer${open ? " open" : ""}`} aria-hidden={!open} aria-label="采购单明细">
        {order && stamp ? (
          <>
            <div className="drawer-head">
              <div className="row">
                <h3>{order.no}</h3>
                <span className="chip" style={{ color, borderColor: color }}>
                  {waveLabel(stamp.wave)}
                </span>
                <button type="button" className="close" onClick={onClose}>
                  关闭
                </button>
              </div>
              <div className="meta-grid">
                {meta.map(([key, value]) => (
                  <div key={key}>
                    <div className="k">{key}</div>
                    <div className="v">{value}</div>
                  </div>
                ))}
              </div>
            </div>
            <div className="drawer-body">
              <h4>四波提醒排期</h4>
              {!plan ? (
                <div className="empty">这张单没有交期，四波提醒都排不出来 —— 先让供应商补交期。</div>
              ) : (
                <div className="plan">
                  {plan.map(({ wave, day }) => {
                    const hit = today >= day;
                    return (
                      <div
                        key={wave.k}
                        className="step"
                        style={{ borderLeftColor: hit ? cssVar(wave.cssVar) : cssVar("--deemph") }}
                      >
                        <div className="s-k">
                          {wave.seq} · {wave.label}
                        </div>
                        <div className="s-d">{dayIso(day)}</div>
                        <div className="s-s">
                          {stamp.done
                            ? "已入库完，不用发"
                            : hit
                              ? stamp.wave === wave.k
                                ? "当前这一波"
                                : "已到点"
                              : `还有 ${day - today} 天`}
                        </div>
                      </div>
                    );
                  })}
                </div>
              )}

              <h4>商品明细</h4>
              <table>
                <thead>
                  <tr>
                    <th>商品</th>
                    <th>编码</th>
                    <th>颜色</th>
                    <th>规格</th>
                    <th>品类</th>
                    <th className="n">数量</th>
                    <th className="n">入库数量</th>
                    <th className="n">待入库</th>
                    <th>交期</th>
                    <th className="n">剩余</th>
                    <th className="n">金额</th>
                  </tr>
                </thead>
                <tbody>
                  {lines.map((line, index) => {
                    const pending = Math.max(0, line.qty - line.inQty);
                    const eta = line.deliveryDate || line.eta || "";
                    const left = eta ? dayNumber(eta) - today : null;
                    const lineWave = left === null ? null : waveOfDays(left);
                    return (
                      <tr key={`${line.sku}-${index}`}>
                        <td>{name(dict.spus, line.spu)}</td>
                        <td>{line.sku || "—"}</td>
                        <td>{name(dict.colors, line.color)}</td>
                        <td>{line.spec || "—"}</td>
                        <td>{name(dict.cats, line.cat)}</td>
                        <td className="n">{int(line.qty)}</td>
                        <td className="n">{int(line.inQty)}</td>
                        <td className="n">{int(pending)}</td>
                        <td title={eta && !line.deliveryDate ? "这行没填交期，用的是最早预计到货日期" : undefined}>
                          {eta || "—"}
                        </td>
                        <td
                          className="n"
                          style={{
                            color:
                              pending > 0 && lineWave && isUrgent(lineWave)
                                ? waveColor(lineWave)
                                : cssVar("--text-muted"),
                          }}
                        >
                          {pending > 0 ? daysText(left) : "已入库完"}
                        </td>
                        <td className="n">{money(line.amount)}</td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          </>
        ) : null}
      </aside>
    </>
  );
}
