import os
import json
import logging
import asyncio
import random
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta, time as dtime
from pathlib import Path
from typing import Dict, List, Tuple, Optional

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    constants,
)
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    PicklePersistence,
    filters,
)

# ───────────────────────────────────────────────────────────────────────────────
# Config
# ───────────────────────────────────────────────────────────────────────────────
REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = (REPO_ROOT / "data") if (REPO_ROOT / "data").exists() else (REPO_ROOT / "Data")
DATA_DIR.mkdir(parents=True, exist_ok=True)

MENULIST_FILE = DATA_DIR / "Menulist.json"
MENU_AND_DISHES_FILE = DATA_DIR / "MenuAndDishes.json"
FAQ_FILE = DATA_DIR / "faq.json"
DASHBOARD_FILE = DATA_DIR / "dashboard.json"

BOT_TOKEN = os.getenv("BOT_TOKEN")
SUBMIT_URL = "https://www.fundamentaldecisions.com/2025/08/23/submission/"

# Fixed baseline: all "Before Timing" values are authored in GMT+7
BASE_TZ_OFFSET_MINUTES = 7 * 60  # GMT+7

# chat_data keys:
#   menu_id: str
#   cards_dishes: List[Dish]
#   list_msg_id: int  (message id of the dish list / selected card)
#   last_menu_list_msg_id: int (message id of the "Select a Menu below." message)
# user_data keys (optional utilities, not used for slot calc):
#   tz_offset_minutes: int  (UTC offset, e.g., +480 for +08:00)

# ───────────────────────────────────────────────────────────────────────────────
# Data models
# ───────────────────────────────────────────────────────────────────────────────
@dataclass
class Dish:
    menu_id: str
    menu_name: str
    dish: str
    price: float
    tagline: str
    nutrition: str
    challenge: str
    challenge_id: str
    best_timing: str          # optional, for display
    before_timing_raw: str    # original string
    before_time: dtime        # parsed time-of-day (interpreted in GMT+7 baseline)

MENUS_BY_ID: Dict[str, str] = {}
DISHES_BY_MENU: Dict[str, List[Dish]] = {}

# ───────────────────────────────────────────────────────────────────────────────
# Utilities
# ───────────────────────────────────────────────────────────────────────────────
def escape_html(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

def fmt_price(p: float) -> str:
    return f"${p:,.2f}" if p else ""

def ensure_sample_faq() -> None:
    if FAQ_FILE.exists():
        return
    sample = [
        {"q": "What is this bot?", "a": "It suggests daily menus and fun mindset challenges for travelers."},
        {"q": "How do I submit my challenge?", "a": f"Use the link shown with your challenge: {SUBMIT_URL}"},
        {"q": "Can I pick multiple dishes a day?", "a": "Yes—pick any dish you like, anytime."}
    ]
    FAQ_FILE.write_text(json.dumps(sample, ensure_ascii=False, indent=2), encoding="utf-8")

def read_faq() -> List[Dict[str, str]]:
    ensure_sample_faq()
    try:
        return json.loads(FAQ_FILE.read_text(encoding="utf-8"))
    except Exception:
        return []

def append_dashboard_entry(user_id: int, challenge_id: str, dish: str, menu_name: str) -> None:
    entry = {
        "date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "user_id": user_id,
        "challenge_id": challenge_id,
        "dish": dish,
        "menu": menu_name,
    }
    rows: List[Dict] = []
    if DASHBOARD_FILE.exists():
        try:
            rows = json.loads(DASHBOARD_FILE.read_text(encoding="utf-8")) or []
        except Exception:
            rows = []
    rows.append(entry)
    DASHBOARD_FILE.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")

def read_user_dashboard(user_id: int) -> List[Dict]:
    if not DASHBOARD_FILE.exists():
        return []
    try:
        rows = json.loads(DASHBOARD_FILE.read_text(encoding="utf-8")) or []
    except Exception:
        return []
    return [r for r in rows if str(r.get("user_id")) == str(user_id)]

def parse_before_time(value: str) -> Optional[dtime]:
    """Accept '11:00:00 AM' (your standard), plus some common variants."""
    if not value:
        return None
    value = value.strip()
    fmts = [
        "%I:%M:%S %p",  # 11:00:00 AM  ✅ your format
        "%I:%M %p",     # 11:00 AM
        "%H:%M:%S",     # 14:00:00
        "%H:%M",        # 14:00
    ]
    for fmt in fmts:
        try:
            return datetime.strptime(value, fmt).time()
        except Exception:
            continue
    return None

def now_in_baseline_tz() -> datetime:
    """Current time in the baseline timezone (GMT+7)."""
    return datetime.now(timezone.utc) + timedelta(minutes=BASE_TZ_OFFSET_MINUTES)

def next_slot_time(today_in_base: datetime, times: List[dtime]) -> dtime:
    """Return the next time in 'times' strictly after 'today_in_base.time()', else earliest (wrap)."""
    if not times:
        return dtime(0, 0)
    now_t = today_in_base.time()
    after = [t for t in times if t > now_t]
    return min(after) if after else min(times)

def filter_dishes_for_next_slot_baseline(dishes: List[Dish]) -> Tuple[List[Dish], dtime]:
    """Filter using the fixed GMT+7 baseline."""
    now_base = now_in_baseline_tz()
    slots = sorted({d.before_time for d in dishes})
    if not slots:
        return [], dtime(0, 0)
    target = next_slot_time(now_base, slots)
    filtered = [d for d in dishes if d.before_time == target]
    return filtered, target

# ───────────────────────────────────────────────────────────────────────────────
# Data loading
# ───────────────────────────────────────────────────────────────────────────────
def load_data() -> Tuple[Dict[str, str], Dict[str, List[Dish]]]:
    if not MENULIST_FILE.exists():
        raise FileNotFoundError(f"Missing file: {MENULIST_FILE}")
    if not MENU_AND_DISHES_FILE.exists():
        raise FileNotFoundError(f"Missing file: {MENU_AND_DISHES_FILE}")

    menus_raw = json.loads(MENULIST_FILE.read_text(encoding="utf-8"))
    menus_by_id: Dict[str, str] = {}
    for row in menus_raw:
        mid = str(row.get("Menu unique ID", "")).strip()
        name = str(row.get("Menu", "")).strip()
        if mid and name:
            menus_by_id[mid] = name

    dishes_raw = json.loads(MENU_AND_DISHES_FILE.read_text(encoding="utf-8"))
    dishes_by_menu: Dict[str, List[Dish]] = {k: [] for k in menus_by_id}
    for row in dishes_raw:
        mid = str(row.get("Menu unique ID", "")).strip()
        menu_name = str(row.get("Menu", menus_by_id.get(mid, ""))).strip()
        dish_name = str(row.get("Dish", "")).strip()
        price = row.get("Price", 0)
        tagline = str(row.get("Tag line", "")).strip()
        nutrition = str(row.get("Nutrition fact", "")).strip()
        challenge = str(row.get("Challenge", "")).strip()
        challenge_id = str(row.get("Challenge Unique ID", "")).strip()
        best_timing = str(row.get("Best Timing", "")).strip()
        before_raw = str(row.get("Before Timing", "")).strip()
        before_t = parse_before_time(before_raw)

        if not mid or not dish_name or not before_t:
            continue

        try:
            p = float(price)
        except Exception:
            p = 0.0

        dishes_by_menu.setdefault(mid, []).append(
            Dish(
                menu_id=mid,
                menu_name=menu_name,
                dish=dish_name,
                price=p,
                tagline=tagline,
                nutrition=nutrition,
                challenge=challenge,
                challenge_id=challenge_id,
                best_timing=best_timing,
                before_timing_raw=before_raw,
                before_time=before_t,  # interpreted in GMT+7 baseline
            )
        )

    # prune menus with no dishes
    for mid in list(dishes_by_menu.keys()):
        if not dishes_by_menu[mid]:
            dishes_by_menu.pop(mid, None)
            menus_by_id.pop(mid, None)

    return menus_by_id, dishes_by_menu

# ───────────────────────────────────────────────────────────────────────────────
# UI helpers
# ───────────────────────────────────────────────────────────────────────────────
def card_text(d: Dish) -> str:
    price = fmt_price(d.price)
    lines = [
        f"<b>{escape_html(d.dish)}</b>" + (f" — {price}" if price else ""),
        f"<i>{escape_html(d.tagline)}</i>" if d.tagline else "",
        f"Nutrition Fact: {escape_html(d.nutrition)}" if d.nutrition else "",
    ]
    return "\n".join([ln for ln in lines if ln])

def render_dish_list_text(menu_name: str, dishes: List[Dish]) -> str:
    """Build a single message that lists all dishes with full details."""
    lines = [f"Menu: {escape_html(menu_name)}. Available food/drink as below. Tap a card to select:\n"]
    for i, d in enumerate(dishes, 1):
        price = fmt_price(d.price)
        lines.append(
            "\n".join([
                f"<b>{i}) {escape_html(d.dish)}</b>" + (f" — {price}" if price else ""),
                f"<i>{escape_html(d.tagline)}</i>" if d.tagline else "",
                f"Nutrition Fact: {escape_html(d.nutrition)}" if d.nutrition else "",
                ""
            ])
        )
    return "\n".join(lines).strip()

def build_dish_buttons(dishes: List[Dish]) -> InlineKeyboardMarkup:
    # Short, numbered labels; full details are shown in the message text.
    rows = []
    for i, d in enumerate(dishes, 1):
        label = f"{i}) {d.dish}"
        rows.append([InlineKeyboardButton(label, callback_data=f"sel:{i-1}")])
    rows.append([InlineKeyboardButton("↩️ Back to menus", callback_data="b")])
    return InlineKeyboardMarkup(rows)

def menu_list_keyboard() -> InlineKeyboardMarkup:
    """Show only 3 random menus (plus Random pick)."""
    rows = [[InlineKeyboardButton("🎲 Random pick", callback_data="r")]]
    all_items = list(MENUS_BY_ID.items())
    random.shuffle(all_items)
    subset = all_items[: min(3, len(all_items))]
    for mid, name in subset:
        rows.append([InlineKeyboardButton(name, callback_data=f"m:{mid}")])
    return InlineKeyboardMarkup(rows)

# ───────────────────────────────────────────────────────────────────────────────
# Handlers
# ───────────────────────────────────────────────────────────────────────────────
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    intro = (
        "👋 Hellooooo <b>I’m Your Bot for Daily Challenges!</b>\n\n"
        "Every day, I’ll provide you a fresh menu of dishes 🍽️.\n"
        "But here’s the twist: each dish comes with a fun challenge designed to spark "
        "reflection, creativity, and growth.\n\n"
        "Pick a dish → reveal a challenge → complete it within 24 hours → submit to earn "
        "your Persona Card 🎴.\n\n"
        "Ready to order?"
    )
    await update.message.reply_text(
        intro,
        parse_mode=constants.ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("👉 Let’s see What’s on the Menu today?", callback_data="list")]]
        ),
    )

async def cmd_today(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await cmd_start(update, context)

async def cmd_set_timezone(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Optional: /set_timezone +08:00   or   /set_timezone -05:30
    (Does not affect slot selection; slots are based on the GMT+7 baseline.)
    """
    if not context.args:
        await update.message.reply_text("Please provide an offset, e.g. /set_timezone +08:00")
        return
    arg = context.args[0].strip()
    try:
        sign = 1 if arg.startswith("+") else -1
        hhmm = arg[1:]
        if ":" in hhmm:
            hh, mm = hhmm.split(":", 1)
        else:
            hh, mm = hhmm, "00"
        minutes = sign * (int(hh) * 60 + int(mm))
    except Exception:
        await update.message.reply_text("Invalid format. Example: /set_timezone +08:00")
        return

    context.user_data["tz_offset_minutes"] = minutes
    await update.message.reply_text(f"Timezone noted (UTC{arg}).")

async def cmd_faq(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    items = read_faq()
    if not items:
        await update.message.reply_text("FAQ is empty.")
        return
    text_lines = ["<b>FAQ</b>"]
    for i, qa in enumerate(items, 1):
        q = escape_html(qa.get("q", ""))
        a = escape_html(qa.get("a", ""))
        text_lines.append(f"\n<b>{i}. {q}</b>\n{a}")
    await update.message.reply_text("\n".join(text_lines), parse_mode=constants.ParseMode.HTML, disable_web_page_preview=True)

async def cmd_dashboard(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    rows = read_user_dashboard(user_id)
    if not rows:
        await update.message.reply_text("Your dashboard is empty.\nPick a dish to start a challenge!")
        return
    lines = ["<b>My Dashboard</b>"]
    for r in rows:
        date = escape_html(str(r.get("date", "")))
        dish = escape_html(str(r.get("dish", "")))
        menu = escape_html(str(r.get("menu", "")))
        lines.append(f"• {date} — {dish} ({menu})")
    await update.message.reply_text("\n".join(lines), parse_mode=constants.ParseMode.HTML)

async def cmd_debug_time(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show baseline (GMT+7) time and count of dishes in the next slot for current menu."""
    now_base = now_in_baseline_tz()
    mid = context.chat_data.get("menu_id")
    if mid and mid in DISHES_BY_MENU and DISHES_BY_MENU[mid]:
        filtered, _ = filter_dishes_for_next_slot_baseline(DISHES_BY_MENU[mid])
        await update.message.reply_text(
            f"Baseline time: {now_base.strftime('%Y-%m-%d %I:%M:%S %p')} (GMT+7)\n"
            f"Dishes available in the next slot: {len(filtered)}"
        )
    else:
        await update.message.reply_text(
            f"Baseline time: {now_base.strftime('%Y-%m-%d %I:%M:%S %p')} (GMT+7)\n"
            "Pick a menu first (/start → See menus)."
        )

async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Please use the buttons or commands below 😊\nAvailable: /faq  /my_dashboard  /debug_time  /set_timezone",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🍽️ See menus", callback_data="list")]]),
    )

async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    data = query.data
    try:
        await query.answer()
    except Exception:
        pass

    # Show a small menu list (3 random)
    if data == "list":
        context.chat_data.clear()
        # Send/replace with a small list
        await query.edit_message_text("Select a Menu below.", reply_markup=menu_list_keyboard())
        # track this message id so we can edit it to just the selected menu name later
        context.chat_data["last_menu_list_msg_id"] = query.message.message_id
        return

    # Random menu → show filtered dish buttons (next slot, in GMT+7)
    if data == "r":
        mid = random.choice(list(DISHES_BY_MENU.keys()))
        context.chat_data["menu_id"] = mid
        menu_name = MENUS_BY_ID.get(mid, "Selected Menu")
        # If we have a previous menu list message, edit it to keep only the selected menu name
        menu_list_id = context.chat_data.get("last_menu_list_msg_id")
        if menu_list_id:
            try:
                await context.bot.edit_message_text(
                    chat_id=query.message.chat_id,
                    message_id=menu_list_id,
                    text=f"Menu: {escape_html(menu_name)}",
                    parse_mode=constants.ParseMode.HTML,
                )
            except Exception as e:
                logging.warning("Could not edit menu list to selected name: %s", e)
        else:
            # fallback: edit current message
            try:
                await query.edit_message_text(
                    text=f"Menu: {escape_html(menu_name)}",
                    parse_mode=constants.ParseMode.HTML,
                )
                context.chat_data["last_menu_list_msg_id"] = query.message.message_id
            except Exception:
                pass
        # Send a NEW message with dishes
        await show_filtered_cards_send_new(query, context)
        return

    # Specific menu → show filtered dish buttons for that menu (in GMT+7)
    if data.startswith("m:"):
        mid = data.split(":", 1)[1]
        if mid not in DISHES_BY_MENU:
            await query.edit_message_text("Sorry, that menu is unavailable.")
            return
        context.chat_data["menu_id"] = mid
        menu_name = MENUS_BY_ID.get(mid, "Selected Menu")

        # Edit the existing menu list message (with buttons) to just the selected menu name
        try:
            await query.edit_message_text(
                text=f"Menu: {escape_html(menu_name)}",
                parse_mode=constants.ParseMode.HTML,
                reply_markup=None,
            )
            context.chat_data["last_menu_list_msg_id"] = query.message.message_id
        except Exception as e:
            logging.warning("Could not edit menu list to selected name: %s", e)

        # Send a NEW message with the dish list
        await show_filtered_cards_send_new(query, context)
        return

    # Back to menus
    if data == "b":
        context.chat_data.pop("menu_id", None)
        context.chat_data.pop("cards_dishes", None)
        context.chat_data.pop("list_msg_id", None)
        # Show fresh 3 random menus
        try:
            await query.edit_message_text("Select a Menu below.", reply_markup=menu_list_keyboard())
            context.chat_data["last_menu_list_msg_id"] = query.message.message_id
        except Exception:
            pass
        return

    # Select a dish from the list
    if data.startswith("sel:"):
        if "menu_id" not in context.chat_data:
            await query.edit_message_text("Select a Menu below.", reply_markup=menu_list_keyboard())
            return
        try:
            idx = int(data.split(":", 1)[1])
        except ValueError:
            idx = 0

        dishes_shown: List[Dish] = context.chat_data.get("cards_dishes", [])
        list_msg_id: Optional[int] = context.chat_data.get("list_msg_id")
        if not dishes_shown or idx < 0 or idx >= len(dishes_shown) or not list_msg_id:
            await query.edit_message_text("Select a Menu below.", reply_markup=menu_list_keyboard())
            return

        d = dishes_shown[idx]
        chat_id = query.message.chat_id

        # Edit the dish list message to show ONLY the selected dish details (no keyboard)
        try:
            await context.bot.edit_message_text(
                chat_id=chat_id,
                message_id=list_msg_id,
                text=card_text(d),
                parse_mode=constants.ParseMode.HTML,
                reply_markup=None,
                disable_web_page_preview=True,
            )
        except Exception as e:
            logging.warning("Could not edit dish list to selected card: %s", e)

        # Clear state for list/choices
        context.chat_data["cards_dishes"] = []
        context.chat_data["list_msg_id"] = None

        # Log selection to dashboard
        user_id = update.effective_user.id
        from_menu = MENUS_BY_ID.get(d.menu_id, d.menu_name)
        append_dashboard_entry(user_id, d.challenge_id, d.dish, from_menu)

        # Post challenge
        combined = (
            f"🥇 Challenge for <b>{escape_html(d.dish)}</b>\n\n"
            f"{escape_html(d.challenge)}\n\n"
            "🍽️ Enjoy your meal!\n"
            "You have 24 hours to complete the challenge and submit your result here to get a Persona Card — "
            "a snapshot of your mindset in action:\n"
            f"<a href=\"{SUBMIT_URL}\">{SUBMIT_URL}</a>"
        )
        await context.bot.send_message(
            chat_id=chat_id,
            text=combined,
            parse_mode=constants.ParseMode.HTML,
            disable_web_page_preview=True,
        )
        await context.bot.send_message(chat_id=chat_id, text="Commands: /faq   /my_dashboard   /debug_time   /set_timezone")
        return

    # Fallback
    await query.edit_message_text("Please use the buttons or /faq /my_dashboard /debug_time /set_timezone.")

# NEW: send a new message for dishes (keep selected menu name message intact)
async def show_filtered_cards_send_new(query, context: ContextTypes.DEFAULT_TYPE):
    """
    Send ONE new message that contains full details for each dish (next slot in GMT+7),
    with numbered buttons to select. We do NOT modify the menu name message above.
    """
    mid = context.chat_data["menu_id"]
    all_dishes = DISHES_BY_MENU[mid]
    menu_name = MENUS_BY_ID.get(mid, "Selected Menu")

    filtered, _slot = filter_dishes_for_next_slot_baseline(all_dishes)

    if not filtered:
        await context.bot.send_message(
            chat_id=query.message.chat_id,
            text=f"Menu: {escape_html(menu_name)}. No dishes available right now. Please check back later.",
            parse_mode=constants.ParseMode.HTML,
            disable_web_page_preview=True,
        )
        return

    body = render_dish_list_text(menu_name, filtered)
    keyboard = build_dish_buttons(filtered)

    msg = await context.bot.send_message(
        chat_id=query.message.chat_id,
        text=body,
        parse_mode=constants.ParseMode.HTML,
        reply_markup=keyboard,
        disable_web_page_preview=True,
    )

    # Remember which dishes were shown and the message id to edit later
    context.chat_data["cards_dishes"] = filtered
    context.chat_data["list_msg_id"] = msg.message_id

# ───────────────────────────────────────────────────────────────────────────────
# Error handler
# ───────────────────────────────────────────────────────────────────────────────
async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logging.exception("Unhandled error: %s", context.error)
    try:
        if isinstance(update, Update) and update.effective_chat:
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text="Oops, something went wrong. Please try again.",
            )
    except Exception:
        pass

# ───────────────────────────────────────────────────────────────────────────────
# Main
# ───────────────────────────────────────────────────────────────────────────────
def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    global MENUS_BY_ID, DISHES_BY_MENU
    MENUS_BY_ID, DISHES_BY_MENU = load_data()
    if not MENUS_BY_ID:
        raise RuntimeError("No menus found; check Menulist.json")
    if not DISHES_BY_MENU:
        raise RuntimeError("No dishes found; check MenuAndDishes.json")
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN environment variable is not set")

    # Ensure sample FAQ exists
    ensure_sample_faq()

    persistence = PicklePersistence(filepath=str(REPO_ROOT / ".bot_state.pkl"))
    app = ApplicationBuilder().token(BOT_TOKEN).persistence(persistence).build()

    async def _post_init(app_):
        me = await app_.bot.get_me()
        logging.info("Bot started as @%s (id=%s). Waiting for messages… [3-MENU LIST + PERSISTENT SELECTION]", me.username, me.id)
        await app_.bot.delete_webhook(drop_pending_updates=False)
    app.post_init = _post_init

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("today", cmd_today))
    app.add_handler(CommandHandler("set_timezone", cmd_set_timezone))  # optional; doesn't affect slots
    app.add_handler(CommandHandler("faq", cmd_faq))
    app.add_handler(CommandHandler("my_dashboard", cmd_dashboard))
    app.add_handler(CommandHandler("debug_time", cmd_debug_time))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_error_handler(on_error)

    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
