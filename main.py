import argparse
import asyncio
import logging
import os
import shutil
import signal
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(override=True)

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("logs/bot.log"),
    ],
)
logger = logging.getLogger(__name__)


def _load_settings():
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not token:
        raise SystemExit("TELEGRAM_BOT_TOKEN not set. Copy .env.example to .env and fill it in.")
    if not chat_id:
        raise SystemExit("TELEGRAM_CHAT_ID not set in .env")
    chat_ids = [int(x.strip()) for x in chat_id.split(",") if x.strip()]
    return {
        "token": token,
        "chat_ids": chat_ids,
        "interval_hours": float(os.getenv("POLL_INTERVAL_HOURS", "4")),
    }


def _ensure_display():
    """If no DISPLAY is set, re-exec under xvfb-run so Chromium can open a window."""
    if not os.environ.get("DISPLAY"):
        xvfb = shutil.which("xvfb-run")
        if not xvfb:
            print(
                "ERROR: No DISPLAY found and xvfb-run is not installed.\n"
                "Install it with:  sudo apt-get install xvfb\n"
                "Or log in locally and copy data/cookies.json to this machine."
            )
            sys.exit(1)
        print("No DISPLAY found — re-launching under xvfb-run (virtual framebuffer)...")
        print("To interact with the browser, forward port 9222 via SSH:")
        print("  ssh -L 9222:localhost:9222 youruser@thishost")
        print("Then open Chrome locally and go to:  chrome://inspect")
        print("Click 'inspect' on the AliExpress tab to control the browser.\n")
        os.execvp(xvfb, [xvfb, "--server-args=-screen 0 1280x800x24", sys.executable] + sys.argv + ["--_xvfb"])


async def login_flow(via_xvfb: bool = False):
    from scraper.browser import BrowserManager
    Path("data").mkdir(exist_ok=True)
    extra_args = ["--remote-debugging-port=9222"] if via_xvfb else []
    mgr = BrowserManager(extra_args=extra_args)
    await mgr.start(headless=False)
    if via_xvfb:
        print("\nBrowser launched. Forward port 9222 over SSH and open chrome://inspect in local Chrome.")
        print("Complete the AliExpress Google login, then return here.\n")
    try:
        await mgr.run_login_flow()
    finally:
        await mgr._browser.close()
        await mgr._playwright.stop()


def novnc_login():
    """
    Start Xvfb + Chromium + x11vnc + websockify so the user can log in
    via a browser-based VNC session (noVNC) over an SSH tunnel.

    Usage:
      python main.py --novnc          # on the server
      ssh -L 6080:localhost:6080 ...  # in another terminal
      open http://localhost:6080      # in your local browser
    """
    import subprocess
    import tempfile
    import threading
    import time

    DISPLAY = ":20"
    VNC_PORT = 5900
    NOVNC_PORT = 6080

    # 1. Start Xvfb
    xvfb = subprocess.Popen(
        ["Xvfb", DISPLAY, "-screen", "0", "1280x800x24", "-nolisten", "tcp"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    time.sleep(1)

    # 2. Start x11vnc (no password, localhost only)
    x11vnc = subprocess.Popen(
        ["x11vnc", "-display", DISPLAY, "-forever", "-nopw",
         "-listen", "localhost", "-rfbport", str(VNC_PORT), "-quiet"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    time.sleep(1)

    # 3. Start websockify with bundled noVNC (serves on NOVNC_PORT)
    novnc_html = Path(__file__).parent / "data" / "novnc"
    novnc_html.mkdir(parents=True, exist_ok=True)
    _write_novnc_index(novnc_html, VNC_PORT)

    websockify = subprocess.Popen(
        ["websockify", "--web", str(novnc_html),
         str(NOVNC_PORT), f"localhost:{VNC_PORT}"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    time.sleep(1)

    # 4. Launch Chromium under DISPLAY
    os.environ["DISPLAY"] = DISPLAY

    async def _run_browser():
        from scraper.browser import BrowserManager
        Path("data").mkdir(exist_ok=True)
        mgr = BrowserManager()
        await mgr.start(headless=False)
        try:
            await mgr.run_login_flow()
        finally:
            await mgr.stop()

    print("\n" + "=" * 60)
    print("noVNC login session started.")
    print(f"\n  1. In another terminal, run:")
    print(f"       ssh -L {NOVNC_PORT}:localhost:{NOVNC_PORT} <user>@<this-host>")
    print(f"\n  2. Open in your local browser:")
    print(f"       http://localhost:{NOVNC_PORT}/vnc.html?autoconnect=1&resize=scale")
    print(f"\n  3. Complete the AliExpress / Google login in the browser window.")
    print(f"     Cookies are saved automatically when you reach the orders page.")
    print("=" * 60 + "\n")

    try:
        asyncio.run(_run_browser())
    finally:
        websockify.terminate()
        x11vnc.terminate()
        xvfb.terminate()
        print("Login session ended.")


def _write_novnc_index(directory: Path, vnc_port: int):
    """Write a minimal noVNC HTML page that loads the client from CDN."""
    html = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>AliExpress Login</title>
<style>body,html{{margin:0;padding:0;background:#1a1a1a;overflow:hidden}}</style>
</head>
<body>
<script>
// Redirect to noVNC app hosted via websockify's built-in static server fallback.
// Since we serve from websockify --web, we need noVNC files present.
// Instead, load noVNC from CDN via an iframe approach won't work (mixed content).
// So we embed a minimal websocket VNC client inline.
</script>
<p style="color:white;font-family:sans-serif;padding:20px">
Loading VNC viewer...<br><br>
If this page does not show the desktop, install noVNC:<br>
<code>sudo apt-get install novnc</code><br>
then re-run <code>python main.py --novnc</code>
</p>
</body>
</html>
"""
    # Try to find system noVNC installation
    novnc_paths = [
        Path("/usr/share/novnc"),
        Path("/usr/share/webapps/novnc"),
        Path("/opt/novnc"),
    ]
    for p in novnc_paths:
        if (p / "vnc.html").exists():
            import shutil as _shutil
            # Symlink or copy noVNC files into our directory
            for item in p.iterdir():
                dest = directory / item.name
                if not dest.exists():
                    if item.is_dir():
                        _shutil.copytree(item, dest)
                    else:
                        _shutil.copy2(item, dest)
            return

    # Fallback: write placeholder + instructions
    (directory / "vnc.html").write_text(html)


async def run_bot():
    from aiogram import Bot, Dispatcher
    from aiogram.fsm.storage.memory import MemoryStorage

    from bot.handlers import AppContext, AppContextMiddleware, router, register_commands
    from scheduler.polling import PollingScheduler
    from scraper.aliexpress import AliExpressScraper
    from scraper.browser import BrowserManager
    from state.store import OrderStateStore

    settings = _load_settings()
    Path("data").mkdir(exist_ok=True)
    Path("logs").mkdir(exist_ok=True)

    # Init state store
    store = OrderStateStore()
    await store.init_db()

    # Init browser + scraper (browser not started yet — lazy start on first scrape)
    browser_mgr = BrowserManager()
    await browser_mgr.start(headless=True)
    await browser_mgr.ensure_authenticated()
    scraper = AliExpressScraper(browser_mgr)

    # Init Telegram
    bot = Bot(token=settings["token"])
    dp = Dispatcher(storage=MemoryStorage())
    await register_commands(bot)

    # Build shared context
    ctx = AppContext(
        scraper=scraper,
        store=store,
        bot=bot,
        chat_ids=settings["chat_ids"],
        scrape_lock=asyncio.Lock(),
    )
    dp.update.middleware(AppContextMiddleware(ctx))
    dp.include_router(router)

    # Start scheduler — pass ctx after it's built
    scheduler = PollingScheduler(ctx)
    ctx.scheduler = scheduler
    scheduler.start(interval_hours=settings["interval_hours"])

    # Graceful shutdown
    loop = asyncio.get_event_loop()

    def _stop(sig):
        logger.info("Signal %s received — shutting down", sig)
        loop.create_task(_shutdown(bot, browser_mgr, scheduler))

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _stop, sig)

    logger.info("Bot starting — polling every %.1f hours", settings["interval_hours"])
    try:
        await dp.start_polling(bot)
    finally:
        scheduler.stop()
        await browser_mgr.stop()
        await bot.session.close()


async def _shutdown(bot, browser_mgr, scheduler):
    scheduler.stop()
    await browser_mgr.stop()
    await bot.session.close()


def main():
    parser = argparse.ArgumentParser(description="AliExpress Order Tracker Bot")
    parser.add_argument(
        "--login",
        action="store_true",
        help="Open a headed browser for one-time Google OAuth login and save cookies.",
    )
    parser.add_argument(
        "--novnc",
        action="store_true",
        help="Start a browser-based VNC login session (Xvfb + x11vnc + websockify).",
    )
    parser.add_argument("--_xvfb", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()

    if args.novnc:
        novnc_login()
    elif args.login:
        via_xvfb = getattr(args, "_xvfb", False)
        if not via_xvfb:
            _ensure_display()
        asyncio.run(login_flow(via_xvfb=via_xvfb))
    else:
        asyncio.run(run_bot())


if __name__ == "__main__":
    main()
