/*
 * 位置数组 payload 的唯一解码点。
 *
 * 后端 backend/procurement_data.py 把采购单和明细行编码成纯位置数组，字典维度只存下标。
 * 以前每个页面各自写一份 O_* / L_* 常量，改列顺序要同步四处且没有运行时校验；现在
 * 列名只写在这里，页面拿到的是命名字段的对象。后端同时下发 `columns`，列名不一致直接报错。
 *
 * 改后端 payload 的列顺序时，同步 `procurement_data.py` 的列名常量和本文件。
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

export interface PayloadColumns {
  orders: string[];
  lines: string[];
}

export interface RawPayload {
  meta: PayloadMeta;
  dict: PayloadDict;
  orders: unknown[][];
  lines: unknown[][];
  /** 有序列名。有则必须与前端常量一致；没有则仍按宽度校验（旧缓存）。 */
  columns?: PayloadColumns;
}

/* ── 采购看板 ─────────────────────────────────────────────── */

export const DASHBOARD_ORDER_COLUMNS = [
  "采购单号", "采购日期", "已确认", "采购员", "供应商", "仓储方",
  "收货地址", "付款方式", "外部单号", "审核日期", "采购单建立时间",
] as const;

export const DASHBOARD_LINE_COLUMNS = [
  "采购单下标", "SPU", "款式", "颜色", "规格", "品类", "季节", "品牌",
  "渠道", "数量", "入库", "金额", "单价", "尺码类型", "尺码", "预计到货", "SKU",
] as const;

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

export const DELIVERY_ORDER_COLUMNS = [
  "采购单号", "采购日期", "已确认", "采购员", "供应商", "仓储方", "外部单号", "审核日期",
] as const;

export const DELIVERY_LINE_COLUMNS = [
  "采购单下标", "SPU", "SKU", "颜色", "规格", "品类",
  "数量", "入库", "交期", "预计到货", "金额",
] as const;

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
 * 列名或列数不对就直接报错，不猜。位置数组一旦错位，页面上每个数字都是错的，
 * 静默渲染比白屏危险得多。
 */
function assertColumns(
  rows: unknown[][],
  incoming: string[] | undefined,
  expected: readonly string[],
  label: string,
): void {
  if (incoming && incoming.length) {
    const same =
      incoming.length === expected.length &&
      incoming.every((name, index) => name === expected[index]);
    if (!same) {
      throw new Error(
        `${label} 列名是 [${incoming.join(", ")}]，前端预期 [${expected.join(", ")}]。` +
          "后端 procurement_data.py 的 payload 结构变了，请同步 frontend/src/data/payload.ts。",
      );
    }
  }
  const row = rows.find((candidate) => Array.isArray(candidate));
  if (row && row.length !== expected.length) {
    throw new Error(
      `${label} 列数是 ${row.length}，前端预期 ${expected.length}（${expected.join(" / ")}）。` +
        "后端 procurement_data.py 的 payload 结构变了，请同步 frontend/src/data/payload.ts。",
    );
  }
}

function col(columns: readonly string[], name: string): number {
  const index = columns.indexOf(name);
  if (index < 0) {
    throw new Error(`payload 缺少列「${name}」`);
  }
  return index;
}

export interface DashboardData {
  meta: PayloadMeta;
  dict: PayloadDict;
  orders: DashboardOrder[];
  lines: DashboardLine[];
}

export function decodeDashboard(payload: RawPayload): DashboardData {
  assertColumns(payload.orders, payload.columns?.orders, DASHBOARD_ORDER_COLUMNS, "采购看板 orders");
  assertColumns(payload.lines, payload.columns?.lines, DASHBOARD_LINE_COLUMNS, "采购看板 lines");
  const orderCol = (name: string) => col(DASHBOARD_ORDER_COLUMNS, name);
  const lineCol = (name: string) => col(DASHBOARD_LINE_COLUMNS, name);
  return {
    meta: payload.meta,
    dict: payload.dict,
    orders: payload.orders.map((row, index) => ({
      index,
      no: str(row[orderCol("采购单号")]),
      date: str(row[orderCol("采购日期")]),
      confirmed: num(row[orderCol("已确认")]) === 1,
      buyer: num(row[orderCol("采购员")]),
      supplier: num(row[orderCol("供应商")]),
      warehouse: num(row[orderCol("仓储方")]),
      address: num(row[orderCol("收货地址")]),
      payment: num(row[orderCol("付款方式")]),
      externalNo: str(row[orderCol("外部单号")]),
      auditDate: str(row[orderCol("审核日期")]),
      createdAt: str(row[orderCol("采购单建立时间")]),
    })),
    lines: payload.lines.map((row) => ({
      order: num(row[lineCol("采购单下标")]),
      spu: num(row[lineCol("SPU")]),
      style: num(row[lineCol("款式")]),
      color: num(row[lineCol("颜色")]),
      spec: str(row[lineCol("规格")]),
      cat: num(row[lineCol("品类")]),
      season: num(row[lineCol("季节")]),
      brand: num(row[lineCol("品牌")]),
      channel: num(row[lineCol("渠道")]),
      qty: num(row[lineCol("数量")]),
      inQty: num(row[lineCol("入库")]),
      amount: num(row[lineCol("金额")]),
      price: num(row[lineCol("单价")]),
      sizeType: num(row[lineCol("尺码类型")]),
      size: str(row[lineCol("尺码")]),
      eta: str(row[lineCol("预计到货")]),
      sku: str(row[lineCol("SKU")]),
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
  assertColumns(payload.orders, payload.columns?.orders, DELIVERY_ORDER_COLUMNS, "交期台账 orders");
  assertColumns(payload.lines, payload.columns?.lines, DELIVERY_LINE_COLUMNS, "交期台账 lines");
  const orderCol = (name: string) => col(DELIVERY_ORDER_COLUMNS, name);
  const lineCol = (name: string) => col(DELIVERY_LINE_COLUMNS, name);
  return {
    meta: payload.meta,
    dict: payload.dict,
    orders: payload.orders.map((row, index) => ({
      index,
      no: str(row[orderCol("采购单号")]),
      date: str(row[orderCol("采购日期")]),
      confirmed: num(row[orderCol("已确认")]) === 1,
      buyer: num(row[orderCol("采购员")]),
      supplier: num(row[orderCol("供应商")]),
      warehouse: num(row[orderCol("仓储方")]),
      externalNo: str(row[orderCol("外部单号")]),
      auditDate: str(row[orderCol("审核日期")]),
    })),
    lines: payload.lines.map((row) => ({
      order: num(row[lineCol("采购单下标")]),
      spu: num(row[lineCol("SPU")]),
      sku: str(row[lineCol("SKU")]),
      color: num(row[lineCol("颜色")]),
      spec: str(row[lineCol("规格")]),
      cat: num(row[lineCol("品类")]),
      qty: num(row[lineCol("数量")]),
      inQty: num(row[lineCol("入库")]),
      deliveryDate: str(row[lineCol("交期")]),
      eta: str(row[lineCol("预计到货")]),
      amount: num(row[lineCol("金额")]),
    })),
  };
}

/** 字典取名，越界回退到占位符而不是 undefined。 */
export function name(dict: string[] | undefined, index: number, fallback = "—"): string {
  const value = dict?.[index];
  return value == null || value === "" ? fallback : value;
}
