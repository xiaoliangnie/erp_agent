/*
 * 位置数组 payload 的唯一解码点。
 *
 * 后端 backend/procurement_data.py 把采购单和明细行编码成纯位置数组，字典维度只存下标。
 * 以前每个页面各自写一份 O_* / L_* 常量，改列顺序要同步四处且没有运行时校验；现在下标
 * 只写在这里，页面拿到的是命名字段的对象。
 *
 * 改后端 payload 的列顺序时，只需要改这个文件的下标常量和 EXPECTED_* 宽度。
 */

/** 字典维度：页面显示的名字都从这里按下标取。 */
export interface PayloadDict {
  buyers: string[];
  suppliers: string[];
  warehouses: string[];
  spus: string[];
  cats: string[];
  colors: string[];
  addrs?: string[];
  pays?: string[];
  styles?: string[];
  seasons?: string[];
  brands?: string[];
  channels?: string[];
}

export interface PayloadMeta {
  source: string;
  generated: string;
  /** 本次 API 从数据库读取并构建响应的中国业务时间。 */
  databaseNow?: string;
  /** 数据同步任务最近一次抽取实时库的中国业务时间。 */
  syncedAt?: string;
  syncLagMinutes?: number | null;
  fresh?: boolean;
  timezone?: string;
  rows: number;
  orders: number;
  minDate: string;
  maxDate: string;
  today?: string;
  availableYears?: string[];
  selectedYear?: string | null;
  warning?: string | null;
  /** 只有交期台账的 payload 带这三个：交期列的覆盖范围。 */
  etaMin?: string;
  etaMax?: string;
  etaCoverage?: number;
}

export interface RawPayload {
  meta: PayloadMeta;
  dict: PayloadDict;
  orders: unknown[][];
  lines: unknown[][];
}

/* ── 采购看板 ─────────────────────────────────────────────── */

const O_NO = 0, O_DATE = 1, O_ST = 2, O_BUYER = 3, O_SUP = 4,
  O_WH = 5, O_ADDR = 6, O_PAY = 7, O_EXT = 8, O_AUDIT = 9, O_CREATED = 10;
const DASHBOARD_ORDER_WIDTH = 11;

const L_O = 0, L_SPU = 1, L_STYLE = 2, L_COLOR = 3, L_SPEC = 4, L_CAT = 5,
  L_SEASON = 6, L_BRAND = 7, L_CHAN = 8, L_QTY = 9, L_IN = 10, L_AMT = 11,
  L_PRICE = 12, L_STYPE = 13, L_SIZE = 14, L_ETA = 15, L_SKU = 16;
const DASHBOARD_LINE_WIDTH = 17;

export interface DashboardOrder {
  index: number;
  no: string;
  date: string;
  confirmed: boolean;
  buyer: number;
  supplier: number;
  warehouse: number;
  address: number;
  payment: number;
  externalNo: string;
  auditDate: string;
  /** 聚水潭采购单建立时间，最近采购单按此字段倒序。 */
  createdAt: string;
}

export interface DashboardLine {
  order: number;
  spu: number;
  style: number;
  color: number;
  spec: string;
  cat: number;
  season: number;
  brand: number;
  channel: number;
  qty: number;
  inQty: number;
  amount: number;
  price: number;
  sizeType: number;
  size: string;
  eta: string;
  sku: string;
}

/* ── 交期提醒台账 ─────────────────────────────────────────── */

const DO_NO = 0, DO_DATE = 1, DO_ST = 2, DO_BUYER = 3, DO_SUP = 4,
  DO_WH = 5, DO_EXT = 6, DO_AUDIT = 7;
const DELIVERY_ORDER_WIDTH = 8;

const DL_O = 0, DL_SPU = 1, DL_SKU = 2, DL_COLOR = 3, DL_SPEC = 4, DL_CAT = 5,
  DL_QTY = 6, DL_IN = 7, DL_DELIVERY = 8, DL_ETA = 9, DL_AMT = 10;
const DELIVERY_LINE_WIDTH = 11;

export interface DeliveryOrder {
  index: number;
  no: string;
  date: string;
  confirmed: boolean;
  buyer: number;
  supplier: number;
  warehouse: number;
  externalNo: string;
  auditDate: string;
}

export interface DeliveryLine {
  order: number;
  spu: number;
  sku: string;
  color: number;
  spec: string;
  cat: number;
  qty: number;
  inQty: number;
  /** 与供应商约定的交期，为空才退到 eta —— 台账页的日期口径。 */
  deliveryDate: string;
  /** 最早预计到货日期 —— 采购看板的日期口径。 */
  eta: string;
  amount: number;
}

/* ── 解码 ─────────────────────────────────────────────────── */

const str = (value: unknown): string => (value == null ? "" : String(value));
const num = (value: unknown): number => {
  const parsed = typeof value === "number" ? value : Number(value);
  return Number.isFinite(parsed) ? parsed : 0;
};

/**
 * 列数不对就直接报错，不猜。位置数组一旦错位，页面上每个数字都是错的，
 * 静默渲染比白屏危险得多。
 */
function assertWidth(rows: unknown[][], expected: number, label: string): void {
  const row = rows.find((candidate) => Array.isArray(candidate));
  if (row && row.length !== expected) {
    throw new Error(
      `${label} 列数是 ${row.length}，前端预期 ${expected}。` +
        "后端 procurement_data.py 的 payload 结构变了，请同步 frontend/src/data/payload.ts 的下标常量。",
    );
  }
}

export interface DashboardData {
  meta: PayloadMeta;
  dict: PayloadDict;
  orders: DashboardOrder[];
  lines: DashboardLine[];
}

export function decodeDashboard(payload: RawPayload): DashboardData {
  assertWidth(payload.orders, DASHBOARD_ORDER_WIDTH, "采购看板 orders");
  assertWidth(payload.lines, DASHBOARD_LINE_WIDTH, "采购看板 lines");
  return {
    meta: payload.meta,
    dict: payload.dict,
    orders: payload.orders.map((row, index) => ({
      index,
      no: str(row[O_NO]),
      date: str(row[O_DATE]),
      confirmed: num(row[O_ST]) === 1,
      buyer: num(row[O_BUYER]),
      supplier: num(row[O_SUP]),
      warehouse: num(row[O_WH]),
      address: num(row[O_ADDR]),
      payment: num(row[O_PAY]),
      externalNo: str(row[O_EXT]),
      auditDate: str(row[O_AUDIT]),
      createdAt: str(row[O_CREATED]),
    })),
    lines: payload.lines.map((row) => ({
      order: num(row[L_O]),
      spu: num(row[L_SPU]),
      style: num(row[L_STYLE]),
      color: num(row[L_COLOR]),
      spec: str(row[L_SPEC]),
      cat: num(row[L_CAT]),
      season: num(row[L_SEASON]),
      brand: num(row[L_BRAND]),
      channel: num(row[L_CHAN]),
      qty: num(row[L_QTY]),
      inQty: num(row[L_IN]),
      amount: num(row[L_AMT]),
      price: num(row[L_PRICE]),
      sizeType: num(row[L_STYPE]),
      size: str(row[L_SIZE]),
      eta: str(row[L_ETA]),
      sku: str(row[L_SKU]),
    })),
  };
}

export interface DeliveryData {
  meta: PayloadMeta;
  dict: PayloadDict;
  orders: DeliveryOrder[];
  lines: DeliveryLine[];
}

export function decodeDelivery(payload: RawPayload): DeliveryData {
  assertWidth(payload.orders, DELIVERY_ORDER_WIDTH, "交期台账 orders");
  assertWidth(payload.lines, DELIVERY_LINE_WIDTH, "交期台账 lines");
  return {
    meta: payload.meta,
    dict: payload.dict,
    orders: payload.orders.map((row, index) => ({
      index,
      no: str(row[DO_NO]),
      date: str(row[DO_DATE]),
      confirmed: num(row[DO_ST]) === 1,
      buyer: num(row[DO_BUYER]),
      supplier: num(row[DO_SUP]),
      warehouse: num(row[DO_WH]),
      externalNo: str(row[DO_EXT]),
      auditDate: str(row[DO_AUDIT]),
    })),
    lines: payload.lines.map((row) => ({
      order: num(row[DL_O]),
      spu: num(row[DL_SPU]),
      sku: str(row[DL_SKU]),
      color: num(row[DL_COLOR]),
      spec: str(row[DL_SPEC]),
      cat: num(row[DL_CAT]),
      qty: num(row[DL_QTY]),
      inQty: num(row[DL_IN]),
      deliveryDate: str(row[DL_DELIVERY]),
      eta: str(row[DL_ETA]),
      amount: num(row[DL_AMT]),
    })),
  };
}

/** 字典取名，越界回退到占位符而不是 undefined。 */
export function name(dict: string[] | undefined, index: number, fallback = "—"): string {
  const value = dict?.[index];
  return value == null || value === "" ? fallback : value;
}
