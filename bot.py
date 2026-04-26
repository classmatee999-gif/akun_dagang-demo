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

# ── Settings default ──
settings = {
    # Entry
    "lot_size":           0.01,
    "ema_fast":           20,
    "ema_slow":           50,
    # Layering
    "layer_trigger":      -10.0,   # Entry layer kalau posisi terakhir floating <= ini
    "max_layers":         5,       # Maks layer per pair
    # Exit
    "total_tp":           25.0,    # Close semua kalau total profit >= ini
    "hard_sl":            -30.0,   # Close semua kalau total floating <= ini (hard SL)
    "trailing_tp":        True,    # Aktifkan trailing TP
    "trailing_pullback":  3.0,     # Close kalau profit turun X$ dari peak
    # Filter
    "adx_filter":         True,    # Hanya trade kalau ADX >= adx_min (trending)
    "adx_min":            20,      # Minimum ADX untuk entry
    "max_active_pairs":   5,       # Maks pair yang trading bersamaan
    # Risk global
    "daily_loss_limit":   -100.0,  # Emergency stop kalau total floating semua pair <= ini
    # Bot
    "check_interval":     60,
    "notify_interval":    3600,
}

# ── State ──
pair_active         = {p: False for p in ALL_PAIRS}
pair_tasks          = {}
pair_state          = {
    p: {
        "last_signal":    None,
        "waiting_cross":  None,
        "peak_profit":    0.0,     # Peak profit tertinggi untuk trailing TP
    } for p in ALL_PAIRS
}
pending_setting_key = {}
bot_start_time      = None
trade_log           = []
daily_stats         = {"trades": 0, "wins": 0, "losses": 0, "total_pl": 0.0}
emergency_stop      = False

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

def active_pair_count():
    return sum(1 for p in ALL_PAIRS if pair_active.get(p))


# ══════════════════════════════════════════
# OANDA
# ══════════════════════════════════════════

def get_candles(pair, count=60, granularity="D"):
    r = instruments.InstrumentsCandles(pair, params={"count": count, "granularity": granularity})
    client.request(r)
    return r.response["candles"]

def get_closes(pair, count=60):
    candles = get_candles(pair, count)
    return [float(c["mid"]["c"]) for c in candles if c["complete"]]

def calculate_ema(data, period):
    return pd.Series(data).ewm(span=period, adjust=False).mean().tolist()

def calculate_adx(pair, period=14):
    """Hitung ADX untuk filter trending/ranging."""
    try:
        candles = get_candles(pair, count=period*3, granularity="D")
        completed = [c for c in candles if c["complete"]]
        if len(completed) < period + 1:
            return 999  # Kalau data tidak cukup, anggap trending (tidak filter)

        highs  = [float(c["mid"]["h"]) for c in completed]
        lows   = [float(c["mid"]["l"]) for c in completed]
        closes = [float(c["mid"]["c"]) for c in completed]

        tr_list, pdm_list, ndm_list = [], [], []
        for i in range(1, len(closes)):
            h, l, pc = highs[i], lows[i], closes[i-1]
            tr  = max(h - l, abs(h - pc), abs(l - pc))
            pdm = max(h - highs[i-1], 0) if (h - highs[i-1]) > (lows[i-1] - l) else 0
            ndm = max(lows[i-1] - l, 0) if (lows[i-1] - l) > (h - highs[i-1]) else 0
            tr_list.append(tr)
            pdm_list.append(pdm)
            ndm_list.append(ndm)

        def smooth(data, p):
            result = [sum(data[:p])]
            for i in range(p, len(data)):
                result.append(result[-1] - result[-1]/p + data[i])
            return result

        atr  = smooth(tr_list,  period)
        pDI  = smooth(pdm_list, period)
        nDI  = smooth(ndm_list, period)

        dx_list = []
        for i in range(len(atr)):
            if atr[i] == 0:
                continue
            pdi = 100 * pDI[i] / atr[i]
            ndi = 100 * nDI[i] / atr[i]
            dx  = 100 * abs(pdi - ndi) / (pdi + ndi) if (pdi + ndi) > 0 else 0
            dx_list.append(dx)

        if not dx_list:
            return 0
        adx = sum(dx_list[-period:]) / period
        return round(adx, 2)
    except Exception as e:
        logger.error("ADX calc error [%s]: %s", pair, e)
        return 999

def get_ema_signal(pair):
    closes = get_closes(pair, 60)
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
        "balance":     float(a["balance"]),
        "nav":         float(a["NAV"]),
        "pl":          float(a["unrealizedPL"]),
        "margin":      float(a["marginUsed"]),
        "free_margin": float(a["marginAvailable"]),
    }


# ══════════════════════════════════════════
# MONITORS
# ══════════════════════════════════════════

async def monitor_daily_loss(app):
    global emergency_stop
    while True:
        try:
            if emergency_stop or not any(pair_active.values()):
                await asyncio.sleep(30)
                continue
            all_trades = get_all_open_trades()
            total_pl   = sum(float(t["unrealizedPL"]) for t in all_trades)
            if total_pl <= settings["daily_loss_limit"]:
                emergency_stop = True
                for pair in ALL_PAIRS:
                    stop_pair(pair)
                await app.bot.send_message(ALLOWED_USER_ID,
                    "🚨 *EMERGENCY STOP\\!*\n\n"
                    "Daily loss limit tercapai\\!\n"
                    "Total floating: `{}`\n"
                    "Limit: `{}`\n\n"
                    "Semua pair dihentikan\\.".format(
                        em("${:.2f}".format(total_pl)),
                        em("${:.2f}".format(settings["daily_loss_limit"]))
                    ), parse_mode="MarkdownV2")
        except Exception as e:
            logger.error("monitor_daily_loss: %s", e)
        await asyncio.sleep(30)


async def auto_summary(app):
    while True:
        interval = settings["notify_interval"]
        if interval <= 0:
            await asyncio.sleep(60)
            continue
        await asyncio.sleep(interval)
        try:
            all_trades   = get_all_open_trades()
            total_pl     = sum(float(t["unrealizedPL"]) for t in all_trades)
            try:
                info    = get_account_info()
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
                    active_pair_count(), len(ALL_PAIRS),
                    len(all_trades),
                    daily_stats["trades"],
                    daily_stats["wins"],
                    daily_stats["losses"],
                    em("${:.2f}".format(daily_stats["total_pl"]))
                ), parse_mode="MarkdownV2")
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
            lbl           = em(pair_label(pair))
            pl_s          = em("${:.2f}".format(total_pl))

            # ── Update peak profit untuk trailing TP ──
            if open_trades and total_pl > state["peak_profit"]:
                state["peak_profit"] = total_pl

            # ── 1. Hard SL per pair ──
            hard_sl = settings["hard_sl"]
            if open_trades and total_pl <= hard_sl:
                pl_closed = close_all_trades(pair, open_trades)
                log_trade(pair, "all", "hard_sl", pl_closed)
                state["last_signal"]   = None
                state["waiting_cross"] = None
                state["peak_profit"]   = 0.0
                await app.bot.send_message(ALLOWED_USER_ID,
                    "🛑 *{}* \u2014 HARD SL TRIGGERED\\!\n"
                    "Total floating: `{}`\n"
                    "Limit: `{}`\n"
                    "Semua posisi ditutup\\. `{}` posisi\\.".format(
                        lbl, pl_s,
                        em("${:.2f}".format(hard_sl)),
                        len(open_trades)
                    ), parse_mode="MarkdownV2")
                await asyncio.sleep(settings["check_interval"])
                continue

            # ── 2. Trailing TP ──
            if settings["trailing_tp"] and open_trades and state["peak_profit"] >= settings["total_tp"]:
                pullback = settings["trailing_pullback"]
                if state["peak_profit"] - total_pl >= pullback:
                    pl_closed = close_all_trades(pair, open_trades)
                    log_trade(pair, "all", "trailing_tp", pl_closed)
                    state["last_signal"]   = None
                    state["waiting_cross"] = None
                    state["peak_profit"]   = 0.0
                    await app.bot.send_message(ALLOWED_USER_ID,
                        "📍 *{}* \u2014 TRAILING TP\\!\n"
                        "Peak profit: `{}`\n"
                        "Close di: `{}`\n"
                        "Pullback: `{}`".format(
                            lbl,
                            em("${:.2f}".format(state["peak_profit"])),
                            pl_s,
                            em("${:.2f}".format(pullback))
                        ), parse_mode="MarkdownV2")
                    await asyncio.sleep(settings["check_interval"])
                    continue

            # ── 3. Normal TP ──
            tp = settings["total_tp"]
            if open_trades and total_pl >= tp and not settings["trailing_tp"]:
                pl_closed = close_all_trades(pair, open_trades)
                log_trade(pair, "all", "tp", pl_closed)
                state["last_signal"]   = None
                state["waiting_cross"] = None
                state["peak_profit"]   = 0.0
                await app.bot.send_message(ALLOWED_USER_ID,
                    "🎯 *{}* \u2014 TP Tercapai\\!\n"
                    "Profit: `{}` \\| Posisi: `{}`".format(lbl, pl_s, len(open_trades)),
                    parse_mode="MarkdownV2")
                await asyncio.sleep(settings["check_interval"])
                continue

            # ── 4. Crossing berlawanan → close semua ──
            if open_trades:
                if n_buy > 0 and signal in ("sell", "sell_active"):
                    pl_closed = close_all_trades(pair, open_trades)
                    log_trade(pair, "buy", "cross_close", pl_closed)
                    state["last_signal"]   = None
                    state["waiting_cross"] = "sell"
                    state["peak_profit"]   = 0.0
                    await app.bot.send_message(ALLOWED_USER_ID,
                        "🔄 *{}* \u2014 Death Cross\\!\n"
                        "BUY ditutup\\. P/L: `{}` \\| `{}` posisi".format(lbl, pl_s, len(open_trades)),
                        parse_mode="MarkdownV2")
                    await asyncio.sleep(settings["check_interval"])
                    continue
                if n_sell > 0 and signal in ("buy", "buy_active"):
                    pl_closed = close_all_trades(pair, open_trades)
                    log_trade(pair, "sell", "cross_close", pl_closed)
                    state["last_signal"]   = None
                    state["waiting_cross"] = "buy"
                    state["peak_profit"]   = 0.0
                    await app.bot.send_message(ALLOWED_USER_ID,
                        "🔄 *{}* \u2014 Golden Cross\\!\n"
                        "SELL ditutup\\. P/L: `{}` \\| `{}` posisi".format(lbl, pl_s, len(open_trades)),
                        parse_mode="MarkdownV2")
                    await asyncio.sleep(settings["check_interval"])
                    continue

            # ── 5. Entry pertama (cek ADX filter) ──
            can_entry_buy  = signal == "buy"  and state["last_signal"] != "buy"  and n_buy  == 0 and state["waiting_cross"] != "sell"
            can_entry_sell = signal == "sell" and state["last_signal"] != "sell" and n_sell == 0 and state["waiting_cross"] != "buy"

            if can_entry_buy or can_entry_sell:
                # Cek ADX filter
                adx_ok = True
                adx_val = 0
                if settings["adx_filter"]:
                    adx_val = calculate_adx(pair)
                    adx_ok  = adx_val >= settings["adx_min"]

                if not adx_ok:
                    logger.info("[%s] ADX %.1f < %d, skip entry.", pair, adx_val, settings["adx_min"])
                    await asyncio.sleep(settings["check_interval"])
                    continue

                if can_entry_buy:
                    place_order(pair, "buy")
                    log_trade(pair, "buy", "entry_1")
                    state["last_signal"]   = "buy"
                    state["waiting_cross"] = None
                    state["peak_profit"]   = 0.0
                    adx_str = " \\| ADX: `{}`".format(em(str(adx_val))) if settings["adx_filter"] else ""
                    await app.bot.send_message(ALLOWED_USER_ID,
                        "📈 *{}* \u2014 ENTRY BUY \\#1\n"
                        "Golden Cross \\| EMA{}/{}{}\nLot: `{}`".format(
                            lbl, settings["ema_fast"], settings["ema_slow"],
                            adx_str, em(str(settings["lot_size"]))
                        ), parse_mode="MarkdownV2")

                elif can_entry_sell:
                    place_order(pair, "sell")
                    log_trade(pair, "sell", "entry_1")
                    state["last_signal"]   = "sell"
                    state["waiting_cross"] = None
                    state["peak_profit"]   = 0.0
                    adx_str = " \\| ADX: `{}`".format(em(str(adx_val))) if settings["adx_filter"] else ""
                    await app.bot.send_message(ALLOWED_USER_ID,
                        "📉 *{}* \u2014 ENTRY SELL \\#1\n"
                        "Death Cross \\| EMA{}/{}{}\nLot: `{}`".format(
                            lbl, settings["ema_fast"], settings["ema_slow"],
                            adx_str, em(str(settings["lot_size"]))
                        ), parse_mode="MarkdownV2")

            # ── 6. Layering ──
            elif open_trades:
                last_pl      = get_last_trade_pl(open_trades)
                total_layers = n_buy + n_sell
                trigger      = settings["layer_trigger"]
                max_layers   = settings["max_layers"]

                if last_pl is not None and last_pl <= trigger:
                    if total_layers >= max_layers:
                        logger.warning("[%s] Max layers %d reached.", pair, max_layers)
                    elif n_buy > 0 and signal in ("buy", "buy_active"):
                        place_order(pair, "buy")
                        log_trade(pair, "buy", "layer_{}".format(n_buy+1))
                        await app.bot.send_message(ALLOWED_USER_ID,
                            "📈 *{}* \u2014 LAYER BUY \\#{}\n"
                            "Last: `{}` \\| Total: `{}`\n"
                            "Layer: `{}/{}`".format(
                                lbl, n_buy+1,
                                em("${:.2f}".format(last_pl)), pl_s,
                                n_buy+1, max_layers
                            ), parse_mode="MarkdownV2")
                    elif n_sell > 0 and signal in ("sell", "sell_active"):
                        place_order(pair, "sell")
                        log_trade(pair, "sell", "layer_{}".format(n_sell+1))
                        await app.bot.send_message(ALLOWED_USER_ID,
                            "📉 *{}* \u2014 LAYER SELL \\#{}\n"
                            "Last: `{}` \\| Total: `{}`\n"
                            "Layer: `{}/{}`".format(
                                lbl, n_sell+1,
                                em("${:.2f}".format(last_pl)), pl_s,
                                n_sell+1, max_layers
                            ), parse_mode="MarkdownV2")

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
    if active_pair_count() >= settings["max_active_pairs"]:
        return "max"
    pair_active[pair] = True
    pair_state[pair]  = {"last_signal": None, "waiting_cross": None, "peak_profit": 0.0}
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
    estop_label = "🔓 Reset Emergency Stop" if emergency_stop else "🚨 Emergency Stop"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📋 Kelola Pair ({}/{})".format(active_pair_count(), settings["max_active_pairs"]), callback_data="menu_pairs")],
        [InlineKeyboardButton("⚙️ Setting",         callback_data="menu_settings"),
         InlineKeyboardButton("📊 Akun",            callback_data="menu_account")],
        [InlineKeyboardButton("📈 Posisi",          callback_data="menu_positions"),
         InlineKeyboardButton("📜 Log Trade",       callback_data="menu_log")],
        [InlineKeyboardButton("▶️ ON Semua",        callback_data="all_on"),
         InlineKeyboardButton("⏹ OFF Semua",       callback_data="all_off")],
        [InlineKeyboardButton("❌ Close Semua Posisi", callback_data="confirm_closeall")],
        [InlineKeyboardButton(estop_label,          callback_data="toggle_estop")],
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
    s   = settings
    adx = "ON ✅" if s["adx_filter"] else "OFF ⭕"
    ttp = "ON ✅" if s["trailing_tp"] else "OFF ⭕"
    notif = "{}s".format(s["notify_interval"]) if s["notify_interval"] > 0 else "Nonaktif"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("─── Entry ───", callback_data="noop")],
        [InlineKeyboardButton("📦 Lot Size: {}".format(s["lot_size"]),             callback_data="set_lot_size")],
        [InlineKeyboardButton("📊 EMA Fast: {}".format(s["ema_fast"]),             callback_data="set_ema_fast"),
         InlineKeyboardButton("📊 EMA Slow: {}".format(s["ema_slow"]),             callback_data="set_ema_slow")],
        [InlineKeyboardButton("─── Layering ───", callback_data="noop")],
        [InlineKeyboardButton("📉 Layer Trigger: ${}".format(s["layer_trigger"]),  callback_data="set_layer_trigger")],
        [InlineKeyboardButton("🔢 Max Layers: {}".format(s["max_layers"]),         callback_data="set_max_layers")],
        [InlineKeyboardButton("─── Exit ───", callback_data="noop")],
        [InlineKeyboardButton("🎯 Take Profit: ${}".format(s["total_tp"]),         callback_data="set_total_tp")],
        [InlineKeyboardButton("🛑 Hard SL: ${}".format(s["hard_sl"]),              callback_data="set_hard_sl")],
        [InlineKeyboardButton("📍 Trailing TP: {}".format(ttp),                    callback_data="toggle_trailing_tp")],
        [InlineKeyboardButton("📍 Pullback: ${}".format(s["trailing_pullback"]),   callback_data="set_trailing_pullback")],
        [InlineKeyboardButton("─── Filter ───", callback_data="noop")],
        [InlineKeyboardButton("📡 ADX Filter: {}".format(adx),                     callback_data="toggle_adx_filter")],
        [InlineKeyboardButton("📡 ADX Min: {}".format(s["adx_min"]),               callback_data="set_adx_min")],
        [InlineKeyboardButton("─── Risk Global ───", callback_data="noop")],
        [InlineKeyboardButton("👥 Max Pair Aktif: {}".format(s["max_active_pairs"]), callback_data="set_max_active_pairs")],
        [InlineKeyboardButton("🔴 Daily Loss Limit: ${}".format(s["daily_loss_limit"]), callback_data="set_daily_loss_limit")],
        [InlineKeyboardButton("─── Bot ───", callback_data="noop")],
        [InlineKeyboardButton("⏱ Interval: {}s".format(s["check_interval"]),       callback_data="set_check_interval")],
        [InlineKeyboardButton("🔔 Auto Summary: {}".format(notif),                  callback_data="set_notify_interval")],
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
    s    = settings
    adx  = "ON" if s["adx_filter"] else "OFF"
    ttp  = "ON" if s["trailing_tp"] else "OFF"
    estop_status = "🚨 EMERGENCY STOP AKTIF" if emergency_stop else "✅ Normal"
    return (
        "🤖 *Forex Trading Bot v2*\n\n"
        "Status: {}\n"
        "Uptime: `{}`\n"
        "Pair aktif: `{}/{}`\n\n"
        "📦 Lot: `{}` \\| 🎯 TP: `${}` \\| 🛑 Hard SL: `${}`\n"
        "📉 Layer: `${}` \\| 🔢 Max: `{}`\n"
        "📍 Trailing TP: `{}` \\| Pullback: `${}`\n"
        "📡 ADX Filter: `{}` \\(min `{}`\\)\n"
        "🔴 Daily Limit: `${}`\n\n"
        "Pilih menu:"
    ).format(
        estop_status,
        em(uptime_str()),
        active_pair_count(), settings["max_active_pairs"],
        em(str(s["lot_size"])),
        em(str(s["total_tp"])),
        em(str(s["hard_sl"])),
        em(str(s["layer_trigger"])),
        s["max_layers"],
        ttp,
        em(str(s["trailing_pullback"])),
        adx, s["adx_min"],
        em(str(s["daily_loss_limit"]))
    )

def text_settings():
    s    = settings
    adx  = "ON ✅" if s["adx_filter"] else "OFF ⭕"
    ttp  = "ON ✅" if s["trailing_tp"] else "OFF ⭕"
    notif = "{}s".format(s["notify_interval"]) if s["notify_interval"] > 0 else "Nonaktif"
    return (
        "⚙️ *Setting Bot v2*\n\n"
        "*── Entry ──*\n"
        "📦 Lot Size: `{}`\n"
        "📊 EMA Fast/Slow: `{}/{}`\n\n"
        "*── Layering ──*\n"
        "📉 Layer Trigger: `${}` — posisi terakhir rugi di bawah ini → entry lagi\n"
        "🔢 Max Layers: `{}` — batas layer per pair\n\n"
        "*── Exit ──*\n"
        "🎯 Take Profit: `${}` — close semua kalau profit di atas ini\n"
        "🛑 Hard SL: `${}` — close semua kalau rugi di bawah ini\n"
        "📍 Trailing TP: `{}` — profit lock otomatis\n"
        "📍 Pullback: `${}` — close kalau profit turun sebesar ini dari peak\n\n"
        "*── Filter ──*\n"
        "📡 ADX Filter: `{}` — hanya entry kalau market trending\n"
        "📡 ADX Min: `{}` — nilai minimum ADX untuk entry\n\n"
        "*── Risk Global ──*\n"
        "👥 Max Pair Aktif: `{}` — maks pair trading bersamaan\n"
        "🔴 Daily Loss Limit: `${}` — emergency stop kalau tercapai\n\n"
        "*── Bot ──*\n"
        "⏱ Check Interval: `{}s`\n"
        "🔔 Auto Summary: `{}`\n\n"
        "Ketuk setting yang ingin diubah:"
    ).format(
        em(str(s["lot_size"])),
        s["ema_fast"], s["ema_slow"],
        em(str(s["layer_trigger"])),
        s["max_layers"],
        em(str(s["total_tp"])),
        em(str(s["hard_sl"])),
        ttp,
        em(str(s["trailing_pullback"])),
        adx,
        s["adx_min"],
        s["max_active_pairs"],
        em(str(s["daily_loss_limit"])),
        s["check_interval"],
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
            peak  = pair_state[pair]["peak_profit"]
            peak_str = " \\| Peak: `{}`".format(em("${:.2f}".format(peak))) if peak > 0 else ""
            lines.append(
                "{} *{}*\n  {} Buy: `{}` \\| Sell: `{}` \\| P/L: `{}`{}".format(
                    icon, em(pair_label(pair)), arrow, n_buy, n_sell,
                    em("${:.2f}".format(total_pl)), peak_str
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
        return (
            "📊 *Status Akun*\n\n"
            "💰 Balance: `{}`\n"
            "📈 NAV: `{}`\n"
            "📉 Floating P/L: `{}`\n"
            "🔒 Margin Used: `{}`\n"
            "🟢 Free Margin: `{}`\n\n"
            "🤖 Pair aktif: `{}/{}`\n"
            "⏱ Uptime: `{}`\n"
            "🌐 Environment: `{}`\n\n"
            "📊 *Statistik Sesi*\n"
            "Trade: `{}` \\| Win: `{}` \\| Loss: `{}`\n"
            "Win Rate: `{}%`\n"
            "P/L sesi: `{}`"
        ).format(
            em("${:.2f}".format(info["balance"])),
            em("${:.2f}".format(info["nav"])),
            em("${:.2f}".format(info["pl"])),
            em("${:.2f}".format(info["margin"])),
            em("${:.2f}".format(info["free_margin"])),
            active_pair_count(), settings["max_active_pairs"],
            em(uptime_str()),
            OANDA_ENV,
            daily_stats["trades"], daily_stats["wins"], daily_stats["losses"],
            em("{:.0f}".format(
                daily_stats["wins"] / daily_stats["trades"] * 100
                if daily_stats["trades"] > 0 else 0
            )),
            em("${:.2f}".format(daily_stats["total_pl"]))
        )
    except Exception as e:
        return "❌ Gagal ambil info akun: `{}`".format(em(str(e)))

def text_log():
    if not trade_log:
        return "📜 *Log Trade*\n\n_Belum ada trade_"
    lines = ["📜 *Log Trade* \\(50 terakhir\\)\n"]
    for t in reversed(trade_log[-50:]):
        pl_str = " \\| `{}`".format(em("${:.2f}".format(t["pl"]))) if t["pl"] is not None else ""
        icon   = "📈" if t["direction"] == "buy" else ("📉" if t["direction"] == "sell" else "🔄")
        lines.append("{} `{}` *{}* \u2014 {}{}".format(
            icon, t["time"], em(t["pair"]), em(t["action"]), pl_str))
    return "\n".join(lines)

SETTING_LABELS = {
    "lot_size":          "Lot Size (sekarang: {})\nContoh: 0.01",
    "layer_trigger":     "Layer Trigger $ (sekarang: {})\nHarus negatif, contoh: -10",
    "max_layers":        "Max Layers (sekarang: {})\nContoh: 5",
    "total_tp":          "Take Profit $ (sekarang: {})\nContoh: 25",
    "hard_sl":           "Hard SL $ (sekarang: {})\nHarus negatif, contoh: -30",
    "trailing_pullback": "Trailing Pullback $ (sekarang: {})\nContoh: 3",
    "adx_min":           "ADX Minimum (sekarang: {})\nContoh: 20 (trending kuat = 25+)",
    "max_active_pairs":  "Max Pair Aktif (sekarang: {})\nContoh: 5",
    "daily_loss_limit":  "Daily Loss Limit $ (sekarang: {})\nHarus negatif, contoh: -100",
    "check_interval":    "Check Interval detik (sekarang: {})\nMinimal 10, contoh: 60",
    "notify_interval":   "Auto Summary detik (sekarang: {})\n0 = nonaktif, contoh: 3600",
    "ema_fast":          "EMA Fast period (sekarang: {})\nContoh: 20",
    "ema_slow":          "EMA Slow period (sekarang: {})\nContoh: 50",
}

SETTING_VALIDATORS = {
    "lot_size":          lambda v: v > 0 or "Harus > 0",
    "layer_trigger":     lambda v: v < 0 or "Harus negatif",
    "max_layers":        lambda v: v >= 1 or "Minimal 1",
    "total_tp":          lambda v: v > 0 or "Harus > 0",
    "hard_sl":           lambda v: v < 0 or "Harus negatif",
    "trailing_pullback": lambda v: v > 0 or "Harus > 0",
    "adx_min":           lambda v: 1 <= v <= 100 or "Antara 1-100",
    "max_active_pairs":  lambda v: 1 <= v <= len(ALL_PAIRS) or "Antara 1-{}".format(len(ALL_PAIRS)),
    "daily_loss_limit":  lambda v: v < 0 or "Harus negatif",
    "check_interval":    lambda v: v >= 10 or "Minimal 10 detik",
    "notify_interval":   lambda v: v >= 0 or "Minimal 0",
    "ema_fast":          lambda v: v >= 2 or "Minimal 2",
    "ema_slow":          lambda v: v >= 2 or "Minimal 2",
}

INT_SETTINGS = {"ema_fast", "ema_slow", "check_interval", "max_layers", "max_active_pairs", "notify_interval", "adx_min"}


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

    async def edit_settings():
        await query.edit_message_text(text_settings(), parse_mode="MarkdownV2", reply_markup=kb_settings())

    if data == "menu_main":
        await edit_main()

    elif data == "menu_pairs":
        cnt = active_pair_count()
        mx  = settings["max_active_pairs"]
        await query.edit_message_text(
            "📋 *Kelola Pair*\n\n✅ \\= Aktif \\| ⭕ \\= Nonaktif\nAktif: `{}/{}` \\(maks {}\\)\n\nKetuk pair untuk toggle:".format(cnt, len(ALL_PAIRS), mx),
            parse_mode="MarkdownV2", reply_markup=kb_pairs(0))

    elif data.startswith("pairs_page_"):
        page = int(data[len("pairs_page_"):])
        cnt  = active_pair_count()
        mx   = settings["max_active_pairs"]
        await query.edit_message_text(
            "📋 *Kelola Pair*\n\n✅ \\= Aktif \\| ⭕ \\= Nonaktif\nAktif: `{}/{}` \\(maks {}\\)\n\nKetuk pair untuk toggle:".format(cnt, len(ALL_PAIRS), mx),
            parse_mode="MarkdownV2", reply_markup=kb_pairs(page))

    elif data.startswith("toggle_") and data != "toggle_estop":
        pair   = data[len("toggle_"):]
        if pair_active.get(pair):
            stop_pair(pair)
            await query.answer("{} ⏹ OFF".format(pair_label(pair)), show_alert=False)
        else:
            if emergency_stop:
                await query.answer("⚠️ Emergency Stop aktif! Reset dulu.", show_alert=True)
                return
            result = start_pair(pair, context.application)
            if result == "max":
                mx = settings["max_active_pairs"]
                await query.answer("⚠️ Maks {} pair aktif tercapai!".format(mx), show_alert=True)
                return
            await query.answer("{} ✅ ON".format(pair_label(pair)), show_alert=False)
        cnt = active_pair_count()
        mx  = settings["max_active_pairs"]
        try:
            await query.edit_message_text(
                "📋 *Kelola Pair*\n\n✅ \\= Aktif \\| ⭕ \\= Nonaktif\nAktif: `{}/{}` \\(maks {}\\)\n\nKetuk pair untuk toggle:".format(cnt, len(ALL_PAIRS), mx),
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
            await query.answer("🚨 Emergency Stop aktif!", show_alert=True)
        await edit_main()

    elif data == "all_on":
        if emergency_stop:
            await query.answer("⚠️ Emergency Stop aktif! Reset dulu.", show_alert=True)
            return
        count = 0
        for pair in ALL_PAIRS:
            if active_pair_count() >= settings["max_active_pairs"]:
                break
            if start_pair(pair, context.application):
                count += 1
        await query.answer("✅ {} pair diaktifkan!".format(count), show_alert=True)
        await edit_main()

    elif data == "all_off":
        for pair in ALL_PAIRS:
            stop_pair(pair)
        await query.answer("⏹ Semua pair OFF!", show_alert=True)
        await edit_main()

    elif data == "menu_settings":
        await edit_settings()

    elif data == "toggle_adx_filter":
        settings["adx_filter"] = not settings["adx_filter"]
        status = "ON ✅" if settings["adx_filter"] else "OFF ⭕"
        await query.answer("ADX Filter: {}".format(status), show_alert=False)
        await edit_settings()

    elif data == "toggle_trailing_tp":
        settings["trailing_tp"] = not settings["trailing_tp"]
        status = "ON ✅" if settings["trailing_tp"] else "OFF ⭕"
        await query.answer("Trailing TP: {}".format(status), show_alert=False)
        await edit_settings()

    elif data.startswith("set_"):
        key = data[len("set_"):]
        pending_setting_key[query.from_user.id] = key
        label = SETTING_LABELS.get(key, key).format(settings.get(key, "?"))
        await query.edit_message_text(
            "✏️ *Ubah {}*\n\n{}\n\nKirim nilai baru:".format(em(key), em(label)),
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
                    pair_state[pair]["peak_profit"] = 0.0
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

        # Cross-validate EMA
        if key == "ema_fast" and value >= settings["ema_slow"]:
            raise ValueError("EMA Fast harus < EMA Slow \\({}\\)".format(settings["ema_slow"]))
        if key == "ema_slow" and value <= settings["ema_fast"]:
            raise ValueError("EMA Slow harus > EMA Fast \\({}\\)".format(settings["ema_fast"]))

        validator = SETTING_VALIDATORS.get(key)
        if validator:
            result = validator(value)
            if result is not True and result is not False:
                pass
            elif result is False:
                raise ValueError("Nilai tidak valid")

        settings[key] = int(value) if key in INT_SETTINGS else value
        del pending_setting_key[user_id]

        await update.message.reply_text(
            "✅ *{}* diubah ke `{}`".format(em(key), em(str(settings[key]))),
            parse_mode="MarkdownV2")
        await update.message.reply_text(text_settings(), parse_mode="MarkdownV2", reply_markup=kb_settings())

    except ValueError as e:
        await update.message.reply_text(
            "❌ Tidak valid: {}\nCoba lagi:".format(str(e)),
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
    logger.info("Bot v2 siap.")
    app.run_polling()

if __name__ == "__main__":
    main()
