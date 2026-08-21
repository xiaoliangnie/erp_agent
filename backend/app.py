# -*- coding: utf-8 -*-
"""采购数据 API 与安全的静态页面服务。"""
import argparse
import gzip
import hashlib
import json
import logging
import mimetypes
import os
import re
import secrets
import subprocess
import threading
import time
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse

from .database import (
    REALTIME_ITEM_TABLE,
    connect,
    describe_target,
    fetch_contract_order,
    fetch_contract_order_choices,
    fetch_exchange_products,
    fetch_followup_purchase_rows,
    fetch_realtime_purchase_rows,
    fetch_realtime_sync_state,
    fetch_realtime_years,
    load_all_env,
    purchase_window_warning,
)
from .procurement_data import build_dashboard_payload, build_delivery_payload
from .contracts import generate_contract, get_contract_options
from .exchange import ExchangeError, ExchangeService
from .exchange.policy import public_policy as exchange_policy
from .agent import (
    RESERVED_TOOLS,
    ActionError,
    AgentDisabled,
    AgentStore,
    AuditLog,
    JobQueue,
    JobWorker,
    LLMError,
    MaintenanceScheduler,
    OperatorMemories,
    Outbox,
    ToolError,
    WorkItems,
    agent_database_path,
    build_agent,
    flag,
)
from .quality import QualityError, build_quality, report_link_valid
from .delivery_reminders import build_reminders, filter_orders
from .dingtalk import (
    DailyReminderScheduler, DingTalkError, DingTalkStreamChannel, build_dingtalk,
    notify_pending_after_restart,
)
from .forecast import ForecastError, ForecastService, ForecastUnavailable
from .business_time import business_now, business_timestamp, business_today
from .ops_status import (
    build_schedules, next_daily, next_insole, next_interval, schedule_row,
    source_card,
)
from .staff_names import WEB_OPERATOR_UNBOUND, buyer_names_equivalent
from .agent.users import is_confirmed_admin_name
from .agent.web_auth import WebAuth, WebAuthError
from .product_images import ProductImageError, ProductImageService
from .order_source import OrderSourceError, fetch_exchange_order_items, fetch_exchange_orders
from .erp import DigitalRuntime, DigitalWorkerLoop, ErpKeepAlive, public_worker_status
from .dropship.scheduler import DailyDropshipScheduler
from .exchange.insole import warm_insole_lines_cache
from .exchange.insole_scheduler import DouyinInsoleScheduler
from .realtime_mirror import build_mirror_from_settings
from .spu_plan import (
    BOARDS,
    DataMissing as SpuDataMissing,
    build_style_alerts,
    load_style_snapshot,
    normalize_board,
    save_style_snapshot,
)
from .spu_plan.plan_source import MAX_UPLOAD_BYTES, PlanSourceError, PlanSourceUpdater
from .spu_plan.scheduler import DailySpuSnapshotScheduler
from .purchase_draft import create_purchase_draft, load_purchase_draft, public_draft
from .purchase_draft.service import PurchaseDraftError, apply_draft_edits, draft_xlsx_path
from .purchase_draft.submit import submit_purchase_draft
from .purchase_draft.workbook import write_blank_purchase_template
from .erp.errors import ErpError, ErpUnknownResult
from .gb_standards import (
    build_gb_sync_from_settings,
    expand_recommend_candidates,
    gb_recommend_prompt,
    mark_recommended_options,
    notify_contract_gb_changes,
    parse_recommended_nos,
    search_contract_standards,
    search_samr_catalog_hits,
)
from .logging_setup import configure_logging
from .paths import OUTPUTS_DIR, ROOT, resolve_repo_path


logger = logging.getLogger(__name__)
FRONTEND = ROOT / "frontend"
# React 单页应用的构建产物，由 `npm run build` 生成；没有构建过就只有接口可用。
DIST = FRONTEND / "dist"
INDEX_HTML = DIST / "index.html"
HOME = "/dashboard"
# 员工书签里还有旧的 .html 地址和更早那版中文路径，一律重定向到现在的前端路由。
LEGACY_PAGES = {
    "/采购看板.html": HOME,
    "/交期提醒台账.html": "/ledger",
    "/合同生成.html": "/contract",
    "/换货.html": "/exchange",
    "/对话.html": "/chat",
    "/工作台.html": "/workbench",
    "/看板": HOME,
    "/台账": "/ledger",
    "/合同": "/contract",
    "/换货": "/exchange",
    "/对话": "/chat",
    "/工作台": "/workbench",
}
# 换货核心给 Playwright 注入；油猴脚本已退役，路径仍提供以免旧书签 404。
STATIC_FILES = {
    "/js/exchange-worker.user.js": FRONTEND / "js" / "exchange-worker.user.js",
    "/js/jst-order-exchange.core.js": FRONTEND / "js" / "jst-order-exchange.core.js",
}
PROJECT_ENV_PATH = ROOT / ".env"
PROJECT_ENV = load_all_env(PROJECT_ENV_PATH) if PROJECT_ENV_PATH.exists() else {}


def setting(name, default=""):
    """读取进程环境变量，并回退到项目 .env 和默认值。"""
    return os.environ.get(name, PROJECT_ENV.get(name, default))


def _tokens_equal(left: str, right: str) -> bool:
    """定长 SHA-256 后再比较，避免长度不等时 compare_digest 抛 ValueError。"""
    digest = lambda value: hashlib.sha256(str(value or "").encode("utf-8")).digest()
    return secrets.compare_digest(digest(left), digest(right))


def shadowed_settings():
    """列出被进程环境变量盖掉的 .env 键；终端里残留一个 export 就够让服务连错库。"""
    return [
        name for name, value in PROJECT_ENV.items()
        if name in os.environ and os.environ[name] != value
    ]


REALTIME_ENV_PATH = str(ROOT / setting("REALTIME_DATABASE_ENV_FILE", "hanli.env"))
EXCHANGE_DATABASE_PATH = resolve_repo_path(
    setting("EXCHANGE_DATABASE_PATH", "files/data/exchange_jobs.sqlite3"),
)
EXCHANGE = ExchangeService(EXCHANGE_DATABASE_PATH)
PRODUCT_IMAGES = ProductImageService(
    resolve_repo_path(setting("PRODUCT_IMAGE_DATABASE_PATH", "files/data/product_image_jobs.sqlite3")),
    resolve_repo_path(setting("PRODUCT_IMAGE_CACHE_DIR", "files/data/product-images")),
)
DIGITAL_WORKER = DigitalWorkerLoop(
    DigitalRuntime.from_settings(setting, root=ROOT),
    EXCHANGE,
    images=PRODUCT_IMAGES,
    poll_seconds=float(setting("ERP_AI_POLL_SECONDS", "3") or 3),
)
ERP_KEEPALIVE = ErpKeepAlive(
    DIGITAL_WORKER.runtime,
    enabled=flag(setting("ERP_AI_KEEPALIVE_ENABLED", "true")),
    start_time=setting("ERP_AI_KEEPALIVE_START", "09:30"),
    end_time=setting("ERP_AI_KEEPALIVE_END", "18:30"),
    interval_seconds=int(setting("ERP_AI_KEEPALIVE_INTERVAL_SECONDS", "180") or 180),
)
DROPSHIP_SCHEDULER = DailyDropshipScheduler(
    runtime=DIGITAL_WORKER.runtime,
    root=ROOT,
    env_path=REALTIME_ENV_PATH,
    send_time=setting("DROPSHIP_SCHEDULE_TIME", "14:00"),
    prepare_lead_minutes=int(setting("DROPSHIP_PREPARE_LEAD_MINUTES", "0") or 0),
    enabled=flag(setting("DROPSHIP_SCHEDULE_ENABLED", "false")),
    conversation_id=setting("DROPSHIP_GROUP_CONVERSATION_ID", "")
    or setting("DINGTALK_GROUP_CONVERSATION_ID", ""),
    oto_buyers=setting("DROPSHIP_OTO_BUYERS", "安安"),
    oto_user_ids=setting("DROPSHIP_OTO_USER_IDS", ""),
)
INSOLE_SCHEDULER = DouyinInsoleScheduler(
    setting=setting,
    env_path=REALTIME_ENV_PATH,
    runtime=DIGITAL_WORKER.runtime,
    root=ROOT,
    enabled=flag(setting("INSOLE_SCHEDULE_ENABLED", "true"), default=True),
    start_time=setting("INSOLE_SCHEDULE_START", "09:30"),
    end_time=setting("INSOLE_SCHEDULE_END", "18:30"),
    interval_minutes=int(setting("INSOLE_SCHEDULE_INTERVAL_MINUTES", "60") or 60),
    shop=setting("INSOLE_SCHEDULE_SHOP", "抖音") or "抖音",
    conversation_id=setting("INSOLE_SCHEDULE_GROUP_CONVERSATION_ID", "")
    or setting("DINGTALK_GROUP_CONVERSATION_ID", ""),
    oto_buyers=setting("INSOLE_SCHEDULE_OTO_BUYERS", "安安"),
    oto_user_ids=setting("INSOLE_SCHEDULE_OTO_USER_IDS", ""),
)
REALTIME_MIRROR, REALTIME_MIRROR_SCHEDULER = build_mirror_from_settings(
    setting, root=ROOT, env_path=REALTIME_ENV_PATH,
)
GB_STANDARDS_SYNCER, GB_STANDARDS_SCHEDULER = build_gb_sync_from_settings(
    setting, env_path=REALTIME_ENV_PATH,
)
CACHE_TTL_SECONDS = 30
CACHE_STALE_SECONDS = 600
CACHE_KEEP_SECONDS = 25
CACHE_YEAR_LIMIT = 3
_cache = {}
_source_state = {"source": None, "warning": None, "year": None}
_cache_lock = threading.Lock()
_rebuilding = set()
# 鞋服 SPU 看板：读结果表即刻返回；重算约一分钟，锁保证同时只有一次。
SPU_REFRESH_LOCK = threading.Lock()
SPU_REFRESH_STATE = {"startedAt": "", "finishedAt": "", "lastError": ""}
SPU_SCHEDULER = DailySpuSnapshotScheduler(
    env_path=REALTIME_ENV_PATH,
    enabled=flag(setting("SPU_SNAPSHOT_ENABLED", "true"), default=True),
    run_time=setting("SPU_SNAPSHOT_TIME", "09:00"),
    lock=SPU_REFRESH_LOCK,
    plan_source=str(resolve_repo_path(
        setting("SPU_PLAN_SOURCE_XLSX", "files/config/重点产品订货表.xlsx"),
    )),
)
PLAN_UPDATER = PlanSourceUpdater(
    env_path=REALTIME_ENV_PATH, source_path=SPU_SCHEDULER.plan_source,
)
_year_locks = {}
_followup_cache = {}
_followup_lock = threading.Lock()


def _year_lock(year: str):
    with _cache_lock:
        return _year_locks.setdefault(year, threading.Lock())


class PageCacheKeeper:
    """看板/台账热缓存：到点自己重算，不等人打开页面。"""

    def __init__(self, *, interval_seconds: int = CACHE_KEEP_SECONDS, initial_delay: int = 20):
        self.interval = max(15, int(interval_seconds))
        self.initial_delay = max(0, int(initial_delay))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._hot = set()
        self.last_ok = ""
        self.last_error = ""

    def remember(self, year: str) -> None:
        text = str(year or "").strip()
        if text:
            self._hot.add(text)

    def status(self) -> dict:
        return {
            "enabled": True,
            "running": bool(self._thread and self._thread.is_alive()),
            "intervalSeconds": self.interval,
            "lastOk": self.last_ok,
            "lastError": self.last_error,
            "hotYears": sorted(self._hot),
        }

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="page-cache-keep", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)
        self._thread = None

    def _loop(self) -> None:
        if self.initial_delay and self._stop.wait(self.initial_delay):
            return
        while not self._stop.is_set():
            self.refresh()
            if self._stop.wait(self.interval):
                return

    def refresh(self) -> None:
        try:
            years = fetch_realtime_years(REALTIME_ENV_PATH)
            current = resolve_source_year(None, years)
            wanted = set(self._hot) | {current}
            now = time.monotonic()
            for year in sorted(wanted):
                self.remember(year)
                with _cache_lock:
                    cached = _cache.get(year)
                    still_hot = bool(cached and cached.get("expires", 0) > now + 5)
                if not still_hot:
                    _fill_source_cache(year, years)
            with _cache_lock:
                follow = _followup_cache.get("all")
                follow_hot = bool(follow and follow.get("expires", 0) > now + 5)
            if not follow_hot:
                _fill_followup_cache()
            self.last_ok = business_now().strftime("%Y-%m-%d %H:%M:%S")
            self.last_error = ""
        except Exception as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"
            logger.exception("看板热缓存刷新失败")


PAGE_CACHE_KEEPER = PageCacheKeeper()


def snapshot_years(rows):
    return sorted({str(row.get("采购日期") or "")[:4] for row in rows
                   if re.match(r"^\d{4}", str(row.get("采购日期") or ""))}, reverse=True)


def resolve_source_year(requested_year, years, current_year=None):
    """把请求年份收成镜像库里真实存在的一年；垃圾值回退到今年或最新年。"""
    if not years:
        raise RuntimeError("实时采购库没有有效年份")
    current_year = str(current_year if current_year is not None else business_today().year)
    requested = str(requested_year).strip() if requested_year not in (None, "") else ""
    if requested in years:
        return requested
    if current_year in years:
        return current_year
    return years[0]


def trim_source_cache(cache, now, *, keep_key=None, limit=CACHE_YEAR_LIMIT):
    """先丢掉过期且不能再拿来垫底的条目，再按过期时间从旧到新逐出。"""
    for key in [item for item, value in cache.items()
                if value.get("staleUntil", value.get("expires", 0)) <= now
                and item != keep_key]:
        cache.pop(key, None)
    while len(cache) > limit:
        candidates = [key for key in cache if key != keep_key]
        if not candidates:
            break
        oldest = min(candidates, key=lambda key: cache[key].get("expires", 0))
        cache.pop(oldest, None)


def source_rows(requested_year=None):
    """只读取聚水潭实时采购库，不再回退旧数据库。"""
    years = fetch_realtime_years(REALTIME_ENV_PATH)
    year = resolve_source_year(requested_year, years)
    rows = fetch_realtime_purchase_rows(year, REALTIME_ENV_PATH)
    return rows, "供应链 API 本地实时镜像", years, year, None


def source_cache(requested_year=None):
    """短时缓存原始明细行和看板数据。过期后先返回上一份，后台再刷新。"""
    years = fetch_realtime_years(REALTIME_ENV_PATH)
    year = resolve_source_year(requested_year, years)
    PAGE_CACHE_KEEPER.remember(year)
    now = time.monotonic()
    with _cache_lock:
        cached = _cache.get(year)
        if cached and cached["expires"] > now:
            return cached
        stale = bool(cached and cached.get("staleUntil", 0) > now)
    if stale:
        _schedule_source_rebuild(year, years)
        return cached
    with _year_lock(year):
        now = time.monotonic()
        with _cache_lock:
            cached = _cache.get(year)
            if cached and cached["expires"] > now:
                return cached
            if cached and cached.get("staleUntil", 0) > now:
                _schedule_source_rebuild(year, years)
                return cached
        return _fill_source_cache(year, years)


def _schedule_source_rebuild(year: str, years) -> None:
    with _cache_lock:
        if year in _rebuilding:
            return
        _rebuilding.add(year)

    def worker():
        try:
            with _year_lock(year):
                _fill_source_cache(year, years)
        except Exception:
            logger.exception("后台刷新看板缓存失败：%s", year)
        finally:
            with _cache_lock:
                _rebuilding.discard(year)

    threading.Thread(target=worker, name=f"source-cache-{year}", daemon=True).start()


def _fill_source_cache(year: str, years):
    rows = fetch_realtime_purchase_rows(year, REALTIME_ENV_PATH)
    source = "供应链 API 本地实时镜像"
    sync_state = fetch_realtime_sync_state(REALTIME_ENV_PATH)
    warning = _mirror_warning(sync_state)
    window_warning = purchase_window_warning(year)
    if window_warning:
        warning = f"{warning}；{window_warning}" if warning else window_warning
    dashboard = build_dashboard_payload(rows, source)
    dashboard["meta"].update(
        warning=warning, availableYears=years, selectedYear=year,
        databaseNow=sync_state["databaseNow"], syncedAt=sync_state["syncedAt"],
        syncLagMinutes=sync_state["syncLagMinutes"], fresh=sync_state["fresh"],
        sourceStatus=sync_state.get("sourceStatus", ""),
        timezone="Asia/Shanghai",
    )
    today = business_today().isoformat()
    dashboard["meta"]["today"] = today
    built = time.monotonic()
    with _cache_lock:
        _cache[year] = {
            # 年度明细从远程镜像读取可能耗时较长，TTL 应从构建完成时开始计算。
            "expires": built + CACHE_TTL_SECONDS,
            "staleUntil": built + CACHE_TTL_SECONDS + CACHE_STALE_SECONDS,
            "rows": rows,
            "meta": {"source": source, "year": year, "availableYears": years,
                     "warning": warning, "today": today, "rows": len(rows)},
            "dashboard": dashboard,
        }
        trim_source_cache(_cache, built, keep_key=year)
        _source_state.update(source=source, warning=warning, year=year)
        return _cache[year]


def cached_source_card():
    """健康页用的数据源卡：只读已有看板缓存，不重算全年明细。"""
    sync = {}
    try:
        sync = fetch_realtime_sync_state(REALTIME_ENV_PATH)
    except Exception:
        sync = {}
    meta = {}
    with _cache_lock:
        year = str(_source_state.get("year") or "")
        cached = _cache.get(year) if year else None
        if cached is None and _cache:
            cached = next(iter(_cache.values()))
        if cached:
            meta = dict((cached.get("dashboard") or {}).get("meta") or cached.get("meta") or {})
    return source_card(meta, sync, _source_state)


def health_schedules():
    """把各调度的 lastRun / 下次执行收成一张表。"""
    now = business_now()
    today = now.date().isoformat()
    mirror = _safe_status(REALTIME_MIRROR_SCHEDULER.status)
    sync = {}
    try:
        sync = fetch_realtime_sync_state(REALTIME_ENV_PATH)
    except Exception:
        sync = {}
    reminder = _safe_status(REMINDER_SCHEDULER.status)
    quality = _safe_status(QUALITY_SCHEDULER.status)
    dropship = _safe_status(DROPSHIP_SCHEDULER.status)
    insole = _safe_status(INSOLE_SCHEDULER.status)
    gb = _safe_status(GB_STANDARDS_SCHEDULER.status)
    spu = _safe_status(SPU_SCHEDULER.status)
    keep = _safe_status(ERP_KEEPALIVE.status)
    jobs = _safe_status(JOB_WORKER.status)
    stream = _safe_status(DINGTALK_STREAM.status)

    rem_next, rem_done = next_daily(
        now, reminder.get("sendTime") or "08:30", last_run=reminder.get("lastRun") or "",
    )
    qty_next, qty_done = next_daily(
        now, quality.get("sendTime") or "17:30", last_run=quality.get("lastRun") or "",
    )
    drop_next, drop_done = next_daily(
        now, dropship.get("prepareTime") or dropship.get("sendTime") or "14:00",
        last_run=dropship.get("lastRun") or "",
    )
    gb_next, gb_done = next_daily(
        now, gb.get("sendTime") or "02:30", last_run=gb.get("lastRun") or "",
    )
    spu_next, spu_done = next_daily(
        now, spu.get("runTime") or "09:00", last_run=spu.get("lastRun") or "",
    )
    plan_next, plan_done = next_daily(
        now, spu.get("runTime") or "09:00", last_run=spu.get("planLastRun") or "",
    )
    insole_next, insole_done = next_insole(
        now,
        start=insole.get("startTime") or "09:30",
        end=insole.get("endTime") or "18:30",
        interval_minutes=int(insole.get("intervalMinutes") or 60),
        last_slot=insole.get("lastSlot") or "",
    )
    keep_next = next_interval(
        now, keep.get("lastOk") or "", int(keep.get("intervalSeconds") or 180),
    )
    cache = _safe_status(PAGE_CACHE_KEEPER.status)
    cache_next = next_interval(
        now, cache.get("lastOk") or "", int(cache.get("intervalSeconds") or CACHE_KEEP_SECONDS),
    )
    rows = [
        schedule_row(
            item_id="mirror", label="镜像增量", group="数据",
            enabled=bool(mirror.get("enabled")),
            running=bool(mirror.get("enabled")),
            last_run=str(sync.get("syncedAt") or ""),
            next_at=next_interval(now, sync.get("syncedAt") or "", 60),
            ran_today=bool(str(sync.get("syncedAt") or "").startswith(today)),
            last_error=str(mirror.get("lastError") or ""),
            detail=str(sync.get("sourceStatus") or ""),
            now=now,
        ),
        schedule_row(
            item_id="pageCache", label="看板热缓存", group="数据",
            enabled=bool(cache.get("enabled")), running=bool(cache.get("running")),
            last_run=str(cache.get("lastOk") or ""), next_at=cache_next,
            ran_today=bool(str(cache.get("lastOk") or "").startswith(today)),
            last_error=str(cache.get("lastError") or ""),
            detail=f"每 {cache.get('intervalSeconds') or CACHE_KEEP_SECONDS} 秒续热"
            + (f"（{('、'.join(cache.get('hotYears') or []) or '本年')}）"),
            now=now,
        ),
        schedule_row(
            item_id="gb", label="国标目录同步", group="数据",
            enabled=bool(gb.get("enabled")), running=bool(gb.get("running")),
            last_run=str(gb.get("lastRun") or ""), next_at=gb_next,
            ran_today=gb_done, last_error=str(gb.get("lastError") or ""),
            detail=f"每天 {gb.get('sendTime') or '02:30'}", now=now,
        ),
        schedule_row(
            item_id="reminder", label="交期催办", group="通知",
            enabled=bool(reminder.get("enabled")), running=bool(reminder.get("running")),
            last_run=str(reminder.get("lastRun") or ""), next_at=rem_next,
            ran_today=rem_done, last_error=str(reminder.get("lastError") or ""),
            detail=f"每天 {reminder.get('sendTime') or '08:30'}", now=now,
        ),
        schedule_row(
            item_id="quality", label="品控日报", group="通知",
            enabled=bool(quality.get("enabled")), running=bool(quality.get("running")),
            last_run=str(quality.get("lastRun") or ""), next_at=qty_next,
            ran_today=qty_done, last_error=str(quality.get("lastError") or ""),
            detail=f"每天 {quality.get('sendTime') or '17:30'}", now=now,
        ),
        schedule_row(
            item_id="dropship", label="代发抓取", group="业务",
            enabled=bool(dropship.get("enabled")), running=bool(dropship.get("running")),
            last_run=str(dropship.get("lastRun") or ""), next_at=drop_next,
            ran_today=drop_done, last_error=str(dropship.get("lastError") or ""),
            detail=f"每天 {dropship.get('prepareTime') or '14:00'}", now=now,
        ),
        schedule_row(
            item_id="insole", label="抖音换鞋垫", group="业务",
            enabled=bool(insole.get("enabled")), running=bool(insole.get("running")),
            last_run=str(insole.get("lastSlot") or insole.get("lastRun") or ""),
            next_at=insole_next, ran_today=insole_done,
            last_error=str(insole.get("lastError") or ""),
            detail=f"{insole.get('startTime') or '09:30'}–{insole.get('endTime') or '18:30'} 每 {insole.get('intervalMinutes') or 60} 分钟",
            now=now,
        ),
        schedule_row(
            item_id="spu", label="鞋服/百货结果表", group="业务",
            enabled=bool(spu.get("enabled")), running=bool(spu.get("running")),
            last_run=str(spu.get("lastRun") or ""), next_at=spu_next,
            ran_today=spu_done, last_error=str(spu.get("lastError") or ""),
            detail=f"每天 {spu.get('runTime') or '09:00'}", now=now,
        ),
        schedule_row(
            item_id="plan", label="生产计划刷新", group="业务",
            enabled=bool(spu.get("enabled")), running=bool(spu.get("running")),
            last_run=str(spu.get("planLastRun") or ""), next_at=plan_next,
            ran_today=plan_done, last_error=str(spu.get("planLastError") or ""),
            detail=f"每天 {spu.get('runTime') or '09:00'} 随结果表", now=now,
        ),
        schedule_row(
            item_id="keepalive", label="ERP 登录态", group="通道",
            enabled=bool(keep.get("enabled")), running=bool(keep.get("running")),
            last_run=str(keep.get("lastOk") or ""), next_at=keep_next,
            ran_today=bool(str(keep.get("lastOk") or "").startswith(today)),
            last_error=str(keep.get("lastError") or ""),
            detail=f"每 {keep.get('intervalSeconds') or 180} 秒", now=now,
        ),
        schedule_row(
            item_id="jobs", label="任务队列", group="通道",
            enabled=bool(jobs.get("enabled")), running=bool(jobs.get("running")),
            last_run="", next_at=None, ran_today=None,
            last_error=str(jobs.get("lastError") or ""),
            detail=f"排队 {jobs.get('queued') or 0}", now=now,
        ),
        schedule_row(
            item_id="stream", label="钉钉 Stream", group="通道",
            enabled=bool(stream.get("enabled")), running=bool(stream.get("running")),
            last_run="", next_at=None, ran_today=None,
            last_error=str(stream.get("lastError") or ""),
            detail="长连" if stream.get("running") else "未连接", now=now,
        ),
    ]
    return build_schedules(now, rows)


def _mirror_warning(sync_state):
    warning = None
    if not sync_state["fresh"]:
        warning = (
            f"数据库镜像最后同步于 {sync_state['syncedAt'] or '未知时间'}"
            + (f"，当前延迟约 {sync_state['syncLagMinutes']} 分钟" if sync_state["syncLagMinutes"] is not None else "")
        )
    if "purchase:failed" in sync_state.get("sourceStatus", ""):
        failure_warning = "API 增量同步当前失败，请检查 Client 路由授权"
        warning = f"{warning}；{failure_warning}" if warning else failure_warning
    return warning


def followup_delivery_cache():
    """交期台账：全库跟单池，不按年度截断。过期先返回上一份。"""
    now = time.monotonic()
    with _cache_lock:
        cached = _followup_cache.get("all")
        if cached and cached["expires"] > now:
            return cached
        stale = bool(cached and cached.get("staleUntil", 0) > now)
    if stale:
        _schedule_followup_rebuild()
        return cached
    with _followup_lock:
        now = time.monotonic()
        with _cache_lock:
            cached = _followup_cache.get("all")
            if cached and cached["expires"] > now:
                return cached
            if cached and cached.get("staleUntil", 0) > now:
                _schedule_followup_rebuild()
                return cached
        return _fill_followup_cache()


def _schedule_followup_rebuild() -> None:
    with _cache_lock:
        if "followup" in _rebuilding:
            return
        _rebuilding.add("followup")

    def worker():
        try:
            with _followup_lock:
                _fill_followup_cache()
        except Exception:
            logger.exception("后台刷新交期缓存失败")
        finally:
            with _cache_lock:
                _rebuilding.discard("followup")

    threading.Thread(target=worker, name="followup-cache", daemon=True).start()


def _fill_followup_cache():
    years = fetch_realtime_years(REALTIME_ENV_PATH)
    rows = fetch_followup_purchase_rows(REALTIME_ENV_PATH)
    source = "供应链 API 本地实时镜像 · 跟单池"
    sync_state = fetch_realtime_sync_state(REALTIME_ENV_PATH)
    warning = _mirror_warning(sync_state)
    delivery = build_delivery_payload(rows, source)
    delivery["meta"].update(
        warning=warning, availableYears=years,
        selectedYear=years[0] if years else "",
        databaseNow=sync_state["databaseNow"], syncedAt=sync_state["syncedAt"],
        syncLagMinutes=sync_state["syncLagMinutes"], fresh=sync_state["fresh"],
        sourceStatus=sync_state.get("sourceStatus", ""),
        timezone="Asia/Shanghai", pool="followup",
    )
    today = business_today().isoformat()
    delivery["meta"]["today"] = today
    built = time.monotonic()
    cached = {
        "expires": built + CACHE_TTL_SECONDS,
        "staleUntil": built + CACHE_TTL_SECONDS + CACHE_STALE_SECONDS,
        "delivery": delivery,
        "rows": rows,
    }
    with _cache_lock:
        _followup_cache["all"] = cached
    return cached


def payloads(requested_year=None):
    cached = source_cache(requested_year)
    return cached["dashboard"], followup_delivery_cache()["delivery"]


def agent_rows(requested_year=None):
    """Agent 工具的数据入口：与两个页面共用同一份缓存和同一套查询。"""
    year = str(requested_year or "").strip()
    cached = source_cache(year if re.fullmatch(r"\d{4}", year) else None)
    return cached["rows"], cached["meta"]


def followup_rows(requested_year=None):
    """跟单催办：全库已确认未完结，与交期台账共用跟单缓存。"""
    del requested_year
    cached = followup_delivery_cache()
    return cached["rows"], {"source": "followup"}


FORECAST = ForecastService.from_settings(setting, root=ROOT, env_path=REALTIME_ENV_PATH)
AGENT_STORE = AgentStore(agent_database_path(setting, ROOT))
WEB_AUTH = WebAuth(AGENT_STORE)
AUDIT = AuditLog(AGENT_STORE)
WEB_BIND_HINT = "请先到钉钉群里发「绑定网页」，用私信里的花名和密码登录"
DINGTALK_SENDER, STAFF_DIRECTORY, REMINDER_NOTIFIER = build_dingtalk(
    setting=setting, store=AGENT_STORE, audit=AUDIT, flag=flag, root=ROOT,
)
DROPSHIP_SCHEDULER.sender = DINGTALK_SENDER
DROPSHIP_SCHEDULER.audit = AUDIT
DROPSHIP_SCHEDULER.directory = STAFF_DIRECTORY
if not DROPSHIP_SCHEDULER.conversation_id:
    DROPSHIP_SCHEDULER.conversation_id = str(
        getattr(DINGTALK_SENDER, "group_conversation_id", "") or ""
    )
QUALITY, QUALITY_SCHEDULER = build_quality(
    setting=setting, store=AGENT_STORE, sender=DINGTALK_SENDER, root=ROOT,
    env_path=REALTIME_ENV_PATH, audit=AUDIT, flag=flag,
)
MEMORIES = OperatorMemories(
    AGENT_STORE, enabled=flag(setting("AGENT_MEMORY_ENABLED", "false")),
)
WORK_ITEMS = WorkItems(AGENT_STORE)
JOBS = JobQueue(AGENT_STORE)
OUTBOX = Outbox(
    AGENT_STORE,
    sender=DINGTALK_SENDER if DINGTALK_SENDER.configured else None,
)
REMINDER_NOTIFIER.outbox = OUTBOX
JOB_WORKER = JobWorker(
    JOBS,
    outbox=OUTBOX,
    handlers={
        "outbox_flush": lambda payload: {
            "delivered": len(OUTBOX.deliver_due(limit=int((payload or {}).get("limit") or 20))),
        },
    },
)
AGENT = build_agent(
    setting=setting, root=ROOT, env_path=REALTIME_ENV_PATH, fetch_rows=agent_rows,
    fetch_followup=followup_rows,
    exchange=EXCHANGE, erp=DIGITAL_WORKER.runtime, forecast=FORECAST,
    notifier=REMINDER_NOTIFIER if REMINDER_NOTIFIER.enabled else None,
    store=AGENT_STORE, audit=AUDIT, directory=STAFF_DIRECTORY,
    quality=QUALITY if QUALITY.enabled else None, memories=MEMORIES,
    mirror=REALTIME_MIRROR,
)
JOB_WORKER.expire = AGENT.actions.expire_due
JOB_WORKER.handlers["pending_expire"] = lambda payload: {"expired": AGENT.actions.expire_due()}
REMINDER_SCHEDULER = DailyReminderScheduler(
    notifier=REMINDER_NOTIFIER, fetch_rows=agent_rows, fetch_followup=followup_rows,
    send_time=setting("DINGTALK_REMINDER_TIME", "08:30"),
    limit=int(setting("DINGTALK_REMINDER_LIMIT", "200") or 200),
    profile="followup",
)
DINGTALK_STREAM = DingTalkStreamChannel(
    runner=AGENT, sender=DINGTALK_SENDER,
    client_id=setting("DINGTALK_CLIENT_ID", ""),
    client_secret=setting("DINGTALK_CLIENT_SECRET", ""),
    audit=AUDIT, enabled=flag(setting("DINGTALK_ENABLED", "false")),
    directory=STAFF_DIRECTORY, quality=QUALITY, memories=MEMORIES,
    insole_scheduler=INSOLE_SCHEDULER,
    admin_user_ids=[
        item.strip() for item in setting("DINGTALK_ADMIN_USER_IDS", "").split(",")
        if item.strip()
    ],
)
DINGTALK_STREAM.plan_updater = PLAN_UPDATER
SPU_SCHEDULER.sender = DINGTALK_SENDER if DINGTALK_SENDER.configured else None
SPU_SCHEDULER.audit = AUDIT
SPU_SCHEDULER.alert_enabled = flag(setting("SPU_PLAN_ALERT_ENABLED", "true"), default=True)
PLAN_UPDATER.sender = SPU_SCHEDULER.sender
PLAN_UPDATER.audit = AUDIT
PLAN_UPDATER.alert_enabled = SPU_SCHEDULER.alert_enabled
INSOLE_SCHEDULER.sender = DINGTALK_SENDER
INSOLE_SCHEDULER.audit = AUDIT
INSOLE_SCHEDULER.directory = STAFF_DIRECTORY
INSOLE_SCHEDULER.mirror = REALTIME_MIRROR
AGENT.insole_scheduler = INSOLE_SCHEDULER
if not INSOLE_SCHEDULER.conversation_id:
    INSOLE_SCHEDULER.conversation_id = str(
        getattr(DINGTALK_SENDER, "group_conversation_id", "") or ""
    )
MAINTENANCE = MaintenanceScheduler(
    store=AGENT_STORE, root=ROOT,
    retention_days=int(setting("AGENT_RETENTION_DAYS", "90") or 90),
)


def _principal_is_admin(principal: dict) -> bool:
    bound = STAFF_DIRECTORY.find_binding(
        operator=principal.get("operator") or "",
        actor_id=principal.get("actorId") or "",
    )
    if bound.get("role") == "admin":
        return True
    return is_confirmed_admin_name(bound.get("buyerName") or principal.get("operator") or "")


def _owns_record(principal: dict, *, operator="", user_id="", actor_id="", admin=False) -> bool:
    if admin:
        return True
    if principal.get("userId") and user_id and principal["userId"] == user_id:
        return True
    if principal.get("actorId") and actor_id and principal["actorId"] == actor_id:
        return True
    return buyer_names_equivalent(principal.get("operator") or "", operator, include_nick=True)


def _notify_gb_contract_changes(sync_result: dict) -> dict:
    """每日国标同步成功后：合同已选用标准的状态跃迁推钉钉群。"""
    return notify_contract_gb_changes(
        REALTIME_ENV_PATH,
        sync_result.get("statusChanges") or [],
        sender=DINGTALK_SENDER if DINGTALK_SENDER.configured else None,
        audit=AUDIT,
    )


if GB_STANDARDS_SYNCER is not None:
    GB_STANDARDS_SYNCER.on_sync = _notify_gb_contract_changes


def _notify_stuck_exchange(job: dict) -> None:
    """执行超时的换货任务不重投，只告警，避免 ERP 双写。"""
    job_id = str(job.get("id") or "")
    operator = str(job.get("operator") or "")
    oids = (job.get("targets") or {}).get("o_ids") or []
    shown = ", ".join(str(item) for item in oids[:10])
    suffix = "…" if len(oids) > 10 else ""
    text = (
        f"换货任务 `{job_id}` 执行超时，已标为中断（stuck），**未自动重投**，"
        f"避免对 ERP 重复写入。\n\n"
        f"- 操作人：{operator or '未知'}\n"
        f"- 订单：{shown}{suffix or ('（无）' if not oids else '')}\n\n"
        "请人工核对 ERP 后再决定是否另建任务。"
    )
    logger.warning("Exchange job stuck: %s", job_id)
    if not flag(setting("DINGTALK_ENABLED", "false")) or not DINGTALK_SENDER.configured:
        return
    try:
        DINGTALK_SENDER.send_markdown("换货任务执行中断", text)
    except Exception as exc:
        logger.error("Stuck exchange alert failed: %s", exc)


EXCHANGE.on_stuck = _notify_stuck_exchange


def _valid_po_id(value) -> bool:
    text = str(value or "").strip()
    return bool(text) and text.isascii() and text.isdecimal()


def _reject_json_constant(value):
    raise ValueError(f"JSON 不能包含 {value}")


def _safe_status(getter):
    try:
        payload = getter()
    except Exception as exc:
        return {"error": type(exc).__name__}
    if isinstance(payload, dict) and payload.get("lastError"):
        payload = {**payload, "lastError": str(payload["lastError"]).split(":")[0][:80]}
    return payload


class Handler(BaseHTTPRequestHandler):
    server_version = "ProcurementDashboard/1.0"
    timeout = 30

    def do_GET(self):
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        if path == "/" or path in LEGACY_PAGES:
            return self.redirect(LEGACY_PAGES.get(path, HOME))
        if path == "/api/health":
            return self.health()
        if self._is_panel_api(path) and not self.require_web_login():
            return
        if path == "/api/now":
            now = business_now()
            return self.json_response({
                "ok": True,
                "now": now.strftime("%Y-%m-%d %H:%M:%S"),
                "today": now.date().isoformat(),
                "tz": "Asia/Shanghai",
            })
        quality_file = re.fullmatch(r"/api/quality/reports/(\d{8})/([a-f0-9]{16})\.xlsx", path)
        if quality_file:
            return self.quality_report_file(quality_file.group(1), quality_file.group(2))
        if path.startswith("/api/exchange/"):
            return self.exchange_get(path, parsed)
        if path == "/api/contracts/options":
            po_id = parse_qs(parsed.query).get("po_id", [""])[0]
            return self.contract_options(po_id)
        if path == "/api/contracts/orders":
            query = parse_qs(parsed.query).get("q", [""])[0]
            return self.contract_orders(query)
        if path == "/api/contracts/gb/search":
            query = parse_qs(parsed.query).get("q", [""])[0]
            name = parse_qs(parsed.query).get("name", [""])[0]
            category = parse_qs(parsed.query).get("category", [""])[0]
            return self.contract_gb_search(query, name=name, category=category)
        image_job = re.fullmatch(r"/api/contracts/images/jobs/([a-f0-9]{24})", path)
        if image_job:
            return self.json_response(PRODUCT_IMAGES.get(image_job.group(1)))
        if path == "/api/agent/contracts/orders":
            if not self.require_agent_token():
                return
            query = parse_qs(parsed.query).get("q", [""])[0]
            return self.contract_orders(query)
        agent_file = re.fullmatch(r"/api/agent/contracts/([a-f0-9]{24})/(file|preview)", path)
        if agent_file:
            if not self.require_agent_token():
                return
            return self.agent_contract_file(agent_file.group(1), agent_file.group(2))
        if path.startswith("/api/agent/") or path.startswith("/api/forecast/"):
            return self.agent_get(path, parsed)
        if path == "/api/spu/summary":
            return self.spu_summary(parsed)
        if path == "/api/spu/analyze":
            return self.spu_analyze_get(parsed)
        if path == "/api/purchase-drafts/template":
            return self.purchase_draft_template()
        draft_file = re.fullmatch(r"/api/purchase-drafts/([a-f0-9]{24})/file", path)
        if draft_file:
            return self.purchase_draft_file(draft_file.group(1))
        draft_get = re.fullmatch(r"/api/purchase-drafts/([a-f0-9]{24})", path)
        if draft_get:
            return self.purchase_draft_get(draft_get.group(1))
        if path in ("/api/dashboard", "/api/delivery"):
            year = parse_qs(parsed.query).get("year", [None])[0]
            year = year if year and re.fullmatch(r"\d{4}", year) else None
            return self.api(path, year)
        if path.startswith("/api/"):
            return self.send_error(404, "Not Found")
        if path in STATIC_FILES:
            return self.static(STATIC_FILES[path])
        return self.spa(path)

    def do_POST(self):
        path = unquote(urlparse(self.path).path)
        if path.startswith("/api/exchange/"):
            return self.exchange_post(path)
        if self._is_panel_api(path) and not self.require_web_login():
            return
        if path == "/api/contracts/images/sync":
            return self.contract_image_sync()
        if path == "/api/agent/contracts/generate":
            if not self.require_agent_token():
                return
            return self.agent_contract_generate()
        if path.startswith("/api/agent/") or path.startswith("/api/forecast/"):
            return self.agent_post(path)
        if path == "/api/contracts/gb/recommend":
            return self.contract_gb_recommend()
        if path == "/api/spu/refresh":
            return self.spu_refresh()
        if path == "/api/spu/analyze":
            return self.spu_analyze()
        if path == "/api/spu/plan-source":
            return self.spu_plan_upload()
        if path == "/api/purchase-drafts":
            return self.purchase_draft_create()
        draft_confirm = re.fullmatch(r"/api/purchase-drafts/([a-f0-9]{24})/confirm", path)
        if draft_confirm:
            return self.purchase_draft_confirm(draft_confirm.group(1))
        draft_save = re.fullmatch(r"/api/purchase-drafts/([a-f0-9]{24})", path)
        if draft_save:
            return self.purchase_draft_save(draft_save.group(1))
        if path not in ("/api/contracts/generate", "/api/contracts/preview"):
            self.send_error(404, "Not Found")
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > 1024 * 1024:
                raise ValueError("请求内容大小不正确")
            body = json.loads(
                self.rfile.read(length).decode("utf-8"),
                parse_constant=_reject_json_constant,
            )
            po_id = str(body.get("poId") or "")
            if not _valid_po_id(po_id):
                raise ValueError("采购单号格式不正确")
            invoice_type = str(body.get("invoiceType") or "")
            from .contracts import INVOICE_LABELS
            if invoice_type not in INVOICE_LABELS:
                raise ValueError("票种只能是 no_invoice、normal_invoice 或 special_invoice")
            stamp = time.time_ns()
            output = OUTPUTS_DIR / "generated" / f"采购合同-{po_id}-{invoice_type}-{stamp}.xlsx"
            preview = output.with_suffix(".png") if path.endswith("preview") else None
            generate_contract(
                po_id, invoice_type, output,
                tax_rate=body.get("taxRate"),
                price_overrides=body.get("priceOverrides") or {},
                gb_overrides=body.get("gbOverrides") or body.get("gb_overrides") or {},
                payment_option=body.get("paymentOption"),
                payment_text=body.get("paymentText"),
                receiving_info=body.get("receivingInfo"),
                inspection_extra=body.get("inspectionExtra"),
                preview_path=preview,
                env_path=REALTIME_ENV_PATH,
            )
            if preview:
                self.file_response(preview, "image/png")
            else:
                self.xlsx_response(output, f"采购合同-{po_id}.xlsx")
        except (ValueError, RuntimeError, subprocess.SubprocessError) as exc:
            self.json_response({"ok": False, "error": str(exc)}, 400)
        except Exception as exc:
            logger.exception("Contract generation error")
            self.json_response({"ok": False, "error": "采购合同生成失败"}, 500)

    def read_json_body(self, max_size=1024 * 1024):
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0 or length > max_size:
            raise ValueError("请求内容大小不正确")
        return json.loads(
            self.rfile.read(length).decode("utf-8"),
            parse_constant=_reject_json_constant,
        )

    def _is_panel_api(self, path):
        if path in ("/api/dashboard", "/api/delivery", "/api/now"):
            return True
        return path.startswith(("/api/contracts/", "/api/spu/", "/api/purchase-drafts"))

    def require_web_login(self):
        token = self.agent_web_token()
        if token and WEB_AUTH.get_session(token):
            return True
        self.json_response({"ok": False, "error": WEB_BIND_HINT}, 401)
        return False

    def require_agent_token(self, *, allow_web_session=False):
        expected = setting("AGENT_API_TOKEN", "").strip()
        supplied = self.headers.get("Authorization", "")
        if supplied.startswith("Bearer "):
            supplied = supplied[7:]
        supplied = supplied.strip()
        if expected and _tokens_equal(supplied, expected):
            return True
        if allow_web_session:
            web_token = self.agent_web_token()
            if web_token and WEB_AUTH.get_session(web_token):
                return True
        if not expected:
            self.json_response({"ok": False, "error": "Agent 接口尚未配置 AGENT_API_TOKEN"}, 503)
            return False
        if allow_web_session:
            self.json_response({
                "ok": False,
                "error": "请先用钉钉「绑定网页」拿到的花名和密码登录，或填写正确的 AGENT_API_TOKEN。",
            }, 401)
            return False
        self.json_response({"ok": False, "error": "Agent 接口认证失败"}, 401)
        return False

    def require_exchange_token(self, *, worker=False):
        name = "EXCHANGE_WORKER_TOKEN" if worker else "EXCHANGE_API_TOKEN"
        expected = setting(name, "").strip()
        other = setting("EXCHANGE_API_TOKEN" if worker else "EXCHANGE_WORKER_TOKEN", "").strip()
        if not expected:
            self.json_response({"ok": False, "error": f"换货接口尚未配置 {name}"}, 503)
            return False
        if other and _tokens_equal(expected, other):
            self.json_response({"ok": False, "error": "换货页面与 ERP Worker 必须使用不同 Token"}, 503)
            return False
        supplied = self.headers.get("Authorization", "")
        if supplied.startswith("Bearer "):
            supplied = supplied[7:]
        if not _tokens_equal(supplied, expected):
            self.json_response({"ok": False, "error": "换货接口认证失败"}, 401)
            return False
        return True

    def contract_image_sync(self):
        try:
            body = self.read_json_body(max_size=64 * 1024)
            po_id = str(body.get("poId") or "")
            _order, items = fetch_contract_order(po_id, REALTIME_ENV_PATH)
            return self.json_response(PRODUCT_IMAGES.create(po_id, items), 201)
        except ProductImageError as exc:
            self.json_response({"ok": False, "error": str(exc)}, exc.status)
        except (ValueError, json.JSONDecodeError) as exc:
            self.json_response({"ok": False, "error": str(exc)}, 400)

    def exchange_get(self, path, parsed):
        worker = path.startswith("/api/exchange/worker/")
        if not self.require_exchange_token(worker=worker):
            return
        try:
            query = parse_qs(parsed.query)
            if path == "/api/exchange/worker/images/next":
                return self.json_response({"job": None, "executor": "backend"})
            if path == "/api/exchange/products":
                products = fetch_exchange_products(
                    REALTIME_ENV_PATH,
                    query=query.get("q", [""])[0],
                    limit=query.get("limit", ["100"])[0],
                )
                return self.json_response({"products": products})
            if path == "/api/exchange/orders":
                return self.json_response(fetch_exchange_orders(
                    setting,
                    REALTIME_ENV_PATH,
                    query=query.get("q", [""])[0],
                    source_sku=query.get("source_sku", [""])[0],
                    shop=query.get("shop", [""])[0],
                    status=query.get("status", [None])[0],
                    date_from=query.get("date_from", [""])[0],
                    date_to=query.get("date_to", [""])[0],
                    limit=query.get("limit", ["50"])[0],
                ))
            if path == "/api/exchange/order-items":
                raw_oids = query.get("o_id", [])
                if len(raw_oids) == 1 and "," in raw_oids[0]:
                    raw_oids = [part for part in raw_oids[0].split(",") if part]
                return self.json_response(fetch_exchange_order_items(
                    setting, REALTIME_ENV_PATH, o_ids=raw_oids,
                ))
            if path == "/api/exchange/policy":
                return self.json_response(exchange_policy())
            if path == "/api/exchange/status":
                return self.json_response(EXCHANGE.status())
            if path == "/api/exchange/jobs":
                return self.json_response({"jobs": EXCHANGE.list_jobs(
                    limit=query.get("limit", ["100"])[0]
                )})
            match = re.fullmatch(r"/api/exchange/jobs/([a-f0-9]{24})", path)
            if match:
                return self.json_response(EXCHANGE.get_job(match.group(1)))
            if path == "/api/exchange/worker/jobs/next":
                return self.json_response({"job": None, "executor": "backend"})
            if path == "/api/exchange/worker/searches/next":
                return self.json_response({"search": None, "executor": "backend"})
            if path == "/api/exchange/worker/probes/next":
                return self.json_response({"probe": None, "executor": "backend"})
            search_match = re.fullmatch(r"/api/exchange/searches/([a-f0-9]{24})", path)
            if search_match:
                return self.json_response(EXCHANGE.get_search(search_match.group(1)))
            probe_match = re.fullmatch(r"/api/exchange/probes/([a-f0-9]{24})", path)
            if probe_match:
                return self.json_response(EXCHANGE.get_probe(probe_match.group(1)))
            self.send_error(404, "Not Found")
        except (ExchangeError, ProductImageError, OrderSourceError) as exc:
            self.json_response({"ok": False, "error": str(exc)}, getattr(exc, "status", 400))
        except Exception as exc:
            logger.exception("Exchange GET error")
            self.json_response({"ok": False, "error": "换货接口暂时不可用"}, 500)

    def exchange_post(self, path):
        worker = path.startswith("/api/exchange/worker/")
        if not self.require_exchange_token(worker=worker):
            return
        try:
            body = self.read_json_body(max_size=16 * 1024 * 1024)
            operator = str(self.headers.get("X-Exchange-Operator") or body.get("operator") or "web")
            if path == "/api/exchange/jobs":
                job = EXCHANGE.create_job(
                    body,
                    operator=operator,
                    idempotency_key=self.headers.get("Idempotency-Key"),
                )
                return self.json_response(job, 201)
            if path == "/api/exchange/searches":
                return self.json_response(EXCHANGE.create_search(str(body.get("sku") or "")), 201)
            if path == "/api/exchange/probes":
                return self.json_response(EXCHANGE.create_probe(str(body.get("kind") or ""), str(body.get("reference") or "")), 201)
            match = re.fullmatch(r"/api/exchange/jobs/([a-f0-9]{24})/(confirm|cancel)", path)
            if match:
                job = (EXCHANGE.confirm(match.group(1), operator) if match.group(2) == "confirm"
                       else EXCHANGE.cancel(match.group(1), operator))
                return self.json_response(job)
            if path == "/api/exchange/worker/heartbeat":
                return self.json_response(EXCHANGE.heartbeat(str(body.get("workerId") or ""), body))
            search_match = re.fullmatch(r"/api/exchange/worker/searches/([a-f0-9]{24})/result", path)
            if search_match:
                return self.json_response(EXCHANGE.report_search(
                    search_match.group(1), str(body.get("workerId") or ""), body.get("result") or {}
                ))
            probe_match = re.fullmatch(r"/api/exchange/worker/probes/([a-f0-9]{24})/result", path)
            if probe_match:
                return self.json_response(EXCHANGE.report_probe(
                    probe_match.group(1), str(body.get("workerId") or ""), body.get("result") or {}
                ))
            match = re.fullmatch(
                r"/api/exchange/worker/images/([a-f0-9]{24})/(upload|result)", path
            )
            if match:
                image_job_id, image_action = match.groups()
                worker_id = str(body.get("workerId") or "")
                if image_action == "upload":
                    return self.json_response(PRODUCT_IMAGES.upload(image_job_id, worker_id, body))
                return self.json_response(PRODUCT_IMAGES.finish(
                    image_job_id, worker_id, body.get("result") or {}
                ))
            match = re.fullmatch(
                r"/api/exchange/worker/jobs/([a-f0-9]{24})/(plan|progress|result)", path
            )
            if match:
                job_id, action = match.groups()
                worker_id = str(body.get("workerId") or "")
                if action == "plan":
                    return self.json_response(EXCHANGE.report_plan(job_id, worker_id, body.get("plan") or {}))
                token = str(body.get("executionToken") or "")
                if action == "progress":
                    return self.json_response(EXCHANGE.report_progress(
                        job_id, worker_id, token, body.get("event") or {}
                    ))
                return self.json_response(EXCHANGE.report_result(
                    job_id, worker_id, token, body.get("result") or {}
                ))
            self.send_error(404, "Not Found")
        except (ExchangeError, ProductImageError, ValueError, json.JSONDecodeError) as exc:
            status = exc.status if isinstance(exc, (ExchangeError, ProductImageError)) else 400
            self.json_response({"ok": False, "error": str(exc)}, status)
        except Exception as exc:
            logger.exception("Exchange POST error")
            self.json_response({"ok": False, "error": "换货接口暂时不可用"}, 500)

    def agent_operator(self, body=None):
        return str(self.headers.get("X-Agent-Operator") or (body or {}).get("operator") or "").strip()[:120]

    def agent_web_token(self, body=None):
        return str(self.headers.get("X-Agent-Web-Token") or (body or {}).get("webToken") or "").strip()

    def agent_principal(self, body=None, *, required=False):
        token = self.agent_web_token(body)
        claimed = self.agent_operator(body)
        session = WEB_AUTH.get_session(token) if token else {}
        if session:
            if claimed and not buyer_names_equivalent(
                claimed, session["buyerName"], include_nick=True,
            ):
                raise ActionError("网页署名与已绑定身份不一致", 403)
            return {
                "operator": session["buyerName"],
                "actorId": session["senderId"],
                "userId": session["userId"],
            }
        if required:
            raise ActionError(WEB_BIND_HINT, 401)
        return {"operator": claimed, "actorId": "", "userId": ""}

    def agent_error(self, exc):
        """把各子系统的异常映射成稳定的 HTTP 状态。"""
        if isinstance(exc, AgentDisabled):
            return self.json_response({"ok": False, "error": str(exc)}, 503)
        if isinstance(exc, ActionError):
            return self.json_response({"ok": False, "error": str(exc)}, exc.status)
        if isinstance(exc, WebAuthError):
            return self.json_response({"ok": False, "error": str(exc)}, 400)
        if isinstance(exc, (ExchangeError, OrderSourceError)):
            return self.json_response({"ok": False, "error": str(exc)}, getattr(exc, "status", 400))
        if isinstance(exc, ForecastUnavailable):
            return self.json_response({"ok": False, "error": str(exc)}, 503)
        if isinstance(exc, (LLMError, DingTalkError)):
            return self.json_response({"ok": False, "error": str(exc)}, 502)
        if isinstance(exc, (ToolError, ForecastError, QualityError, ValueError, json.JSONDecodeError)):
            return self.json_response({"ok": False, "error": str(exc)}, 400)
        if type(exc).__module__.split(".")[0] == "pymysql":
            logger.exception("Agent API database error")
            return self.json_response({"ok": False, "error": "实时数据库暂时不可用"}, 503)
        logger.exception("Agent API error")
        return self.json_response({"ok": False, "error": "Agent 接口暂时不可用"}, 500)

    def agent_get(self, path, parsed):
        forecast = path.startswith("/api/forecast/")
        if not self.require_agent_token(allow_web_session=not forecast):
            return
        try:
            query = parse_qs(parsed.query)
            if path == "/api/agent/me":
                principal = self.agent_principal(required=True)
                return self.json_response({
                    "ok": True,
                    "operator": principal["operator"],
                    "buyerName": principal["operator"],
                    "userId": principal["userId"],
                })
            if path == "/api/agent/status":
                return self.json_response({
                    "ok": True,
                    "agent": AGENT.status(),
                    "forecast": FORECAST.status(),
                    "dingtalk": {**DINGTALK_STREAM.status(), "reminder": REMINDER_SCHEDULER.status(),
                                 "notifier": REMINDER_NOTIFIER.status()},
                    "reservedTools": RESERVED_TOOLS,
                    "quality": QUALITY_SCHEDULER.status(),
                    "dropship": DROPSHIP_SCHEDULER.status(),
                    "insoleSchedule": INSOLE_SCHEDULER.status(),
                    "jobs": JOB_WORKER.status(),
                    "outbox": {"pending": OUTBOX.pending_count()},
                })
            if path == "/api/forecast/status":
                return self.json_response({"ok": True, **FORECAST.status()})
            principal = self.agent_principal(required=True)
            admin = _principal_is_admin(principal)
            if path == "/api/agent/workbench":
                return self.json_response(WORK_ITEMS.assemble(
                    actions=AGENT.actions,
                    exchange=EXCHANGE,
                    quality=QUALITY,
                    jobs=JOBS,
                    outbox=OUTBOX,
                    operator="" if admin else principal["operator"],
                    limit=int(query.get("limit", ["50"])[0] or 50),
                ))
            if path == "/api/agent/actions":
                kwargs = {
                    "session_id": query.get("session_id", [None])[0],
                    "limit": query.get("limit", ["20"])[0],
                }
                if not admin:
                    kwargs.update(
                        operator=principal["operator"],
                        actor_id=principal["actorId"],
                        user_id=principal["userId"],
                    )
                return self.json_response({"actions": AGENT.pending(**kwargs)})
            match = re.fullmatch(r"/api/agent/actions/([a-f0-9]{24})", path)
            if match:
                action = AGENT.actions.get(match.group(1))
                if not _owns_record(
                    principal, operator=action.get("operator") or "",
                    user_id=action.get("userId") or "", actor_id=action.get("actorId") or "",
                    admin=admin,
                ):
                    raise ActionError("无权查看该待确认动作", 403)
                return self.json_response(action)
            match = re.fullmatch(r"/api/agent/sessions/([a-f0-9]{24})/messages", path)
            if match:
                session = AGENT.sessions.get(match.group(1))
                if not session:
                    raise ActionError("会话不存在", 404)
                if not _owns_record(
                    principal, operator=session.get("operator") or "",
                    user_id=session.get("userId") or "", admin=admin,
                ):
                    raise ActionError("无权查看该会话", 403)
                return self.json_response({"messages": AGENT.sessions.transcript(
                    match.group(1), limit=int(query.get("limit", ["50"])[0] or 50)
                )})
            if path in ("/api/agent/audit/runs", "/api/agent/audit/tools"):
                if not admin:
                    raise ActionError("只有管理员可以查看审计", 403)
                if path.endswith("/runs"):
                    return self.json_response({"runs": AUDIT.recent_runs(
                        limit=int(query.get("limit", ["20"])[0] or 20)
                    )})
                return self.json_response({"tools": AUDIT.recent_tools(
                    limit=int(query.get("limit", ["50"])[0] or 50)
                )})
            if path == "/api/agent/staff":
                bindings = STAFF_DIRECTORY.list()
                if not admin:
                    bindings = [
                        item for item in bindings
                        if (principal["actorId"] and item.get("dingtalkUserId") == principal["actorId"])
                        or buyer_names_equivalent(
                            principal["operator"], item.get("buyerName") or "", include_nick=True,
                        )
                    ]
                return self.json_response({"bindings": bindings})
            if path == "/api/agent/reminders":
                rows, meta = followup_rows()
                reminders = build_reminders(
                    rows, query.get("today", [None])[0], profile="followup",
                )
                orders, matched = filter_orders(
                    reminders,
                    buckets=query.get("bucket") or None,
                    buyer=query.get("buyer", [""])[0],
                    limit=query.get("limit", ["100"])[0],
                )
                return self.json_response({
                    "today": reminders["today"], "year": meta.get("year"),
                    "totals": reminders["totals"], "buckets": reminders["buckets"],
                    "byBuyer": reminders["byBuyer"], "matched": matched, "orders": orders,
                })
            self.send_error(404, "Not Found")
        except Exception as exc:
            self.agent_error(exc)

    def agent_post(self, path):
        try:
            if path == "/api/agent/login":
                body = self.read_json_body(max_size=2 * 1024 * 1024)
                result = WEB_AUTH.login(
                    username=str(body.get("username") or body.get("operator") or ""),
                    password=str(body.get("password") or ""),
                    directory=STAFF_DIRECTORY,
                )
                return self.json_response({"ok": True, **result})
            if path == "/api/agent/logout":
                body = {}
                try:
                    body = self.read_json_body(max_size=2 * 1024 * 1024)
                except ValueError:
                    body = {}
                WEB_AUTH.revoke(self.agent_web_token(body))
                return self.json_response({"ok": True})
            if path == "/api/agent/web-bind":
                body = self.read_json_body(max_size=2 * 1024 * 1024)
                result = WEB_AUTH.consume_code(
                    operator=self.agent_operator(body),
                    code=str(body.get("code") or body.get("bindCode") or ""),
                    directory=STAFF_DIRECTORY,
                )
                return self.json_response({"ok": True, **result})
            forecast = path.startswith("/api/forecast/")
            if not self.require_agent_token(allow_web_session=not forecast):
                return
            body = self.read_json_body(max_size=2 * 1024 * 1024)
            if path == "/api/forecast/predict":
                return self.json_response(FORECAST.predict(
                    body.get("keys") or body.get("skus") or [],
                    horizon_days=body.get("horizonDays"),
                    start_date=body.get("startDate"),
                ))
            if path == "/api/forecast/order-suggestion":
                return self.json_response(FORECAST.order_suggestion(
                    body.get("keys") or body.get("skus") or [],
                    lead_time_days=body.get("leadTimeDays"),
                    service_level=body.get("serviceLevel"),
                    buffer_days=body.get("bufferDays"),
                    inventory=body.get("inventory"),
                    today=body.get("today"),
                ))
            if path == "/api/forecast/reload":
                return self.json_response({"ok": True, **FORECAST.reload()})
            principal = self.agent_principal(body, required=True)
            operator = principal["operator"]
            actor_id = principal["actorId"]
            if path == "/api/agent/chat":
                return self.json_response(AGENT.chat(
                    message=body.get("message") or "",
                    session_key=str(body.get("sessionKey") or body.get("sessionId") or operator or "web"),
                    operator=operator,
                    channel="web",
                    actor_id=actor_id,
                ))
            match = re.fullmatch(r"/api/agent/actions/([a-f0-9]{24})/(confirm|cancel)", path)
            if match:
                action_id, action = match.groups()
                if action == "confirm":
                    return self.json_response(AGENT.confirm(
                        action_id, operator, channel="web", actor_id=actor_id,
                    ))
                return self.json_response(AGENT.cancel(
                    action_id, operator, channel="web", actor_id=actor_id,
                ))
            match = re.fullmatch(r"/api/agent/sessions/([a-f0-9]{24})/reset", path)
            if match:
                session = AGENT.sessions.get(match.group(1))
                if not session:
                    raise ActionError("会话不存在", 404)
                if not _owns_record(
                    principal, operator=session.get("operator") or "",
                    user_id=session.get("userId") or "",
                    admin=_principal_is_admin(principal),
                ):
                    raise ActionError("无权重置该会话", 403)
                return self.json_response(AGENT.sessions.reset(match.group(1)))
            if path == "/api/agent/staff":
                if not _principal_is_admin(principal):
                    raise ActionError("只有管理员可以改员工绑定", 403)
                return self.json_response(STAFF_DIRECTORY.upsert(
                    body.get("buyerName") or body.get("buyer_name") or "",
                    dingtalk_user_id=body.get("dingtalkUserId") or "",
                    mobile=body.get("mobile") or "",
                    note=body.get("note") or "",
                    aliases=body.get("aliases") or (),
                ), 201)
            match = re.fullmatch(r"/api/agent/quality/([a-f0-9]{6})/(resolve|cancel)", path)
            if match:
                issue_id, decision = match.groups()
                return self.json_response(WORK_ITEMS.decide_quality(
                    QUALITY, issue_id=issue_id, decision=decision, operator=operator,
                    resolution=str(body.get("resolution") or ""),
                    directory=STAFF_DIRECTORY,
                ))
            if path == "/api/agent/quality/report":
                if not STAFF_DIRECTORY.known_operator(operator):
                    raise ActionError(WEB_OPERATOR_UNBOUND, 403)
                return self.json_response(QUALITY_SCHEDULER.run_once(operator=operator or "web"))
            if path == "/api/agent/reminders/push":
                if not STAFF_DIRECTORY.known_operator(operator):
                    raise ActionError(WEB_OPERATOR_UNBOUND, 403)
                buckets = body.get("buckets")
                if buckets is not None and not isinstance(buckets, list):
                    raise ValueError("buckets 必须是数组")
                return self.json_response(REMINDER_SCHEDULER.run_once(
                    today=body.get("today"),
                    buyer=str(body.get("buyer") or "").strip(),
                    buckets=buckets,
                    operator=operator or "web",
                    profile="followup",
                ))
            self.send_error(404, "Not Found")
        except Exception as exc:
            self.agent_error(exc)

    def agent_contract_generate(self):
        try:
            body = self.read_json_body()
            po_id = str(body.get("purchaseOrderNo") or body.get("poId") or "")
            if not _valid_po_id(po_id):
                raise ValueError("采购单号格式不正确")
            invoice_type = str(body.get("invoiceType") or "special_invoice")
            from .contracts import INVOICE_LABELS
            if invoice_type not in INVOICE_LABELS:
                raise ValueError("票种只能是 no_invoice、normal_invoice 或 special_invoice")
            contract_id = secrets.token_hex(12)
            output_dir = OUTPUTS_DIR / "agent" / contract_id
            output = output_dir / "contract.xlsx"
            preview = output_dir / "preview.png"
            generate_contract(
                po_id, invoice_type, output,
                tax_rate=body.get("taxRate"),
                price_overrides=body.get("priceOverrides") or {},
                gb_overrides=body.get("gbOverrides") or body.get("gb_overrides") or {},
                payment_option=body.get("paymentOption"),
                payment_text=body.get("paymentText"),
                receiving_info=body.get("receivingInfo"),
                inspection_extra=body.get("inspectionExtra"),
                preview_path=preview,
                env_path=REALTIME_ENV_PATH,
            )
            self.json_response({
                "ok": True,
                "contractId": contract_id,
                "purchaseOrderNo": po_id,
                "invoiceType": invoice_type,
                "fileName": f"采购合同-{po_id}.xlsx",
                "previewUrl": f"/api/agent/contracts/{contract_id}/preview",
                "downloadUrl": f"/api/agent/contracts/{contract_id}/file",
            }, 201)
        except (ValueError, RuntimeError, subprocess.SubprocessError) as exc:
            self.json_response({"ok": False, "error": str(exc)}, 400)
        except Exception as exc:
            logger.exception("Agent contract generation error")
            self.json_response({"ok": False, "error": "Agent 生成采购合同失败"}, 500)

    def agent_contract_file(self, contract_id, kind):
        output_dir = OUTPUTS_DIR / "agent" / contract_id
        if kind == "file":
            path = output_dir / "contract.xlsx"
            if path.exists():
                return self.xlsx_response(path, f"采购合同-{contract_id}.xlsx")
        else:
            path = output_dir / "preview.png"
            if path.exists():
                return self.file_response(path, "image/png")
        self.json_response({"ok": False, "error": "合同文件不存在或已清理"}, 404)

    def contract_options(self, po_id):
        try:
            if not _valid_po_id(po_id):
                raise ValueError("采购单号格式不正确")
            self.json_response(get_contract_options(po_id, REALTIME_ENV_PATH))
        except ValueError as exc:
            self.json_response({"ok": False, "error": str(exc)}, 400)

    def contract_orders(self, query=""):
        try:
            self.json_response({"orders": fetch_contract_order_choices(REALTIME_ENV_PATH, query=query)})
        except Exception as exc:
            logger.exception("Contract order list error")
            self.json_response({"ok": False, "error": "采购单列表暂时不可用"}, 503)

    def contract_gb_search(self, query="", name="", category=""):
        """合同页按标准号前缀或名称找执行标准，只读目录，不改任何选择。"""
        query = str(query or "").strip()[:64]
        try:
            standards = search_contract_standards(
                REALTIME_ENV_PATH, query, limit=12,
                product_name=str(name or "").strip()[:80],
                category=str(category or "").strip()[:80],
            )
            self.json_response({"query": query, "standards": standards})
        except Exception:
            logger.exception("Contract GB search error")
            self.json_response({"ok": False, "error": "国标目录暂时不可用"}, 503)

    def contract_gb_recommend(self):
        """用商品信息给执行标准排序；模型只能从候选里挑，不能编造标准号。"""
        try:
            body = self.read_json_body(max_size=64 * 1024)
        except ValueError as exc:
            return self.json_response({"ok": False, "error": str(exc)}, 400)
        candidates = body.get("candidates") or []
        if not isinstance(candidates, list):
            return self.json_response({"ok": False, "error": "candidates 必须是数组"}, 400)
        options = []
        for raw in candidates[:40]:
            if not isinstance(raw, dict):
                continue
            number = str(raw.get("standardNo") or raw.get("standard_no") or "").strip()
            if not number:
                continue
            options.append({
                "samrId": str(raw.get("samrId") or raw.get("samr_id") or ""),
                "standardNo": number,
                "nameCn": str(raw.get("nameCn") or raw.get("name_cn") or ""),
                "status": str(raw.get("status") or ""),
                "nature": str(raw.get("nature") or ""),
                "stdType": str(raw.get("stdType") or raw.get("std_type") or ""),
                "recommended": False,
                "recommendReason": "",
            })
        product = {
            "name": str(body.get("name") or "").strip()[:80],
            "category": str(body.get("category") or "").strip()[:80],
            "specification": str(body.get("specification") or "").strip()[:120],
            "remark": str(body.get("remark") or "").strip()[:120],
        }
        picked = []
        source = "catalog"
        if product["name"] or product["category"]:
            try:
                extras = expand_recommend_candidates(
                    REALTIME_ENV_PATH, product, options, limit=12,
                )
                if extras:
                    options.extend(extras)
                    source = "catalog+search"
            except Exception:
                logger.exception("Contract GB catalog expand failed")
        if flag(setting("CONTRACT_GB_WEBSEARCH", "false")) and product["name"]:
            try:
                extras = search_samr_catalog_hits(
                    REALTIME_ENV_PATH, product["name"], limit=8,
                )
                known = {item["standardNo"] for item in options}
                added = False
                for extra in extras:
                    if extra["standardNo"] not in known:
                        options.append(extra)
                        known.add(extra["standardNo"])
                        added = True
                if added:
                    source = "catalog+websearch"
            except Exception:
                logger.exception("Contract GB websearch fallback failed")
        if flag(setting("CONTRACT_GB_AI", "true")) and AGENT.llm.configured and options:
            try:
                answer = AGENT.llm.chat(gb_recommend_prompt(product, options))
                picked = parse_recommended_nos(
                    answer.get("content"),
                    [item["standardNo"] for item in options],
                )
                if picked:
                    source = "ai"
            except Exception:
                logger.exception("Contract GB AI recommend failed")
        standards = mark_recommended_options(options, picked)
        self.json_response({
            "ok": True,
            "source": source,
            "standards": standards,
            "recommended": [item for item in standards if item.get("recommended")],
        })

    def file_response(self, path, content_type):
        body = Path(path).read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def xlsx_response(self, path, filename):
        body = Path(path).read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        self.send_header("Content-Disposition", "attachment; filename*=UTF-8''" + quote(filename))
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def health(self):
        """数据库探活 + 各子系统状态。库连不上也要把子系统状态报全，那正是最需要看的时候。"""
        # 异常类型名不带库地址和账号，健康检查无鉴权，不要回具体报错文本。
        now = business_now()
        payload = {
            "ok": True,
            "database": "connected",
            "now": now.strftime("%Y-%m-%d %H:%M:%S"),
            "today": now.date().isoformat(),
        }
        try:
            with connect(REALTIME_ENV_PATH, autocommit=True) as conn:
                with conn.cursor() as cursor:
                    cursor.execute("SELECT 1")
                    cursor.fetchone()
        except Exception as exc:
            payload.update({"ok": False, "database": "unavailable", "error": type(exc).__name__})
        try:
            sync = fetch_realtime_sync_state(REALTIME_ENV_PATH)
            payload["syncedAt"] = sync.get("syncedAt") or ""
            payload["syncLagMinutes"] = sync.get("syncLagMinutes")
        except Exception:
            payload.setdefault("syncedAt", "")
            payload.setdefault("syncLagMinutes", None)
        payload.update({
            "activeSource": _source_state["source"] or "供应链 API 本地实时镜像",
            "activeYear": _source_state["year"],
            "warning": _source_state["warning"],
            "realtimeMirror": _safe_status(REALTIME_MIRROR_SCHEDULER.status),
            "gbStandards": _safe_status(GB_STANDARDS_SCHEDULER.status),
            "exchange": _safe_status(EXCHANGE.status),
            "erpWorker": _safe_status(lambda: public_worker_status({
                **DIGITAL_WORKER.status(),
                "keepAlive": ERP_KEEPALIVE.status(),
            })),
            "jobs": _safe_status(JOB_WORKER.status),
            "outbox": _safe_status(lambda: {"pending": OUTBOX.pending_count()}),
            "agent": _safe_status(lambda: {
                "enabled": AGENT.enabled, "available": AGENT.available,
                "llm": AGENT.llm.status(), "tools": len(AGENT.registry.names()),
            }),
            "forecast": _safe_status(lambda: FORECAST.status()["artifact"]),
            "dingtalk": {
                "stream": _safe_status(DINGTALK_STREAM.status),
                "reminder": _safe_status(REMINDER_SCHEDULER.status),
                "sender": _safe_status(DINGTALK_SENDER.status),
            },
            "quality": _safe_status(QUALITY_SCHEDULER.status),
            "dropship": _safe_status(DROPSHIP_SCHEDULER.status),
            "insoleSchedule": _safe_status(INSOLE_SCHEDULER.status),
            "spuSnapshot": _safe_status(SPU_SCHEDULER.status),
            "spuPlanUpload": _safe_status(PLAN_UPDATER.status),
            "source": cached_source_card(),
            "schedules": health_schedules(),
        })
        self.json_response(payload, 200 if payload["ok"] else 503)

    def _spu_board(self, parsed=None):
        query = parsed.query if parsed is not None else urlparse(self.path).query
        return normalize_board((parse_qs(query).get("board") or ["apparel"])[0])

    def spu_summary(self, parsed=None):
        """SPU 看板：读对应结果表，不触发重算。`board=baihuo` 为自营百货。"""
        board = self._spu_board(parsed)
        try:
            payload = load_style_snapshot(REALTIME_ENV_PATH, board=board)
        except Exception:
            logger.exception("SPU snapshot read failed")
            return self.json_response({"ok": False, "error": "SPU 结果表暂时不可用"}, 503)
        payload["refreshing"] = SPU_REFRESH_LOCK.locked()
        payload["refresh"] = dict(SPU_REFRESH_STATE)
        from .spu_plan.analyze import load_day_analyses
        payload["analyses"] = load_day_analyses(board=board)
        return self.json_response(payload)

    def spu_refresh(self):
        """后台重算结果表（约一分钟）；已在跑就直接返回 busy。"""
        board = self._spu_board()
        if not SPU_REFRESH_LOCK.acquire(blocking=False):
            return self.json_response({"ok": True, "started": False, "busy": True, "board": board})

        def run():
            try:
                result = build_style_alerts(REALTIME_ENV_PATH, board=board)
                save_style_snapshot(REALTIME_ENV_PATH, result, board=board)
                SPU_REFRESH_STATE.update(
                    finishedAt=business_timestamp(), lastError="",
                )
            except SpuDataMissing as exc:
                SPU_REFRESH_STATE.update(lastError=str(exc))
            except Exception as exc:
                logger.exception("SPU snapshot refresh failed")
                SPU_REFRESH_STATE.update(lastError=str(exc)[:500])
            finally:
                SPU_REFRESH_LOCK.release()

        SPU_REFRESH_STATE.update(startedAt=business_timestamp(), lastError="")
        threading.Thread(target=run, name="spu-refresh", daemon=True).start()
        return self.json_response({"ok": True, "started": True, "busy": False, "board": board})

    def purchase_draft_create(self):
        """看板勾选 → 采购单草稿（预览 JSON + 本机 xlsx）。不写 ERP。"""
        try:
            body = self.read_json_body()
        except ValueError as exc:
            return self.json_response({"ok": False, "error": str(exc)}, 400)
        if not isinstance(body, dict):
            return self.json_response({"ok": False, "error": "请求不是对象"}, 400)
        style_ids = body.get("styleIds") or body.get("style_ids") or []
        if not isinstance(style_ids, list):
            return self.json_response({"ok": False, "error": "styleIds 必须是数组"}, 400)
        quantities = body.get("quantities") if isinstance(body.get("quantities"), dict) else {}
        try:
            principal = self.agent_principal(body)
            draft = create_purchase_draft(
                board=str(body.get("board") or "apparel"),
                style_ids=style_ids,
                quantities=quantities,
                env_path=REALTIME_ENV_PATH,
                operator=principal.get("operator") or "",
            )
        except PurchaseDraftError as exc:
            return self.json_response({"ok": False, "error": str(exc)}, 400)
        except Exception:
            logger.exception("purchase draft create failed")
            return self.json_response({"ok": False, "error": "采购单草稿暂时不可用"}, 503)
        return self.json_response(public_draft(draft))

    def purchase_draft_save(self, draft_id):
        try:
            body = self.read_json_body()
            draft = apply_draft_edits(load_purchase_draft(draft_id), body)
        except PurchaseDraftError as exc:
            return self.json_response({"ok": False, "error": str(exc)}, 400)
        except Exception:
            logger.exception("purchase draft save failed")
            return self.json_response({"ok": False, "error": "保存草稿失败"}, 503)
        return self.json_response(public_draft(draft))

    def purchase_draft_confirm(self, draft_id):
        try:
            body = self.read_json_body()
            draft = load_purchase_draft(draft_id)
            result = submit_purchase_draft(
                draft, DIGITAL_WORKER.runtime,
                env_path=REALTIME_ENV_PATH, body=body,
            )
        except PurchaseDraftError as exc:
            return self.json_response({"ok": False, "error": str(exc)}, 400)
        except ErpUnknownResult as exc:
            return self.json_response({"ok": False, "error": str(exc), "unknown": True}, 409)
        except (ErpError, ValueError) as exc:
            return self.json_response({"ok": False, "error": str(exc)}, 400)
        except Exception:
            logger.exception("purchase draft confirm failed")
            return self.json_response({"ok": False, "error": "写入 ERP 失败"}, 503)
        return self.json_response(result)

    def purchase_draft_get(self, draft_id):
        try:
            draft = load_purchase_draft(draft_id)
        except PurchaseDraftError as exc:
            return self.json_response({"ok": False, "error": str(exc)}, 404)
        return self.json_response(public_draft(draft))

    def purchase_draft_file(self, draft_id):
        try:
            draft = load_purchase_draft(draft_id)
        except PurchaseDraftError as exc:
            return self.json_response({"ok": False, "error": str(exc)}, 404)
        path = draft_xlsx_path(draft_id)
        if not path.is_file():
            return self.json_response({"ok": False, "error": "采购单文件已不在"}, 404)
        return self.xlsx_response(path, draft.get("filename") or f"{draft_id}-采购单草稿.xlsx")

    def purchase_draft_template(self):
        path = write_blank_purchase_template()
        return self.xlsx_response(path, "采购单模板.xlsx")

    def _spu_snapshot_or_error(self, board="apparel"):
        try:
            return load_style_snapshot(REALTIME_ENV_PATH, board=board), None
        except Exception:
            logger.exception("SPU snapshot read failed")
            return None, self.json_response({"ok": False, "error": "SPU 结果表暂时不可用"}, 503)

    def _spu_snapshot_for_style(self, style_id: str, board=None):
        """分析时先看指定看板，再在另一张结果表找。"""
        from .spu_plan.analyze import find_style

        preferred = normalize_board(board) if board else None
        order = [preferred] if preferred else []
        for name in BOARDS:
            if name not in order:
                order.append(name)
        for name in order:
            snapshot, error = self._spu_snapshot_or_error(name)
            if error:
                return None, None, error
            if find_style(snapshot, style_id) is not None:
                return snapshot, name, None
        return None, None, None

    def spu_analyze_get(self, parsed):
        """读当天/昨日缓存，不调模型。"""
        from .spu_plan.analyze import load_cached_analysis

        style_id = str((parse_qs(parsed.query).get("styleId") or [""])[0]).strip()
        if not style_id or len(style_id) > 64:
            return self.json_response({"ok": False, "error": "styleId 不正确"}, 400)
        cached = load_cached_analysis(style_id)
        if cached is None:
            return self.json_response({
                "ok": True, "styleId": style_id, "analysis": "",
                "analyzedAt": "", "day": "", "cached": False, "stale": False,
            })
        return self.json_response(cached)

    def spu_analyze(self):
        """单款分析：当天有缓存直接回；否则模型先调工具再写缓存。"""
        from .spu_plan.analyze import find_style, run_style_analysis

        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > 4096:
                raise ValueError("请求内容大小不正确")
            body = json.loads(self.rfile.read(length).decode("utf-8"))
            style_id = str(body.get("styleId") or "").strip()
            if not style_id or len(style_id) > 64:
                raise ValueError("styleId 不正确")
            board = body.get("board")
        except (ValueError, json.JSONDecodeError) as exc:
            return self.json_response({"ok": False, "error": str(exc)}, 400)
        snapshot, _board, error = self._spu_snapshot_for_style(style_id, board)
        if error:
            return error
        if snapshot is None or find_style(snapshot, style_id) is None:
            return self.json_response({"ok": False, "error": "该款式不在当前结果表里"}, 404)
        try:
            result = run_style_analysis(
                style_id, snapshot=snapshot,
                llm=AGENT.llm if AGENT.enabled else None,
                force=bool(body.get("force")),
            )
        except ValueError as exc:
            return self.json_response({"ok": False, "error": str(exc)}, 400)
        except RuntimeError as exc:
            return self.json_response({"ok": False, "error": str(exc)}, 503)
        except LLMError as exc:
            return self.json_response({"ok": False, "error": f"模型调用失败：{exc}"}, 502)
        return self.json_response(result)

    def spu_plan_upload(self):
        """网页上传订货表：校验四张表后覆盖源文件并后台重生成生产计划表。"""
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if length <= 0 or length > MAX_UPLOAD_BYTES:
            return self.json_response({"ok": False, "error": "文件大小不正确（上限 40MB）"}, 400)
        data = self.rfile.read(length)
        try:
            checked = PLAN_UPDATER.update(data, origin="web")
        except PlanSourceError as exc:
            status = 409 if "还在重生成" in str(exc) else 400
            return self.json_response({"ok": False, "error": str(exc)}, status)
        return self.json_response({
            "ok": True, "started": True, "styles": checked.get("styles"),
            "message": "已保存订货表，正在重生成生产计划表（约 3 分钟）",
        })

    def quality_report_file(self, compact_date, sig):
        secret = setting("QUALITY_REPORT_LINK_SECRET", "")
        if not report_link_valid(secret, compact_date, sig):
            return self.json_response({"ok": False, "error": "链接无效或已过期"}, 404)
        path = OUTPUTS_DIR / "quality" / f"品控台账-{compact_date}.xlsx"
        if not path.exists():
            return self.json_response({"ok": False, "error": "日报文件不存在"}, 404)
        return self.xlsx_response(path, path.name)

    def api(self, path, year):
        try:
            if path.endswith("delivery"):
                self.json_response(followup_delivery_cache()["delivery"])
                return
            self.json_response(source_cache(year)["dashboard"])
        except Exception as exc:
            logger.exception("API error")
            self.json_response({"ok": False, "error": "采购数据暂时不可用"}, 503)

    def json_response(self, value, status=200):
        body = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if "gzip" in self.headers.get("Accept-Encoding", ""):
            body = gzip.compress(body, compresslevel=5)
            encoding = "gzip"
        else:
            encoding = None
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        if encoding:
            self.send_header("Content-Encoding", encoding)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def redirect(self, location):
        self.send_response(302)
        self.send_header("Location", quote(location, safe="/?=&"))
        self.end_headers()

    def spa(self, path):
        """托管 frontend/dist：命中构建产物就发文件，其余交给前端路由。"""
        if not INDEX_HTML.exists():
            return self.send_error(
                503,
                "Frontend Not Built",
                "前端还没构建：在仓库根目录执行 npm install && npm run build，或开发时用 npm run dev。",
            )
        target = self.resolve_asset(path)
        if target is not None:
            # 带内容哈希的产物可以长缓存，index.html 每次都要重新取。
            return self.static(target, cache="public, max-age=31536000, immutable" if "/assets/" in path else "no-cache")
        return self.static(INDEX_HTML, cache="no-cache")

    @staticmethod
    def resolve_asset(path):
        """把 URL 路径映射到 dist 里的文件，越界或不存在都返回 None。"""
        try:
            candidate = (DIST / path.lstrip("/")).resolve()
            if not candidate.is_file():
                return None
            if DIST.resolve() not in candidate.parents:
                return None
            return candidate
        except (ValueError, OSError):
            return None

    def static(self, file_path, cache="no-cache"):
        body = file_path.read_bytes()
        content_type = mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
        textual = content_type.startswith("text/") or content_type.endswith(("javascript", "json"))
        self.send_response(200)
        self.send_header("Content-Type", content_type + ("; charset=utf-8" if textual else ""))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", cache)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def handle_one_request(self):
        # 浏览器等不及大响应或看到 503 就断开是常态，socketserver 会为此打一整页栈。
        try:
            super().handle_one_request()
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True

    def log_message(self, fmt, *args):
        message = "%s - %s" % (self.address_string(), fmt % args)
        if "/api/health" in message:
            logger.debug(message)
        else:
            logger.info(message)


def main():
    parser = argparse.ArgumentParser(description="启动采购看板和数据库 API")
    parser.add_argument("--host", default=setting("APP_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(setting("APP_PORT", "8777")))
    args = parser.parse_args()
    configure_logging(level=setting("LOG_LEVEL", "INFO"), log_file=setting("LOG_FILE", "files/data/app.log"))
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    logger.info("采购看板已启动：http://%s:%s%s", args.host, args.port, HOME)
    # .env 在导入时读一次，改了配置不重启就不生效；把实际连的库打出来，免得对着旧连接排查。
    try:
        logger.info("镜像库：%s → %s", Path(REALTIME_ENV_PATH).name, describe_target(REALTIME_ENV_PATH))
    except (OSError, ValueError) as exc:
        logger.warning("镜像库配置不可用（%s）：%s", Path(REALTIME_ENV_PATH).name, exc)
    shadowed = shadowed_settings()
    if shadowed:
        logger.warning("注意：%s 被进程环境变量覆盖，.env 里的值没有生效", "、".join(shadowed))
    if not INDEX_HTML.exists():
        logger.warning("前端未构建：frontend/dist 不存在，页面返回 503（npm install && npm run build），接口仍可用")
    if AGENT.available:
        logger.info(
            "Agent 已启用：%s / %s · %s 个工具",
            AGENT.llm.provider, AGENT.llm.model, len(AGENT.registry.names()),
        )
    else:
        logger.info("Agent 未启用（AGENT_ENABLED 或模型凭证未就绪），/api/agent/chat 返回 503")
    restart_pending = list(getattr(AGENT, "restart_pending", None) or [])
    restart_ttl = int(getattr(AGENT, "restart_confirm_seconds", 300) or 300)
    if restart_pending:
        JOBS.enqueue("pending_expire", {}, delay_seconds=restart_ttl + 10, max_attempts=3)
        restart_notify = notify_pending_after_restart(
            restart_pending,
            sender=DINGTALK_SENDER if DINGTALK_SENDER.configured else None,
            directory=STAFF_DIRECTORY,
            audit=AUDIT,
            ttl_seconds=restart_ttl,
        )
        logger.warning(
            "进程重启补发待确认：%s 条，已私聊 %s，跳过 %s",
            restart_notify["count"], restart_notify["sent"], restart_notify["skipped"],
        )
    stream_status = DINGTALK_STREAM.start()
    if stream_status.get("running"):
        logger.info("钉钉 Stream 已启动（企业内部应用机器人长连）")
    elif flag(setting("DINGTALK_ENABLED", "false")):
        reason = stream_status.get("lastError") or "缺少 DINGTALK_CLIENT_ID / DINGTALK_CLIENT_SECRET"
        logger.warning("钉钉 Stream 未启动：%s", reason)
    else:
        logger.info("钉钉 Stream 未启用（DINGTALK_ENABLED=false）")
    REALTIME_MIRROR_SCHEDULER.start()
    if REALTIME_MIRROR_SCHEDULER.enabled:
        logger.info("订单/采购单/商品/供应商 API 实时镜像同步已启用")
    GB_STANDARDS_SCHEDULER.start()
    if GB_STANDARDS_SCHEDULER.enabled:
        logger.info("国标目录同步已启用：每天 %s", GB_STANDARDS_SCHEDULER.status()["sendTime"])
    reminder_on = flag(setting("DINGTALK_REMINDER_ENABLED", "false"))
    if reminder_on and REMINDER_NOTIFIER.enabled:
        REMINDER_SCHEDULER.start()
        logger.info("每日交期催办推送已启用：%s", REMINDER_SCHEDULER.status()["sendTime"])
    elif reminder_on:
        logger.warning("每日催办已开但发送通道未配置（需要 Webhook 或 应用机器人+群会话 ID）")
    if flag(setting("QUALITY_REPORT_ENABLED", "false")) and REMINDER_NOTIFIER.enabled:
        QUALITY_SCHEDULER.start()
        logger.info("每日品控日报已启用：%s", QUALITY_SCHEDULER.status()["sendTime"])
    DROPSHIP_SCHEDULER.start()
    INSOLE_SCHEDULER.start()
    if REALTIME_ENV_PATH:
        warm_insole_lines_cache(setting, REALTIME_ENV_PATH)
    SPU_SCHEDULER.start()
    if SPU_SCHEDULER.enabled:
        logger.info("鞋服 SPU 结果表每日重算已启用：%s", SPU_SCHEDULER.status()["runTime"])
    if DROPSHIP_SCHEDULER.enabled:
        logger.info(
            "每日代发已启用：%s 开始抓取，抓到后发群并私聊（不覆盖已填表）",
            DROPSHIP_SCHEDULER.status()["prepareTime"],
        )
    if INSOLE_SCHEDULER.enabled:
        logger.info(
            "抖音换鞋垫定时已启用：%s–%s 每 %s 分钟一轮",
            INSOLE_SCHEDULER.status()["startTime"],
            INSOLE_SCHEDULER.status()["endTime"],
            INSOLE_SCHEDULER.status()["intervalMinutes"],
        )
    MAINTENANCE.start()
    JOB_WORKER.start()
    erp_status = DIGITAL_WORKER.start()
    if erp_status.get("running"):
        logger.info("ERP Digital Worker 已启用：换货、探测、搜 SKU、商品图片走后端 Playwright")
    elif flag(setting("ERP_AI_ENABLED", "true")):
        logger.warning("ERP Digital Worker 未启动：%s", erp_status.get("lastError") or "未知原因")
    keep_status = ERP_KEEPALIVE.start()
    if keep_status.get("running"):
        logger.info("ERP 登录态随服务启停（浏览器 + cookie，不固定订单页）")
    elif ERP_KEEPALIVE.enabled:
        logger.info("ERP 登录态保活未启动：%s", keep_status.get("lastError") or "未配置")
    PAGE_CACHE_KEEPER.start()
    logger.info("看板/台账热缓存已启用：%s 秒后续热，之后每 %s 秒刷新",
                PAGE_CACHE_KEEPER.initial_delay, PAGE_CACHE_KEEPER.interval)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        PAGE_CACHE_KEEPER.stop()
        REALTIME_MIRROR_SCHEDULER.stop()
        GB_STANDARDS_SCHEDULER.stop()
        REMINDER_SCHEDULER.stop()
        QUALITY_SCHEDULER.stop()
        DROPSHIP_SCHEDULER.stop()
        INSOLE_SCHEDULER.stop()
        SPU_SCHEDULER.stop()
        MAINTENANCE.stop()
        JOB_WORKER.stop()
        DINGTALK_STREAM.stop()
        ERP_KEEPALIVE.stop()
        DIGITAL_WORKER.stop()
        server.server_close()


if __name__ == "__main__":
    main()
