import { useEffect, useState } from "react";
import { publicApi } from "../api/client";

/** 业务时钟按东八区走，不读浏览器本地时区。 */
const TZ_MS = 8 * 60 * 60 * 1000;
const TICK_MS = 1000;
const SYNC_MS = 30_000;

interface ClockPayload {
  now: string;
  today: string;
}

function parseBusiness(text: string): number | null {
  const match = /^(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2}):(\d{2})/.exec(text);
  if (!match) return null;
  return Date.UTC(
    Number(match[1]),
    Number(match[2]) - 1,
    Number(match[3]),
    Number(match[4]) - 8,
    Number(match[5]),
    Number(match[6]),
  );
}

function formatBusiness(ms: number): string {
  const shifted = new Date(ms + TZ_MS);
  const pad = (value: number) => String(value).padStart(2, "0");
  return [
    `${shifted.getUTCFullYear()}-${pad(shifted.getUTCMonth() + 1)}-${pad(shifted.getUTCDate())}`,
    `${pad(shifted.getUTCHours())}:${pad(shifted.getUTCMinutes())}:${pad(shifted.getUTCSeconds())}`,
  ].join(" ");
}

/**
 * 先对齐 `/api/now`，再按偏移每秒走。页面时间会变，业务日仍以服务端为准。
 */
export function useServerClock(): { now: string; today: string; ready: boolean } {
  const [now, setNow] = useState("");
  const [today, setToday] = useState("");

  useEffect(() => {
    let offset = 0;
    let timer = 0;
    let syncTimer = 0;
    let cancelled = false;

    const tick = () => {
      if (cancelled) return;
      const stamp = formatBusiness(Date.now() + offset);
      setNow(stamp);
      setToday(stamp.slice(0, 10));
    };

    const sync = async () => {
      try {
        const payload = await publicApi.get<ClockPayload>("/api/now");
        const parsed = parseBusiness(payload.now);
        if (parsed == null || cancelled) return;
        offset = parsed - Date.now();
        setToday(payload.today || payload.now.slice(0, 10));
        tick();
      } catch {
        // 对不齐就继续用上一轮偏移，不退回本机时区。
      }
    };

    void sync();
    timer = window.setInterval(tick, TICK_MS);
    syncTimer = window.setInterval(() => {
      if (!document.hidden) void sync();
    }, SYNC_MS);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
      window.clearInterval(syncTimer);
    };
  }, []);

  return { now, today, ready: Boolean(now) };
}
