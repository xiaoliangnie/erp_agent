import type { ExchangeJob, JobStatus } from "./types";
import { CANCELLABLE, STATUS_LABELS } from "./types";

interface JobCardProps {
  job: ExchangeJob;
  open: boolean;
  onToggle: () => void;
  onAct: (jobId: string, action: "confirm" | "cancel") => void;
}

function PlanTable({ job }: { job: ExchangeJob }) {
  const plan = job.plan;
  if (!plan) return <div className="small">等待 ERP Worker 返回真实订单试算结果。</div>;
  const replacement = job.rules.replacements[0];
  const typeLabel = (value?: string) => value === "same_style" ? "同款式换货" : value === "special_mapping" ? "特殊白名单映射" : "规则待核对";
  return (
    <>
      <div className="small">
        共 {plan.total} 单 · 可换 {plan.exchangeable} · 跳过 {plan.skipped}
      </div>
      <table className="plan">
        <thead>
          <tr>
            <th>o_id / 平台单号</th>
            <th>源 → 目标</th>
            <th className="n">数量</th>
            <th>模式 / 结果</th>
          </tr>
        </thead>
        <tbody>
          {plan.plans.map((row) => (
            <tr key={row.o_id}>
              <td>
                <span className="mono">{row.o_id}</span>
                {row.so_id ? <div className="small">{row.so_id}</div> : null}
              </td>
              <td>
                <div>{row.src_sku_id ?? row.source_sku ?? replacement.from} → {row.new_sku_id ?? row.target_sku ?? replacement.to}</div>
                <div className={`small exchange-risk ${row.exchange_type ?? "unknown"}`}>
                  {typeLabel(row.exchange_type)}
                  {row.source_style || row.target_style ? ` · ${row.source_style || "?"} → ${row.target_style || "?"}` : ""}
                </div>
                {row.warning ? <div className="small risk-warning">{row.warning}</div> : null}
              </td>
              <td className="n">{row.qty ?? "—"}</td>
              <td className={row.ok ? "ok" : "skip"}>{row.ok ? (row.mode ?? "可换") : (row.reason ?? "跳过")}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </>
  );
}

function oidLabel(value: unknown): string {
  if (typeof value === "string" || typeof value === "number") return String(value);
  if (value && typeof value === "object" && "o_id" in value) {
    return String((value as { o_id: unknown }).o_id ?? "");
  }
  return "";
}

function Progress({ job }: { job: ExchangeJob }) {
  const succeeded = (job.result?.succeeded ?? []).map(oidLabel).filter(Boolean);
  const failed = (job.result?.failed ?? []).map(oidLabel).filter(Boolean);
  if (!job.progress.length && !job.result && job.status !== "stuck") return null;
  return (
    <div className="notice">
      {job.status === "stuck" ? (
        <div>执行已中断，未自动重投。请先核对 ERP 里哪些订单已经改过。</div>
      ) : null}
      {job.progress.length ? (
        job.progress.map((row) => (
          <div key={row.o_id}>
            {row.o_id}：{row.status === "success" ? "成功" : `失败 ${row.error ?? ""}`}
          </div>
        ))
      ) : null}
      {job.result ? (
        <div className="small" style={{ marginTop: 7 }}>
          <div>完成：{succeeded.length}，失败：{failed.length}</div>
          {succeeded.length ? <div>已改 ERP：{succeeded.join("、")}</div> : null}
          {failed.length ? <div>失败订单：{failed.join("、")}</div> : null}
        </div>
      ) : null}
    </div>
  );
}

export function JobCard({ job, open, onToggle, onAct }: JobCardProps) {
  const replacement = job.rules.replacements[0];
  const canCancel = CANCELLABLE.includes(job.status);
  const canConfirm = job.status === "awaiting_confirm" && (job.plan?.exchangeable ?? 0) > 0;
  const typeLabel = replacement.exchangeType === "same_style" ? "同款式换货" : replacement.exchangeType === "special_mapping" ? "特殊白名单映射" : "系统规则";

  return (
    <article className={`job${open ? " open" : ""}`}>
      <button type="button" className="job-head" onClick={onToggle} aria-expanded={open}>
        <span className={`pill ${job.status}`}>{STATUS_LABELS[job.status as JobStatus] ?? job.status}</span>
        <span className="mono job-id">{job.id}</span>
        <span className="small">
          {typeLabel} · {replacement.from} → {replacement.to} · {job.targets.o_ids.length} 单
        </span>
      </button>
      {open ? (
        <div className="job-body">
          <PlanTable job={job} />
          <Progress job={job} />
          {job.error ? <div className="notice">{job.error}</div> : null}
          {canCancel || canConfirm ? (
            <div className="job-actions">
              {canCancel ? (
                <button type="button" className="btn danger" onClick={() => onAct(job.id, "cancel")}>
                  取消
                </button>
              ) : null}
              {canConfirm ? (
                <button type="button" className="btn primary" onClick={() => onAct(job.id, "confirm")}>
                  确认执行 {job.plan?.exchangeable} 单
                </button>
              ) : null}
            </div>
          ) : null}
          <div className="small">
            创建：{job.operator} · {job.createdAt}
            {job.workerId ? ` · Worker ${job.workerId}` : ""}
            {job.attempts ? ` · 已回收 ${job.attempts} 次` : ""}
          </div>
        </div>
      ) : null}
    </article>
  );
}
