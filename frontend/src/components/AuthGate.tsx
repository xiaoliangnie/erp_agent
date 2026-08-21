import type { ReactNode } from "react";
import { useAuth } from "../auth/AuthContext";
import { Loading } from "./PageState";
import { LoginForm } from "./LoginForm";

export function AuthGate({ children }: { children: ReactNode }) {
  const { ready, loggedIn } = useAuth();
  if (!ready) return <Loading label="正在核对登录…" />;
  if (!loggedIn) {
    return (
      <main className="login-screen">
        <h1>蜀黍家采购</h1>
        <p className="small">登录后才能打开看板和工作台。</p>
        <LoginForm />
      </main>
    );
  }
  return children;
}
