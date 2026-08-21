import { createContext, useCallback, useContext, useEffect, useMemo, useState } from "react";
import type { ReactNode } from "react";
import { AUTH_EXPIRED_EVENT, WEB_TOKEN_KEY, agentApi, errorText, publicApi, type Credentials } from "../api/client";

interface AuthContextValue {
  ready: boolean;
  loggedIn: boolean;
  operator: string;
  credentials: Credentials;
  login: (username: string, password: string) => Promise<void>;
  logout: () => Promise<void>;
  noteAuthError: (error: unknown) => string;
}

const AuthContext = createContext<AuthContextValue | null>(null);

function readStoredToken(): string {
  return localStorage.getItem(WEB_TOKEN_KEY) ?? "";
}

function writeStoredToken(token: string) {
  if (token) localStorage.setItem(WEB_TOKEN_KEY, token);
  else localStorage.removeItem(WEB_TOKEN_KEY);
}

export function AuthProvider({ children }: { children: ReactNode }) {
  const [ready, setReady] = useState(false);
  const [operator, setOperator] = useState("");
  const [webToken, setWebToken] = useState(() => readStoredToken());

  const credentials = useMemo<Credentials>(
    () => ({ token: "", operator, webToken }),
    [operator, webToken],
  );

  const clear = useCallback(() => {
    writeStoredToken("");
    setWebToken("");
    setOperator("");
  }, []);

  useEffect(() => {
    const token = readStoredToken();
    if (!token) {
      setReady(true);
      return;
    }
    let cancelled = false;
    agentApi
      .get<{ operator?: string; buyerName?: string }>("/api/agent/me", {
        token: "", operator: "", webToken: token,
      })
      .then((me) => {
        if (cancelled) return;
        setWebToken(token);
        setOperator(me.operator || me.buyerName || "");
      })
      .catch(() => {
        if (!cancelled) clear();
      })
      .finally(() => {
        if (!cancelled) setReady(true);
      });
    return () => {
      cancelled = true;
    };
  }, [clear]);

  useEffect(() => {
    const onExpired = () => clear();
    window.addEventListener(AUTH_EXPIRED_EVENT, onExpired);
    return () => window.removeEventListener(AUTH_EXPIRED_EVENT, onExpired);
  }, [clear]);

  const login = useCallback(async (username: string, password: string) => {
    const result = await publicApi.post<{ webToken: string; operator: string }>(
      "/api/agent/login",
      { username: username.trim(), password },
    );
    writeStoredToken(result.webToken);
    setWebToken(result.webToken);
    setOperator(result.operator);
  }, []);

  const logout = useCallback(async () => {
    const token = webToken.trim();
    if (token) {
      try {
        await agentApi.post("/api/agent/logout", {}, { token: "", operator, webToken: token });
      } catch {
        // 本地清掉即可，服务端会话过期也不挡退出。
      }
    }
    clear();
  }, [clear, operator, webToken]);

  const noteAuthError = useCallback((error: unknown) => {
    const text = errorText(error);
    if (
      text.includes("绑定网页")
      || text.includes("登录")
      || text.includes("花名或密码")
      || text.includes("网页账号")
    ) {
      clear();
    }
    return text;
  }, [clear]);

  const value = useMemo<AuthContextValue>(
    () => ({
      ready,
      loggedIn: Boolean(webToken.trim() && operator.trim()),
      operator,
      credentials,
      login,
      logout,
      noteAuthError,
    }),
    [credentials, login, logout, noteAuthError, operator, ready, webToken],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthContextValue {
  const value = useContext(AuthContext);
  if (!value) throw new Error("useAuth 必须放在 AuthProvider 里");
  return value;
}
