import { useCallback, useEffect, useRef, useState } from "react";
import { TopBar } from "../../components/TopBar";
import { agentApi, errorText } from "../../api/client";
import { useCredentials } from "../../hooks/useCredentials";
import { newId } from "../../lib/id";
import { ExecutedCard, PendingCard } from "./ActionCard";
import type { AgentStatus, ChatReply, ExecutedAction, Message } from "./types";
import "./chat.css";

const SAMPLES = [
  { label: "今年采购概况", ask: "今年的采购金额、待入库和入库率各是多少？" },
  { label: "逾期催办清单", ask: "现在逾期的采购单有多少张，按采购员分别列出待入库件数。" },
  { label: "T-10 这一波", ask: "T-10 这一波有哪些单需要催？" },
  { label: "订单换货", ask: "把订单 11530151 里的 XZ25401308-101 换成 XZ25401308-09906" },
  { label: "抖音鞋垫", ask: "查询一下现在抖音需要更换的鞋垫订单，进行处理" },
];

const GREETING =
  "填好 Token、姓名，以及钉钉私信里的 20 位网页身份码后连接。助手只能通过固定工具查库和生成产物；生成合同、登记换货、发钉钉催办这类动作会先给出要点，等你点确认才执行。";

function readSessionKey(): string {
  const stored = sessionStorage.getItem("agentSessionKey");
  if (stored) return stored;
  const created = newId();
  sessionStorage.setItem("agentSessionKey", created);
  return created;
}

export default function ChatPage() {
  const { credentials, update, ensureBound, filled, bound } = useCredentials("agent");
  const [status, setStatus] = useState<AgentStatus | null>(null);
  const [messages, setMessages] = useState<Message[]>([
    { id: "greeting", role: "system", text: GREETING },
  ]);
  const [draft, setDraft] = useState("");
  const [sending, setSending] = useState(false);
  const [message, setMessage] = useState("");
  const sessionKey = useRef(readSessionKey());
  const sessionId = useRef("");
  const logRef = useRef<HTMLDivElement>(null);

  const credentialsRef = useRef(credentials);
  credentialsRef.current = credentials;

  useEffect(() => {
    const log = logRef.current;
    if (log) log.scrollTop = log.scrollHeight;
  }, [messages]);

  const connect = useCallback(async () => {
    if (!filled) throw new Error("请填写 Token、姓名和网页身份码");
    const auth = await ensureBound();
    setStatus(await agentApi.get<AgentStatus>("/api/agent/status", auth));
    setMessage("");
  }, [ensureBound, filled]);

  const bootstrapped = useRef(false);
  const autoConnectStoredCredentials = useRef(bound && filled);
  useEffect(() => {
    if (bootstrapped.current || !autoConnectStoredCredentials.current) return;
    bootstrapped.current = true;
    connect().catch((error: unknown) => setMessage(errorText(error)));
  }, [connect]);

  const send = useCallback(
    async (text: string) => {
      const question = text.trim();
      if (!question || sending) return;
      const auth = await ensureBound();
      credentialsRef.current = auth;
      const replyId = newId();
      setDraft("");
      setSending(true);
      setMessages((current) => [
        ...current,
        { id: newId(), role: "user", text: question },
        { id: replyId, role: "assistant", text: "正在查数据…", pending: true },
      ]);
      try {
        const answer = await agentApi.post<ChatReply>(
          "/api/agent/chat",
          { message: question, sessionKey: sessionKey.current, operator: auth.operator.trim() },
          auth,
        );
        sessionId.current = answer.sessionId;
        setMessages((current) =>
          current.map((item) =>
            item.id === replyId
              ? {
                  ...item,
                  pending: false,
                  text: answer.reply,
                  steps: answer.steps ?? [],
                  actions: answer.pendingActions ?? [],
                }
              : item,
          ),
        );
      } catch (error) {
        const text = errorText(error);
        setMessages((current) =>
          current.map((item) => (item.id === replyId ? { ...item, pending: false, error: true, text } : item)),
        );
      } finally {
        setSending(false);
      }
    },
    [ensureBound, sending],
  );

  const decide = useCallback(async (messageId: string, actionId: string, decision: "confirm" | "cancel") => {
    const auth = await ensureBound();
    credentialsRef.current = auth;
    const done = await agentApi.post<ExecutedAction>(
      `/api/agent/actions/${actionId}/${decision}`,
      { operator: auth.operator.trim() },
      auth,
    );
    setMessages((current) =>
      current.map((item) =>
        item.id === messageId
          ? {
              ...item,
              actions: (item.actions ?? []).filter((action) => action.id !== actionId),
              executed: { ...(item.executed ?? {}), [actionId]: done },
            }
          : item,
      ),
    );
  }, [ensureBound]);

  async function resetSession() {
    if (!window.confirm("开新话题？助手不再带着这次对话的上文；历史仍留在审计里。")) {
      return;
    }
    if (sessionId.current) {
      const auth = await ensureBound();
      credentialsRef.current = auth;
      await agentApi
        .post(`/api/agent/sessions/${sessionId.current}/reset`, {}, auth)
        .catch(() => undefined);
    }
    setMessages([{ id: newId(), role: "system", text: "已开新话题。此前对话仍可追查，助手不再带着上文。" }]);
  }

  const agent = status?.agent;
  const stateText = !status
    ? "尚未连接"
    : agent?.available
      ? `${agent.llm.model} · ${agent.tools.length} 个工具 · 最多 ${agent.maxToolSteps} 步`
      : "Agent 未启用：需在 .env 设置 AGENT_ENABLED=true 和模型密钥";

  return (
    <>
      <TopBar title="采购助手" sub="模型只选工具和组织话术，数字全部来自确定性代码" />
      <main className="chat-layout">
        <section className="panel chat">
          <div className="log" ref={logRef}>
            {messages.map((item) => (
              <div key={item.id} className={`msg ${item.role}`}>
                <span className="who">
                  {item.role === "user"
                    ? credentials.operator.trim() || "我"
                    : item.role === "assistant"
                      ? "助手"
                      : "提示"}
                </span>
                <div className="bubble">
                  <span className={item.error ? "bubble-error" : item.pending ? "small" : undefined}>{item.text}</span>
                  {item.steps?.length ? (
                    <div className="steps">
                      {item.steps.map((step, index) => (
                        <span
                          key={`${step.tool}-${index}`}
                          className={`step-chip${step.status === "error" ? " error" : step.actionId ? " wait" : ""}`}
                        >
                          {step.tool}
                        </span>
                      ))}
                    </div>
                  ) : null}
                  {item.actions?.map((action) => (
                    <PendingCard
                      key={action.id}
                      action={action}
                      onDecide={(actionId, decision) => decide(item.id, actionId, decision)}
                    />
                  ))}
                  {Object.entries(item.executed ?? {}).map(([actionId, executed]) => (
                    <ExecutedCard key={actionId} executed={executed} auth={credentials} />
                  ))}
                </div>
              </div>
            ))}
          </div>
          <div className="composer">
            <textarea
              placeholder={"例如：查一下 604264 这张采购单\n或：把订单 11530151 里的 XZ25401308-101 换成 XZ25401308-09906"}
              value={draft}
              rows={2}
              onChange={(event) => setDraft(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === "Enter" && !event.shiftKey) {
                  event.preventDefault();
                  void send(draft);
                }
              }}
            />
            <button type="button" className="btn primary" disabled={sending} onClick={() => void send(draft)}>
              发送
            </button>
          </div>
        </section>

        <section className="chat-side">
          <div className="panel">
            <div className="panel-head">
              <strong>连接</strong>
            </div>
            <div className="credentials-grid" style={{ marginTop: 12 }}>
              <input
                type="password"
                autoComplete="off"
                placeholder="AGENT_API_TOKEN"
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
                onClick={() => connect().catch((error: unknown) => setMessage(errorText(error)))}
              >
                连接
              </button>
            </div>
            <div className="small" style={{ marginTop: 7 }}>
              {bound
                ? "网页身份已绑定，存在本机。Token 只在当前标签页。群里发「绑定网页」可重新要码。"
                : "先到钉钉群 @机器人发「绑定网页」，把私信里的 20 位码和绑定姓名填在这里。Token 只存在当前标签页。"}
            </div>
            <div className="statusline" style={{ marginTop: 12 }}>
              <span className={`dot ${agent?.available ? "online" : "offline"}`} />
              <span className="small">{stateText}</span>
            </div>
            {message ? <div className="status error">{message}</div> : null}
            <div className="samples">
              {SAMPLES.map((sample) => (
                <button key={sample.label} type="button" className="btn" onClick={() => void send(sample.ask)}>
                  {sample.label}
                </button>
              ))}
            </div>
            <div style={{ marginTop: 10 }}>
              <button type="button" className="btn" onClick={() => void resetSession()}>
                清空当前会话
              </button>
            </div>
          </div>

          <div className="panel" style={{ marginTop: 16 }}>
            <div className="panel-head">
              <strong>可用工具</strong>
              <small>L0 直接执行，L1/L2 必须人工确认</small>
            </div>
            <div className="tools">
              {!agent ? (
                <div className="small">连接后加载</div>
              ) : (
                <>
                  {status && !status.forecast.ready ? (
                    <div className="small">预测模型工件尚未就绪，预测与订货建议会提示先训练模型。</div>
                  ) : null}
                  {agent.tools.map((tool) => (
                    <div key={tool.name} className="tool">
                      <b className="mono">{tool.name}</b>
                      <span className={`risk ${tool.risk}`}>
                        {tool.risk} {tool.riskLabel}
                      </span>
                      <p>{tool.description}</p>
                    </div>
                  ))}
                </>
              )}
            </div>
          </div>
        </section>
      </main>
    </>
  );
}
