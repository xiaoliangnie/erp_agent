# -*- coding: utf-8 -*-
"""仓库根与本地文件根。

``config`` / ``templates`` / ``data`` / ``outputs`` 都在 ``files/`` 下。
``.env`` 或旧代码里仍写 ``data/app.log``、``config/buyers.json`` 时，自动映射到新位置。
测试夹具若在临时根下自建 ``config/``，则继续用那一套，不强行改到 ``files/``。
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FILES_DIR = ROOT / "files"
CONFIG_DIR = FILES_DIR / "config"
DATA_DIR = FILES_DIR / "data"
TEMPLATES_DIR = FILES_DIR / "templates"
OUTPUTS_DIR = FILES_DIR / "outputs"

LOCAL_TOPS = ("config", "data", "templates", "outputs")


def local_dir(name: str, *, root: Path | None = None) -> Path:
    """返回 config / data / templates / outputs 的实际目录。"""
    if name not in LOCAL_TOPS:
        raise ValueError(f"不是本地文件目录：{name}")
    base = Path(root) if root is not None else ROOT
    mapped = base / "files" / name
    legacy = base / name
    if mapped.exists():
        return mapped
    if legacy.exists():
        return legacy
    if root is None or base.resolve() == ROOT.resolve():
        return mapped
    return legacy


def resolve_repo_path(value, *, root: Path | None = None) -> Path:
    """相对路径相对仓库根；旧顶栏目录接到 files/ 下。"""
    text = str(value or "").strip()
    if not text:
        raise ValueError("路径为空")
    path = Path(text)
    if path.is_absolute():
        return path
    base = Path(root) if root is not None else ROOT
    parts = path.parts
    if parts and parts[0] in LOCAL_TOPS:
        folder = local_dir(parts[0], root=base)
        return folder.joinpath(*parts[1:]) if len(parts) > 1 else folder
    if parts and parts[0] == "files" and len(parts) > 1 and parts[1] in LOCAL_TOPS:
        folder = local_dir(parts[1], root=base)
        return folder.joinpath(*parts[2:]) if len(parts) > 2 else folder
    return base / path
