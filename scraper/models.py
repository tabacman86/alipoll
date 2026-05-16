from dataclasses import dataclass, field
from datetime import datetime


# Statuses considered "en route" — trigger change notifications
EN_ROUTE_STATUSES = {
    "awaiting delivery",
    "shipped",
    "in transit",
    "on the way",
    "packed",
    "dispatched",
    "out for delivery",
    "in customs",
    "delivering",
    "arrived at destination",
    "processing",
    "ready to ship",
    "delivered",
}

# Display sort order (lower = shown first)
STATUS_PRIORITY: dict[str, int] = {
    "awaiting delivery": 0,
    "out for delivery": 1,
    "delivered": 1,
    "arrived at destination": 2,
    "in customs": 3,
    "in transit": 4,
    "shipped": 5,
    "on the way": 5,
    "delivering": 5,
    "dispatched": 6,
    "packed": 7,
    "ready to ship": 7,
    "processing": 8,
    "payment successful": 9,
    "payment pending": 10,
    "completed": 11,
    "cancelled": 12,
    "closed": 12,
}

# Status groups for display in /active — order determines section order
STATUS_GROUPS: list[tuple[str, set[str]]] = [
    ("🚚 אצל שליח",      {"awaiting delivery", "out for delivery", "arrived at destination", "delivered"}),
    ("🛃 במכס",           {"in customs"}),
    ("✈️ בדרך",           {"in transit", "shipped", "on the way", "delivering", "dispatched"}),
    ("📦 בהכנה",          {"packed", "processing", "ready to ship"}),
    ("💳 ממתין לתשלום",   {"payment successful", "payment pending"}),
    ("✅ הושלם",          {"completed"}),
    ("❌ בוטל",           {"cancelled", "closed"}),
]


def status_priority(status: str) -> int:
    return STATUS_PRIORITY.get(status.lower(), 8)


def status_group_label(status: str) -> str:
    s = status.lower()
    for label, statuses in STATUS_GROUPS:
        if s in statuses:
            return label
    return "❓ אחר"


def is_en_route(status: str) -> bool:
    return status.lower() in EN_ROUTE_STATUSES


@dataclass
class Order:
    order_id: str
    item_name: str
    status: str
    order_url: str
    order_date: datetime | None = None
    tracking_number: str | None = None
    estimated_delivery: str | None = None
    seller: str | None = None
    recipient: str | None = None
    sub_status: str | None = None
    thumbnail_url: str | None = None
    last_seen: datetime = field(default_factory=datetime.utcnow)


class SessionExpiredError(Exception):
    pass


class CloudflareBlockError(Exception):
    pass
