import type { ExecutedAction, PendingAction } from "../chat/types";

export interface WorkItemSummary {
  actionId?: string;
  expiresAt?: string;
  preview?: unknown;
  jobId?: string;
  href?: string;
  sourceSku?: string;
  targetSku?: string;
  orderCount?: number;
  supplier?: string;
  poId?: string;
  issueId?: string;
  error?: string;
  [key: string]: unknown;
}

export interface WorkItem {
  id: string;
  kind: string;
  sourceTable: string;
  sourceId: string;
  status: string;
  title: string;
  operator: string;
  userId: string;
  tool: string;
  risk: string;
  summary: WorkItemSummary;
  createdAt: string;
  updatedAt: string;
  actionId?: string;
  action?: PendingAction | null;
}

export interface WorkbenchJob {
  id: string;
  kind: string;
  status: string;
  error?: string;
  attempts?: number;
  createdAt: string;
}

export interface WorkbenchOutboxItem {
  id: string;
  kind: string;
  status: string;
  channel: string;
  title: string;
  attempts: number;
  error: string;
  createdAt: string;
  deliveredAt?: string;
  duplicatePossible?: boolean;
}

export interface WorkbenchPayload {
  items: WorkItem[];
  jobs: WorkbenchJob[];
  outbox: {
    pending: number;
    recent: WorkbenchOutboxItem[];
  };
}

export type { ExecutedAction, PendingAction };
