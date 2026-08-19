import { useCallback, useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { TopBar } from "../../components/TopBar";
import { agentApi, errorText } from "../../api/client";
import { useCredentials } from "../../hooks/useCredentials";
import { ROUTES } from "../../routes";
import { ExecutedCard, PendingCard } from "../chat/ActionCard";
import type { ExecutedAction } from "../chat/types";
import type { WorkbenchPayload, WorkItem } from "./types";
import "../chat/chat.css";
import "./workbench.css";

const KIND_LABEL: Record<string, string> = {
  pending_action: "待确认",
  exchange_job: "换货任务",
  quality_issue: "品控",
};

const STATUS_LABEL: Record<string, string> = {
  open: "待处理",
  in_progress: "执行中",
  failed: "失败",
  resolved: "已完成",
  cancelled: "已取消",
  expired: "已超时",
};

function qualityIssueId(item: WorkItem): string {
  return String(item.summary.issueId || item.sourceId || "").trim();
}

function previewText(item: WorkItem): string {
  const preview = item.action?.preview ?? item.summary.preview;
  if (preview == null) {
    const bits = [
      item.summary.sourceSku && item.summary.targetSku
        ? `${item.summary.sourceSku} → ${item.summary.targetSku}`
        : "",
      item.summary.supplier || "",
      item.summary.poId || "",
      item.summary.error || "",
    ].filter(Boolean);
    return bits.join("\n") || "没有更多要点";
  }
  return JSON.stringify(preview, null, 2);
}

export default function WorkbenchPage() {
  const { credentials, update, ensureBound, filled, bound } = useCredentials("agent");
  const [payload, setPayload] = useState<WorkbenchPayload | null>(null);
  const [executed, setExecuted] = useState<Record<string, ExecutedAction>>({});
  const [message, setMessage] = useState("");
  const [loading, setLoading] = useState(false);
  const credentialsRef = useRef(credentials);
  credentialsRef.current = credentials;

  const load = useCallback(async () => {
    if (!filled) throw new Error("请填写姓名，以及钉钉私信里的 20 位网页身份码");
    const auth = await ensureBound();
    setLoading(true);
    try {
      const query = new URLSearchParams({ operator: auth.operator.trim() });
      setPayload(await agentApi.get<WorkbenchPayload>(`/api/agent/workbench?${query}`, auth));
      setMessage("");
    } finally {
      setLoading(false);
    }
  }, [ensureBound, filled]);

  const bootstrapped = useRef(false);
  const autoConnect = useRef(bound && filled);
  useEffect(() => {
    if (bootstrapped.current || !autoConnect.current) return;
    bootstrapped.current = true;
    load().catch((error: unknown) => setMessage(errorText(error)));
  }, [load]);

  const decide = useCallback(async (actionId: string, decision: "confirm" | "cancel") => {
    const auth = await ensureBound();
    credentialsRef.current = auth;
    const done = await agentApi.post<ExecutedAction>(
      `/api/agent/actions/${actionId}/${decision}`,
      { operator: auth.operator.trim() },
      auth,
    );
    setExecuted((current) => ({ ...current, [actionId]: done }));
    await load().catch(() => undefined);
  }, [ensureBound, load]);

  const decideQuality = useCallback(async (issueId: string, decision: "resolve" | "cancel") => {
    setMessage("");
    const auth = await ensureBound();
    credentialsRef.current = auth;
    await agentApi.post(
      `/api/agent/quality/${issueId}/${decision}`,
      { operator: auth.operator.trim() },
      auth,
    );
    await load().catch(() => undefined);
  }, [ensureBound, load]);

  const items = payload?.items ?? [];
  const confirmable = items.filter((item) => (
    (item.kind === "pending_action" && item.action?.id && !executed[item.action.id])
    || (item.kind === "quality_issue" && item.status === "open")
  ));

  return (
    <>
      <TopBar title="工作台" sub="待确认、换货任务和品控待办集中在这里，状态与对话里是同一份" />
      <main className="workbench">
        <section className="panel">
          <div className="panel-head">
            <strong>连接</strong>
            <small>与采购助手共用姓名和网页身份，共享 Token 可留空</small>
          </div>
          <div className="credentials-grid" style={{ marginTop: 12 }}>
            <input
              type="password"
              autoComplete="off"
              placeholder="AGENT_API_TOKEN（可选）"
              value={credentials.token}
              onChange={(event) => update({ token: event.target.value })}
            />
            <input
              autoComplete="off"
              placeholder="钉钉/采购员姓名"
              value={credentials.operator}
              onChange={(event) => update({ operator: event.target.value })}
            />
            {bound ? null : (
              <input
                autoComplete="off"
                placeholder="钉钉私信 20 位网页身份码"
                value={credentials.bindCode ?? ""}
                onChange={(event) => update({ bindCode: event.target.value })}
              />
            )}
            <button
              type="button"
              className="btn"
              disabled={loading}
              onClick={() => load().catch((error: unknown) => setMessage(errorText(error)))}
            >
              {loading ? "刷新中…" : "刷新"}
            </button>
          </div>
          {message ? <div className="status error">{message}</div> : null}
        </section>

        <section className="panel">
          <div className="panel-head">
            <strong>待我处理</strong>
            <small>{confirmable.length} 条可处理 · 共 {items.length} 条</small>
          </div>
          {!payload ? (
            <div className="empty">连接后加载待办</div>
          ) : items.length === 0 ? (
            <div className="empty">当前没有待办。对话里生成的确认会出现在这里。</div>
          ) : (
            items.map((item) => {
              const action = item.action;
              const done = action?.id ? executed[action.id] : undefined;
              return (
                <article
                  key={item.id}
                  className={`workbench-item${item.status === "open" ? " is-open" : ""}${item.status === "failed" ? " is-failed" : ""}`}
                >
                  <h3>{item.title}</h3>
                  <div className="workbench-meta">
                    <span>{KIND_LABEL[item.kind] || item.kind}</span>
                    <span>{STATUS_LABEL[item.status] || item.status}</span>
                    {item.risk ? <span>{item.risk}</span> : null}
                    {item.operator ? <span>{item.operator}</span> : null}
                    <span className="mono">{item.updatedAt}</span>
                  </div>
                  {done ? (
                    <ExecutedCard executed={done} auth={credentials} />
                  ) : action ? (
                    <PendingCard
                      action={action}
                      onDecide={(actionId, decision) => decide(actionId, decision)}
                    />
                  ) : (
                    <>
                      <pre className="workbench-preview">{previewText(item)}</pre>
                      {item.kind === "quality_issue" && item.status === "open" ? (
                        <div className="action-row">
                          <button
                            type="button"
                            className="btn primary"
                            disabled={loading}
                            onClick={() => decideQuality(qualityIssueId(item), "resolve")
                              .catch((error: unknown) => setMessage(errorText(error)))}
                          >
                            关闭
                          </button>
                          <button
                            type="button"
                            className="btn danger"
                            disabled={loading}
                            onClick={() => decideQuality(qualityIssueId(item), "cancel")
                              .catch((error: unknown) => setMessage(errorText(error)))}
                          >
                            撤销
                          </button>
                        </div>
                      ) : null}
                      {item.kind === "exchange_job" ? (
                        <div className="action-row">
                          <Link className="btn" to={ROUTES.exchange}>去换货页二次确认</Link>
                        </div>
                      ) : null}
                    </>
                  )}
                </article>
              );
            })
          )}
        </section>

        <section className="panel workbench-side">
          <div className="panel-head">
            <strong>出站与后台任务</strong>
            <small>发送失败会留在这里补发，不重跑工具</small>
          </div>
          <div className="workbench-row">
            <span>待补发钉钉</span>
            <b>{payload?.outbox.pending ?? 0}</b>
          </div>
          {(payload?.outbox.recent ?? []).map((item) => (
            <div key={item.id} className="workbench-row">
              <span>{item.title || item.kind} · {item.status}{item.duplicatePossible ? " · 可能重复" : ""}</span>
              <span className="mono">{item.createdAt}</span>
            </div>
          ))}
          {(payload?.jobs ?? []).slice(0, 8).map((job) => (
            <div key={job.id} className="workbench-row">
              <span>{job.kind} · {job.status}{job.error ? ` · ${job.error}` : ""}</span>
              <span className="mono">{job.createdAt}</span>
            </div>
          ))}
        </section>
      </main>
    </>
  );
}
