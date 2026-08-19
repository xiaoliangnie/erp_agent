import { useCallback, useState } from "react";
import { agentApi, type Credentials } from "../api/client";

const WEB_TOKEN_KEY = "agentWebToken";

function readWebToken(prefix: string): string {
  if (prefix !== "agent") return "";
  return localStorage.getItem(WEB_TOKEN_KEY) ?? "";
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
      }
    },
    [prefix, tokenKey, operatorKey],
  );

  const hasToken = credentials.token.trim() !== "";
  const hasName = credentials.operator.trim() !== "";
  const hasWeb = prefix !== "agent" || Boolean(credentials.webToken?.trim());
  const canBind = prefix === "agent" && hasName && Boolean(credentials.bindCode?.trim());
  const filled = prefix === "agent" ? hasName && (hasWeb || canBind) : hasToken && hasName;
  const bound = prefix !== "agent" || Boolean(credentials.webToken?.trim());

  const ensureBound = useCallback(async () => {
    if (prefix !== "agent") {
      remember(credentials);
      return credentials;
    }
    if (credentials.webToken?.trim()) {
      remember(credentials);
      return credentials;
    }
    const code = credentials.bindCode?.trim() ?? "";
    if (!code) throw new Error("请填写钉钉私信里的 20 位网页身份码");
    const result = await agentApi.post<{ webToken: string; operator: string }>(
      "/api/agent/web-bind",
      { operator: credentials.operator.trim(), code },
      { token: credentials.token, operator: credentials.operator },
    );
    const next: Credentials = {
      token: credentials.token,
      operator: result.operator || credentials.operator,
      webToken: result.webToken,
      bindCode: "",
    };
    remember(next);
    setCredentials(next);
    return next;
  }, [credentials, prefix, remember]);

  return { credentials, update, remember, ensureBound, filled, bound };
}
