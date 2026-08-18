# -*- coding: utf-8 -*-
"""网页和钉钉共用的会话指令：新话题 / 记住 / 忘记。"""
from __future__ import annotations

import re


NEW_TOPIC_PATTERN = re.compile(r"^\s*(新话题|重置会话)\s*$")
REMEMBER_PATTERN = re.compile(r"^\s*记住\s+(.+)$")
FORGET_PATTERN = re.compile(r"^\s*忘记\s+(.+)$")


def parse_session_command(text: str) -> dict | None:
    text = str(text or "").strip()
    if not text:
        return None
    if NEW_TOPIC_PATTERN.match(text):
        return {"name": "new_topic"}
    remembered = REMEMBER_PATTERN.match(text)
    if remembered:
        return {"name": "remember", "content": remembered.group(1).strip()}
    forgotten = FORGET_PATTERN.match(text)
    if forgotten:
        return {"name": "forget", "keyword": forgotten.group(1).strip()}
    return None
