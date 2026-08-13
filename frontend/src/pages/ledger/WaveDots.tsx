import type { CSSProperties } from "react";
import { cssVar, dayIso } from "../../lib/format";
import type { OrderWave } from "./waves";
import { TIMELINE, planWaves } from "./waves";

interface WaveDotsProps {
  etaDay: number | null;
  today: number;
  current: OrderWave;
}

/** 四波时间轴：今天走到哪一波，前面的都算「已到点」。 */
export function WaveDots({ etaDay, today, current }: WaveDotsProps) {
  const plan = planWaves(etaDay);
  return (
    <span className="waves">
      {TIMELINE.map((wave, index) => {
        if (!plan) {
          return (
            <i key={wave.k} title="未排期，四波都发不出去">
              {index + 1}
            </i>
          );
        }
        const { day } = plan[index];
        const hit = today >= day;
        const classes = [hit ? "hit" : "", current === wave.k ? "cur" : ""].filter(Boolean).join(" ");
        // --ring 是 CSS 自定义属性，CSSProperties 没有它的键，只能整体断言一次。
        const style = {
          ...(hit ? { background: cssVar(wave.cssVar) } : {}),
          ...(current === wave.k ? { "--ring": cssVar(wave.cssVar) } : {}),
        } as CSSProperties;
        return (
          <i
            key={wave.k}
            className={classes || undefined}
            style={style}
            title={`${wave.seq} · ${wave.label}：${dayIso(day)}${hit ? "（已到点）" : `（还有 ${day - today} 天）`}`}
          >
            {index + 1}
          </i>
        );
      })}
    </span>
  );
}
