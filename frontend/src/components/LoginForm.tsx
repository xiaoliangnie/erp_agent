import { useState } from "react";
import type { FormEvent } from "react";
import { useAuth } from "../auth/AuthContext";

export function LoginForm({ note }: { note?: string }) {
  const { login } = useAuth();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  async function submit(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError("");
    try {
      await login(username, password);
    } catch (caught: unknown) {
      setError(caught instanceof Error ? caught.message : String(caught));
    } finally {
      setBusy(false);
    }
  }

  return (
    <form className="login-form" onSubmit={(event) => void submit(event)}>
      <p className="small">
        {note || "到钉钉群发「绑定网页」，密码会私信给你。登录后 30 天不用再输。"}
      </p>
      <div className="credentials-grid" style={{ marginTop: 10 }}>
        <input
          autoComplete="username"
          placeholder="花名"
          value={username}
          onChange={(event) => setUsername(event.target.value)}
        />
        <input
          type="password"
          autoComplete="current-password"
          placeholder="密码"
          value={password}
          onChange={(event) => setPassword(event.target.value)}
        />
        <button type="submit" className="btn primary" disabled={busy || !username.trim() || !password}>
          {busy ? "登录中…" : "登录"}
        </button>
      </div>
      {error ? <div className="status error">{error}</div> : null}
    </form>
  );
}
