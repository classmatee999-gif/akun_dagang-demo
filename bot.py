import asyncio
import logging
from datetime import datetime, time as dtime
import pytz
import requests as req_lib
import oandapyV20
from oandapyV20.endpoints import orders, trades, instruments, accounts
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, CallbackQueryHandler, MessageHandler, filters
import pandas as pd
import os

# ─────────────────────────────────────────
# KONFIGURASI
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

WIB = pytz.timezone("Asia/Jakarta")

settings = {
    # Entry
    "lot_size":              0.01,
    "lot_dynamic":           False,   # Auto scale lot berdasarkan balance
    "lot_risk_pct":          1.0,     # % balance per trade kalau lot_dynamic ON
    "ema_fast":              20,
    "ema_slow":              50,
    # Layering
    "layer_trigger":         -10.0,
    "max_layers":            5,
    # Exit
    "total_tp":              25.0,
    "hard_sl":               -30.0,
    "trailing_tp":           True,
    "trailing_pullback":     3.0,
    # Filter
    "adx_filter":            True,
    "adx_min":               20,
    "max_active_pairs":      5,
    # Trading Hours (WIB)
    "trading_hours":         True,    # Hanya trade di jam aktif
    "trading_start":         "15:00", # London open (WIB)
    "trading_end":           "23:00", # NY close (WIB)
    "block_friday":          True,    # Blokir Jumat setelah 22:00 WIB
    "block_monday":          True,    # Blokir Senin sebelum 10:00 WIB
    # News filter
    "news_filter":           True,    # Pause saat high-impact news
    "news_pause_minutes":    30,      # Pause X menit sebelum & sesudah news
    # Risk global
    "daily_loss_limit":      -100.0,
    "margin_warning":        50.0,    # Alert kalau margin used >= X%
    "margin_stop":           80.0,    # Stop semua entry kalau margin >= X%
    # Bot
    "check_interval":        60,
    "notify_interval":       3600,
}

pair_active         = {p: False for p in ALL_PAIRS}
pair_tasks          = {}
pair_state          = {p: {"last_signal": None, "waiting_cross": None, "peak_profit": 0.0} for p in ALL_PAIRS}
pending_setting_key = {}
bot_start_time      = None
trade_log           = []
daily_stats         = {"trades": 0, "wins": 0, "losses": 0, "total_pl": 0.0}
emergency_stop      = False
margin_warned       = False
news_cache          = {"events": [], "last_fetch": None}

logging.basicConfig(format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)
client = oandapyV20.API(access_token=OANDA_TOKEN, environment=OANDA_ENV)


# ══════════════════════════════════════════
# UTILS
# ══════════════════════════════════════════

def pair_label(pair): return pair.replace("_", "/")

def em(text):
    text = str(text)
    for ch in r"_*[]()~`>#+-=|{}.!":
        text = text.replace(ch, "\\" + ch)
    return text

def is_allowed(update): return update.effective_user.id == ALLOWED_USER_ID

def uptime_str():
    if not bot_start_time: return "N/A"
    delta = datetime.now() - bot_start_time
    h, rem = divmod(int(delta.total_seconds()), 3600)
    m, s   = divmod(rem, 60)
    return "{}j {}m {}d".format(h, m, s)

def now_wib():
    return datetime.now(WIB)

def active_pair_count():
    return sum(1 for p in ALL_PAIRS if pair_active.get(p))

def log_trade(pair, direction, action, pl=None):
    trade_log.append({"time": now_wib().strftime("%H:%M:%S"), "pair": pair_label(pair),
                      "direction": direction, "action": action, "pl": pl})
    if len(trade_log) > 100: trade_log.pop(0)
    if pl is not None:
        daily_stats["trades"] += 1
        daily_stats["total_pl"] += pl
        if pl >= 0: daily_stats["wins"] += 1
        else: daily_stats["losses"] += 1


# ══════════════════════════════════════════
# TRADING HOURS FILTER
# ══════════════════════════════════════════

def is_trading_time():
    if not settings["trading_hours"]:
        return True, ""
    now    = now_wib()
    wd     = now.weekday()  # 0=Senin ... 4=Jumat ... 5=Sabtu 6=Minggu

    # Weekend = tutup
    if wd >= 5:
        return False, "Weekend — market tutup"

    # Jumat malam
    if settings["block_friday"] and wd == 4:
        cutoff = dtime(22, 0)
        if now.time() >= cutoff:
            return False, "Jumat malam — risiko gap weekend"

    # Senin pagi
    if settings["block_monday"] and wd == 0:
        cutoff = dtime(10, 0)
        if now.time() < cutoff:
            return False, "Senin pagi — risiko gap weekend"

    # Jam trading
    try:
        start_h, start_m = map(int, settings["trading_start"].split(":"))
        end_h,   end_m   = map(int, settings["trading_end"].split(":"))
        start_t = dtime(start_h, start_m)
        end_t   = dtime(end_h,   end_m)
        if not (start_t <= now.time() <= end_t):
            return False, "Di luar jam trading ({}-{} WIB)".format(
                settings["trading_start"], settings["trading_end"])
    except Exception:
        pass

    return True, ""


# ══════════════════════════════════════════
# NEWS FILTER
# ══════════════════════════════════════════

def fetch_news_events():
    """Ambil high-impact news dari ForexFactory RSS."""
    try:
        now = now_wib()
        last = news_cache["last_fetch"]
        # Refresh setiap 1 jam
        if last and (now - last).total_seconds() < 3600:
            return news_cache["events"]

        url = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
        resp = req_lib.get(url, timeout=10)
        data = resp.json()

        high_impact = []
        for event in data:
            if event.get("impact", "").lower() != "high":
                continue
            try:
                # Parse waktu event ke WIB
                dt_str  = event.get("date", "")
                dt_utc  = datetime.strptime(dt_str, "%Y-%m-%dT%H:%M:%S%z")
                dt_wib  = dt_utc.astimezone(WIB)
                high_impact.append({"time": dt_wib, "title": event.get("title", ""), "currency": event.get("currency", "")})
            except Exception:
                continue

        news_cache["events"]     = high_impact
        news_cache["last_fetch"] = now
        logger.info("News fetched: %d high-impact events", len(high_impact))
        return high_impact
    except Exception as e:
        logger.error("fetch_news: %s", e)
        return news_cache.get("events", [])

def is_news_time(pair):
    """Cek apakah sekarang dekat dengan high-impact news yang relevan dengan pair."""
    if not settings["news_filter"]:
        return False, ""
    try:
        events  = fetch_news_events()
        now     = now_wib()
        pause   = settings["news_pause_minutes"]
        # Ambil currency dari pair
        base, quote = pair.split("_")
        for event in events:
            if event["currency"] not in (base, quote):
                continue
            diff = (event["time"] - now).total_seconds() / 60
            if -pause <= diff <= pause:
                return True, "{} ({})".format(event["title"], event["currency"])
    except Exception as e:
        logger.error("is_news_time: %s", e)
    return False, ""


# ══════════════════════════════════════════
# OANDA
# ══════════════════════════════════════════

def get_candles(pair, count=60, granularity="D"):
    r = instruments.InstrumentsCandles(pair, params={"count": count, "granularity": granularity})
    client.request(r)
    return r.response["candles"]

def get_closes(pair, count=60):
    return [float(c["mid"]["c"]) for c in get_candles(pair, count) if c["complete"]]

def calculate_ema(data, period):
    return pd.Series(data).ewm(span=period, adjust=False).mean().tolist()

def calculate_adx(pair, period=14):
    try:
        candles   = [c for c in get_candles(pair, count=period*3) if c["complete"]]
        if len(candles) < period + 1: return 999
        highs     = [float(c["mid"]["h"]) for c in candles]
        lows      = [float(c["mid"]["l"]) for c in candles]
        closes    = [float(c["mid"]["c"]) for c in candles]
        tr_l, pdm_l, ndm_l = [], [], []
        for i in range(1, len(closes)):
            h, l, pc = highs[i], lows[i], closes[i-1]
            tr_l.append(max(h-l, abs(h-pc), abs(l-pc)))
            pdm_l.append(max(h-highs[i-1], 0) if (h-highs[i-1]) > (lows[i-1]-l) else 0)
            ndm_l.append(max(lows[i-1]-l, 0) if (lows[i-1]-l) > (h-highs[i-1]) else 0)
        def smooth(d, p):
            r = [sum(d[:p])]
            for i in range(p, len(d)): r.append(r[-1] - r[-1]/p + d[i])
            return r
        atr = smooth(tr_l, period); pDI = smooth(pdm_l, period); nDI = smooth(ndm_l, period)
        dx_l = []
        for i in range(len(atr)):
            if atr[i] == 0: continue
            p = 100*pDI[i]/atr[i]; n = 100*nDI[i]/atr[i]
            dx_l.append(100*abs(p-n)/(p+n) if (p+n) > 0 else 0)
        return round(sum(dx_l[-period:])/period, 2) if dx_l else 0
    except Exception as e:
        logger.error("ADX [%s]: %s", pair, e)
        return 999

def get_ema_signal(pair):
    closes = get_closes(pair, 60)
    fast, slow = settings["ema_fast"], settings["ema_slow"]
    if len(closes) < slow + 2: return None
    ef = calculate_ema(closes, fast); es = calculate_ema(closes, slow)
    c_f, c_s = ef[-1], es[-1]; p_f, p_s = ef[-2], es[-2]
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

def get_last_trade_pl(ot):
    if not ot: return None
    return float(sorted(ot, key=lambda t: t["openTime"], reverse=True)[0]["unrealizedPL"])

def get_total_pl(ot): return sum(float(t["unrealizedPL"]) for t in ot)

def count_buy_sell(ot):
    return ([t for t in ot if int(float(t["currentUnits"])) > 0],
            [t for t in ot if int(float(t["currentUnits"])) < 0])

def get_account_info():
    r = accounts.AccountSummary(ACCOUNT_ID)
    client.request(r)
    a = r.response["account"]
    balance     = float(a["balance"])
    margin_used = float(a["marginUsed"])
    nav         = float(a["NAV"])
    margin_pct  = (margin_used / nav * 100) if nav > 0 else 0
    return {
        "balance":     balance,
        "nav":         nav,
        "pl":          float(a["unrealizedPL"]),
        "margin":      margin_used,
        "free_margin": float(a["marginAvailable"]),
        "margin_pct":  round(margin_pct, 1),
    }

def calculate_lot(balance):
    """Hitung lot dinamis berdasarkan % balance."""
    if not settings["lot_dynamic"]:
        return settings["lot_size"]
    risk_amount = balance * settings["lot_risk_pct"] / 100
    # Asumsi 1 lot = $10 per pip, SL = 30 pip
    lot = round(risk_amount / 300, 2)
    return max(0.01, min(lot, 10.0))

def place_order(pair, direction, lot=None):
    if lot is None:
        lot = settings["lot_size"]
    units = str(int(lot * 100000))
    if direction == "sell": units = "-" + units
    r = orders.OrderCreate(ACCOUNT_ID, data={"order": {"type": "MARKET", "instrument": pair, "units": units}})
    client.request(r)
    logger.info("[%s] %s lot=%.2f placed.", pair, direction.upper(), lot)

def close_all_trades(pair, ot):
    pl = get_total_pl(ot)
    for t in ot:
        try: client.request(trades.TradeClose(ACCOUNT_ID, tradeID=t["id"]))
        except Exception as e: logger.error("[%s] close error: %s", pair, e)
    return pl


# ══════════════════════════════════════════
# MONITORS
# ══════════════════════════════════════════

async def monitor_margin(app):
    """Monitor margin level, kirim warning & stop entry kalau kritis."""
    global margin_warned, emergency_stop
    while True:
        try:
            if not any(pair_active.values()):
                await asyncio.sleep(60)
                continue
            info       = get_account_info()
            margin_pct = info["margin_pct"]
            warn_lvl   = settings["margin_warning"]
            stop_lvl   = settings["margin_stop"]

            if margin_pct >= stop_lvl and not emergency_stop:
                emergency_stop = True
                for pair in ALL_PAIRS: stop_pair(pair)
                await app.bot.send_message(ALLOWED_USER_ID,
                    "🚨 *MARGIN CRITICAL\\!*\n\n"
                    "Margin used: `{}%`\n"
                    "Limit: `{}%`\n\n"
                    "Semua pair dihentikan untuk mencegah margin call\\.".format(
                        em(str(margin_pct)), em(str(stop_lvl))),
                    parse_mode="MarkdownV2")

            elif margin_pct >= warn_lvl and not margin_warned:
                margin_warned = True
                await app.bot.send_message(ALLOWED_USER_ID,
                    "⚠️ *MARGIN WARNING*\n\n"
                    "Margin used: `{}%`\n"
                    "Warning level: `{}%`\n\n"
                    "Pertimbangkan untuk close sebagian posisi\\.".format(
                        em(str(margin_pct)), em(str(warn_lvl))),
                    parse_mode="MarkdownV2")

            elif margin_pct < warn_lvl:
                margin_warned = False  # Reset warning

        except Exception as e:
            logger.error("monitor_margin: %s", e)
        await asyncio.sleep(60)


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
                for pair in ALL_PAIRS: stop_pair(pair)
                await app.bot.send_message(ALLOWED_USER_ID,
                    "🚨 *EMERGENCY STOP\\!*\n\n"
                    "Daily loss limit tercapai\\!\n"
                    "Total floating: `{}`\n"
                    "Limit: `{}`\n\nSemua pair dihentikan\\.".format(
                        em("${:.2f}".format(total_pl)),
                        em("${:.2f}".format(settings["daily_loss_limit"]))),
                    parse_mode="MarkdownV2")
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
            all_trades = get_all_open_trades()
            total_pl   = sum(float(t["unrealizedPL"]) for t in all_trades)
            info       = get_account_info()
            is_tt, _   = is_trading_time()
            await app.bot.send_message(ALLOWED_USER_ID,
                "⏰ *Ringkasan Otomatis*\n\n"
                "🤖 Uptime: `{}`\n"
                "💰 Balance: `{}`\n"
                "📊 Floating P/L: `{}`\n"
                "🔒 Margin: `{}%`\n"
                "📋 Pair aktif: `{}/{}`\n"
                "🕐 Jam trading: `{}`\n\n"
                "📈 Sesi ini:\n"
                "  Trade: `{}` \\| Win: `{}` \\| Loss: `{}`\n"
                "  P/L sesi: `{}`".format(
                    em(uptime_str()),
                    em("${:.2f}".format(info["balance"])),
                    em("${:.2f}".format(total_pl)),
                    em(str(info["margin_pct"])),
                    active_pair_count(), len(ALL_PAIRS),
                    "Aktif ✅" if is_tt else "Nonaktif ⭕",
                    daily_stats["trades"], daily_stats["wins"], daily_stats["losses"],
                    em("${:.2f}".format(daily_stats["total_pl"]))),
                parse_mode="MarkdownV2")
        except Exception as e:
            logger.error("auto_summary: %s", e)


# ══════════════════════════════════════════
# TRADING LOOP
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
            signal   = get_ema_signal(pair)
            ot       = get_open_trades(pair)
            buys, sells = count_buy_sell(ot)
            n_buy, n_sell = len(buys), len(sells)
            total_pl = get_total_pl(ot)
            lbl      = em(pair_label(pair))
            pl_s     = em("${:.2f}".format(total_pl))

            # Update peak profit
            if ot and total_pl > state["peak_profit"]:
                state["peak_profit"] = total_pl

            # ── 1. Hard SL ──
            if ot and total_pl <= settings["hard_sl"]:
                pl_c = close_all_trades(pair, ot)
                log_trade(pair, "all", "hard_sl", pl_c)
                state.update({"last_signal": None, "waiting_cross": None, "peak_profit": 0.0})
                await app.bot.send_message(ALLOWED_USER_ID,
                    "🛑 *{}* \u2014 HARD SL\\!\n"
                    "Floating: `{}` \\| Limit: `{}`\n"
                    "`{}` posisi ditutup\\.".format(
                        lbl, pl_s, em("${:.2f}".format(settings["hard_sl"])), len(ot)),
                    parse_mode="MarkdownV2")
                await asyncio.sleep(settings["check_interval"])
                continue

            # ── 2. Trailing TP ──
            if settings["trailing_tp"] and ot and state["peak_profit"] >= settings["total_tp"]:
                if state["peak_profit"] - total_pl >= settings["trailing_pullback"]:
                    pl_c = close_all_trades(pair, ot)
                    log_trade(pair, "all", "trailing_tp", pl_c)
                    state.update({"last_signal": None, "waiting_cross": None, "peak_profit": 0.0})
                    await app.bot.send_message(ALLOWED_USER_ID,
                        "📍 *{}* \u2014 TRAILING TP\\!\n"
                        "Peak: `{}` \u2192 Close: `{}`".format(
                            lbl,
                            em("${:.2f}".format(state["peak_profit"])), pl_s),
                        parse_mode="MarkdownV2")
                    await asyncio.sleep(settings["check_interval"])
                    continue

            # ── 3. Normal TP ──
            if ot and total_pl >= settings["total_tp"] and not settings["trailing_tp"]:
                pl_c = close_all_trades(pair, ot)
                log_trade(pair, "all", "tp", pl_c)
                state.update({"last_signal": None, "waiting_cross": None, "peak_profit": 0.0})
                await app.bot.send_message(ALLOWED_USER_ID,
                    "🎯 *{}* \u2014 TP\\! Profit: `{}`".format(lbl, pl_s),
                    parse_mode="MarkdownV2")
                await asyncio.sleep(settings["check_interval"])
                continue

            # ── 4. Crossing berlawanan ──
            if ot:
                if n_buy > 0 and signal in ("sell", "sell_active"):
                    pl_c = close_all_trades(pair, ot)
                    log_trade(pair, "buy", "cross_close", pl_c)
                    state.update({"last_signal": None, "waiting_cross": "sell", "peak_profit": 0.0})
                    await app.bot.send_message(ALLOWED_USER_ID,
                        "🔄 *{}* \u2014 Death Cross\\!\nBUY ditutup\\. P/L: `{}`".format(lbl, pl_s),
                        parse_mode="MarkdownV2")
                    await asyncio.sleep(settings["check_interval"])
                    continue
                if n_sell > 0 and signal in ("buy", "buy_active"):
                    pl_c = close_all_trades(pair, ot)
                    log_trade(pair, "sell", "cross_close", pl_c)
                    state.update({"last_signal": None, "waiting_cross": "buy", "peak_profit": 0.0})
                    await app.bot.send_message(ALLOWED_USER_ID,
                        "🔄 *{}* \u2014 Golden Cross\\!\nSELL ditutup\\. P/L: `{}`".format(lbl, pl_s),
                        parse_mode="MarkdownV2")
                    await asyncio.sleep(settings["check_interval"])
                    continue

            # ── 5. Entry baru ──
            can_buy  = signal == "buy"  and state["last_signal"] != "buy"  and n_buy  == 0 and state["waiting_cross"] != "sell"
            can_sell = signal == "sell" and state["last_signal"] != "sell" and n_sell == 0 and state["waiting_cross"] != "buy"

            if can_buy or can_sell:
                # Check trading hours
                tt_ok, tt_reason = is_trading_time()
                if not tt_ok:
                    logger.info("[%s] Skip entry: %s", pair, tt_reason)
                    await asyncio.sleep(settings["check_interval"])
                    continue

                # Check news
                news_ok, news_title = is_news_time(pair)
                if news_ok:
                    logger.info("[%s] Skip entry: news %s", pair, news_title)
                    await asyncio.sleep(settings["check_interval"])
                    continue

                # Check margin
                try:
                    info = get_account_info()
                    if info["margin_pct"] >= settings["margin_stop"]:
                        logger.warning("[%s] Skip entry: margin critical %.1f%%", pair, info["margin_pct"])
                        await asyncio.sleep(settings["check_interval"])
                        continue
                    # Lot dinamis
                    lot = calculate_lot(info["balance"])
                except Exception:
                    lot = settings["lot_size"]

                # Check ADX
                adx_val = 0
                if settings["adx_filter"]:
                    adx_val = calculate_adx(pair)
                    if adx_val < settings["adx_min"]:
                        logger.info("[%s] Skip entry: ADX %.1f < %d", pair, adx_val, settings["adx_min"])
                        await asyncio.sleep(settings["check_interval"])
                        continue

                adx_str  = " \\| ADX: `{}`".format(em(str(adx_val))) if settings["adx_filter"] else ""
                lot_str  = em("{:.2f}".format(lot))
                dyn_str  = " \\(dinamis\\)" if settings["lot_dynamic"] else ""

                if can_buy:
                    place_order(pair, "buy", lot)
                    log_trade(pair, "buy", "entry_1")
                    state.update({"last_signal": "buy", "waiting_cross": None, "peak_profit": 0.0})
                    await app.bot.send_message(ALLOWED_USER_ID,
                        "📈 *{}* \u2014 ENTRY BUY \\#1\n"
                        "EMA {}/{}{}\nLot: `{}`{}".format(
                            lbl, settings["ema_fast"], settings["ema_slow"],
                            adx_str, lot_str, dyn_str),
                        parse_mode="MarkdownV2")
                else:
                    place_order(pair, "sell", lot)
                    log_trade(pair, "sell", "entry_1")
                    state.update({"last_signal": "sell", "waiting_cross": None, "peak_profit": 0.0})
                    await app.bot.send_message(ALLOWED_USER_ID,
                        "📉 *{}* \u2014 ENTRY SELL \\#1\n"
                        "EMA {}/{}{}\nLot: `{}`{}".format(
                            lbl, settings["ema_fast"], settings["ema_slow"],
                            adx_str, lot_str, dyn_str),
                        parse_mode="MarkdownV2")

            # ── 6. Layering ──
            elif ot:
                last_pl = get_last_trade_pl(ot)
                total_layers = n_buy + n_sell
                if last_pl is not None and last_pl <= settings["layer_trigger"]:
                    if total_layers >= settings["max_layers"]:
                        logger.warning("[%s] Max layers reached.", pair)
                    else:
                        # Cek trading hours & news untuk layering juga
                        tt_ok, _ = is_trading_time()
                        news_ok, _ = is_news_time(pair)
                        try:
                            info = get_account_info()
                            margin_ok = info["margin_pct"] < settings["margin_stop"]
                            lot = calculate_lot(info["balance"])
                        except Exception:
                            margin_ok = True
                            lot = settings["lot_size"]

                        if tt_ok and not news_ok and margin_ok:
                            ls   = em("${:.2f}".format(last_pl))
                            lot_str = em("{:.2f}".format(lot))
                            if n_buy > 0 and signal in ("buy", "buy_active"):
                                place_order(pair, "buy", lot)
                                log_trade(pair, "buy", "layer_{}".format(n_buy+1))
                                await app.bot.send_message(ALLOWED_USER_ID,
                                    "📈 *{}* \u2014 LAYER BUY \\#{}\n"
                                    "Last: `{}` \\| Total: `{}`\n"
                                    "Layer: `{}/{}` \\| Lot: `{}`".format(
                                        lbl, n_buy+1, ls, pl_s,
                                        n_buy+1, settings["max_layers"], lot_str),
                                    parse_mode="MarkdownV2")
                            elif n_sell > 0 and signal in ("sell", "sell_active"):
                                place_order(pair, "sell", lot)
                                log_trade(pair, "sell", "layer_{}".format(n_sell+1))
                                await app.bot.send_message(ALLOWED_USER_ID,
                                    "📉 *{}* \u2014 LAYER SELL \\#{}\n"
                                    "Last: `{}` \\| Total: `{}`\n"
                                    "Layer: `{}/{}` \\| Lot: `{}`".format(
                                        lbl, n_sell+1, ls, pl_s,
                                        n_sell+1, settings["max_layers"], lot_str),
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
    if pair_active.get(pair): return False
    if active_pair_count() >= settings["max_active_pairs"]: return "max"
    pair_active[pair] = True
    pair_state[pair]  = {"last_signal": None, "waiting_cross": None, "peak_profit": 0.0}
    pair_tasks[pair]  = asyncio.create_task(trading_loop_pair(pair, app))
    return True

def stop_pair(pair):
    if not pair_active.get(pair): return False
    pair_active[pair] = False
    if pair in pair_tasks:
        pair_tasks[pair].cancel()
        del pair_tasks[pair]
    return True


# ══════════════════════════════════════════
# KEYBOARDS
# ══════════════════════════════════════════

def kb_main():
    estop = "🔓 Reset Emergency Stop" if emergency_stop else "🚨 Emergency Stop"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📋 Kelola Pair ({}/{})".format(active_pair_count(), settings["max_active_pairs"]), callback_data="menu_pairs")],
        [InlineKeyboardButton("⚙️ Setting",       callback_data="menu_settings"),
         InlineKeyboardButton("📊 Akun",          callback_data="menu_account")],
        [InlineKeyboardButton("📈 Posisi",        callback_data="menu_positions"),
         InlineKeyboardButton("📜 Log",           callback_data="menu_log")],
        [InlineKeyboardButton("🕐 Jam Trading",   callback_data="menu_hours"),
         InlineKeyboardButton("📰 News",          callback_data="menu_news")],
        [InlineKeyboardButton("▶️ ON Semua",      callback_data="all_on"),
         InlineKeyboardButton("⏹ OFF Semua",     callback_data="all_off")],
        [InlineKeyboardButton("❌ Close Semua",   callback_data="confirm_closeall")],
        [InlineKeyboardButton(estop,              callback_data="toggle_estop")],
    ])

def kb_pairs(page=0):
    per_page    = 9
    page_pairs  = ALL_PAIRS[page*per_page:(page+1)*per_page]
    total_pages = (len(ALL_PAIRS) + per_page - 1) // per_page
    rows = []
    for pair in page_pairs:
        icon = "✅" if pair_active.get(pair) else "⭕"
        rows.append([InlineKeyboardButton("{} {}".format(icon, pair_label(pair)), callback_data="toggle_" + pair)])
    nav = []
    if page > 0: nav.append(InlineKeyboardButton("◀️", callback_data="pairs_page_{}".format(page-1)))
    nav.append(InlineKeyboardButton("{}/{}".format(page+1, total_pages), callback_data="noop"))
    if page < total_pages - 1: nav.append(InlineKeyboardButton("▶️", callback_data="pairs_page_{}".format(page+1)))
    rows.append(nav)
    rows.append([InlineKeyboardButton("🏠 Menu", callback_data="menu_main")])
    return InlineKeyboardMarkup(rows)

def kb_settings():
    s   = settings
    adx = "ON ✅" if s["adx_filter"] else "OFF ⭕"
    ttp = "ON ✅" if s["trailing_tp"] else "OFF ⭕"
    dyn = "ON ✅" if s["lot_dynamic"] else "OFF ⭕"
    nws = "ON ✅" if s["news_filter"] else "OFF ⭕"
    thr = "ON ✅" if s["trading_hours"] else "OFF ⭕"
    fri = "ON ✅" if s["block_friday"] else "OFF ⭕"
    mon = "ON ✅" if s["block_monday"] else "OFF ⭕"
    notif = "{}s".format(s["notify_interval"]) if s["notify_interval"] > 0 else "OFF"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("━━━ Entry ━━━", callback_data="noop")],
        [InlineKeyboardButton("📦 Lot: {}".format(s["lot_size"]),               callback_data="set_lot_size"),
         InlineKeyboardButton("⚡ Lot Dinamis: {}".format(dyn),                 callback_data="toggle_lot_dynamic")],
        [InlineKeyboardButton("💹 Risk %: {}%".format(s["lot_risk_pct"]),       callback_data="set_lot_risk_pct")],
        [InlineKeyboardButton("📊 EMA Fast: {}".format(s["ema_fast"]),          callback_data="set_ema_fast"),
         InlineKeyboardButton("📊 EMA Slow: {}".format(s["ema_slow"]),          callback_data="set_ema_slow")],
        [InlineKeyboardButton("━━━ Layering ━━━", callback_data="noop")],
        [InlineKeyboardButton("📉 Trigger: ${}".format(s["layer_trigger"]),     callback_data="set_layer_trigger"),
         InlineKeyboardButton("🔢 Max: {}".format(s["max_layers"]),             callback_data="set_max_layers")],
        [InlineKeyboardButton("━━━ Exit ━━━", callback_data="noop")],
        [InlineKeyboardButton("🎯 TP: ${}".format(s["total_tp"]),               callback_data="set_total_tp"),
         InlineKeyboardButton("🛑 Hard SL: ${}".format(s["hard_sl"]),           callback_data="set_hard_sl")],
        [InlineKeyboardButton("📍 Trailing TP: {}".format(ttp),                 callback_data="toggle_trailing_tp"),
         InlineKeyboardButton("📍 Pullback: ${}".format(s["trailing_pullback"]), callback_data="set_trailing_pullback")],
        [InlineKeyboardButton("━━━ Filter ━━━", callback_data="noop")],
        [InlineKeyboardButton("📡 ADX: {}".format(adx),                         callback_data="toggle_adx_filter"),
         InlineKeyboardButton("📡 ADX Min: {}".format(s["adx_min"]),            callback_data="set_adx_min")],
        [InlineKeyboardButton("📰 News Filter: {}".format(nws),                 callback_data="toggle_news_filter"),
         InlineKeyboardButton("⏸ Pause: {}m".format(s["news_pause_minutes"]),  callback_data="set_news_pause_minutes")],
        [InlineKeyboardButton("🕐 Trading Hours: {}".format(thr),               callback_data="toggle_trading_hours")],
        [InlineKeyboardButton("📅 Blok Jumat: {}".format(fri),                  callback_data="toggle_block_friday"),
         InlineKeyboardButton("📅 Blok Senin: {}".format(mon),                  callback_data="toggle_block_monday")],
        [InlineKeyboardButton("━━━ Risk Global ━━━", callback_data="noop")],
        [InlineKeyboardButton("👥 Max Pair: {}".format(s["max_active_pairs"]),  callback_data="set_max_active_pairs")],
        [InlineKeyboardButton("🔴 Daily Limit: ${}".format(s["daily_loss_limit"]), callback_data="set_daily_loss_limit")],
        [InlineKeyboardButton("⚠️ Margin Warn: {}%".format(s["margin_warning"]), callback_data="set_margin_warning"),
         InlineKeyboardButton("🛑 Margin Stop: {}%".format(s["margin_stop"]),   callback_data="set_margin_stop")],
        [InlineKeyboardButton("━━━ Bot ━━━", callback_data="noop")],
        [InlineKeyboardButton("⏱ Interval: {}s".format(s["check_interval"]),   callback_data="set_check_interval"),
         InlineKeyboardButton("🔔 Summary: {}".format(notif),                   callback_data="set_notify_interval")],
        [InlineKeyboardButton("🏠 Menu", callback_data="menu_main")],
    ])

def kb_back():
    return InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Menu Utama", callback_data="menu_main")]])

def kb_confirm_closeall():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Ya, Close Semua", callback_data="do_closeall"),
         InlineKeyboardButton("❌ Batal",            callback_data="menu_main")],
    ])


# ══════════════════════════════════════════
# TEXT BUILDERS
# ══════════════════════════════════════════

def text_main():
    s       = settings
    tt_ok, tt_r = is_trading_time()
    tt_str  = "✅ Aktif" if tt_ok else "⭕ {}".format(tt_r)
    estop   = "🚨 EMERGENCY STOP" if emergency_stop else "✅ Normal"
    adx     = "ON" if s["adx_filter"] else "OFF"
    ttp     = "ON" if s["trailing_tp"] else "OFF"
    nws     = "ON" if s["news_filter"] else "OFF"
    return (
        "🤖 *Forex Trading Bot v3*\n\n"
        "Status: {}\n"
        "Uptime: `{}`\n"
        "Jam Trading: {}\n"
        "Pair aktif: `{}/{}`\n\n"
        "📦 Lot: `{}` \\| 🎯 TP: `${}` \\| 🛑 SL: `${}`\n"
        "📉 Layer: `${}` \\| Max: `{}`\n"
        "📍 Trailing: `{}` \\| ADX: `{}` \\| News: `{}`\n"
        "🔴 Daily Limit: `${}`\n\n"
        "Pilih menu:"
    ).format(
        estop, em(uptime_str()), em(tt_str),
        active_pair_count(), settings["max_active_pairs"],
        em(str(s["lot_size"])), em(str(s["total_tp"])), em(str(s["hard_sl"])),
        em(str(s["layer_trigger"])), s["max_layers"],
        ttp, adx, nws,
        em(str(s["daily_loss_limit"]))
    )

async def text_positions():
    lines = ["📈 *Status Posisi*\n"]
    grand = 0.0
    any_t = False
    for pair in ALL_PAIRS:
        try:
            ot = get_open_trades(pair)
            if not ot: continue
            any_t = True
            pl    = get_total_pl(ot)
            grand += pl
            buys, sells = count_buy_sell(ot)
            icon  = "✅" if pair_active.get(pair) else "⭕"
            arrow = "📈" if pl >= 0 else "📉"
            peak  = pair_state[pair]["peak_profit"]
            pk    = " Peak:`{}`".format(em("${:.2f}".format(peak))) if peak > 0 else ""
            lines.append("{} *{}*\n  {} B:`{}` S:`{}` P/L:`{}`{}".format(
                icon, em(pair_label(pair)), arrow,
                len(buys), len(sells), em("${:.2f}".format(pl)), pk))
        except Exception:
            pass
    if not any_t:
        lines.append("_Tidak ada posisi terbuka_")
    else:
        arrow = "📈" if grand >= 0 else "📉"
        lines.append("\n{} *Grand Total: `{}`*".format(arrow, em("${:.2f}".format(grand))))
    return "\n".join(lines)

async def text_account():
    try:
        info = get_account_info()
        wr   = "{:.0f}%".format(daily_stats["wins"]/daily_stats["trades"]*100) if daily_stats["trades"] > 0 else "N/A"
        m_warn = " ⚠️" if info["margin_pct"] >= settings["margin_warning"] else ""
        return (
            "📊 *Status Akun*\n\n"
            "💰 Balance: `{}`\n"
            "📈 NAV: `{}`\n"
            "📉 Floating P/L: `{}`\n"
            "🔒 Margin Used: `{}%`{}\n"
            "🟢 Free Margin: `{}`\n\n"
            "🤖 Pair aktif: `{}/{}`\n"
            "⏱ Uptime: `{}`\n"
            "🌐 Env: `{}`\n\n"
            "📊 *Statistik Sesi*\n"
            "Trade: `{}` \\| Win: `{}` \\| Loss: `{}`\n"
            "Win Rate: `{}`\n"
            "P/L sesi: `{}`"
        ).format(
            em("${:.2f}".format(info["balance"])),
            em("${:.2f}".format(info["nav"])),
            em("${:.2f}".format(info["pl"])),
            em(str(info["margin_pct"])), m_warn,
            em("${:.2f}".format(info["free_margin"])),
            active_pair_count(), settings["max_active_pairs"],
            em(uptime_str()), OANDA_ENV,
            daily_stats["trades"], daily_stats["wins"], daily_stats["losses"],
            em(wr),
            em("${:.2f}".format(daily_stats["total_pl"]))
        )
    except Exception as e:
        return "❌ Error: `{}`".format(em(str(e)))

def text_log():
    if not trade_log: return "📜 *Log Trade*\n\n_Belum ada trade_"
    lines = ["📜 *Log Trade*\n"]
    for t in reversed(trade_log[-50:]):
        pl_str = " `{}`".format(em("${:.2f}".format(t["pl"]))) if t["pl"] is not None else ""
        icon   = "📈" if t["direction"] == "buy" else ("📉" if t["direction"] == "sell" else "🔄")
        lines.append("{} `{}` *{}* \u2014 {}{}".format(
            icon, t["time"], em(t["pair"]), em(t["action"]), pl_str))
    return "\n".join(lines)

def text_hours():
    s = settings
    tt_ok, tt_r = is_trading_time()
    now = now_wib()
    return (
        "🕐 *Trading Hours*\n\n"
        "Waktu WIB sekarang: `{}`\n"
        "Status: {}\n\n"
        "Trading Hours: `{}` \\({}\\)\n"
        "Jam aktif: `{} \u2014 {}`\n"
        "Blok Jumat malam: `{}`\n"
        "Blok Senin pagi: `{}`\n\n"
        "_Ubah di menu Setting_"
    ).format(
        em(now.strftime("%H:%M:%S %A")),
        "✅ Aktif" if tt_ok else "⭕ " + em(tt_r),
        "ON ✅" if s["trading_hours"] else "OFF ⭕",
        "hanya entry di jam aktif" if s["trading_hours"] else "entry kapan saja",
        em(s["trading_start"]), em(s["trading_end"]),
        "ON ✅" if s["block_friday"] else "OFF ⭕",
        "ON ✅" if s["block_monday"] else "OFF ⭕",
    )

async def text_news():
    events = fetch_news_events()
    now    = now_wib()
    lines  = ["📰 *High\\-Impact News Minggu Ini*\n"]
    if not events:
        lines.append("_Tidak ada data atau gagal fetch_")
    else:
        for e in events[:15]:
            diff  = (e["time"] - now).total_seconds() / 60
            arrow = "🔴" if abs(diff) <= settings["news_pause_minutes"] else ("⏳" if diff > 0 else "✅")
            t_str = e["time"].strftime("%a %H:%M")
            lines.append("{} `{}` *{}* \\- {}".format(
                arrow, em(t_str), em(e["currency"]), em(e["title"][:30])))
    lines.append("\n🔴 = Dekat/sedang berlangsung \\| ⏳ = Akan datang \\| ✅ = Sudah lewat")
    return "\n".join(lines)


# ══════════════════════════════════════════
# SETTING CONFIG
# ══════════════════════════════════════════

SETTING_LABELS = {
    "lot_size":           "Lot Size (skrg: {})\nContoh: 0.01",
    "lot_risk_pct":       "Risk % per trade (skrg: {}%)\nContoh: 1.0",
    "ema_fast":           "EMA Fast (skrg: {})\nContoh: 20",
    "ema_slow":           "EMA Slow (skrg: {})\nContoh: 50",
    "layer_trigger":      "Layer Trigger $ (skrg: {})\nNegatif, contoh: -10",
    "max_layers":         "Max Layers (skrg: {})\nContoh: 5",
    "total_tp":           "Take Profit $ (skrg: {})\nContoh: 25",
    "hard_sl":            "Hard SL $ (skrg: {})\nNegatif, contoh: -30",
    "trailing_pullback":  "Trailing Pullback $ (skrg: {})\nContoh: 3",
    "adx_min":            "ADX Min (skrg: {})\nContoh: 20",
    "news_pause_minutes": "News Pause menit (skrg: {})\nContoh: 30",
    "trading_start":      "Jam mulai WIB (skrg: {})\nFormat HH:MM, contoh: 15:00",
    "trading_end":        "Jam selesai WIB (skrg: {})\nFormat HH:MM, contoh: 23:00",
    "max_active_pairs":   "Max Pair Aktif (skrg: {})\nContoh: 5",
    "daily_loss_limit":   "Daily Loss Limit $ (skrg: {})\nNegatif, contoh: -100",
    "margin_warning":     "Margin Warning % (skrg: {}%)\nContoh: 50",
    "margin_stop":        "Margin Stop % (skrg: {}%)\nContoh: 80",
    "check_interval":     "Check Interval detik (skrg: {})\nMin 10, contoh: 60",
    "notify_interval":    "Auto Summary detik (skrg: {})\n0=OFF, contoh: 3600",
}

INT_SETTINGS  = {"ema_fast", "ema_slow", "check_interval", "max_layers",
                 "max_active_pairs", "notify_interval", "adx_min", "news_pause_minutes"}
TIME_SETTINGS = {"trading_start", "trading_end"}

TOGGLE_SETTINGS = {
    "toggle_adx_filter":     "adx_filter",
    "toggle_trailing_tp":    "trailing_tp",
    "toggle_lot_dynamic":    "lot_dynamic",
    "toggle_news_filter":    "news_filter",
    "toggle_trading_hours":  "trading_hours",
    "toggle_block_friday":   "block_friday",
    "toggle_block_monday":   "block_monday",
}


# ══════════════════════════════════════════
# HANDLERS
# ══════════════════════════════════════════

async def start_command(update, context):
    global bot_start_time
    if not is_allowed(update): return
    if not bot_start_time: bot_start_time = datetime.now()
    await update.message.reply_text(text_main(), parse_mode="MarkdownV2", reply_markup=kb_main())


async def button_handler(update, context):
    global emergency_stop
    query = update.callback_query
    await query.answer()
    if query.from_user.id != ALLOWED_USER_ID: return
    data = query.data

    async def edit_main():
        await query.edit_message_text(text_main(), parse_mode="MarkdownV2", reply_markup=kb_main())
    async def edit_settings():
        await query.edit_message_text("⚙️ *Setting Bot v3*\n\nKetuk untuk ubah:", parse_mode="MarkdownV2", reply_markup=kb_settings())

    if data == "menu_main": await edit_main()
    elif data == "menu_settings": await edit_settings()

    elif data == "menu_pairs":
        cnt = active_pair_count(); mx = settings["max_active_pairs"]
        await query.edit_message_text(
            "📋 *Kelola Pair*\n\nAktif: `{}/{}` \\(maks {}\\)\nKetuk untuk toggle:".format(cnt, len(ALL_PAIRS), mx),
            parse_mode="MarkdownV2", reply_markup=kb_pairs(0))

    elif data.startswith("pairs_page_"):
        page = int(data[len("pairs_page_"):]); cnt = active_pair_count(); mx = settings["max_active_pairs"]
        await query.edit_message_text(
            "📋 *Kelola Pair*\n\nAktif: `{}/{}` \\(maks {}\\)\nKetuk untuk toggle:".format(cnt, len(ALL_PAIRS), mx),
            parse_mode="MarkdownV2", reply_markup=kb_pairs(page))

    elif data.startswith("toggle_") and data not in TOGGLE_SETTINGS and data != "toggle_estop":
        pair = data[len("toggle_"):]
        if pair_active.get(pair):
            stop_pair(pair)
            await query.answer("{} OFF".format(pair_label(pair)))
        else:
            if emergency_stop:
                await query.answer("Emergency Stop aktif! Reset dulu.", show_alert=True); return
            result = start_pair(pair, context.application)
            if result == "max":
                await query.answer("Maks {} pair aktif!".format(settings["max_active_pairs"]), show_alert=True); return
            await query.answer("{} ON".format(pair_label(pair)))
        cnt = active_pair_count(); mx = settings["max_active_pairs"]
        try:
            await query.edit_message_text(
                "📋 *Kelola Pair*\n\nAktif: `{}/{}` \\(maks {}\\)\nKetuk untuk toggle:".format(cnt, len(ALL_PAIRS), mx),
                parse_mode="MarkdownV2", reply_markup=kb_pairs(0))
        except Exception: pass

    elif data in TOGGLE_SETTINGS:
        key = TOGGLE_SETTINGS[data]
        settings[key] = not settings[key]
        await query.answer("{}: {}".format(key, "ON" if settings[key] else "OFF"))
        await edit_settings()

    elif data == "toggle_estop":
        if emergency_stop:
            emergency_stop = False
            await query.answer("Emergency Stop direset!", show_alert=True)
        else:
            emergency_stop = True
            for pair in ALL_PAIRS: stop_pair(pair)
            await query.answer("Emergency Stop aktif!", show_alert=True)
        await edit_main()

    elif data == "all_on":
        if emergency_stop:
            await query.answer("Emergency Stop aktif! Reset dulu.", show_alert=True); return
        count = 0
        for pair in ALL_PAIRS:
            if active_pair_count() >= settings["max_active_pairs"]: break
            if start_pair(pair, context.application) is True: count += 1
        await query.answer("{} pair diaktifkan!".format(count), show_alert=True)
        await edit_main()

    elif data == "all_off":
        for pair in ALL_PAIRS: stop_pair(pair)
        await query.answer("Semua pair OFF!")
        await edit_main()

    elif data.startswith("set_"):
        key   = data[len("set_"):]
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

    elif data == "menu_hours":
        await query.edit_message_text(text_hours(), parse_mode="MarkdownV2", reply_markup=kb_back())

    elif data == "menu_news":
        txt = await text_news()
        await query.edit_message_text(txt, parse_mode="MarkdownV2", reply_markup=kb_back())

    elif data == "confirm_closeall":
        await query.edit_message_text(
            "⚠️ *Yakin close SEMUA posisi?*", parse_mode="MarkdownV2",
            reply_markup=kb_confirm_closeall())

    elif data == "do_closeall":
        closed = 0; total = 0.0
        for pair in ALL_PAIRS:
            try:
                ot = get_open_trades(pair)
                if ot:
                    pl = close_all_trades(pair, ot)
                    total += pl; closed += len(ot)
                    log_trade(pair, "all", "manual_close", pl)
                    pair_state[pair]["peak_profit"] = 0.0
            except Exception as e:
                logger.error("closeall [%s]: %s", pair, e)
        await query.edit_message_text(
            "✅ Ditutup: `{}` posisi\nTotal P/L: `{}`".format(closed, em("${:.2f}".format(total))),
            parse_mode="MarkdownV2", reply_markup=kb_back())

    elif data == "noop": pass


async def receive_setting_value(update, context):
    if not is_allowed(update): return
    user_id = update.effective_user.id
    key     = pending_setting_key.get(user_id)
    if not key: return
    text = update.message.text.strip()
    try:
        if key in TIME_SETTINGS:
            # Validasi format HH:MM
            parts = text.split(":")
            if len(parts) != 2 or not all(p.isdigit() for p in parts):
                raise ValueError("Format harus HH:MM, contoh: 15:00")
            h, m = int(parts[0]), int(parts[1])
            if not (0 <= h <= 23 and 0 <= m <= 59):
                raise ValueError("Jam tidak valid")
            settings[key] = text
        else:
            value = float(text)
            # Validasi
            neg_keys = {"layer_trigger", "hard_sl", "daily_loss_limit"}
            pos_keys = {"lot_size", "lot_risk_pct", "total_tp", "trailing_pullback"}
            if key in neg_keys and value >= 0:
                raise ValueError("Harus negatif")
            if key in pos_keys and value <= 0:
                raise ValueError("Harus positif")
            if key == "check_interval" and value < 10:
                raise ValueError("Minimal 10 detik")
            if key == "notify_interval" and value < 0:
                raise ValueError("Minimal 0")
            if key == "ema_fast" and value >= settings["ema_slow"]:
                raise ValueError("EMA Fast harus < EMA Slow")
            if key == "ema_slow" and value <= settings["ema_fast"]:
                raise ValueError("EMA Slow harus > EMA Fast")
            if key in ("margin_warning", "margin_stop") and not (0 < value < 100):
                raise ValueError("Harus antara 0-100")
            if key == "margin_warning" and value >= settings["margin_stop"]:
                raise ValueError("Warning harus < Stop level")
            if key == "margin_stop" and value <= settings["margin_warning"]:
                raise ValueError("Stop harus > Warning level")
            settings[key] = int(value) if key in INT_SETTINGS else value

        del pending_setting_key[user_id]
        await update.message.reply_text(
            "✅ *{}* \u2192 `{}`".format(em(key), em(str(settings[key]))),
            parse_mode="MarkdownV2")
        await update.message.reply_text("⚙️ *Setting Bot v3*\n\nKetuk untuk ubah:",
            parse_mode="MarkdownV2", reply_markup=kb_settings())

    except ValueError as e:
        await update.message.reply_text(
            "❌ {}\nCoba lagi:".format(str(e)), parse_mode="MarkdownV2")


# ══════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════

async def post_init(app):
    global bot_start_time
    bot_start_time = datetime.now()
    asyncio.create_task(monitor_daily_loss(app))
    asyncio.create_task(monitor_margin(app))
    asyncio.create_task(auto_summary(app))
    logger.info("Bot v3 background tasks started.")


def main():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("menu",  start_command))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, receive_setting_value))
    logger.info("Bot v3 siap.")
    app.run_polling()

if __name__ == "__main__":
    main()
