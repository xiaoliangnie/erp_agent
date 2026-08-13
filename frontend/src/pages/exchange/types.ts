/** 换货任务状态机，与 backend/exchange/service.py 一致。 */
export type JobStatus =
  | "pending"
  | "planning"
  | "awaiting_confirm"
  | "confirmed"
  | "executing"
  | "done"
  | "failed"
  | "cancelled";

export const STATUS_LABELS: Record<JobStatus, string> = {
  pending: "等待 Worker",
  planning: "正在读取订单",
  awaiting_confirm: "等待人工确认",
  confirmed: "已确认待执行",
  executing: "正在换货",
  done: "已完成",
  failed: "失败",
  cancelled: "已取消",
};

/** 还能取消的状态。 */
export const CANCELLABLE: JobStatus[] = ["pending", "planning", "awaiting_confirm", "confirmed"];

export interface PlanRow {
  o_id: string;
  so_id?: string;
  src_sku_id?: string;
  new_sku_id?: string;
  source_sku?: string;
  target_sku?: string;
  qty?: number | string;
  ok?: boolean;
  mode?: string;
  reason?: string;
  exchange_type?: "same_style" | "special_mapping" | "unknown";
  source_style?: string;
  target_style?: string;
  warning?: string;
}

export interface JobPlan {
  total: number;
  exchangeable: number;
  skipped: number;
  plans: PlanRow[];
}

export interface ProgressRow {
  o_id: string;
  status: string;
  error?: string;
}

export interface JobResult {
  succeeded?: string[];
  failed?: string[];
}

export interface ExchangeJob {
  id: string;
  status: JobStatus;
  operator: string;
  rules: {
    strategy: string;
    replacements: {
      from: string;
      to: string;
      exchangeType?: "same_style" | "special_mapping";
      sourceStyle?: string;
      targetStyle?: string;
      policyName?: string;
    }[];
  };
  targets: { o_ids: string[]; limit: number };
  plan: JobPlan | null;
  progress: ProgressRow[];
  result: JobResult | null;
  workerId: string | null;
  error: string | null;
  createdAt: string;
}

export interface ExchangeStatus {
  workers: { workerId: string; pageUrl: string; version: string; online: boolean; ready: boolean }[];
  onlineWorkers: number;
  jobs: Record<string, number>;
}

export interface ExchangeProduct {
  sku: string;
  styleCode?: string;
  name: string;
  properties?: string;
}

export interface ExchangeOrder {
  oId: string;
  platformOrderNo?: string;
  orderDate?: string;
  status?: string;
  shopName?: string;
  buyer?: string;
}

export interface ExchangeOrderSearch {
  configured: boolean;
  source: "database" | "unconfigured" | "partial";
  message: string;
  orders: ExchangeOrder[];
}

export interface ExchangePolicyMapping {
  name: string;
  sourceSku: string;
  sourceStyle: string;
  targetStyle: string;
  targetSkus: string[];
}

export interface ExchangePolicy {
  defaultPolicy: "same_style";
  specialMappings: ExchangePolicyMapping[];
}

export interface ExchangeOrderItem extends ExchangeProduct {
  orderCount: number;
  totalQuantity: number;
}

export interface ExchangeOrderItems {
  configured: boolean;
  source: "database" | "unconfigured" | "partial";
  message: string;
  selectedOrderCount: number;
  items: ExchangeOrderItem[];
}
