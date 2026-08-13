import { name } from "../../data/payload";
import type { PayloadDict } from "../../data/payload";
import type { LedgerOrder, WaveStamp } from "./model";
import { waveRank } from "./waves";

export type SortKey =
  | "urgent"
  | "no"
  | "date"
  | "supplier"
  | "eta"
  | "left"
  | "qty"
  | "in"
  | "pending"
  | "buyer"
  | "wave";

/** 数量类列点一下先给降序，其余升序 —— 看「谁最多」比「谁最少」常用。 */
export const DESC_FIRST: SortKey[] = ["qty", "in", "pending"];

const FAR_FUTURE = 1e9;
/** 排序里没有交期的排最后：用一个比任何日期都大的字符串。 */
const NO_DATE = "\uFFFF";

type Key = string | number | (string | number)[];

function keyOf(order: LedgerOrder, stamp: WaveStamp, dict: PayloadDict, sortKey: SortKey): Key {
  switch (sortKey) {
    // 默认序：先按波次，同波次按剩余天数，再按待入库量倒序。
    case "urgent":
      return [waveRank(stamp.wave), stamp.left === null ? FAR_FUTURE : stamp.left, -order.pending];
    case "no":
      return order.no;
    case "date":
      return order.date;
    case "supplier":
      return name(dict.suppliers, order.supplier);
    case "eta":
      return order.eta || NO_DATE;
    case "left":
      return stamp.left === null ? FAR_FUTURE : stamp.left;
    case "qty":
      return order.qty;
    case "in":
      return order.inQty;
    case "pending":
      return order.pending;
    case "buyer":
      return name(dict.buyers, order.buyer);
    case "wave":
      return waveRank(stamp.wave);
  }
}

export function sortOrders(
  orders: LedgerOrder[],
  stamps: Map<number, WaveStamp>,
  dict: PayloadDict,
  sortKey: SortKey,
  sortDir: number,
): LedgerOrder[] {
  const fallback = (a: LedgerOrder, b: LedgerOrder) => (a.no < b.no ? -1 : 1);
  return orders.slice().sort((a, b) => {
    const stampA = stamps.get(a.index);
    const stampB = stamps.get(b.index);
    if (!stampA || !stampB) return 0;
    const keyA = keyOf(a, stampA, dict, sortKey);
    const keyB = keyOf(b, stampB, dict, sortKey);
    if (Array.isArray(keyA) && Array.isArray(keyB)) {
      for (let index = 0; index < keyA.length; index += 1) {
        if (keyA[index] !== keyB[index]) return (keyA[index] < keyB[index] ? -1 : 1) * sortDir;
      }
      return fallback(a, b);
    }
    if (keyA === keyB) return fallback(a, b);
    return (keyA < keyB ? -1 : 1) * sortDir;
  });
}
