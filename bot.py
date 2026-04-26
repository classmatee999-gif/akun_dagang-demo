import asyncio
import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes, CallbackQueryHandler, MessageHandler, filters
import oandapyV20
from oandapyV20.endpoints import orders, trades, instruments, accounts
import pandas as pd
import os

TELEGRAM_TOKEN  = os.environ.get("8721338927:AAFwI8cYBQiuIc14eH03FNZKrE1-ooIUJj4")
OANDA_TOKEN     = os.environ.get("0bfbe1cc9698a5b93a60b46b5bae86c9-34b23230db37270e68d96d5bc0b256ac")
ACCOUNT_ID      = os.environ.get("101-001-29134814-001")
ALLOWED_USER_ID = int(os.environ.get("ALLOWED_USER_ID", "6903511431"))
OANDA_ENV       = os.environ.get("OANDA_ENV", "practice")

ALL_PAIRS = [
    "EUR_USD", "GBP_USD", "USD_JPY", "USD_CHF", "AUD_USD",
    "USD_CAD", "NZD_USD", "EUR_GBP", "EUR_JPY", "GBP_JPY",
    "AUD_JPY", "EUR_AUD", "EUR_CAD", "GBP_CAD", "CAD_JPY",
]

settings = {
    "lot_size":       0.01,
    "layer_trigger":  -2.0,
    "total_tp":       10.0,
    "check_interval": 60,
    "ema_fast":       20,
    "ema_slow":       50,
}

pair_active = {p: False for p in ALL_PAIRS}
pair_tasks  = {}
pair_state  = {p: {"last_signal": None, "waiting_cross": None} for p in ALL_PAIRS}
pending_setting_key = {}

logging.basicConfig(format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)
client = oandapyV20.API(access_token=OANDA_TOKEN, environment=OANDA_ENV)


# ── Utils ──

def pair_label(pair):
    return pair.replace("_", "/")

def em(text):
    text = str(text)
    for ch in r"_*[]()~`>#+-=|{}.!":
        text = text.replace(ch, "\\" + ch)
    return text

def is_allowed(update):
    return update.effective_user.id == ALLOWED_USER_ID


# ── OANDA ──

def get_candles(pair, count=60):
    r = instruments.InstrumentsCandles(pair, params={"count": count, "granularity": "D"})
    client.request(r)
    return [float(c["mid"]["c"]) for c in r.response["candles"] if c["complete"]]

def calculate_ema(data, period):
    return pd.Series(data).ewm(span=period, adjust=False).mean().tolist()

def get_ema_signal(pair):
    closes = get_candles(pair, 60)
    fast, slow = settings["ema_fast"], settings["ema_slow"]
    if len(closes) < slow + 2:
        return None
    ef = calculate_ema(closes, fast)
    es = calculate_ema(closes, slow)
    c_f, c_s = ef[-1], es[-1]
    p_f, p_s = ef[-2], es[-2]
    if p_f <= p_s and c_f > c_s:  return "buy"
    if p_f >= p_s and c_f < c_s:  return "sell"
    if c_f > c_s:                  return "buy_active"
    if c_f < c_s:                  return "sell_active"
    return None

def get_open_trades(pair):
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

def place_order(pair, direction):
    units = str(int(settings["lot_size"] * 100000))
    if direction == "sell":
        units = "-" + units
    r = orders.OrderCreate(ACCOUNT_ID, data={"order": {"type": "MARKET", "instrument": pair, "units": units}})
    client.request(r)
    logger.info("[%s] %s placed.", pair, direction.upper())

def close_all_trades(pair, open_trades):
    for trade in open_trades:
        try:
            client.request(trades.TradeClose(ACCOUNT_ID, tradeID=trade["id"]))
        except Exception as e:
            logger.error("[%s] close error: %s", pair, e)

def get_account_info():
    r = accounts.AccountSummary(ACCOUNT_ID)
    client.request(r)
    a = r.response["account"]
    return {
        "balance":      float(a["balance"]),
        "nav":          float(a["NAV"]),
        "pl":           float(a["unrealizedPL"]),
        "margin":       float(a["marginUsed"]),
        "free_margin":  float(a["marginAvailable"]),
    }


# ── Trading Loop ──

async def trading_loop_pair(pair, app):
    state = pair_state[pair]
    logger.info("[%s] Loop start.", pair)
    while pair_active.get(pair, False):
        try:
            signal        = get_ema_signal(pair)
            open_trades   = get_open_trades(pair)
            n_buy, n_sell = count_buy_sell(open_trades)
            total_pl      = get_total_pl(open_trades)
            tp            = settings["total_tp"]
            trigger       = settings["layer_trigger"]
            lbl           = em(pair_label(pair))
            pl_s          = em("${:.2f}".format(total_pl))

            if open_trades and total_pl >= tp:
                close_all_trades(pair, open_trades)
                await app.bot.send_message(ALLOWED_USER_ID,
                    "*{}* \u2014 TP tercapai\\!\nProfit: `{}`".format(lbl, pl_s),
                    parse_mode="MarkdownV2")
                state["last_signal"] = None
                state["waiting_cross"] = None
                await asyncio.sleep(settings["check_interval"])
                continue

            if open_trades:
                if n_buy > 0 and signal in ("sell", "sell_active"):
                    close_all_trades(pair, open_trades)
                    await app.bot.send_message(ALLOWED_USER_ID,
                        "\U0001f504 *{}* \u2014 Death Cross\\!\nBUY ditutup\\. P/L: `{}`".format(lbl, pl_s),
                        parse_mode="MarkdownV2")
                    state["last_signal"] = None
                    state["waiting_cross"] = "sell"
                    await asyncio.sleep(settings["check_interval"])
                    continue
                if n_sell > 0 and signal in ("buy", "buy_active"):
                    close_all_trades(pair, open_trades)
                    await app.bot.send_message(ALLOWED_USER_ID,
                        "\U0001f504 *{}* \u2014 Golden Cross\\!\nSELL ditutup\\. P/L: `{}`".format(lbl, pl_s),
                        parse_mode="MarkdownV2")
                    state["last_signal"] = None
                    state["waiting_cross"] = "buy"
                    await asyncio.sleep(settings["check_interval"])
                    continue

            if signal == "buy" and state["last_signal"] != "buy" and n_buy == 0 and state["waiting_cross"] != "sell":
                place_order(pair, "buy")
                state["last_signal"] = "buy"
                state["waiting_cross"] = None
                lot = em(str(settings["lot_size"]))
                await app.bot.send_message(ALLOWED_USER_ID,
                    "\U0001f4c8 *{}* \u2014 ENTRY BUY \\#1\nGolden Cross \\| Lot: `{}`".format(lbl, lot),
                    parse_mode="MarkdownV2")

            elif signal == "sell" and state["last_signal"] != "sell" and n_sell == 0 and state["waiting_cross"] != "buy":
                place_order(pair, "sell")
                state["last_signal"] = "sell"
                state["waiting_cross"] = None
                lot = em(str(settings["lot_size"]))
                await app.bot.send_message(ALLOWED_USER_ID,
                    "\U0001f4c9 *{}* \u2014 ENTRY SELL \\#1\nDeath Cross \\| Lot: `{}`".format(lbl, lot),
                    parse_mode="MarkdownV2")

            elif open_trades:
                last_pl = get_last_trade_pl(open_trades)
                if last_pl is not None and last_pl <= trigger:
                    ls = em("${:.2f}".format(last_pl))
                    if n_buy > 0 and signal in ("buy", "buy_active"):
                        place_order(pair, "buy")
                        await app.bot.send_message(ALLOWED_USER_ID,
                            "\U0001f4c8 *{}* \u2014 LAYER BUY \\#{}\nLast: `{}` \\| Total: `{}`".format(lbl, n_buy+1, ls, pl_s),
                            parse_mode="MarkdownV2")
                    elif n_sell > 0 and signal in ("sell", "sell_active"):
                        place_order(pair, "sell")
                        await app.bot.send_message(ALLOWED_USER_ID,
                            "\U0001f4c9 *{}* \u2014 LAYER SELL \\#{}\nLast: `{}` \\| Total: `{}`".format(lbl, n_sell+1, ls, pl_s),
                            parse_mode="MarkdownV2")

        except Exception as e:
            logger.error("[%s] %s", pair, e)
            try:
                await app.bot.send_message(ALLOWED_USER_ID,
                    "\u26a0\ufe0f *{}* error: `{}`".format(em(pair_label(pair)), em(str(e))),
                    parse_mode="MarkdownV2")
            except Exception:
                pass

        await asyncio.sleep(settings["check_interval"])
    logger.info("[%s] Loop stop.", pair)


def start_pair(pair, app):
    if pair_active.get(pair):
        return False
    pair_active[pair] = True
    pair_state[pair]  = {"last_signal": None, "waiting_cross": None}
    pair_tasks[pair]  = asyncio.create_task(trading_loop_pair(pair, app))
    return True

def stop_pair(pair):
    if not pair_active.get(pair):
        return False
    pair_active[pair] = False
    if pair in pair_tasks:
        pair_tasks[pair].cancel()
        del pair_tasks[pair]
    return True


# ── Keyboards ──

def kb_main():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📋 Kelola Pair",     callback_data="menu_pairs"),
         InlineKeyboardButton("⚙️ Setting",         callback_data="menu_settings")],
        [InlineKeyboardButton("📊 Status Akun",     callback_data="menu_account"),
         InlineKeyboardButton("📈 Status Posisi",   callback_data="menu_positions")],
        [InlineKeyboardButton("▶️ ON Semua Pair",   callback_data="all_on"),
         InlineKeyboardButton("⏹ OFF Semua Pair",  callback_data="all_off")],
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
        rows.append([InlineKeyboardButton("{} {}".format(icon, pair_label(pair)), callback_data="toggle_" + pair)])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀️", callback_data="pairs_page_{}".format(page-1)))
    nav.append(InlineKeyboardButton("{}/{}".format(page+1, total_pages), callback_data="noop"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton("▶️", callback_data="pairs_page_{}".format(page+1)))
    rows.append(nav)
    rows.append([InlineKeyboardButton("🏠 Menu Utama", callback_data="menu_main")])
    return InlineKeyboardMarkup(rows)

def kb_settings():
    s = settings
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📦 Lot Size: {}".format(s["lot_size"]),          callback_data="set_lot_size")],
        [InlineKeyboardButton("📉 Layer Trigger: ${}".format(s["layer_trigger"]), callback_data="set_layer_trigger")],
        [InlineKeyboardButton("🎯 Take Profit: ${}".format(s["total_tp"]),       callback_data="set_total_tp")],
        [InlineKeyboardButton("⏱ Interval: {}s".format(s["check_interval"]),    callback_data="set_check_interval")],
        [InlineKeyboardButton("📊 EMA Fast: {}".format(s["ema_fast"]),           callback_data="set_ema_fast")],
        [InlineKeyboardButton("📊 EMA Slow: {}".format(s["ema_slow"]),           callback_data="set_ema_slow")],
        [InlineKeyboardButton("🏠 Menu Utama", callback_data="menu_main")],
    ])

def kb_confirm_closeall():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Ya, Close Semua", callback_data="do_closeall"),
         InlineKeyboardButton("❌ Batal",            callback_data="menu_main")],
    ])

def kb_back():
    return InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Menu Utama", callback_data="menu_main")]])


# ── Text builders ──

def text_main():
    active_count = sum(1 for p in ALL_PAIRS if pair_active.get(p))
    s = settings
    return (
        "🤖 *Forex Trading Bot*\n\n"
        "Pair aktif: `{}/{}`\n"
        "Lot: `{}` \\| TP: `${}` \\| Layer: `${}`\n"
        "EMA: `{}/{}` \\| Interval: `{}s`\n\n"
        "Pilih menu:"
    ).format(
        active_count, len(ALL_PAIRS),
        em(str(s["lot_size"])),
        em(str(s["total_tp"])),
        em(str(s["layer_trigger"])),
        s["ema_fast"], s["ema_slow"],
        s["check_interval"]
    )

def text_settings():
    s = settings
    return (
        "⚙️ *Setting Bot*\n\n"
        "📦 Lot Size: `{}`\n"
        "📉 Layer Trigger: `${}` _\\(posisi terakhir floating di bawah ini → entry lagi\\)_\n"
        "🎯 Take Profit: `${}` _\\(total profit di atas ini → close semua\\)_\n"
        "⏱ Interval: `{} detik`\n"
        "📊 EMA Fast: `{}`\n"
        "📊 EMA Slow: `{}`\n\n"
        "Ketuk setting yang ingin diubah:"
    ).format(
        em(str(s["lot_size"])),
        em(str(s["layer_trigger"])),
        em(str(s["total_tp"])),
        s["check_interval"],
        s["ema_fast"],
        s["ema_slow"]
    )

async def text_positions():
    lines = ["📈 *Status Posisi*\n"]
    grand_total = 0.0
    any_trade   = False
    for pair in ALL_PAIRS:
        try:
            ot = get_open_trades(pair)
            if not ot:
                continue
            any_trade     = True
            total_pl      = get_total_pl(ot)
            grand_total  += total_pl
            n_buy, n_sell = count_buy_sell(ot)
            icon  = "✅" if pair_active.get(pair) else "⭕"
            arrow = "📈" if total_pl >= 0 else "📉"
            lines.append(
                "{} *{}*\n  {} Buy: `{}` \\| Sell: `{}` \\| P/L: `{}`".format(
                    icon, em(pair_label(pair)), arrow, n_buy, n_sell,
                    em("${:.2f}".format(total_pl))
                )
            )
        except Exception:
            pass
    if not any_trade:
        lines.append("_Tidak ada posisi terbuka_")
    else:
        lines.append("\n💰 *Grand Total P/L: `{}`*".format(em("${:.2f}".format(grand_total))))
    return "\n".join(lines)

async def text_account():
    try:
        info = get_account_info()
        active_count = sum(1 for p in ALL_PAIRS if pair_active.get(p))
        return (
            "📊 *Status Akun*\n\n"
            "💰 Balance: `{}`\n"
            "📈 NAV: `{}`\n"
            "📉 Floating P/L: `{}`\n"
            "🔒 Margin Used: `{}`\n"
            "🟢 Free Margin: `{}`\n\n"
            "Pair aktif: `{}`\n"
            "Environment: `{}`"
        ).format(
            em("${:.2f}".format(info["balance"])),
            em("${:.2f}".format(info["nav"])),
            em("${:.2f}".format(info["pl"])),
            em("${:.2f}".format(info["margin"])),
            em("${:.2f}".format(info["free_margin"])),
            active_count,
            OANDA_ENV
        )
    except Exception as e:
        return "❌ Gagal ambil info akun: `{}`".format(em(str(e)))


# ── Handlers ──

async def start_command(update, context):
    if not is_allowed(update):
        return
    await update.message.reply_text(text_main(), parse_mode="MarkdownV2", reply_markup=kb_main())


async def button_handler(update, context):
    query = update.callback_query
    await query.answer()
    if query.from_user.id != ALLOWED_USER_ID:
        return
    data = query.data

    if data == "menu_main":
        await query.edit_message_text(text_main(), parse_mode="MarkdownV2", reply_markup=kb_main())

    elif data == "menu_pairs":
        active_count = sum(1 for p in ALL_PAIRS if pair_active.get(p))
        await query.edit_message_text(
            "📋 *Kelola Pair*\n\n✅ \\= Aktif \\| ⭕ \\= Nonaktif\nAktif: `{}/{}`\n\nKetuk pair untuk toggle:".format(active_count, len(ALL_PAIRS)),
            parse_mode="MarkdownV2", reply_markup=kb_pairs(0))

    elif data.startswith("pairs_page_"):
        page = int(data[len("pairs_page_"):])
        active_count = sum(1 for p in ALL_PAIRS if pair_active.get(p))
        await query.edit_message_text(
            "📋 *Kelola Pair*\n\n✅ \\= Aktif \\| ⭕ \\= Nonaktif\nAktif: `{}/{}`\n\nKetuk pair untuk toggle:".format(active_count, len(ALL_PAIRS)),
            parse_mode="MarkdownV2", reply_markup=kb_pairs(page))

    elif data.startswith("toggle_"):
        pair = data[len("toggle_"):]
        if pair_active.get(pair):
            stop_pair(pair)
            await query.answer("{} ⏹ Dinonaktifkan".format(pair_label(pair)), show_alert=False)
        else:
            start_pair(pair, context.application)
            await query.answer("{} ✅ Diaktifkan".format(pair_label(pair)), show_alert=False)
        active_count = sum(1 for p in ALL_PAIRS if pair_active.get(p))
        try:
            await query.edit_message_text(
                "📋 *Kelola Pair*\n\n✅ \\= Aktif \\| ⭕ \\= Nonaktif\nAktif: `{}/{}`\n\nKetuk pair untuk toggle:".format(active_count, len(ALL_PAIRS)),
                parse_mode="MarkdownV2", reply_markup=kb_pairs(0))
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

    elif data == "menu_settings":
        await query.edit_message_text(text_settings(), parse_mode="MarkdownV2", reply_markup=kb_settings())

    elif data.startswith("set_"):
        key = data[len("set_"):]
        pending_setting_key[query.from_user.id] = key
        labels = {
            "lot_size":       "Lot Size (sekarang: {})\nContoh: 0.01".format(settings["lot_size"]),
            "layer_trigger":  "Layer Trigger dalam $ (sekarang: {})\nHarus negatif, contoh: -2".format(settings["layer_trigger"]),
            "total_tp":       "Take Profit dalam $ (sekarang: {})\nContoh: 10".format(settings["total_tp"]),
            "check_interval": "Interval cek dalam detik (sekarang: {})\nContoh: 60".format(settings["check_interval"]),
            "ema_fast":       "EMA Fast period (sekarang: {})\nContoh: 20".format(settings["ema_fast"]),
            "ema_slow":       "EMA Slow period (sekarang: {})\nContoh: 50".format(settings["ema_slow"]),
        }
        await query.edit_message_text(
            "✏️ *Ubah {}*\n\n{}\n\nKirim nilai baru:".format(em(key), em(labels.get(key, key))),
            parse_mode="MarkdownV2",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Batal", callback_data="menu_settings")]]))

    elif data == "menu_account":
        txt = await text_account()
        await query.edit_message_text(txt, parse_mode="MarkdownV2", reply_markup=kb_back())

    elif data == "menu_positions":
        txt = await text_positions()
        await query.edit_message_text(txt, parse_mode="MarkdownV2", reply_markup=kb_back())

    elif data == "confirm_closeall":
        await query.edit_message_text(
            "⚠️ *Yakin mau close SEMUA posisi?*\nAksi ini tidak bisa dibatalkan\\.",
            parse_mode="MarkdownV2", reply_markup=kb_confirm_closeall())

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
                logger.error("closeall [%s]: %s", pair, e)
        await query.edit_message_text(
            "✅ *Selesai\\!*\nDitutup: `{}` posisi\nTotal P/L: `{}`".format(closed, em("${:.2f}".format(total))),
            parse_mode="MarkdownV2", reply_markup=kb_back())

    elif data == "noop":
        pass


async def receive_setting_value(update, context):
    if not is_allowed(update):
        return
    user_id = update.effective_user.id
    key     = pending_setting_key.get(user_id)
    if not key:
        return
    text = update.message.text.strip()
    try:
        value = float(text)
        if key == "lot_size" and value <= 0:
            raise ValueError("Lot harus > 0")
        if key == "layer_trigger" and value >= 0:
            raise ValueError("Layer trigger harus negatif, contoh: -2")
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

        settings[key] = int(value) if key in ("ema_fast", "ema_slow", "check_interval") else value
        del pending_setting_key[user_id]

        await update.message.reply_text(
            "✅ *{}* diubah ke `{}`".format(em(key), em(str(settings[key]))),
            parse_mode="MarkdownV2")
        await update.message.reply_text(text_settings(), parse_mode="MarkdownV2", reply_markup=kb_settings())

    except ValueError as e:
        await update.message.reply_text(
            "❌ Nilai tidak valid: `{}`\nCoba lagi:".format(em(str(e))),
            parse_mode="MarkdownV2")


def main():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("menu",  start_command))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, receive_setting_value))
    logger.info("Bot siap.")
    app.run_polling()

if __name__ == "__main__":
    main()
