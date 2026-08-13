# -*- coding: utf-8 -*-
"""采购员署名归一。

ERP 里同一个人经常同时出现花名和「真名（花名）」，例如「利特」与「李佳冬（利特）」。
催办 @ 和 L1/L2 确认人都按这套规则视为同一员工，不改采购明细上的原始署名。
"""
from __future__ import annotations

import re


_PAREN_TAIL = re.compile(r"^(.*?)[\(（]([^）\)]+)[\)）]\s*$")
_NAME_SPLIT = re.compile(r"[,，、/;；]+")


def split_buyer_name(name: str) -> tuple[str, str]:
    """拆成「括号外、括号内」。没有括号则花名为空。"""
    name = str(name or "").strip()
    match = _PAREN_TAIL.match(name)
    if not match:
        return name, ""
    return match.group(1).strip(), match.group(2).strip()


def buyer_name_keys(name: str) -> set[str]:
    """用于互认的署名片段：全称、括号外、括号内。"""
    name = str(name or "").strip()
    if not name:
        return set()
    base, nick = split_buyer_name(name)
    return {part for part in (name, base, nick) if part}


def buyer_names_equivalent(left: str, right: str) -> bool:
    """两个署名是否像同一个人。空串互不匹配。"""
    keys_left = buyer_name_keys(left)
    keys_right = buyer_name_keys(right)
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
