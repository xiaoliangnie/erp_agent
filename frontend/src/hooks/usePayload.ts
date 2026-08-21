import { useCallback, useEffect, useState } from "react";
import { errorText, publicApi } from "../api/client";
import type { RawPayload } from "../data/payload";

interface PayloadState<T> {
  data: T | null;
  error: string;
  loading: boolean;
  refreshing: boolean;
  reload: () => void;
}

interface MemoryHit {
  payload: RawPayload;
  at: number;
}

const memory = new Map<string, MemoryHit>();
/** 与服务端热缓存同一量级：25 秒内再进页不重拉。 */
const HOT_MS = 25_000;

function cacheKey(path: string, year: string | null): string {
  return year ? `${path}?year=${year}` : path;
}

/**
 * 取一份位置数组 payload 并解码。
 *
 * 同会话热数据直接上屏；过了 25 秒先出上一份再后台刷新。
 * 读不到仍报错，不把旧离线快照当成实时数据。
 */
export function usePayload<T>(path: string, year: string | null, decode: (payload: RawPayload) => T): PayloadState<T> {
  const key = cacheKey(path, year);
  const [data, setData] = useState<T | null>(() => {
    const hit = memory.get(key);
    return hit ? decode(hit.payload) : null;
  });
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(() => !memory.has(key));
  const [refreshing, setRefreshing] = useState(false);
  const [attempt, setAttempt] = useState(0);

  const reload = useCallback(() => setAttempt((current) => current + 1), []);

  useEffect(() => {
    const hit = memory.get(key);
    const age = hit ? Date.now() - hit.at : Number.POSITIVE_INFINITY;
    if (hit) {
      setData(decode(hit.payload));
      setLoading(false);
      setError("");
      if (age < HOT_MS && attempt === 0) {
        setRefreshing(false);
        return;
      }
    } else {
      setData(null);
      setLoading(true);
    }

    const controller = new AbortController();
    let cancelled = false;
    setRefreshing(Boolean(hit));
    const url = year ? `${path}?year=${encodeURIComponent(year)}` : path;
    publicApi
      .get<RawPayload>(url, { signal: controller.signal })
      .then((payload) => {
        if (cancelled) return;
        memory.set(key, { payload, at: Date.now() });
        setData(decode(payload));
        setError("");
      })
      .catch((caught: unknown) => {
        if (cancelled || controller.signal.aborted) return;
        if (!memory.has(key)) setData(null);
        setError(errorText(caught));
      })
      .finally(() => {
        if (cancelled) return;
        setLoading(false);
        setRefreshing(false);
      });
    return () => {
      cancelled = true;
      controller.abort();
    };
    // decode 是模块级纯函数，不进依赖数组，否则每次渲染都会重新请求。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [path, year, attempt, key]);

  return { data, error, loading, refreshing, reload };
}
