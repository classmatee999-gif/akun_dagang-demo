import asyncio
import logging
from datetime import datetime, timedelta
from collections import defaultdict
import pytz
import requests as req_lib
import xml.etree.ElementTree as ET
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, CallbackQueryHandler, MessageHandler, filters
import oandapyV20
from oandapyV20.endpoints import orders, trades, accounts
import pandas as pd
import numpy as np
import os

# ─────────────────────────────────────────
# KONFIGURASI
# ─────────────────────────────────────────
TELEGRAM_TOKEN  = os.environ.get("TELEGRAM_TOKEN", "").strip()
OANDA_TOKEN     = os.environ.get("OANDA_TOKEN", "").strip()
ACCOUNT_ID      = os.environ.get("ACCOUNT_ID", "").strip()
ALLOWED_USER_ID = int(os.environ.get("ALLOWED_USER_ID", "0").strip())
OANDA_ENV       = os.environ.get("OANDA_ENV", "practice").strip()

ALL_PAIRS = [
    "EUR_USD", "GBP_USD", "USD_JPY", "AUD_USD",
    "USD_CAD", "NZD_USD", "EUR_GBP", "EUR_JPY",
    "AUD_JPY", "EUR_AUD", "EUR_CAD", "GBP_CAD", "CAD_JPY",
]

# Korelasi pair — tidak boleh aktif bersamaan
CORRELATION_GROUPS = [
    {"EUR_USD", "GBP_USD", "EUR_GBP"},   # EUR & GBP majors
    {"USD_JPY", "EUR_JPY", "AUD_JPY", "CAD_JPY"},  # JPY crosses
    {"AUD_USD", "AUD_JPY", "EUR_AUD"},   # AUD pairs
    {"USD_CAD", "GBP_CAD", "EUR_CAD"},   # CAD pairs
]

WIB = pytz.timezone("Asia/Jakarta")

settings = {
    # Entry
    "lot_size":             0.01,
    "dynamic_lot":          True,
    "dynamic_lot_per":      1000.0,
    "ema_fast":             20,
    "ema_slow":             50,
    # Multi-timeframe
    "mtf_filter":           True,    # H4 confirmation
    # RSI filter
    "rsi_filter":           True,
    "rsi_period":           14,
    "rsi_buy_min":          50,      # RSI harus > ini untuk buy
    "rsi_sell_max":         50,      # RSI harus < ini untuk sell
    # Layering
    "layer_trigger":        -10.0,
    "max_layers":           5,
    "use_fib_layers":       True,    # Layer di Fib levels bukan fixed $
    # Exit
    "total_tp":             25.0,
    "hard_sl":              -30.0,
    "trailing_tp":          True,
    "trailing_pullback":    3.0,
    "partial_close":        True,    # Close sebagian saat profit tertentu
    "partial_close_pct":    50,      # % posisi yang di-close saat partial
    "partial_close_at":     15.0,    # Partial close kalau profit >= ini
    "breakeven_at":         5.0,     # Pindah SL ke BE kalau profit >= ini
    # ATR-based SL/TP
    "atr_based":            True,
    "atr_period":           14,
    "atr_sl_multiplier":    2.0,     # Hard SL = ATR * multiplier
    "atr_tp_multiplier":    4.0,     # TP = ATR * multiplier
    # Filter
    "adx_filter":           True,
    "adx_min":              20,
    "max_active_pairs":     5,
    "correlation_filter":   True,
    # Trading Hours
    "trading_hours":        True,
    "hour_start":           15,
    "hour_end":             23,
    "skip_friday":          True,
    "skip_friday_hour":     21,
    "skip_monday":          True,
    "skip_monday_hour":     10,
    # News filter
    "news_filter":          True,
    "news_pause_before":    30,
    "news_pause_after":     30,
    # Margin protection
    "margin_warning":       200.0,
    "margin_stop":          150.0,
    # Performance tracking
    "min_winrate":          40.0,    # Nonaktifkan pair kalau winrate < ini (min 20 trades)
    "drawdown_recovery":    True,    # Kurangi lot 50% saat drawdown
    "drawdown_threshold":   -20.0,   # Drawdown threshold untuk recovery mode
    # Risk global
    "daily_loss_limit":     -100.0,
    # Bot
    "check_interval":       60,
    "notify_interval":      3600,
}

# ── State ──
pair_active         = {p: False for p in ALL_PAIRS}
pair_tasks          = {}
pair_state          = {
    p: {
        "last_signal":      None,
        "waiting_cross":    None,
        "peak_profit":      0.0,
        "partial_done":     False,   # Sudah partial close?
        "breakeven_set":    False,   # Sudah set break even?
        "entry_price":      None,    # Harga entry pertama
        "atr_sl":           None,    # Hard SL berbasis ATR
        "atr_tp":           None,    # TP berbasis ATR
    } for p in ALL_PAIRS
}

# Performance tracking per pair
pair_performance = {
    p: {
        "trades":   0,
        "wins":     0,
        "losses":   0,
        "total_pl": 0.0,
        "disabled": False,   # Auto-disabled karena winrate rendah
    } for p in ALL_PAIRS
}

# Peak balance untuk drawdown tracking
peak_balance        = 0.0
drawdown_mode       = False

pending_setting_key = {}
bot_start_time      = None
trade_log           = []
daily_stats         = {"trades": 0, "wins": 0, "losses": 0, "total_pl": 0.0}
emergency_stop      = False
unavailable_pairs   = set()
news_cache          = []
news_cache_time     = None
margin_warned       = False

logging.basicConfig(format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)
client = oandapyV20.API(access_token=OANDA_TOKEN, environment=OANDA_ENV)

_token_preview = OANDA_TOKEN[:6] + "..." + OANDA_TOKEN[-4:] if len(OANDA_TOKEN) > 10 else "EMPTY"
logger.info("Config — ENV: %s | ACCOUNT: %s | TOKEN: %s", OANDA_ENV, ACCOUNT_ID, _token_preview)


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

def active_pair_count():
    return sum(1 for p in ALL_PAIRS if pair_active.get(p))

def uptime_str():
    if not bot_start_time:
        return "N/A"
    delta = datetime.now() - bot_start_time
    h, rem = divmod(int(delta.total_seconds()), 3600)
    m, s   = divmod(rem, 60)
    return "{}j {}m {}d".format(h, m, s)

def now_wib():
    return datetime.now(WIB)

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
        perf = pair_performance[pair]
        perf["trades"] += 1
        perf["total_pl"] += pl
        if pl >= 0:
            daily_stats["wins"] += 1
            perf["wins"] += 1
        else:
            daily_stats["losses"] += 1
            perf["losses"] += 1
        # Cek winrate — nonaktifkan kalau terlalu rendah
        if perf["trades"] >= 20:
            wr = perf["wins"] / perf["trades"] * 100
            if wr < settings["min_winrate"] and not perf["disabled"]:
                perf["disabled"] = True
                stop_pair(pair)
                logger.warning("[%s] Auto-disabled: winrate %.1f%% < %.1f%%", pair, wr, settings["min_winrate"])


# ══════════════════════════════════════════
# CANDLES (direct REST — lebih reliable)
# ══════════════════════════════════════════

def get_candles(pair, count=60, granularity="D", retries=3):
    base = "https://api-fxpractice.oanda.com" if OANDA_ENV == "practice" else "https://api-fxtrade.oanda.com"
    url  = "{}/v3/instruments/{}/candles".format(base, pair)
    last_err = None
    for attempt in range(retries):
        try:
            resp = req_lib.get(url,
                headers={"Authorization": "Bearer " + OANDA_TOKEN, "Content-Type": "application/json"},
                params={"count": str(count), "granularity": granularity, "price": "M"},
                timeout=15)
            # 5xx error = server problem, retry
            if resp.status_code in (500, 502, 503, 504):
                last_err = Exception("{} server error".format(resp.status_code))
                import time; time.sleep(3)
                continue
            if resp.status_code != 200:
                raise Exception("{} {}".format(resp.status_code, resp.text[:200]))
            data = resp.json()
            if "candles" not in data:
                raise Exception("No candles: {}".format(data))
            return data["candles"]
        except req_lib.exceptions.Timeout:
            last_err = Exception("Timeout")
            import time; time.sleep(3)
            continue
        except req_lib.exceptions.ConnectionError as e:
            last_err = e
            import time; time.sleep(3)
            continue
    raise last_err or Exception("get_candles failed after {} retries".format(retries))

def get_closes(pair, count=60, granularity="D"):
    candles = get_candles(pair, count, granularity)
    return [float(c["mid"]["c"]) for c in candles if c["complete"]]

def get_ohlc(pair, count=60, granularity="D"):
    candles = get_candles(pair, count, granularity)
    completed = [c for c in candles if c["complete"]]
    return {
        "o": [float(c["mid"]["o"]) for c in completed],
        "h": [float(c["mid"]["h"]) for c in completed],
        "l": [float(c["mid"]["l"]) for c in completed],
        "c": [float(c["mid"]["c"]) for c in completed],
    }


# ══════════════════════════════════════════
# INDICATORS
# ══════════════════════════════════════════

def calculate_ema(data, period):
    return pd.Series(data).ewm(span=period, adjust=False).mean().tolist()

def calculate_rsi(closes, period=14):
    s     = pd.Series(closes)
    delta = s.diff()
    gain  = delta.clip(lower=0)
    loss  = -delta.clip(upper=0)
    avg_g = gain.ewm(com=period-1, min_periods=period).mean()
    avg_l = loss.ewm(com=period-1, min_periods=period).mean()
    rs    = avg_g / avg_l
    rsi   = 100 - (100 / (1 + rs))
    return rsi.tolist()

def calculate_atr(pair, period=14, granularity="D"):
    try:
        ohlc = get_ohlc(pair, count=period*3, granularity=granularity)
        highs, lows, closes = ohlc["h"], ohlc["l"], ohlc["c"]
        tr_list = []
        for i in range(1, len(closes)):
            tr = max(
                highs[i] - lows[i],
                abs(highs[i] - closes[i-1]),
                abs(lows[i]  - closes[i-1])
            )
            tr_list.append(tr)
        if not tr_list:
            return 0
        atr = pd.Series(tr_list).ewm(span=period, adjust=False).mean().iloc[-1]
        return round(atr, 6)
    except Exception as e:
        logger.error("ATR [%s]: %s", pair, e)
        return 0

def calculate_adx(pair, period=14):
    try:
        ohlc      = get_ohlc(pair, count=period*3, granularity="D")
        highs, lows, closes = ohlc["h"], ohlc["l"], ohlc["c"]
        if len(closes) < period + 1:
            return 999
        tr_list, pdm_list, ndm_list = [], [], []
        for i in range(1, len(closes)):
            h, l, pc = highs[i], lows[i], closes[i-1]
            tr  = max(h - l, abs(h - pc), abs(l - pc))
            pdm = max(h - highs[i-1], 0) if (h - highs[i-1]) > (lows[i-1] - l) else 0
            ndm = max(lows[i-1] - l, 0)  if (lows[i-1] - l) > (h - highs[i-1]) else 0
            tr_list.append(tr); pdm_list.append(pdm); ndm_list.append(ndm)
        def smooth(data, p):
            res = [sum(data[:p])]
            for i in range(p, len(data)):
                res.append(res[-1] - res[-1]/p + data[i])
            return res
        atr = smooth(tr_list, period)
        pDI = smooth(pdm_list, period)
        nDI = smooth(ndm_list, period)
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
        return round(sum(dx_list[-period:]) / period, 2)
    except Exception as e:
        logger.error("ADX [%s]: %s", pair, e)
        return 999

def get_fib_levels(pair, granularity="D", lookback=50):
    try:
        ohlc  = get_ohlc(pair, count=lookback, granularity=granularity)
        highs = ohlc["h"]
        lows  = ohlc["l"]
        swing_high = max(highs[-20:])
        swing_low  = min(lows[-20:])
        diff = swing_high - swing_low
        return {
            "0":    swing_high,
            "23.6": swing_high - 0.236 * diff,
            "38.2": swing_high - 0.382 * diff,
            "50":   swing_high - 0.500 * diff,
            "61.8": swing_high - 0.618 * diff,
            "100":  swing_low,
        }
    except Exception as e:
        logger.error("Fib [%s]: %s", pair, e)
        return {}

def get_ema_signal(pair):
    closes = get_closes(pair, 60, "D")
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

def get_h4_signal(pair):
    """Konfirmasi arah dari H4."""
    try:
        closes = get_closes(pair, 60, "H4")
        if len(closes) < 52:
            return None
        ef = calculate_ema(closes, settings["ema_fast"])
        es = calculate_ema(closes, settings["ema_slow"])
        if ef[-1] > es[-1]:  return "buy"
        if ef[-1] < es[-1]:  return "sell"
        return None
    except Exception as e:
        logger.error("H4 signal [%s]: %s", pair, e)
        return None

def get_rsi(pair):
    try:
        closes = get_closes(pair, 60, "D")
        if len(closes) < settings["rsi_period"] + 1:
            return 50
        rsi = calculate_rsi(closes, settings["rsi_period"])
        return round(rsi[-1], 2)
    except Exception as e:
        logger.error("RSI [%s]: %s", pair, e)
        return 50


# ══════════════════════════════════════════
# OANDA TRADING
# ══════════════════════════════════════════

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
    global drawdown_mode
    if not settings["dynamic_lot"]:
        lot = settings["lot_size"]
    else:
        if balance is None:
            try:
                info    = get_account_info()
                balance = info["balance"]
            except Exception:
                return settings["lot_size"]
        lot = round((balance / settings["dynamic_lot_per"]) * 0.01, 4)
        lot = max(0.01, lot)
    # Drawdown recovery — kurangi lot 50%
    if drawdown_mode and settings["drawdown_recovery"]:
        lot = max(0.01, round(lot * 0.5, 4))
    return lot

def place_order(pair, direction, lot=None):
    if lot is None:
        lot = get_lot_size()
    units = str(int(lot * 100000))
    if direction == "sell":
        units = "-" + units
    r = orders.OrderCreate(ACCOUNT_ID, data={
        "order": {"type": "MARKET", "instrument": pair, "units": units}
    })
    client.request(r)
    logger.info("[%s] %s lot=%.4f", pair, direction.upper(), lot)
    return lot

def close_all_trades(pair, open_trades):
    total_pl = get_total_pl(open_trades)
    for trade in open_trades:
        try:
            client.request(trades.TradeClose(ACCOUNT_ID, tradeID=trade["id"]))
        except Exception as e:
            logger.error("[%s] close error: %s", pair, e)
    return total_pl

def close_partial_trades(pair, open_trades, pct):
    """Close sebagian posisi berdasarkan persentase."""
    n_close = max(1, int(len(open_trades) * pct / 100))
    sorted_trades = sorted(open_trades, key=lambda t: t["openTime"])
    closed_pl = 0.0
    for trade in sorted_trades[:n_close]:
        try:
            closed_pl += float(trade["unrealizedPL"])
            client.request(trades.TradeClose(ACCOUNT_ID, tradeID=trade["id"]))
        except Exception as e:
            logger.error("[%s] partial close error: %s", pair, e)
    return closed_pl, n_close


# ══════════════════════════════════════════
# TRADING HOURS
# ══════════════════════════════════════════

def is_trading_time():
    if not settings["trading_hours"]:
        return True, "Hours filter off"
    now     = now_wib()
    hour    = now.hour
    weekday = now.weekday()
    if weekday in (5, 6):
        return False, "Weekend"
    if weekday == 4 and settings["skip_friday"] and hour >= settings["skip_friday_hour"]:
        return False, "Jumat malam"
    if weekday == 0 and settings["skip_monday"] and hour < settings["skip_monday_hour"]:
        return False, "Senin pagi"
    if not (settings["hour_start"] <= hour < settings["hour_end"]):
        return False, "Di luar jam trading ({}:00-{}:00 WIB)".format(
            settings["hour_start"], settings["hour_end"])
    return True, "OK"


# ══════════════════════════════════════════
# NEWS FILTER
# ══════════════════════════════════════════

def fetch_news():
    global news_cache, news_cache_time
    try:
        if news_cache_time and (datetime.now() - news_cache_time).seconds < 3600:
            return news_cache
        resp = req_lib.get(
            "https://nfs.faireconomy.media/ff_calendar_thisweek.xml",
            timeout=10)
        root   = ET.fromstring(resp.content)
        events = []
        for item in root.findall(".//event"):
            try:
                if (item.findtext("impact", "") or "").lower() != "high":
                    continue
                title  = item.findtext("title", "")
                country = item.findtext("country", "")
                date_s = item.findtext("date", "")
                time_s = item.findtext("time", "")
                if not date_s or not time_s:
                    continue
                dt = datetime.strptime("{} {}".format(date_s, time_s), "%m-%d-%Y %I:%M%p")
                dt_wib = dt.replace(tzinfo=pytz.timezone("US/Eastern")).astimezone(WIB).replace(tzinfo=None)
                events.append({"title": title, "country": country, "time": dt_wib})
            except Exception:
                continue
        news_cache      = events
        news_cache_time = datetime.now()
        return events
    except Exception as e:
        logger.error("fetch_news: %s", e)
        return news_cache

def is_news_time():
    if not settings["news_filter"]:
        return False, None
    try:
        events = fetch_news()
        now    = datetime.now()
        for ev in events:
            diff = (ev["time"] - now).total_seconds() / 60
            if -settings["news_pause_after"] <= diff <= settings["news_pause_before"]:
                return True, ev
        return False, None
    except Exception:
        return False, None

def get_upcoming_news(n=5):
    try:
        events   = fetch_news()
        now      = datetime.now()
        upcoming = sorted([e for e in events if (e["time"] - now).total_seconds() > 0],
                          key=lambda x: x["time"])
        return upcoming[:n]
    except Exception:
        return []


# ══════════════════════════════════════════
# CORRELATION FILTER
# ══════════════════════════════════════════

def is_correlated(pair):
    """Cek apakah ada pair yang berkorelasi sudah aktif."""
    if not settings["correlation_filter"]:
        return False
    for group in CORRELATION_GROUPS:
        if pair in group:
            for other in group:
                if other != pair and pair_active.get(other):
                    return True
    return False


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
                    "EMERGENCY STOP! Daily loss limit ${:.2f} tercapai. Total floating: ${:.2f}".format(
                        settings["daily_loss_limit"], total_pl))
        except Exception as e:
            logger.error("monitor_daily_loss: %s", e)
        await asyncio.sleep(30)

async def monitor_margin(app):
    global margin_warned
    while True:
        try:
            if not any(pair_active.values()):
                await asyncio.sleep(60)
                continue
            info         = get_account_info()
            margin_level = info["margin_level"]
            if margin_level < settings["margin_stop"] and info["margin"] > 0:
                for pair in ALL_PAIRS:
                    stop_pair(pair)
                await app.bot.send_message(ALLOWED_USER_ID,
                    "MARGIN KRITIS! Level: {:.1f}%. Semua pair dihentikan.".format(margin_level))
                margin_warned = True
            elif margin_level < settings["margin_warning"] and not margin_warned:
                await app.bot.send_message(ALLOWED_USER_ID,
                    "WARNING MARGIN! Level: {:.1f}%. Free margin: ${:.2f}".format(
                        margin_level, info["free_margin"]))
                margin_warned = True
            elif margin_level >= settings["margin_warning"]:
                margin_warned = False
        except Exception as e:
            logger.error("monitor_margin: %s", e)
        await asyncio.sleep(60)

async def monitor_drawdown(app):
    global peak_balance, drawdown_mode
    while True:
        try:
            info    = get_account_info()
            balance = info["balance"]
            if balance > peak_balance:
                peak_balance = balance
                if drawdown_mode:
                    drawdown_mode = False
                    await app.bot.send_message(ALLOWED_USER_ID,
                        "Recovery! Balance ${:.2f} kembali ke peak. Lot normal.".format(balance))
            elif peak_balance > 0:
                drawdown_pct = balance - peak_balance
                if drawdown_pct <= settings["drawdown_threshold"] and not drawdown_mode:
                    drawdown_mode = True
                    await app.bot.send_message(ALLOWED_USER_ID,
                        "DRAWDOWN MODE! Balance ${:.2f} (peak ${:.2f}). Lot dikurangi 50%.".format(
                            balance, peak_balance))
        except Exception as e:
            logger.error("monitor_drawdown: %s", e)
        await asyncio.sleep(300)

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
            trading_ok, trading_reason = is_trading_time()
            news_ok, news_ev           = is_news_time()
            status = "OK" if (trading_ok and not news_ok) else (
                "PAUSE NEWS" if news_ok else trading_reason)
            msg = (
                "AUTO SUMMARY\n"
                "Uptime: {} | Balance: ${:.2f}\n"
                "Floating: ${:.2f} | Margin: {:.1f}%\n"
                "Pair aktif: {}/{} | Status: {}\n"
                "Drawdown mode: {} | Trades: {} W:{} L:{}\n"
                "P/L sesi: ${:.2f}"
            ).format(
                uptime_str(), info["balance"],
                total_pl, info["margin_level"],
                active_pair_count(), settings["max_active_pairs"], status,
                "YA" if drawdown_mode else "TIDAK",
                daily_stats["trades"], daily_stats["wins"], daily_stats["losses"],
                daily_stats["total_pl"]
            )
            await app.bot.send_message(ALLOWED_USER_ID, msg)
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

        # Cek auto-disabled performance
        if pair_performance[pair]["disabled"]:
            stop_pair(pair)
            await app.bot.send_message(ALLOWED_USER_ID,
                "{} auto-disabled: winrate rendah.".format(pair_label(pair)))
            return

        try:
            signal        = get_ema_signal(pair)
            open_trades   = get_open_trades(pair)
            n_buy, n_sell = count_buy_sell(open_trades)
            total_pl      = get_total_pl(open_trades)
            lbl           = pair_label(pair)

            # Update peak profit
            if open_trades and total_pl > state["peak_profit"]:
                state["peak_profit"] = total_pl

            # Determine effective hard SL & TP (ATR-based kalau aktif)
            eff_sl = state["atr_sl"] if (settings["atr_based"] and state["atr_sl"]) else settings["hard_sl"]
            eff_tp = state["atr_tp"] if (settings["atr_based"] and state["atr_tp"]) else settings["total_tp"]

            # ── 1. Hard SL ──
            if open_trades and total_pl <= eff_sl:
                pl_closed = close_all_trades(pair, open_trades)
                log_trade(pair, "all", "hard_sl", pl_closed)
                state.update({"last_signal": None, "waiting_cross": None, "peak_profit": 0.0,
                              "partial_done": False, "breakeven_set": False,
                              "atr_sl": None, "atr_tp": None})
                await app.bot.send_message(ALLOWED_USER_ID,
                    "HARD SL {} | P/L: ${:.2f} | SL: ${:.2f}".format(lbl, pl_closed, eff_sl))
                await asyncio.sleep(settings["check_interval"])
                continue

            # ── 2. Partial Close ──
            if (settings["partial_close"] and open_trades and
                    total_pl >= settings["partial_close_at"] and
                    not state["partial_done"] and len(open_trades) > 1):
                pl_partial, n_closed = close_partial_trades(pair, open_trades, settings["partial_close_pct"])
                log_trade(pair, "partial", "partial_close", pl_partial)
                state["partial_done"] = True
                await app.bot.send_message(ALLOWED_USER_ID,
                    "PARTIAL CLOSE {} | Ditutup: {} posisi | P/L: ${:.2f}".format(
                        lbl, n_closed, pl_partial))
                await asyncio.sleep(2)
                open_trades = get_open_trades(pair)
                total_pl    = get_total_pl(open_trades)

            # ── 3. Break Even ──
            if (open_trades and total_pl >= settings["breakeven_at"] and
                    not state["breakeven_set"] and state["atr_sl"] is not None):
                state["atr_sl"]       = 0.0  # Break even = minimal tidak rugi
                state["breakeven_set"] = True
                await app.bot.send_message(ALLOWED_USER_ID,
                    "BREAK EVEN SET {} | Profit: ${:.2f}".format(lbl, total_pl))

            # ── 4. Trailing TP ──
            if (settings["trailing_tp"] and open_trades and
                    state["peak_profit"] >= eff_tp):
                if state["peak_profit"] - total_pl >= settings["trailing_pullback"]:
                    pl_closed = close_all_trades(pair, open_trades)
                    log_trade(pair, "all", "trailing_tp", pl_closed)
                    state.update({"last_signal": None, "waiting_cross": None, "peak_profit": 0.0,
                                  "partial_done": False, "breakeven_set": False,
                                  "atr_sl": None, "atr_tp": None})
                    await app.bot.send_message(ALLOWED_USER_ID,
                        "TRAILING TP {} | Close: ${:.2f}".format(lbl, pl_closed))
                    await asyncio.sleep(settings["check_interval"])
                    continue

            # ── 5. Normal TP ──
            if open_trades and total_pl >= eff_tp and not settings["trailing_tp"]:
                pl_closed = close_all_trades(pair, open_trades)
                log_trade(pair, "all", "tp", pl_closed)
                state.update({"last_signal": None, "waiting_cross": None, "peak_profit": 0.0,
                              "partial_done": False, "breakeven_set": False,
                              "atr_sl": None, "atr_tp": None})
                await app.bot.send_message(ALLOWED_USER_ID,
                    "TP {} | P/L: ${:.2f}".format(lbl, pl_closed))
                await asyncio.sleep(settings["check_interval"])
                continue

            # ── 6. Crossing berlawanan ──
            if open_trades:
                if n_buy > 0 and signal in ("sell", "sell_active"):
                    pl_closed = close_all_trades(pair, open_trades)
                    log_trade(pair, "buy", "cross_close", pl_closed)
                    state.update({"last_signal": None, "waiting_cross": "sell", "peak_profit": 0.0,
                                  "partial_done": False, "breakeven_set": False,
                                  "atr_sl": None, "atr_tp": None})
                    await app.bot.send_message(ALLOWED_USER_ID,
                        "DEATH CROSS {} | BUY ditutup | P/L: ${:.2f}".format(lbl, pl_closed))
                    await asyncio.sleep(settings["check_interval"])
                    continue
                if n_sell > 0 and signal in ("buy", "buy_active"):
                    pl_closed = close_all_trades(pair, open_trades)
                    log_trade(pair, "sell", "cross_close", pl_closed)
                    state.update({"last_signal": None, "waiting_cross": "buy", "peak_profit": 0.0,
                                  "partial_done": False, "breakeven_set": False,
                                  "atr_sl": None, "atr_tp": None})
                    await app.bot.send_message(ALLOWED_USER_ID,
                        "GOLDEN CROSS {} | SELL ditutup | P/L: ${:.2f}".format(lbl, pl_closed))
                    await asyncio.sleep(settings["check_interval"])
                    continue

            # ── 7. Entry pertama ──
            can_buy  = (signal == "buy"  and state["last_signal"] != "buy"
                        and n_buy  == 0 and state["waiting_cross"] != "sell")
            can_sell = (signal == "sell" and state["last_signal"] != "sell"
                        and n_sell == 0 and state["waiting_cross"] != "buy")

            if can_buy or can_sell:
                # Trading hours
                trading_ok, trading_reason = is_trading_time()
                if not trading_ok:
                    await asyncio.sleep(settings["check_interval"])
                    continue

                # News filter
                news_pause, news_ev = is_news_time()
                if news_pause:
                    await asyncio.sleep(settings["check_interval"])
                    continue

                # Margin check
                try:
                    info = get_account_info()
                    if info["margin_level"] < settings["margin_stop"] and info["margin"] > 0:
                        await asyncio.sleep(settings["check_interval"])
                        continue
                    lot = get_lot_size(info["balance"])
                except Exception:
                    lot = settings["lot_size"]

                # ADX filter
                if settings["adx_filter"]:
                    adx_val = calculate_adx(pair)
                    if adx_val < settings["adx_min"]:
                        await asyncio.sleep(settings["check_interval"])
                        continue
                else:
                    adx_val = 0

                # RSI filter
                rsi_val = get_rsi(pair)
                if settings["rsi_filter"]:
                    if can_buy  and rsi_val < settings["rsi_buy_min"]:
                        await asyncio.sleep(settings["check_interval"])
                        continue
                    if can_sell and rsi_val > settings["rsi_sell_max"]:
                        await asyncio.sleep(settings["check_interval"])
                        continue

                # Multi-timeframe H4 filter
                if settings["mtf_filter"]:
                    h4 = get_h4_signal(pair)
                    if can_buy  and h4 != "buy":
                        await asyncio.sleep(settings["check_interval"])
                        continue
                    if can_sell and h4 != "sell":
                        await asyncio.sleep(settings["check_interval"])
                        continue

                # Correlation filter
                if is_correlated(pair):
                    await asyncio.sleep(settings["check_interval"])
                    continue

                # Hitung ATR-based SL & TP
                if settings["atr_based"]:
                    atr = calculate_atr(pair, settings["atr_period"])
                    if atr > 0:
                        # Convert ATR (price) ke dollar value (approx 0.01 lot)
                        pip_value = lot * 10  # approx untuk major pairs
                        atr_dollar = atr * pip_value * 10000
                        state["atr_sl"] = -(atr_dollar * settings["atr_sl_multiplier"])
                        state["atr_tp"] = atr_dollar * settings["atr_tp_multiplier"]
                    else:
                        state["atr_sl"] = settings["hard_sl"]
                        state["atr_tp"] = settings["total_tp"]
                else:
                    state["atr_sl"] = settings["hard_sl"]
                    state["atr_tp"] = settings["total_tp"]

                direction = "buy" if can_buy else "sell"
                place_order(pair, direction, lot)
                log_trade(pair, direction, "entry_1")
                state.update({
                    "last_signal":   direction,
                    "waiting_cross": None,
                    "peak_profit":   0.0,
                    "partial_done":  False,
                    "breakeven_set": False,
                })

                await app.bot.send_message(ALLOWED_USER_ID,
                    "ENTRY {} {} #1\n"
                    "EMA{}/{} | ADX:{:.1f} | RSI:{:.1f}\n"
                    "Lot:{:.4f} | SL:${:.2f} | TP:${:.2f}".format(
                        direction.upper(), lbl,
                        settings["ema_fast"], settings["ema_slow"],
                        adx_val, rsi_val,
                        lot,
                        state["atr_sl"] or settings["hard_sl"],
                        state["atr_tp"] or settings["total_tp"]
                    ))

            # ── 8. Layering ──
            elif open_trades:
                last_pl      = get_last_trade_pl(open_trades)
                total_layers = n_buy + n_sell

                if last_pl is None or total_layers >= settings["max_layers"]:
                    await asyncio.sleep(settings["check_interval"])
                    continue

                # Fibonacci layers
                should_layer = False
                if settings["use_fib_layers"] and state["entry_price"]:
                    fib = get_fib_levels(pair)
                    closes = get_closes(pair, 5, "D")
                    current_price = closes[-1] if closes else None
                    if current_price and fib:
                        fib_levels = [fib.get("38.2"), fib.get("50"), fib.get("61.8")]
                        for level in fib_levels:
                            if level and abs(current_price - level) / level < 0.001:
                                should_layer = True
                                break
                else:
                    should_layer = last_pl <= settings["layer_trigger"]

                if should_layer:
                    trading_ok, _ = is_trading_time()
                    news_pause, _ = is_news_time()
                    if not trading_ok or news_pause:
                        await asyncio.sleep(settings["check_interval"])
                        continue

                    try:
                        info = get_account_info()
                        lot  = get_lot_size(info["balance"])
                        if info["margin_level"] < settings["margin_stop"] and info["margin"] > 0:
                            await asyncio.sleep(settings["check_interval"])
                            continue
                    except Exception:
                        lot = settings["lot_size"]

                    if n_buy > 0 and signal in ("buy", "buy_active"):
                        place_order(pair, "buy", lot)
                        log_trade(pair, "buy", "layer_{}".format(n_buy+1))
                        await app.bot.send_message(ALLOWED_USER_ID,
                            "LAYER BUY {} #{} | Last:${:.2f} | Total:${:.2f} | {}/{}".format(
                                lbl, n_buy+1, last_pl, total_pl, n_buy+1, settings["max_layers"]))
                    elif n_sell > 0 and signal in ("sell", "sell_active"):
                        place_order(pair, "sell", lot)
                        log_trade(pair, "sell", "layer_{}".format(n_sell+1))
                        await app.bot.send_message(ALLOWED_USER_ID,
                            "LAYER SELL {} #{} | Last:${:.2f} | Total:${:.2f} | {}/{}".format(
                                lbl, n_sell+1, last_pl, total_pl, n_sell+1, settings["max_layers"]))

        except Exception as e:
            err_str = str(e)
            # Pair tidak tersedia — nonaktifkan permanen
            if "Insufficient authorization" in err_str:
                unavailable_pairs.add(pair)
                stop_pair(pair)
                await app.bot.send_message(ALLOWED_USER_ID,
                    "{} dinonaktifkan — pair tidak tersedia di akun ini.".format(pair_label(pair)))
                return
            # Server error sementara (5xx, timeout) — log saja, tidak notif Telegram
            if any(x in err_str for x in ("500 ", "502 ", "503 ", "504 ", "Timeout", "Connection error")):
                logger.warning("[%s] Server error sementara: %s", pair, err_str[:80])
                await asyncio.sleep(settings["check_interval"])
                continue
            # Error lain — notif Telegram
            logger.error("[%s] %s", pair, e)
            try:
                await app.bot.send_message(ALLOWED_USER_ID,
                    "Error {}: {}".format(pair_label(pair), str(e)[:100]))
            except Exception:
                pass

        await asyncio.sleep(settings["check_interval"])
    logger.info("[%s] Loop stop.", pair)


def start_pair(pair, app):
    if pair_active.get(pair):            return False
    if pair in unavailable_pairs:        return "unavailable"
    if pair_performance[pair]["disabled"]: return "disabled"
    if active_pair_count() >= settings["max_active_pairs"]: return "max"
    if is_correlated(pair):              return "correlated"
    pair_active[pair] = True
    pair_state[pair]  = {
        "last_signal": None, "waiting_cross": None, "peak_profit": 0.0,
        "partial_done": False, "breakeven_set": False,
        "entry_price": None, "atr_sl": None, "atr_tp": None,
    }
    pair_tasks[pair] = asyncio.create_task(trading_loop_pair(pair, app))
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
    estop_label = "RESET Emergency Stop" if emergency_stop else "EMERGENCY STOP"
    dm_label    = "Drawdown Mode: ON" if drawdown_mode else "Drawdown Mode: OFF"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Pair ({}/{})".format(active_pair_count(), settings["max_active_pairs"]), callback_data="menu_pairs")],
        [InlineKeyboardButton("Setting",    callback_data="menu_settings"),
         InlineKeyboardButton("Akun",       callback_data="menu_account")],
        [InlineKeyboardButton("Posisi",     callback_data="menu_positions"),
         InlineKeyboardButton("Log",        callback_data="menu_log")],
        [InlineKeyboardButton("News",       callback_data="menu_news"),
         InlineKeyboardButton("Jam",        callback_data="menu_hours")],
        [InlineKeyboardButton("Performance",callback_data="menu_performance")],
        [InlineKeyboardButton("ON Semua",   callback_data="all_on"),
         InlineKeyboardButton("OFF Semua",  callback_data="all_off")],
        [InlineKeyboardButton("Close Semua Posisi", callback_data="confirm_closeall")],
        [InlineKeyboardButton(estop_label,  callback_data="toggle_estop")],
    ])

def kb_pairs(page=0):
    per_page    = 8
    start       = page * per_page
    page_pairs  = ALL_PAIRS[start:start+per_page]
    total_pages = (len(ALL_PAIRS) + per_page - 1) // per_page
    rows = []
    for pair in page_pairs:
        if pair in unavailable_pairs:
            icon = "X"
        elif pair_performance[pair]["disabled"]:
            icon = "LOW"
        elif pair_active.get(pair):
            icon = "ON"
        elif is_correlated(pair):
            icon = "COR"
        else:
            icon = "OFF"
        rows.append([InlineKeyboardButton("[{}] {}".format(icon, pair_label(pair)),
                                          callback_data="toggle_" + pair)])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("< Prev", callback_data="pairs_page_{}".format(page-1)))
    nav.append(InlineKeyboardButton("{}/{}".format(page+1, total_pages), callback_data="noop"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton("Next >", callback_data="pairs_page_{}".format(page+1)))
    rows.append(nav)
    rows.append([InlineKeyboardButton("Menu Utama", callback_data="menu_main")])
    return InlineKeyboardMarkup(rows)

def kb_settings():
    s   = settings
    def tog(key): return "ON" if s[key] else "OFF"
    notif = "{}s".format(s["notify_interval"]) if s["notify_interval"] > 0 else "Off"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("-- ENTRY --", callback_data="noop")],
        [InlineKeyboardButton("Lot: {} | Dynamic: {}".format(s["lot_size"], tog("dynamic_lot")), callback_data="set_lot_size")],
        [InlineKeyboardButton("Toggle Dynamic Lot", callback_data="toggle_dynamic_lot")],
        [InlineKeyboardButton("Per $: {}".format(s["dynamic_lot_per"]), callback_data="set_dynamic_lot_per")],
        [InlineKeyboardButton("EMA Fast: {}".format(s["ema_fast"]), callback_data="set_ema_fast"),
         InlineKeyboardButton("EMA Slow: {}".format(s["ema_slow"]), callback_data="set_ema_slow")],
        [InlineKeyboardButton("-- FILTER --", callback_data="noop")],
        [InlineKeyboardButton("MTF H4: {}".format(tog("mtf_filter")), callback_data="toggle_mtf_filter")],
        [InlineKeyboardButton("RSI Filter: {} Min:{} Max:{}".format(tog("rsi_filter"), s["rsi_buy_min"], s["rsi_sell_max"]), callback_data="set_rsi_buy_min")],
        [InlineKeyboardButton("Toggle RSI", callback_data="toggle_rsi_filter")],
        [InlineKeyboardButton("ADX Filter: {} Min:{}".format(tog("adx_filter"), s["adx_min"]), callback_data="set_adx_min")],
        [InlineKeyboardButton("Toggle ADX", callback_data="toggle_adx_filter")],
        [InlineKeyboardButton("Correlation: {}".format(tog("correlation_filter")), callback_data="toggle_correlation_filter")],
        [InlineKeyboardButton("-- LAYERING --", callback_data="noop")],
        [InlineKeyboardButton("Layer Trigger: ${}".format(s["layer_trigger"]), callback_data="set_layer_trigger")],
        [InlineKeyboardButton("Max Layers: {}".format(s["max_layers"]), callback_data="set_max_layers")],
        [InlineKeyboardButton("Fib Layers: {}".format(tog("use_fib_layers")), callback_data="toggle_use_fib_layers")],
        [InlineKeyboardButton("-- EXIT --", callback_data="noop")],
        [InlineKeyboardButton("TP: ${}".format(s["total_tp"]), callback_data="set_total_tp"),
         InlineKeyboardButton("Hard SL: ${}".format(s["hard_sl"]), callback_data="set_hard_sl")],
        [InlineKeyboardButton("Trailing TP: {}".format(tog("trailing_tp")), callback_data="toggle_trailing_tp")],
        [InlineKeyboardButton("Pullback: ${}".format(s["trailing_pullback"]), callback_data="set_trailing_pullback")],
        [InlineKeyboardButton("Partial Close: {}".format(tog("partial_close")), callback_data="toggle_partial_close")],
        [InlineKeyboardButton("Partial %: {}%".format(s["partial_close_pct"]), callback_data="set_partial_close_pct"),
         InlineKeyboardButton("Partial at: ${}".format(s["partial_close_at"]), callback_data="set_partial_close_at")],
        [InlineKeyboardButton("Break Even at: ${}".format(s["breakeven_at"]), callback_data="set_breakeven_at")],
        [InlineKeyboardButton("-- ATR --", callback_data="noop")],
        [InlineKeyboardButton("ATR Based: {}".format(tog("atr_based")), callback_data="toggle_atr_based")],
        [InlineKeyboardButton("ATR SL x{}".format(s["atr_sl_multiplier"]), callback_data="set_atr_sl_multiplier"),
         InlineKeyboardButton("ATR TP x{}".format(s["atr_tp_multiplier"]), callback_data="set_atr_tp_multiplier")],
        [InlineKeyboardButton("-- JAM --", callback_data="noop")],
        [InlineKeyboardButton("Hours: {} {}-{}WIB".format(tog("trading_hours"), s["hour_start"], s["hour_end"]), callback_data="toggle_trading_hours")],
        [InlineKeyboardButton("Jam Start: {}".format(s["hour_start"]), callback_data="set_hour_start"),
         InlineKeyboardButton("Jam End: {}".format(s["hour_end"]), callback_data="set_hour_end")],
        [InlineKeyboardButton("Skip Jumat: {} h>{}".format(tog("skip_friday"), s["skip_friday_hour"]), callback_data="toggle_skip_friday")],
        [InlineKeyboardButton("Skip Senin: {} h<{}".format(tog("skip_monday"), s["skip_monday_hour"]), callback_data="toggle_skip_monday")],
        [InlineKeyboardButton("-- NEWS --", callback_data="noop")],
        [InlineKeyboardButton("News Filter: {}".format(tog("news_filter")), callback_data="toggle_news_filter")],
        [InlineKeyboardButton("Before: {}m".format(s["news_pause_before"]), callback_data="set_news_pause_before"),
         InlineKeyboardButton("After: {}m".format(s["news_pause_after"]), callback_data="set_news_pause_after")],
        [InlineKeyboardButton("-- MARGIN --", callback_data="noop")],
        [InlineKeyboardButton("Warn: {}%".format(s["margin_warning"]), callback_data="set_margin_warning"),
         InlineKeyboardButton("Stop: {}%".format(s["margin_stop"]), callback_data="set_margin_stop")],
        [InlineKeyboardButton("-- RISK --", callback_data="noop")],
        [InlineKeyboardButton("Max Pair: {}".format(s["max_active_pairs"]), callback_data="set_max_active_pairs"),
         InlineKeyboardButton("Daily Limit: ${}".format(s["daily_loss_limit"]), callback_data="set_daily_loss_limit")],
        [InlineKeyboardButton("Drawdown Recovery: {}".format(tog("drawdown_recovery")), callback_data="toggle_drawdown_recovery")],
        [InlineKeyboardButton("Drawdown Threshold: ${}".format(s["drawdown_threshold"]), callback_data="set_drawdown_threshold")],
        [InlineKeyboardButton("Min Winrate: {}%".format(s["min_winrate"]), callback_data="set_min_winrate")],
        [InlineKeyboardButton("-- BOT --", callback_data="noop")],
        [InlineKeyboardButton("Interval: {}s".format(s["check_interval"]), callback_data="set_check_interval"),
         InlineKeyboardButton("Summary: {}".format(notif), callback_data="set_notify_interval")],
        [InlineKeyboardButton("Menu Utama", callback_data="menu_main")],
    ])

def kb_confirm_closeall():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Ya, Close Semua", callback_data="do_closeall"),
         InlineKeyboardButton("Batal", callback_data="menu_main")],
    ])

def kb_back():
    return InlineKeyboardMarkup([[InlineKeyboardButton("Menu Utama", callback_data="menu_main")]])


# ══════════════════════════════════════════
# TEXT BUILDERS
# ══════════════════════════════════════════

def text_main():
    s  = settings
    ok, reason = is_trading_time()
    nok, nev   = is_news_time()
    status = "OK - Entry diizinkan"
    if emergency_stop: status = "EMERGENCY STOP"
    elif nok:          status = "PAUSE - News: {}".format(nev["title"][:25] if nev else "")
    elif not ok:       status = "PAUSE - {}".format(reason)

    filters_on = []
    if s["mtf_filter"]:         filters_on.append("MTF")
    if s["rsi_filter"]:         filters_on.append("RSI")
    if s["adx_filter"]:         filters_on.append("ADX")
    if s["correlation_filter"]: filters_on.append("COR")
    if s["news_filter"]:        filters_on.append("NEWS")
    if s["trading_hours"]:      filters_on.append("HOURS")

    return (
        "FOREX BOT v4\n\n"
        "Status: {}\n"
        "Waktu: {} WIB\n"
        "Uptime: {}\n"
        "Pair aktif: {}/{}\n"
        "Drawdown mode: {}\n\n"
        "TP: ${} | Hard SL: ${}\n"
        "Layer: ${} max {}\n"
        "ATR: {} | Trailing: {} | Partial: {}\n"
        "Filters: {}\n\n"
        "Pilih menu:"
    ).format(
        status,
        now_wib().strftime("%H:%M %a"),
        uptime_str(),
        active_pair_count(), s["max_active_pairs"],
        "YA" if drawdown_mode else "TIDAK",
        s["total_tp"], s["hard_sl"],
        s["layer_trigger"], s["max_layers"],
        "ON" if s["atr_based"] else "OFF",
        "ON" if s["trailing_tp"] else "OFF",
        "ON" if s["partial_close"] else "OFF",
        " | ".join(filters_on) if filters_on else "NONE",
    )

async def text_positions():
    lines = ["STATUS POSISI\n"]
    grand = 0.0
    any_t = False
    for pair in ALL_PAIRS:
        try:
            ot = get_open_trades(pair)
            if not ot: continue
            any_t    = True
            pl       = get_total_pl(ot)
            grand   += pl
            nb, ns   = count_buy_sell(ot)
            st       = pair_state[pair]
            icon     = "ON" if pair_active.get(pair) else "OFF"
            peak_s   = " peak:${:.2f}".format(st["peak_profit"]) if st["peak_profit"] > 0 else ""
            be_s     = " [BE]" if st["breakeven_set"] else ""
            partial_s= " [P]"  if st["partial_done"]  else ""
            lines.append("[{}] {} B:{} S:{} P/L:${:.2f}{}{}{}".format(
                icon, pair_label(pair), nb, ns, pl, peak_s, be_s, partial_s))
        except Exception:
            pass
    if not any_t:
        lines.append("Tidak ada posisi terbuka")
    else:
        lines.append("\nGrand Total: ${:.2f}".format(grand))
    return "\n".join(lines)

async def text_account():
    try:
        info = get_account_info()
        ml   = info["margin_level"]
        ml_s = "KRITIS" if ml < settings["margin_stop"] else ("WARNING" if ml < settings["margin_warning"] else "OK")
        lot  = get_lot_size(info["balance"])
        wr   = "{:.0f}%".format(daily_stats["wins"] / daily_stats["trades"] * 100) if daily_stats["trades"] > 0 else "N/A"
        return (
            "STATUS AKUN\n\n"
            "Balance: ${:.2f}\n"
            "NAV: ${:.2f}\n"
            "Floating P/L: ${:.2f}\n"
            "Margin Used: ${:.2f}\n"
            "Free Margin: ${:.2f}\n"
            "Margin Level: {:.1f}% [{}]\n"
            "Lot aktif: {:.4f}{}\n"
            "Peak Balance: ${:.2f}\n\n"
            "Sesi ini:\n"
            "Trade:{} Win:{} Loss:{} WR:{}\n"
            "P/L: ${:.2f}"
        ).format(
            info["balance"], info["nav"], info["pl"],
            info["margin"], info["free_margin"],
            ml, ml_s,
            lot, " [DRAWDOWN -50%]" if drawdown_mode else "",
            peak_balance,
            daily_stats["trades"], daily_stats["wins"],
            daily_stats["losses"], wr,
            daily_stats["total_pl"]
        )
    except Exception as e:
        return "Error: {}".format(str(e))

def text_performance():
    lines = ["PERFORMANCE PER PAIR\n"]
    for pair in ALL_PAIRS:
        p  = pair_performance[pair]
        if p["trades"] == 0:
            continue
        wr = p["wins"] / p["trades"] * 100
        status = ""
        if pair in unavailable_pairs:      status = "[UNAVAILABLE]"
        elif p["disabled"]:                status = "[AUTO-DISABLED]"
        elif pair_active.get(pair):        status = "[ON]"
        else:                              status = "[OFF]"
        lines.append("{} {} | T:{} W:{} L:{} WR:{:.0f}% PL:${:.2f}".format(
            status, pair_label(pair),
            p["trades"], p["wins"], p["losses"], wr, p["total_pl"]))
    if len(lines) == 1:
        lines.append("Belum ada data trade")
    return "\n".join(lines)

def text_news():
    upcoming = get_upcoming_news(8)
    if not upcoming:
        return "NEWS\n\nTidak ada high-impact news atau gagal fetch."
    lines = ["HIGH-IMPACT NEWS MINGGU INI\n"]
    nok, _ = is_news_time()
    if nok: lines.append("BOT SEDANG PAUSE KARENA NEWS\n")
    now = datetime.now()
    for ev in upcoming:
        diff = int((ev["time"] - now).total_seconds() / 60)
        when = "{}m lagi".format(diff) if diff < 60 else ev["time"].strftime("%a %H:%M WIB")
        lines.append("[{}] {} - {}".format(ev["country"], ev["title"], when))
    return "\n".join(lines)

def text_hours():
    s  = settings
    ok, reason = is_trading_time()
    return (
        "TRADING HOURS\n\n"
        "Sekarang: {} WIB\n"
        "Status: {}\n\n"
        "Hours Filter: {}\n"
        "Aktif: {}:00 - {}:00 WIB\n"
        "Skip Jumat > {}:00: {}\n"
        "Skip Senin  < {}:00: {}\n\n"
        "Sesi London+NY: 15:00-23:00 WIB\n"
        "Sesi Tokyo: 07:00-15:00 WIB"
    ).format(
        now_wib().strftime("%H:%M %a"), reason,
        "ON" if s["trading_hours"] else "OFF",
        s["hour_start"], s["hour_end"],
        s["skip_friday_hour"], "ON" if s["skip_friday"] else "OFF",
        s["skip_monday_hour"], "ON" if s["skip_monday"] else "OFF",
    )

def text_log():
    if not trade_log:
        return "LOG TRADE\n\nBelum ada trade."
    lines = ["LOG TRADE (30 terakhir)\n"]
    for t in reversed(trade_log[-30:]):
        pl_s = " ${:.2f}".format(t["pl"]) if t["pl"] is not None else ""
        lines.append("[{}] {} {} {}{}".format(
            t["time"], t["pair"], t["direction"], t["action"], pl_s))
    return "\n".join(lines)


# ══════════════════════════════════════════
# SETTING CONFIG
# ══════════════════════════════════════════

SETTING_LABELS = {
    "lot_size":           "Lot Size base (skrg: {}). Contoh: 0.01",
    "dynamic_lot_per":    "Dynamic lot per $X (skrg: {}). Contoh: 1000",
    "ema_fast":           "EMA Fast period (skrg: {}). Contoh: 20",
    "ema_slow":           "EMA Slow period (skrg: {}). Contoh: 50",
    "rsi_buy_min":        "RSI min untuk BUY (skrg: {}). Contoh: 50",
    "rsi_sell_max":       "RSI max untuk SELL (skrg: {}). Contoh: 50",
    "rsi_period":         "RSI period (skrg: {}). Contoh: 14",
    "adx_min":            "ADX minimum (skrg: {}). Contoh: 20",
    "layer_trigger":      "Layer trigger $ (skrg: {}). Harus negatif. Contoh: -10",
    "max_layers":         "Max layers (skrg: {}). Contoh: 5",
    "total_tp":           "Take Profit $ (skrg: {}). Contoh: 25",
    "hard_sl":            "Hard SL $ (skrg: {}). Harus negatif. Contoh: -30",
    "trailing_pullback":  "Trailing pullback $ (skrg: {}). Contoh: 3",
    "partial_close_pct":  "Partial close % (skrg: {}). Contoh: 50",
    "partial_close_at":   "Partial close at profit $ (skrg: {}). Contoh: 15",
    "breakeven_at":       "Break even at profit $ (skrg: {}). Contoh: 5",
    "atr_period":         "ATR period (skrg: {}). Contoh: 14",
    "atr_sl_multiplier":  "ATR SL multiplier (skrg: {}). Contoh: 2",
    "atr_tp_multiplier":  "ATR TP multiplier (skrg: {}). Contoh: 4",
    "hour_start":         "Jam mulai WIB (skrg: {}). Contoh: 15",
    "hour_end":           "Jam selesai WIB (skrg: {}). Contoh: 23",
    "skip_friday_hour":   "Skip Jumat setelah jam (skrg: {}). Contoh: 21",
    "skip_monday_hour":   "Skip Senin sebelum jam (skrg: {}). Contoh: 10",
    "news_pause_before":  "Pause sebelum news menit (skrg: {}). Contoh: 30",
    "news_pause_after":   "Pause setelah news menit (skrg: {}). Contoh: 30",
    "margin_warning":     "Margin warning % (skrg: {}). Contoh: 200",
    "margin_stop":        "Margin stop % (skrg: {}). Contoh: 150",
    "max_active_pairs":   "Max pair aktif (skrg: {}). Contoh: 5",
    "daily_loss_limit":   "Daily loss limit $ (skrg: {}). Harus negatif. Contoh: -100",
    "drawdown_threshold": "Drawdown threshold $ (skrg: {}). Harus negatif. Contoh: -20",
    "min_winrate":        "Min winrate % (skrg: {}). Contoh: 40",
    "check_interval":     "Check interval detik (skrg: {}). Min 10",
    "notify_interval":    "Summary interval detik (skrg: {}). 0=off",
}

INT_SETTINGS = {
    "ema_fast", "ema_slow", "check_interval", "max_layers", "max_active_pairs",
    "notify_interval", "adx_min", "hour_start", "hour_end", "skip_friday_hour",
    "skip_monday_hour", "news_pause_before", "news_pause_after",
    "margin_warning", "margin_stop", "rsi_period", "partial_close_pct", "atr_period",
}

TOGGLE_SETTINGS = {
    "toggle_adx_filter":          "adx_filter",
    "toggle_trailing_tp":         "trailing_tp",
    "toggle_trading_hours":       "trading_hours",
    "toggle_news_filter":         "news_filter",
    "toggle_dynamic_lot":         "dynamic_lot",
    "toggle_skip_friday":         "skip_friday",
    "toggle_skip_monday":         "skip_monday",
    "toggle_mtf_filter":          "mtf_filter",
    "toggle_rsi_filter":          "rsi_filter",
    "toggle_correlation_filter":  "correlation_filter",
    "toggle_partial_close":       "partial_close",
    "toggle_atr_based":           "atr_based",
    "toggle_use_fib_layers":      "use_fib_layers",
    "toggle_drawdown_recovery":   "drawdown_recovery",
}


# ══════════════════════════════════════════
# HANDLERS
# ══════════════════════════════════════════

async def start_command(update, context):
    global bot_start_time, peak_balance
    if not is_allowed(update): return
    if not bot_start_time:
        bot_start_time = datetime.now()
        try:
            info = get_account_info()
            peak_balance = info["balance"]
        except Exception:
            pass
    await update.message.reply_text(text_main(), reply_markup=kb_main())

async def testentry_command(update, context):
    if not is_allowed(update): return
    await update.message.reply_text("Testing entry EUR/USD...")
    results = []
    for direction in ["buy", "sell"]:
        try:
            units = "1000" if direction == "buy" else "-1000"
            r = orders.OrderCreate(ACCOUNT_ID, data={
                "order": {"type": "MARKET", "instrument": "EUR_USD", "units": units}
            })
            client.request(r)
            trade_id = r.response["orderFillTransaction"]["tradeOpened"]["tradeID"]
            results.append("OK {} trade_id={}".format(direction.upper(), trade_id))
            await asyncio.sleep(1)
            client.request(trades.TradeClose(ACCOUNT_ID, tradeID=trade_id))
            results.append("OK {} closed".format(direction.upper()))
        except Exception as e:
            results.append("FAIL {}: {}".format(direction.upper(), str(e)))
    await update.message.reply_text("Test Result:\n" + "\n".join(results))

async def button_handler(update, context):
    global emergency_stop
    query = update.callback_query
    await query.answer()
    if query.from_user.id != ALLOWED_USER_ID: return
    data = query.data

    async def edit_main():
        try:
            await query.edit_message_text(text_main(), reply_markup=kb_main())
        except Exception:
            pass

    async def edit_settings():
        try:
            await query.edit_message_text("SETTING BOT v4\n\nKetuk untuk ubah:",
                                          reply_markup=kb_settings())
        except Exception:
            pass

    if data == "menu_main":       await edit_main()
    elif data == "menu_settings": await edit_settings()
    elif data == "noop":          pass

    elif data == "menu_pairs":
        cnt, mx = active_pair_count(), settings["max_active_pairs"]
        try:
            await query.edit_message_text(
                "KELOLA PAIR\nAktif: {}/{} (maks {})\n[ON]=aktif [OFF]=nonaktif [COR]=korelasi [X]=unavail [LOW]=winrate rendah".format(
                    cnt, len(ALL_PAIRS), mx),
                reply_markup=kb_pairs(0))
        except Exception: pass

    elif data.startswith("pairs_page_"):
        page = int(data[len("pairs_page_"):])
        cnt, mx = active_pair_count(), settings["max_active_pairs"]
        try:
            await query.edit_message_text(
                "KELOLA PAIR\nAktif: {}/{} (maks {})".format(cnt, len(ALL_PAIRS), mx),
                reply_markup=kb_pairs(page))
        except Exception: pass

    elif data.startswith("toggle_") and data not in TOGGLE_SETTINGS and data != "toggle_estop":
        pair = data[len("toggle_"):]
        if pair not in ALL_PAIRS: return
        if pair_active.get(pair):
            stop_pair(pair)
            await query.answer("{} OFF".format(pair_label(pair)))
        else:
            if emergency_stop:
                await query.answer("Emergency Stop aktif!", show_alert=True); return
            result = start_pair(pair, context.application)
            msgs = {
                "max":         "Maks {} pair aktif!".format(settings["max_active_pairs"]),
                "unavailable": "{} tidak tersedia".format(pair_label(pair)),
                "disabled":    "{} disabled (winrate rendah)".format(pair_label(pair)),
                "correlated":  "{} berkorelasi dengan pair aktif".format(pair_label(pair)),
            }
            if result in msgs:
                await query.answer(msgs[result], show_alert=True); return
            await query.answer("{} ON".format(pair_label(pair)))
        cnt, mx = active_pair_count(), settings["max_active_pairs"]
        try:
            await query.edit_message_text(
                "KELOLA PAIR\nAktif: {}/{} (maks {})".format(cnt, len(ALL_PAIRS), mx),
                reply_markup=kb_pairs(0))
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
            await query.answer("EMERGENCY STOP aktif!", show_alert=True)
        await edit_main()

    elif data == "all_on":
        if emergency_stop:
            await query.answer("Emergency Stop aktif!", show_alert=True); return
        count = 0
        for pair in ALL_PAIRS:
            if active_pair_count() >= settings["max_active_pairs"]: break
            if start_pair(pair, context.application) is True: count += 1
        await query.answer("{} pair ON!".format(count), show_alert=True)
        await edit_main()

    elif data == "all_off":
        for pair in ALL_PAIRS: stop_pair(pair)
        await query.answer("Semua pair OFF!", show_alert=True)
        await edit_main()

    elif data == "menu_account":
        txt = await text_account()
        try:
            await query.edit_message_text(txt, reply_markup=kb_back())
        except Exception: pass

    elif data == "menu_positions":
        txt = await text_positions()
        try:
            await query.edit_message_text(txt, reply_markup=kb_back())
        except Exception: pass

    elif data == "menu_log":
        try:
            await query.edit_message_text(text_log(), reply_markup=kb_back())
        except Exception: pass

    elif data == "menu_news":
        try:
            await query.edit_message_text(text_news(), reply_markup=kb_back())
        except Exception: pass

    elif data == "menu_hours":
        try:
            await query.edit_message_text(text_hours(), reply_markup=kb_back())
        except Exception: pass

    elif data == "menu_performance":
        try:
            await query.edit_message_text(text_performance(), reply_markup=kb_back())
        except Exception: pass

    elif data == "confirm_closeall":
        try:
            await query.edit_message_text("Yakin close SEMUA posisi?",
                                          reply_markup=kb_confirm_closeall())
        except Exception: pass

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
        try:
            await query.edit_message_text(
                "Selesai! Ditutup: {} posisi\nTotal P/L: ${:.2f}".format(closed, total),
                reply_markup=kb_back())
        except Exception: pass

    elif data.startswith("set_"):
        key = data[len("set_"):]
        pending_setting_key[query.from_user.id] = key
        label = SETTING_LABELS.get(key, key).format(settings.get(key, "?"))
        try:
            await query.edit_message_text(
                "Ubah {}\n\n{}\n\nKirim nilai baru:".format(key, label),
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("Batal", callback_data="menu_settings")
                ]]))
        except Exception: pass


async def receive_setting_value(update, context):
    if not is_allowed(update): return
    user_id = update.effective_user.id
    key     = pending_setting_key.get(user_id)
    if not key: return
    text = update.message.text.strip()
    try:
        value = float(text)
        validators = {
            "lot_size":           value > 0,
            "layer_trigger":      value < 0,
            "max_layers":         value >= 1,
            "total_tp":           value > 0,
            "hard_sl":            value < 0,
            "trailing_pullback":  value > 0,
            "adx_min":            1 <= value <= 100,
            "rsi_buy_min":        0 <= value <= 100,
            "rsi_sell_max":       0 <= value <= 100,
            "max_active_pairs":   1 <= value <= len(ALL_PAIRS),
            "daily_loss_limit":   value < 0,
            "check_interval":     value >= 10,
            "notify_interval":    value >= 0,
            "ema_fast":           value >= 2,
            "ema_slow":           value >= 2,
            "hour_start":         0 <= value <= 23,
            "hour_end":           0 <= value <= 23,
            "margin_warning":     value > 0,
            "margin_stop":        value > 0,
            "partial_close_pct":  0 < value <= 100,
            "partial_close_at":   value > 0,
            "breakeven_at":       value > 0,
            "atr_sl_multiplier":  value > 0,
            "atr_tp_multiplier":  value > 0,
            "drawdown_threshold": value < 0,
            "min_winrate":        0 <= value <= 100,
        }
        if key in validators and not validators[key]:
            raise ValueError("Nilai tidak valid untuk {}".format(key))
        if key == "ema_fast" and value >= settings["ema_slow"]:
            raise ValueError("EMA Fast harus < EMA Slow")
        if key == "ema_slow" and value <= settings["ema_fast"]:
            raise ValueError("EMA Slow harus > EMA Fast")
        if key == "margin_stop" and value >= settings["margin_warning"]:
            raise ValueError("Margin Stop harus < Margin Warning")

        settings[key] = int(value) if key in INT_SETTINGS else value
        del pending_setting_key[user_id]
        await update.message.reply_text(
            "{} -> {}".format(key, settings[key]))
        await update.message.reply_text(
            "SETTING BOT v4\n\nKetuk untuk ubah:", reply_markup=kb_settings())
    except ValueError as e:
        await update.message.reply_text("Error: {}\nCoba lagi:".format(str(e)))


# ══════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════

async def post_init(app):
    global bot_start_time, peak_balance
    bot_start_time = datetime.now()
    try:
        info = get_account_info()
        peak_balance = info["balance"]
        logger.info("Balance awal: $%.2f", peak_balance)
    except Exception as e:
        logger.error("post_init account: %s", e)

    # Simpan referensi task supaya tidak di-garbage collect
    app.bot_data["bg_tasks"] = [
        asyncio.create_task(monitor_daily_loss(app), name="monitor_daily_loss"),
        asyncio.create_task(monitor_margin(app),     name="monitor_margin"),
        asyncio.create_task(monitor_drawdown(app),   name="monitor_drawdown"),
        asyncio.create_task(auto_summary(app),       name="auto_summary"),
    ]
    # Pre-fetch news di background thread
    loop = asyncio.get_event_loop()
    loop.run_in_executor(None, fetch_news)
    logger.info("Bot v4 siap. Background tasks: %d", len(app.bot_data["bg_tasks"]))

def main():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start",      start_command))
    app.add_handler(CommandHandler("menu",       start_command))
    app.add_handler(CommandHandler("testentry",  testentry_command))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, receive_setting_value))
    app.run_polling()

if __name__ == "__main__":
    main()
