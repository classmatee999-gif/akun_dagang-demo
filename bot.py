import asyncio
import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes, CallbackQueryHandler, MessageHandler, filters, ConversationHandler
import oandapyV20
from oandapyV20.endpoints import orders, trades, instruments, accounts
import pandas as pd
import os

# ─────────────────────────────────────────
# KONFIGURASI — bisa diubah via Telegram
# ─────────────────────────────────────────
TELEGRAM_TOKEN  = os.environ.get("8721338927:AAFwI8cYBQiuIc14eH03FNZKrE1-ooIUJj4")
OANDA_TOKEN     = os.environ.get("0bfbe1cc9698a5b93a60b46b5bae86c9-34b23230db37270e68d96d5bc0b256ac")
ACCOUNT_ID      = os.environ.get("101-001-29134814-001")
ALLOWED_USER_ID = int(os.environ.get("ALLOWED_USER_ID", "6903511431"))
OANDA_ENV       = os.environ.get("OANDA_ENV", "practice")
# ─────────────────────────────────────────

ALL_PAIRS = [
    "EUR_USD", "GBP_USD", "USD_JPY", "USD_CHF", "AUD_USD",
    "USD_CAD", "NZD_USD", "EUR_GBP", "EUR_JPY", "GBP_JPY",
    "AUD_JPY", "EUR_AUD", "EUR_CAD", "GBP_CAD", "CAD_JPY",
]

# ── Setting yang bisa diubah via Telegram ──
settings = {
    "lot_size":       0.01,
    "layer_trigger":  -2.0,
    "total_tp":       10.0,
    "check_interval": 60,
    "ema_fast":       20,
    "ema_slow":       50,
}

pair_active: dict[str, bool]  = {p: False for p in ALL_PAIRS}
pair_tasks:  dict[str, object] = {}
pair_state:  dict[str, dict]  = {p: {"last_signal": None, "waiting_cross": None} for p in ALL_PAIRS}

# ConversationHandler states
AWAIT_SETTING_VALUE = 1
pending_setting_key = {}  # user_id -> key yang sedang diedit

logging.basicConfig(format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

client = oandapyV20.API(access_token=OANDA_TOKEN, environment=OANDA_ENV)


# ══════════════════════════════════════════
# UTILS
# ══════════════════════════════════════════

def pair_label(pair: str) -> str:
    return pair.replace("_", "/")

def em(text: str) -> str:
    """Escape MarkdownV2."""
    for ch in r"_*[]()~`>#+-=|{}.!":
        text = text.replace(ch, f"\\{ch}")
    return text

def is_allowed(update: Update) -> bool:
    return update.effective_user.id == ALLOWED_USER_ID


# ══════════════════════════════════════════
# OANDA
# ══════════════════════════════════════════

def get_candles(pair: str, count=60):
    params = {"count": count, "granularity": "D"}
    r = instruments.InstrumentsCandles(pair, params=params)
    client.request(r)
    return [float(c["mid"]["c"]) for c in r.response["candles"] if c["complete"]]

def calculate_ema(data, period):
    return pd.Series(data).ewm(span=period, adjust=False).mean().tolist()

def get_ema_signal(pair: str):
    closes = get_candles(pair, 60)
    fast   = settings["ema_fast"]
    slow   = settings["ema_slow"]
    if len(closes) < slow + 2:
        return None
    ema_f = calculate_ema(closes, fast)
    ema_s = calculate_ema(closes, slow)
    c_f, c_s = ema_f[-1], ema_s[-1]
    p_f, p_s = ema_f[-2], ema_s[-2]
    if p_f <= p_s and c_f > c_s:   return "buy"
    if p_f >= p_s and c_f < c_s:   return "sell"
    if c_f > c_s:                   return "buy_active"
    if c_f < c_s:                   return "sell_active"
    return None

def get_open_trades(pair: str):
    r = trades.TradesList(ACCOUNT_ID, params={"instrument": pair, "state": "OPEN"})
    client.request(r)
    return r.response.get("trades", [])

def get_last_trade_pl(open_trades):
    if not open_trades:
        return None
    return float(sorted(open_trades, key=lambda t: t["openTime"], reverse=True)[0]["unrealizedPL"])

def get_total_pl(open_trades):
    return sum(float(t["unrealizedPL"]) for t in open_trades)

def count_buy_sell(open_trades):
    buys  = [t for t in open_trades if int(float(t["currentUnits"])) > 0]
    sells = [t for t in open_trades if int(float(t["currentUnits"])) < 0]
    return len(buys), len(sells)

def place_order(pair: str, direction: str):
    units = str(int(settings["lot_size"] * 100000))
    if direction == "sell":
        units = "-" + units
    r = orders.OrderCreate(ACCOUNT_ID, data={"order": {"type": "MARKET", "instrument": pair, "units": units}})
    client.request(r)
    logger.info(f"[{pair}] {direction.upper()} placed.")

def close_all_trades(pair: str, open_trades):
    for trade in open_trades:
        try:
            client.request(trades.TradeClose(ACCOUNT_ID, tradeID=trade["id"]))
        except Exception as e:
            logger.error(f"[{pair}] close error: {e}")

def get_account_info():
    r = accounts.AccountSummary(ACCOUNT_ID)
    client.request(r)
    acc = r.response["account"]
    return {
        "balance":   float(acc["balance"]),
        "nav":       float(acc["NAV"]),
        "pl":        float(acc["unrealizedPL"]),
        "margin":    float(acc["marginUsed"]),
        "free_margin": float(acc["marginAvailable"]),
    }


# ══════════════════════════════════════════
# TRADING LOOP PER PAIR
# ══════════════════════════════════════════

async def trading_loop_pair(pair: str, app):
    state = pair_state[pair]
    logger.info(f"[{pair}] Loop start.")

    while pair_active.get(pair, False):
        try:
            signal        = get_ema_signal(pair)
            open_trades   = get_open_trades(pair)
            n_buy, n_sell = count_buy_sell(open_trades)
            total_pl      = get_total_pl(open_trades)
            lbl           = em(pair_label(pair))
            pl_s          = em(f"${total_pl:.2f}")
            tp            = settings["total_tp"]
            trigger       = settings["layer_trigger"]

            # 1. TP
            if open_trades and total_pl >= tp:
                close_all_trades(pair, open_trades)
                await app.bot.send_message(ALLOWED_USER_ID,
                    f"🎯 *{lbl}* — TP tercapai\\!\nProfit: `{pl_s}` ✅", parse_mode="MarkdownV2")
                state["last_signal"] = None
                state["waiting_cross"] = None
                await asyncio.sleep(settings["check_interval"])
                continue

            # 2. Crossing berlawanan
            if open_trades:
                if n_buy > 0 and signal in ("sell", "sell_active"):
                    close_all_trades(pair, open_trades)
                    await app.bot.send_message(ALLOWED_USER_ID,
                        f"🔄 *{lbl}* — Death Cross\\!\nBUY ditutup\\. P/L: `{pl_s}`", parse_mode="MarkdownV2")
                    state["last_signal"] = None
                    state["waiting_cross"] = "sell"
                    await asyncio.sleep(settings["check_interval"])
                    continue
                if n_sell > 0 and signal in ("buy", "buy_active"):
                    close_all_trades(pair, open_trades)
                    await app.bot.send_message(ALLOWED_USER_ID,
                        f"🔄 *{lbl}* — Golden Cross\\!\nSELL ditutup\\. P/L: `{pl_s}`", parse_mode="MarkdownV2")
                    state["last_signal"] = None
                    state["waiting_cross"] = "buy"
                    await asyncio.sleep(settings["check_interval"])
                    continue

            # 3. Entry pertama
            if signal == "buy" and state["last_signal"] != "buy" and n_buy == 0 and state["waiting_cross"] != "sell":
                place_order(pair, "buy")
                state["last_signal"] = "buy"
                state["waiting_cross"] = None
                lot = em(str(settings["lot_size"]))
                await app.bot.send_message(ALLOWED_USER_ID,
                    f"📈 *{lbl}* — ENTRY BUY \\#1\nGolden Cross \\| Lot: `{lot}`", parse_mode="MarkdownV2")

            elif signal == "sell" and state["last_signal"] != "sell" and n_sell == 0 and state["waiting_cross"] != "buy":
                place_order(pair, "sell")
                state["last_signal"] = "sell"
                state["waiting_cross"] = None
                lot = em(str(settings["lot_size"]))
                await app.bot.send_message(ALLOWED_USER_ID,
                    f"📉 *{lbl}* — ENTRY SELL \\#1\nDeath Cross \\| Lot: `{lot}`", parse_mode="MarkdownV2")

            # 4. Layering
            elif open_trades:
                last_pl = get_last_trade_pl(open_trades)
                if last_pl is not None and last_pl <= trigger:
                    ls = em(f"${last_pl:.2f}")
                    if n_buy > 0 and signal in ("buy", "buy_active"):
                        place_order(pair, "buy")
                        await app.bot.send_message(ALLOWED_USER_ID,
                            f"📈 *{lbl}* — LAYER BUY \\#{n_buy+1}\nLast: `{ls}` \\| Total: `{pl_s}`", parse_mode="MarkdownV2")
                    elif n_sell > 0 and signal in ("sell", "sell_active"):
                        place_order(pair, "sell")
                        await app.bot.send_message(ALLOWED_USER_ID,
                            f"📉 *{lbl}* — LAYER SELL \\#{n_sell+1}\nLast: `{ls}` \\| Total: `{pl_s}`", parse_mode="MarkdownV2")

        except Exception as e:
            logger.error(f"[{pair}] {e}")
            try:
                await app.bot.send_message(ALLOWED_USER_ID,
                    f"⚠️ *{em(pair_label(pair))}* error: `{em(str(e))}`", parse_mode="MarkdownV2")
            except Exception:
                pass

        await asyncio.sleep(settings["check_interval"])

    logger.info(f"[{pair}] Loop stop.")


def start_pair(pair: str, app):
    if pair_active.get(pair):
        return False
    pair_active[pair] = True
    pair_state[pair]  = {"last_signal": None, "waiting_cross": None}
    pair_tasks[pair]  = asyncio.create_task(trading_loop_pair(pair, app))
    return True

def stop_pair(pair: str):
    if not pair_active.get(pair):
        return False
    pair_active[pair] = False
    if pair in pair_tasks:
        pair_tasks[pair].cancel()
        del pair_tasks[pair]
    return True


# ══════════════════════════════════════════
# KEYBOARDS
# ══════════════════════════════════════════

def kb_main():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📋 Kelola Pair",    callback_data="menu_pairs"),
         InlineKeyboardButton("⚙️ Setting",        callback_data="menu_settings")],
        [InlineKeyboardButton("📊 Status Akun",    callback_data="menu_account"),
         InlineKeyboardButton("📈 Status Posisi",  callback_data="menu_positions")],
        [InlineKeyboardButton("▶️ ON Semua Pair",  callback_data="all_on"),
         InlineKeyboardButton("⏹ OFF Semua Pair", callback_data="all_off")],
        [InlineKeyboardButton("❌ Close Semua Posisi", callback_data="confirm_closeall")],
    ])

def kb_pairs(page=0):
    per_page    = 9
    start       = page * per_page
    page_pairs  = ALL_PAIRS[start:start+per_page]
    total_pages = (len(ALL_PAIRS) + per_page - 1) // per_page
    rows = []
    for pair in page_pairs:
        icon = "✅" if pair_active.get(pair) else "⭕"
        rows.append([InlineKeyboardButton(f"{icon} {pair_label(pair)}", callback_data=f"toggle_{pair}")])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀️", callback_data=f"pairs_page_{page-1}"))
    nav.append(InlineKeyboardButton(f"{page+1}/{total_pages}", callback_data="noop"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton("▶️", callback_data=f"pairs_page_{page+1}"))
    rows.append(nav)
    rows.append([InlineKeyboardButton("🏠 Menu Utama", callback_data="menu_main")])
    return InlineKeyboardMarkup(rows)

def kb_settings():
    s = settings
    rows = [
        [InlineKeyboardButton(f"📦 Lot Size: {s['lot_size']}",         callback_data="set_lot_size")],
        [InlineKeyboardButton(f"📉 Layer Trigger: ${s['layer_trigger']}",  callback_data="set_layer_trigger")],
        [InlineKeyboardButton(f"🎯 Take Profit: ${s['total_tp']}",      callback_data="set_total_tp")],
        [InlineKeyboardButton(f"⏱ Interval: {s['check_interval']}s",   callback_data="set_check_interval")],
        [InlineKeyboardButton(f"📊 EMA Fast: {s['ema_fast']}",          callback_data="set_ema_fast")],
        [InlineKeyboardButton(f"📊 EMA Slow: {s['ema_slow']}",          callback_data="set_ema_slow")],
        [InlineKeyboardButton("🏠 Menu Utama", callback_data="menu_main")],
    ]
    return InlineKeyboardMarkup(rows)

def kb_confirm_closeall():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Ya, Close Semua", callback_data="do_closeall"),
         InlineKeyboardButton("❌ Batal",           callback_data="menu_main")],
    ])

def kb_back():
    return InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Menu Utama", callback_data="menu_main")]])


# ══════════════════════════════════════════
# CONTENT BUILDERS
# ══════════════════════════════════════════

def text_main():
    active_count = sum(1 for p in ALL_PAIRS if pair_active.get(p))
    return (
        "🤖 *Forex Trading Bot*\n\n"
        f"Pair aktif: `{active_count}/{len(ALL_PAIRS)}`\n"
        f"Lot: `{em(str(settings['lot_size']))}` \\| TP: `{em(f'${settings[\"total_tp\"]}')}` \\| Layer: `{em(f'${settings[\"layer_trigger\"]}')}` \n"
        f"EMA: `{settings['ema_fast']}/{settings['ema_slow']}` \\| Interval: `{settings['check_interval']}s`\n\n"
        "Pilih menu:"
    )

def text_settings():
    s = settings
    return (
        "⚙️ *Setting Bot*\n\n"
        f"📦 Lot Size: `{em(str(s['lot_size']))}`\n"
        f"📉 Layer Trigger: `{em(f'${s[\"layer_trigger\"]}')}` _\\(entry lagi kalau posisi terakhir floating di bawah ini\\)_\n"
        f"🎯 Take Profit: `{em(f'${s[\"total_tp\"]}')}` _\\(close semua kalau total profit di atas ini\\)_\n"
        f"⏱ Check Interval: `{s['check_interval']} detik`\n"
        f"📊 EMA Fast: `{s['ema_fast']}`\n"
        f"📊 EMA Slow: `{s['ema_slow']}`\n\n"
        "Ketuk setting yang ingin diubah:"
    )

async def text_positions():
    lines = ["📈 *Status Posisi*\n"]
    any_trade = False
    grand_total = 0.0
    for pair in ALL_PAIRS:
        try:
            ot = get_open_trades(pair)
            if not ot:
                continue
            any_trade  = True
            total_pl   = get_total_pl(ot)
            grand_total += total_pl
            n_buy, n_sell = count_buy_sell(ot)
            icon = "✅" if pair_active.get(pair) else "⭕"
            pl_color = "📈" if total_pl >= 0 else "📉"
            lines.append(
                f"{icon} *{em(pair_label(pair))}*\n"
                f"  {pl_color} Buy: `{n_buy}` \\| Sell: `{n_sell}` \\| P/L: `{em(f'${total_pl:.2f}')}`"
            )
        except Exception:
            pass
    if not any_trade:
        lines.append("_Tidak ada posisi terbuka_")
    else:
        lines.append(f"\n💰 *Grand Total P/L: `{em(f'${grand_total:.2f}')}`*")
    return "\n".join(lines)

async def text_account():
    try:
        info = get_account_info()
        active_count = sum(1 for p in ALL_PAIRS if pair_active.get(p))
        return (
            "📊 *Status Akun*\n\n"
            f"💰 Balance: `{em(f'${info[\"balance\"]:.2f}')}`\n"
            f"📈 NAV: `{em(f'${info[\"nav\"]:.2f}')}`\n"
            f"📉 Floating P/L: `{em(f'${info[\"pl\"]:.2f}')}`\n"
            f"🔒 Margin Used: `{em(f'${info[\"margin\"]:.2f}')}`\n"
            f"🟢 Free Margin: `{em(f'${info[\"free_margin\"]:.2f}')}`\n\n"
            f"Bot aktif di `{active_count}` pair\n"
            f"Environment: `{OANDA_ENV}`"
        )
    except Exception as e:
        return f"❌ Gagal ambil info akun: `{em(str(e))}`"


# ══════════════════════════════════════════
# COMMAND HANDLERS
# ══════════════════════════════════════════

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    await update.message.reply_text(text_main(), parse_mode="MarkdownV2", reply_markup=kb_main())


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.from_user.id != ALLOWED_USER_ID:
        return

    data = query.data

    # ── Menu utama ──
    if data == "menu_main":
        await query.edit_message_text(text_main(), parse_mode="MarkdownV2", reply_markup=kb_main())

    # ── Pairs ──
    elif data == "menu_pairs":
        active_count = sum(1 for p in ALL_PAIRS if pair_active.get(p))
        await query.edit_message_text(
            f"📋 *Kelola Pair*\n\n✅ \\= Aktif \\| ⭕ \\= Nonaktif\nAktif: `{active_count}/{len(ALL_PAIRS)}`\n\nKetuk pair untuk toggle:",
            parse_mode="MarkdownV2", reply_markup=kb_pairs(0)
        )

    elif data.startswith("pairs_page_"):
        page = int(data[len("pairs_page_"):])
        active_count = sum(1 for p in ALL_PAIRS if pair_active.get(p))
        await query.edit_message_text(
            f"📋 *Kelola Pair*\n\n✅ \\= Aktif \\| ⭕ \\= Nonaktif\nAktif: `{active_count}/{len(ALL_PAIRS)}`\n\nKetuk pair untuk toggle:",
            parse_mode="MarkdownV2", reply_markup=kb_pairs(page)
        )

    elif data.startswith("toggle_"):
        pair = data[len("toggle_"):]
        if pair_active.get(pair):
            stop_pair(pair)
            status = "⏹ Dinonaktifkan"
        else:
            start_pair(pair, context.application)
            status = "✅ Diaktifkan"
        await query.answer(f"{pair_label(pair)} {status}", show_alert=False)
        active_count = sum(1 for p in ALL_PAIRS if pair_active.get(p))
        try:
            await query.edit_message_text(
                f"📋 *Kelola Pair*\n\n✅ \\= Aktif \\| ⭕ \\= Nonaktif\nAktif: `{active_count}/{len(ALL_PAIRS)}`\n\nKetuk pair untuk toggle:",
                parse_mode="MarkdownV2", reply_markup=kb_pairs(0)
            )
        except Exception:
            pass

    elif data == "all_on":
        for pair in ALL_PAIRS:
            start_pair(pair, context.application)
        await query.answer("✅ Semua pair diaktifkan!", show_alert=True)
        await query.edit_message_text(text_main(), parse_mode="MarkdownV2", reply_markup=kb_main())

    elif data == "all_off":
        for pair in ALL_PAIRS:
            stop_pair(pair)
        await query.answer("⏹ Semua pair dinonaktifkan!", show_alert=True)
        await query.edit_message_text(text_main(), parse_mode="MarkdownV2", reply_markup=kb_main())

    # ── Settings ──
    elif data == "menu_settings":
        await query.edit_message_text(text_settings(), parse_mode="MarkdownV2", reply_markup=kb_settings())

    elif data.startswith("set_"):
        key = data[len("set_"):]
        pending_setting_key[query.from_user.id] = key
        labels = {
            "lot_size":       f"Lot Size (sekarang: {settings['lot_size']})\nContoh: 0.01 atau 0.1",
            "layer_trigger":  f"Layer Trigger dalam $ (sekarang: {settings['layer_trigger']})\nHarus negatif, contoh: -2",
            "total_tp":       f"Take Profit dalam $ (sekarang: {settings['total_tp']})\nContoh: 10",
            "check_interval": f"Interval cek dalam detik (sekarang: {settings['check_interval']})\nContoh: 60",
            "ema_fast":       f"EMA Fast period (sekarang: {settings['ema_fast']})\nContoh: 20",
            "ema_slow":       f"EMA Slow period (sekarang: {settings['ema_slow']})\nContoh: 50",
        }
        await query.edit_message_text(
            f"✏️ *Ubah {em(key)}*\n\n{em(labels.get(key, key))}\n\nKirim nilai baru:",
            parse_mode="MarkdownV2",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Batal", callback_data="menu_settings")]])
        )

    # ── Account ──
    elif data == "menu_account":
        txt = await text_account()
        await query.edit_message_text(txt, parse_mode="MarkdownV2", reply_markup=kb_back())

    # ── Positions ──
    elif data == "menu_positions":
        txt = await text_positions()
        await query.edit_message_text(txt, parse_mode="MarkdownV2", reply_markup=kb_back())

    # ── Close all ──
    elif data == "confirm_closeall":
        await query.edit_message_text(
            "⚠️ *Yakin mau close SEMUA posisi?*\nAksi ini tidak bisa dibatalkan\\.",
            parse_mode="MarkdownV2", reply_markup=kb_confirm_closeall()
        )

    elif data == "do_closeall":
        closed = 0
        total  = 0.0
        for pair in ALL_PAIRS:
            try:
                ot = get_open_trades(pair)
                if ot:
                    total  += get_total_pl(ot)
                    closed += len(ot)
                    close_all_trades(pair, ot)
            except Exception as e:
                logger.error(f"closeall [{pair}]: {e}")
        pl_s = em(f"${total:.2f}")
        await query.edit_message_text(
            f"✅ *Selesai\\!*\nDitutup: `{closed}` posisi\nTotal P/L: `{pl_s}`",
            parse_mode="MarkdownV2", reply_markup=kb_back()
        )

    elif data == "noop":
        pass


# ── Terima nilai setting baru ──
async def receive_setting_value(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    user_id = update.effective_user.id
    key     = pending_setting_key.get(user_id)
    if not key:
        return

    text = update.message.text.strip()
    try:
        value = float(text)
        # Validasi
        if key == "lot_size" and value <= 0:
            raise ValueError("Lot harus > 0")
        if key == "layer_trigger" and value >= 0:
            raise ValueError("Layer trigger harus negatif")
        if key == "total_tp" and value <= 0:
            raise ValueError("TP harus > 0")
        if key == "check_interval" and value < 10:
            raise ValueError("Interval minimal 10 detik")
        if key in ("ema_fast", "ema_slow") and value < 2:
            raise ValueError("EMA period minimal 2")
        if key == "ema_fast" and value >= settings["ema_slow"]:
            raise ValueError("EMA Fast harus lebih kecil dari EMA Slow")
        if key == "ema_slow" and value <= settings["ema_fast"]:
            raise ValueError("EMA Slow harus lebih besar dari EMA Fast")

        if key in ("ema_fast", "ema_slow", "check_interval"):
            settings[key] = int(value)
        else:
            settings[key] = value

        del pending_setting_key[user_id]
        await update.message.reply_text(
            f"✅ *{em(key)}* diubah ke `{em(str(settings[key]))}`",
            parse_mode="MarkdownV2",
            reply_markup=kb_settings()
        )
        # Kirim ulang menu setting
        await update.message.reply_text(text_settings(), parse_mode="MarkdownV2", reply_markup=kb_settings())

    except ValueError as e:
        await update.message.reply_text(
            f"❌ Nilai tidak valid: `{em(str(e))}`\nCoba lagi:",
            parse_mode="MarkdownV2"
        )


# ══════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════

def main():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start",  start_command))
    app.add_handler(CommandHandler("menu",   start_command))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, receive_setting_value))
    logger.info("Bot siap.")
    app.run_polling()

if __name__ == "__main__":
    main()
