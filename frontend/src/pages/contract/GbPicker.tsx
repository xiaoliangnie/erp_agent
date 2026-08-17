import { useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { errorText, publicApi } from "../../api/client";
import type { ContractItem, GbOption } from "./types";

function gbStatusKind(status: string): "critical" | "warning" | "good" | "" {
  if (status === "废止") return "critical";
  if (status === "即将实施") return "warning";
  if (status === "现行") return "good";
  return "";
}

function mergeOptions(...groups: GbOption[][]): GbOption[] {
  const seen = new Set<string>();
  const out: GbOption[] = [];
  for (const group of groups) {
    for (const option of group) {
      if (!option.standardNo || seen.has(option.standardNo)) continue;
      seen.add(option.standardNo);
      out.push(option);
    }
  }
  return out;
}

function StatusBadge({ status }: { status: string }) {
  if (!status) return null;
  const kind = gbStatusKind(status);
  return <span className={`gb-badge${kind ? ` ${kind}` : ""}`}>{status}</span>;
}

interface Props {
  item: ContractItem;
  selected: string;
  onSelect: (standardNo: string, option?: GbOption) => void;
  onError: (text: string) => void;
}

export function GbPicker({ item, selected, onSelect, onError }: Props) {
  const catalog = item.gbOptions ?? [];
  const [open, setOpen] = useState(false);
  const [draft, setDraft] = useState("");
  const [found, setFound] = useState<GbOption[]>([]);
  const [recommended, setRecommended] = useState<GbOption[]>(
    () => catalog.filter((option) => option.recommended),
  );
  const [menuPos, setMenuPos] = useState({ top: 0, left: 0, width: 360 });
  const boxRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  const timerRef = useRef(0);
  const onErrorRef = useRef(onError);
  onErrorRef.current = onError;

  const selectedOption = useMemo(
    () => mergeOptions(recommended, found, catalog).find((option) => option.standardNo === selected),
    [catalog, found, recommended, selected],
  );

  useEffect(() => () => window.clearTimeout(timerRef.current), []);

  useEffect(() => {
    setDraft("");
    setFound([]);
    setOpen(false);
    setRecommended((item.gbOptions ?? []).filter((option) => option.recommended));
  }, [item.poiId]);

  useEffect(() => {
    const onDoc = (event: MouseEvent) => {
      const target = event.target as Node;
      if (boxRef.current?.contains(target)) return;
      if ((target as HTMLElement).closest?.(".gb-menu")) return;
      setOpen(false);
    };
    document.addEventListener("mousedown", onDoc);
    return () => document.removeEventListener("mousedown", onDoc);
  }, []);

  useLayoutEffect(() => {
    if (!open || !boxRef.current) return;
    const update = () => {
      const rect = boxRef.current?.getBoundingClientRect();
      if (!rect) return;
      const width = Math.min(Math.max(rect.width, 360), window.innerWidth - 24);
      const left = Math.min(rect.left, window.innerWidth - width - 12);
      const below = rect.bottom + 4;
      const maxHeight = 280;
      const top = below + maxHeight > window.innerHeight - 8
        ? Math.max(8, rect.top - maxHeight - 4)
        : below;
      setMenuPos({ top, left, width });
    };
    update();
    window.addEventListener("resize", update);
    window.addEventListener("scroll", update, true);
    return () => {
      window.removeEventListener("resize", update);
      window.removeEventListener("scroll", update, true);
    };
  }, [open]);

  useEffect(() => {
    if (open) inputRef.current?.focus();
  }, [open]);

  useEffect(() => {
    const controller = new AbortController();
    publicApi.post<{ standards?: GbOption[] }>(
      "/api/contracts/gb/recommend",
      {
        name: item.name,
        category: item.category,
        specification: item.specification,
        remark: item.remark,
        candidates: (item.gbOptions ?? []).slice(0, 20),
      },
      { signal: controller.signal },
    ).then((data) => {
      const next = data.standards?.filter((option) => option.recommended) ?? [];
      if (next.length) setRecommended(next);
    }).catch((error: unknown) => {
      if (error instanceof Error && error.name === "AbortError") return;
    });
    return () => controller.abort();
  }, [item.category, item.name, item.poiId, item.remark, item.specification]);

  function search(keyword: string) {
    const trimmed = keyword.trim();
    if (trimmed.length < 2) {
      setFound([]);
      return;
    }
    const params = new URLSearchParams({
      q: trimmed,
      name: item.name || "",
      category: item.category || "",
    });
    publicApi.get<{ standards?: GbOption[] }>(`/api/contracts/gb/search?${params}`)
      .then((data) => setFound(data.standards ?? []))
      .catch((error: unknown) => onErrorRef.current(errorText(error)));
  }

  function pick(standardNo: string, option?: GbOption) {
    onSelect(standardNo, option);
    setDraft("");
    setOpen(false);
  }

  const typing = draft.trim().length >= 2;
  const list = typing
    ? found
    : mergeOptions(
      recommended,
      catalog.filter((option) => option.status === "现行" || option.status === "即将实施"),
    );

  const menu = open ? createPortal(
    <div
      className="gb-menu"
      role="listbox"
      style={{ top: menuPos.top, left: menuPos.left, width: menuPos.width }}
    >
      <div className="gb-menu-head">
        {typing ? `以 ${draft.trim()} 开头` : "推荐 · 点选后写入合同"}
      </div>
      {list.map((option) => (
        <button
          type="button"
          key={option.standardNo}
          className={`gb-option${selected === option.standardNo ? " active" : ""}`}
          onClick={() => pick(option.standardNo, option)}
        >
          <span className="gb-option-main">
            {option.recommended && !typing ? <em className="gb-rec">推荐</em> : null}
            <b>{option.standardNo}</b>
            <span>{option.nameCn}</span>
          </span>
          <StatusBadge status={option.status} />
        </button>
      ))}
      {list.length === 0 ? (
        <div className="gb-empty">
          {typing ? "没有以该前缀开头的标准" : "该类暂无推荐，输入标准号查找"}
        </div>
      ) : null}
    </div>,
    document.body,
  ) : null;

  return (
    <div className="gb-cell" ref={boxRef}>
      <div className={`gb-control${open ? " open" : ""}`}>
        {open ? (
          <input
            ref={inputRef}
            className="gb-combobox"
            placeholder="输入 GB/T 36 按前缀查找"
            value={draft}
            onChange={(event) => {
              const keyword = event.target.value;
              setDraft(keyword);
              window.clearTimeout(timerRef.current);
              timerRef.current = window.setTimeout(() => search(keyword), 220);
            }}
            onKeyDown={(event) => {
              if (event.key === "Escape") setOpen(false);
            }}
          />
        ) : (
          <button
            type="button"
            className={`gb-value${selectedOption ? "" : " empty"}`}
            onClick={() => {
              setDraft("");
              setFound([]);
              setOpen(true);
            }}
          >
            {selectedOption ? (
              <>
                <b>{selectedOption.standardNo}</b>
                <span className="gb-name">{selectedOption.nameCn}</span>
                <StatusBadge status={selectedOption.status} />
              </>
            ) : (
              <span className="gb-placeholder">选择执行标准</span>
            )}
          </button>
        )}
        {selected ? (
          <button
            type="button"
            className="gb-clear"
            aria-label="清除执行标准"
            onClick={(event) => {
              event.stopPropagation();
              pick("");
            }}
          >
            ×
          </button>
        ) : null}
      </div>
      {menu}
    </div>
  );
}
