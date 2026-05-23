import asyncio
import logging
import os
import signal
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




async def run_bot():
    from aiogram import Bot, Dispatcher
    from aiogram.fsm.storage.memory import MemoryStorage

    from bot.handlers import AppContext, AppContextMiddleware, WhitelistMiddleware, router, register_commands
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
    dp.update.middleware(WhitelistMiddleware(settings["chat_ids"]))
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
    asyncio.run(run_bot())


if __name__ == "__main__":
    main()
