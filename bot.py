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
    {"USD_JPY", "EUR_JPY", "AUD_JPY", "CAD_JPY"},  # JPY pairs
    {"AUD_USD", "NZD_USD", "AUD_JPY", "EUR_AUD"},  # AUD/commodity
    {"USD_CAD", "EUR_CAD", "GBP_CAD"},   # CAD pairs
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
    "mtf_enabled":          True,    # Konfirmasi H4
    # RSI filter
    "rsi_filter":           True,
    "rsi_period":           14,
    "rsi_buy_min":          50,      # RSI harus > ini untuk buy
    "rsi_sell_max":         50,      # RSI harus < ini untuk sell
    # ADX filter
    "adx_filter":           True,
    "adx_min":              20,
    # Layering
    "layer_trigger":        -10.0,
    "max_layers":           5,
    "fib_layers":           True,    # Pakai Fibonacci untuk layer entry
    # Exit
    "total_tp":             25.0,
    "hard_sl":              -30.0,
    "trailing_tp":          True,
    "trailing_pullback":    3.0,
    "partial_close":        True,    # Partial close saat profit >= partial_tp
    "partial_tp":           15.0,    # Close 50% posisi saat profit >= ini
    "partial_pct":          50,      # Persentase posisi yang di-close
    "breakeven":            True,    # Pindah SL ke breakeven
    "breakeven_trigger":    5.0,     # Trigger breakeven saat posisi pertama profit >= ini
    # ATR
    "atr_sl_tp":            True,    # Gunakan ATR untuk SL/TP dinamis
    "atr_period":           14,
    "atr_sl_mult":          1.5,     # SL = ATR * multiplier
    "atr_tp_mult":          3.0,     # TP = ATR * multiplier
    # Correlation
    "correlation_filter":   True,
    "max_corr_pairs":       1,       # Maks 1 pair aktif per grup korelasi
    # Performance
    "perf_tracking":        True,
    "perf_min_trades":      10,      # Min trade sebelum evaluasi
    "perf_min_winrate":     40,      # Nonaktifkan pair jika winrate < ini %
    "drawdown_recovery":    True,    # Kurangi lot saat drawdown
    "drawdown_threshold":   -20.0,   # Trigger recovery mode
    # Trading Hours
    "trading_hours":        True,
    "hour_start":           15,
    "hour_end":             23,
    "skip_friday":          True,
    "skip_friday_hour":     21,
    "skip_monday":          True,
    "skip_monday_hour":     10,
    # News
    "news_filter":          True,
    "news_pause_before":    30,
    "news_pause_after":     30,
    # Margin
    "margin_warning":       200.0,
    "margin_stop":          150.0,
    # Risk global
    "max_active_pairs":     5,
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
        "breakeven_done":   False,   # Sudah set breakeven?
        "entry_price":      None,    # Harga entry pertama
        "atr_sl":           None,    # Dynamic SL dari ATR
        "atr_tp":           None,    # Dynamic TP dari ATR
    } for p in ALL_PAIRS
}

# Performance tracking per pair
pair_perf = {
    p: {"trades": 0, "wins": 0, "losses": 0, "total_pl": 0.0,
        "disabled_by_perf": False, "peak_balance": 0.0}
    for p in ALL_PAIRS
}

unavailable_pairs   = set()
pending_setting_key = {}
bot_start_time      = None
trade_log           = []
daily_stats         = {"trades": 0, "wins": 0, "losses": 0, "total_pl": 0.0}
emergency_stop      = False
recovery_mode       = False   # Drawdown recovery mode
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

def pair_label(pair):   return pair.replace("_", "/")
def em(text):
    text = str(text)
    for ch in r"_*[]()~`>#+-=|{}.!":
        text = text.replace(ch, "\\" + ch)
    return text
def is_allowed(update): return update.effective_user.id == ALLOWED_USER_ID
def active_pair_count(): return sum(1 for p in ALL_PAIRS if pair_active.get(p))
def now_wib():          return datetime.now(WIB)
def uptime_str():
    if not bot_start_time: return "N/A"
    delta = datetime.now() - bot_start_time
    h, rem = divmod(int(delta.total_seconds()), 3600)
    m, s   = divmod(rem, 60)
    return "{}j {}m {}d".format(h, m, s)

def log_trade(pair, direction, action, pl=None):
    entry = {"time": datetime.now().strftime("%H:%M:%S"), "pair": pair_label(pair),
             "direction": direction, "action": action, "pl": pl}
    trade_log.append(entry)
    if len(trade_log) > 200: trade_log.pop(0)
    if pl is not None:
        daily_stats["trades"] += 1
        daily_stats["total_pl"] += pl
        if pl >= 0: daily_stats["wins"] += 1
        else:       daily_stats["losses"] += 1
        # Update pair performance
        pair_perf[pair]["trades"] += 1
        pair_perf[pair]["total_pl"] += pl
        if pl >= 0: pair_perf[pair]["wins"] += 1
        else:       pair_perf[pair]["losses"] += 1


# ══════════════════════════════════════════
# MARKET DATA
# ══════════════════════════════════════════

def get_candles_raw(pair, count=60, granularity="D"):
    base = "https://api-fxpractice.oanda.com" if OANDA_ENV == "practice" else "https://api-fxtrade.oanda.com"
    url  = "{}/v3/instruments/{}/candles".format(base, pair)
    resp = req_lib.get(url,
        headers={"Authorization": "Bearer " + OANDA_TOKEN, "Content-Type": "application/json"},
        params={"count": str(count), "granularity": granularity, "price": "M"},
        timeout=10)
    if resp.status_code != 200:
        raise Exception("{} {}".format(resp.status_code, resp.text))
    data = resp.json()
    if "candles" not in data:
        raise Exception("No candles: {}".format(data))
    return data["candles"]

def get_closes(pair, count=60, granularity="D"):
    candles = get_candles_raw(pair, count, granularity)
    return [float(c["mid"]["c"]) for c in candles if c["complete"]]

def get_ohlc(pair, count=60, granularity="D"):
    candles = get_candles_raw(pair, count, granularity)
    completed = [c for c in candles if c["complete"]]
    return {
        "opens":  [float(c["mid"]["o"]) for c in completed],
        "highs":  [float(c["mid"]["h"]) for c in completed],
        "lows":   [float(c["mid"]["l"]) for c in completed],
        "closes": [float(c["mid"]["c"]) for c in completed],
    }

def calculate_ema(data, period):
    return pd.Series(data).ewm(span=period, adjust=False).mean().tolist()

def calculate_rsi(closes, period=14):
    s      = pd.Series(closes)
    delta  = s.diff()
    gain   = delta.clip(lower=0).ewm(com=period-1, adjust=False).mean()
    loss   = (-delta.clip(upper=0)).ewm(com=period-1, adjust=False).mean()
    rs     = gain / loss
    rsi    = 100 - (100 / (1 + rs))
    return rsi.tolist()

def calculate_adx(ohlc, period=14):
    try:
        highs, lows, closes = ohlc["highs"], ohlc["lows"], ohlc["closes"]
        if len(closes) < period + 1: return 0
        tr_list, pdm_list, ndm_list = [], [], []
        for i in range(1, len(closes)):
            h, l, pc = highs[i], lows[i], closes[i-1]
            tr  = max(h - l, abs(h - pc), abs(l - pc))
            pdm = max(h - highs[i-1], 0) if (h - highs[i-1]) > (lows[i-1] - l) else 0
            ndm = max(lows[i-1] - l, 0)  if (lows[i-1] - l) > (h - highs[i-1]) else 0
            tr_list.append(tr); pdm_list.append(pdm); ndm_list.append(ndm)
        def smooth(data, p):
            r = [sum(data[:p])]
            for i in range(p, len(data)): r.append(r[-1] - r[-1]/p + data[i])
            return r
        atr  = smooth(tr_list, period)
        pDIs = smooth(pdm_list, period)
        nDIs = smooth(ndm_list, period)
        dx_list = []
        for i in range(len(atr)):
            if atr[i] == 0: continue
            pdi = 100 * pDIs[i] / atr[i]
            ndi = 100 * nDIs[i] / atr[i]
            dx  = 100 * abs(pdi - ndi) / (pdi + ndi) if (pdi + ndi) > 0 else 0
            dx_list.append(dx)
        if not dx_list: return 0
        return round(sum(dx_list[-period:]) / period, 2)
    except Exception as e:
        logger.error("ADX error: %s", e)
        return 0

def calculate_atr(ohlc, period=14):
    highs, lows, closes = ohlc["highs"], ohlc["lows"], ohlc["closes"]
    trs = []
    for i in range(1, len(closes)):
        tr = max(highs[i] - lows[i], abs(highs[i] - closes[i-1]), abs(lows[i] - closes[i-1]))
        trs.append(tr)
    if not trs: return 0
    atr = pd.Series(trs).ewm(span=period, adjust=False).mean().iloc[-1]
    return round(atr, 5)

def get_swing_fib_levels(pair):
    """Hitung level Fibonacci dari swing high/low 20 candle terakhir."""
    try:
        ohlc       = get_ohlc(pair, count=30, granularity="D")
        highs      = ohlc["highs"]
        lows       = ohlc["lows"]
        swing_high = max(highs[-20:])
        swing_low  = min(lows[-20:])
        diff       = swing_high - swing_low
        return {
            "high":   swing_high,
            "low":    swing_low,
            "fib382": swing_high - diff * 0.382,
            "fib500": swing_high - diff * 0.500,
            "fib618": swing_high - diff * 0.618,
        }
    except Exception:
        return None

def get_ema_signal_mtf(pair):
    """
    Multi-timeframe signal:
    Daily + H4 harus sama arah.
    Return: signal string atau None
    """
    try:
        # Daily signal
        closes_d = get_closes(pair, 60, "D")
        if len(closes_d) < 52: return None
        ema20_d = calculate_ema(closes_d, settings["ema_fast"])
        ema50_d = calculate_ema(closes_d, settings["ema_slow"])
        c20_d, c50_d = ema20_d[-1], ema50_d[-1]
        p20_d, p50_d = ema20_d[-2], ema50_d[-2]

        if p20_d <= p50_d and c20_d > c50_d:   daily_sig = "buy"
        elif p20_d >= p50_d and c20_d < c50_d:  daily_sig = "sell"
        elif c20_d > c50_d:                      daily_sig = "buy_active"
        elif c20_d < c50_d:                      daily_sig = "sell_active"
        else:                                     return None

        if not settings["mtf_enabled"]:
            return daily_sig

        # H4 confirmation
        closes_h4 = get_closes(pair, 60, "H4")
        if len(closes_h4) < 22: return daily_sig  # Kalau data H4 kurang, pakai daily saja
        ema20_h4 = calculate_ema(closes_h4, settings["ema_fast"])
        ema50_h4 = calculate_ema(closes_h4, settings["ema_slow"])
        h4_bull  = ema20_h4[-1] > ema50_h4[-1]
        h4_bear  = ema20_h4[-1] < ema50_h4[-1]

        # Daily dan H4 harus searah
        if daily_sig in ("buy", "buy_active") and h4_bull:   return daily_sig
        if daily_sig in ("sell", "sell_active") and h4_bear:  return daily_sig

        return None  # Tidak konfirmasi — skip entry
    except Exception as e:
        logger.error("[%s] MTF signal error: %s", pair, e)
        return None

def get_rsi_value(pair):
    try:
        closes = get_closes(pair, 50, "D")
        if len(closes) < settings["rsi_period"] + 1: return 50
        rsi = calculate_rsi(closes, settings["rsi_period"])
        return round(rsi[-1], 1)
    except Exception:
        return 50


# ══════════════════════════════════════════
# CORRELATION CHECK
# ══════════════════════════════════════════

def is_correlation_blocked(pair):
    """Cek apakah pair berkorelasi dengan pair yang sudah aktif."""
    if not settings["correlation_filter"]: return False
    for group in CORRELATION_GROUPS:
        if pair not in group: continue
        active_in_group = [p for p in group if p != pair and pair_active.get(p)]
        if len(active_in_group) >= settings["max_corr_pairs"]:
            return True, active_in_group[0]
    return False, None


# ══════════════════════════════════════════
# PERFORMANCE CHECK
# ══════════════════════════════════════════

def check_pair_performance(pair):
    """Cek apakah pair layak trading berdasarkan historis."""
    if not settings["perf_tracking"]: return True
    perf = pair_perf[pair]
    if perf["trades"] < settings["perf_min_trades"]: return True  # Belum cukup data
    winrate = perf["wins"] / perf["trades"] * 100
    if winrate < settings["perf_min_winrate"]:
        perf["disabled_by_perf"] = True
        return False
    perf["disabled_by_perf"] = False
    return True


# ══════════════════════════════════════════
# TRADING HOURS & NEWS
# ══════════════════════════════════════════

def is_trading_time():
    if not settings["trading_hours"]: return True, "OK"
    now     = now_wib()
    hour    = now.hour
    weekday = now.weekday()
    if weekday in (5, 6): return False, "Weekend"
    if weekday == 4 and settings["skip_friday"] and hour >= settings["skip_friday_hour"]:
        return False, "Jumat malam"
    if weekday == 0 and settings["skip_monday"] and hour < settings["skip_monday_hour"]:
        return False, "Senin pagi"
    if not (settings["hour_start"] <= hour < settings["hour_end"]):
        return False, "Di luar jam {}:00-{}:00 WIB".format(settings["hour_start"], settings["hour_end"])
    return True, "OK"

def fetch_news():
    global news_cache, news_cache_time
    try:
        if news_cache_time and (datetime.now() - news_cache_time).seconds < 3600:
            return news_cache
        resp = req_lib.get("https://nfs.faireconomy.media/ff_calendar_thisweek.xml", timeout=10)
        root = ET.fromstring(resp.content)
        events = []
        for item in root.findall(".//event"):
            try:
                if item.findtext("impact", "").lower() != "high": continue
                title  = item.findtext("title", "")
                date_s = item.findtext("date", "")
                time_s = item.findtext("time", "")
                if not date_s or not time_s: continue
                dt = datetime.strptime("{} {}".format(date_s, time_s), "%m-%d-%Y %I:%M%p")
                dt_wib = dt.replace(tzinfo=pytz.timezone("US/Eastern")).astimezone(WIB).replace(tzinfo=None)
                events.append({"title": title, "time": dt_wib})
            except Exception: continue
        news_cache      = events
        news_cache_time = datetime.now()
        return events
    except Exception as e:
        logger.error("fetch_news: %s", e)
        return news_cache

def is_news_time():
    if not settings["news_filter"]: return False, None
    try:
        events = fetch_news()
        now    = datetime.now()
        for ev in events:
            diff_min = (ev["time"] - now).total_seconds() / 60
            if -settings["news_pause_after"] <= diff_min <= settings["news_pause_before"]:
                return True, ev
        return False, None
    except Exception:
        return False, None

def get_upcoming_news(max_events=5):
    try:
        events   = fetch_news()
        now      = datetime.now()
        upcoming = sorted([e for e in events if (e["time"] - now).total_seconds() > 0], key=lambda x: x["time"])
        return upcoming[:max_events]
    except Exception:
        return []


# ══════════════════════════════════════════
# OANDA ORDERS
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
    return {
        "balance":      float(a["balance"]),
        "nav":          nav,
        "pl":           float(a["unrealizedPL"]),
        "margin":       margin,
        "free_margin":  float(a["marginAvailable"]),
        "margin_level": round(nav / margin * 100, 1) if margin > 0 else 9999,
    }

def get_lot_size(balance=None):
    if not settings["dynamic_lot"]: lot = settings["lot_size"]
    else:
        if balance is None:
            try:    balance = get_account_info()["balance"]
            except: return settings["lot_size"]
        lot = round((balance / settings["dynamic_lot_per"]) * 0.01, 4)
    # Kurangi lot 50% saat recovery mode
    if recovery_mode and settings["drawdown_recovery"]:
        lot = round(lot * 0.5, 4)
    return max(0.01, lot)

def place_order(pair, direction, lot=None):
    if lot is None: lot = get_lot_size()
    units = str(int(lot * 100000))
    if direction == "sell": units = "-" + units
    r = orders.OrderCreate(ACCOUNT_ID, data={"order": {"type": "MARKET", "instrument": pair, "units": units}})
    client.request(r)
    logger.info("[%s] %s lot=%.4f", pair, direction.upper(), lot)
    return lot

def close_trade_partial(pair, open_trades, pct):
    """Close sebagian posisi (pct = persentase 0-100)."""
    closed_pl = 0.0
    n_to_close = max(1, int(len(open_trades) * pct / 100))
    sorted_trades = sorted(open_trades, key=lambda t: t["openTime"])
    for trade in sorted_trades[:n_to_close]:
        try:
            units_to_close = abs(int(float(trade["currentUnits"]) * pct / 100))
            units_to_close = max(1, units_to_close)
            r = trades.TradeClose(ACCOUNT_ID, tradeID=trade["id"],
                data={"units": str(units_to_close)})
            client.request(r)
            closed_pl += float(trade["unrealizedPL"]) * (pct / 100)
        except Exception as e:
            logger.error("[%s] partial close error: %s", pair, e)
    return closed_pl

def close_all_trades(pair, open_trades):
    total_pl = get_total_pl(open_trades)
    for trade in open_trades:
        try: client.request(trades.TradeClose(ACCOUNT_ID, tradeID=trade["id"]))
        except Exception as e: logger.error("[%s] close error: %s", pair, e)
    return total_pl


# ══════════════════════════════════════════
# MONITORS
# ══════════════════════════════════════════

async def monitor_daily_loss(app):
    global emergency_stop
    while True:
        try:
            if not emergency_stop and any(pair_active.values()):
                all_trades = get_all_open_trades()
                total_pl   = sum(float(t["unrealizedPL"]) for t in all_trades)
                if total_pl <= settings["daily_loss_limit"]:
                    emergency_stop = True
                    for p in ALL_PAIRS: stop_pair(p)
                    await app.bot.send_message(ALLOWED_USER_ID,
                        "EMERGENCY STOP! Daily loss limit ${} tercapai. Total floating: ${:.2f}".format(
                            settings["daily_loss_limit"], total_pl))
        except Exception as e: logger.error("monitor_daily_loss: %s", e)
        await asyncio.sleep(30)

async def monitor_margin(app):
    global margin_warned
    while True:
        try:
            if any(pair_active.values()):
                info = get_account_info()
                ml   = info["margin_level"]
                if ml < settings["margin_stop"] and info["margin"] > 0:
                    for p in ALL_PAIRS: stop_pair(p)
                    await app.bot.send_message(ALLOWED_USER_ID,
                        "MARGIN KRITIS {}%! Semua pair dihentikan.".format(ml))
                    margin_warned = True
                elif ml < settings["margin_warning"] and not margin_warned:
                    await app.bot.send_message(ALLOWED_USER_ID,
                        "WARNING: Margin Level {}%. Free margin: ${:.2f}".format(ml, info["free_margin"]))
                    margin_warned = True
                elif ml >= settings["margin_warning"]:
                    margin_warned = False
        except Exception as e: logger.error("monitor_margin: %s", e)
        await asyncio.sleep(60)

async def monitor_drawdown(app):
    """Monitor drawdown dan aktifkan recovery mode."""
    global recovery_mode
    while True:
        try:
            info    = get_account_info()
            balance = info["balance"]
            pl      = info["pl"]
            # Update peak balance per pair
            for p in ALL_PAIRS:
                if pair_perf[p]["peak_balance"] < balance:
                    pair_perf[p]["peak_balance"] = balance
            # Cek drawdown global
            if pl <= settings["drawdown_threshold"] and not recovery_mode:
                recovery_mode = True
                await app.bot.send_message(ALLOWED_USER_ID,
                    "RECOVERY MODE aktif. Floating: ${:.2f}. Lot dikurangi 50%.".format(pl))
            elif pl > 0 and recovery_mode:
                recovery_mode = False
                await app.bot.send_message(ALLOWED_USER_ID,
                    "Recovery mode selesai. Lot normal kembali.")
        except Exception as e: logger.error("monitor_drawdown: %s", e)
        await asyncio.sleep(120)

async def monitor_performance(app):
    """Cek performance tiap pair dan nonaktifkan yang underperform."""
    while True:
        await asyncio.sleep(3600)  # Cek setiap jam
        try:
            if not settings["perf_tracking"]: continue
            for pair in ALL_PAIRS:
                perf = pair_perf[pair]
                if perf["trades"] < settings["perf_min_trades"]: continue
                winrate = perf["wins"] / perf["trades"] * 100
                if winrate < settings["perf_min_winrate"] and pair_active.get(pair):
                    stop_pair(pair)
                    perf["disabled_by_perf"] = True
                    await app.bot.send_message(ALLOWED_USER_ID,
                        "{} dinonaktifkan. Win rate {:.0f}% < {}% (dari {} trade)".format(
                            pair_label(pair), winrate, settings["perf_min_winrate"], perf["trades"]))
        except Exception as e: logger.error("monitor_performance: %s", e)

async def auto_summary(app):
    while True:
        interval = settings["notify_interval"]
        if interval <= 0:
            await asyncio.sleep(60); continue
        await asyncio.sleep(interval)
        try:
            all_trades = get_all_open_trades()
            total_pl   = sum(float(t["unrealizedPL"]) for t in all_trades)
            info       = get_account_info()
            wr = "{:.0f}%".format(daily_stats["wins"]/daily_stats["trades"]*100) if daily_stats["trades"] > 0 else "N/A"
            msg = (
                "RINGKASAN OTOMATIS\n\n"
                "Uptime: {}\nBalance: ${:.2f}\nFloating: ${:.2f}\n"
                "Margin Level: {}%\nPair aktif: {}/{}\n"
                "Recovery mode: {}\n\n"
                "Sesi ini:\nTrade: {} | Win: {} | Loss: {}\nWin Rate: {} | P/L: ${:.2f}"
            ).format(
                uptime_str(), info["balance"], total_pl,
                info["margin_level"], active_pair_count(), len(ALL_PAIRS),
                "ON" if recovery_mode else "OFF",
                daily_stats["trades"], daily_stats["wins"], daily_stats["losses"],
                wr, daily_stats["total_pl"]
            )
            await app.bot.send_message(ALLOWED_USER_ID, msg)
        except Exception as e: logger.error("auto_summary: %s", e)


# ══════════════════════════════════════════
# TRADING LOOP
# ══════════════════════════════════════════

async def trading_loop_pair(pair, app):
    global emergency_stop
    state = pair_state[pair]
    logger.info("[%s] Loop start.", pair)

    while pair_active.get(pair, False):
        if emergency_stop:
            await asyncio.sleep(60); continue
        try:
            open_trades   = get_open_trades(pair)
            n_buy, n_sell = count_buy_sell(open_trades)
            total_pl      = get_total_pl(open_trades)
            lbl           = em(pair_label(pair))
            pl_s          = em("${:.2f}".format(total_pl))

            # Update peak profit
            if open_trades and total_pl > state["peak_profit"]:
                state["peak_profit"] = total_pl

            # ── 1. Hard SL (atau ATR SL) ──
            sl_limit = state["atr_sl"] if settings["atr_sl_tp"] and state["atr_sl"] else settings["hard_sl"]
            if open_trades and total_pl <= sl_limit:
                pl_c = close_all_trades(pair, open_trades)
                log_trade(pair, "all", "hard_sl", pl_c)
                state.update({"last_signal": None, "waiting_cross": None, "peak_profit": 0.0,
                               "partial_done": False, "breakeven_done": False,
                               "entry_price": None, "atr_sl": None, "atr_tp": None})
                await app.bot.send_message(ALLOWED_USER_ID,
                    "HARD SL {} | Floating: {} | Limit: {}".format(
                        pair_label(pair), pl_s, em("${:.2f}".format(sl_limit))))
                await asyncio.sleep(settings["check_interval"]); continue

            # ── 2. Partial Close ──
            if settings["partial_close"] and open_trades and not state["partial_done"]:
                if total_pl >= settings["partial_tp"]:
                    pl_c = close_trade_partial(pair, open_trades, settings["partial_pct"])
                    state["partial_done"] = True
                    log_trade(pair, "partial", "partial_close", pl_c)
                    await app.bot.send_message(ALLOWED_USER_ID,
                        "PARTIAL CLOSE {} | {}% posisi ditutup | Profit: {}".format(
                            pair_label(pair), settings["partial_pct"], pl_s))
                    open_trades = get_open_trades(pair)  # Refresh

            # ── 3. Trailing TP (atau ATR TP) ──
            tp_target = state["atr_tp"] if settings["atr_sl_tp"] and state["atr_tp"] else settings["total_tp"]
            if settings["trailing_tp"] and open_trades and state["peak_profit"] >= tp_target:
                if state["peak_profit"] - total_pl >= settings["trailing_pullback"]:
                    pl_c = close_all_trades(pair, open_trades)
                    log_trade(pair, "all", "trailing_tp", pl_c)
                    state.update({"last_signal": None, "waiting_cross": None, "peak_profit": 0.0,
                                   "partial_done": False, "breakeven_done": False,
                                   "entry_price": None, "atr_sl": None, "atr_tp": None})
                    await app.bot.send_message(ALLOWED_USER_ID,
                        "TRAILING TP {} | Peak: {} | Close: {}".format(
                            pair_label(pair), em("${:.2f}".format(state["peak_profit"])), pl_s))
                    await asyncio.sleep(settings["check_interval"]); continue

            # ── 4. Normal TP ──
            if open_trades and total_pl >= tp_target and not settings["trailing_tp"]:
                pl_c = close_all_trades(pair, open_trades)
                log_trade(pair, "all", "tp", pl_c)
                state.update({"last_signal": None, "waiting_cross": None, "peak_profit": 0.0,
                               "partial_done": False, "breakeven_done": False,
                               "entry_price": None, "atr_sl": None, "atr_tp": None})
                await app.bot.send_message(ALLOWED_USER_ID,
                    "TP {} | Profit: {}".format(pair_label(pair), pl_s))
                await asyncio.sleep(settings["check_interval"]); continue

            # ── 5. Crossing berlawanan ──
            if open_trades:
                signal = get_ema_signal_mtf(pair)
                if n_buy > 0 and signal in ("sell", "sell_active"):
                    pl_c = close_all_trades(pair, open_trades)
                    log_trade(pair, "buy", "cross_close", pl_c)
                    state.update({"last_signal": None, "waiting_cross": "sell", "peak_profit": 0.0,
                                   "partial_done": False, "breakeven_done": False,
                                   "entry_price": None, "atr_sl": None, "atr_tp": None})
                    await app.bot.send_message(ALLOWED_USER_ID,
                        "DEATH CROSS {} | BUY ditutup | P/L: {}".format(pair_label(pair), pl_s))
                    await asyncio.sleep(settings["check_interval"]); continue
                if n_sell > 0 and signal in ("buy", "buy_active"):
                    pl_c = close_all_trades(pair, open_trades)
                    log_trade(pair, "sell", "cross_close", pl_c)
                    state.update({"last_signal": None, "waiting_cross": "buy", "peak_profit": 0.0,
                                   "partial_done": False, "breakeven_done": False,
                                   "entry_price": None, "atr_sl": None, "atr_tp": None})
                    await app.bot.send_message(ALLOWED_USER_ID,
                        "GOLDEN CROSS {} | SELL ditutup | P/L: {}".format(pair_label(pair), pl_s))
                    await asyncio.sleep(settings["check_interval"]); continue

            # ── 6. Entry baru ──
            signal = get_ema_signal_mtf(pair)
            can_buy  = signal == "buy"  and state["last_signal"] != "buy"  and n_buy  == 0 and state["waiting_cross"] != "sell"
            can_sell = signal == "sell" and state["last_signal"] != "sell" and n_sell == 0 and state["waiting_cross"] != "buy"

            if can_buy or can_sell:
                # Trading hours
                trading_ok, trading_reason = is_trading_time()
                if not trading_ok:
                    await asyncio.sleep(settings["check_interval"]); continue

                # News filter
                news_pause, _ = is_news_time()
                if news_pause:
                    await asyncio.sleep(settings["check_interval"]); continue

                # Performance check
                if not check_pair_performance(pair):
                    logger.info("[%s] Disabled by performance.", pair)
                    stop_pair(pair); break

                # Correlation filter
                corr_blocked, corr_pair = is_correlation_blocked(pair)
                if corr_blocked:
                    logger.info("[%s] Blocked by correlation with %s.", pair, corr_pair)
                    await asyncio.sleep(settings["check_interval"]); continue

                # Margin check
                try:
                    info = get_account_info()
                    if info["margin_level"] < settings["margin_stop"] and info["margin"] > 0:
                        await asyncio.sleep(settings["check_interval"]); continue
                    lot = get_lot_size(info["balance"])
                except Exception:
                    lot = settings["lot_size"]

                # OHLC data untuk indikator
                try:
                    ohlc = get_ohlc(pair, 60, "D")
                except Exception as e:
                    logger.error("[%s] OHLC error: %s", pair, e)
                    await asyncio.sleep(settings["check_interval"]); continue

                # ADX filter
                if settings["adx_filter"]:
                    adx_val = calculate_adx(ohlc)
                    if adx_val < settings["adx_min"]:
                        await asyncio.sleep(settings["check_interval"]); continue
                else:
                    adx_val = 0

                # RSI filter
                rsi_val = 50
                if settings["rsi_filter"]:
                    rsi_val = calculate_rsi(ohlc["closes"], settings["rsi_period"])[-1]
                    rsi_val = round(rsi_val, 1)
                    if can_buy  and rsi_val < settings["rsi_buy_min"]:
                        await asyncio.sleep(settings["check_interval"]); continue
                    if can_sell and rsi_val > settings["rsi_sell_max"]:
                        await asyncio.sleep(settings["check_interval"]); continue

                # ATR untuk dynamic SL/TP
                atr = calculate_atr(ohlc, settings["atr_period"])
                pip_value = 10 * lot  # Estimasi pip value
                if settings["atr_sl_tp"] and atr > 0:
                    atr_sl_dollar = round(atr * settings["atr_sl_mult"] * 10000 * lot * 10, 2)
                    atr_tp_dollar = round(atr * settings["atr_tp_mult"] * 10000 * lot * 10, 2)
                    state["atr_sl"] = -abs(atr_sl_dollar)
                    state["atr_tp"] = abs(atr_tp_dollar)
                else:
                    state["atr_sl"] = None
                    state["atr_tp"] = None

                # Place order
                direction = "buy" if can_buy else "sell"
                place_order(pair, direction, lot)
                log_trade(pair, direction, "entry_1")
                state["last_signal"]    = direction
                state["waiting_cross"]  = None
                state["peak_profit"]    = 0.0
                state["partial_done"]   = False
                state["breakeven_done"] = False
                state["entry_price"]    = ohlc["closes"][-1]

                sl_info = em("${:.2f}".format(state["atr_sl"])) if state["atr_sl"] else em(str(settings["hard_sl"]))
                tp_info = em("${:.2f}".format(state["atr_tp"])) if state["atr_tp"] else em(str(settings["total_tp"]))
                icon    = "BUY" if can_buy else "SELL"
                await app.bot.send_message(ALLOWED_USER_ID,
                    "ENTRY {} {} | EMA{}/{} | ADX:{} RSI:{}\nLot:{} | SL:{} TP:{}".format(
                        icon, pair_label(pair),
                        settings["ema_fast"], settings["ema_slow"],
                        adx_val, rsi_val,
                        lot, sl_info, tp_info))

            # ── 7. Layering ──
            elif open_trades:
                last_pl      = get_last_trade_pl(open_trades)
                total_layers = n_buy + n_sell

                # Fibonacci layering
                layer_trigger = settings["layer_trigger"]
                if settings["fib_layers"] and state["entry_price"]:
                    fibs = get_swing_fib_levels(pair)
                    if fibs:
                        closes = get_ohlc(pair, 5, "D")["closes"]
                        curr_price = closes[-1] if closes else state["entry_price"]
                        # Layer di level Fibonacci
                        fib_trigger = any([
                            abs(curr_price - fibs["fib382"]) < 0.0010,
                            abs(curr_price - fibs["fib500"]) < 0.0010,
                            abs(curr_price - fibs["fib618"]) < 0.0010,
                        ])
                        if not fib_trigger and last_pl and last_pl > layer_trigger:
                            await asyncio.sleep(settings["check_interval"]); continue

                if last_pl is not None and last_pl <= layer_trigger:
                    if total_layers >= settings["max_layers"]:
                        pass  # Max layer reached
                    else:
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

                            if lot:
                                signal = get_ema_signal_mtf(pair)
                                ls     = em("${:.2f}".format(last_pl))
                                if n_buy > 0 and signal in ("buy", "buy_active"):
                                    place_order(pair, "buy", lot)
                                    log_trade(pair, "buy", "layer_{}".format(n_buy+1))
                                    await app.bot.send_message(ALLOWED_USER_ID,
                                        "LAYER BUY #{} {} | Last:{} Total:{} | {}/{}".format(
                                            n_buy+1, pair_label(pair), ls, pl_s, n_buy+1, settings["max_layers"]))
                                elif n_sell > 0 and signal in ("sell", "sell_active"):
                                    place_order(pair, "sell", lot)
                                    log_trade(pair, "sell", "layer_{}".format(n_sell+1))
                                    await app.bot.send_message(ALLOWED_USER_ID,
                                        "LAYER SELL #{} {} | Last:{} Total:{} | {}/{}".format(
                                            n_sell+1, pair_label(pair), ls, pl_s, n_sell+1, settings["max_layers"]))

        except Exception as e:
            err_str = str(e)
            logger.error("[%s] %s", pair, e)
            if "Insufficient authorization" in err_str:
                unavailable_pairs.add(pair)
                stop_pair(pair)
                await app.bot.send_message(ALLOWED_USER_ID,
                    "{} dinonaktifkan - pair tidak tersedia di akun ini.".format(pair_label(pair)))
                return
            try:
                await app.bot.send_message(ALLOWED_USER_ID,
                    "Error {}: {}".format(pair_label(pair), str(e)[:100]))
            except Exception: pass

        await asyncio.sleep(settings["check_interval"])
    logger.info("[%s] Loop stop.", pair)


def start_pair(pair, app):
    if pair_active.get(pair): return False
    if pair in unavailable_pairs: return "unavailable"
    if pair_perf[pair].get("disabled_by_perf"): return "perf"
    if active_pair_count() >= settings["max_active_pairs"]: return "max"
    pair_active[pair] = True
    pair_state[pair]  = {"last_signal": None, "waiting_cross": None, "peak_profit": 0.0,
                          "partial_done": False, "breakeven_done": False,
                          "entry_price": None, "atr_sl": None, "atr_tp": None}
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
    estop = "Reset Emergency Stop" if emergency_stop else "EMERGENCY STOP"
    rec   = " | RECOVERY MODE" if recovery_mode else ""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Pair ({}/{}){}".format(active_pair_count(), settings["max_active_pairs"], rec), callback_data="menu_pairs")],
        [InlineKeyboardButton("Setting", callback_data="menu_settings"),
         InlineKeyboardButton("Akun",    callback_data="menu_account")],
        [InlineKeyboardButton("Posisi",  callback_data="menu_positions"),
         InlineKeyboardButton("Log",     callback_data="menu_log")],
        [InlineKeyboardButton("News",    callback_data="menu_news"),
         InlineKeyboardButton("Jam Trading", callback_data="menu_hours")],
        [InlineKeyboardButton("Performance", callback_data="menu_perf"),
         InlineKeyboardButton("Korelasi",    callback_data="menu_corr")],
        [InlineKeyboardButton("ON Semua",    callback_data="all_on"),
         InlineKeyboardButton("OFF Semua",   callback_data="all_off")],
        [InlineKeyboardButton("Close Semua Posisi", callback_data="confirm_closeall")],
        [InlineKeyboardButton(estop, callback_data="toggle_estop")],
    ])

def kb_pairs(page=0):
    per_page    = 9
    start       = page * per_page
    page_pairs  = ALL_PAIRS[start:start+per_page]
    total_pages = (len(ALL_PAIRS) + per_page - 1) // per_page
    rows = []
    for pair in page_pairs:
        if pair in unavailable_pairs:                icon = "🚫"
        elif pair_perf[pair].get("disabled_by_perf"): icon = "📉"
        elif pair_active.get(pair):                  icon = "✅"
        else:                                         icon = "⭕"
        rows.append([InlineKeyboardButton("{} {}".format(icon, pair_label(pair)), callback_data="toggle_" + pair)])
    nav = []
    if page > 0: nav.append(InlineKeyboardButton("Prev", callback_data="pairs_page_{}".format(page-1)))
    nav.append(InlineKeyboardButton("{}/{}".format(page+1, total_pages), callback_data="noop"))
    if page < total_pages - 1: nav.append(InlineKeyboardButton("Next", callback_data="pairs_page_{}".format(page+1)))
    rows.append(nav)
    rows.append([InlineKeyboardButton("Menu", callback_data="menu_main")])
    return InlineKeyboardMarkup(rows)

def kb_settings():
    s = settings
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("── ENTRY ──", callback_data="noop")],
        [InlineKeyboardButton("Lot: {} | Dynamic: {}".format(s["lot_size"], "ON" if s["dynamic_lot"] else "OFF"), callback_data="set_lot_size")],
        [InlineKeyboardButton("Toggle Dynamic Lot", callback_data="toggle_dynamic_lot")],
        [InlineKeyboardButton("Per $: {}".format(s["dynamic_lot_per"]), callback_data="set_dynamic_lot_per")],
        [InlineKeyboardButton("EMA Fast: {}".format(s["ema_fast"]), callback_data="set_ema_fast"),
         InlineKeyboardButton("EMA Slow: {}".format(s["ema_slow"]), callback_data="set_ema_slow")],
        [InlineKeyboardButton("── FILTER ──", callback_data="noop")],
        [InlineKeyboardButton("Multi-TF H4: {}".format("ON" if s["mtf_enabled"] else "OFF"), callback_data="toggle_mtf_enabled")],
        [InlineKeyboardButton("RSI Filter: {} Min:{}".format("ON" if s["rsi_filter"] else "OFF", s["rsi_buy_min"]), callback_data="set_rsi_buy_min")],
        [InlineKeyboardButton("Toggle RSI Filter", callback_data="toggle_rsi_filter")],
        [InlineKeyboardButton("ADX Filter: {} Min:{}".format("ON" if s["adx_filter"] else "OFF", s["adx_min"]), callback_data="set_adx_min")],
        [InlineKeyboardButton("Toggle ADX Filter", callback_data="toggle_adx_filter")],
        [InlineKeyboardButton("Correlation Filter: {}".format("ON" if s["correlation_filter"] else "OFF"), callback_data="toggle_correlation_filter")],
        [InlineKeyboardButton("── LAYERING ──", callback_data="noop")],
        [InlineKeyboardButton("Layer Trigger: ${}".format(s["layer_trigger"]), callback_data="set_layer_trigger")],
        [InlineKeyboardButton("Max Layers: {}".format(s["max_layers"]), callback_data="set_max_layers")],
        [InlineKeyboardButton("Fibonacci Layers: {}".format("ON" if s["fib_layers"] else "OFF"), callback_data="toggle_fib_layers")],
        [InlineKeyboardButton("── EXIT ──", callback_data="noop")],
        [InlineKeyboardButton("TP: ${}".format(s["total_tp"]), callback_data="set_total_tp"),
         InlineKeyboardButton("Hard SL: ${}".format(s["hard_sl"]), callback_data="set_hard_sl")],
        [InlineKeyboardButton("Trailing TP: {}".format("ON" if s["trailing_tp"] else "OFF"), callback_data="toggle_trailing_tp"),
         InlineKeyboardButton("Pullback: ${}".format(s["trailing_pullback"]), callback_data="set_trailing_pullback")],
        [InlineKeyboardButton("Partial Close: {} at ${}".format("ON" if s["partial_close"] else "OFF", s["partial_tp"]), callback_data="toggle_partial_close")],
        [InlineKeyboardButton("Partial %: {}%".format(s["partial_pct"]), callback_data="set_partial_pct"),
         InlineKeyboardButton("Partial TP: ${}".format(s["partial_tp"]), callback_data="set_partial_tp")],
        [InlineKeyboardButton("ATR SL/TP: {} SL:{}x TP:{}x".format("ON" if s["atr_sl_tp"] else "OFF", s["atr_sl_mult"], s["atr_tp_mult"]), callback_data="toggle_atr_sl_tp")],
        [InlineKeyboardButton("ATR SL Mult: {}".format(s["atr_sl_mult"]), callback_data="set_atr_sl_mult"),
         InlineKeyboardButton("ATR TP Mult: {}".format(s["atr_tp_mult"]), callback_data="set_atr_tp_mult")],
        [InlineKeyboardButton("── PERFORMANCE ──", callback_data="noop")],
        [InlineKeyboardButton("Perf Tracking: {}".format("ON" if s["perf_tracking"] else "OFF"), callback_data="toggle_perf_tracking")],
        [InlineKeyboardButton("Min Win Rate: {}%".format(s["perf_min_winrate"]), callback_data="set_perf_min_winrate"),
         InlineKeyboardButton("Min Trades: {}".format(s["perf_min_trades"]), callback_data="set_perf_min_trades")],
        [InlineKeyboardButton("Drawdown Recovery: {}".format("ON" if s["drawdown_recovery"] else "OFF"), callback_data="toggle_drawdown_recovery")],
        [InlineKeyboardButton("Drawdown Threshold: ${}".format(s["drawdown_threshold"]), callback_data="set_drawdown_threshold")],
        [InlineKeyboardButton("── RISK GLOBAL ──", callback_data="noop")],
        [InlineKeyboardButton("Max Pair: {}".format(s["max_active_pairs"]), callback_data="set_max_active_pairs"),
         InlineKeyboardButton("Daily Limit: ${}".format(s["daily_loss_limit"]), callback_data="set_daily_loss_limit")],
        [InlineKeyboardButton("Margin Warn: {}%".format(s["margin_warning"]), callback_data="set_margin_warning"),
         InlineKeyboardButton("Margin Stop: {}%".format(s["margin_stop"]), callback_data="set_margin_stop")],
        [InlineKeyboardButton("── TRADING HOURS ──", callback_data="noop")],
        [InlineKeyboardButton("Hours: {} {}-{}WIB".format("ON" if s["trading_hours"] else "OFF", s["hour_start"], s["hour_end"]), callback_data="toggle_trading_hours")],
        [InlineKeyboardButton("Jam Start: {}".format(s["hour_start"]), callback_data="set_hour_start"),
         InlineKeyboardButton("Jam End: {}".format(s["hour_end"]), callback_data="set_hour_end")],
        [InlineKeyboardButton("Skip Jumat: {} h>{}".format("ON" if s["skip_friday"] else "OFF", s["skip_friday_hour"]), callback_data="toggle_skip_friday")],
        [InlineKeyboardButton("Skip Senin: {} h<{}".format("ON" if s["skip_monday"] else "OFF", s["skip_monday_hour"]), callback_data="toggle_skip_monday")],
        [InlineKeyboardButton("── NEWS ──", callback_data="noop")],
        [InlineKeyboardButton("News Filter: {}".format("ON" if s["news_filter"] else "OFF"), callback_data="toggle_news_filter")],
        [InlineKeyboardButton("Sebelum: {}m".format(s["news_pause_before"]), callback_data="set_news_pause_before"),
         InlineKeyboardButton("Setelah: {}m".format(s["news_pause_after"]), callback_data="set_news_pause_after")],
        [InlineKeyboardButton("── BOT ──", callback_data="noop")],
        [InlineKeyboardButton("Interval: {}s".format(s["check_interval"]), callback_data="set_check_interval"),
         InlineKeyboardButton("Summary: {}s".format(s["notify_interval"]), callback_data="set_notify_interval")],
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
    np, nev    = is_news_time()
    if np:      entry_status = "PAUSE: News"
    elif not ok: entry_status = "PAUSE: {}".format(reason)
    else:        entry_status = "OK"
    rec_str = " | RECOVERY MODE" if recovery_mode else ""
    return (
        "FOREX BOT v4\n\n"
        "Status: {}{}\nEntry: {}\nWaktu: {} WIB\nUptime: {}\nPair: {}/{}\n\n"
        "TP:${} | SL:${} | Layer:${} max:{}\n"
        "MTF:{} | RSI:{} | ADX:{} | Corr:{}\n"
        "Trailing:{} | Partial:{} | ATR:{}\n"
        "Perf:{} | DrawRecov:{}\n"
        "Hours:{} | News:{}"
    ).format(
        "EMERGENCY STOP" if emergency_stop else "Normal", rec_str,
        entry_status, now_wib().strftime("%H:%M %a"), uptime_str(),
        active_pair_count(), settings["max_active_pairs"],
        s["total_tp"], s["hard_sl"], s["layer_trigger"], s["max_layers"],
        "ON" if s["mtf_enabled"] else "OFF",
        "ON" if s["rsi_filter"] else "OFF",
        "ON" if s["adx_filter"] else "OFF",
        "ON" if s["correlation_filter"] else "OFF",
        "ON" if s["trailing_tp"] else "OFF",
        "ON" if s["partial_close"] else "OFF",
        "ON" if s["atr_sl_tp"] else "OFF",
        "ON" if s["perf_tracking"] else "OFF",
        "ON" if s["drawdown_recovery"] else "OFF",
        "ON" if s["trading_hours"] else "OFF",
        "ON" if s["news_filter"] else "OFF",
    )

async def text_positions():
    lines = ["STATUS POSISI\n"]
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
            state  = pair_state[pair]
            peak   = state["peak_profit"]
            atr_sl = state["atr_sl"]
            atr_tp = state["atr_tp"]
            sl_str = "${:.2f}".format(atr_sl) if atr_sl else str(settings["hard_sl"])
            tp_str = "${:.2f}".format(atr_tp) if atr_tp else str(settings["total_tp"])
            arrow  = "UP" if total_pl >= 0 else "DN"
            icon   = "ON" if pair_active.get(pair) else "OFF"
            lines.append(
                "[{}] {} | {} B:{} S:{} PL:${:.2f} Peak:${:.2f}\n  SL:{} TP:{} Partial:{}".format(
                    icon, pair_label(pair), arrow, n_buy, n_sell,
                    total_pl, peak, sl_str, tp_str,
                    "done" if state["partial_done"] else "no"
                )
            )
        except Exception: pass
    if not any_trade: lines.append("Tidak ada posisi terbuka")
    else: lines.append("\nGrand Total: ${:.2f}".format(grand_total))
    return "\n".join(lines)

async def text_account():
    try:
        info = get_account_info()
        lot  = get_lot_size(info["balance"])
        wr   = "{:.0f}%".format(daily_stats["wins"]/daily_stats["trades"]*100) if daily_stats["trades"] > 0 else "N/A"
        ml   = info["margin_level"]
        ml_s = "KRITIS" if ml < settings["margin_stop"] else ("WARN" if ml < settings["margin_warning"] else "OK")
        return (
            "STATUS AKUN\n\n"
            "Balance: ${:.2f}\nNAV: ${:.2f}\nFloating: ${:.2f}\n"
            "Margin Used: ${:.2f}\nFree Margin: ${:.2f}\n"
            "Margin Level: {}% [{}]\n"
            "Lot aktif: {}\nRecovery Mode: {}\n\n"
            "Pair: {}/{} | Uptime: {}\nEnv: {}\n\n"
            "STATISTIK SESI\n"
            "Trade:{} Win:{} Loss:{}\nWin Rate:{} | P/L:${:.2f}"
        ).format(
            info["balance"], info["nav"], info["pl"],
            info["margin"], info["free_margin"],
            ml, ml_s, round(lot, 4),
            "ON" if recovery_mode else "OFF",
            active_pair_count(), len(ALL_PAIRS), uptime_str(), OANDA_ENV,
            daily_stats["trades"], daily_stats["wins"], daily_stats["losses"],
            wr, daily_stats["total_pl"]
        )
    except Exception as e:
        return "Error akun: {}".format(str(e))

def text_performance():
    lines = ["PERFORMANCE PER PAIR\n"]
    for pair in ALL_PAIRS:
        perf = pair_perf[pair]
        if perf["trades"] == 0: continue
        wr     = perf["wins"] / perf["trades"] * 100
        status = "DISABLED" if perf["disabled_by_perf"] else ("ON" if pair_active.get(pair) else "OFF")
        lines.append(
            "[{}] {} | Trade:{} WR:{:.0f}% PL:${:.2f}".format(
                status, pair_label(pair), perf["trades"], wr, perf["total_pl"]
            )
        )
    if len(lines) == 1: lines.append("Belum ada data trade")
    return "\n".join(lines)

def text_correlation():
    lines = ["KORELASI PAIR\n", "Filter: {}\n".format("ON" if settings["correlation_filter"] else "OFF")]
    for i, group in enumerate(CORRELATION_GROUPS):
        active_in_group = [p for p in group if pair_active.get(p)]
        lines.append("Grup {}: {}".format(i+1, ", ".join(pair_label(p) for p in group)))
        if active_in_group:
            lines.append("  Aktif: {}".format(", ".join(pair_label(p) for p in active_in_group)))
    return "\n".join(lines)

def text_news_page():
    upcoming = get_upcoming_news(8)
    lines    = ["HIGH-IMPACT NEWS MINGGU INI\n"]
    np, _    = is_news_time()
    if np: lines.append("BOT PAUSE KARENA NEWS\n")
    if not upcoming:
        lines.append("Tidak ada news atau gagal fetch")
    for ev in upcoming:
        diff = int((ev["time"] - datetime.now()).total_seconds() / 60)
        when = "{}m lagi".format(diff) if diff < 60 else ev["time"].strftime("%a %H:%M WIB")
        lines.append("{} | {}".format(when, ev["title"]))
    return "\n".join(lines)

def text_hours():
    s  = settings
    ok, reason = is_trading_time()
    return (
        "TRADING HOURS\n\nSekarang: {} WIB\nStatus: {}\n\n"
        "Hours: {} | {}:00-{}:00 WIB\n"
        "Skip Jumat >{}:00: {}\nSkip Senin <{}:00: {}\n\n"
        "Sesi London+NY: 15:00-23:00 WIB\nSesi Tokyo: 07:00-15:00 WIB"
    ).format(
        now_wib().strftime("%H:%M %a"),
        "OK" if ok else reason,
        "ON" if s["trading_hours"] else "OFF", s["hour_start"], s["hour_end"],
        s["skip_friday_hour"], "ON" if s["skip_friday"] else "OFF",
        s["skip_monday_hour"], "ON" if s["skip_monday"] else "OFF",
    )

def text_log():
    if not trade_log: return "LOG TRADE\n\nBelum ada trade"
    lines = ["LOG TRADE (30 terakhir)\n"]
    for t in reversed(trade_log[-30:]):
        pl = " ${:.2f}".format(t["pl"]) if t["pl"] is not None else ""
        lines.append("{} {} {} {}{}".format(t["time"], t["pair"], t["direction"], t["action"], pl))
    return "\n".join(lines)


# ══════════════════════════════════════════
# SETTING CONFIG
# ══════════════════════════════════════════

SETTING_LABELS = {
    "lot_size":           "Lot Size base (skrg: {})\nContoh: 0.01",
    "dynamic_lot_per":    "Dynamic lot per $X balance (skrg: {})\nContoh: 1000",
    "ema_fast":           "EMA Fast (skrg: {})\nContoh: 20",
    "ema_slow":           "EMA Slow (skrg: {})\nContoh: 50",
    "rsi_period":         "RSI Period (skrg: {})\nContoh: 14",
    "rsi_buy_min":        "RSI min untuk BUY (skrg: {})\nContoh: 50",
    "rsi_sell_max":       "RSI max untuk SELL (skrg: {})\nContoh: 50",
    "adx_min":            "ADX Minimum (skrg: {})\nContoh: 20",
    "layer_trigger":      "Layer Trigger $ (skrg: {})\nHarus negatif: -10",
    "max_layers":         "Max Layers (skrg: {})\nContoh: 5",
    "total_tp":           "Take Profit $ (skrg: {})\nContoh: 25",
    "hard_sl":            "Hard SL $ (skrg: {})\nHarus negatif: -30",
    "trailing_pullback":  "Trailing Pullback $ (skrg: {})\nContoh: 3",
    "partial_tp":         "Partial TP $ (skrg: {})\nContoh: 15",
    "partial_pct":        "Partial % (skrg: {})\nContoh: 50",
    "atr_period":         "ATR Period (skrg: {})\nContoh: 14",
    "atr_sl_mult":        "ATR SL Multiplier (skrg: {})\nContoh: 1.5",
    "atr_tp_mult":        "ATR TP Multiplier (skrg: {})\nContoh: 3.0",
    "perf_min_winrate":   "Min Win Rate % (skrg: {})\nContoh: 40",
    "perf_min_trades":    "Min Trade sebelum evaluasi (skrg: {})\nContoh: 10",
    "drawdown_threshold": "Drawdown Threshold $ (skrg: {})\nHarus negatif: -20",
    "max_active_pairs":   "Max Pair Aktif (skrg: {})\nContoh: 5",
    "daily_loss_limit":   "Daily Loss Limit $ (skrg: {})\nHarus negatif: -100",
    "margin_warning":     "Margin Warning % (skrg: {})\nContoh: 200",
    "margin_stop":        "Margin Stop % (skrg: {})\nContoh: 150",
    "hour_start":         "Jam mulai WIB (skrg: {})\nContoh: 15",
    "hour_end":           "Jam selesai WIB (skrg: {})\nContoh: 23",
    "skip_friday_hour":   "Skip Jumat setelah jam (skrg: {})\nContoh: 21",
    "skip_monday_hour":   "Skip Senin sebelum jam (skrg: {})\nContoh: 10",
    "news_pause_before":  "Pause sebelum news menit (skrg: {})\nContoh: 30",
    "news_pause_after":   "Pause setelah news menit (skrg: {})\nContoh: 30",
    "check_interval":     "Interval detik (skrg: {})\nMinimal 10",
    "notify_interval":    "Auto summary detik (skrg: {})\n0 = nonaktif",
    "max_corr_pairs":     "Max pair per korelasi grup (skrg: {})\nContoh: 1",
    "breakeven_trigger":  "Breakeven trigger $ (skrg: {})\nContoh: 5",
}

INT_SETTINGS = {
    "ema_fast", "ema_slow", "rsi_period", "rsi_buy_min", "rsi_sell_max",
    "adx_min", "max_layers", "max_active_pairs", "notify_interval",
    "check_interval", "hour_start", "hour_end", "skip_friday_hour",
    "skip_monday_hour", "news_pause_before", "news_pause_after",
    "margin_warning", "margin_stop", "perf_min_winrate", "perf_min_trades",
    "partial_pct", "atr_period", "max_corr_pairs",
}

TOGGLE_SETTINGS = {
    "toggle_dynamic_lot":       ("dynamic_lot",       "Dynamic Lot"),
    "toggle_mtf_enabled":       ("mtf_enabled",       "Multi-TF H4"),
    "toggle_rsi_filter":        ("rsi_filter",        "RSI Filter"),
    "toggle_adx_filter":        ("adx_filter",        "ADX Filter"),
    "toggle_correlation_filter":("correlation_filter","Correlation Filter"),
    "toggle_fib_layers":        ("fib_layers",        "Fibonacci Layers"),
    "toggle_trailing_tp":       ("trailing_tp",       "Trailing TP"),
    "toggle_partial_close":     ("partial_close",     "Partial Close"),
    "toggle_atr_sl_tp":         ("atr_sl_tp",         "ATR SL/TP"),
    "toggle_perf_tracking":     ("perf_tracking",     "Perf Tracking"),
    "toggle_drawdown_recovery": ("drawdown_recovery", "Drawdown Recovery"),
    "toggle_trading_hours":     ("trading_hours",     "Trading Hours"),
    "toggle_news_filter":       ("news_filter",       "News Filter"),
    "toggle_skip_friday":       ("skip_friday",       "Skip Jumat"),
    "toggle_skip_monday":       ("skip_monday",       "Skip Senin"),
}


# ══════════════════════════════════════════
# HANDLERS
# ══════════════════════════════════════════

async def start_command(update, context):
    global bot_start_time
    if not is_allowed(update): return
    if not bot_start_time: bot_start_time = datetime.now()
    await update.message.reply_text(text_main(), reply_markup=kb_main())

async def testentry_command(update, context):
    if not is_allowed(update): return
    await update.message.reply_text("Menjalankan test entry...")
    results = []
    for direction in ["buy", "sell"]:
        try:
            units = "1000" if direction == "buy" else "-1000"
            r = orders.OrderCreate(ACCOUNT_ID, data={"order": {"type": "MARKET", "instrument": "EUR_USD", "units": units}})
            client.request(r)
            trade_id = r.response["orderFillTransaction"]["tradeOpened"]["tradeID"]
            results.append("OK {} ID:{}".format(direction.upper(), trade_id))
            await asyncio.sleep(1)
            client.request(trades.TradeClose(ACCOUNT_ID, tradeID=trade_id))
            results.append("OK {} closed".format(direction.upper()))
        except Exception as e:
            results.append("FAIL {}: {}".format(direction.upper(), str(e)[:80]))
    await update.message.reply_text("Test Entry:\n" + "\n".join(results))

async def button_handler(update, context):
    global emergency_stop
    query = update.callback_query
    await query.answer()
    if query.from_user.id != ALLOWED_USER_ID: return
    data = query.data

    async def edit_main():
        try: await query.edit_message_text(text_main(), reply_markup=kb_main())
        except Exception: pass

    async def edit_settings():
        try: await query.edit_message_text("SETTING BOT v4\nKetuk untuk ubah:", reply_markup=kb_settings())
        except Exception: pass

    if   data == "menu_main":     await edit_main()
    elif data == "menu_settings": await edit_settings()
    elif data == "noop":          pass

    elif data == "menu_pairs":
        cnt, mx = active_pair_count(), settings["max_active_pairs"]
        try:
            await query.edit_message_text(
                "KELOLA PAIR\nAktif: {}/{} (maks {})\nKetuk untuk toggle:".format(cnt, len(ALL_PAIRS), mx),
                reply_markup=kb_pairs(0))
        except Exception: pass

    elif data.startswith("pairs_page_"):
        page = int(data[len("pairs_page_"):])
        cnt, mx = active_pair_count(), settings["max_active_pairs"]
        try:
            await query.edit_message_text(
                "KELOLA PAIR\nAktif: {}/{} (maks {})\nKetuk untuk toggle:".format(cnt, len(ALL_PAIRS), mx),
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
            if result == "max":
                await query.answer("Maks {} pair!".format(settings["max_active_pairs"]), show_alert=True); return
            if result == "unavailable":
                await query.answer("{} tidak tersedia".format(pair_label(pair)), show_alert=True); return
            if result == "perf":
                await query.answer("{} dinonaktifkan karena win rate rendah".format(pair_label(pair)), show_alert=True); return
            await query.answer("{} ON".format(pair_label(pair)))
        cnt, mx = active_pair_count(), settings["max_active_pairs"]
        try:
            await query.edit_message_text(
                "KELOLA PAIR\nAktif: {}/{} (maks {})\nKetuk untuk toggle:".format(cnt, len(ALL_PAIRS), mx),
                reply_markup=kb_pairs(0))
        except Exception: pass

    elif data in TOGGLE_SETTINGS:
        key, label = TOGGLE_SETTINGS[data]
        settings[key] = not settings[key]
        await query.answer("{}: {}".format(label, "ON" if settings[key] else "OFF"))
        await edit_settings()

    elif data == "toggle_estop":
        if emergency_stop:
            emergency_stop = False
            await query.answer("Emergency Stop direset!", show_alert=True)
        else:
            emergency_stop = True
            for p in ALL_PAIRS: stop_pair(p)
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
        for p in ALL_PAIRS: stop_pair(p)
        await query.answer("Semua pair OFF!", show_alert=True)
        await edit_main()

    elif data == "menu_account":
        txt = await text_account()
        try: await query.edit_message_text(txt, reply_markup=kb_back())
        except Exception: pass

    elif data == "menu_positions":
        txt = await text_positions()
        try: await query.edit_message_text(txt, reply_markup=kb_back())
        except Exception: pass

    elif data == "menu_log":
        try: await query.edit_message_text(text_log(), reply_markup=kb_back())
        except Exception: pass

    elif data == "menu_news":
        try: await query.edit_message_text(text_news_page(), reply_markup=kb_back())
        except Exception: pass

    elif data == "menu_hours":
        try: await query.edit_message_text(text_hours(), reply_markup=kb_back())
        except Exception: pass

    elif data == "menu_perf":
        try: await query.edit_message_text(text_performance(), reply_markup=kb_back())
        except Exception: pass

    elif data == "menu_corr":
        try: await query.edit_message_text(text_correlation(), reply_markup=kb_back())
        except Exception: pass

    elif data == "confirm_closeall":
        try: await query.edit_message_text("Yakin close SEMUA posisi?", reply_markup=kb_confirm_closeall())
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
            except Exception as e: logger.error("closeall [%s]: %s", pair, e)
        try: await query.edit_message_text("Ditutup: {} posisi\nTotal P/L: ${:.2f}".format(closed, total), reply_markup=kb_back())
        except Exception: pass

    elif data.startswith("set_"):
        key = data[len("set_"):]
        pending_setting_key[query.from_user.id] = key
        label = SETTING_LABELS.get(key, key).format(settings.get(key, "?"))
        try:
            await query.edit_message_text(
                "Ubah {}\n\n{}\n\nKirim nilai baru:".format(key, label),
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Batal", callback_data="menu_settings")]]))
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
            "news_pause_before":  value >= 0,
            "news_pause_after":   value >= 0,
            "partial_pct":        0 < value <= 100,
            "partial_tp":         value > 0,
            "atr_sl_mult":        value > 0,
            "atr_tp_mult":        value > 0,
            "perf_min_winrate":   0 <= value <= 100,
            "perf_min_trades":    value >= 1,
            "drawdown_threshold": value < 0,
            "max_corr_pairs":     value >= 1,
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
        await update.message.reply_text("{} -> {}".format(key, settings[key]))
        await update.message.reply_text("SETTING BOT v4\nKetuk untuk ubah:", reply_markup=kb_settings())
    except ValueError as e:
        await update.message.reply_text("Error: {}\nCoba lagi:".format(str(e)))


# ══════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════

async def post_init(app):
    global bot_start_time
    bot_start_time = datetime.now()
    asyncio.create_task(monitor_daily_loss(app))
    asyncio.create_task(monitor_margin(app))
    asyncio.create_task(monitor_drawdown(app))
    asyncio.create_task(monitor_performance(app))
    asyncio.create_task(auto_summary(app))
    asyncio.create_task(asyncio.to_thread(fetch_news))
    logger.info("Bot v4 started.")

def main():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start",     start_command))
    app.add_handler(CommandHandler("menu",      start_command))
    app.add_handler(CommandHandler("testentry", testentry_command))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, receive_setting_value))
    logger.info("Bot v4 siap.")
    app.run_polling()

if __name__ == "__main__":
    main()
