# -*- coding: utf-8 -*-
"""采购员 ↔ 钉钉身份映射。

只用于群内 @ 到人（架构方案 §9：员工同权，第一期不做角色矩阵），不承担权限判定。
数据落 Agent 业务库 `staff_bindings`，也可以用 `config/staff_bindings.json` 做初始种子。
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
               note: str = "", aliases=()) -> dict:
        names = parse_buyer_names(buyer_name)
        for alias in aliases or ():
            names.extend(parse_buyer_names(alias))
        names = parse_buyer_names("、".join(names))
        if not names:
            raise ValueError("采购员姓名不能为空")
        last = {}
        for name in names:
            last = self._upsert_one(
                name, dingtalk_user_id=dingtalk_user_id, mobile=mobile, note=note,
            )
        last["aliases"] = names
        return last

    def _upsert_one(self, buyer_name: str, *, dingtalk_user_id: str = "", mobile: str = "",
                    note: str = "") -> dict:
        with self.store.write() as conn:
            conn.execute(
                """INSERT INTO staff_bindings (buyer_name, dingtalk_user_id, mobile, note, updated_at)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(buyer_name) DO UPDATE SET
                     dingtalk_user_id=excluded.dingtalk_user_id, mobile=excluded.mobile,
                     note=excluded.note, updated_at=excluded.updated_at""",
                (buyer_name, str(dingtalk_user_id or "").strip(), str(mobile or "").strip(),
                 str(note or "").strip(), now()),
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
            if buyer_names_equivalent(name, item["buyerName"]):
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
            )
            count += 1
        return count

    @staticmethod
    def _row(row) -> dict:
        return {
            "buyerName": row["buyer_name"],
            "dingtalkUserId": row["dingtalk_user_id"],
            "mobile": row["mobile"],
            "note": row["note"],
            "updatedAt": row["updated_at"],
        }
