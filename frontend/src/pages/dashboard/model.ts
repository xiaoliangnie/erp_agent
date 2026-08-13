import { cssVar } from "../../lib/format";
import { name } from "../../data/payload";
import type { DashboardData, DashboardLine, DashboardOrder, PayloadDict } from "../../data/payload";
import { isoMonday, shiftDays } from "./charts/geometry";
import { TIERS, TIER_BY_KEY, isUrgent, tierOfDays } from "./tiers";
import type { TierKey } from "./tiers";

export interface DashFilters {
  preset: string;
  from: string;
  to: string;
  status: string;
  cat: string;
  buyer: string;
  query: string;
  eta: string;
}

export const PRESETS = [
  { k: "year", label: "全年" },
  { k: "30", label: "近 30 天" },
  { k: "90", label: "近 90 天" },
  { k: "180", label: "近 180 天" },
  { k: "custom", label: "自定义" },
];

/** 年度边界：右端不超过数据最新日期。 */
export function yearBounds(year: string, maxDate: string): [string, string] {
  const start = `${year}-01-01`;
  const naturalEnd = `${year}-12-31`;
  return [start, naturalEnd < maxDate ? naturalEnd : maxDate];
}

export function resolveRange(filters: DashFilters, year: string, maxDate: string): [string, string] {
  const [yearStart, yearEnd] = yearBounds(year, maxDate);
  if (filters.preset === "year") return [yearStart, yearEnd];
  if (filters.preset === "custom") {
    return [filters.from < yearStart ? yearStart : filters.from, filters.to > yearEnd ? yearEnd : filters.to];
  }
  const from = shiftDays(yearEnd, -(Number.parseInt(filters.preset, 10) - 1));
  return [from < yearStart ? yearStart : from, yearEnd];
}

/** 距今天的天数，null 表示没有预计到货日。 */
export function makeEtaDays(today: string) {
  const t0 = Date.parse(`${today}T00:00:00`);
  const cache = new Map<string, number>();
  return (iso: string): number | null => {
    if (!iso) return null;
    let value = cache.get(iso);
    if (value === undefined) {
      value = Math.round((Date.parse(`${iso}T00:00:00`) - t0) / 86_400_000);
      cache.set(iso, value);
    }
    return value;
  };
}

export type EtaDays = ReturnType<typeof makeEtaDays>;

/** 明细行所处的提醒档；已入库完的行没有期限可催，返回 null。 */
export function lineTier(line: DashboardLine, etaDays: EtaDays): TierKey | null {
  if (line.qty - line.inQty <= 0) return null;
  return tierOfDays(etaDays(line.eta));
}

export interface Slice {
  rows: DashboardLine[];
  /** 不含到货期限这一条筛选 —— 预警卡按它算，选中某档时其它档不会归零。 */
  baseRows: DashboardLine[];
  orderIndexes: number[];
  from: string;
  to: string;
}

export function applyFilters(
  data: DashboardData,
  filters: DashFilters,
  year: string,
  etaDays: EtaDays,
): Slice {
  const { orders, lines, dict } = data;
  const [from, to] = resolveRange(filters, year, data.meta.maxDate);
  const query = filters.query.trim().toLowerCase();
  const cat = filters.cat === "" ? -1 : Number(filters.cat);
  const buyer = filters.buyer === "" ? -1 : Number(filters.buyer);
  const status = filters.status === "" ? -1 : Number(filters.status);

  const rows: DashboardLine[] = [];
  const baseRows: DashboardLine[] = [];
  const seen = new Set<number>();

  for (const line of lines) {
    const order = orders[line.order];
    if (!order) continue;
    if (order.date < from || order.date > to) continue;
    if (status >= 0 && Number(order.confirmed) !== status) continue;
    if (cat >= 0 && line.cat !== cat) continue;
    if (buyer >= 0 && order.buyer !== buyer) continue;
    if (query && !lineText(line, order, dict).includes(query)) continue;
    baseRows.push(line);
    if (filters.eta) {
      const tier = lineTier(line, etaDays);
      if (tier === null) continue;
      if (filters.eta !== "open" && tier !== filters.eta) continue;
    }
    rows.push(line);
    seen.add(line.order);
  }

  return { rows, baseRows, orderIndexes: [...seen], from, to };
}

function lineText(line: DashboardLine, order: DashboardOrder, dict: PayloadDict): string {
  return [
    name(dict.spus, line.spu),
    name(dict.colors, line.color),
    line.spec,
    line.sku,
    name(dict.styles, line.style),
    order.no,
    order.externalNo,
  ]
    .join(" ")
    .toLowerCase();
}

export interface Totals {
  amount: number;
  qty: number;
  inQty: number;
  open: number;
  orders: number;
  lines: number;
  pending: number;
  suppliers: number;
}

export function totals(slice: Slice, orders: DashboardOrder[]): Totals {
  let amount = 0;
  let qty = 0;
  let inQty = 0;
  for (const line of slice.rows) {
    amount += line.amount;
    qty += line.qty;
    inQty += line.inQty;
  }
  let pending = 0;
  const suppliers = new Set<number>();
  for (const index of slice.orderIndexes) {
    const order = orders[index];
    if (!order) continue;
    if (!order.confirmed) pending += 1;
    suppliers.add(order.supplier);
  }
  return {
    amount,
    qty,
    inQty,
    open: qty - inQty,
    orders: slice.orderIndexes.length,
    lines: slice.rows.length,
    pending,
    suppliers: suppliers.size,
  };
}

export interface PeriodPoint {
  key: string;
  amount: number;
  qty: number;
  orders: number;
}

export function byPeriod(
  rows: DashboardLine[],
  orders: DashboardOrder[],
  granularity: "day" | "week" | "month",
): PeriodPoint[] {
  const map = new Map<string, { amount: number; qty: number; orders: Set<number> }>();
  for (const line of rows) {
    const date = orders[line.order]?.date;
    if (!date) continue;
    const key = granularity === "month" ? date.slice(0, 7) : granularity === "week" ? isoMonday(date) : date;
    let entry = map.get(key);
    if (!entry) {
      entry = { amount: 0, qty: 0, orders: new Set() };
      map.set(key, entry);
    }
    entry.amount += line.amount;
    entry.qty += line.qty;
    entry.orders.add(line.order);
  }
  return [...map.entries()]
    .map(([key, entry]) => ({ key, amount: entry.amount, qty: entry.qty, orders: entry.orders.size }))
    .sort((a, b) => (a.key < b.key ? -1 : 1));
}

export interface DimPoint {
  k: number;
  name: string;
  amount: number;
  qty: number;
  inQty: number;
  lines: number;
  orders: number;
}

export function byDim(
  rows: DashboardLine[],
  keyOf: (line: DashboardLine) => number,
  names: string[] | undefined,
): DimPoint[] {
  const map = new Map<number, { amount: number; qty: number; inQty: number; lines: number; orders: Set<number> }>();
  for (const line of rows) {
    const key = keyOf(line);
    let entry = map.get(key);
    if (!entry) {
      entry = { amount: 0, qty: 0, inQty: 0, lines: 0, orders: new Set() };
      map.set(key, entry);
    }
    entry.amount += line.amount;
    entry.qty += line.qty;
    entry.inQty += line.inQty;
    entry.lines += 1;
    entry.orders.add(line.order);
  }
  return [...map.entries()].map(([key, entry]) => ({
    k: key,
    name: name(names, key),
    amount: entry.amount,
    qty: entry.qty,
    inQty: entry.inQty,
    lines: entry.lines,
    orders: entry.orders.size,
  }));
}

export interface SizeRow {
  name: string;
  total: number;
  cells: Map<string, number>;
}

/** 尺码曲线矩阵：行 = 商品，列 = 尺码，每行按自身采购量归一。 */
export function sizeMatrix(rows: DashboardLine[], dict: PayloadDict, mode: number) {
  const perSpu = new Map<number, SizeRow>();
  for (const line of rows) {
    if (line.sizeType !== mode || !line.size) continue;
    let entry = perSpu.get(line.spu);
    if (!entry) {
      entry = { name: name(dict.spus, line.spu), total: 0, cells: new Map() };
      perSpu.set(line.spu, entry);
    }
    entry.total += line.qty;
    entry.cells.set(line.size, (entry.cells.get(line.size) ?? 0) + line.qty);
  }
  const rowList = [...perSpu.values()].sort((a, b) => b.total - a.total).slice(0, 10);
  // 只保留展示行里真正出现过的码段，避免长尾码把横轴撑空。
  const cols = new Set<string>();
  for (const row of rowList) {
    for (const [size, qty] of row.cells) if (qty > 0) cols.add(size);
  }
  return { cols: [...cols].sort((a, b) => Number.parseFloat(a) - Number.parseFloat(b)), rows: rowList };
}

export interface MixPart {
  name: string;
  value: number;
  color: string;
  note: string;
  risk?: boolean;
}

/** 待入库量按到货排期分三档。 */
export function openMix(rows: DashboardLine[], today: string): MixPart[] {
  let overdue = 0;
  let planned = 0;
  let unplanned = 0;
  for (const line of rows) {
    const open = line.qty - line.inQty;
    if (open <= 0) continue;
    if (!line.eta) unplanned += open;
    else if (line.eta < today) overdue += open;
    else planned += open;
  }
  return [
    { name: "已逾期", value: overdue, color: cssVar("--critical"), note: "预计到货日已过", risk: true },
    { name: "排期内待到货", value: planned, color: cssVar("--series-1"), note: "预计到货日在今天及以后" },
    { name: "未排期", value: unplanned, color: cssVar("--track"), note: "没有填预计到货日期" },
  ];
}

export interface TierBucket {
  k: TierKey;
  label: string;
  note: string;
  color: string;
  qty: number;
  lines: number;
  orders: number;
  buyers: number;
  due: string;
}

export interface BuyerBucket {
  k: number;
  name: string;
  cells: Map<TierKey, { qty: number; orders: number }>;
  qty: number;
  urgent: number;
  orders: number;
  due: string;
}

/** 到货预警：待入库量按提醒档拆开，并落到人头上。 */
export function alertData(
  source: DashboardLine[],
  orders: DashboardOrder[],
  dict: PayloadDict,
  etaDays: EtaDays,
) {
  const tiers = TIERS.map((tier) => ({
    k: tier.k,
    label: tier.label,
    note: tier.note,
    color: cssVar(tier.cssVar),
    qty: 0,
    lines: 0,
    orderSet: new Set<number>(),
    buyerSet: new Set<number>(),
    due: "",
  }));
  const tierByKey = new Map(tiers.map((tier) => [tier.k, tier]));
  const byBuyer = new Map<
    number,
    {
      k: number;
      name: string;
      cells: Map<TierKey, { qty: number; orderSet: Set<number> }>;
      qty: number;
      urgent: number;
      orderSet: Set<number>;
      due: string;
    }
  >();

  for (const line of source) {
    const tierKey = lineTier(line, etaDays);
    if (!tierKey) continue;
    const order = orders[line.order];
    if (!order) continue;
    const open = line.qty - line.inQty;
    const tier = tierByKey.get(tierKey)!;
    tier.qty += open;
    tier.lines += 1;
    tier.orderSet.add(line.order);
    tier.buyerSet.add(order.buyer);
    if (line.eta && (!tier.due || line.eta < tier.due)) tier.due = line.eta;

    let buyer = byBuyer.get(order.buyer);
    if (!buyer) {
      buyer = {
        k: order.buyer,
        name: name(dict.buyers, order.buyer),
        cells: new Map(),
        qty: 0,
        urgent: 0,
        orderSet: new Set(),
        due: "",
      };
      byBuyer.set(order.buyer, buyer);
    }
    let cell = buyer.cells.get(tierKey);
    if (!cell) {
      cell = { qty: 0, orderSet: new Set() };
      buyer.cells.set(tierKey, cell);
    }
    cell.qty += open;
    cell.orderSet.add(line.order);
    buyer.qty += open;
    buyer.orderSet.add(line.order);
    if (isUrgent(tierKey)) buyer.urgent += open;
    if (line.eta && (!buyer.due || line.eta < buyer.due)) buyer.due = line.eta;
  }

  const tierBuckets: TierBucket[] = tiers.map((tier) => ({
    k: tier.k,
    label: tier.label,
    note: tier.note,
    color: tier.color,
    qty: tier.qty,
    lines: tier.lines,
    orders: tier.orderSet.size,
    buyers: tier.buyerSet.size,
    due: tier.due,
  }));

  const buyerBuckets: BuyerBucket[] = [...byBuyer.values()]
    .sort((a, b) => b.urgent - a.urgent || b.qty - a.qty)
    .map((buyer) => ({
      k: buyer.k,
      name: buyer.name,
      cells: new Map([...buyer.cells].map(([key, cell]) => [key, { qty: cell.qty, orders: cell.orderSet.size }])),
      qty: buyer.qty,
      urgent: buyer.urgent,
      orders: buyer.orderSet.size,
      due: buyer.due,
    }));

  return {
    tiers: tierBuckets,
    buyers: buyerBuckets,
    total: tierBuckets.reduce((sum, tier) => sum + tier.qty, 0),
    urgent: tierBuckets.filter((tier) => isUrgent(tier.k)).reduce((sum, tier) => sum + tier.qty, 0),
  };
}

export interface OrderDue {
  due: string;
  days: number | null;
  open: number;
  noDate: boolean;
  tier: TierKey | null;
  /** 排序键：剩余天数升序，未排期排在有期限之后，全部入库完的垫底。 */
  sort: number;
}

/** 采购单级的期限：取该单所有待入库行里最早的预计到货日，档位取最急的一档。 */
export function orderDue(lines: DashboardLine[], etaDays: EtaDays): OrderDue {
  let due = "";
  let rank = 99;
  let open = 0;
  let noDate = false;
  for (const line of lines) {
    const pending = line.qty - line.inQty;
    if (pending <= 0) continue;
    open += pending;
    const tier = lineTier(line, etaDays);
    if (tier) rank = Math.min(rank, TIER_BY_KEY.get(tier)!.rank);
    if (line.eta) {
      if (!due || line.eta < due) due = line.eta;
    } else {
      noDate = true;
    }
  }
  const days = due ? etaDays(due) : null;
  return {
    due,
    days,
    open,
    noDate,
    tier: rank === 99 ? null : TIERS[rank].k,
    sort: open <= 0 ? 9e15 : days === null ? 9e14 : days,
  };
}

export interface OrderRow {
  orderIndex: number;
  lines: DashboardLine[];
  lineCount: number;
  qty: number;
  inQty: number;
  amount: number;
  cats: number[];
  due: OrderDue;
}

export type OrderSortKey =
  | "no" | "date" | "created" | "due" | "left" | "st" | "buyer" | "cat"
  | "n" | "qty" | "inq" | "open" | "rate" | "amt" | "wh";

export function buildOrderRows(
  rows: DashboardLine[],
  orders: DashboardOrder[],
  dict: PayloadDict,
  etaDays: EtaDays,
  sortKey: OrderSortKey,
  sortDir: number,
): OrderRow[] {
  const map = new Map<number, OrderRow>();
  for (const line of rows) {
    let entry = map.get(line.order);
    if (!entry) {
      entry = {
        orderIndex: line.order,
        lines: [],
        lineCount: 0,
        qty: 0,
        inQty: 0,
        amount: 0,
        cats: [],
        due: { due: "", days: null, open: 0, noDate: false, tier: null, sort: 9e15 },
      };
      map.set(line.order, entry);
    }
    entry.lineCount += 1;
    entry.qty += line.qty;
    entry.inQty += line.inQty;
    entry.amount += line.amount;
    if (!entry.cats.includes(line.cat)) entry.cats.push(line.cat);
    entry.lines.push(line);
  }

  const list = [...map.values()];
  for (const entry of list) entry.due = orderDue(entry.lines, etaDays);

  const keyOf = (entry: OrderRow): string | number => {
    const order = orders[entry.orderIndex];
    switch (sortKey) {
      case "no": return order.no;
      case "date": return order.date;
      case "created": return order.createdAt || order.date;
      case "st": return Number(order.confirmed);
      case "buyer": return name(dict.buyers, order.buyer);
      case "cat": return name(dict.cats, entry.cats[0]);
      case "n": return entry.lineCount;
      case "qty": return entry.qty;
      case "inq": return entry.inQty;
      case "due":
      case "left": return entry.due.sort;
      case "open": return entry.due.open;
      case "rate": return entry.qty ? entry.inQty / entry.qty : 0;
      case "amt": return entry.amount;
      case "wh": return name(dict.warehouses, order.warehouse);
    }
  };

  return list.sort((a, b) => {
    const keyA = keyOf(a);
    const keyB = keyOf(b);
    if (keyA === keyB) return 0;
    return (keyA > keyB ? 1 : -1) * sortDir;
  });
}
