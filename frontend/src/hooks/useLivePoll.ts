import { useEffect } from "react";

/**
 * 页在前台时按间隔拉数据，切走就停。失败不打断当前画面。
 * 看板/建单这种「算出来的表」用轮询就够，不必上 WebSocket。
 */
export function useLivePoll(
  callback: () => void | Promise<void>,
  intervalMs: number,
  enabled = true,
): void {
  useEffect(() => {
    if (!enabled || intervalMs <= 0) return;
    let cancelled = false;
    const run = () => {
      if (cancelled || document.hidden) return;
      void callback();
    };
    const timer = window.setInterval(run, intervalMs);
    const onVisible = () => {
      if (!document.hidden) run();
    };
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
      document.removeEventListener("visibilitychange", onVisible);
    };
  }, [callback, enabled, intervalMs]);
}
