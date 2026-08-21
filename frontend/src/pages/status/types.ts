export type ScheduleState = "ok" | "due" | "late" | "error" | "off";

export interface SourceCard {
  name: string;
  queriedAt?: string;
  syncedAt?: string;
  syncLagMinutes?: number | null;
  fresh?: boolean;
  year?: string;
  minDate?: string;
  maxDate?: string;
  orders?: number | null;
  rows?: number | null;
  warning?: string;
  sourceStatus?: string;
  today?: string;
}

export interface ScheduleRow {
  id: string;
  label: string;
  group: string;
  enabled: boolean;
  running: boolean;
  state: ScheduleState;
  detail: string;
  lastRun: string;
  nextRun: string;
  dueInSeconds: number | null;
  ranToday: boolean | null;
  lastError: string;
}

export interface ServiceChip {
  id: string;
  label: string;
  ok: boolean;
  detail: string;
}

export interface HealthPayload {
  ok: boolean;
  now?: string;
  today?: string;
  database?: string;
  syncedAt?: string;
  syncLagMinutes?: number | null;
  source?: SourceCard;
  schedules?: ScheduleRow[];
  realtimeMirror?: { enabled?: boolean; running?: boolean; lastError?: string };
  agent?: { enabled?: boolean; available?: boolean; tools?: number };
  erpWorker?: {
    enabled?: boolean;
    running?: boolean;
    browserOpen?: boolean;
    lastError?: string;
    keepAlive?: { running?: boolean; warmed?: boolean; lastOk?: string; lastError?: string };
  };
  jobs?: { enabled?: boolean; running?: boolean; queued?: number; lastError?: string };
  dingtalk?: {
    stream?: { enabled?: boolean; running?: boolean; lastError?: string };
    sender?: { app?: boolean; oto?: boolean };
  };
  insoleSchedule?: { enabled?: boolean; running?: boolean; lastError?: string };
  dropship?: { enabled?: boolean; running?: boolean; lastError?: string };
  spuSnapshot?: { enabled?: boolean; running?: boolean; lastError?: string };
  gbStandards?: { enabled?: boolean; running?: boolean; lastError?: string };
}
