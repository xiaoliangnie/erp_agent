import { dayNumber } from "../../lib/format";
import { name } from "../../data/payload";
import type { DeliveryData, DeliveryLine } from "../../data/payload";
import type { OrderWave } from "./waves";
import { waveOfDays } from "./waves";

/**
 * 单级视图。数量、金额、代表商品这些不随基准日变化，建一次就固定；
 * 剩余天数和波次跟着「今天」走，另算（见 stampWaves）。
 */
export interface LedgerOrder {
  index: number;
  no: string;
  date: string;
  confirmed: boolean;
  buyer: number;
  supplier: number;
  warehouse: number;
  externalNo: string;
  lines: DeliveryLine[];
  qty: number;
  inQty: number;
  pending: number;
  amount: number;
  /** 整单交期：所有待入库行里最早的那个。 */
  eta: string;
  /** 交期是从 item_delivery_date 来的，还是退到了预计到货日。 */
  etaSource: "" | "交期" | "预计到货";
  /** 单内不同交期的个数，>1 时表格上标注「取最早」。 */
  etaSpread: number;
  etaDay: number | null;
  /** 数量最大的商品作为代表。 */
  product: string;
  productCount: number;
  category: string;
  haystack: string;
}

/** 随基准日变化的部分。 */
export interface WaveStamp {
  done: boolean;
  left: number | null;
  wave: OrderWave;
}

export function buildOrders(data: DeliveryData): LedgerOrder[] {
  const { dict, orders, lines } = data;
  const built: LedgerOrder[] = orders.map((order) => ({
    index: order.index,
    no: order.no,
    date: order.date,
    confirmed: order.confirmed,
    buyer: order.buyer,
    supplier: order.supplier,
    warehouse: order.warehouse,
    externalNo: order.externalNo,
    lines: [],
    qty: 0,
    inQty: 0,
    pending: 0,
    amount: 0,
    eta: "",
    etaSource: "",
    etaSpread: 0,
    etaDay: null,
    product: "",
    productCount: 0,
    category: "",
    haystack: "",
  }));

  for (const line of lines) {
    const order = built[line.order];
    if (!order) continue;
    order.lines.push(line);
    order.qty += line.qty;
    order.inQty += line.inQty;
    order.pending += Math.max(0, line.qty - line.inQty);
    order.amount += line.amount;
  }

  for (const order of built) {
    if (!order.lines.length) continue;

    // 交期优先看待入库行；整单已入库完的退一步用全部行，好歹显示个日期。
    const pending = order.lines.filter((line) => line.qty - line.inQty > 0);
    const pool = pending.length ? pending : order.lines;
    const agreed = pool.map((line) => line.deliveryDate).filter(Boolean);
    const expected = pool.map((line) => line.eta).filter(Boolean);
    if (agreed.length) {
      order.eta = agreed.reduce((a, b) => (a < b ? a : b));
      order.etaSource = "交期";
      order.etaSpread = new Set(agreed).size;
    } else if (expected.length) {
      order.eta = expected.reduce((a, b) => (a < b ? a : b));
      order.etaSource = "预计到货";
      order.etaSpread = new Set(expected).size;
    }
    order.etaDay = order.eta ? dayNumber(order.eta) : null;

    const byProduct = new Map<number, number>();
    for (const line of order.lines) byProduct.set(line.spu, (byProduct.get(line.spu) ?? 0) + line.qty);
    let best = -1;
    for (const [spu, qty] of byProduct) {
      if (qty > best) {
        best = qty;
        order.product = name(dict.spus, spu);
      }
    }
    order.productCount = byProduct.size;
    const categories = new Set(order.lines.map((line) => line.cat));
    order.category = name(dict.cats, order.lines[0].cat) + (categories.size > 1 ? ` +${categories.size - 1}` : "");

    order.haystack = [
      order.no,
      order.externalNo,
      name(dict.suppliers, order.supplier),
      name(dict.buyers, order.buyer),
      [...byProduct.keys()].map((spu) => name(dict.spus, spu)).join(" "),
      order.lines.map((line) => line.sku).join(" "),
    ]
      .join(" ")
      .toLowerCase();
  }

  return built;
}

/** 按基准日给每张单盖上剩余天数与波次。 */
export function stampWaves(orders: LedgerOrder[], today: string): Map<number, WaveStamp> {
  const t0 = dayNumber(today);
  const stamps = new Map<number, WaveStamp>();
  for (const order of orders) {
    const done = order.pending <= 0;
    const left = order.etaDay === null ? null : order.etaDay - t0;
    stamps.set(order.index, { done, left, wave: done ? "done" : waveOfDays(left) });
  }
  return stamps;
}
