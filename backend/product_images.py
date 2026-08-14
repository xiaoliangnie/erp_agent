# -*- coding: utf-8 -*-
"""商品图片缓存和 ERP 浏览器同步任务。"""
from __future__ import annotations

import base64
import binascii
import json
import re
import secrets
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
CONFIG_IMAGE_DIR = ROOT / "config" / "product-images"
IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".webp")
MAX_IMAGE_BYTES = 10 * 1024 * 1024
WORKER_ID_RE = re.compile(r"^[A-Za-z0-9_.-]{1,120}$")
SYNC_TIMEOUT_SECONDS = 5 * 60
MAX_CLAIM_ATTEMPTS = 3


class ProductImageError(ValueError):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _parse_ts(value) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _is_stale(stamp, timeout_seconds: int, *, now: datetime | None = None) -> bool:
    parsed = _parse_ts(stamp)
    if parsed is None:
        return False
    current = now or datetime.now(timezone.utc)
    return (current - parsed).total_seconds() >= timeout_seconds


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    names = {info[1] for info in conn.execute(f"PRAGMA table_info({table})")}
    if column not in names:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _loads(value: str | None, default: Any) -> Any:
    try:
        return json.loads(value) if value else default
    except (TypeError, json.JSONDecodeError):
        return default


def _safe_key(value: str) -> str:
    value = str(value or "").strip()
    return value if value and Path(value).name == value and re.fullmatch(r"[A-Za-z0-9_.-]{1,160}", value) else ""


def _existing_image(directory: Path, *keys: str) -> Path | None:
    for key in keys:
        safe = _safe_key(key)
        if not safe:
            continue
        for suffix in IMAGE_SUFFIXES:
            path = directory / f"{safe}{suffix}"
            if path.is_file():
                return path.resolve()
    return None


def resolve_product_image(product: dict, *, sku: str, style: str,
                          cache_dir: Path | None = None, root: Path | None = None) -> dict:
    """按配置路径 → SKU 配置目录 → ERP 缓存目录解析图片。"""
    base = Path(root) if root is not None else ROOT
    configured = product.get("image_path")
    if configured:
        path = Path(str(configured)).expanduser()
        path = path if path.is_absolute() else base / path
        if path.is_file() and path.suffix.lower() in IMAGE_SUFFIXES:
            return {"path": str(path.resolve()), "source": "商品映射", "status": "ready", "error": ""}
    local = _existing_image(base / "config" / "product-images", sku, style)
    if local:
        return {"path": str(local), "source": "SKU 本地图片", "status": "ready", "error": ""}
    cached = _existing_image(cache_dir or base / "data" / "product-images", sku, style)
    if cached:
        return {"path": str(cached), "source": "聚水潭接口缓存", "status": "ready", "error": ""}
    return {
        "path": "", "source": "", "status": "missing",
        "error": "镜像库尚无此商品图片，且供应链 API / 聚水潭 Worker 尚未同步成功",
    }


class ProductImageService:
    """持久化只读图片同步任务；浏览器 worker 负责使用 ERP 登录态取图。"""

    def __init__(
        self,
        database_path: str | Path,
        image_dir: str | Path,
        *,
        sync_timeout_seconds: int = SYNC_TIMEOUT_SECONDS,
        max_claim_attempts: int = MAX_CLAIM_ATTEMPTS,
    ):
        self.database_path = Path(database_path)
        self.image_dir = Path(image_dir)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.image_dir.mkdir(parents=True, exist_ok=True)
        self.sync_timeout_seconds = max(1, int(sync_timeout_seconds))
        self.max_claim_attempts = max(1, int(max_claim_attempts))
        self._lock = threading.RLock()
        with self._connect() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS product_image_jobs (
                    id TEXT PRIMARY KEY,
                    po_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    targets_json TEXT NOT NULL,
                    progress_json TEXT NOT NULL DEFAULT '[]',
                    worker_id TEXT,
                    error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    finished_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_product_image_jobs_status_created
                    ON product_image_jobs(status, created_at);
            """)
            _ensure_column(conn, "product_image_jobs", "claimed_at", "TEXT")
            _ensure_column(conn, "product_image_jobs", "attempts", "INTEGER NOT NULL DEFAULT 0")

    def _connect(self):
        conn = sqlite3.connect(self.database_path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode = WAL")
        return conn

    @staticmethod
    def _claim_stamp(row) -> str | None:
        keys = row.keys()
        claimed = row["claimed_at"] if "claimed_at" in keys else None
        return str(claimed or row["updated_at"] or "") or None

    @staticmethod
    def _attempts(row) -> int:
        keys = row.keys()
        if "attempts" not in keys:
            return 0
        try:
            return int(row["attempts"] or 0)
        except (TypeError, ValueError):
            return 0

    def _reclaim_locked(self, conn, now: str) -> None:
        now_dt = _parse_ts(now) or datetime.now(timezone.utc)
        rows = conn.execute("SELECT * FROM product_image_jobs WHERE status='syncing'").fetchall()
        for row in rows:
            if not _is_stale(self._claim_stamp(row), self.sync_timeout_seconds, now=now_dt):
                continue
            attempts = self._attempts(row) + 1
            if attempts >= self.max_claim_attempts:
                conn.execute(
                    """UPDATE product_image_jobs SET status='failed', attempts=?, worker_id=NULL,
                       claimed_at=NULL, error=?, updated_at=?, finished_at=? WHERE id=?""",
                    (attempts, "图片同步领取超时，已超过最大重试次数", now, now, row["id"]),
                )
            else:
                conn.execute(
                    """UPDATE product_image_jobs SET status='pending', attempts=?, worker_id=NULL,
                       claimed_at=NULL, updated_at=? WHERE id=?""",
                    (attempts, now, row["id"]),
                )

    def _reclaim_now(self) -> None:
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._reclaim_locked(conn, _now())

    def create(self, po_id: str, items: list[dict]) -> dict:
        po_id = str(po_id or "").strip()
        if not po_id.isdigit():
            raise ProductImageError("采购单号格式不正确")
        targets = []
        for item in items:
            sku = str(item.get("sku_id") or "").strip()
            style = str(item.get("i_id") or "").strip()
            if sku and not resolve_product_image({}, sku=sku, style=style, cache_dir=self.image_dir)["path"]:
                targets.append({"sku": sku, "style": style})
        now = _now()
        self._reclaim_now()
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                """SELECT * FROM product_image_jobs WHERE po_id=? AND status IN ('pending','syncing')
                   ORDER BY created_at DESC LIMIT 1""", (po_id,),
            ).fetchone()
            if existing:
                return self._row(existing)
            job_id = secrets.token_hex(12)
            status = "done" if not targets else "pending"
            finished = now if status == "done" else None
            conn.execute(
                """INSERT INTO product_image_jobs
                   (id,po_id,status,targets_json,created_at,updated_at,finished_at)
                   VALUES (?,?,?,?,?,?,?)""",
                (job_id, po_id, status, _json(targets), now, now, finished),
            )
            return self._row(conn.execute("SELECT * FROM product_image_jobs WHERE id=?", (job_id,)).fetchone())

    def get(self, job_id: str) -> dict:
        self._reclaim_now()
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM product_image_jobs WHERE id=?", (job_id,)).fetchone()
            job = self._row(row) if row else None
        if not job:
            raise ProductImageError("图片同步任务不存在", 404)
        return job

    def next(self, worker_id: str) -> dict | None:
        worker_id = self._worker(worker_id)
        now = _now()
        self._reclaim_now()
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM product_image_jobs WHERE status='pending' ORDER BY created_at LIMIT 1"
            ).fetchone()
            if not row:
                return None
            conn.execute(
                "UPDATE product_image_jobs SET status='syncing',worker_id=?,claimed_at=?,updated_at=? WHERE id=?",
                (worker_id, now, now, row["id"]),
            )
            return self._row(conn.execute("SELECT * FROM product_image_jobs WHERE id=?", (row["id"],)).fetchone())

    def upload(self, job_id: str, worker_id: str, payload: dict) -> dict:
        worker_id = self._worker(worker_id)
        sku = _safe_key(payload.get("sku"))
        if not sku:
            raise ProductImageError("SKU 格式不正确")
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = self._must_syncing(conn, job_id, worker_id)
            targets = {item["sku"] for item in _loads(row["targets_json"], [])}
            if sku not in targets:
                raise ProductImageError("上传了非目标 SKU 图片", 409)
            mime = str(payload.get("mimeType") or "").lower().split(";", 1)[0]
            suffix = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp"}.get(mime)
            if not suffix:
                raise ProductImageError("只接受 PNG、JPEG 或 WebP 图片")
            try:
                data = base64.b64decode(str(payload.get("imageBase64") or ""), validate=True)
            except (ValueError, binascii.Error) as exc:
                raise ProductImageError("图片 Base64 格式不正确") from exc
            if not data or len(data) > MAX_IMAGE_BYTES:
                raise ProductImageError("图片为空或超过 10MB")
            if mime == "image/png" and not data.startswith(b"\x89PNG\r\n\x1a\n"):
                raise ProductImageError("PNG 文件签名不正确")
            if mime == "image/jpeg" and not data.startswith(b"\xff\xd8\xff"):
                raise ProductImageError("JPEG 文件签名不正确")
            destination = self.image_dir / f"{sku}{suffix}"
            destination.write_bytes(data)
            progress = _loads(row["progress_json"], [])
            progress = [item for item in progress if item.get("sku") != sku]
            progress.append({"sku": sku, "status": "ready", "sourceUrl": str(payload.get("sourceUrl") or "")[:1000]})
            conn.execute(
                "UPDATE product_image_jobs SET progress_json=?,updated_at=? WHERE id=?",
                (_json(progress), _now(), job_id),
            )
        return {"ok": True, "sku": sku}

    def finish(self, job_id: str, worker_id: str, result: dict) -> dict:
        worker_id = self._worker(worker_id)
        now = _now()
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = self._must_syncing(conn, job_id, worker_id)
            failed = result.get("failed") if isinstance(result.get("failed"), list) else []
            progress = _loads(row["progress_json"], [])
            status = "done" if progress and not failed else "failed"
            error = str(result.get("error") or "")[:1000] or ("部分或全部商品缺少 ERP 图片" if failed else None)
            conn.execute(
                """UPDATE product_image_jobs SET status=?,error=?,updated_at=?,finished_at=? WHERE id=?""",
                (status, error, now, now, job_id),
            )
            return self._row(conn.execute("SELECT * FROM product_image_jobs WHERE id=?", (job_id,)).fetchone())

    @staticmethod
    def _worker(value: str) -> str:
        value = str(value or "").strip()
        if not WORKER_ID_RE.fullmatch(value):
            raise ProductImageError("worker_id 格式不正确")
        return value

    @staticmethod
    def _must_syncing(conn, job_id, worker_id):
        row = conn.execute("SELECT * FROM product_image_jobs WHERE id=?", (job_id,)).fetchone()
        if not row:
            raise ProductImageError("图片同步任务不存在", 404)
        if row["status"] != "syncing" or row["worker_id"] != worker_id:
            raise ProductImageError("图片同步任务状态或 Worker 不匹配", 409)
        return row

    @staticmethod
    def _row(row) -> dict:
        return {
            "id": row["id"], "purchaseOrderNo": row["po_id"], "status": row["status"],
            "targets": _loads(row["targets_json"], []), "progress": _loads(row["progress_json"], []),
            "workerId": row["worker_id"], "error": row["error"],
            "attempts": ProductImageService._attempts(row),
            "claimedAt": row["claimed_at"] if "claimed_at" in row.keys() else None,
            "createdAt": row["created_at"], "updatedAt": row["updated_at"], "finishedAt": row["finished_at"],
        }
