import html
from datetime import datetime

from aiogram import Bot

from bot.translator import translate_status
from scraper.models import Order, STATUS_GROUPS, is_en_route, status_priority, status_group_label


def _esc(text: str | None) -> str:
    return html.escape(str(text or ""))


def format_order(order: Order, highlight: str | None = None, item_name_he: str | None = None) -> str:
    lines = []
    status_he = translate_status(order.status)

    if highlight == "NEW":
        lines.append("🆕 <b>New order</b>")
    elif highlight:
        old_he = translate_status(highlight)
        lines.append(f"🔄 <b>Status changed:</b> {_esc(old_he)} ← <b>{_esc(status_he)}</b>")

    display_name = item_name_he or order.item_name
    lines.append(f"📦 <b>{_esc(display_name)}</b>")
    lines.append(f"Status: <b>{_esc(status_he)}</b>")
    if order.sub_status:
        lines.append(f"📍 {_esc(order.sub_status)}")

    if order.recipient:
        lines.append(f"Recipient: {_esc(order.recipient)}")
    if order.order_date:
        lines.append(f"Order date: {order.order_date.strftime('%d/%m/%Y')}")
    if order.completed_at:
        lines.append(f"Completed: {order.completed_at.strftime('%d/%m/%Y')}")
    if order.tracking_number:
        lines.append(f"Tracking: <code>{_esc(order.tracking_number)}</code>")
    if order.estimated_delivery:
        lines.append(f"Est. delivery: {_esc(order.estimated_delivery)}")
    if order.seller:
        lines.append(f"Seller: {_esc(order.seller)}")

    lines.append(f'<a href="{_esc(order.order_url)}">Order #{_esc(order.order_id)}</a>')
    return "\n".join(lines)


async def translate_orders(orders: list[Order], store) -> dict[str, str]:
    """Returns {order_id: hebrew_item_name} for all orders."""
    from bot.translator import translate_item_name
    result = {}
    for order in orders:
        result[order.order_id] = await translate_item_name(order.item_name, store)
    return result


def sort_orders(orders: list[Order]) -> list[Order]:
    return sorted(orders, key=lambda o: status_priority(o.status))


def format_order_list(
    orders: list[Order],
    changed: list[tuple[Order, str]] | None = None,
    translations: dict[str, str] | None = None,
    recipient_filter: str | None = None,
    sort_by_recipient: bool = False,
) -> str:
    changed_ids = {o.order_id: old for o, old in changed} if changed else {}

    if recipient_filter:
        orders = [o for o in orders
                  if o.recipient and recipient_filter.lower() in o.recipient.lower()]

    if sort_by_recipient:
        orders = sorted(orders, key=lambda o: (o.recipient or "ת", status_priority(o.status)))
    else:
        orders = sort_orders(orders)

    if not orders:
        msg = "No orders found"
        if recipient_filter:
            msg += f" for '{recipient_filter}'"
        return msg

    parts = []
    for order in orders:
        old_status = changed_ids.get(order.order_id)
        item_he = (translations or {}).get(order.order_id)
        parts.append(format_order(order, highlight=old_status, item_name_he=item_he))

    updates_text = f" — {len(changed)} update(s)" if changed else ""
    header = f"<b>Orders ({len(orders)}){updates_text}</b>"
    return header + "\n\n" + "\n\n─────────────\n\n".join(parts)


def format_active_grouped(
    orders: list[Order],
    translations: dict[str, str] | None = None,
    recipient_filter: str | None = None,
    group_by_recipient: bool = False,
) -> str:
    if recipient_filter:
        orders = [o for o in orders if o.recipient and recipient_filter.lower() in o.recipient.lower()]

    active = [o for o in orders if o.status.lower() not in {"completed", "cancelled", "closed"}]
    active = sort_orders(active)

    if not active:
        msg = "No active orders"
        if recipient_filter:
            msg += f" for '{recipient_filter}'"
        return msg

    title = f"<b>Active orders ({len(active)})</b>"

    if group_by_recipient:
        sections: dict[str, list[Order]] = {}
        for o in active:
            key = o.recipient or "No recipient"
            sections.setdefault(key, []).append(o)
        parts = []
        for label in sorted(sections):
            group_orders = sections[label]
            header = f"<b>👤 {_esc(label)} ({len(group_orders)})</b>"
            cards = "\n\n".join(
                format_order(o, item_name_he=(translations or {}).get(o.order_id))
                for o in group_orders
            )
            parts.append(f"{header}\n\n{cards}")
        return title + "\n\n" + "\n\n━━━━━━━━━━━━━\n\n".join(parts)

    # Group by status section
    sections = {}
    for label, statuses in STATUS_GROUPS:
        if label in {"✅ Completed", "❌ Cancelled"}:
            continue
        matching = [o for o in active if o.status.lower() in statuses]
        if matching:
            sections[label] = matching

    grouped_statuses = {s for _, grp in STATUS_GROUPS for s in grp}
    others = [o for o in active if o.status.lower() not in grouped_statuses]
    if others:
        sections["❓ Other"] = others

    parts = []
    for label, group_orders in sections.items():
        header = f"<b>{label} ({len(group_orders)})</b>"
        cards = "\n\n".join(
            format_order(o, item_name_he=(translations or {}).get(o.order_id))
            for o in group_orders
        )
        parts.append(f"{header}\n\n{cards}")

    return title + "\n\n" + "\n\n━━━━━━━━━━━━━\n\n".join(parts)


async def send_change_notification(
    bot: Bot,
    chat_id: int | str,
    changed: list[tuple[Order, str]],
    store=None,
) -> None:
    from bot.translator import translate_item_name
    # Only notify for en-route status changes
    en_route_changes = [
        (order, old) for order, old in changed
        if is_en_route(order.status)
    ]
    for order, old_status in en_route_changes:
        item_he = await translate_item_name(order.item_name, store) if store else None
        text = format_order(order, highlight=old_status, item_name_he=item_he)
        await bot.send_message(chat_id, text, parse_mode="HTML", disable_web_page_preview=True)
