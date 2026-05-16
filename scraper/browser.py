import json
import logging
import os
import re
from pathlib import Path

from playwright.async_api import async_playwright, Browser, BrowserContext, Playwright

from scraper.models import SessionExpiredError, CloudflareBlockError

logger = logging.getLogger(__name__)

LOGIN_URL = "https://www.aliexpress.com/p/order/index.html"
_SESSION_EXPIRED_PATTERN = re.compile(r"(passport|login)\.(aliexpress|alibaba)\.com")


class BrowserManager:
    def __init__(self, cookies_path: str = "data/cookies.json", extra_args: list[str] | None = None):
        self._cookies_path = Path(cookies_path)
        self._extra_args = extra_args or []
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None

    async def start(self, headless: bool = True) -> None:
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(
            headless=headless,
            args=["--no-sandbox", "--disable-blink-features=AutomationControlled"] + self._extra_args,
        )
        self._context = await self._browser.new_context(
            viewport={"width": 1280, "height": 800},
            locale="en-US",
            timezone_id="America/New_York",
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
            ),
        )
        await self._context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )

    async def load_cookies(self) -> bool:
        if not self._cookies_path.exists():
            return False
        try:
            raw = json.loads(self._cookies_path.read_text())
            # Strip CDP-only fields that Playwright's add_cookies rejects
            allowed = {"name", "value", "domain", "path", "expires", "httpOnly", "secure", "sameSite"}
            cookies = [{k: v for k, v in c.items() if k in allowed} for c in raw]
            await self._context.add_cookies(cookies)
            logger.info("Loaded %d cookies from %s", len(cookies), self._cookies_path)
            return True
        except Exception as e:
            logger.warning("Failed to load cookies: %s", e)
            return False

    async def save_cookies(self) -> None:
        cookies = await self._context.cookies()
        self._cookies_path.parent.mkdir(parents=True, exist_ok=True)
        self._cookies_path.write_text(json.dumps(cookies, indent=2))
        os.chmod(self._cookies_path, 0o600)
        logger.info("Saved %d cookies to %s", len(cookies), self._cookies_path)

    async def is_session_valid(self) -> bool:
        page = await self._context.new_page()
        try:
            await page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=30_000)
            url = page.url
            logger.debug("Session check URL: %s", url)
            if _SESSION_EXPIRED_PATTERN.search(url):
                logger.info("Session invalid — redirected to login page")
                return False
            return True
        except Exception as e:
            logger.warning("Session validity check failed: %s", e)
            return False
        finally:
            await page.close()

    async def new_page(self):
        return await self._context.new_page()

    async def ensure_authenticated(self) -> None:
        if not self._cookies_path.exists():
            raise SessionExpiredError("No cookies file found. Run: python main.py --login")
        await self.load_cookies()
        if not await self.is_session_valid():
            raise SessionExpiredError("AliExpress session expired. Run: python main.py --login")

    async def run_login_flow(self) -> None:
        """Open a headed browser, wait for the user to complete Google OAuth."""
        import asyncio as _asyncio
        page = await self._context.new_page()
        logger.info("Opening AliExpress orders page — please complete login in the browser window.")
        await page.goto(LOGIN_URL, wait_until="domcontentloaded")
        print("\nPlease log in to AliExpress in the browser window.")
        print("Waiting for orders page (or press Enter here to save cookies manually)...\n")

        auto_done = _asyncio.Event()
        seen_login = False

        async def _on_url_change(frame):
            nonlocal seen_login
            url = page.url
            if "passport" in url or "login" in url:
                seen_login = True
            elif seen_login and "/p/order/" in url:
                auto_done.set()

        page.on("framenavigated", _on_url_change)

        # Also accept manual Enter press
        def _stdin_reader():
            try:
                input()
            except Exception:
                pass
            auto_done.set()

        import threading
        t = threading.Thread(target=_stdin_reader, daemon=True)
        t.start()

        try:
            await _asyncio.wait_for(auto_done.wait(), timeout=300)
        except _asyncio.TimeoutError:
            raise RuntimeError("Login timed out after 5 minutes.")
        finally:
            page.remove_listener("framenavigated", _on_url_change)
            await page.close()

        await self.save_cookies()
        print("Login successful. Cookies saved to", self._cookies_path)

    async def stop(self) -> None:
        if self._context:
            try:
                await self.save_cookies()
            except Exception:
                pass
        if self._browser:
            await self._browser.close()
        if self._playwright:
            await self._playwright.stop()
        self._context = None
        self._browser = None
        self._playwright = None
