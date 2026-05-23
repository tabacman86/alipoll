import asyncio
import logging
from datetime import datetime, timezone

from aiogram import Router, BaseMiddleware, Bot, F
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    Message, BotCommand,
    InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery,
    ReplyKeyboardRemove,
)

from bot.notifications import format_active_grouped, format_order_list, translate_orders
from scraper.models import SessionExpiredError, CloudflareBlockError

logger = logging.getLogger(__name__)
router = Router()


class RecipientFilter(StatesGroup):
    waiting_for_active = State()
    waiting_for_cached = State()

BOT_COMMANDS = [
    BotCommand(command="update",  description="עדכן הזמנות מ-AliExpress"),
    BotCommand(command="active",  description="הזמנות פעילות — לפי שלב או נמען"),
    BotCommand(command="cached",  description="כל ההזמנות מהמסד המקומי"),
    BotCommand(command="status",  description="סטטוס הבוט"),
    BotCommand(command="relogin", description="התחברות מחדש ל-AliExpress דרך הדפדפן"),
]


async def register_commands(bot: Bot) -> None:
    from aiogram.types import BotCommandScopeAllPrivateChats
    await bot.set_my_commands(BOT_COMMANDS, scope=BotCommandScopeAllPrivateChats())


class AppContext:
    def __init__(self, scraper, store, bot, chat_ids: list[int], scrape_lock, scheduler=None):
        self.scraper = scraper
        self.store = store
        self.bot = bot
        self.chat_ids = chat_ids
        self.scrape_lock = scrape_lock
        self.scheduler = scheduler
        self.last_poll: datetime | None = None
        self.relogin_in_progress: bool = False


class AppContextMiddleware(BaseMiddleware):
    def __init__(self, ctx: AppContext):
        self._ctx = ctx

    async def __call__(self, handler, event, data):
        data["ctx"] = self._ctx
        return await handler(event, data)


class WhitelistMiddleware(BaseMiddleware):
    def __init__(self, allowed_chat_ids: list[int]):
        self._allowed = set(allowed_chat_ids)

    async def __call__(self, handler, event, data):
        # event is an aiogram Update object when registered on dp.update
        chat_id = None
        if event.message:
            chat_id = event.message.chat.id
        elif event.callback_query and event.callback_query.message:
            chat_id = event.callback_query.message.chat.id

        if chat_id is not None and chat_id not in self._allowed:
            logger.warning("Blocked unauthorized chat_id=%s", chat_id)
            return
        return await handler(event, data)


@router.message(Command("start"))
async def cmd_start(message: Message, ctx: AppContext):
    await message.answer(
        "<b>מעקב הזמנות AliExpress</b>\n\n"
        "פקודות:\n"
        "/update — עדכן מ-AliExpress ושמור ב-DB\n"
        "/active — הזמנות פעילות לפי שלב\n"
        "/active שם — פעילות + סינון נמען\n"
        "/cached — כל ההזמנות מהמסד המקומי\n"
        "/cached שם — מסד מקומי + סינון נמען\n"
        "/status — סטטוס הבוט\n"
        "/login — הוראות התחברות מחדש",
        parse_mode="HTML",
    )


@router.message(Command("update"))
async def cmd_update(message: Message, ctx: AppContext):
    """Scrape AliExpress, sync DB, report changes."""
    await message.answer("מעדכן הזמנות מ-AliExpress, רגע…")
    try:
        async with ctx.scrape_lock:
            orders = await ctx.scraper.fetch_orders(full=True)
            known = await ctx.store.get_recipients()
            await ctx.scraper.enrich_recipients(orders, known, fetch_completed=True)
            changed = await ctx.store.get_changed(orders)
            await ctx.store.upsert_all(orders)
        ctx.last_poll = datetime.now(timezone.utc)

        if changed:
            new_count = sum(1 for _, old in changed if old == "NEW")
            updated_count = len(changed) - new_count
            parts = []
            if new_count:
                parts.append(f"{new_count} הזמנות חדשות")
            if updated_count:
                parts.append(f"{updated_count} עדכוני סטטוס")
            await message.answer(
                f"✅ עודכן — {len(orders)} הזמנות נסרקו, {', '.join(parts)}.\n"
                "השתמש ב-/active כדי לראות הזמנות פעילות.",
            )
        else:
            await message.answer(
                f"✅ עודכן — {len(orders)} הזמנות נסרקו, אין שינויים.\n"
                "השתמש ב-/active כדי לראות הזמנות פעילות.",
            )
    except SessionExpiredError:
        await message.answer(
            "פג תוקף הסשן. הרץ <code>python main.py --login</code> כדי להתחבר מחדש.",
            parse_mode="HTML",
        )
    except CloudflareBlockError:
        await message.answer("Cloudflare חסם את הגישה. נסה שוב בעוד כמה דקות.")
    except Exception as e:
        logger.exception("Error in /update handler")
        await message.answer(f"שגיאה בעדכון הזמנות: {e}")


_RECIPIENT_KEYWORDS = {"recipient", "recipients", "נמען", "נמענים"}


_ACTIVE_KEYBOARD = InlineKeyboardMarkup(inline_keyboard=[[
    InlineKeyboardButton(text="📦 לפי שלב",     callback_data="active:status"),
    InlineKeyboardButton(text="👤 לפי נמען", callback_data="active:recipient"),
]])


@router.message(Command("active", "byrecipient"))
async def cmd_active(message: Message, ctx: AppContext):
    """Ask how to group active orders."""
    text = message.text or ""
    parts = text.split(maxsplit=1)
    arg = parts[1].strip() if len(parts) > 1 else None

    # Direct recipient filter: /active שם
    if arg and arg.lower() not in _RECIPIENT_KEYWORDS:
        rows = await ctx.store.get_active()
        if not rows:
            await message.answer("אין הזמנות במסד הנתונים המקומי.")
            return
        orders = _rows_to_orders(rows)
        translations = await translate_orders(orders, ctx.store)
        reply = format_active_grouped(orders, translations=translations, recipient_filter=arg)
        for chunk in _split_message(reply, 4096):
            await message.answer(chunk, parse_mode="HTML", disable_web_page_preview=True)
        return

    await message.answer("כיצד לקבץ את ההזמנות הפעילות?", reply_markup=_ACTIVE_KEYBOARD)


@router.callback_query(F.data.startswith("active:"))
async def cb_active(call: CallbackQuery, ctx: AppContext, state: FSMContext):
    if call.data == "active:recipient":
        await call.message.edit_text("הכנס שם נמען לסינון:")
        await state.set_state(RecipientFilter.waiting_for_active)
        await call.answer()
        return

    await call.message.edit_text("טוען הזמנות…")
    rows = await ctx.store.get_active()
    if not rows:
        await call.message.edit_text("אין הזמנות במסד הנתונים המקומי.")
        return

    orders = _rows_to_orders(rows)
    translations = await translate_orders(orders, ctx.store)
    reply = format_active_grouped(orders, translations=translations)

    chunks = _split_message(reply, 4096)
    await call.message.edit_text(chunks[0], parse_mode="HTML", disable_web_page_preview=True)
    for chunk in chunks[1:]:
        await call.message.answer(chunk, parse_mode="HTML", disable_web_page_preview=True)
    await call.answer()


@router.message(StateFilter(RecipientFilter.waiting_for_active))
async def cb_active_recipient_name(message: Message, ctx: AppContext, state: FSMContext):
    name = (message.text or "").strip()
    await state.clear()
    if not name:
        await message.answer("לא הוזן שם.")
        return

    rows = await ctx.store.get_active()
    orders = _rows_to_orders(rows) if rows else []
    translations = await translate_orders(orders, ctx.store)
    reply = format_active_grouped(orders, translations=translations, recipient_filter=name)
    for chunk in _split_message(reply, 4096):
        await message.answer(chunk, parse_mode="HTML", disable_web_page_preview=True)


_CACHED_KEYBOARD = InlineKeyboardMarkup(inline_keyboard=[[
    InlineKeyboardButton(text="📦 לפי סטטוס",  callback_data="cached:status"),
    InlineKeyboardButton(text="👤 לפי נמען",   callback_data="cached:recipient"),
]])


@router.message(Command("cached"))
async def cmd_cached(message: Message, ctx: AppContext):
    """Ask how to sort all orders."""
    text = message.text or ""
    parts = text.split(maxsplit=1)
    arg = parts[1].strip() if len(parts) > 1 else None

    # Direct recipient filter: /cached שם
    if arg and arg.lower() not in _RECIPIENT_KEYWORDS:
        rows = await ctx.store.get_cached()
        if not rows:
            await message.answer("אין הזמנות במסד הנתונים המקומי.")
            return
        orders = _rows_to_orders(rows)
        translations = await translate_orders(orders, ctx.store)
        reply = format_order_list(orders, translations=translations, recipient_filter=arg)
        for chunk in _split_message(reply, 4096):
            await message.answer(chunk, parse_mode="HTML", disable_web_page_preview=True)
        return

    await message.answer("כיצד למיין את ההזמנות?", reply_markup=_CACHED_KEYBOARD)


@router.callback_query(F.data.startswith("cached:"))
async def cb_cached(call: CallbackQuery, ctx: AppContext, state: FSMContext):
    if call.data == "cached:recipient":
        await call.message.edit_text("הכנס שם נמען לסינון:")
        await state.set_state(RecipientFilter.waiting_for_cached)
        await call.answer()
        return

    await call.message.edit_text("טוען הזמנות…")
    rows = await ctx.store.get_cached()
    if not rows:
        await call.message.edit_text("אין הזמנות במסד הנתונים המקומי.")
        return

    orders = _rows_to_orders(rows)
    translations = await translate_orders(orders, ctx.store)
    reply = format_order_list(orders, translations=translations)

    chunks = _split_message(reply, 4096)
    await call.message.edit_text(chunks[0], parse_mode="HTML", disable_web_page_preview=True)
    for chunk in chunks[1:]:
        await call.message.answer(chunk, parse_mode="HTML", disable_web_page_preview=True)
    await call.answer()


@router.message(StateFilter(RecipientFilter.waiting_for_cached))
async def cb_cached_recipient_name(message: Message, ctx: AppContext, state: FSMContext):
    name = (message.text or "").strip()
    await state.clear()
    if not name:
        await message.answer("לא הוזן שם.")
        return

    rows = await ctx.store.get_cached()
    orders = _rows_to_orders(rows) if rows else []
    translations = await translate_orders(orders, ctx.store)
    reply = format_order_list(orders, translations=translations, recipient_filter=name)
    for chunk in _split_message(reply, 4096):
        await message.answer(chunk, parse_mode="HTML", disable_web_page_preview=True)


@router.message(Command("status"))
async def cmd_status(message: Message, ctx: AppContext):
    order_count = await ctx.store.count()
    last = ctx.last_poll.strftime("%d/%m/%Y %H:%M UTC") if ctx.last_poll else "לא בוצע"

    next_run = "לא ידוע"
    if ctx.scheduler:
        nrt = ctx.scheduler.next_run_time()
        if nrt:
            next_run = nrt.strftime("%d/%m/%Y %H:%M UTC")

    lines = [
        "<b>סטטוס הבוט</b>",
        f"הזמנות במסד הנתונים: {order_count}",
        f"עדכון אחרון: {last}",
        f"עדכון הבא: {next_run}",
    ]
    await message.answer("\n".join(lines), parse_mode="HTML")


@router.message(Command("relogin"))
async def cmd_relogin(message: Message, ctx: AppContext):
    if ctx.relogin_in_progress:
        await message.answer("⚠️ תהליך התחברות כבר פועל.")
        return

    ctx.relogin_in_progress = True
    status_msg = await message.answer("⏳ מפעיל סשן VNC...")

    async def _run():
        import asyncio
        from scraper.browser import BrowserManager, NoVNCStack

        try:
            # Start VNC stack in a thread (has blocking sleeps)
            vnc = await asyncio.to_thread(NoVNCStack().__enter__)

            await ctx.bot.edit_message_text(
                chat_id=message.chat.id,
                message_id=status_msg.message_id,
                text=(
                    "🖥 <b>סשן VNC פעיל</b>\n\n"
                    "1. פתח מנהרת SSH:\n"
                    "<code>ssh -L 6080:localhost:6080 user@your-server</code>\n\n"
                    "2. פתח בדפדפן שלך:\n"
                    "<code>http://localhost:6080/vnc.html?autoconnect=1&resize=scale</code>\n\n"
                    "3. השלם את ההתחברות ל-AliExpress.\n"
                    "הבוט יאשר אוטומטית כשהכניסה תצליח."
                ),
                parse_mode="HTML",
            )

            # Run headed login browser
            login_mgr = BrowserManager()
            await login_mgr.start(headless=False)
            try:
                await login_mgr.run_login_flow(use_stdin=False)
            finally:
                await login_mgr.stop()

            # Tear down VNC
            await asyncio.to_thread(vnc.__exit__, None, None, None)

            # Reload cookies into the running bot's browser context
            await ctx.scraper._browser.reload_cookies()

            await ctx.bot.edit_message_text(
                chat_id=message.chat.id,
                message_id=status_msg.message_id,
                text="✅ ההתחברות הצליחה! הבוט ממשיך לפעול.",
            )

        except Exception as e:
            logger.error("relogin failed: %s", e)
            await ctx.bot.edit_message_text(
                chat_id=message.chat.id,
                message_id=status_msg.message_id,
                text=f"❌ שגיאה בהתחברות: {e}",
            )
        finally:
            ctx.relogin_in_progress = False

    asyncio.create_task(_run())


def _rows_to_orders(rows: dict) -> list:
    from scraper.models import Order
    return [
        Order(
            order_id=r["order_id"],
            item_name=r["item_name"],
            status=r["status"],
            order_url=r["order_url"],
            order_date=datetime.fromisoformat(r["order_date"]) if r.get("order_date") else None,
            tracking_number=r.get("tracking_number"),
            estimated_delivery=r.get("estimated_delivery"),
            seller=r.get("seller"),
            recipient=r.get("recipient"),
            sub_status=r.get("sub_status"),
            completed_at=datetime.fromisoformat(r["completed_at"]) if r.get("completed_at") else None,
        )
        for r in rows.values()
    ]


def _split_message(text: str, max_len: int) -> list[str]:
    chunks = []
    while len(text) > max_len:
        split_at = text.rfind("\n", 0, max_len)
        if split_at == -1:
            split_at = max_len
        chunks.append(text[:split_at])
        text = text[split_at:].lstrip("\n")
    if text:
        chunks.append(text)
    return chunks
