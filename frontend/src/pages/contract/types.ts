/** 票种只有这三种，税率默认 0 / 0 / 13，员工可覆盖。 */
export type InvoiceType = "no_invoice" | "normal_invoice" | "special_invoice";

export const INVOICE_LABELS: Record<InvoiceType, string> = {
  no_invoice: "不开票",
  normal_invoice: "普票",
  special_invoice: "专票",
};

export const DEFAULT_RATES: Record<InvoiceType, number> = {
  no_invoice: 0,
  normal_invoice: 0,
  special_invoice: 13,
};

/** 付款方式条款：label 只在下拉里显示，写进合同的是 text。 */
export interface PaymentOption {
  key: string;
  label: string;
  text: string;
}

export interface GbOption {
  samrId: string;
  standardNo: string;
  nameCn: string;
  status: string;
  nature: string;
  stdType: string;
  recommended?: boolean;
  recommendReason?: string;
}

export interface ContractItem {
  poiId: string;
  sku: string;
  styleCode: string;
  name: string;
  specification: string;
  category: string;
  /** 合同单位：products.json 优先，否则镜像商品资料。 */
  unit?: string;
  /** 国标码（商品条码）；与执行标准不是同一列。 */
  nationalCode?: string;
  deliveryDate: string;
  quantity: number;
  inQuantity: number;
  erpPrice: number;
  /** config/products.json 里维护的三类票种价格。 */
  prices: Partial<Record<InvoiceType, number>>;
  hasImage: boolean;
  imageStatus: "ready" | "missing" | "failed";
  imageSource: string;
  imageError: string;
  /** 国标目录候选；与 Excel「国标码」（商品条码）不是同一列。 */
  gbOptions: GbOption[];
  gbStandard: string;
  /** 同一供应商同一 SKU 的历史采购价，最近的在前，不含本单。 */
  priceHistory: PriceHistoryEntry[];
  /** ERP 备注原文；合同页展示，并可能解析出备注单价。 */
  remark?: string;
  /** 备注里第一个数字，如「包体32+2个魔术贴标3.45」→ 32。 */
  remarkPrice?: number | null;
}

export interface PriceHistoryEntry {
  price: number;
  quantity: number;
  poId: string;
  date: string;
}

export interface ContractOptions {
  purchaseOrderNo: string;
  orderDate: string;
  deliveryDate: string;
  status: string;
  purchaser: string;
  receiveAddress: string;
  warehouse: string;
  /** 预选条款的正文；没预选时为空。 */
  paymentMethod: string;
  paymentOptions: PaymentOption[];
  /** 预选的条款键：上次用过的优先，其次按 ERP 付款方式；都没有则为空。 */
  paymentOption: string;
  paymentSource: "" | "history" | "erp";
  paymentNote: string;
  /** 这家供应商上次实际写进合同的条款正文。 */
  lastPaymentText: string;
  supplierShortName: string;
  /** 本机供应商表命中且字段齐全、未冻结，或内部往来户，才允许生成合同。 */
  supplierMapped: boolean;
  /** 公司内部户：不列收付款信息，不要求 Excel 全称/账户。 */
  supplierInternal: boolean;
  supplierFrozen: boolean;
  supplierMissingFields: string[];
  supplierIssue: string;
  supplierLegalName: string;
  /** 主数据里的发票类型原文，如 专用发票(13%)；内部户为「内部往来」。 */
  supplierInvoiceLabel: string;
  supplierBankAccountName?: string;
  supplierBankName?: string;
  supplierBankAccount?: string;
  /** 合同表头「收货信息」默认文案，员工可改。 */
  receivingInfo?: string;
  /** 默认检验标准两条；手输追加从 3 起编号。 */
  inspectionStandards?: string;
  invoiceRates: Partial<Record<InvoiceType, number>>;
  erpPriceMode: InvoiceType | null;
  /** 内部户可用 ERP 单价，不要求 products.json 该票种价。 */
  useErpPrice: boolean;
  totalQuantity: number;
  items: ContractItem[];
}

export interface OrderChoice {
  purchaseOrderNo: string;
  orderDate: string;
  supplier: string;
  purchaser: string;
}

export interface ProductImageJob {
  id: string;
  purchaseOrderNo: string;
  status: "pending" | "syncing" | "done" | "failed";
  targets: { sku: string; style: string }[];
  progress: { sku: string; status: string }[];
  error: string | null;
}
