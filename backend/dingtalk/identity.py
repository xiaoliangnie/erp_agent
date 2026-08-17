# -*- coding: utf-8 -*-
"""采购员 ↔ 钉钉身份映射。

用于群内 @ 到人，以及网页 L1/L2 判断署名是否为已知员工。
角色只有 viewer / operator 两档，不是权限矩阵。数据落 Agent 业务库
`staff_bindings`，也可以用 `config/staff_bindings.json` 做初始种子。
同一人在 ERP 里可能有花名和「真名（花名）」两套署名，绑定任一即可 @ 到。
"""
from __future__ import annotations

import json
from pathlib import Path

from ..agent.store import AgentStore, now
from ..staff_names import buyer_names_equivalent, parse_buyer_names


class StaffDirectory:
    def __init__(self, store: AgentStore):
        self.store = store

    def upsert(self, buyer_name: str, *, dingtalk_user_id: str = "", mobile: str = "",
               note: str = "", aliases=(), role: str = "") -> dict:
        names = parse_buyer_names(buyer_name)
        for alias in aliases or ():
            names.extend(parse_buyer_names(alias))
        names = parse_buyer_names("、".join(names))
        if not names:
            raise ValueError("采购员姓名不能为空")
        last = {}
        for name in names:
            last = self._upsert_one(
                name, dingtalk_user_id=dingtalk_user_id, mobile=mobile, note=note, role=role,
            )
        last["aliases"] = names
        return last

    def _upsert_one(self, buyer_name: str, *, dingtalk_user_id: str = "", mobile: str = "",
                    note: str = "", role: str = "") -> dict:
        role = str(role or "").strip().lower()
        if role and role not in ("viewer", "operator"):
            raise ValueError("角色只能是 viewer 或 operator")
        with self.store.write() as conn:
            existing = conn.execute(
                "SELECT * FROM staff_bindings WHERE buyer_name = ?", (buyer_name,),
            ).fetchone()
            kept_role = role or ((existing["role"] if existing and "role" in existing.keys() else "") or "operator")
            if existing:
                conn.execute(
                    """UPDATE staff_bindings
                       SET dingtalk_user_id=?, mobile=?, note=?, role=?, updated_at=?
                       WHERE buyer_name=?""",
                    (str(dingtalk_user_id or "").strip(), str(mobile or "").strip(),
                     str(note or "").strip(), kept_role, now(), buyer_name),
                )
            else:
                conn.execute(
                    """INSERT INTO staff_bindings
                       (buyer_name, dingtalk_user_id, mobile, note, role, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (buyer_name, str(dingtalk_user_id or "").strip(), str(mobile or "").strip(),
                     str(note or "").strip(), kept_role or "operator", now()),
                )
        return self.get(buyer_name)

    def get(self, buyer_name: str) -> dict:
        with self.store.read() as conn:
            row = conn.execute(
                "SELECT * FROM staff_bindings WHERE buyer_name = ?", (str(buyer_name or "").strip(),)
            ).fetchone()
        return self._row(row) if row else {}

    def get_by_dingtalk_user_id(self, user_id: str) -> dict:
        user_id = str(user_id or "").strip()
        if not user_id:
            return {}
        with self.store.read() as conn:
            row = conn.execute(
                "SELECT * FROM staff_bindings WHERE dingtalk_user_id = ? ORDER BY updated_at DESC LIMIT 1",
                (user_id,),
            ).fetchone()
        return self._row(row) if row else {}

    def list(self) -> list[dict]:
        with self.store.read() as conn:
            rows = conn.execute("SELECT * FROM staff_bindings ORDER BY buyer_name").fetchall()
        return [self._row(row) for row in rows]

    def find_binding(self, *, operator: str = "", actor_id: str = "") -> dict:
        """钉钉 userId 优先，其次按署名/花名命中绑定行。"""
        actor_id = str(actor_id or "").strip()
        if actor_id:
            bound = self.get_by_dingtalk_user_id(actor_id)
            if bound:
                return bound
        operator = str(operator or "").strip()
        if not operator:
            return {}
        exact = self.get(operator)
        if exact:
            return exact
        return self._match(operator, self.list())

    def known_operator(self, operator: str) -> bool:
        """网页署名是否对应绑定表里的采购员/钉钉姓名（空表失败关闭）。"""
        return bool(self.find_binding(operator=operator))

    def bound_buyer_names(self, binding: dict) -> tuple[str, ...]:
        """同一钉钉身份下的全部采购员署名，供「我名下」过滤。"""
        if not binding:
            return ()
        names: list[str] = []
        primary = str(binding.get("buyerName") or "").strip()
        if primary:
            names.append(primary)
        user_id = str(binding.get("dingtalkUserId") or "").strip()
        if user_id:
            for item in self.list():
                if item.get("dingtalkUserId") == user_id and item["buyerName"] not in names:
                    names.append(item["buyerName"])
        return tuple(names)

    def resolve(self, buyer_names) -> dict:
        """把采购员姓名换成钉钉 userId / 手机号，并列出未绑定的人。

        「利特」绑上之后，「李佳冬（利特）」也会命中同一条钉钉身份。
        """
        bindings = [item for item in self.list() if item["dingtalkUserId"] or item["mobile"]]
        user_ids, mobiles, unbound, matched = [], [], [], []
        for name in buyer_names:
            binding = self._match(str(name or "").strip(), bindings)
            if not binding:
                unbound.append(name)
                continue
            matched.append(name)
            if binding["dingtalkUserId"] and binding["dingtalkUserId"] not in user_ids:
                user_ids.append(binding["dingtalkUserId"])
            if binding["mobile"] and binding["mobile"] not in mobiles:
                mobiles.append(binding["mobile"])
        return {"userIds": user_ids, "mobiles": mobiles, "unbound": unbound, "matched": matched}

    @staticmethod
    def _match(name: str, bindings: list) -> dict:
        if not name:
            return {}
        for item in bindings:
            if item["buyerName"] == name:
                return item
        for item in bindings:
            if buyer_names_equivalent(name, item["buyerName"], include_nick=True):
                return item
        return {}

    def seed_from_json(self, path) -> int:
        """从 JSON 导入绑定；文件不存在就跳过。

        期望形状：`{"利特": {"dingtalk_user_id": "...", "mobile": "...", "aliases": ["李佳冬（利特）"]}}`
        """
        path = Path(path)
        if not path.exists():
            return 0
        payload = json.loads(path.read_text(encoding="utf-8"))
        count = 0
        for buyer_name, detail in (payload or {}).items():
            detail = detail if isinstance(detail, dict) else {}
            self.upsert(
                buyer_name,
                dingtalk_user_id=detail.get("dingtalk_user_id", ""),
                mobile=detail.get("mobile", ""),
                note=detail.get("note", ""),
                aliases=detail.get("aliases") or (),
                role=detail.get("role", ""),
            )
            count += 1
        return count

    @staticmethod
    def _row(row) -> dict:
        keys = row.keys()
        role = row["role"] if "role" in keys else "operator"
        return {
            "buyerName": row["buyer_name"],
            "dingtalkUserId": row["dingtalk_user_id"],
            "mobile": row["mobile"],
            "note": row["note"],
            "role": str(role or "operator"),
            "updatedAt": row["updated_at"],
        }
