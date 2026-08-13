import { useCallback, useState } from "react";
import type { Credentials } from "../api/client";

/**
 * Token 和操作人姓名只存当前标签页 sessionStorage —— 关掉标签页就没了，
 * 也不会被其他页面读到。数据库凭证、ERP Cookie 一律不进浏览器。
 */
export function useCredentials(prefix: string) {
  const tokenKey = `${prefix}Token`;
  const operatorKey = `${prefix}Operator`;

  const [credentials, setCredentials] = useState<Credentials>(() => ({
    token: sessionStorage.getItem(tokenKey) ?? "",
    operator: sessionStorage.getItem(operatorKey) ?? "",
  }));

  const update = useCallback((patch: Partial<Credentials>) => {
    setCredentials((current) => ({ ...current, ...patch }));
  }, []);

  const remember = useCallback(
    (next: Credentials) => {
      sessionStorage.setItem(tokenKey, next.token.trim());
      sessionStorage.setItem(operatorKey, next.operator.trim());
    },
    [tokenKey, operatorKey],
  );

  const filled = credentials.token.trim() !== "" && credentials.operator.trim() !== "";

  return { credentials, update, remember, filled };
}
