import { useEffect, useRef, useState } from "react";

/**
 * 图表按容器实际宽度排版，所以需要观察宽度变化。
 * 用 ResizeObserver 而不是 window resize：卡片宽度还会因为筛选栏换行而变。
 */
export function useElementWidth<T extends HTMLElement>(fallback = 360) {
  const ref = useRef<T>(null);
  const [width, setWidth] = useState(fallback);

  useEffect(() => {
    const node = ref.current;
    if (!node) return;
    const observer = new ResizeObserver((entries) => {
      const measured = entries[0]?.contentRect.width ?? 0;
      if (measured > 0) setWidth(Math.round(measured));
    });
    observer.observe(node);
    return () => observer.disconnect();
  }, []);

  return { ref, width };
}
