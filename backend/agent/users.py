# -*- coding: utf-8 -*-
"""内部 User 主表。

users 是身份事实主体；staff_bindings 仍是钉钉/网页兼容层。
解析采购员时不得静默建用户。
"""
from __future__ import annotations

import secrets
from dataclasses import dataclass

from ..staff_names import (
    NameCluster,
    buyer_names_equivalent,
    cluster_staff_names,
    normalize_staff_name,
    parse_staff_name,
)
from .store import AgentStore, dumps, loads, now


def new_user_id() -> str:
    return "usr_" + secrets.token_hex(5)


# 人工确认后才写入。自动聚类不得引用这份名单去猜。
CONFIRMED_IDENTITIES = (
    {
        "real_name": "韩立",
        "canonical_name": "韩立",
        "display_name": "韩立",
        "aliases": ("韩立", "管理员"),
        "source": "manual",
        "confirmed_on": "2026-08-17",
        "note": "韩立是 ERP「管理员」账号本人；钉钉已绑定。",
    },
)


def is_confirmed_admin_name(name: str) -> bool:
    """韩立 / ERP「管理员」视为钉钉管理员，不依赖 bindings.role。"""
    name = str(name or "").strip()
    if not name:
        return False
    for item in CONFIRMED_IDENTITIES:
        aliases = tuple(item.get("aliases") or ())
        if "管理员" not in aliases and item.get("canonical_name") != "韩立":
            continue
        candidates = (
            item.get("canonical_name"),
            item.get("real_name"),
            item.get("display_name"),
            *aliases,
        )
        for candidate in candidates:
            if candidate and buyer_names_equivalent(name, candidate, include_nick=True):
                return True
    return False


# 先不合并，只记录。重新跑 seed 也不会把它们建成 User。
DEFERRED_ALIAS_REVIEWS = (
    {"aliases": ("黄娟25",), "related": "黄娟", "reason": "digit_suffix", "note": "先不管"},
    {"aliases": ("戴启伟17",), "related": "戴启伟", "reason": "digit_suffix", "note": "先不管"},
    {"aliases": ("熊凯丽1&10",), "related": "熊凯丽", "reason": "digit_suffix", "note": "先不管"},
    {"aliases": ("魏大可（01）",), "related": "", "reason": "code_in_parentheses", "note": "先不管"},
    {"aliases": ("吴慧（03）",), "related": "", "reason": "code_in_parentheses", "note": "先不管"},
    {"aliases": ("小溪-商品实习生",), "related": "", "reason": "role_or_temp", "note": "先不管"},
    {"aliases": ("临时-张龙祥", "临时工-夏梓轩", "临时工-涂猛", "临时工-赵祥兵"), "related": "", "reason": "temp_worker", "note": "先不管"},
    {"aliases": ("杭州仓-利特",), "related": "李佳冬/利特", "reason": "warehouse_prefix", "note": "先不管，未并入利特"},
    {"aliases": ("无际云帆-协助机", "邹灵念11"), "related": "", "reason": "not_person_like", "note": "先不管"},
)


@dataclass(frozen=True)
class UserResolution:
    matched: bool
    user_id: str = ""
    canonical_name: str = ""
    display_name: str = ""
    matched_alias: str = ""
    confidence: str = ""
    reason: str = ""
    aliases: tuple[str, ...] = ()

    def as_public(self) -> dict:
        if not self.matched:
            return {"matched": False, "reason": self.reason or "unknown_buyer"}
        return {
            "matched": True,
            "user_id": self.user_id,
            "canonical_name": self.canonical_name,
            "display_name": self.display_name,
            "matched_alias": self.matched_alias,
            "confidence": self.confidence,
            "aliases": list(self.aliases),
        }


class UserRepository:
    def __init__(self, store: AgentStore):
        self.store = store

    def create(self, *, real_name: str = "", nickname: str = "", canonical_name: str,
               display_name: str = "", aliases=(), dingtalk_userid: str = "",
               dingtalk_unionid: str = "", mobile: str = "", department: str = "",
               status: str = "active", source: str = "manual", user_id: str = "") -> dict:
        user_id = str(user_id or "").strip() or new_user_id()
        canonical_name = str(canonical_name or "").strip()
        if not canonical_name:
            raise ValueError("canonical_name 不能为空")
        aliases = _unique_aliases(aliases, extra=(canonical_name, display_name, real_name, nickname))
        stamp = now()
        with self.store.write() as conn:
            conn.execute(
                """INSERT INTO users
                   (user_id, real_name, nickname, canonical_name, display_name,
                    erp_buyer_aliases_json, dingtalk_userid, dingtalk_unionid,
                    mobile, department, status, source, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    user_id, str(real_name or "").strip(), str(nickname or "").strip(),
                    canonical_name, str(display_name or canonical_name).strip(),
                    dumps(aliases), str(dingtalk_userid or "").strip(),
                    str(dingtalk_unionid or "").strip(), str(mobile or "").strip(),
                    str(department or "").strip(), str(status or "active").strip() or "active",
                    str(source or "manual").strip() or "manual", stamp, stamp,
                ),
            )
        return self.get(user_id)

    def get(self, user_id: str) -> dict:
        user_id = str(user_id or "").strip()
        if not user_id:
            return {}
        with self.store.read() as conn:
            row = conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()
        return _row(row) if row else {}

    def list(self, *, status: str = "") -> list[dict]:
        sql = "SELECT * FROM users"
        params: tuple = ()
        if status:
            sql += " WHERE status = ?"
            params = (status,)
        sql += " ORDER BY canonical_name"
        with self.store.read() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [_row(row) for row in rows]

    def update_binding(self, user_id: str, *, dingtalk_userid: str = "",
                       mobile: str = "", aliases=()) -> dict:
        current = self.get(user_id)
        if not current:
            raise ValueError("用户不存在")
        next_aliases = _unique_aliases(current.get("erpBuyerAliases") or (), extra=aliases)
        with self.store.write() as conn:
            conn.execute(
                """UPDATE users
                   SET dingtalk_userid=CASE WHEN ?!='' THEN ? ELSE dingtalk_userid END,
                       mobile=CASE WHEN ?!='' THEN ? ELSE mobile END,
                       erp_buyer_aliases_json=?,
                       updated_at=?
                   WHERE user_id=?""",
                (
                    str(dingtalk_userid or "").strip(), str(dingtalk_userid or "").strip(),
                    str(mobile or "").strip(), str(mobile or "").strip(),
                    dumps(next_aliases), now(), user_id,
                ),
            )
        return self.get(user_id)

    def resolve_by_erp_buyer(self, name: str) -> UserResolution:
        """按 ERP 采购员署名解析 User。匹配不到不建用户。"""
        raw = str(name or "").strip()
        if not raw:
            return UserResolution(False, reason="empty_buyer")
        users = [item for item in self.list(status="active")]
        if not users:
            return UserResolution(False, reason="unknown_buyer")
        normalized = normalize_staff_name(raw)
        exact = []
        alias_hits = []
        nick_hits = []
        for user in users:
            aliases = list(user.get("erpBuyerAliases") or ())
            for alias in aliases:
                if alias == raw or normalize_staff_name(alias) == normalized:
                    exact.append((user, alias, "exact" if alias == raw else "normalized"))
                    break
            else:
                if any(buyer_names_equivalent(raw, alias) for alias in aliases):
                    alias_hits.append((user, _best_alias(raw, aliases), "alias"))
                elif any(buyer_names_equivalent(raw, alias, include_nick=True) for alias in aliases):
                    nick_hits.append((user, _best_alias(raw, aliases, include_nick=True), "nickname"))
        chosen = exact or alias_hits or nick_hits
        if not chosen:
            return UserResolution(False, reason="unknown_buyer")
        unique_ids = {item[0]["userId"] for item in chosen}
        if len(unique_ids) > 1:
            return UserResolution(False, reason="ambiguous_buyer")
        user, alias, confidence = chosen[0]
        return UserResolution(
            True,
            user_id=user["userId"],
            canonical_name=user["canonicalName"],
            display_name=user["displayName"],
            matched_alias=alias,
            confidence=confidence,
            aliases=tuple(user.get("erpBuyerAliases") or ()),
        )

    def resolve_by_dingtalk(self, dingtalk_userid: str) -> UserResolution:
        user_id = str(dingtalk_userid or "").strip()
        if not user_id:
            return UserResolution(False, reason="empty_dingtalk")
        with self.store.read() as conn:
            row = conn.execute(
                """SELECT * FROM users
                   WHERE dingtalk_userid = ? AND status = 'active'
                   ORDER BY updated_at DESC LIMIT 1""",
                (user_id,),
            ).fetchone()
        if not row:
            return UserResolution(False, reason="unknown_dingtalk")
        user = _row(row)
        return UserResolution(
            True,
            user_id=user["userId"],
            canonical_name=user["canonicalName"],
            display_name=user["displayName"],
            matched_alias=user_id,
            confidence="dingtalk",
            aliases=tuple(user.get("erpBuyerAliases") or ()),
        )

    def seed_clusters(self, clusters, *, source: str = "purchase_order_seed",
                      include_review: bool = False) -> dict:
        """把已确认聚类写入 users。needs_review 默认不建用户。"""
        created, reused, skipped = [], [], []
        for cluster in clusters or ():
            if isinstance(cluster, NameCluster):
                payload = cluster
            else:
                payload = NameCluster(
                    aliases=list(cluster.get("aliases") or ()),
                    status=str(cluster.get("status") or "auto"),
                    reason=str(cluster.get("reason") or ""),
                    real_name=str(cluster.get("real_name") or cluster.get("realName") or ""),
                    nickname=str(cluster.get("nickname") or ""),
                    canonical_name=str(cluster.get("canonical_name") or cluster.get("canonicalName") or ""),
                    display_name=str(cluster.get("display_name") or cluster.get("displayName") or ""),
                )
            if payload.status != "auto" and not include_review:
                skipped.append(payload.as_public())
                continue
            if not payload.canonical_name and not payload.aliases:
                skipped.append(payload.as_public())
                continue
            existing = None
            for alias in payload.aliases:
                hit = self.resolve_by_erp_buyer(alias)
                if hit.matched:
                    existing = self.get(hit.user_id)
                    break
            if existing:
                self.update_binding(existing["userId"], aliases=payload.aliases)
                reused.append(self.get(existing["userId"]))
                continue
            created.append(self.create(
                real_name=payload.real_name,
                nickname=payload.nickname,
                canonical_name=payload.canonical_name or payload.aliases[0],
                display_name=payload.display_name or payload.canonical_name,
                aliases=payload.aliases,
                source=source,
            ))
        return {
            "created": created,
            "reused": reused,
            "skipped": skipped,
            "createdCount": len(created),
            "reusedCount": len(reused),
            "skippedCount": len(skipped),
        }

    def update_profile(self, user_id: str, *, real_name: str = "", nickname: str = "",
                       canonical_name: str = "", display_name: str = "",
                       source: str = "", department: str = "") -> dict:
        current = self.get(user_id)
        if not current:
            raise ValueError("用户不存在")
        next_canonical = str(canonical_name or current["canonicalName"]).strip()
        if not next_canonical:
            raise ValueError("canonical_name 不能为空")
        with self.store.write() as conn:
            conn.execute(
                """UPDATE users
                   SET real_name=?, nickname=?, canonical_name=?, display_name=?,
                       source=?, department=?, updated_at=?
                   WHERE user_id=?""",
                (
                    str(real_name or current["realName"]).strip(),
                    str(nickname or current["nickname"]).strip(),
                    next_canonical,
                    str(display_name or current["displayName"] or next_canonical).strip(),
                    str(source or current["source"]).strip() or current["source"],
                    str(department or current["department"]).strip() or current["department"],
                    now(), user_id,
                ),
            )
        return self.get(user_id)

    def disable(self, user_id: str) -> dict:
        current = self.get(user_id)
        if not current:
            raise ValueError("用户不存在")
        with self.store.write() as conn:
            conn.execute(
                "UPDATE users SET status='disabled', updated_at=? WHERE user_id=?",
                (now(), user_id),
            )
        return self.get(user_id)

    def apply_confirmed_identities(self, identities=None) -> dict:
        """写入人工确认的身份。可合并已有自动种子，不猜未确认别名。"""
        applied = []
        for item in identities if identities is not None else CONFIRMED_IDENTITIES:
            aliases = _unique_aliases(
                item.get("aliases") or (),
                extra=(item.get("canonical_name"), item.get("real_name"), item.get("display_name")),
            )
            matches = _users_matching_aliases(self, aliases)
            keep = _pick_keep_user(matches, item)
            if keep is None:
                keep = self.create(
                    real_name=str(item.get("real_name") or ""),
                    nickname=str(item.get("nickname") or ""),
                    canonical_name=str(item.get("canonical_name") or aliases[0]),
                    display_name=str(item.get("display_name") or item.get("canonical_name") or ""),
                    aliases=aliases,
                    source=str(item.get("source") or "manual"),
                )
            else:
                extra_aliases = []
                for other in matches:
                    extra_aliases.extend(other.get("erpBuyerAliases") or ())
                    if other["userId"] != keep["userId"] and other.get("dingtalkUserId"):
                        self.update_binding(keep["userId"], dingtalk_userid=other["dingtalkUserId"])
                self.update_profile(
                    keep["userId"],
                    real_name=str(item.get("real_name") or keep["realName"]),
                    nickname=str(item.get("nickname") or keep["nickname"]),
                    canonical_name=str(item.get("canonical_name") or keep["canonicalName"]),
                    display_name=str(item.get("display_name") or item.get("canonical_name") or keep["displayName"]),
                    source=str(item.get("source") or keep["source"]),
                )
                self.update_binding(keep["userId"], aliases=list(aliases) + extra_aliases)
                for other in matches:
                    if other["userId"] != keep["userId"]:
                        self.disable(other["userId"])
                keep = self.get(keep["userId"])
            applied.append(keep)
        return {"applied": applied, "appliedCount": len(applied)}

    def attach_staff_bindings(self) -> int:
        """给已有 staff_bindings 回填 user_id，不改绑定姓名。"""
        updated = 0
        with self.store.write() as conn:
            rows = conn.execute("SELECT buyer_name, dingtalk_user_id, user_id FROM staff_bindings").fetchall()
        for row in rows:
            buyer = row["buyer_name"]
            current = row["user_id"] if "user_id" in row.keys() else ""
            hit = self.resolve_by_erp_buyer(buyer)
            if not hit.matched:
                continue
            if current == hit.user_id:
                if row["dingtalk_user_id"]:
                    self.update_binding(hit.user_id, dingtalk_userid=row["dingtalk_user_id"])
                continue
            with self.store.write() as conn:
                conn.execute(
                    "UPDATE staff_bindings SET user_id=? WHERE buyer_name=?",
                    (hit.user_id, buyer),
                )
            if row["dingtalk_user_id"]:
                self.update_binding(hit.user_id, dingtalk_userid=row["dingtalk_user_id"])
            updated += 1
        return updated


def resolve_user_by_erp_buyer(users: UserRepository, name: str) -> UserResolution:
    return users.resolve_by_erp_buyer(name)


def link_binding_to_user(users: UserRepository, buyer_name: str,
                         *, dingtalk_userid: str = "", mobile: str = "") -> str:
    """把已有 staff_bindings 行挂到 users.user_id。匹配不到不建用户。"""
    hit = users.resolve_by_erp_buyer(buyer_name)
    if not hit.matched:
        return ""
    users.update_binding(
        hit.user_id, dingtalk_userid=dingtalk_userid, mobile=mobile, aliases=(buyer_name,),
    )
    with users.store.write() as conn:
        conn.execute(
            "UPDATE staff_bindings SET user_id=? WHERE buyer_name=?",
            (hit.user_id, str(buyer_name or "").strip()),
        )
    return hit.user_id


def analyze_buyer_records(records) -> dict:
    clusters = cluster_staff_names(records)
    auto = [item for item in clusters if item.status == "auto"]
    review = [item for item in clusters if item.status != "auto"]
    return {
        "rawCount": len(records or ()),
        "clusterCount": len(clusters),
        "autoCount": len(auto),
        "reviewCount": len(review),
        "clusters": [item.as_public() for item in clusters],
        "auto": [item.as_public() for item in auto],
        "needsReview": [item.as_public() for item in review],
    }


def _users_matching_aliases(repo: UserRepository, aliases) -> list[dict]:
    seen = set()
    matches = []
    wanted = {normalize_staff_name(name) for name in aliases if name}
    wanted_raw = {str(name).strip() for name in aliases if str(name).strip()}
    for alias in aliases:
        hit = repo.resolve_by_erp_buyer(alias)
        if hit.matched and hit.user_id not in seen:
            seen.add(hit.user_id)
            matches.append(repo.get(hit.user_id))
    for user in repo.list(status="active"):
        if user["userId"] in seen:
            continue
        names = [
            user.get("canonicalName"), user.get("realName"), user.get("displayName"),
            *(user.get("erpBuyerAliases") or ()),
        ]
        if any(str(name).strip() in wanted_raw or normalize_staff_name(name) in wanted
               for name in names if name):
            seen.add(user["userId"])
            matches.append(user)
    return matches


def _pick_keep_user(matches, item) -> dict | None:
    if not matches:
        return None
    canonical = str(item.get("canonical_name") or "").strip()
    real_name = str(item.get("real_name") or "").strip()
    for user in matches:
        if user.get("canonicalName") == canonical or user.get("realName") == real_name:
            return user
    for user in matches:
        if user.get("dingtalkUserId"):
            return user
    return matches[0]


def _unique_aliases(aliases, extra=()) -> list[str]:
    seen = set()
    result = []
    for item in list(aliases or ()) + list(extra or ()):
        name = str(item or "").strip()
        if not name or name in seen:
            continue
        seen.add(name)
        result.append(name)
    return result


def _best_alias(name: str, aliases, *, include_nick: bool = False) -> str:
    parsed = parse_staff_name(name)
    for alias in aliases:
        if alias == name or normalize_staff_name(alias) == parsed.normalized:
            return alias
    for alias in aliases:
        if buyer_names_equivalent(name, alias, include_nick=include_nick):
            return alias
    return aliases[0] if aliases else name


def _row(row) -> dict:
    aliases = loads(row["erp_buyer_aliases_json"], []) or []
    if not isinstance(aliases, list):
        aliases = []
    return {
        "userId": row["user_id"],
        "realName": row["real_name"],
        "nickname": row["nickname"],
        "canonicalName": row["canonical_name"],
        "displayName": row["display_name"],
        "erpBuyerAliases": [str(item) for item in aliases if str(item).strip()],
        "dingtalkUserId": row["dingtalk_userid"],
        "dingtalkUnionId": row["dingtalk_unionid"],
        "mobile": row["mobile"],
        "department": row["department"],
        "status": row["status"],
        "source": row["source"],
        "createdAt": row["created_at"],
        "updatedAt": row["updated_at"],
    }
