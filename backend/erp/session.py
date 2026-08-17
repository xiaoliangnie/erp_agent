# -*- coding: utf-8 -*-
"""Playwright 浏览器会话。只做登录、保登录态、打开白名单页面。"""
from __future__ import annotations

import logging
from pathlib import Path

from .errors import ErpError

logger = logging.getLogger(__name__)


def _dismiss_prompts(page) -> None:
    """关掉登录后的提示框（未绑手机等），不处理验证码。"""
    for _ in range(4):
        clicked = False
        for name in ("确定", "确 定", "知道了", "关闭"):
            button = page.get_by_role("button", name=name)
            try:
                if button.count():
                    button.first.click(timeout=2000)
                    clicked = True
                    page.wait_for_timeout(400)
            except Exception:
                continue
        if not clicked:
            return


def playwright_available() -> bool:
    try:
        import playwright.sync_api  # noqa: F401
    except ImportError:
        return False
    return True


class BrowserSession:
    """同步 Playwright 上下文。同一进程只开一个浏览器。"""

    def __init__(self, config: dict, secrets: dict | None = None, *, playwright=None):
        self.config = config
        self._secrets = secrets or {}
        self._playwright_mod = playwright
        self._pw = None
        self._browser = None
        self._context = None
        self.page = None

    def start(self, *, headed=None) -> None:
        if self.page is not None:
            return
        api = self._playwright_mod
        if api is None:
            if not playwright_available():
                raise ErpError("未安装 Playwright。请执行：pip install playwright && playwright install chromium")
            from playwright.sync_api import sync_playwright
            api = sync_playwright().start()
            self._pw = api
        headless = self.config.get("headless", True) if headed is None else not headed
        state = Path(self.config["storageStatePath"])
        state.parent.mkdir(parents=True, exist_ok=True)
        launch = api.chromium.launch(headless=headless)
        self._browser = launch
        kwargs = {"viewport": {"width": 1440, "height": 900}}
        if state.exists():
            kwargs["storage_state"] = str(state)
        self._context = launch.new_context(**kwargs)
        self.page = self._context.new_page()

    def close(self) -> None:
        for closer in (self._context, self._browser):
            if closer is None:
                continue
            try:
                closer.close()
            except Exception:
                logger.exception("关闭 ERP 浏览器失败")
        if self._pw is not None:
            try:
                self._pw.stop()
            except Exception:
                logger.exception("停止 Playwright 失败")
        self.page = None
        self._context = None
        self._browser = None
        self._pw = None

    def save_state(self) -> None:
        if self._context is None:
            return
        path = Path(self.config["storageStatePath"])
        path.parent.mkdir(parents=True, exist_ok=True)
        self._context.storage_state(path=str(path))

    def goto(self, url: str, *, timeout=60000):
        if self.page is None:
            raise ErpError("浏览器尚未启动")
        self.page.goto(url, wait_until="domcontentloaded", timeout=timeout)
        try:
            self.page.wait_for_function(
                """() => {
                    if (document.querySelector('#login_id')) return true;
                    if (/login\\.aspx/i.test(location.href)) return false;
                    return !!(document.body && document.body.innerText && document.body.innerText.length > 20);
                }""",
                timeout=min(15000, timeout),
            )
        except Exception:
            pass
        return self.page

    def logged_in(self) -> bool:
        if self.page is None:
            return False
        href = str(self.page.url or "").lower()
        if not href.startswith("http"):
            return False
        if "login.aspx" in href:
            return False
        if "erp321.com" not in href and "jushuitan.com" not in href:
            return False
        try:
            if self.page.locator("#login_id").count():
                return False
        except Exception:
            pass
        return True

    def login_if_needed(self, *, headed=False, wait_ms=180000) -> dict:
        """密码登录；没有密码且 headed 时等人扫码/手输。已在 ERP 内则不重开首页。"""
        self.start(headed=headed or not self.config.get("headless", True))
        if self.logged_in():
            return {"ok": True, "loggedIn": True, "url": self.page.url, "method": "already"}
        self.goto(self.config["baseUrl"])
        if self.logged_in():
            self.save_state()
            return {"ok": True, "loggedIn": True, "url": self.page.url, "method": "storage_state"}
        username = self.config.get("username") or ""
        password = self._secrets.get("password") or ""
        page = self.page
        if username and password:
            account = page.locator("#login_id")
            if account.count() == 0:
                raise ErpError("登录页没有 #login_id，选择器可能已变")
            account.fill(username)
            page.locator("#password").fill(password)
            box = page.locator("input[type=checkbox]")
            try:
                if box.count() and not box.first.is_checked():
                    box.first.check()
            except Exception:
                logger.debug("登录页协议勾选框不可点，继续提交")
            page.get_by_role("button", name="立即登录").click()
            try:
                page.get_by_text("尚未绑定手机号码").wait_for(timeout=8000)
            except Exception:
                pass
            _dismiss_prompts(page)
            method = "password"
        elif headed:
            logger.info("ERP 登录页已打开，请在浏览器里完成登录（不要把密码发到聊天）")
            method = "manual"
        else:
            raise ErpError("未登录且没有 ERP_AI_USERNAME / ERP_AI_PASSWORD。先跑 scripts/run_erp_worker.py login")
        try:
            page.wait_for_function(
                "() => !/login\\.aspx/i.test(location.href) && !document.querySelector('#login_id')",
                timeout=wait_ms,
            )
        except Exception as exc:
            hint = ""
            try:
                texts = page.locator("body").inner_text()
                for line in str(texts).splitlines():
                    line = line.strip()
                    if line and any(key in line for key in ("错误", "失败", "验证", "锁定", "不正确")):
                        hint = line[:80]
                        break
            except Exception:
                hint = ""
            raise ErpError("登录后仍停在登录页" + (f"：{hint}" if hint else "")) from exc
        if not self.logged_in():
            raise ErpError("登录后仍停在登录页")
        self.save_state()
        return {"ok": True, "loggedIn": True, "url": page.url, "method": method}
