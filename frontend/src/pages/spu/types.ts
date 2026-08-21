/** /api/spu/summary 的行；字段名与后端 load_style_snapshot 一致。 */
export interface SpuStyle {
  styleId: string;
  name: string;
  /** 最近一笔采购的供应商简称 */
  lastSupplier?: string;
  categoryLine: string;
  skuCount: number;
  sales1: number;
  sales7: number;
  sales14?: number;
  sales15?: number;
  salesPrev7: number;
  wowRatio: number | null;
  /** 趋势线逐日出库，旧 → 新；百货近 30 天 */
  salesDaily: number[];
  sales30: number;
  sales60: number;
  /** 近 90 天出库；鞋服结果表可能为 0 */
  sales90?: number;
  /** 百货：按店铺设置分组拆的线上/线下出库 */
  sales7Online?: number;
  sales7Offline?: number;
  sales15Online?: number;
  sales15Offline?: number;
  sales30Online?: number;
  sales30Offline?: number;
  sales60Online?: number;
  sales60Offline?: number;
  sales90Online?: number;
  sales90Offline?: number;
  /** 百货：(7×4 + 15×2 + 30) / 3 */
  monthlySales?: number | null;
  /** 百货：本款出库对上的真实店铺，按近30天件数排序 */
  saleShops?: SaleShop[];
  dailyAvg: number;
  turnoverDays: number | null;
  stockout: boolean;
  brokenSkus: number;
  shortSkus: number;
  onHand: number;
  qty: number;
  occupy: number;
  inbound: number;
  /** 进货仓库存；不进总库存 */
  inQty?: number;
  replenishQty: number | null;
  /** 建议下单：补货建议向上取整（有起订量按起订量倍数，否则 10 的倍数） */
  orderQty: number | null;
  /** 商品备注解析出的起订量；同款多值取最大 */
  moq: number | null;
  remark: string;
  year?: string;
  season?: string;
  category?: string;
  labels?: string[];
}

export interface SaleShop {
  shopId: string;
  shopName: string;
  groupName: string;
  channel: "online" | "offline";
  qty7?: number;
  qty15?: number;
  qty30: number;
}

export interface SpuSummary {
  ok: boolean;
  board?: "apparel" | "baihuo";
  computedAt: string;
  styleCount: number;
  stockoutCount: number;
  brokenStyleCount: number;
  shortStyleCount: number;
  styles: SpuStyle[];
  refreshing: boolean;
  refresh: { startedAt: string; finishedAt: string; lastError: string };
  analyses?: Record<string, AnalyzePayload>;
}

export interface AnalyzePayload {
  analysis?: string;
  analyzedAt?: string;
  stale?: boolean;
  cached?: boolean;
}

export interface SpuAnalysis {
  status: "idle" | "loading" | "done" | "stale" | "error";
  text: string;
  error: string;
  analyzedAt?: string;
  stale?: boolean;
}

export const wowText = (value: number | null): string =>
  value === null ? "—" : `${value > 0 ? "+" : ""}${(value * 100).toFixed(1)}%`;

export const turnoverText = (value: number | null): string =>
  value === null ? "—" : value.toFixed(1);

/** 百货月销量 = (7天×4 + 15天×2 + 30天) / 3 */
export function monthlySalesBaihuo(sales7: number, sales15: number, sales30: number): number {
  return (Math.max(0, sales7) * 4 + Math.max(0, sales15) * 2 + Math.max(0, sales30)) / 3;
}

export function sparkValues(style: SpuStyle, board: "apparel" | "baihuo"): number[] {
  const values = style.salesDaily ?? [];
  return board === "baihuo" ? values.slice(-30) : values;
}
