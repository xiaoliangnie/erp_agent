import { useCallback, useEffect, useMemo, useState } from "react";
import { TopBar } from "../../components/TopBar";
import { LoadFailed, Loading } from "../../components/PageState";
import { errorText, publicApi } from "../../api/client";
import { useAuth } from "../../auth/AuthContext";
import { useServerClock } from "../../hooks/useServerClock";
import { int } from "../../lib/format";
import { WorkbenchPanel } from "../workbench/WorkbenchPanel";
import type { HealthPayload, ScheduleRow, ScheduleState, ServiceChip, SourceCard } from "./types";
import "../chat/chat.css";
import "../workbench/workbench.css";
import "./status.css";

const POLL_MS = 15000;

const STATE_LABEL: Record<ScheduleState, string> = {
  ok: "正常",
  due: "到点",
  late: "今日未执行",
  error: "出错",
  off: "未启用",
};

function formatDue(seconds: number | null): string {
  if (seconds == null) return "—";
  if (seconds <= 0) return "已到点";
  if (seconds < 60) return `${seconds} 秒`;
  if (seconds < 3600) {
    const minutes = Math.floor(seconds / 60);
    const rest = seconds % 60;
    return rest ? `${minutes} 分 ${rest} 秒` : `${minutes} 分`;
  }
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  return minutes ? `${hours} 小时 ${minutes} 分` : `${hours} 小时`;
}

function ranLabel(row: ScheduleRow): string {
  if (!row.enabled) return "关闭";
  if (row.ranToday === true) return "今日已跑";
  if (row.ranToday === false) return row.state === "late" ? "今日未跑" : "尚未到点";
  return row.running ? "在跑" : "待机";
}

function serviceChips(health: HealthPayload): ServiceChip[] {
  const stream = health.dingtalk?.stream ?? {};
  const erp = health.erpWorker ?? {};
  const keep = erp.keepAlive ?? {};
  return [
    { id: "db", label: "数据库", ok: health.ok && health.database === "connected", detail: health.database || "" },
    {
      id: "mirror",
      label: "镜像同步",
      ok: Boolean(health.realtimeMirror?.enabled) && !health.realtimeMirror?.lastError,
      detail: health.syncedAt ? `最近 ${health.syncedAt}` : "",
    },
    {
      id: "stream",
      label: "钉钉 Stream",
      ok: Boolean(stream.running),
      detail: stream.lastError || (stream.running ? "已连接" : "未连接"),
    },
    {
      id: "agent",
      label: "采购助手",
      ok: Boolean(health.agent?.available),
      detail: health.agent?.available ? `${health.agent.tools ?? 0} 个工具` : "未启用",
    },
    {
      id: "erp",
      label: "ERP 登录态",
      ok: Boolean(erp.running && keep.warmed && !keep.lastError),
      detail: keep.lastOk ? `保活 ${keep.lastOk}` : (erp.lastError || ""),
    },
    {
      id: "jobs",
      label: "任务队列",
      ok: Boolean(health.jobs?.running) && !health.jobs?.lastError,
      detail: `排队 ${health.jobs?.queued ?? 0}`,
    },
  ];
}

function sourceLine(source: SourceCard | undefined, fallback: HealthPayload): string {
  const card = source ?? ({} as SourceCard);
  const bits = [
    `数据源 ${card.name || "供应链 API 本地实时镜像"}`,
    card.queriedAt ? `查询于 ${card.queriedAt}` : "",
    (card.syncedAt || fallback.syncedAt) ? `最近同步 ${card.syncedAt || fallback.syncedAt}` : "",
    card.minDate && card.maxDate ? `业务日期 ${card.minDate} ~ ${card.maxDate}` : "",
    card.orders != null && card.rows != null
      ? `${int(Number(card.orders))} 单 / ${int(Number(card.rows))} 行明细`
      : "看板尚未查询，单数待第一次打开采购看板后出现",
  ];
  if (card.warning) bits.push(card.warning);
  return bits.filter(Boolean).join(" · ");
}

export default function StatusPage() {
  const { loggedIn } = useAuth();
  const [health, setHealth] = useState<HealthPayload | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const clock = useServerClock();

  const load = useCallback(async (signal?: AbortSignal) => {
    const next = await publicApi.get<HealthPayload>("/api/health", { signal });
    setHealth(next);
    setError("");
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    load(controller.signal)
      .catch((caught: unknown) => {
        if (controller.signal.aborted) return;
        setError(errorText(caught));
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    const timer = window.setInterval(() => {
      load().catch((caught: unknown) => setError(errorText(caught)));
    }, POLL_MS);
    return () => {
      controller.abort();
      window.clearInterval(timer);
    };
  }, [load]);

  const chips = useMemo(() => (health ? serviceChips(health) : []), [health]);
  const schedules = health?.schedules ?? [];

  if (loading && !health) return <Loading label="正在读取运行状态…" />;
  if (error && !health) return <LoadFailed title="读不到运行状态" message={error} onRetry={() => void load()} />;
  if (!health) return <LoadFailed title="读不到运行状态" message="接口没有返回数据。" onRetry={() => void load()} />;

  return (
    <>
      <TopBar
        title="工作台"
        sub={[clock.ready ? `现在 ${clock.now}` : "", sourceLine(health.source, health)].filter(Boolean).join(" · ")}
      />
      <div className="status-page">
        {error ? <p className="small status-note">{error}（仍显示上一轮）</p> : null}

        {loggedIn ? <WorkbenchPanel /> : null}

        <section className="status-chips" aria-label="服务">
          {chips.map((chip) => (
            <article key={chip.id} className={`status-chip ${chip.ok ? "is-ok" : "is-bad"}`}>
              <p className="eyebrow">{chip.ok ? "正常" : "异常"}</p>
              <h2>{chip.label}</h2>
              <p className="small">{chip.detail || "—"}</p>
            </article>
          ))}
        </section>

        <section className="status-table-wrap">
          <h2>定时任务</h2>
          <p className="small">看今日有没有跑、下次还有多久。15 秒刷新一次。</p>
          <table className="status-table">
            <thead>
              <tr>
                <th>任务</th>
                <th>分组</th>
                <th>状态</th>
                <th>今日</th>
                <th>上次</th>
                <th>下次</th>
                <th className="num">还要多久</th>
              </tr>
            </thead>
            <tbody>
              {schedules.map((row) => (
                <tr key={row.id} className={`is-${row.state}`}>
                  <td>
                    <strong>{row.label}</strong>
                    <div className="small">{row.detail || (row.running ? "线程在跑" : "")}</div>
                    {row.lastError ? <div className="small status-error">{row.lastError}</div> : null}
                  </td>
                  <td>{row.group}</td>
                  <td>{STATE_LABEL[row.state]}</td>
                  <td>{ranLabel(row)}</td>
                  <td className="num">{row.lastRun || "—"}</td>
                  <td className="num">{row.nextRun || "—"}</td>
                  <td className="num">{formatDue(row.dueInSeconds)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>
      </div>
    </>
  );
}
