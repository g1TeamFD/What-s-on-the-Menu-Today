import os
import json
import logging
from dataclasses import dataclass
from typing import Dict, List, Any, Tuple
from pathlib import Path
import random

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
    filters,
    PicklePersistence,
)

# ---------- Config ----------
DATA_DIR = Path(__file__).resolve().parent.parent / "data"
MENULIST_FILE = DATA_DIR / "Menulist.json"
MENU_AND_DISHES_FILE = DATA_DIR / "MenuAndDishes.json"

BOT_TOKEN = os.getenv("BOT_TOKEN")   # set this in Codespaces Secrets

# ---------- Models ----------
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

# ---------- Data Loading ----------
def load_data() -> Tuple[Dict[str, str], Dict[str, List[Dish]]]:
    """Loads Menus and Dishes; returns (menus_by_id, dishes_by_menu_id)."""
    if not MENULIST_FILE.exists():
        raise FileNotFoundError(f"Missing {MENULIST_FILE}. Create data/Menulist.json")
    if not MENU_AND_DISHES_FILE.exists():
        raise FileNotFoundError(f"Missing {MENU_AND_DISHES_FILE}. Create data/MenuAndDishes.json")

    with open(MENULIST_FILE, "r", encoding="utf-8") as f:
        menus_raw = json.load(f)

    menus_by_id: Dict[str, str] = {}
    for row in menus_raw:
        mid = str(row.get("Menu unique ID", "")).strip()
        name = str(row.get("Menu", "")).strip()
        if not mid or not name:
            continue
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
        d = Dish(
            menu_id=mid, menu_name=menu_name, dish=dish_name,
            price=float(price) if isinstance(price, (int, float, str)) and str(price) else 0.0,
            tagline=tagline, nutrition=nutrition, challenge=challenge, challenge_id=challenge_id
        )
        dishes_by_menu.setdefault(mid, []).append(d)

    # Remove menus that have no dishes
    to_delete = [mid for mid, items in dishes_by_menu.items() if not items]
    for mid in to_delete:
        del dishes_by_menu[mid]
        menus_by_id.pop(mid, None)

    return menus_by_id, dishes_by_menu

# Global (loaded in main())
MENUS_BY_ID: Dict[str, str] = {}
DISHES_BY_MENU: Dict[str, List[Dish]] = {}

# ---------- UI helpers ----------
def menu_list_keyboard() -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton("🎲 Random pick", callback_data="r")]]
    for mid, name in sorted(MENUS_BY_ID.items(), key=lambda kv: kv[1].lower()):
        rows.append([InlineKeyboardButton(name, callback_data=f"m:{mid}")])
    return InlineKeyboardMarkup(rows)

def dish_nav_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("« Prev", callback_data="p"),
                InlineKeyboardButton("Select this dish ✅", callback_data="s"),
                InlineKeyboardButton("Next »", callback_data="n"),
            ],
            [InlineKeyboardButton("↩️ Back to menus", callback_data="b")],
        ]
    )

def format_card(d: Dish) -> str:
    price_text = f"${d.price:,.2f}" if d.price else ""
    lines = [
        f"<b>{escape_html(d.dish)}</b>" + (f" — {escape_html(price_text)}" if price_text else ""),
        f"<i>{escape_html(d.tagline)}</i>" if d.tagline else "",
        f"Nutrition Fact: {escape_html(d.nutrition)}" if d.nutrition else "",
    ]
    return "\n".join([ln for ln in lines if ln])

def escape_html(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )

def encouragement_message() -> str:
    link = "https://www.fundamentaldecisions.com/2025/08/23/submission/"
    return (
        "🍽️ Enjoy your meal!\n"
        "You have 24 hours to complete the challenge and submit your result here to get a Persona Card — a snapshot of your mindset in action:\n"
        f"{link}\n"
        "We’re cheering for you! 💪"
    )

# chat_data keys:
#   menu_id: str
#   index: int
#   challenge_shown: bool

# ---------- Handlers ----------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "What’s on the Menu today?",
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("🍽️ See menus", callback_data="list")]]
        ),
    )

async def cmd_today(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await cmd_start(update, context)

async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # Block free text; buttons only
    await update.message.reply_text(
        "Please use the buttons below 😊",
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("🍽️ See menus", callback_data="list")]]
        ),
    )

async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    data = query.data
    await query.answer()

    chat = context.chat_data

    if data == "list":
        chat.clear()
        await query.edit_message_text("Choose a Menu:", reply_markup=menu_list_keyboard())
        return

    if data == "r":
        mid = random.choice(list(DISHES_BY_MENU.keys()))
        chat["menu_id"] = mid
        chat["index"] = 0
        await show_current_card(query, context)
        return

    if data.startswith("m:"):
        mid = data.split(":", 1)[1]
        if mid not in DISHES_BY_MENU:
            await query.edit_message_text("Sorry, that menu is unavailable.")
            return
        chat["menu_id"] = mid
        chat["index"] = 0
        await show_current_card(query, context)
        return

    if data in ("p", "n", "s", "b"):
        if "menu_id" not in chat:
            await query.edit_message_text("Choose a Menu:", reply_markup=menu_list_keyboard())
            return

        if data == "p":
            chat["index"] = max(0, chat.get("index", 0) - 1)
            await show_current_card(query, context)
            return
        elif data == "n":
            chat["index"] = min(len(DISHES_BY_MENU[chat["menu_id"]]) - 1, chat.get("index", 0) + 1)
            await show_current_card(query, context)
            return
        elif data == "s":
            d = current_dish(chat)
            text = f"🥇 Challenge for <b>{escape_html(d.dish)}</b>\n\n{escape_html(d.challenge)}"
            await query.get_message().reply_text(text, parse_mode=constants.ParseMode.HTML)
            await query.get_message().reply_text(encouragement_message())
            chat["challenge_shown"] = True
            return
        elif data == "b":
            chat.clear()
            await query.edit_message_text("Choose a Menu:", reply_markup=menu_list_keyboard())
            return

    await query.edit_message_text("Please use the buttons below.")

def current_dish(chat_data: Dict[str, Any]) -> Dish:
    mid = chat_data["menu_id"]
    idx = chat_data.get("index", 0)
    items = DISHES_BY_MENU[mid]
    idx = max(0, min(idx, len(items) - 1))
    return items[idx]

async def show_current_card(query, context: ContextTypes.DEFAULT_TYPE):
    d = current_dish(context.chat_data)
    text = format_card(d)
    await query.edit_message_text(
        text=text,
        parse_mode=constants.ParseMode.HTML,
        reply_markup=dish_nav_keyboard(),
    )

# ---------- Main ----------
def main() -> None:
    global MENUS_BY_ID, DISHES_BY_MENU

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    MENUS_BY_ID, DISHES_BY_MENU = load_data()
    if not MENUS_BY_ID:
        raise RuntimeError("No menus found; check data/Menulist.json")
    if not DISHES_BY_MENU:
        raise RuntimeError("No dishes found; check data/MenuAndDishes.json")

    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN environment variable is not set")

    persistence = PicklePersistence(filepath=str(Path(".bot_state.pkl")))
    app = ApplicationBuilder().token(BOT_TOKEN).persistence(persistence).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("today", cmd_today))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
