import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler

logger = logging.getLogger(__name__)


class PollingScheduler:
    def __init__(self, ctx):
        self._ctx = ctx
        self._scheduler = AsyncIOScheduler()

    def start(self, interval_hours: float) -> None:
        self._scheduler.add_job(
            self._poll_job,
            trigger="interval",
            hours=interval_hours,
            id="aliexpress_poll",
            max_instances=1,
            misfire_grace_time=600,
        )
        self._scheduler.start()
        logger.info("Scheduler started — polling every %.1f hours", interval_hours)

    def stop(self) -> None:
        if self._scheduler.running:
            self._scheduler.shutdown(wait=False)

    def next_run_time(self):
        job = self._scheduler.get_job("aliexpress_poll")
        return job.next_run_time if job else None

    async def _poll_job(self) -> None:
        from bot.notifications import send_change_notification
        from scraper.models import SessionExpiredError, CloudflareBlockError

        logger.info("Scheduled delta poll starting")
        try:
            async with self._ctx.scrape_lock:
                # Delta poll: only page 1 (recent orders)
                orders = await self._ctx.scraper.fetch_orders(full=False)

                # Apply cached recipients so we don't re-fetch detail pages
                known = await self._ctx.store.get_enrichment_cache()
                await self._ctx.scraper.enrich_recipients(orders, known)

                changed = await self._ctx.store.get_changed(orders)
                await self._ctx.store.upsert_all(orders)

            if changed:
                logger.info("Status changes detected: %d orders", len(changed))
                for cid in self._ctx.chat_ids:
                    await send_change_notification(
                        self._ctx.bot, cid, changed, store=self._ctx.store
                    )
            else:
                logger.info("No status changes detected")
        except SessionExpiredError as e:
            logger.warning("Session expired during poll: %s", e)
            for cid in self._ctx.chat_ids:
                await self._ctx.bot.send_message(
                    cid,
                    "⚠️ פג תוקף הסשן.\nשלח /relogin כדי להתחבר מחדש.",
                    parse_mode="HTML",
                )
        except CloudflareBlockError as e:
            logger.warning("Cloudflare block during poll: %s", e)
            for cid in self._ctx.chat_ids:
                await self._ctx.bot.send_message(
                    cid,
                    "⚠️ Cloudflare חסם את הגישה. נסה שוב מאוחר יותר.",
                    parse_mode="HTML",
                )
        except Exception as e:
            logger.exception("Unexpected error during scheduled poll")
            for cid in self._ctx.chat_ids:
                await self._ctx.bot.send_message(cid, f"⚠️ שגיאה בעדכון הזמנות: {e}")
