import asyncio
import logging
import os
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes, CallbackQueryHandler
import oandapyV20
from oandapyV20.endpoints import orders, positions, trades, instruments, accounts
import pandas as pd
import numpy as np

# ─────────────────────────────────────────
# KONFIGURASI — ISI SESUAI AKUN KAMU
# ─────────────────────────────────────────
TELEGRAM_TOKEN   = "ISI_TOKEN_BOT_TELEGRAM_KAMU"
OANDA_TOKEN      = "ISI_API_TOKEN_OANDA_KAMU"
ACCOUNT_ID       = "ISI_ACCOUNT_ID_OANDA_KAMU"
ALLOWED_USER_ID  = 123456789          # Ganti dengan Telegram user ID kamu
INSTRUMENT       = "EUR_USD"
LOT_SIZE         = 0.01               # Fixed lot
LAYER_TRIGGER    = -2.0               # Entry lagi kalau posisi terakhir floating -$2
TOTAL_TP         = 10.0               # Close semua kalau total profit >= $10
CHECK_INTERVAL   = 60                 # Cek kondisi setiap 60 detik
OANDA_ENV        = "practice"         # "practice" untuk demo, "live" untuk real
# ─────────────────────────────────────────

logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

client  = oandapyV20.API(access_token=OANDA_TOKEN, environment=OANDA_ENV)
bot_running = False
bot_task    = None


# ══════════════════════════════════════════
# FUNGSI OANDA
# ══════════════════════════════════════════

def get_candles(count=60):
    """Ambil candle Daily EUR/USD."""
    params = {"count": count, "granularity": "D"}
    r = instruments.InstrumentsCandles(INSTRUMENT, params=params)
    client.request(r)
    candles = r.response["candles"]
    closes = [float(c["mid"]["c"]) for c in candles if c["complete"]]
    return closes


def calculate_ema(data, period):
    """Hitung EMA dari list harga."""
    s = pd.Series(data)
    return s.ewm(span=period, adjust=False).mean().tolist()


def get_ema_signal():
    """
    Return:
      'buy'  → EMA20 baru saja crossing di atas EMA50 (1 candle lalu)
      'sell' → EMA20 baru saja crossing di bawah EMA50 (1 candle lalu)
      'buy_active'  → EMA20 masih di atas EMA50 (tidak baru crossing)
      'sell_active' → EMA20 masih di bawah EMA50
      None   → data tidak cukup
    """
    closes = get_candles(60)
    if len(closes) < 52:
        return None

    ema20 = calculate_ema(closes, 20)
    ema50 = calculate_ema(closes, 50)

    # Indeks -1 = candle terakhir yang sudah close
    curr20, curr50 = ema20[-1], ema50[-1]
    prev20, prev50 = ema20[-2], ema50[-2]

    bullish_cross = prev20 <= prev50 and curr20 > curr50   # Golden cross
    bearish_cross = prev20 >= prev50 and curr20 < curr50   # Death cross

    if bullish_cross:
        return "buy"
    elif bearish_cross:
        return "sell"
    elif curr20 > curr50:
        return "buy_active"
    elif curr20 < curr50:
        return "sell_active"
    return None


def get_open_trades():
    """Ambil semua posisi terbuka."""
    r = trades.TradesList(ACCOUNT_ID, params={"instrument": INSTRUMENT, "state": "OPEN"})
    client.request(r)
    return r.response.get("trades", [])


def get_last_trade_pl(open_trades):
    """Ambil unrealized P/L dari posisi yang paling terakhir dibuka."""
    if not open_trades:
        return None
    # Sort by openTime, ambil yang terbaru
    sorted_trades = sorted(open_trades, key=lambda t: t["openTime"], reverse=True)
    return float(sorted_trades[0]["unrealizedPL"])


def get_total_pl(open_trades):
    """Hitung total unrealized P/L semua posisi."""
    return sum(float(t["unrealizedPL"]) for t in open_trades)


def count_buy_sell(open_trades):
    buys  = [t for t in open_trades if int(float(t["currentUnits"])) > 0]
    sells = [t for t in open_trades if int(float(t["currentUnits"])) < 0]
    return len(buys), len(sells)


def place_order(direction: str):
    """Place market order. direction: 'buy' atau 'sell'."""
    units = str(int(LOT_SIZE * 100000))
    if direction == "sell":
        units = "-" + units

    order_data = {
        "order": {
            "type": "MARKET",
            "instrument": INSTRUMENT,
            "units": units,
        }
    }
    r = orders.OrderCreate(ACCOUNT_ID, data=order_data)
    client.request(r)
    logger.info(f"Order {direction.upper()} placed.")
    return r.response


def close_all_trades(open_trades):
    """Close semua posisi terbuka."""
    for trade in open_trades:
        trade_id = trade["id"]
        r = trades.TradeClose(ACCOUNT_ID, tradeID=trade_id)
        try:
            client.request(r)
            logger.info(f"Closed trade {trade_id}")
        except Exception as e:
            logger.error(f"Gagal close trade {trade_id}: {e}")


def get_account_balance():
    r = accounts.AccountSummary(ACCOUNT_ID)
    client.request(r)
    return float(r.response["account"]["balance"])


# ══════════════════════════════════════════
# LOGIKA UTAMA BOT
# ══════════════════════════════════════════

async def trading_loop(app):
    """Loop utama yang jalan selama bot aktif."""
    global bot_running

    last_signal   = None   # Signal terakhir yang diproses
    waiting_cross = None   # Arah yang sedang ditunggu konfirmasi setelah crossing berlawanan

    logger.info("Trading loop dimulai.")

    while bot_running:
        try:
            signal      = get_ema_signal()
            open_trades = get_open_trades()
            n_buy, n_sell = count_buy_sell(open_trades)
            total_pl    = get_total_pl(open_trades)

            logger.info(f"Signal: {signal} | Trades: {len(open_trades)} | Total P/L: ${total_pl:.2f}")

            # ── 1. Cek total profit target ──
            if open_trades and total_pl >= TOTAL_TP:
                close_all_trades(open_trades)
                msg = f"🎯 *Total profit +${total_pl:.2f} tercapai!*\nSemua posisi ditutup."
                await app.bot.send_message(chat_id=ALLOWED_USER_ID, text=msg, parse_mode="Markdown")
                last_signal   = None
                waiting_cross = None
                await asyncio.sleep(CHECK_INTERVAL)
                continue

            # ── 2. Cek crossing berlawanan → close semua ──
            if open_trades:
                if n_buy > 0 and signal in ("sell", "sell_active"):
                    close_all_trades(open_trades)
                    msg = f"🔄 *EMA crossing berlawanan (Death Cross)*\nSemua posisi BUY ditutup. P/L: ${total_pl:.2f}\nMenunggu konfirmasi SELL..."
                    await app.bot.send_message(chat_id=ALLOWED_USER_ID, text=msg, parse_mode="Markdown")
                    last_signal   = None
                    waiting_cross = "sell"  # Menunggu candle konfirmasi sell
                    await asyncio.sleep(CHECK_INTERVAL)
                    continue

                if n_sell > 0 and signal in ("buy", "buy_active"):
                    close_all_trades(open_trades)
                    msg = f"🔄 *EMA crossing berlawanan (Golden Cross)*\nSemua posisi SELL ditutup. P/L: ${total_pl:.2f}\nMenunggu konfirmasi BUY..."
                    await app.bot.send_message(chat_id=ALLOWED_USER_ID, text=msg, parse_mode="Markdown")
                    last_signal   = None
                    waiting_cross = "buy"
                    await asyncio.sleep(CHECK_INTERVAL)
                    continue

            # ── 3. Entry pertama setelah crossing (konfirmasi 1 candle) ──
            # Entry buy: signal "buy" (crossing baru terjadi di candle sebelumnya, sudah close)
            if signal == "buy" and last_signal != "buy" and n_buy == 0 and waiting_cross != "sell":
                place_order("buy")
                last_signal   = "buy"
                waiting_cross = None
                msg = "📈 *ENTRY BUY #1*\nGolden Cross terdeteksi (EMA20 > EMA50)\nLot: 0.01"
                await app.bot.send_message(chat_id=ALLOWED_USER_ID, text=msg, parse_mode="Markdown")

            elif signal == "sell" and last_signal != "sell" and n_sell == 0 and waiting_cross != "buy":
                place_order("sell")
                last_signal   = "sell"
                waiting_cross = None
                msg = "📉 *ENTRY SELL #1*\nDeath Cross terdeteksi (EMA20 < EMA50)\nLot: 0.01"
                await app.bot.send_message(chat_id=ALLOWED_USER_ID, text=msg, parse_mode="Markdown")

            # ── 4. Layering — posisi terakhir floating <= -$2 ──
            elif open_trades:
                last_pl = get_last_trade_pl(open_trades)

                if last_pl is not None and last_pl <= LAYER_TRIGGER:
                    if n_buy > 0 and signal in ("buy", "buy_active"):
                        place_order("buy")
                        layer_num = n_buy + 1
                        msg = f"📈 *LAYER BUY #{layer_num}*\nPosisi terakhir floating ${last_pl:.2f}\nTotal posisi: {layer_num} | Total P/L: ${total_pl:.2f}"
                        await app.bot.send_message(chat_id=ALLOWED_USER_ID, text=msg, parse_mode="Markdown")

                    elif n_sell > 0 and signal in ("sell", "sell_active"):
                        place_order("sell")
                        layer_num = n_sell + 1
                        msg = f"📉 *LAYER SELL #{layer_num}*\nPosisi terakhir floating ${last_pl:.2f}\nTotal posisi: {layer_num} | Total P/L: ${total_pl:.2f}"
                        await app.bot.send_message(chat_id=ALLOWED_USER_ID, text=msg, parse_mode="Markdown")

        except Exception as e:
            logger.error(f"Error di trading loop: {e}")
            await app.bot.send_message(
                chat_id=ALLOWED_USER_ID,
                text=f"⚠️ *Error:* `{str(e)}`",
                parse_mode="Markdown"
            )

        await asyncio.sleep(CHECK_INTERVAL)

    logger.info("Trading loop berhenti.")


# ══════════════════════════════════════════
# TELEGRAM COMMAND HANDLERS
# ══════════════════════════════════════════

def is_allowed(update: Update) -> bool:
    return update.effective_user.id == ALLOWED_USER_ID


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    keyboard = [
        [InlineKeyboardButton("▶️ Start Bot", callback_data="start_bot"),
         InlineKeyboardButton("⏹ Stop Bot",  callback_data="stop_bot")],
        [InlineKeyboardButton("📊 Status",    callback_data="status")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(
        "🤖 *Forex Trading Bot*\n\n"
        "Strategi: EMA 20/50 Layering\n"
        "Pair: EUR/USD | Lot: 0.01\n"
        "TP: +$10 | Layer: setiap -$2\n\n"
        "Gunakan tombol di bawah untuk mengontrol bot:",
        parse_mode="Markdown",
        reply_markup=reply_markup
    )


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global bot_running, bot_task

    query = update.callback_query
    await query.answer()

    if query.from_user.id != ALLOWED_USER_ID:
        return

    if query.data == "start_bot":
        if bot_running:
            await query.edit_message_text("⚠️ Bot sudah berjalan!")
            return
        bot_running = True
        bot_task    = asyncio.create_task(trading_loop(context.application))
        await query.edit_message_text(
            "✅ *Bot AKTIF*\n\nMemantau EUR/USD timeframe Daily.\n"
            "Kamu akan mendapat notifikasi setiap ada aksi.",
            parse_mode="Markdown"
        )

    elif query.data == "stop_bot":
        if not bot_running:
            await query.edit_message_text("⚠️ Bot sudah berhenti!")
            return
        bot_running = False
        if bot_task:
            bot_task.cancel()
        await query.edit_message_text("⏹ *Bot DIHENTIKAN*\nPosisi yang terbuka dibiarkan berjalan.", parse_mode="Markdown")

    elif query.data == "status":
        try:
            open_trades = get_open_trades()
            total_pl    = get_total_pl(open_trades)
            n_buy, n_sell = count_buy_sell(open_trades)
            balance     = get_account_balance()
            signal      = get_ema_signal()

            signal_text = {
                "buy": "🟢 Golden Cross (BUY)",
                "sell": "🔴 Death Cross (SELL)",
                "buy_active": "🟢 EMA20 di atas EMA50",
                "sell_active": "🔴 EMA20 di bawah EMA50",
                None: "❓ Tidak diketahui"
            }.get(signal, "❓")

            status_msg = (
                f"📊 *STATUS BOT*\n\n"
                f"Bot: {'✅ Aktif' if bot_running else '⏹ Berhenti'}\n"
                f"Signal: {signal_text}\n\n"
                f"Posisi BUY terbuka: {n_buy}\n"
                f"Posisi SELL terbuka: {n_sell}\n"
                f"Total floating P/L: ${total_pl:.2f}\n\n"
                f"Balance: ${balance:.2f}"
            )
            await query.edit_message_text(status_msg, parse_mode="Markdown")
        except Exception as e:
            await query.edit_message_text(f"❌ Error ambil status: {e}")


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    try:
        open_trades   = get_open_trades()
        total_pl      = get_total_pl(open_trades)
        n_buy, n_sell = count_buy_sell(open_trades)
        balance       = get_account_balance()
        signal        = get_ema_signal()

        signal_text = {
            "buy": "🟢 Golden Cross (BUY)",
            "sell": "🔴 Death Cross (SELL)",
            "buy_active": "🟢 EMA20 di atas EMA50",
            "sell_active": "🔴 EMA20 di bawah EMA50",
            None: "❓ Tidak diketahui"
        }.get(signal, "❓")

        msg = (
            f"📊 *STATUS BOT*\n\n"
            f"Bot: {'✅ Aktif' if bot_running else '⏹ Berhenti'}\n"
            f"Signal: {signal_text}\n\n"
            f"Posisi BUY terbuka: {n_buy}\n"
            f"Posisi SELL terbuka: {n_sell}\n"
            f"Total floating P/L: ${total_pl:.2f}\n\n"
            f"Balance: ${balance:.2f}"
        )
        await update.message.reply_text(msg, parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}")


async def closeall_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Command manual untuk close semua posisi."""
    if not is_allowed(update):
        return
    open_trades = get_open_trades()
    if not open_trades:
        await update.message.reply_text("Tidak ada posisi terbuka.")
        return
    total_pl = get_total_pl(open_trades)
    close_all_trades(open_trades)
    await update.message.reply_text(
        f"✅ *Semua posisi ditutup secara manual.*\nP/L: ${total_pl:.2f}",
        parse_mode="Markdown"
    )


# ══════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════

def main():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start",    start_command))
    app.add_handler(CommandHandler("status",   status_command))
    app.add_handler(CommandHandler("closeall", closeall_command))
    app.add_handler(CallbackQueryHandler(button_handler))

    logger.info("Bot Telegram siap. Menunggu perintah...")
    app.run_polling()


if __name__ == "__main__":
    main()
