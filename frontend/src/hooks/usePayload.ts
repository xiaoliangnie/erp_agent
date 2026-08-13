import { useCallback, useEffect, useState } from "react";
import { errorText, publicApi } from "../api/client";
import type { RawPayload } from "../data/payload";

interface PayloadState<T> {
  data: T | null;
  error: string;
  loading: boolean;
  reload: () => void;
}

/**
 * 取一份位置数组 payload 并解码。
 *
 * 没有离线快照回退：读不到就报错，不给员工看历史数据充当实时数据。
 */
export function usePayload<T>(path: string, year: string | null, decode: (payload: RawPayload) => T): PayloadState<T> {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const [attempt, setAttempt] = useState(0);

  const reload = useCallback(() => setAttempt((current) => current + 1), []);

  useEffect(() => {
    const controller = new AbortController();
    let cancelled = false;
    setLoading(true);
    setError("");
    const url = year ? `${path}?year=${encodeURIComponent(year)}` : path;
    publicApi
      .get<RawPayload>(url, { signal: controller.signal })
      .then((payload) => {
        if (cancelled) return;
        setData(decode(payload));
      })
      .catch((caught: unknown) => {
        if (cancelled || controller.signal.aborted) return;
        setError(errorText(caught));
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
      controller.abort();
    };
    // decode 是模块级纯函数，不进依赖数组，否则每次渲染都会重新请求。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [path, year, attempt]);

  return { data, error, loading, reload };
}
