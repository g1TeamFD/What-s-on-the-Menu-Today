import os
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Tuple

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
MENULIST_FILE = DATA_DIR / "Menulist.json"
MENU_AND_DISHES_FILE = DATA_DIR / "MenuAndDishes.json"

BOT_TOKEN = os.getenv("BOT_TOKEN")
SUBMIT_URL = "https://www.fundamentaldecisions.com/2025/08/23/submission/"

# Dishes list UI
PAGE_SIZE = 8  # number of dish buttons per page

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


# ───────────────────────────────────────────────────────────────────────────────
# Data loading
# ───────────────────────────────────────────────────────────────────────────────
def load_data() -> Tuple[Dict[str, str], Dict[str, List[Dish]]]:
    if not MENULIST_FILE.exists():
        raise FileNotFoundError(f"Missing file: {MENULIST_FILE}")
    if not MENU_AND_DISHES_FILE.exists():
        raise FileNotFoundError(f"Missing file: {MENU_AND_DISHES_FILE}")

    with open(MENULIST_FILE, "r", encoding="utf-8") as f:
        menus_raw = json.load(f)

    menus_by_id: Dict[str, str] = {}
    for row in menus_raw:
        mid = str(row.get("Menu unique ID", "")).strip()
        name = str(row.get("Menu", "")).strip()
        if mid and name:
            menus_by_id[mid] = name

    with open(MENU_AND_DISHES_FILE, "r", encoding="utf-8") as f:
        dishes_raw = json.load(f)

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
        if not mid or not dish_name:
            continue
        try:
            p = float(price)
        except Exception:
            p = 0.0
        dishes_by_menu.setdefault(mid, []).append(
            Dish(mid, menu_name, dish_name, p, tagline, nutrition, challenge, challenge_id)
        )

    # Remove menus with no dishes
    for mid in list(dishes_by_menu.keys()):
        if not dishes_by_menu[mid]:
            dishes_by_menu.pop(mid, None)
            menus_by_id.pop(mid, None)

    return menus_by_id, dishes_by_menu


MENUS_BY_ID: Dict[str, str] = {}
DISHES_BY_MENU: Dict[str, List[Dish]] = {}

# ───────────────────────────────────────────────────────────────────────────────
# UI helpers
# ───────────────────────────────────────────────────────────────────────────────
def escape_html(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def menu_list_keyboard() -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton("🎲 Random pick", callback_data="r")]]
    for mid, name in sorted(MENUS_BY_ID.items(), key=lambda kv: kv[1].lower()):
        rows.append([InlineKeyboardButton(name, callback_data=f"m:{mid}")])
    return InlineKeyboardMarkup(rows)


def dish_buttons_keyboard(mid: str, page: int = 0) -> InlineKeyboardMarkup:
    """Build a paginated keyboard of dish buttons for a menu."""
    dishes = DISHES_BY_MENU[mid]
    start = page * PAGE_SIZE
    end = min(start + PAGE_SIZE, len(dishes))

    rows: List[List[InlineKeyboardButton]] = []
    for idx in range(start, end):
        d = dishes[idx]
        label_price = f" — ${d.price:,.2f}" if d.price else ""
        label = f"{d.dish}{label_price}"
        rows.append([InlineKeyboardButton(label, callback_data=f"ds:{mid}:{idx}:{page}")])

    # Pagination row (if needed)
    nav: List[InlineKeyboardButton] = []
    if page > 0:
        nav.append(InlineKeyboardButton("« Prev", callback_data=f"dp:{mid}:{page-1}"))
    if end < len(dishes):
        nav.append(InlineKeyboardButton("Next »", callback_data=f"dp:{mid}:{page+1}"))
    if nav:
        rows.append(nav)

    # Back row
    rows.append([InlineKeyboardButton("↩️ Back to menus", callback_data="b")])
    return InlineKeyboardMarkup(rows)


def dish_list_header(mid: str, page: int) -> str:
    dishes = DISHES_BY_MENU[mid]
    menu_name = MENUS_BY_ID.get(mid, "Selected Menu")
    total_pages = (len(dishes) - 1) // PAGE_SIZE + 1
    return (
        f"<b>{escape_html(menu_name)}</b> — {len(dishes)} dishes\n"
        f"Page {page + 1}/{total_pages}\n"
        "Pick a dish below to see its challenge:"
    )


# ───────────────────────────────────────────────────────────────────────────────
# Handlers
# ───────────────────────────────────────────────────────────────────────────────
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "What’s on the Menu today?",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🍽️ See menus", callback_data="list")]]),
    )


async def cmd_today(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await cmd_start(update, context)


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Please use the buttons below 😊",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🍽️ See menus", callback_data="list")]]),
    )


async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    data = query.data
    await query.answer()

    # Show menu list
    if data == "list":
        context.chat_data.clear()
        await query.edit_message_text("Choose a Menu:", reply_markup=menu_list_keyboard())
        return

    # Random menu
    if data == "r":
        from random import choice
        mid = choice(list(DISHES_BY_MENU.keys()))
        context.chat_data["menu_id"] = mid
        context.chat_data["page"] = 0
        await show_dish_list(query, mid, 0)
        return

    # Specific menu
    if data.startswith("m:"):
        mid = data.split(":", 1)[1]
        if mid not in DISHES_BY_MENU:
            await query.edit_message_text("Sorry, that menu is unavailable.")
            return
        context.chat_data["menu_id"] = mid
        context.chat_data["page"] = 0
        await show_dish_list(query, mid, 0)
        return

    # Dishes pagination
    if data.startswith("dp:"):
        _, mid, page_s = data.split(":")
        page = max(0, int(page_s))
        context.chat_data["menu_id"] = mid
        context.chat_data["page"] = page
        await show_dish_list(query, mid, page)
        return

    # Dish selected
    if data.startswith("ds:"):
        _, mid, idx_s, page_s = data.split(":")
        idx = int(idx_s)
        page = int(page_s)
        dishes = DISHES_BY_MENU.get(mid, [])
        if not dishes or idx < 0 or idx >= len(dishes):
            await query.edit_message_text("Choose a Menu:", reply_markup=menu_list_keyboard())
            return
        d = dishes[idx]
        combined = (
            f"🥇 Challenge for <b>{escape_html(d.dish)}</b>\n\n"
            f"{escape_html(d.challenge)}\n\n"
            "🍽️ Enjoy your meal!\n"
            "You have 24 hours to complete the challenge and submit your result here to get a Persona Card — "
            "a snapshot of your mindset in action:\n"
            f"<a href=\"{SUBMIT_URL}\">{SUBMIT_URL}</a>"
        )
        # Save where the user was, to allow "Back to dishes"
        context.chat_data["menu_id"] = mid
        context.chat_data["page"] = page

        kb = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("⬅️ Back to dishes", callback_data=f"dp:{mid}:{page}")],
                [InlineKeyboardButton("↩️ Back to menus", callback_data="b")],
            ]
        )
        await query.edit_message_text(
            combined, parse_mode=constants.ParseMode.HTML, disable_web_page_preview=True, reply_markup=kb
        )
        return

    # Back to menus
    if data == "b":
        context.chat_data.clear()
        await query.edit_message_text("Choose a Menu:", reply_markup=menu_list_keyboard())
        return

    # Fallback
    await query.edit_message_text("Please use the buttons below.")


async def show_dish_list(query, mid: str, page: int) -> None:
    await query.edit_message_text(
        dish_list_header(mid, page),
        parse_mode=constants.ParseMode.HTML,
        reply_markup=dish_buttons_keyboard(mid, page),
        disable_web_page_preview=True,
    )


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

    persistence = PicklePersistence(filepath=str(REPO_ROOT / ".bot_state.pkl"))
    app = ApplicationBuilder().token(BOT_TOKEN).persistence(persistence).build()

    # Clear old webhook so polling works in dev
    async def _post_init(app_):
        me = await app_.bot.get_me()
        logging.info("Bot started as @%s (id=%s). Waiting for messages… [LIST MODE]", me.username, me.id)
        await app_.bot.delete_webhook(drop_pending_updates=False)
    app.post_init = _post_init

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("today", cmd_today))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
