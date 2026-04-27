import asyncio
import logging
from datetime import datetime, time as dtime
import pytz
import requests as req_lib
import xml.etree.ElementTree as ET
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, CallbackQueryHandler, MessageHandler, filters
import oandapyV20
from oandapyV20.endpoints import orders, trades, instruments, accounts
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
    "lot_size":             0.01,
    "dynamic_lot":          True,   # Lot scaling otomatis berdasarkan balance
    "dynamic_lot_per":      1000.0, # 0.01 lot per $1000 balance
    "ema_fast":             20,
    "ema_slow":             50,
    # Layering
    "layer_trigger":        -10.0,
    "max_layers":           5,
    # Exit
    "total_tp":             25.0,
    "hard_sl":              -30.0,
    "trailing_tp":          True,
    "trailing_pullback":    3.0,
    # Filter
    "adx_filter":           True,
    "adx_min":              20,
    "max_active_pairs":     5,
    # Trading Hours (WIB)
    "trading_hours":        True,   # Hanya trade di jam aktif
    "hour_start":           15,     # 15.00 WIB = London open
    "hour_end":             23,     # 23.00 WIB = NY close
    "skip_friday":          True,   # Skip entry Jumat > jam skip_friday_hour
    "skip_friday_hour":     21,     # Jumat setelah jam ini = stop entry
    "skip_monday":          True,   # Skip entry Senin < jam skip_monday_hour
    "skip_monday_hour":     10,     # Senin sebelum jam ini = stop entry
    # News filter
    "news_filter":          True,   # Pause saat high-impact news
    "news_pause_before":    30,     # Menit sebelum news
    "news_pause_after":     30,     # Menit setelah news
    # Margin protection
    "margin_warning":       200.0,  # Alert kalau margin level < ini %
    "margin_stop":          150.0,  # Stop semua entry kalau margin level < ini %
    # Risk global
    "daily_loss_limit":     -100.0,
    # Bot
    "check_interval":       60,
    "notify_interval":      3600,
}

pair_active         = {p: False for p in ALL_PAIRS}
pair_tasks          = {}
pair_state          = {
    p: {"last_signal": None, "waiting_cross": None, "peak_profit": 0.0}
    for p in ALL_PAIRS
}
pending_setting_key = {}
bot_start_time      = None
trade_log           = []
daily_stats         = {"trades": 0, "wins": 0, "losses": 0, "total_pl": 0.0}
emergency_stop      = False
news_cache          = []      # Cache berita dari Forex Factory
news_cache_time     = None
margin_warned       = False   # Sudah kirim warning margin hari ini?

logging.basicConfig(format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)
client = oandapyV20.API(access_token=OANDA_TOKEN, environment=OANDA_ENV)


# ══════════════════════════════════════════
# UTILS
# ══════════════════════════════════════════

def pair_label(pair):   return pair.replace("_", "/")
def em(text):
    text = str(text)
    for ch in r"_*[]()~`>#+-=|{}.!":
        text = text.replace(ch, "\\" + ch)
    return text
def is_allowed(update): return update.effective_user.id == ALLOWED_USER_ID
def active_pair_count(): return sum(1 for p in ALL_PAIRS if pair_active.get(p))
def uptime_str():
    if not bot_start_time: return "N/A"
    delta = datetime.now() - bot_start_time
    h, rem = divmod(int(delta.total_seconds()), 3600)
    m, s   = divmod(rem, 60)
    return "{}j {}m {}d".format(h, m, s)

def now_wib():
    return datetime.now(WIB)

def log_trade(pair, direction, action, pl=None):
    entry = {"time": datetime.now().strftime("%H:%M:%S"), "pair": pair_label(pair),
             "direction": direction, "action": action, "pl": pl}
    trade_log.append(entry)
    if len(trade_log) > 100: trade_log.pop(0)
    if pl is not None:
        daily_stats["trades"] += 1
        daily_stats["total_pl"] += pl
        if pl >= 0: daily_stats["wins"] += 1
        else:       daily_stats["losses"] += 1


# ══════════════════════════════════════════
# TRADING HOURS CHECK
# ══════════════════════════════════════════

def is_trading_time():
    """Cek apakah sekarang waktu yang aman untuk entry."""
    if not settings["trading_hours"]:
        return True, "Trading hours dinonaktifkan"

    now      = now_wib()
    hour     = now.hour
    weekday  = now.weekday()  # 0=Senin, 4=Jumat, 5=Sabtu, 6=Minggu

    # Weekend
    if weekday == 5 or weekday == 6:
        return False, "Weekend — market tutup"

    # Jumat sore
    if weekday == 4 and settings["skip_friday"] and hour >= settings["skip_friday_hour"]:
        return False, "Jumat malam — hindari gap weekend"

    # Senin pagi
    if weekday == 0 and settings["skip_monday"] and hour < settings["skip_monday_hour"]:
        return False, "Senin pagi — tunggu market stabil"

    # Jam trading
    h_start = settings["hour_start"]
    h_end   = settings["hour_end"]
    if not (h_start <= hour < h_end):
        return False, "Di luar jam trading ({}:00-{}:00 WIB)".format(h_start, h_end)

    return True, "OK"


# ══════════════════════════════════════════
# NEWS FILTER (Forex Factory RSS)
# ══════════════════════════════════════════

def fetch_news():
    """Ambil high-impact news dari Forex Factory RSS."""
    global news_cache, news_cache_time
    try:
        # Refresh tiap 1 jam
        if news_cache_time and (datetime.now() - news_cache_time).seconds < 3600:
            return news_cache

        resp = req_lib.get(
            "https://nfs.faireconomy.media/ff_calendar_thisweek.xml",
            timeout=10
        )
        root = ET.fromstring(resp.content)
        events = []
        for item in root.findall(".//event"):
            try:
                impact = item.findtext("impact", "")
                if impact.lower() != "high":
                    continue
                title   = item.findtext("title", "")
                country = item.findtext("country", "")
                date_s  = item.findtext("date", "")
                time_s  = item.findtext("time", "")
                if not date_s or not time_s:
                    continue
                dt_str = "{} {}".format(date_s, time_s)
                try:
                    dt = datetime.strptime(dt_str, "%m-%d-%Y %I:%M%p")
                    # Convert dari EST ke WIB (+12 jam dari EST)
                    dt_wib = dt.replace(tzinfo=pytz.timezone("US/Eastern")).astimezone(WIB)
                    dt_wib = dt_wib.replace(tzinfo=None)
                    events.append({"title": title, "country": country, "time": dt_wib})
                except Exception:
                    continue
            except Exception:
                continue

        news_cache      = events
        news_cache_time = datetime.now()
        logger.info("News cache updated: %d high-impact events", len(events))
        return events
    except Exception as e:
        logger.error("fetch_news: %s", e)
        return news_cache  # Return cache lama kalau gagal

def is_news_time():
    """Cek apakah sekarang terlalu dekat dengan high-impact news."""
    if not settings["news_filter"]:
        return False, None

    try:
        events  = fetch_news()
        now     = datetime.now()
        before  = settings["news_pause_before"]
        after   = settings["news_pause_after"]

        for ev in events:
            diff_min = (ev["time"] - now).total_seconds() / 60
            if -after <= diff_min <= before:
                return True, ev
        return False, None
    except Exception as e:
        logger.error("is_news_time: %s", e)
        return False, None

def get_upcoming_news(max_events=5):
    """Ambil news high-impact yang akan datang."""
    try:
        events = fetch_news()
        now    = datetime.now()
        upcoming = [e for e in events if (e["time"] - now).total_seconds() > 0]
        upcoming.sort(key=lambda x: x["time"])
        return upcoming[:max_events]
    except Exception:
        return []


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
    try:
        candles   = get_candles(pair, count=period*3, granularity="D")
        completed = [c for c in candles if c["complete"]]
        if len(completed) < period + 1:
            return 999
        highs  = [float(c["mid"]["h"]) for c in completed]
        lows   = [float(c["mid"]["l"]) for c in completed]
        closes = [float(c["mid"]["c"]) for c in completed]
        tr_list, pdm_list, ndm_list = [], [], []
        for i in range(1, len(closes)):
            h, l, pc = highs[i], lows[i], closes[i-1]
            tr  = max(h - l, abs(h - pc), abs(l - pc))
            pdm = max(h - highs[i-1], 0) if (h - highs[i-1]) > (lows[i-1] - l) else 0
            ndm = max(lows[i-1] - l, 0)  if (lows[i-1] - l)  > (h - highs[i-1]) else 0
            tr_list.append(tr); pdm_list.append(pdm); ndm_list.append(ndm)
        def smooth(data, p):
            result = [sum(data[:p])]
            for i in range(p, len(data)):
                result.append(result[-1] - result[-1]/p + data[i])
            return result
        atr  = smooth(tr_list, period)
        pDI  = smooth(pdm_list, period)
        nDI  = smooth(ndm_list, period)
        dx_list = []
        for i in range(len(atr)):
            if atr[i] == 0: continue
            pdi = 100 * pDI[i] / atr[i]
            ndi = 100 * nDI[i] / atr[i]
            dx  = 100 * abs(pdi - ndi) / (pdi + ndi) if (pdi + ndi) > 0 else 0
            dx_list.append(dx)
        if not dx_list: return 0
        return round(sum(dx_list[-period:]) / period, 2)
    except Exception as e:
        logger.error("ADX [%s]: %s", pair, e)
        return 999

def get_ema_signal(pair):
    closes = get_closes(pair, 60)
    fast, slow = settings["ema_fast"], settings["ema_slow"]
    if len(closes) < slow + 2: return None
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
    if not open_trades: return None
    return float(sorted(open_trades, key=lambda t: t["openTime"], reverse=True)[0]["unrealizedPL"])

def get_total_pl(open_trades):
    return sum(float(t["unrealizedPL"]) for t in open_trades)

def count_buy_sell(open_trades):
    buys  = [t for t in open_trades if int(float(t["currentUnits"])) > 0]
    sells = [t for t in open_trades if int(float(t["currentUnits"])) < 0]
    return len(buys), len(sells)

def get_account_info():
    r = accounts.AccountSummary(ACCOUNT_ID)
    client.request(r)
    a = r.response["account"]
    nav    = float(a["NAV"])
    margin = float(a["marginUsed"])
    margin_level = (nav / margin * 100) if margin > 0 else 9999
    return {
        "balance":      float(a["balance"]),
        "nav":          nav,
        "pl":           float(a["unrealizedPL"]),
        "margin":       margin,
        "free_margin":  float(a["marginAvailable"]),
        "margin_level": round(margin_level, 1),
    }

def get_lot_size(balance=None):
    """Hitung lot size dinamis berdasarkan balance."""
    if not settings["dynamic_lot"]:
        return settings["lot_size"]
    if balance is None:
        try:
            info    = get_account_info()
            balance = info["balance"]
        except Exception:
            return settings["lot_size"]
    lot = round((balance / settings["dynamic_lot_per"]) * 0.01, 4)
    return max(0.01, lot)

def place_order(pair, direction, lot=None):
    if lot is None:
        lot = get_lot_size()
    units = str(int(lot * 100000))
    if direction == "sell": units = "-" + units
    r = orders.OrderCreate(ACCOUNT_ID, data={"order": {"type": "MARKET", "instrument": pair, "units": units}})
    client.request(r)
    logger.info("[%s] %s placed lot=%.4f", pair, direction.upper(), lot)
    return lot

def close_all_trades(pair, open_trades):
    total_pl = get_total_pl(open_trades)
    for trade in open_trades:
        try:
            client.request(trades.TradeClose(ACCOUNT_ID, tradeID=trade["id"]))
        except Exception as e:
            logger.error("[%s] close error: %s", pair, e)
    return total_pl


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
                for pair in ALL_PAIRS: stop_pair(pair)
                await app.bot.send_message(ALLOWED_USER_ID,
                    "🚨 *EMERGENCY STOP\\!*\n\n"
                    "Daily loss limit tercapai\\!\n"
                    "Total floating: `{}`\n"
                    "Limit: `{}`\n\nSemua pair dihentikan\\.".format(
                        em("${:.2f}".format(total_pl)),
                        em("${:.2f}".format(settings["daily_loss_limit"]))
                    ), parse_mode="MarkdownV2")
        except Exception as e:
            logger.error("monitor_daily_loss: %s", e)
        await asyncio.sleep(30)


async def monitor_margin(app):
    """Monitor margin level dan kirim warning kalau kritis."""
    global margin_warned
    while True:
        try:
            if not any(pair_active.values()):
                await asyncio.sleep(60)
                continue
            info          = get_account_info()
            margin_level  = info["margin_level"]
            warn_level    = settings["margin_warning"]
            stop_level    = settings["margin_stop"]

            if margin_level < stop_level:
                # Stop semua entry baru
                for pair in ALL_PAIRS: stop_pair(pair)
                await app.bot.send_message(ALLOWED_USER_ID,
                    "🚨 *MARGIN KRITIS\\!*\n\n"
                    "Margin Level: `{}%`\n"
                    "Stop Level: `{}%`\n\n"
                    "Semua pair dihentikan untuk mencegah Margin Call\\!\n"
                    "Free Margin: `{}`".format(
                        em(str(margin_level)),
                        em(str(stop_level)),
                        em("${:.2f}".format(info["free_margin"]))
                    ), parse_mode="MarkdownV2")
                margin_warned = True

            elif margin_level < warn_level and not margin_warned:
                await app.bot.send_message(ALLOWED_USER_ID,
                    "⚠️ *PERINGATAN MARGIN\\!*\n\n"
                    "Margin Level: `{}%`\n"
                    "Warning Level: `{}%`\n"
                    "Free Margin: `{}`\n\n"
                    "Pertimbangkan untuk mengurangi posisi\\.".format(
                        em(str(margin_level)),
                        em(str(warn_level)),
                        em("${:.2f}".format(info["free_margin"]))
                    ), parse_mode="MarkdownV2")
                margin_warned = True

            elif margin_level >= warn_level:
                margin_warned = False  # Reset warning

        except Exception as e:
            logger.error("monitor_margin: %s", e)
        await asyncio.sleep(60)


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
            try:
                info    = get_account_info()
                balance = info["balance"]
                margin_level = info["margin_level"]
            except Exception:
                balance      = 0.0
                margin_level = 0.0

            trading_ok, trading_reason = is_trading_time()
            news_ok, news_ev = is_news_time()

            status_lines = []
            if not trading_ok:
                status_lines.append("⏰ {}".format(em(trading_reason)))
            if news_ok and news_ev:
                status_lines.append("📰 News: {}".format(em(news_ev["title"])))

            status_str = "\n".join(status_lines) if status_lines else "✅ Normal"

            await app.bot.send_message(ALLOWED_USER_ID,
                "⏰ *Ringkasan Otomatis*\n\n"
                "🤖 Uptime: `{}`\n"
                "💰 Balance: `{}`\n"
                "📊 Floating P/L: `{}`\n"
                "📋 Pair aktif: `{}/{}`\n"
                "🔒 Margin Level: `{}%`\n"
                "Status: {}\n\n"
                "📈 Sesi ini:\n"
                "  Trade: `{}` \\| Win: `{}` \\| Loss: `{}`\n"
                "  P/L sesi: `{}`".format(
                    em(uptime_str()),
                    em("${:.2f}".format(balance)),
                    em("${:.2f}".format(total_pl)),
                    active_pair_count(), len(ALL_PAIRS),
                    em(str(margin_level)),
                    status_str,
                    daily_stats["trades"], daily_stats["wins"], daily_stats["losses"],
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

            # Update peak profit
            if open_trades and total_pl > state["peak_profit"]:
                state["peak_profit"] = total_pl

            # ── 1. Hard SL ──
            if open_trades and total_pl <= settings["hard_sl"]:
                pl_closed = close_all_trades(pair, open_trades)
                log_trade(pair, "all", "hard_sl", pl_closed)
                state.update({"last_signal": None, "waiting_cross": None, "peak_profit": 0.0})
                await app.bot.send_message(ALLOWED_USER_ID,
                    "🛑 *{}* \u2014 HARD SL\\!\n"
                    "Floating: `{}` \\| Limit: `{}`\n"
                    "`{}` posisi ditutup\\.".format(
                        lbl, pl_s, em("${:.2f}".format(settings["hard_sl"])), len(open_trades)
                    ), parse_mode="MarkdownV2")
                await asyncio.sleep(settings["check_interval"])
                continue

            # ── 2. Trailing TP ──
            if settings["trailing_tp"] and open_trades and state["peak_profit"] >= settings["total_tp"]:
                if state["peak_profit"] - total_pl >= settings["trailing_pullback"]:
                    pl_closed = close_all_trades(pair, open_trades)
                    log_trade(pair, "all", "trailing_tp", pl_closed)
                    state.update({"last_signal": None, "waiting_cross": None, "peak_profit": 0.0})
                    await app.bot.send_message(ALLOWED_USER_ID,
                        "📍 *{}* \u2014 TRAILING TP\\!\n"
                        "Peak: `{}` \u2192 Close: `{}`".format(
                            lbl,
                            em("${:.2f}".format(state["peak_profit"])), pl_s
                        ), parse_mode="MarkdownV2")
                    await asyncio.sleep(settings["check_interval"])
                    continue

            # ── 3. Normal TP ──
            if open_trades and total_pl >= settings["total_tp"] and not settings["trailing_tp"]:
                pl_closed = close_all_trades(pair, open_trades)
                log_trade(pair, "all", "tp", pl_closed)
                state.update({"last_signal": None, "waiting_cross": None, "peak_profit": 0.0})
                await app.bot.send_message(ALLOWED_USER_ID,
                    "🎯 *{}* \u2014 TP\\!\nProfit: `{}` \\| `{}` posisi".format(
                        lbl, pl_s, len(open_trades)
                    ), parse_mode="MarkdownV2")
                await asyncio.sleep(settings["check_interval"])
                continue

            # ── 4. Crossing berlawanan ──
            if open_trades:
                if n_buy > 0 and signal in ("sell", "sell_active"):
                    pl_closed = close_all_trades(pair, open_trades)
                    log_trade(pair, "buy", "cross_close", pl_closed)
                    state.update({"last_signal": None, "waiting_cross": "sell", "peak_profit": 0.0})
                    await app.bot.send_message(ALLOWED_USER_ID,
                        "🔄 *{}* \u2014 Death Cross\\!\nBUY ditutup\\. P/L: `{}`".format(lbl, pl_s),
                        parse_mode="MarkdownV2")
                    await asyncio.sleep(settings["check_interval"])
                    continue
                if n_sell > 0 and signal in ("buy", "buy_active"):
                    pl_closed = close_all_trades(pair, open_trades)
                    log_trade(pair, "sell", "cross_close", pl_closed)
                    state.update({"last_signal": None, "waiting_cross": "buy", "peak_profit": 0.0})
                    await app.bot.send_message(ALLOWED_USER_ID,
                        "🔄 *{}* \u2014 Golden Cross\\!\nSELL ditutup\\. P/L: `{}`".format(lbl, pl_s),
                        parse_mode="MarkdownV2")
                    await asyncio.sleep(settings["check_interval"])
                    continue

            # ── 5. Cek kondisi untuk entry baru ──
            can_buy  = signal == "buy"  and state["last_signal"] != "buy"  and n_buy  == 0 and state["waiting_cross"] != "sell"
            can_sell = signal == "sell" and state["last_signal"] != "sell" and n_sell == 0 and state["waiting_cross"] != "buy"

            if can_buy or can_sell:
                # ── Cek trading hours ──
                trading_ok, trading_reason = is_trading_time()
                if not trading_ok:
                    logger.info("[%s] Skip entry: %s", pair, trading_reason)
                    await asyncio.sleep(settings["check_interval"])
                    continue

                # ── Cek news filter ──
                news_pause, news_ev = is_news_time()
                if news_pause and news_ev:
                    logger.info("[%s] Skip entry: news %s", pair, news_ev["title"])
                    await asyncio.sleep(settings["check_interval"])
                    continue

                # ── Cek margin ──
                try:
                    info = get_account_info()
                    if info["margin_level"] < settings["margin_stop"] and info["margin"] > 0:
                        logger.warning("[%s] Skip entry: margin level %.1f%%", pair, info["margin_level"])
                        await asyncio.sleep(settings["check_interval"])
                        continue
                    lot = get_lot_size(info["balance"])
                except Exception:
                    lot = settings["lot_size"]

                # ── Cek ADX ──
                adx_val = 0
                if settings["adx_filter"]:
                    adx_val = calculate_adx(pair)
                    if adx_val < settings["adx_min"]:
                        logger.info("[%s] Skip entry: ADX %.1f < %d", pair, adx_val, settings["adx_min"])
                        await asyncio.sleep(settings["check_interval"])
                        continue

                adx_str = " \\| ADX: `{}`".format(em(str(adx_val))) if settings["adx_filter"] else ""
                lot_str = em(str(round(lot, 4)))

                if can_buy:
                    place_order(pair, "buy", lot)
                    log_trade(pair, "buy", "entry_1")
                    state.update({"last_signal": "buy", "waiting_cross": None, "peak_profit": 0.0})
                    await app.bot.send_message(ALLOWED_USER_ID,
                        "📈 *{}* \u2014 ENTRY BUY \\#1\n"
                        "EMA{}/{}{} \\| Lot: `{}`".format(
                            lbl, settings["ema_fast"], settings["ema_slow"], adx_str, lot_str
                        ), parse_mode="MarkdownV2")
                else:
                    place_order(pair, "sell", lot)
                    log_trade(pair, "sell", "entry_1")
                    state.update({"last_signal": "sell", "waiting_cross": None, "peak_profit": 0.0})
                    await app.bot.send_message(ALLOWED_USER_ID,
                        "📉 *{}* \u2014 ENTRY SELL \\#1\n"
                        "EMA{}/{}{} \\| Lot: `{}`".format(
                            lbl, settings["ema_fast"], settings["ema_slow"], adx_str, lot_str
                        ), parse_mode="MarkdownV2")

            # ── 6. Layering ──
            elif open_trades:
                last_pl      = get_last_trade_pl(open_trades)
                total_layers = n_buy + n_sell
                if last_pl is not None and last_pl <= settings["layer_trigger"]:
                    if total_layers >= settings["max_layers"]:
                        logger.warning("[%s] Max layers reached.", pair)
                    else:
                        # Cek trading hours dan news untuk layering juga
                        trading_ok, _ = is_trading_time()
                        news_pause, _ = is_news_time()
                        if trading_ok and not news_pause:
                            try:
                                info = get_account_info()
                                lot  = get_lot_size(info["balance"])
                                if info["margin_level"] < settings["margin_stop"] and info["margin"] > 0:
                                    lot = None
                            except Exception:
                                lot = settings["lot_size"]

                            if lot is not None:
                                ls  = em("${:.2f}".format(last_pl))
                                if n_buy > 0 and signal in ("buy", "buy_active"):
                                    place_order(pair, "buy", lot)
                                    log_trade(pair, "buy", "layer_{}".format(n_buy+1))
                                    await app.bot.send_message(ALLOWED_USER_ID,
                                        "📈 *{}* \u2014 LAYER BUY \\#{}\n"
                                        "Last: `{}` \\| Total: `{}` \\| Layer: `{}/{}`".format(
                                            lbl, n_buy+1, ls, pl_s, n_buy+1, settings["max_layers"]
                                        ), parse_mode="MarkdownV2")
                                elif n_sell > 0 and signal in ("sell", "sell_active"):
                                    place_order(pair, "sell", lot)
                                    log_trade(pair, "sell", "layer_{}".format(n_sell+1))
                                    await app.bot.send_message(ALLOWED_USER_ID,
                                        "📉 *{}* \u2014 LAYER SELL \\#{}\n"
                                        "Last: `{}` \\| Total: `{}` \\| Layer: `{}/{}`".format(
                                            lbl, n_sell+1, ls, pl_s, n_sell+1, settings["max_layers"]
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
    estop_label = "🔓 Reset Emergency Stop" if emergency_stop else "🚨 Emergency Stop"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📋 Pair ({}/{})".format(active_pair_count(), settings["max_active_pairs"]), callback_data="menu_pairs")],
        [InlineKeyboardButton("⚙️ Setting",       callback_data="menu_settings"),
         InlineKeyboardButton("📊 Akun",          callback_data="menu_account")],
        [InlineKeyboardButton("📈 Posisi",        callback_data="menu_positions"),
         InlineKeyboardButton("📜 Log",           callback_data="menu_log")],
        [InlineKeyboardButton("📰 News",          callback_data="menu_news"),
         InlineKeyboardButton("⏰ Jam Trading",   callback_data="menu_hours")],
        [InlineKeyboardButton("▶️ ON Semua",      callback_data="all_on"),
         InlineKeyboardButton("⏹ OFF Semua",     callback_data="all_off")],
        [InlineKeyboardButton("❌ Close Semua",   callback_data="confirm_closeall")],
        [InlineKeyboardButton(estop_label,        callback_data="toggle_estop")],
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
    rows.append([InlineKeyboardButton("🏠 Menu", callback_data="menu_main")])
    return InlineKeyboardMarkup(rows)

def kb_settings():
    s   = settings
    adx = "ON ✅" if s["adx_filter"] else "OFF ⭕"
    ttp = "ON ✅" if s["trailing_tp"] else "OFF ⭕"
    th  = "ON ✅" if s["trading_hours"] else "OFF ⭕"
    nf  = "ON ✅" if s["news_filter"] else "OFF ⭕"
    dl  = "ON ✅" if s["dynamic_lot"] else "OFF ⭕"
    sf  = "ON ✅" if s["skip_friday"] else "OFF ⭕"
    sm  = "ON ✅" if s["skip_monday"] else "OFF ⭕"
    notif = "{}s".format(s["notify_interval"]) if s["notify_interval"] > 0 else "Off"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("── Entry ──", callback_data="noop")],
        [InlineKeyboardButton("📦 Lot: {}  💡 Dynamic: {}".format(s["lot_size"], dl), callback_data="set_lot_size")],
        [InlineKeyboardButton("💡 Toggle Dynamic Lot", callback_data="toggle_dynamic_lot")],
        [InlineKeyboardButton("💡 Per $: {}".format(s["dynamic_lot_per"]), callback_data="set_dynamic_lot_per")],
        [InlineKeyboardButton("📊 EMA Fast: {}".format(s["ema_fast"]), callback_data="set_ema_fast"),
         InlineKeyboardButton("📊 EMA Slow: {}".format(s["ema_slow"]), callback_data="set_ema_slow")],
        [InlineKeyboardButton("── Layering ──", callback_data="noop")],
        [InlineKeyboardButton("📉 Layer Trigger: ${}".format(s["layer_trigger"]), callback_data="set_layer_trigger")],
        [InlineKeyboardButton("🔢 Max Layers: {}".format(s["max_layers"]), callback_data="set_max_layers")],
        [InlineKeyboardButton("── Exit ──", callback_data="noop")],
        [InlineKeyboardButton("🎯 TP: ${}".format(s["total_tp"]), callback_data="set_total_tp"),
         InlineKeyboardButton("🛑 Hard SL: ${}".format(s["hard_sl"]), callback_data="set_hard_sl")],
        [InlineKeyboardButton("📍 Trailing TP: {}".format(ttp), callback_data="toggle_trailing_tp")],
        [InlineKeyboardButton("📍 Pullback: ${}".format(s["trailing_pullback"]), callback_data="set_trailing_pullback")],
        [InlineKeyboardButton("── Filter ──", callback_data="noop")],
        [InlineKeyboardButton("📡 ADX: {}  Min: {}".format(adx, s["adx_min"]), callback_data="set_adx_min")],
        [InlineKeyboardButton("📡 Toggle ADX Filter", callback_data="toggle_adx_filter")],
        [InlineKeyboardButton("── Trading Hours ──", callback_data="noop")],
        [InlineKeyboardButton("⏰ Hours: {}  {}-{}WIB".format(th, s["hour_start"], s["hour_end"]), callback_data="toggle_trading_hours")],
        [InlineKeyboardButton("⏰ Jam Start: {}".format(s["hour_start"]), callback_data="set_hour_start"),
         InlineKeyboardButton("⏰ Jam End: {}".format(s["hour_end"]), callback_data="set_hour_end")],
        [InlineKeyboardButton("📅 Skip Jumat: {}  h>{}".format(sf, s["skip_friday_hour"]), callback_data="toggle_skip_friday")],
        [InlineKeyboardButton("📅 Skip Senin: {}  h<{}".format(sm, s["skip_monday_hour"]), callback_data="toggle_skip_monday")],
        [InlineKeyboardButton("── News Filter ──", callback_data="noop")],
        [InlineKeyboardButton("📰 News Filter: {}".format(nf), callback_data="toggle_news_filter")],
        [InlineKeyboardButton("📰 Sebelum: {}m".format(s["news_pause_before"]), callback_data="set_news_pause_before"),
         InlineKeyboardButton("📰 Setelah: {}m".format(s["news_pause_after"]), callback_data="set_news_pause_after")],
        [InlineKeyboardButton("── Margin ──", callback_data="noop")],
        [InlineKeyboardButton("⚠️ Warn: {}%".format(s["margin_warning"]), callback_data="set_margin_warning"),
         InlineKeyboardButton("🛑 Stop: {}%".format(s["margin_stop"]), callback_data="set_margin_stop")],
        [InlineKeyboardButton("── Risk Global ──", callback_data="noop")],
        [InlineKeyboardButton("👥 Max Pair: {}".format(s["max_active_pairs"]), callback_data="set_max_active_pairs"),
         InlineKeyboardButton("🔴 Daily Limit: ${}".format(s["daily_loss_limit"]), callback_data="set_daily_loss_limit")],
        [InlineKeyboardButton("── Bot ──", callback_data="noop")],
        [InlineKeyboardButton("⏱ Interval: {}s".format(s["check_interval"]), callback_data="set_check_interval"),
         InlineKeyboardButton("🔔 Summary: {}".format(notif), callback_data="set_notify_interval")],
        [InlineKeyboardButton("🏠 Menu Utama", callback_data="menu_main")],
    ])

def kb_confirm_closeall():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Ya, Close Semua", callback_data="do_closeall"),
         InlineKeyboardButton("❌ Batal", callback_data="menu_main")],
    ])

def kb_back():
    return InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Menu Utama", callback_data="menu_main")]])


# ══════════════════════════════════════════
# TEXT BUILDERS
# ══════════════════════════════════════════

def text_main():
    s   = settings
    now = now_wib()
    trading_ok, trading_reason = is_trading_time()
    news_pause, news_ev        = is_news_time()
    estop        = "🚨 EMERGENCY STOP" if emergency_stop else "✅ Normal"
    if news_pause:
        title = news_ev["title"][:20] if news_ev else "News"
        trade_status = "📰 Pause: {}".format(em(title))
    elif not trading_ok:
        trade_status = "⏰ {}".format(em(trading_reason))
    else:
        trade_status = "✅ Entry OK"

    adx_s  = "ON" if s["adx_filter"]     else "OFF"
    ttp_s  = "ON" if s["trailing_tp"]    else "OFF"
    th_s   = "ON" if s["trading_hours"]  else "OFF"
    nf_s   = "ON" if s["news_filter"]    else "OFF"

    lines = [
        "🤖 *Forex Bot v3*",
        "",
        "Status: {}".format(estop),
        "Entry: {}".format(trade_status),
        "Waktu WIB: `{}`".format(em(now.strftime("%H:%M %a"))),
        "Uptime: `{}`".format(em(uptime_str())),
        "Pair aktif: `{}/{}`".format(active_pair_count(), settings["max_active_pairs"]),
        "",
        "🎯 TP: `${}` \\| 🛑 SL: `${}`".format(em(str(s["total_tp"])), em(str(s["hard_sl"]))),
        "📉 Layer: `${}` max `{}`".format(em(str(s["layer_trigger"])), s["max_layers"]),
        "📡 ADX: `{}` \\| 📍 Trailing: `{}`".format(adx_s, ttp_s),
        "⏰ Hours: `{}` \\| 📰 News: `{}`".format(th_s, nf_s),
        "",
        "Pilih menu:",
    ]
    return "\n".join(lines)

async def text_positions():
    lines = ["📈 *Status Posisi*\n"]
    grand_total = 0.0
    any_trade   = False
    for pair in ALL_PAIRS:
        try:
            ot = get_open_trades(pair)
            if not ot: continue
            any_trade    = True
            total_pl     = get_total_pl(ot)
            grand_total += total_pl
            n_buy, n_sell = count_buy_sell(ot)
            icon  = "✅" if pair_active.get(pair) else "⭕"
            arrow = "📈" if total_pl >= 0 else "📉"
            peak  = pair_state[pair]["peak_profit"]
            peak_str = " \\| Peak: `{}`".format(em("${:.2f}".format(peak))) if peak > 0 else ""
            lines.append("{} *{}*\n  {} B:`{}` S:`{}` P/L:`{}`{}".format(
                icon, em(pair_label(pair)), arrow, n_buy, n_sell,
                em("${:.2f}".format(total_pl)), peak_str))
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
        ml   = info["margin_level"]
        ml_icon = "🔴" if ml < settings["margin_stop"] else ("🟡" if ml < settings["margin_warning"] else "🟢")
        lot  = get_lot_size(info["balance"])
        wr   = "{:.0f}%".format(daily_stats["wins"] / daily_stats["trades"] * 100) if daily_stats["trades"] > 0 else "N/A"
        return (
            "📊 *Status Akun*\n\n"
            "💰 Balance: `{}`\n"
            "📈 NAV: `{}`\n"
            "📉 Floating P/L: `{}`\n"
            "🔒 Margin Used: `{}`\n"
            "🟢 Free Margin: `{}`\n"
            "{} Margin Level: `{}%`\n"
            "📦 Lot aktif: `{}`\n\n"
            "🤖 Pair: `{}/{}` \\| Uptime: `{}`\n"
            "🌐 Env: `{}`\n\n"
            "📊 *Statistik Sesi*\n"
            "Trade: `{}` \\| Win: `{}` \\| Loss: `{}`\n"
            "Win Rate: `{}` \\| P/L: `{}`"
        ).format(
            em("${:.2f}".format(info["balance"])),
            em("${:.2f}".format(info["nav"])),
            em("${:.2f}".format(info["pl"])),
            em("${:.2f}".format(info["margin"])),
            em("${:.2f}".format(info["free_margin"])),
            ml_icon, em(str(ml)),
            em(str(round(lot, 4))),
            active_pair_count(), settings["max_active_pairs"],
            em(uptime_str()), OANDA_ENV,
            daily_stats["trades"], daily_stats["wins"], daily_stats["losses"],
            em(wr), em("${:.2f}".format(daily_stats["total_pl"]))
        )
    except Exception as e:
        return "❌ Error: `{}`".format(em(str(e)))

def text_news():
    upcoming = get_upcoming_news(8)
    if not upcoming:
        return "📰 *High\\-Impact News*\n\n_Tidak ada news minggu ini atau gagal fetch_"
    lines = ["📰 *High\\-Impact News \\(Minggu Ini\\)*\n"]
    now   = datetime.now()
    is_pause, _ = is_news_time()
    if is_pause:
        lines.append("⚠️ *BOT SEDANG PAUSE KARENA NEWS*\n")
    for ev in upcoming:
        diff_min = int((ev["time"] - now).total_seconds() / 60)
        if diff_min < 60:
            when = "{}m lagi".format(diff_min)
        else:
            when = ev["time"].strftime("%a %H:%M WIB")
        lines.append("🔴 *{}* \\- {}\n    `{}`".format(
            em(ev["country"]), em(ev["title"]), em(when)))
    return "\n".join(lines)

def text_hours():
    s  = settings
    ok, reason = is_trading_time()
    now = now_wib()
    status = "✅ Entry OK" if ok else "⏰ {}".format(reason)
    return (
        "⏰ *Trading Hours*\n\n"
        "Sekarang: `{}` WIB\n"
        "Status: {}\n\n"
        "Trading Hours: `{}`\n"
        "Jam aktif: `{}:00 \u2013 {}:00 WIB`\n\n"
        "Skip Jumat setelah `{}:00`: `{}`\n"
        "Skip Senin sebelum `{}:00`: `{}`\n\n"
        "_Sesi London\\+NY: 15:00\\-23:00 WIB_\n"
        "_Sesi Tokyo: 07:00\\-15:00 WIB_"
    ).format(
        em(now.strftime("%H:%M %a")),
        em(status),
        "ON ✅" if s["trading_hours"] else "OFF ⭕",
        s["hour_start"], s["hour_end"],
        s["skip_friday_hour"], "ON ✅" if s["skip_friday"] else "OFF ⭕",
        s["skip_monday_hour"], "ON ✅" if s["skip_monday"] else "OFF ⭕",
    )

def text_log():
    if not trade_log:
        return "📜 *Log Trade*\n\n_Belum ada trade_"
    lines = ["📜 *Log Trade*\n"]
    for t in reversed(trade_log[-30:]):
        pl_str = " `{}`".format(em("${:.2f}".format(t["pl"]))) if t["pl"] is not None else ""
        icon   = "📈" if t["direction"] == "buy" else ("📉" if t["direction"] == "sell" else "🔄")
        lines.append("{} `{}` *{}* {}{}".format(
            icon, t["time"], em(t["pair"]), em(t["action"]), pl_str))
    return "\n".join(lines)


# ══════════════════════════════════════════
# SETTING CONFIG
# ══════════════════════════════════════════

SETTING_LABELS = {
    "lot_size":           "Lot Size base (sekarang: {})\nContoh: 0.01",
    "dynamic_lot_per":    "Dynamic lot per $X balance (sekarang: {})\nContoh: 1000 = 0.01 lot per $1000",
    "ema_fast":           "EMA Fast (sekarang: {})\nContoh: 20",
    "ema_slow":           "EMA Slow (sekarang: {})\nContoh: 50",
    "layer_trigger":      "Layer Trigger $ (sekarang: {})\nHarus negatif, contoh: -10",
    "max_layers":         "Max Layers (sekarang: {})\nContoh: 5",
    "total_tp":           "Take Profit $ (sekarang: {})\nContoh: 25",
    "hard_sl":            "Hard SL $ (sekarang: {})\nHarus negatif, contoh: -30",
    "trailing_pullback":  "Trailing Pullback $ (sekarang: {})\nContoh: 3",
    "adx_min":            "ADX Minimum (sekarang: {})\nContoh: 20",
    "hour_start":         "Jam mulai trading WIB (sekarang: {})\nContoh: 15",
    "hour_end":           "Jam selesai trading WIB (sekarang: {})\nContoh: 23",
    "skip_friday_hour":   "Skip Jumat setelah jam WIB (sekarang: {})\nContoh: 21",
    "skip_monday_hour":   "Skip Senin sebelum jam WIB (sekarang: {})\nContoh: 10",
    "news_pause_before":  "Pause sebelum news (menit) (sekarang: {})\nContoh: 30",
    "news_pause_after":   "Pause setelah news (menit) (sekarang: {})\nContoh: 30",
    "margin_warning":     "Margin warning level % (sekarang: {})\nContoh: 200",
    "margin_stop":        "Margin stop level % (sekarang: {})\nContoh: 150",
    "max_active_pairs":   "Max pair aktif (sekarang: {})\nContoh: 5",
    "daily_loss_limit":   "Daily loss limit $ (sekarang: {})\nHarus negatif, contoh: -100",
    "check_interval":     "Check interval detik (sekarang: {})\nMinimal 10",
    "notify_interval":    "Auto summary detik (sekarang: {})\n0 = nonaktif",
}

INT_SETTINGS = {
    "ema_fast", "ema_slow", "check_interval", "max_layers", "max_active_pairs",
    "notify_interval", "adx_min", "hour_start", "hour_end", "skip_friday_hour",
    "skip_monday_hour", "news_pause_before", "news_pause_after",
    "margin_warning", "margin_stop",
}

TOGGLE_SETTINGS = {
    "toggle_adx_filter":     ("adx_filter",     "ADX Filter"),
    "toggle_trailing_tp":    ("trailing_tp",     "Trailing TP"),
    "toggle_trading_hours":  ("trading_hours",   "Trading Hours"),
    "toggle_news_filter":    ("news_filter",     "News Filter"),
    "toggle_dynamic_lot":    ("dynamic_lot",     "Dynamic Lot"),
    "toggle_skip_friday":    ("skip_friday",     "Skip Jumat"),
    "toggle_skip_monday":    ("skip_monday",     "Skip Senin"),
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

    if data == "menu_main":       await edit_main()
    elif data == "menu_settings": await edit_settings()
    elif data == "noop":          pass

    elif data == "menu_pairs":
        cnt, mx = active_pair_count(), settings["max_active_pairs"]
        await query.edit_message_text(
            "📋 *Kelola Pair*\n\nAktif: `{}/{}` \\(maks {}\\)\nKetuk untuk toggle:".format(cnt, len(ALL_PAIRS), mx),
            parse_mode="MarkdownV2", reply_markup=kb_pairs(0))

    elif data.startswith("pairs_page_"):
        page = int(data[len("pairs_page_"):])
        cnt, mx = active_pair_count(), settings["max_active_pairs"]
        await query.edit_message_text(
            "📋 *Kelola Pair*\n\nAktif: `{}/{}` \\(maks {}\\)\nKetuk untuk toggle:".format(cnt, len(ALL_PAIRS), mx),
            parse_mode="MarkdownV2", reply_markup=kb_pairs(page))

    elif data.startswith("toggle_") and data not in TOGGLE_SETTINGS and data != "toggle_estop":
        pair = data[len("toggle_"):]
        if pair not in ALL_PAIRS: return
        if pair_active.get(pair):
            stop_pair(pair)
            await query.answer("{} ⏹ OFF".format(pair_label(pair)))
        else:
            if emergency_stop:
                await query.answer("⚠️ Emergency Stop aktif!", show_alert=True); return
            result = start_pair(pair, context.application)
            if result == "max":
                await query.answer("⚠️ Maks {} pair!".format(settings["max_active_pairs"]), show_alert=True); return
            await query.answer("{} ✅ ON".format(pair_label(pair)))
        cnt, mx = active_pair_count(), settings["max_active_pairs"]
        try:
            await query.edit_message_text(
                "📋 *Kelola Pair*\n\nAktif: `{}/{}` \\(maks {}\\)\nKetuk untuk toggle:".format(cnt, len(ALL_PAIRS), mx),
                parse_mode="MarkdownV2", reply_markup=kb_pairs(0))
        except Exception: pass

    elif data in TOGGLE_SETTINGS:
        key, label = TOGGLE_SETTINGS[data]
        settings[key] = not settings[key]
        status = "ON ✅" if settings[key] else "OFF ⭕"
        await query.answer("{}: {}".format(label, status))
        await edit_settings()

    elif data == "toggle_estop":
        if emergency_stop:
            emergency_stop = False
            await query.answer("✅ Emergency Stop direset!", show_alert=True)
        else:
            emergency_stop = True
            for pair in ALL_PAIRS: stop_pair(pair)
            await query.answer("🚨 Emergency Stop aktif!", show_alert=True)
        await edit_main()

    elif data == "all_on":
        if emergency_stop:
            await query.answer("⚠️ Emergency Stop aktif!", show_alert=True); return
        count = 0
        for pair in ALL_PAIRS:
            if active_pair_count() >= settings["max_active_pairs"]: break
            if start_pair(pair, context.application) is True: count += 1
        await query.answer("✅ {} pair ON!".format(count), show_alert=True)
        await edit_main()

    elif data == "all_off":
        for pair in ALL_PAIRS: stop_pair(pair)
        await query.answer("⏹ Semua OFF!", show_alert=True)
        await edit_main()

    elif data == "menu_account":
        txt = await text_account()
        await query.edit_message_text(txt, parse_mode="MarkdownV2", reply_markup=kb_back())

    elif data == "menu_positions":
        txt = await text_positions()
        await query.edit_message_text(txt, parse_mode="MarkdownV2", reply_markup=kb_back())

    elif data == "menu_log":
        await query.edit_message_text(text_log(), parse_mode="MarkdownV2", reply_markup=kb_back())

    elif data == "menu_news":
        await query.edit_message_text(text_news(), parse_mode="MarkdownV2", reply_markup=kb_back())

    elif data == "menu_hours":
        await query.edit_message_text(text_hours(), parse_mode="MarkdownV2", reply_markup=kb_back())

    elif data == "confirm_closeall":
        await query.edit_message_text(
            "⚠️ *Yakin close SEMUA posisi?*", parse_mode="MarkdownV2", reply_markup=kb_confirm_closeall())

    elif data == "do_closeall":
        closed, total = 0, 0.0
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

    elif data.startswith("set_"):
        key = data[len("set_"):]
        pending_setting_key[query.from_user.id] = key
        label = SETTING_LABELS.get(key, key).format(settings.get(key, "?"))
        await query.edit_message_text(
            "✏️ *Ubah {}*\n\n{}\n\nKirim nilai baru:".format(em(key), em(label)),
            parse_mode="MarkdownV2",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Batal", callback_data="menu_settings")]]))


async def receive_setting_value(update, context):
    if not is_allowed(update): return
    user_id = update.effective_user.id
    key     = pending_setting_key.get(user_id)
    if not key: return
    text = update.message.text.strip()
    try:
        value = float(text)
        # Validasi spesifik
        validators = {
            "lot_size":          (value > 0,                               "Harus > 0"),
            "layer_trigger":     (value < 0,                               "Harus negatif"),
            "max_layers":        (value >= 1,                              "Minimal 1"),
            "total_tp":          (value > 0,                               "Harus > 0"),
            "hard_sl":           (value < 0,                               "Harus negatif"),
            "trailing_pullback": (value > 0,                               "Harus > 0"),
            "adx_min":           (1 <= value <= 100,                       "Antara 1-100"),
            "max_active_pairs":  (1 <= value <= len(ALL_PAIRS),            "Antara 1-{}".format(len(ALL_PAIRS))),
            "daily_loss_limit":  (value < 0,                               "Harus negatif"),
            "check_interval":    (value >= 10,                             "Minimal 10"),
            "notify_interval":   (value >= 0,                              "Minimal 0"),
            "ema_fast":          (value >= 2,                              "Minimal 2"),
            "ema_slow":          (value >= 2,                              "Minimal 2"),
            "hour_start":        (0 <= value <= 23,                        "Antara 0-23"),
            "hour_end":          (0 <= value <= 23,                        "Antara 0-23"),
            "margin_warning":    (value > 0,                               "Harus > 0"),
            "margin_stop":       (value > 0,                               "Harus > 0"),
            "news_pause_before": (value >= 0,                              "Minimal 0"),
            "news_pause_after":  (value >= 0,                              "Minimal 0"),
        }
        if key in validators:
            ok, msg = validators[key]
            if not ok: raise ValueError(msg)
        if key == "ema_fast" and value >= settings["ema_slow"]:
            raise ValueError("EMA Fast harus < EMA Slow")
        if key == "ema_slow" and value <= settings["ema_fast"]:
            raise ValueError("EMA Slow harus > EMA Fast")
        if key == "margin_stop" and value >= settings["margin_warning"]:
            raise ValueError("Margin Stop harus < Margin Warning")

        settings[key] = int(value) if key in INT_SETTINGS else value
        del pending_setting_key[user_id]
        await update.message.reply_text(
            "✅ *{}* \u2192 `{}`".format(em(key), em(str(settings[key]))),
            parse_mode="MarkdownV2")
        await update.message.reply_text(
            "⚙️ *Setting Bot v3*\n\nKetuk untuk ubah:",
            parse_mode="MarkdownV2", reply_markup=kb_settings())
    except ValueError as e:
        await update.message.reply_text(
            "❌ `{}`\nCoba lagi:".format(em(str(e))), parse_mode="MarkdownV2")


# ══════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════

async def post_init(app):
    global bot_start_time
    bot_start_time = datetime.now()
    asyncio.create_task(monitor_daily_loss(app))
    asyncio.create_task(monitor_margin(app))
    asyncio.create_task(auto_summary(app))
    # Pre-fetch news
    asyncio.create_task(asyncio.to_thread(fetch_news))
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
