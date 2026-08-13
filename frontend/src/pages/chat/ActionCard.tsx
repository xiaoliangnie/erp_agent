import { useState } from "react";
import { errorText, fetchBlob, openBlob } from "../../api/client";
import type { Credentials } from "../../api/client";
import type { ExecutedAction, PendingAction } from "./types";

interface PendingCardProps {
  action: PendingAction;
  onDecide: (actionId: string, decision: "confirm" | "cancel") => Promise<void>;
}

/**
 * L1/L2 动作卡：确认前只展示要点。确认人必须是发起人，服务端会再校验一次，
 * 这里禁用按钮只是防手抖重复点。
 */
export function PendingCard({ action, onDecide }: PendingCardProps) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  async function decide(decision: "confirm" | "cancel") {
    if (decision === "confirm" && !window.confirm("确认执行该动作？它会真正生成产物或对外发送。")) return;
    setBusy(true);
    setError("");
    try {
      await onDecide(action.id, decision);
    } catch (caught) {
      setError(errorText(caught));
      setBusy(false);
    }
  }

  return (
    <div className="action">
      <h3>{action.title}</h3>
      <div className="risk">
        {action.risk} · 需要你确认后才执行 · {action.expiresAt} 前有效
      </div>
      <pre>{JSON.stringify(action.preview, null, 2)}</pre>
      <div className="action-row">
        <button type="button" className="btn primary" disabled={busy} onClick={() => decide("confirm")}>
          确认执行
        </button>
        <button type="button" className="btn danger" disabled={busy} onClick={() => decide("cancel")}>
          取消
        </button>
      </div>
      {error ? <div className="status error">{error}</div> : null}
    </div>
  );
}

interface ExecutedCardProps {
  executed: ExecutedAction;
  auth: Credentials;
}

export function ExecutedCard({ executed, auth }: ExecutedCardProps) {
  const [error, setError] = useState("");
  const result = executed.result ?? {};

  // 产物接口要 Bearer，不能直接给 <a href>/<img src>，先取 blob 再开。
  async function open(path: string, fileName?: string) {
    try {
      openBlob(await fetchBlob(path, auth), fileName);
    } catch (caught) {
      setError(errorText(caught));
    }
  }

  return (
    <div className="action">
      <h3>{executed.title}</h3>
      <div className="risk">{executed.status === "executed" ? "已执行" : `已${executed.status}`}</div>
      <pre>{JSON.stringify(result, null, 2)}</pre>
      {result.downloadUrl || result.previewUrl ? (
        <div className="action-row">
          {result.downloadUrl ? (
            <button type="button" className="btn" onClick={() => open(result.downloadUrl!, result.fileName ?? "contract.xlsx")}>
              下载 Excel
            </button>
          ) : null}
          {result.previewUrl ? (
            <button type="button" className="btn" onClick={() => open(result.previewUrl!)}>
              查看预览
            </button>
          ) : null}
        </div>
      ) : null}
      {error ? <div className="status error">{error}</div> : null}
    </div>
  );
}
