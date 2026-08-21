import { useCallback, useState } from "react";
import { agentApi, errorText, type Credentials } from "../api/client";

const WEB_TOKEN_KEY = "agentWebToken";
const BIND_CODE_RE = /^[0-9a-fA-F]{20}$/;

function readWebToken(prefix: string): string {
  if (prefix !== "agent") return "";
  return localStorage.getItem(WEB_TOKEN_KEY) ?? "";
}

/** 钉钉私信身份码是 20 位十六进制；共享 Token 按约定是更长的随机串。 */
export function looksLikeBindCode(value: string): boolean {
  return BIND_CODE_RE.test(value.trim());
}

function shouldForgetWebSession(error: unknown): boolean {
  const text = error instanceof Error ? error.message : String(error);
  return (
    text.includes("绑定网页")
    || text.includes("身份码")
    || text.includes("网页署名")
    || text.includes("AGENT_API_TOKEN")
  );
}

/**
 * Token 和操作人姓名只存当前标签页 sessionStorage —— 关掉标签页就没了。
 * 网页身份 webToken 存在 localStorage，钉钉要码绑定一次后不用重绑。
 * 数据库凭证、ERP Cookie 一律不进浏览器。
 */
export function useCredentials(prefix: string) {
  const tokenKey = `${prefix}Token`;
  const operatorKey = `${prefix}Operator`;

  const [credentials, setCredentials] = useState<Credentials>(() => ({
    token: sessionStorage.getItem(tokenKey) ?? "",
    operator: sessionStorage.getItem(operatorKey) ?? "",
    webToken: readWebToken(prefix),
    bindCode: "",
  }));

  const update = useCallback((patch: Partial<Credentials>) => {
    setCredentials((current) => ({ ...current, ...patch }));
  }, []);

  const remember = useCallback(
    (next: Credentials) => {
      sessionStorage.setItem(tokenKey, next.token.trim());
      sessionStorage.setItem(operatorKey, next.operator.trim());
      if (prefix === "agent") {
        const webToken = next.webToken?.trim() ?? "";
        if (webToken) localStorage.setItem(WEB_TOKEN_KEY, webToken);
        else localStorage.removeItem(WEB_TOKEN_KEY);
      }
    },
    [prefix, tokenKey, operatorKey],
  );

  const forgetWebSession = useCallback(() => {
    if (prefix !== "agent") return;
    localStorage.removeItem(WEB_TOKEN_KEY);
    setCredentials((current) => ({ ...current, webToken: "" }));
  }, [prefix]);

  const noteBindError = useCallback((error: unknown) => {
    if (shouldForgetWebSession(error)) forgetWebSession();
    return errorText(error);
  }, [forgetWebSession]);

  const hasToken = credentials.token.trim() !== "";
  const hasName = credentials.operator.trim() !== "";
  const hasWeb = prefix !== "agent" || Boolean(credentials.webToken?.trim());
  const tokenAsCode = prefix === "agent" && looksLikeBindCode(credentials.token);
  const canBind = prefix === "agent" && hasName && (
    Boolean(credentials.bindCode?.trim()) || tokenAsCode
  );
  const filled = prefix === "agent" ? hasName && (hasWeb || canBind) : hasToken && hasName;
  const bound = prefix !== "agent" || Boolean(credentials.webToken?.trim());

  const ensureBound = useCallback(async () => {
    if (prefix !== "agent") {
      remember(credentials);
      return credentials;
    }
    const operator = credentials.operator.trim();
    let token = credentials.token.trim();
    let code = credentials.bindCode?.trim() ?? "";
    if (!code && looksLikeBindCode(token)) {
      code = token;
      token = "";
    }
    if (code) {
      const result = await agentApi.post<{ webToken: string; operator: string }>(
        "/api/agent/web-bind",
        { operator, code },
        { token, operator },
      );
      const next: Credentials = {
        token,
        operator: result.operator || operator,
        webToken: result.webToken,
        bindCode: "",
      };
      remember(next);
      setCredentials(next);
      return next;
    }
    if (credentials.webToken?.trim()) {
      const next = { ...credentials, token, operator };
      remember(next);
      return next;
    }
    throw new Error("请填写钉钉私信里的 20 位网页身份码");
  }, [credentials, prefix, remember]);

  return {
    credentials, update, remember, ensureBound, forgetWebSession, noteBindError,
    filled, bound,
  };
}
