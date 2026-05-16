import logging
import re
from datetime import datetime, timedelta

from bs4 import BeautifulSoup, Tag

from scraper.browser import BrowserManager
from scraper.models import Order, CloudflareBlockError

logger = logging.getLogger(__name__)

ORDERS_URL = "https://www.aliexpress.com/p/order/index.html"
DETAIL_URL = "https://www.aliexpress.com/p/order/detail.html?orderId={}"
_ORDER_ID_RE = re.compile(r"orderId=(\d+)")
_DATE_RE = re.compile(r"Order date:\s*(.+)")
_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}
_MAX_PAGES = 25  # max "View orders" button clicks before giving up


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
        self, orders: list[Order], known: dict[str, str | None], fetch_completed: bool = False
    ) -> None:
        """Fetch recipients from detail pages for orders not yet in DB. Mutates orders in place."""
        for order in orders:
            needs_detail = (
                order.recipient is None
                or order.sub_status is None
                or order.item_name == "Unknown item"
            )
            if not needs_detail:
                if order.order_id in known and known[order.order_id]:
                    order.recipient = known[order.order_id]
                continue

            if order.order_id in known and known[order.order_id] and order.item_name != "Unknown item":
                order.recipient = known[order.order_id]
                if order.sub_status is not None:
                    continue

            if order.status.lower() == "completed" and not fetch_completed:
                if order.order_id in known and known[order.order_id]:
                    order.recipient = known[order.order_id]
                continue

            recipient, sub_status, item_name = await self._fetch_recipient(order.order_id)
            if recipient:
                order.recipient = recipient
            if sub_status:
                order.sub_status = sub_status
            if item_name and order.item_name == "Unknown item":
                order.item_name = item_name

    async def _fetch_all_orders(self, full: bool, cutoff: datetime) -> list[Order]:
        """
        Load the orders page and click 'View orders' until all are rendered (full=True)
        or just parse the initial 10 (full=False).
        """
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
                # Click "View orders" until all orders are loaded or cutoff reached
                prev_count = 0
                for _ in range(_MAX_PAGES):
                    count = await page.evaluate(
                        "() => document.querySelectorAll('.order-item').length"
                    )
                    if count == prev_count:
                        break
                    prev_count = count

                    # Check if oldest visible order is past cutoff
                    html_now = await page.content()
                    orders_so_far = self._parse_html(html_now)
                    dates = [o.order_date for o in orders_so_far if o.order_date]
                    if dates and min(dates) < cutoff:
                        break

                    btn = page.locator(".order-more button")
                    if await btn.count() == 0:
                        break
                    await btn.scroll_into_view_if_needed()
                    await page.wait_for_timeout(300)
                    await btn.click()
                    await page.wait_for_timeout(1500)

            html = await page.content()
        finally:
            await page.close()

        _SKIP = {"cancelled", "canceled", "closed"}
        orders = [o for o in self._parse_html(html) if o.status.lower() not in _SKIP]
        if full:
            orders = [o for o in orders if o.order_date is None or o.order_date >= cutoff]
        return orders

    async def _fetch_recipient(self, order_id: str) -> tuple[str | None, str | None, str | None]:
        """Fetch order detail page and extract (recipient, sub_status, item_name)."""
        page = await self._browser.new_page()
        try:
            await page.goto(DETAIL_URL.format(order_id), wait_until="load", timeout=60_000)
            html = await page.content()
        except Exception as e:
            logger.debug("Could not fetch detail for %s: %s", order_id, e)
            return None, None, None
        finally:
            await page.close()

        soup = BeautifulSoup(html, "html.parser")

        # Recipient: contact-info div — first text node is the name
        recipient = None
        contact = soup.find("div", class_="contact-info")
        if contact:
            name = contact.find(string=True, recursive=False)
            if name:
                name = name.strip()
                if name:
                    recipient = name
            if not recipient:
                texts = [t.strip() for t in contact.stripped_strings]
                if texts:
                    recipient = texts[0]

        # Sub-status: last tracking event (most recent) on detail page
        sub_status = None
        tracks = soup.find_all("div", class_="order-detail-item-track-info-desc")
        if tracks:
            text = tracks[-1].get_text(strip=True)
            if text:
                sub_status = text

        # Item name fallback: detail page item-title (for orders missing name on list page)
        _TITLE_SKIP = {"return", "refund", "policy", "commitment", "coin", "love"}
        item_name = None
        for title_el in soup.find_all(class_="item-title"):
            text = title_el.get_text(strip=True)
            lower = text.lower()
            if len(text) > 15 and not any(s in lower for s in _TITLE_SKIP):
                item_name = text[:200]
                break

        return recipient, sub_status, item_name

    def _parse_html(self, html: str) -> list[Order]:
        soup = BeautifulSoup(html, "html.parser")
        items = soup.find_all("div", class_="order-item")
        orders = []
        for item in items:
            order = self._parse_item(item)
            if order:
                orders.append(order)
        return orders

    def _parse_item(self, item: Tag) -> Order | None:
        try:
            # Order ID
            order_id = None
            detail_link = item.find("a", attrs={"data-pl": "order_item_header_detail"})
            if detail_link and detail_link.get("href"):
                m = _ORDER_ID_RE.search(detail_link["href"])
                if m:
                    order_id = m.group(1)
            if not order_id:
                for text in item.stripped_strings:
                    m = re.search(r"Order ID:\s*(\d+)", text)
                    if m:
                        order_id = m.group(1)
                        break
            if not order_id:
                return None

            # Status
            status_el = item.find("span", class_="order-item-header-status-text")
            status = status_el.get_text(strip=True) if status_el else "Unknown"

            # Order date
            order_date = None
            for text in item.stripped_strings:
                m = _DATE_RE.match(text)
                if m:
                    order_date = _parse_date(m.group(1))
                    break

            # Item name
            item_name = "Unknown item"
            name_el = item.find("div", class_="order-item-content-info-name")
            if name_el:
                span = name_el.find("span", title=True)
                item_name = span["title"] if span else name_el.get_text(strip=True)

            # Seller
            seller_el = item.find("span", class_="order-item-store-name")
            seller = seller_el.get_text(strip=True) if seller_el else None

            # Thumbnail
            thumbnail_url = None
            img_el = item.find("div", class_="order-item-content-img")
            if img_el and img_el.get("style"):
                m = re.search(r'url\(["\']?([^"\')\s]+)["\']?\)', img_el["style"])
                if m:
                    thumbnail_url = m.group(1)

            # Tracking number from track link
            tracking_number = None
            track_link = item.find("a", href=re.compile(r"/tracking/"))
            if track_link and track_link.get("href"):
                m = re.search(r"logisticsNo=([^&]+)", track_link["href"])
                if m:
                    tracking_number = m.group(1)

            order_url = f"https://www.aliexpress.com/p/order/detail.html?orderId={order_id}"

            return Order(
                order_id=order_id,
                item_name=item_name[:200],
                status=status,
                order_url=order_url,
                order_date=order_date,
                tracking_number=tracking_number,
                seller=seller,
                thumbnail_url=thumbnail_url,
                last_seen=datetime.utcnow(),
            )
        except Exception as e:
            logger.warning("Failed to parse order item: %s", e)
            return None
