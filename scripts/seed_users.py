# -*- coding: utf-8 -*-
"""扫描采购员署名，生成分析报告，并按确认聚类写入 users。

默认只分析、不写库。加 --seed 才写入 Agent SQLite。
不得把 DISTINCT 字符串直接建成不同用户。
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.agent.store import AgentStore
from backend.agent.users import (
    CONFIRMED_IDENTITIES,
    DEFERRED_ALIAS_REVIEWS,
    UserRepository,
    analyze_buyer_records,
)
from backend.staff_names import cluster_staff_names


def load_csv_records(path: Path) -> list[dict]:
    counts: Counter[str] = Counter()
    last: dict[str, str] = {}
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if "采购员" not in (reader.fieldnames or []):
            raise ValueError(f"{path} 没有「采购员」列")
        for row in reader:
            name = (row.get("采购员") or "").strip()
            if not name:
                continue
            counts[name] += 1
            stamp = (row.get("采购日期") or row.get("审核日期") or "")[:19]
            if stamp >= last.get(name, ""):
                last[name] = stamp
    return [
        {"raw": name, "count": count, "last_seen": last.get(name, "")}
        for name, count in counts.most_common()
    ]


def load_mysql_records(env_path: Path) -> list[dict]:
    from backend.database import REALTIME_MAIN_TABLE, read_query

    rows = read_query(
        str(env_path),
        f"""
        SELECT TRIM(purchaser_name) AS buyer,
               COUNT(*) AS row_count,
               COUNT(DISTINCT po_id) AS po_count,
               MIN(LEFT(po_date, 10)) AS first_seen,
               MAX(LEFT(po_date, 10)) AS last_seen
        FROM `{REALTIME_MAIN_TABLE}`
        WHERE TRIM(COALESCE(purchaser_name, '')) != ''
        GROUP BY TRIM(purchaser_name)
        ORDER BY po_count DESC, buyer
        """,
    )
    return [
        {
            "raw": str(row["buyer"] or "").strip(),
            "count": int(row["po_count"] or 0),
            "last_seen": str(row["last_seen"] or ""),
            "first_seen": str(row.get("first_seen") or ""),
            "row_count": int(row.get("row_count") or 0),
        }
        for row in rows
        if str(row.get("buyer") or "").strip()
    ]


def render_report(analysis: dict, *, source: str, extra_notes=()) -> str:
    lines = [
        "# 采购员身份分析",
        "",
        f"数据来源：{source}",
        f"原始署名数：{analysis['rawCount']}",
        f"聚类后：{analysis['clusterCount']}（自动确认 {analysis['autoCount']}，待人工 {analysis['reviewCount']}）",
        "",
        "规则：同一括号对、标准化后的同一字符串、以及唯一外名/花名命中可以自动合并。",
        "岗位署名「鞋子理单（三三）」在花名唯一时并入「三三」。数字后缀、括号内工号、临时工仍进 `needs_review`。",
        "",
        "## 自动确认",
        "",
        "| 标准名 | 花名 | 别名 | 出现次数 | 最近出现 |",
        "|---|---|---|---:|---|",
    ]
    for item in analysis["auto"]:
        lines.append(
            f"| {item['canonicalName']} | {item['nickname']} | "
            f"{'、'.join(item['aliases'])} | {item['occurrences']} | {item['lastSeen']} |"
        )
    lines.extend(["", "## 待人工确认", "", "| 别名 | 原因 | 出现次数 | 最近出现 |", "|---|---|---:|---|"])
    for item in analysis["needsReview"]:
        lines.append(
            f"| {'、'.join(item['aliases'])} | {item['reason']} | "
            f"{item['occurrences']} | {item['lastSeen']} |"
        )
    notes = list(extra_notes or ())
    notes.append("2026-08-17 人工确认：「三三」与「鞋子理单（三三）」是同一个人。")
    notes.append("2026-08-17 人工确认：韩立是 ERP「管理员」账号本人；`--seed` 会合并到同一 `user_id`。")
    lines.extend(["", "## 人工确认", "", "| 标准名 | 别名 | 说明 |", "|---|---|---|"])
    for item in CONFIRMED_IDENTITIES:
        lines.append(
            f"| {item['canonical_name']} | {'、'.join(item['aliases'])} | {item['note']} |"
        )
    lines.extend(["", "## 暂不处理", "", "| 别名 | 可能相关 | 原因 | 说明 |", "|---|---|---|---|"])
    for item in DEFERRED_ALIAS_REVIEWS:
        lines.append(
            f"| {'、'.join(item['aliases'])} | {item['related'] or '—'} | "
            f"{item['reason']} | {item['note']} |"
        )
    lines.extend(["", "## 备注", ""])
    lines.extend(f"- {note}" for note in notes)
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="扫描采购员并可选写入 users")
    parser.add_argument("--csv", default="files/data/snapshots/采购单完整数据.csv")
    parser.add_argument("--env", default="hanli.env")
    parser.add_argument("--live", action="store_true", help="从镜像库 realtime_purchase_orders 只读扫描")
    parser.add_argument("--seed", action="store_true", help="把自动确认的聚类写入 Agent SQLite")
    parser.add_argument("--include-review", action="store_true")
    parser.add_argument("--database", default="files/data/agent.sqlite3")
    parser.add_argument("--report", default="docs/reports/user_identity_analysis.md")
    args = parser.parse_args()

    source = "csv"
    records = []
    extra = []
    if args.live:
        env_path = ROOT / args.env
        try:
            records = load_mysql_records(env_path)
            source = f"mysql:{env_path.name}"
        except Exception as exc:
            extra.append(f"镜像库扫描失败，已回退 CSV：{type(exc).__name__}: {exc}")
            records = load_csv_records(ROOT / args.csv)
            source = f"csv:{args.csv}（live 失败回退）"
    else:
        records = load_csv_records(ROOT / args.csv)
        source = f"csv:{args.csv}"

    analysis = analyze_buyer_records(records)
    report_path = ROOT / args.report
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(render_report(analysis, source=source, extra_notes=extra), encoding="utf-8")
    json_path = report_path.with_suffix(".json")
    json_path.write_text(json.dumps(analysis, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"report {report_path}")
    print(f"auto {analysis['autoCount']} review {analysis['reviewCount']}")

    if not args.seed:
        return 0
    store = AgentStore(ROOT / args.database)
    users = UserRepository(store)
    result = users.seed_clusters(
        cluster_staff_names(records),
        include_review=args.include_review,
    )
    confirmed = users.apply_confirmed_identities()
    linked = users.attach_staff_bindings()
    print(
        f"seed created={result['createdCount']} reused={result['reusedCount']} "
        f"skipped={result['skippedCount']} confirmed={confirmed['appliedCount']} "
        f"bindings_linked={linked}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
