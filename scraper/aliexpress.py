import logging
import re
from datetime import datetime, timedelta

from scraper.browser import BrowserManager
from scraper.models import Order, CloudflareBlockError

logger = logging.getLogger(__name__)

ORDERS_URL = "https://www.aliexpress.com/p/order/index.html"
DETAIL_URL = "https://www.aliexpress.com/p/order/detail.html?orderId={}"

_MAX_PAGES = 25  # max "View orders" button clicks before giving up
_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}

# JS injected into the orders list page — returns all order data as a plain array.
_JS_EXTRACT_ORDERS = """
() => {
    const items = document.querySelectorAll('.order-item');
    return Array.from(items).map(item => {
        // Order ID
        let orderId = null;
        const detailLink = item.querySelector('a[data-pl="order_item_header_detail"]');
        if (detailLink) {
            const m = detailLink.href.match(/orderId=(\\d+)/);
            if (m) orderId = m[1];
        }
        if (!orderId) {
            const walker = document.createTreeWalker(item, NodeFilter.SHOW_TEXT);
            let node;
            while ((node = walker.nextNode())) {
                const m = node.textContent.match(/Order ID:\\s*(\\d+)/);
                if (m) { orderId = m[1]; break; }
            }
        }
        if (!orderId) return null;

        // Status
        const statusEl = item.querySelector('.order-item-header-status-text');
        const status = statusEl ? statusEl.textContent.trim() : 'Unknown';

        // Order date (text node like "Order date: Apr 27, 2026")
        let orderDate = null;
        {
            const walker = document.createTreeWalker(item, NodeFilter.SHOW_TEXT);
            let node;
            while ((node = walker.nextNode())) {
                const m = node.textContent.match(/Order date:\\s*(.+)/);
                if (m) { orderDate = m[1].trim(); break; }
            }
        }

        // Item name
        let itemName = 'Unknown item';
        const nameEl = item.querySelector('.order-item-content-info-name');
        if (nameEl) {
            const span = nameEl.querySelector('[title]');
            itemName = span ? span.title : nameEl.textContent.trim();
        }

        // Seller
        const sellerEl = item.querySelector('.order-item-store-name');
        const seller = sellerEl ? sellerEl.textContent.trim() : null;

        // Thumbnail (background-image on the content-img div)
        let thumbnailUrl = null;
        const imgEl = item.querySelector('.order-item-content-img');
        if (imgEl && imgEl.style.backgroundImage) {
            const m = imgEl.style.backgroundImage.match(/url\\(["']?([^"')]+)["']?\\)/);
            if (m) thumbnailUrl = m[1];
        }

        // Tracking number from tracking link
        let trackingNumber = null;
        const trackLink = item.querySelector('a[href*="/tracking/"]');
        if (trackLink) {
            const m = trackLink.href.match(/logisticsNo=([^&]+)/);
            if (m) trackingNumber = decodeURIComponent(m[1]);
        }

        return { orderId, status, orderDate, itemName, seller, thumbnailUrl, trackingNumber };
    }).filter(Boolean);
}
"""

# JS injected into the order detail page — returns recipient, sub_status, item_name.
_JS_EXTRACT_DETAIL = """
() => {
    // Recipient: first text node in .contact-info
    let recipient = null;
    const contactEl = document.querySelector('.contact-info');
    if (contactEl) {
        for (const node of contactEl.childNodes) {
            if (node.nodeType === Node.TEXT_NODE && node.textContent.trim()) {
                recipient = node.textContent.trim();
                break;
            }
        }
        if (!recipient) {
            const texts = Array.from(contactEl.querySelectorAll('*'))
                .map(el => el.textContent.trim()).filter(Boolean);
            recipient = texts[0] || null;
        }
    }

    // Sub-status: last .order-detail-item-track-info-desc (most recent event)
    let subStatus = null;
    const trackEls = document.querySelectorAll('.order-detail-item-track-info-desc');
    if (trackEls.length) {
        subStatus = trackEls[trackEls.length - 1].textContent.trim() || null;
    }

    // Item name fallback for orders with no name on list page
    const SKIP = ['return', 'refund', 'policy', 'commitment', 'coin', 'love'];
    let itemName = null;
    for (const el of document.querySelectorAll('.item-title')) {
        const text = el.textContent.trim();
        const lower = text.toLowerCase();
        if (text.length > 15 && !SKIP.some(s => lower.includes(s))) {
            itemName = text.slice(0, 200);
            break;
        }
    }

    return { recipient, subStatus, itemName };
}
"""


def _parse_date(text: str) -> datetime | None:
    """Parse 'Apr 27, 2026' or 'Apr 27 2026' style dates."""
    text = text.strip()
    m = re.match(r"([A-Za-z]+)\s+(\d+),?\s+(\d{4})", text)
    if not m:
        return None
    month = _MONTHS.get(m.group(1).lower())
    if not month:
        return None
    return datetime(int(m.group(3)), month, int(m.group(2)))


class AliExpressScraper:
    def __init__(self, browser: BrowserManager):
        self._browser = browser

    async def fetch_orders(self, full: bool = False) -> list[Order]:
        """
        full=True  → load all orders by clicking "View orders" until exhausted.
        full=False → first 10 orders only (fast delta poll).
        """
        cutoff = datetime.utcnow() - timedelta(days=92)
        orders = await self._fetch_all_orders(full=full, cutoff=cutoff)
        logger.info("Fetched %d orders (%s)", len(orders), "full history" if full else "delta page 1")
        return orders

    async def enrich_recipients(
        self, orders: list[Order], known: dict[str, dict], fetch_completed: bool = False
    ) -> None:
        """Fetch detail pages only for orders missing data not available on the list page.
        known = {order_id: {recipient, sub_status, item_name}} from store.get_enrichment_cache().
        """
        to_fetch = []
        for order in orders:
            cached = known.get(order.order_id, {})

            # Fill from cache first
            if not order.recipient and cached.get("recipient"):
                order.recipient = cached["recipient"]
            if not order.sub_status and cached.get("sub_status"):
                order.sub_status = cached["sub_status"]
            if order.item_name == "Unknown item" and cached.get("item_name") and cached["item_name"] != "Unknown item":
                order.item_name = cached["item_name"]

            # Only fetch detail page if something is still missing
            still_missing = (
                not order.recipient
                or not order.sub_status
                or order.item_name == "Unknown item"
            )
            if not still_missing:
                continue

            if order.status.lower() == "completed" and not fetch_completed:
                continue

            to_fetch.append(order)

        logger.info("Enriching %d/%d orders from detail pages", len(to_fetch), len(orders))
        for order in to_fetch:
            recipient, sub_status, item_name = await self._fetch_detail(order.order_id)
            if recipient:
                order.recipient = recipient
            if sub_status:
                order.sub_status = sub_status
            if item_name and order.item_name == "Unknown item":
                order.item_name = item_name

    async def _fetch_all_orders(self, full: bool, cutoff: datetime) -> list[Order]:
        page = await self._browser.new_page()
        try:
            await page.goto(ORDERS_URL, wait_until="load", timeout=90_000)

            landed = page.url
            if "passport" in landed or "login" in landed:
                from scraper.models import SessionExpiredError
                raise SessionExpiredError("Redirected to login — session expired.")

            title = await page.title()
            if "just a moment" in title.lower():
                raise CloudflareBlockError("Cloudflare challenge encountered.")

            if full:
                prev_count = 0
                for _ in range(_MAX_PAGES):
                    count = await page.evaluate(
                        "() => document.querySelectorAll('.order-item').length"
                    )
                    if count == prev_count:
                        break
                    prev_count = count

                    # Stop early if oldest visible order is past cutoff
                    raw = await page.evaluate(_JS_EXTRACT_ORDERS)
                    dates = [_parse_date(r["orderDate"]) for r in raw if r.get("orderDate")]
                    if dates and min(d for d in dates if d) < cutoff:
                        break

                    btn = page.locator(".order-more button")
                    if await btn.count() == 0:
                        break
                    await btn.scroll_into_view_if_needed()
                    await page.wait_for_timeout(300)
                    await btn.click()
                    await page.wait_for_timeout(1500)

            raw_orders = await page.evaluate(_JS_EXTRACT_ORDERS)
        finally:
            await page.close()

        _SKIP = {"cancelled", "canceled", "closed"}
        orders = []
        for r in raw_orders:
            if r["status"].lower() in _SKIP:
                continue
            order_date = _parse_date(r["orderDate"]) if r.get("orderDate") else None
            if full and order_date and order_date < cutoff:
                continue
            order_id = r["orderId"]
            orders.append(Order(
                order_id=order_id,
                item_name=(r["itemName"] or "Unknown item")[:200],
                status=r["status"],
                order_url=f"https://www.aliexpress.com/p/order/detail.html?orderId={order_id}",
                order_date=order_date,
                tracking_number=r.get("trackingNumber"),
                seller=r.get("seller"),
                thumbnail_url=r.get("thumbnailUrl"),
                last_seen=datetime.utcnow(),
            ))
        return orders

    async def _fetch_detail(self, order_id: str) -> tuple[str | None, str | None, str | None]:
        """Fetch order detail page and extract (recipient, sub_status, item_name) via JS."""
        page = await self._browser.new_page()
        try:
            await page.goto(DETAIL_URL.format(order_id), wait_until="load", timeout=60_000)
            result = await page.evaluate(_JS_EXTRACT_DETAIL)
        except Exception as e:
            logger.debug("Could not fetch detail for %s: %s", order_id, e)
            return None, None, None
        finally:
            await page.close()

        return result.get("recipient"), result.get("subStatus"), result.get("itemName")
