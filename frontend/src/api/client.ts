/*
 * 同源接口调用。
 *
 * 页面接口和 /api/agent/* 都带 X-Agent-Web-Token（localStorage）。
 * 脚本仍可用 Bearer AGENT_API_TOKEN；Worker 用 EXCHANGE_WORKER_TOKEN。
 * 未登录不能进面板。会话 30 天。
 */

export const WEB_TOKEN_KEY = "agentWebToken";
export const AUTH_EXPIRED_EVENT = "agent-auth-expired";

export function readWebToken(): string {
  return (localStorage.getItem(WEB_TOKEN_KEY) ?? "").trim();
}

export function webAuthHeaders(): Record<string, string> {
  const webToken = readWebToken();
  return webToken ? { "X-Agent-Web-Token": webToken } : {};
}

export class ApiError extends Error {
  readonly status: number;

  constructor(message: string, status: number) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

export interface Credentials {
  token: string;
  operator: string;
  webToken?: string;
  bindCode?: string;
}

interface RequestOptions {
  method?: string;
  body?: unknown;
  /** 附加请求头，例如换货任务的 Idempotency-Key。 */
  headers?: Record<string, string>;
  signal?: AbortSignal;
}

async function readError(response: Response): Promise<string> {
  try {
    const payload = (await response.json()) as { error?: string };
    if (payload && typeof payload.error === "string" && payload.error) return payload.error;
  } catch {
    // 非 JSON 响应（例如网关的 HTML 错误页）只能按状态码说话。
  }
  return `HTTP ${response.status}`;
}

async function request<T>(path: string, options: RequestOptions, auth?: Credentials): Promise<T> {
  const headers: Record<string, string> = { Accept: "application/json", ...options.headers };
  if (options.body !== undefined) headers["Content-Type"] = "application/json";
  if (auth) {
    const token = auth.token.trim();
    if (token) headers.Authorization = `Bearer ${token}`;
  }
  const webToken = auth?.webToken?.trim() || readWebToken();
  if (webToken) headers["X-Agent-Web-Token"] = webToken;
  const response = await fetch(path, {
    method: options.method ?? "GET",
    headers,
    body: options.body === undefined ? undefined : JSON.stringify(options.body),
    signal: options.signal,
  });
  if (!response.ok) {
    if (response.status === 401 && !path.endsWith("/api/agent/login")) {
      window.dispatchEvent(new Event(AUTH_EXPIRED_EVENT));
    }
    throw new ApiError(await readError(response), response.status);
  }
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

/** 页面接口。未登录会被 401；已登录自动带网页会话。 */
export const publicApi = {
  get: <T>(path: string, options: RequestOptions = {}) => request<T>(path, options),
  post: <T>(path: string, body: unknown, options: RequestOptions = {}) =>
    request<T>(path, { ...options, method: "POST", body }),
};

export const exchangeApi = {
  get: <T>(path: string, auth: Credentials, options: RequestOptions = {}) =>
    request<T>(path, options, auth),
  post: <T>(path: string, body: unknown, auth: Credentials, options: RequestOptions = {}) =>
    request<T>(path, { ...options, method: "POST", body }, auth),
};

export const agentApi = {
  get: <T>(path: string, auth: Credentials, options: RequestOptions = {}) =>
    request<T>(path, options, auth),
  post: <T>(path: string, body: unknown, auth: Credentials, options: RequestOptions = {}) =>
    request<T>(path, { ...options, method: "POST", body }, auth),
};

/**
 * 取带鉴权的二进制产物（合同 Excel / 预览 PNG）。
 * 不能直接把 URL 塞进 <a href> 或 <img src>，那样带不上 Bearer。
 */
export async function fetchBlob(path: string, auth?: Credentials): Promise<Blob> {
  const headers: Record<string, string> = {};
  if (auth?.token.trim()) headers.Authorization = `Bearer ${auth.token.trim()}`;
  Object.assign(headers, webAuthHeaders());
  const response = await fetch(path, { headers });
  if (!response.ok) {
    if (response.status === 401) window.dispatchEvent(new Event(AUTH_EXPIRED_EVENT));
    throw new ApiError(await readError(response), response.status);
  }
  return await response.blob();
}

/** 下载或新窗口打开一个 blob；60 秒后回收 object URL。 */
export function openBlob(blob: Blob, fileName?: string): void {
  const url = URL.createObjectURL(blob);
  if (fileName) {
    const link = document.createElement("a");
    link.href = url;
    link.download = fileName;
    link.click();
  } else {
    window.open(url, "_blank", "noopener");
  }
  setTimeout(() => URL.revokeObjectURL(url), 60_000);
}

export function errorText(error: unknown): string {
  if (error instanceof Error) return error.message;
  return String(error);
}
