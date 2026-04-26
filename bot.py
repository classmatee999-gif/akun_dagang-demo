import asyncio
import logging
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes, CallbackQueryHandler, MessageHandler, filters
import oandapyV20
from oandapyV20.endpoints import orders, trades, instruments, accounts
import pandas as pd
import os

# ─────────────────────────────────────────
# KONFIGURASI via Environment Variables
# ─────────────────────────────────────────
TELEGRAM_TOKEN  = os.environ.get("TELEGRAM_TOKEN", "")
OANDA_TOKEN     = os.environ.get("OANDA_TOKEN", "")
ACCOUNT_ID      = os.environ.get("ACCOUNT_ID", "")
ALLOWED_USER_ID = int(os.environ.get("ALLOWED_USER_ID", "0"))
OANDA_ENV       = os.environ.get("OANDA_ENV", "practice")

ALL_PAIRS = [
    "EUR_USD", "GBP_USD", "USD_JPY", "USD_CHF", "AUD_USD",
    "USD_CAD", "NZD_USD", "EUR_GBP", "EUR_JPY", "GBP_JPY",
    "AUD_JPY", "EUR_AUD", "EUR_CAD", "GBP_CAD", "CAD_JPY",
]

# ── Setting default (bisa diubah via Telegram) ──
settings = {
    "lot_size":         0.01,
    "layer_trigger":    -2.0,
    "total_tp":         10.0,
    "check_interval":   60,
    "ema_fast":         20,
    "ema_slow":         50,
    "max_layers":       10,       # Batas maksimal layer per pair
    "daily_loss_limit": -50.0,    # Bot otomatis berhenti jika total floating semua pair <= nilai ini
    "notify_interval":  3600,     # Kirim ringkasan otomatis setiap X detik (0 = nonaktif)
}

# ── State ──
pair_active         = {p: False for p in ALL_PAIRS}
pair_tasks          = {}
pair_state          = {p: {"last_signal": None, "waiting_cross": None} for p in ALL_PAIRS}
pending_setting_key = {}
bot_start_time      = None
trade_log           = []          # Log semua trade yang terjadi
daily_stats         = {"trades": 0, "wins": 0, "losses": 0, "total_pl": 0.0}
emergency_stop      = False       # Kill switch darurat

logging.basicConfig(format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)
client = oandapyV20.API(access_token=OANDA_TOKEN, environment=OANDA_ENV)


# ══════════════════════════════════════════
# UTILS
# ══════════════════════════════════════════

def pair_label(pair):
    return pair.replace("_", "/")

def em(text):
    text = str(text)
    for ch in r"_*[]()~`>#+-=|{}.!":
        text = text.replace(ch, "\\" + ch)
    return text

def is_allowed(update):
    return update.effective_user.id == ALLOWED_USER_ID

def uptime_str():
    if not bot_start_time:
        return "N/A"
    delta = datetime.now() - bot_start_time
    h, rem = divmod(int(delta.total_seconds()), 3600)
    m, s   = divmod(rem, 60)
    return "{}j {}m {}d".format(h, m, s)

def log_trade(pair, direction, action, pl=None):
    entry = {
        "time":      datetime.now().strftime("%H:%M:%S"),
        "pair":      pair_label(pair),
        "direction": direction,
        "action":    action,
        "pl":        pl,
    }
    trade_log.append(entry)
    if len(trade_log) > 100:
        trade_log.pop(0)
    if pl is not None:
        daily_stats["trades"] += 1
        daily_stats["total_pl"] += pl
        if pl >= 0:
            daily_stats["wins"] += 1
        else:
            daily_stats["losses"] += 1


# ══════════════════════════════════════════
# OANDA
# ══════════════════════════════════════════

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

def get_all_open_trades():
    r = trades.TradesList(ACCOUNT_ID, params={"state": "OPEN"})
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
    total_pl = get_total_pl(open_trades)
    for trade in open_trades:
        try:
            client.request(trades.TradeClose(ACCOUNT_ID, tradeID=trade["id"]))
        except Exception as e:
            logger.error("[%s] close error: %s", pair, e)
    return total_pl

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
        "margin_rate":  float(a["marginCloseoutPercent"]) * 100 if "marginCloseoutPercent" in a else 0,
    }

def get_price(pair):
    r = instruments.InstrumentsCandles(pair, params={"count": 1, "granularity": "S5"})
    client.request(r)
    c = r.response["candles"][-1]["mid"]
    return float(c["c"])


# ══════════════════════════════════════════
# DAILY LOSS LIMIT MONITOR
# ══════════════════════════════════════════

async def monitor_daily_loss(app):
    """Monitor global daily loss limit. Jika tercapai, stop semua pair."""
    global emergency_stop
    while True:
        try:
            if emergency_stop:
                await asyncio.sleep(60)
                continue
            # Skip jika tidak ada pair aktif
            if not any(pair_active.values()):
                await asyncio.sleep(30)
                continue
            all_trades = get_all_open_trades()
            total_pl   = sum(float(t["unrealizedPL"]) for t in all_trades)
            limit      = settings["daily_loss_limit"]
            if total_pl <= limit:
                emergency_stop = True
                for pair in ALL_PAIRS:
                    stop_pair(pair)
                await app.bot.send_message(ALLOWED_USER_ID,
                    "🚨 *EMERGENCY STOP\\!*\n\n"
                    "Daily loss limit tercapai\\!\n"
                    "Total floating: `{}`\n"
                    "Limit: `{}`\n\n"
                    "Semua pair dihentikan\\. Posisi dibiarkan terbuka\\.\n"
                    "Gunakan /menu untuk mengaktifkan ulang\\.".format(
                        em("${:.2f}".format(total_pl)),
                        em("${:.2f}".format(limit))
                    ),
                    parse_mode="MarkdownV2")
        except Exception as e:
            logger.error("monitor_daily_loss: %s", e)
        await asyncio.sleep(30)


# ══════════════════════════════════════════
# AUTO SUMMARY NOTIFIKASI
# ══════════════════════════════════════════

async def auto_summary(app):
    """Kirim ringkasan otomatis setiap notify_interval detik."""
    while True:
        interval = settings["notify_interval"]
        if interval <= 0:
            await asyncio.sleep(60)
            continue
        await asyncio.sleep(interval)
        try:
            all_trades   = get_all_open_trades()
            total_pl     = sum(float(t["unrealizedPL"]) for t in all_trades)
            active_count = sum(1 for p in ALL_PAIRS if pair_active.get(p))
            try:
                info = get_account_info()
                balance = info["balance"]
            except Exception:
                balance = 0.0

            await app.bot.send_message(ALLOWED_USER_ID,
                "⏰ *Ringkasan Otomatis*\n\n"
                "🤖 Uptime: `{}`\n"
                "💰 Balance: `{}`\n"
                "📊 Floating P/L: `{}`\n"
                "📋 Pair aktif: `{}/{}`\n"
                "🔄 Total posisi: `{}`\n\n"
                "📈 Sesi ini:\n"
                "  Trade: `{}` \\| Win: `{}` \\| Loss: `{}`\n"
                "  Total P/L sesi: `{}`".format(
                    em(uptime_str()),
                    em("${:.2f}".format(balance)),
                    em("${:.2f}".format(total_pl)),
                    active_count, len(ALL_PAIRS),
                    len(all_trades),
                    daily_stats["trades"],
                    daily_stats["wins"],
                    daily_stats["losses"],
                    em("${:.2f}".format(daily_stats["total_pl"]))
                ),
                parse_mode="MarkdownV2")
        except Exception as e:
            logger.error("auto_summary: %s", e)


# ══════════════════════════════════════════
# TRADING LOOP PER PAIR
# ══════════════════════════════════════════

async def trading_loop_pair(pair, app):
    global emergency_stop
    state = pair_state[pair]
    logger.info("[%s] Loop start.", pair)

    while pair_active.get(pair, False):
        if emergency_stop:
            await asyncio.sleep(60)
            continue
        try:
            signal        = get_ema_signal(pair)
            open_trades   = get_open_trades(pair)
            n_buy, n_sell = count_buy_sell(open_trades)
            total_pl      = get_total_pl(open_trades)
            tp            = settings["total_tp"]
            trigger       = settings["layer_trigger"]
            max_layers    = settings["max_layers"]
            lbl           = em(pair_label(pair))
            pl_s          = em("${:.2f}".format(total_pl))

            # ── 1. TP tercapai ──
            if open_trades and total_pl >= tp:
                pl_closed = close_all_trades(pair, open_trades)
                log_trade(pair, "all", "TP", pl_closed)
                await app.bot.send_message(ALLOWED_USER_ID,
                    "🎯 *{}* \u2014 TP Tercapai\\!\n"
                    "Profit: `{}`\n"
                    "Jumlah posisi ditutup: `{}`".format(lbl, pl_s, len(open_trades)),
                    parse_mode="MarkdownV2")
                state["last_signal"] = None
                state["waiting_cross"] = None
                await asyncio.sleep(settings["check_interval"])
                continue

            # ── 2. Crossing berlawanan ──
            if open_trades:
                if n_buy > 0 and signal in ("sell", "sell_active"):
                    pl_closed = close_all_trades(pair, open_trades)
                    log_trade(pair, "buy", "cross_close", pl_closed)
                    await app.bot.send_message(ALLOWED_USER_ID,
                        "🔄 *{}* \u2014 Death Cross\\!\n"
                        "Semua BUY ditutup\\.\n"
                        "P/L: `{}` \\| Posisi: `{}`".format(lbl, pl_s, len(open_trades)),
                        parse_mode="MarkdownV2")
                    state["last_signal"] = None
                    state["waiting_cross"] = "sell"
                    await asyncio.sleep(settings["check_interval"])
                    continue
                if n_sell > 0 and signal in ("buy", "buy_active"):
                    pl_closed = close_all_trades(pair, open_trades)
                    log_trade(pair, "sell", "cross_close", pl_closed)
                    await app.bot.send_message(ALLOWED_USER_ID,
                        "🔄 *{}* \u2014 Golden Cross\\!\n"
                        "Semua SELL ditutup\\.\n"
                        "P/L: `{}` \\| Posisi: `{}`".format(lbl, pl_s, len(open_trades)),
                        parse_mode="MarkdownV2")
                    state["last_signal"] = None
                    state["waiting_cross"] = "buy"
                    await asyncio.sleep(settings["check_interval"])
                    continue

            # ── 3. Entry pertama ──
            if signal == "buy" and state["last_signal"] != "buy" and n_buy == 0 and state["waiting_cross"] != "sell":
                place_order(pair, "buy")
                log_trade(pair, "buy", "entry_1")
                state["last_signal"] = "buy"
                state["waiting_cross"] = None
                await app.bot.send_message(ALLOWED_USER_ID,
                    "📈 *{}* \u2014 ENTRY BUY \\#1\n"
                    "Golden Cross \\(EMA{} \\> EMA{}\\)\n"
                    "Lot: `{}`".format(lbl, settings["ema_fast"], settings["ema_slow"], em(str(settings["lot_size"]))),
                    parse_mode="MarkdownV2")

            elif signal == "sell" and state["last_signal"] != "sell" and n_sell == 0 and state["waiting_cross"] != "buy":
                place_order(pair, "sell")
                log_trade(pair, "sell", "entry_1")
                state["last_signal"] = "sell"
                state["waiting_cross"] = None
                await app.bot.send_message(ALLOWED_USER_ID,
                    "📉 *{}* \u2014 ENTRY SELL \\#1\n"
                    "Death Cross \\(EMA{} \\< EMA{}\\)\n"
                    "Lot: `{}`".format(lbl, settings["ema_fast"], settings["ema_slow"], em(str(settings["lot_size"]))),
                    parse_mode="MarkdownV2")

            # ── 4. Layering ──
            elif open_trades:
                last_pl = get_last_trade_pl(open_trades)
                total_layers = n_buy + n_sell

                if last_pl is not None and last_pl <= trigger:
                    if total_layers >= max_layers:
                        # Sudah mencapai max layer — kirim warning tapi tidak entry
                        logger.warning("[%s] Max layers %d reached.", pair, max_layers)
                    elif n_buy > 0 and signal in ("buy", "buy_active"):
                        place_order(pair, "buy")
                        log_trade(pair, "buy", "layer_{}".format(n_buy + 1))
                        await app.bot.send_message(ALLOWED_USER_ID,
                            "📈 *{}* \u2014 LAYER BUY \\#{}\n"
                            "Last: `{}` \\| Total: `{}`\n"
                            "Layer: `{}/{}`".format(
                                lbl, n_buy+1,
                                em("${:.2f}".format(last_pl)), pl_s,
                                n_buy+1, max_layers),
                            parse_mode="MarkdownV2")
                    elif n_sell > 0 and signal in ("sell", "sell_active"):
                        place_order(pair, "sell")
                        log_trade(pair, "sell", "layer_{}".format(n_sell + 1))
                        await app.bot.send_message(ALLOWED_USER_ID,
                            "📉 *{}* \u2014 LAYER SELL \\#{}\n"
                            "Last: `{}` \\| Total: `{}`\n"
                            "Layer: `{}/{}`".format(
                                lbl, n_sell+1,
                                em("${:.2f}".format(last_pl)), pl_s,
                                n_sell+1, max_layers),
                            parse_mode="MarkdownV2")

        except Exception as e:
            logger.error("[%s] %s", pair, e)
            try:
                await app.bot.send_message(ALLOWED_USER_ID,
                    "⚠️ *{}* error: `{}`".format(em(pair_label(pair)), em(str(e))),
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


# ══════════════════════════════════════════
# KEYBOARDS
# ══════════════════════════════════════════

def kb_main():
    active_count = sum(1 for p in ALL_PAIRS if pair_active.get(p))
    estop_label  = "🚨 Reset Emergency Stop" if emergency_stop else "🚨 Emergency Stop"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📋 Kelola Pair ({}/{})".format(active_count, len(ALL_PAIRS)), callback_data="menu_pairs")],
        [InlineKeyboardButton("⚙️ Setting",           callback_data="menu_settings"),
         InlineKeyboardButton("📊 Akun",              callback_data="menu_account")],
        [InlineKeyboardButton("📈 Posisi",            callback_data="menu_positions"),
         InlineKeyboardButton("📜 Log Trade",         callback_data="menu_log")],
        [InlineKeyboardButton("▶️ ON Semua",          callback_data="all_on"),
         InlineKeyboardButton("⏹ OFF Semua",         callback_data="all_off")],
        [InlineKeyboardButton("❌ Close Semua Posisi", callback_data="confirm_closeall")],
        [InlineKeyboardButton(estop_label,            callback_data="toggle_estop")],
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
        [InlineKeyboardButton("📦 Lot Size: {}".format(s["lot_size"]),             callback_data="set_lot_size")],
        [InlineKeyboardButton("📉 Layer Trigger: ${}".format(s["layer_trigger"]),  callback_data="set_layer_trigger")],
        [InlineKeyboardButton("🎯 Take Profit: ${}".format(s["total_tp"]),         callback_data="set_total_tp")],
        [InlineKeyboardButton("🔢 Max Layers: {}".format(s["max_layers"]),         callback_data="set_max_layers")],
        [InlineKeyboardButton("⏱ Interval: {}s".format(s["check_interval"]),      callback_data="set_check_interval")],
        [InlineKeyboardButton("📊 EMA Fast: {}".format(s["ema_fast"]),             callback_data="set_ema_fast")],
        [InlineKeyboardButton("📊 EMA Slow: {}".format(s["ema_slow"]),             callback_data="set_ema_slow")],
        [InlineKeyboardButton("🛑 Daily Loss Limit: ${}".format(s["daily_loss_limit"]), callback_data="set_daily_loss_limit")],
        [InlineKeyboardButton("🔔 Auto Summary: {}s".format(s["notify_interval"]), callback_data="set_notify_interval")],
        [InlineKeyboardButton("🏠 Menu Utama", callback_data="menu_main")],
    ])

def kb_confirm_closeall():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Ya, Close Semua", callback_data="do_closeall"),
         InlineKeyboardButton("❌ Batal",            callback_data="menu_main")],
    ])

def kb_back():
    return InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Menu Utama", callback_data="menu_main")]])


# ══════════════════════════════════════════
# TEXT BUILDERS
# ══════════════════════════════════════════

def text_main():
    active_count = sum(1 for p in ALL_PAIRS if pair_active.get(p))
    s = settings
    estop_status = "🚨 EMERGENCY STOP AKTIF" if emergency_stop else "✅ Normal"
    return (
        "🤖 *Forex Trading Bot*\n\n"
        "Status: {}\n"
        "Uptime: `{}`\n"
        "Pair aktif: `{}/{}`\n\n"
        "Lot: `{}` \\| TP: `${}` \\| Layer: `${}`\n"
        "Max Layer: `{}` \\| EMA: `{}/{}`\n"
        "Daily Loss Limit: `${}`\n\n"
        "Pilih menu:"
    ).format(
        estop_status,
        em(uptime_str()),
        active_count, len(ALL_PAIRS),
        em(str(s["lot_size"])),
        em(str(s["total_tp"])),
        em(str(s["layer_trigger"])),
        s["max_layers"],
        s["ema_fast"], s["ema_slow"],
        em(str(s["daily_loss_limit"]))
    )

def text_settings():
    s = settings
    notif = "{}s".format(s["notify_interval"]) if s["notify_interval"] > 0 else "Nonaktif"
    return (
        "⚙️ *Setting Bot*\n\n"
        "📦 Lot Size: `{}`\n"
        "📉 Layer Trigger: `${}` — posisi terakhir floating di bawah ini → entry lagi\n"
        "🎯 Take Profit: `${}` — total profit di atas ini → close semua\n"
        "🔢 Max Layers: `{}` — batas maksimal layer per pair\n"
        "⏱ Check Interval: `{} detik`\n"
        "📊 EMA Fast: `{}` \\| EMA Slow: `{}`\n"
        "🛑 Daily Loss Limit: `${}` — semua pair berhenti jika tercapai\n"
        "🔔 Auto Summary: `{}`\n\n"
        "Ketuk setting yang ingin diubah:"
    ).format(
        em(str(s["lot_size"])),
        em(str(s["layer_trigger"])),
        em(str(s["total_tp"])),
        s["max_layers"],
        s["check_interval"],
        s["ema_fast"], s["ema_slow"],
        em(str(s["daily_loss_limit"])),
        em(notif)
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
        arrow = "📈" if grand_total >= 0 else "📉"
        lines.append("\n{} *Grand Total: `{}`*".format(arrow, em("${:.2f}".format(grand_total))))
    return "\n".join(lines)

async def text_account():
    try:
        info = get_account_info()
        active_count = sum(1 for p in ALL_PAIRS if pair_active.get(p))
        margin_warn  = " ⚠️" if info["margin_rate"] > 50 else ""
        return (
            "📊 *Status Akun*\n\n"
            "💰 Balance: `{}`\n"
            "📈 NAV: `{}`\n"
            "📉 Floating P/L: `{}`\n"
            "🔒 Margin Used: `{}`{}\n"
            "🟢 Free Margin: `{}`\n\n"
            "🤖 Pair aktif: `{}/{}`\n"
            "⏱ Uptime: `{}`\n"
            "🌐 Environment: `{}`\n\n"
            "📊 *Statistik Sesi*\n"
            "Total trade: `{}` \\| Win: `{}` \\| Loss: `{}`\n"
            "P/L sesi: `{}`"
        ).format(
            em("${:.2f}".format(info["balance"])),
            em("${:.2f}".format(info["nav"])),
            em("${:.2f}".format(info["pl"])),
            em("${:.2f}".format(info["margin"])), margin_warn,
            em("${:.2f}".format(info["free_margin"])),
            active_count, len(ALL_PAIRS),
            em(uptime_str()),
            OANDA_ENV,
            daily_stats["trades"], daily_stats["wins"], daily_stats["losses"],
            em("${:.2f}".format(daily_stats["total_pl"]))
        )
    except Exception as e:
        return "❌ Gagal ambil info akun: `{}`".format(em(str(e)))

def text_log():
    if not trade_log:
        return "📜 *Log Trade*\n\n_Belum ada trade_"
    lines = ["📜 *Log Trade* \\(50 terakhir\\)\n"]
    for t in reversed(trade_log[-50:]):
        pl_str = " \\| P/L: `{}`".format(em("${:.2f}".format(t["pl"]))) if t["pl"] is not None else ""
        icon   = "📈" if t["direction"] == "buy" else ("📉" if t["direction"] == "sell" else "🔄")
        lines.append("{} `{}` *{}* — {}{}".format(
            icon, t["time"], em(t["pair"]), em(t["action"]), pl_str))
    return "\n".join(lines)


# ══════════════════════════════════════════
# HANDLERS
# ══════════════════════════════════════════

async def start_command(update, context):
    global bot_start_time
    if not is_allowed(update):
        return
    if not bot_start_time:
        bot_start_time = datetime.now()
    await update.message.reply_text(text_main(), parse_mode="MarkdownV2", reply_markup=kb_main())


async def button_handler(update, context):
    global emergency_stop
    query = update.callback_query
    await query.answer()
    if query.from_user.id != ALLOWED_USER_ID:
        return
    data = query.data

    async def edit_main():
        await query.edit_message_text(text_main(), parse_mode="MarkdownV2", reply_markup=kb_main())

    if data == "menu_main":
        await edit_main()

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

    elif data.startswith("toggle_") and not data.startswith("toggle_estop"):
        pair = data[len("toggle_"):]
        if pair_active.get(pair):
            stop_pair(pair)
            await query.answer("{} ⏹ OFF".format(pair_label(pair)), show_alert=False)
        else:
            if emergency_stop:
                await query.answer("⚠️ Emergency Stop aktif! Reset dulu.", show_alert=True)
                return
            start_pair(pair, context.application)
            await query.answer("{} ✅ ON".format(pair_label(pair)), show_alert=False)
        active_count = sum(1 for p in ALL_PAIRS if pair_active.get(p))
        try:
            await query.edit_message_text(
                "📋 *Kelola Pair*\n\n✅ \\= Aktif \\| ⭕ \\= Nonaktif\nAktif: `{}/{}`\n\nKetuk pair untuk toggle:".format(active_count, len(ALL_PAIRS)),
                parse_mode="MarkdownV2", reply_markup=kb_pairs(0))
        except Exception:
            pass

    elif data == "toggle_estop":
        if emergency_stop:
            emergency_stop = False
            await query.answer("✅ Emergency Stop direset!", show_alert=True)
        else:
            emergency_stop = True
            for pair in ALL_PAIRS:
                stop_pair(pair)
            await query.answer("🚨 Emergency Stop aktif! Semua pair dihentikan.", show_alert=True)
        await edit_main()

    elif data == "all_on":
        if emergency_stop:
            await query.answer("⚠️ Emergency Stop aktif! Reset dulu.", show_alert=True)
            return
        for pair in ALL_PAIRS:
            start_pair(pair, context.application)
        await query.answer("✅ Semua pair ON!", show_alert=True)
        await edit_main()

    elif data == "all_off":
        for pair in ALL_PAIRS:
            stop_pair(pair)
        await query.answer("⏹ Semua pair OFF!", show_alert=True)
        await edit_main()

    elif data == "menu_settings":
        await query.edit_message_text(text_settings(), parse_mode="MarkdownV2", reply_markup=kb_settings())

    elif data.startswith("set_"):
        key = data[len("set_"):]
        pending_setting_key[query.from_user.id] = key
        labels = {
            "lot_size":         "Lot Size (sekarang: {})\nContoh: 0.01".format(settings["lot_size"]),
            "layer_trigger":    "Layer Trigger dalam $ (sekarang: {})\nHarus negatif, contoh: -2".format(settings["layer_trigger"]),
            "total_tp":         "Take Profit dalam $ (sekarang: {})\nContoh: 10".format(settings["total_tp"]),
            "max_layers":       "Max Layers (sekarang: {})\nContoh: 10".format(settings["max_layers"]),
            "check_interval":   "Interval cek dalam detik (sekarang: {})\nMinimal 10, contoh: 60".format(settings["check_interval"]),
            "ema_fast":         "EMA Fast period (sekarang: {})\nContoh: 20".format(settings["ema_fast"]),
            "ema_slow":         "EMA Slow period (sekarang: {})\nContoh: 50".format(settings["ema_slow"]),
            "daily_loss_limit": "Daily Loss Limit dalam $ (sekarang: {})\nHarus negatif, contoh: -50".format(settings["daily_loss_limit"]),
            "notify_interval":  "Auto Summary interval dalam detik (sekarang: {})\n0 = nonaktif, contoh: 3600".format(settings["notify_interval"]),
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

    elif data == "menu_log":
        await query.edit_message_text(text_log(), parse_mode="MarkdownV2", reply_markup=kb_back())

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
                    pl = close_all_trades(pair, ot)
                    total  += pl
                    closed += len(ot)
                    log_trade(pair, "all", "manual_close", pl)
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
            raise ValueError("Harus negatif, contoh: -2")
        if key == "total_tp" and value <= 0:
            raise ValueError("TP harus > 0")
        if key == "max_layers" and value < 1:
            raise ValueError("Max layers minimal 1")
        if key == "check_interval" and value < 10:
            raise ValueError("Minimal 10 detik")
        if key in ("ema_fast", "ema_slow") and value < 2:
            raise ValueError("Minimal 2")
        if key == "ema_fast" and value >= settings["ema_slow"]:
            raise ValueError("EMA Fast harus < EMA Slow")
        if key == "ema_slow" and value <= settings["ema_fast"]:
            raise ValueError("EMA Slow harus > EMA Fast")
        if key == "daily_loss_limit" and value >= 0:
            raise ValueError("Harus negatif, contoh: -50")
        if key == "notify_interval" and value < 0:
            raise ValueError("Minimal 0 (0 = nonaktif)")

        settings[key] = int(value) if key in ("ema_fast", "ema_slow", "check_interval", "max_layers", "notify_interval") else value
        del pending_setting_key[user_id]

        await update.message.reply_text(
            "✅ *{}* diubah ke `{}`".format(em(key), em(str(settings[key]))),
            parse_mode="MarkdownV2")
        await update.message.reply_text(text_settings(), parse_mode="MarkdownV2", reply_markup=kb_settings())

    except ValueError as e:
        await update.message.reply_text(
            "❌ Tidak valid: `{}`\nCoba lagi:".format(em(str(e))),
            parse_mode="MarkdownV2")


# ══════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════

async def post_init(app):
    global bot_start_time
    bot_start_time = datetime.now()
    asyncio.create_task(monitor_daily_loss(app))
    asyncio.create_task(auto_summary(app))
    logger.info("Background tasks started.")


def main():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("menu",  start_command))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, receive_setting_value))
    logger.info("Bot siap.")
    app.run_polling()

if __name__ == "__main__":
    main()
