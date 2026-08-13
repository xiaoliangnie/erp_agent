import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState } from "react";
import type { ReactNode } from "react";

export interface TipRow {
  name: string;
  value: string;
  color?: string;
}

interface TipState {
  title: string;
  rows: TipRow[];
  x: number;
  y: number;
}

interface Pointer {
  clientX: number;
  clientY: number;
}

interface TooltipApi {
  show: (event: Pointer, title: string, rows: TipRow[]) => void;
  move: (event: Pointer) => void;
  hide: () => void;
}

const TooltipContext = createContext<TooltipApi | null>(null);

const PAD = 14;

export function TooltipProvider({ children }: { children: ReactNode }) {
  const [tip, setTip] = useState<TipState | null>(null);
  const nodeRef = useRef<HTMLDivElement>(null);

  // 贴着指针放，够不下就翻到另一侧，不让气泡出视口。
  const place = useCallback((event: Pointer) => {
    const box = nodeRef.current?.getBoundingClientRect();
    const width = box?.width ?? 160;
    const height = box?.height ?? 80;
    let x = event.clientX + PAD;
    let y = event.clientY + PAD;
    if (x + width > window.innerWidth - 8) x = event.clientX - width - PAD;
    if (y + height > window.innerHeight - 8) y = event.clientY - height - PAD;
    return { x: Math.max(8, x), y: Math.max(8, y) };
  }, []);

  const api = useMemo<TooltipApi>(
    () => ({
      show(event, title, rows) {
        const { x, y } = place(event);
        setTip({ title, rows, x, y });
      },
      move(event) {
        setTip((current) => (current ? { ...current, ...place(event) } : current));
      },
      hide() {
        setTip(null);
      },
    }),
    [place],
  );

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") setTip(null);
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, []);

  return (
    <TooltipContext.Provider value={api}>
      {children}
      <div
        ref={nodeRef}
        className={`tooltip${tip ? " open" : ""}`}
        role="status"
        aria-live="polite"
        style={{ left: tip?.x ?? 0, top: tip?.y ?? 0 }}
      >
        {tip ? (
          <>
            <div className="tt-title">{tip.title}</div>
            {tip.rows.map((row) => (
              <div key={row.name} className="tt-row">
                <span className="tt-name">
                  {row.color ? <i className="tt-key" style={{ background: row.color }} /> : null}
                  {row.name}
                </span>
                <span className="tt-val">{row.value}</span>
              </div>
            ))}
          </>
        ) : null}
      </div>
    </TooltipContext.Provider>
  );
}

export function useTooltip(): TooltipApi {
  const api = useContext(TooltipContext);
  if (!api) throw new Error("useTooltip 必须在 TooltipProvider 内使用");
  return api;
}

/**
 * 给任意图形标记挂上 hover 与键盘焦点的同一套读数 —— 鼠标看得到的，
 * Tab 也要读得到，所以 aria-label 把整段读数写进去。
 */
export function useHoverable() {
  const { show, move, hide } = useTooltip();
  return useCallback(
    (title: string, rows: TipRow[]) => ({
      tabIndex: 0,
      role: "img" as const,
      "aria-label": `${title}：${rows.map((row) => `${row.name} ${row.value}`).join("，")}`,
      onPointerEnter: (event: React.PointerEvent) => show(event, title, rows),
      onPointerMove: (event: React.PointerEvent) => move(event),
      onPointerLeave: () => hide(),
      onFocus: (event: React.FocusEvent<SVGElement | HTMLElement>) => {
        const box = event.currentTarget.getBoundingClientRect();
        show({ clientX: box.left + box.width / 2, clientY: box.top }, title, rows);
      },
      onBlur: () => hide(),
    }),
    [show, move, hide],
  );
}
