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

export interface GbOption {
  samrId: string;
  standardNo: string;
  nameCn: string;
  status: string;
  nature: string;
  stdType: string;
}

export interface ContractItem {
  poiId: string;
  sku: string;
  styleCode: string;
  name: string;
  specification: string;
  category: string;
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
}

export interface ContractOptions {
  purchaseOrderNo: string;
  orderDate: string;
  deliveryDate: string;
  status: string;
  purchaser: string;
  receiveAddress: string;
  warehouse: string;
  paymentMethod: string;
  supplierShortName: string;
  /** 供应商没维护完整映射就不许生成合同，不给占位信息。 */
  supplierMapped: boolean;
  supplierLegalName: string;
  invoiceRates: Partial<Record<InvoiceType, number>>;
  erpPriceMode: InvoiceType | null;
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
