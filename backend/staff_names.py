# -*- coding: utf-8 -*-
"""采购员署名归一。

ERP 里同一个人经常同时出现花名和「真名（花名）」，例如「利特」与「李佳冬（利特）」。
催办 @ 和 L1/L2 确认人都按这套规则视为同一员工，不改采购明细上的原始署名。
网页发起或确认 L1/L2 时，署名必须能对上员工绑定表里的某一条（花名或「真名（花名）」均可）。
"""
from __future__ import annotations

import re
import unicodedata
from collections import Counter
from dataclasses import dataclass, field

WEB_OPERATOR_UNBOUND = (
    "网页操作人未在员工绑定表中，不能发起或确认需要确认的动作。"
    "请填写与钉钉/采购员一致的姓名，或先在钉钉回复「绑定 姓名」。"
)

VIEWER_WRITE_DENIED = "当前角色是 viewer，只能查询，不能生成合同、换货或外发。"

SELF_SCOPE_UNBOUND = (
    "还没绑定采购员姓名，无法按「我名下」过滤。"
    "网页请署名已绑定的采购员，钉钉回复「绑定 姓名」。"
)


_PAREN_TAIL = re.compile(r"^(.*?)[\(（]([^）\)]+)[\)）]\s*$")
_PAREN_NORMAL = re.compile(r"^(.*)\(([^)]+)\)\s*$")
_NAME_SPLIT = re.compile(r"[,，、/;；]+")
_DIGITS = re.compile(r"\d")
_TRAILING_DIGITS = re.compile(r"^(.*?)(\d+)$")
_CJK = re.compile(r"[\u4e00-\u9fff]")


def normalize_staff_name(name: str) -> str:
    """统一全角/半角、括号和空白，不改汉字本身。"""
    text = unicodedata.normalize("NFKC", str(name or ""))
    text = text.replace("\u3000", " ")
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r"\s*\(\s*", "(", text)
    text = re.sub(r"\s*\)\s*", ")", text)
    return text


@dataclass(frozen=True)
class StaffName:
    raw: str
    normalized: str
    outer: str
    inner: str

    @property
    def tokens(self) -> tuple[str, ...]:
        return tuple(part for part in (self.outer, self.inner) if part)

    @property
    def pair(self) -> frozenset[str] | None:
        if self.outer and self.inner:
            return frozenset({self.outer, self.inner})
        return None

    @property
    def inner_is_code(self) -> bool:
        return bool(self.inner) and not _CJK.search(self.inner)


def parse_staff_name(name: str) -> StaffName:
    """解析采购员署名：标准化后再拆括号。"""
    raw = str(name or "").strip()
    normalized = normalize_staff_name(raw)
    match = _PAREN_NORMAL.match(normalized)
    if match:
        outer, inner = match.group(1).strip(), match.group(2).strip()
    else:
        outer, inner = normalized, ""
    return StaffName(raw=raw, normalized=normalized, outer=outer, inner=inner)


def _personal_tokens(parsed: StaffName) -> list[str]:
    names = []
    if not parsed.inner and _looks_like_personal_name(parsed.normalized):
        names.append(parsed.normalized)
    if parsed.outer and _looks_like_personal_name(parsed.outer):
        names.append(parsed.outer)
    if parsed.inner and not parsed.inner_is_code and _looks_like_personal_name(parsed.inner):
        names.append(parsed.inner)
    return names


def _cluster_real_and_nick(parsed_rows: list[StaffName]) -> tuple[str, str]:
    """展示用真名/花名：只从像人名的片段里选，岗位外名不当真名。"""
    scores: Counter[str] = Counter()
    outer_bonus = set()
    for row in parsed_rows:
        for name in _personal_tokens(row):
            scores[name] += 1
        if row.inner and row.outer and _looks_like_personal_name(row.outer):
            outer_bonus.add(row.outer)
    if not scores:
        first = parsed_rows[0]
        return first.outer, first.inner if not first.inner_is_code else ""

    def rank(name: str) -> tuple:
        return (len(name), name in outer_bonus, scores[name])

    real = max(scores, key=rank)
    others = sorted((name for name in scores if name != real), key=rank, reverse=True)
    return real, others[0] if others else ""


_NOT_PERSON = (
    "公司", "厂", "工作室", "有限", "鞋垫", "鞋子", "理单", "涂胶",
    "供应商", "临时", "账号", "系统", "测试",
)


def _looks_like_personal_name(text: str) -> bool:
    """2–4 个汉字的人名；角色账号、数字后缀、店名不视为人名。"""
    text = normalize_staff_name(text)
    if not text:
        return False
    if any(token in text for token in _NOT_PERSON):
        return False
    if "-" in text or "/" in text or _DIGITS.search(text):
        return False
    cleaned = text.replace("·", "")
    return bool(re.fullmatch(r"[\u4e00-\u9fff]{2,4}", cleaned))


@dataclass
class NameCluster:
    aliases: list[str]
    status: str
    reason: str = ""
    real_name: str = ""
    nickname: str = ""
    canonical_name: str = ""
    display_name: str = ""
    occurrences: int = 0
    last_seen: str = ""
    source_rows: list[dict] = field(default_factory=list)

    def as_public(self) -> dict:
        return {
            "aliases": list(self.aliases),
            "status": self.status,
            "reason": self.reason,
            "realName": self.real_name,
            "nickname": self.nickname,
            "canonicalName": self.canonical_name,
            "displayName": self.display_name,
            "occurrences": self.occurrences,
            "lastSeen": self.last_seen,
        }


def cluster_staff_names(records) -> list[NameCluster]:
    """按确定性规则聚类采购员署名。不确定的进入 needs_review，不自动合并。"""
    items = []
    for record in records or ():
        if isinstance(record, str):
            raw, count, last_seen = record, 1, ""
        else:
            raw = str(record.get("raw") or record.get("name") or "").strip()
            count = int(record.get("count") or record.get("occurrences") or 1)
            last_seen = str(record.get("last_seen") or record.get("lastSeen") or "")
        if not raw:
            continue
        items.append({
            "raw": raw,
            "parsed": parse_staff_name(raw),
            "count": count,
            "last_seen": last_seen,
        })
    if not items:
        return []

    parent = list(range(len(items)))

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(left: int, right: int) -> None:
        root_left, root_right = find(left), find(right)
        if root_left != root_right:
            parent[root_right] = root_left

    pair_groups: dict[frozenset[str], list[int]] = {}
    outer_groups: dict[str, list[int]] = {}
    inner_groups: dict[str, list[int]] = {}
    standalone: dict[str, list[int]] = {}
    for index, item in enumerate(items):
        parsed = item["parsed"]
        if parsed.pair and not parsed.inner_is_code:
            pair_groups.setdefault(parsed.pair, []).append(index)
        if parsed.outer:
            outer_groups.setdefault(parsed.outer, []).append(index)
        if parsed.inner and not parsed.inner_is_code:
            inner_groups.setdefault(parsed.inner, []).append(index)
        if not parsed.inner:
            standalone.setdefault(parsed.normalized, []).append(index)

    for indexes in pair_groups.values():
        for index in indexes[1:]:
            union(indexes[0], index)

    review_links: list[tuple[int, int, str]] = []

    def unique_combined_roots(indexes: list[int]) -> list[int]:
        roots = []
        for index in indexes:
            if items[index]["parsed"].pair and not items[index]["parsed"].inner_is_code:
                root = find(index)
                if root not in roots:
                    roots.append(root)
        return roots

    for name, indexes in standalone.items():
        parsed = items[indexes[0]]["parsed"]
        if _TRAILING_DIGITS.match(parsed.normalized) and _DIGITS.search(parsed.normalized):
            continue
        outer_hits = unique_combined_roots(outer_groups.get(name, []))
        inner_hits = unique_combined_roots(inner_groups.get(name, []))
        hits = []
        for root in outer_hits + inner_hits:
            if root not in hits:
                hits.append(root)
        if len(hits) == 1:
            combined_outer = items[hits[0]]["parsed"].outer
            # 真名（花名）可合并；岗位（花名）在花名唯一时也可合并，例如「鞋子理单（三三）」=「三三」。
            if _looks_like_personal_name(combined_outer) or _looks_like_personal_name(name):
                for index in indexes:
                    union(hits[0], index)
        elif len(hits) > 1:
            for index in indexes:
                for root in hits:
                    review_links.append((index, root, "standalone_matches_multiple"))

    clusters: dict[int, NameCluster] = {}
    for index, item in enumerate(items):
        root = find(index)
        cluster = clusters.get(root)
        if cluster is None:
            cluster = NameCluster(aliases=[], status="auto")
            clusters[root] = cluster
        if item["raw"] not in cluster.aliases:
            cluster.aliases.append(item["raw"])
        cluster.occurrences += item["count"]
        if item["last_seen"] >= cluster.last_seen:
            cluster.last_seen = item["last_seen"]
        cluster.source_rows.append(item)

    review_roots = set()
    for left, right, reason in review_links:
        review_roots.add(find(left))
        review_roots.add(find(right))
        for root in (find(left), find(right)):
            cluster = clusters[root]
            cluster.status = "needs_review"
            if reason not in cluster.reason:
                cluster.reason = ",".join(part for part in (cluster.reason, reason) if part)

    shared_token_pairs: dict[str, set[int]] = {}
    for root, cluster in clusters.items():
        tokens = set()
        for row in cluster.source_rows:
            tokens.update(row["parsed"].tokens)
        for token in tokens:
            shared_token_pairs.setdefault(token, set()).add(root)
    for token, roots in shared_token_pairs.items():
        combined_roots = [
            root for root in roots
            if any(row["parsed"].pair for row in clusters[root].source_rows)
        ]
        if len(combined_roots) > 1:
            for root in combined_roots:
                cluster = clusters[root]
                cluster.status = "needs_review"
                if "shared_token_across_combined" not in cluster.reason:
                    cluster.reason = ",".join(
                        part for part in (cluster.reason, "shared_token_across_combined") if part
                    )

    for cluster in clusters.values():
        parsed_rows = [row["parsed"] for row in cluster.source_rows]
        cluster.real_name, cluster.nickname = _cluster_real_and_nick(parsed_rows)
        if cluster.real_name and cluster.nickname:
            cluster.canonical_name = cluster.real_name
            cluster.display_name = f"{cluster.real_name}（{cluster.nickname}）"
        else:
            cluster.canonical_name = cluster.real_name or cluster.aliases[0]
            cluster.display_name = cluster.canonical_name
        if cluster.status == "auto":
            if not _looks_like_personal_name(cluster.real_name or cluster.canonical_name):
                cluster.status = "needs_review"
                cluster.reason = ",".join(
                    part for part in (cluster.reason, "not_person_like") if part
                )
            elif any(row["parsed"].inner_is_code for row in cluster.source_rows):
                cluster.status = "needs_review"
                cluster.reason = ",".join(
                    part for part in (cluster.reason, "code_in_parentheses") if part
                )
            elif any(_TRAILING_DIGITS.match(row["parsed"].normalized) and _DIGITS.search(row["parsed"].normalized)
                     and not row["parsed"].inner for row in cluster.source_rows):
                cluster.status = "needs_review"
                cluster.reason = ",".join(
                    part for part in (cluster.reason, "trailing_digits") if part
                )
        cluster.aliases = sorted(cluster.aliases, key=lambda name: (-len(name), name))

    return sorted(clusters.values(), key=lambda item: (-item.occurrences, item.canonical_name))


def split_buyer_name(name: str) -> tuple[str, str]:
    """拆成「括号外、括号内」。没有括号则花名为空。"""
    name = str(name or "").strip()
    match = _PAREN_TAIL.match(name)
    if not match:
        return name, ""
    return match.group(1).strip(), match.group(2).strip()


def buyer_name_keys(name: str, *, include_nick: bool = False) -> set[str]:
    """用于互认的署名片段：全称、括号外；括号内花名默认不作为独立键。"""
    name = str(name or "").strip()
    if not name:
        return set()
    base, nick = split_buyer_name(name)
    keys = {part for part in (name, base) if part}
    if include_nick and nick:
        keys.add(nick)
    return keys


def buyer_names_equivalent(left: str, right: str, *, include_nick: bool = False) -> bool:
    """两个署名是否像同一个人。空串互不匹配。确认流默认不含括号内花名。"""
    keys_left = buyer_name_keys(left, include_nick=include_nick)
    keys_right = buyer_name_keys(right, include_nick=include_nick)
    return bool(keys_left and keys_right and keys_left & keys_right)


def parse_buyer_names(text: str) -> list[str]:
    """「绑定 利特、李佳冬（利特）」这种一次写多个署名。"""
    names = []
    seen = set()
    for part in _NAME_SPLIT.split(str(text or "").strip()):
        name = part.strip()
        if not name or name in seen:
            continue
        seen.add(name)
        names.append(name)
    return names
