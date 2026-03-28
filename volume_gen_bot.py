"""
Volume Generation Bot v1.0 - Panther Platform
Strategy: Maker-Maker Ping-Pong
Target: $100K+ daily notional volume, capital preservation
"""

from __future__ import annotations

import ccxt
import time
import logging
import os
import sys
import signal
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("VolumeGenBot")


# ──────────────────────────────────────────────────────────
# CONFIGURATION
# ──────────────────────────────────────────────────────────

@dataclass
class BotConfig:
    # Exchange
    symbol: str = "BTC/USDT:USDT"       # Bybit USDT perpetual
    leverage: int = 30
    margin_mode: str = "isolated"

    # Volume
    daily_volume_target: float = 100_000.0   # $100K daily target

    # Position sizing
    position_utilization: float = 0.60       # Use 60% of max leverage capacity

    # Strategy parameters
    entry_offset_pct: float = 0.015          # Enter 0.015% from mid price
    spread_pct: float = 0.05                 # Target 0.05% spread (covers 0.04% fees + buffer)
    entry_timeout_sec: int = 90              # Cancel entry if not filled in 90s
    position_timeout_sec: int = 180          # Close position after 3 min max hold

    # Risk management
    stop_loss_pct: float = 0.10              # 0.10% stop loss per trade
    max_daily_loss_pct: float = 0.03         # 3% max daily drawdown
    max_consecutive_losses: int = 5          # Pause after 5 consecutive stops
    cooldown_after_loss_sec: int = 30        # 30s cooldown after a stop hit

    # Execution
    poll_interval_sec: float = 0.5           # Check order status every 500ms
    cycle_pause_sec: float = 5.0             # Pause between cycles
    max_market_spread_pct: float = 0.20      # Skip if book spread > 0.20%

    # Fees (Bybit VIP 0 maker rate)
    maker_fee_rate: float = 0.0002           # 0.02%
    taker_fee_rate: float = 0.00055          # 0.055% (used on stop exits)

    # Safety
    min_notional_usd: float = 5.0
    min_equity_usd: float = 20.0
    fallback_equity: float = 150.0           # If balance API fails


# ──────────────────────────────────────────────────────────
# VOLUME GENERATION BOT
# ──────────────────────────────────────────────────────────

class VolumeGenBot:
    def __init__(self, config: BotConfig, api_key: str, secret: str):
        self.config = config
        self.exchange = ccxt.bybit({
            "apiKey": api_key,
            "secret": secret,
            "options": {"defaultType": "swap"},
            "enableRateLimit": True,
        })

        # Day tracking
        self._current_day: str = ""
        self._equity_start: float = 0.0
        self._equity_live: float = 0.0

        # Daily counters
        self.daily_volume: float = 0.0
        self.daily_pnl: float = 0.0
        self.trade_count: int = 0
        self.win_count: int = 0
        self.loss_count: int = 0
        self.consecutive_losses: int = 0

        # Direction toggle: True = long cycle, False = short cycle
        self._next_is_long: bool = True

        # Control
        self.running: bool = False

    # ──── EXCHANGE HELPERS ────────────────────────────────

    def _get_equity(self) -> float:
        """Fetch live USDT equity from Bybit."""
        try:
            bal = self.exchange.fetch_balance()
            equity = float(bal.get("total", {}).get("USDT", 0))
            if equity > 0:
                self._equity_live = equity
                return equity
        except Exception as e:
            logger.warning(f"Balance fetch failed: {e}")
        # Fallback
        if self._equity_live > 0:
            return self._equity_live
        return self.config.fallback_equity

    def _get_ticker(self) -> dict:
        return self.exchange.fetch_ticker(self.config.symbol)

    def _get_mid_price(self) -> float:
        t = self._get_ticker()
        bid, ask = t.get("bid", 0), t.get("ask", 0)
        if bid and ask:
            return (bid + ask) / 2
        raise ValueError("No bid/ask available")

    def _get_book_spread_pct(self) -> float:
        t = self._get_ticker()
        bid, ask = t.get("bid"), t.get("ask")
        if bid and ask and bid > 0:
            return (ask - bid) / bid * 100
        return float("inf")

    def _setup_leverage(self):
        try:
            self.exchange.set_leverage(self.config.leverage, self.config.symbol)
        except Exception as e:
            logger.warning(f"Set leverage: {e}")
        try:
            self.exchange.set_margin_mode(
                self.config.margin_mode, self.config.symbol
            )
        except Exception as e:
            logger.warning(f"Set margin mode: {e}")

    # ──── ORDER HELPERS ───────────────────────────────────

    def _place_limit(self, side: str, price: float, size: float) -> dict:
        price_str = self.exchange.price_to_precision(self.config.symbol, price)
        return self.exchange.create_order(
            symbol=self.config.symbol,
            type="limit",
            side=side,
            amount=size,
            price=float(price_str),
            params={"postOnly": True, "timeInForce": "GTC"},
        )

    def _cancel(self, order_id: str):
        try:
            self.exchange.cancel_order(order_id, self.config.symbol)
        except Exception:
            pass

    def _close_position(self):
        """Market-close any open position."""
        try:
            positions = self.exchange.fetch_positions([self.config.symbol])
            for pos in positions:
                contracts = abs(float(pos.get("contracts", 0)))
                if contracts > 0:
                    side = "sell" if pos["side"] == "long" else "buy"
                    self.exchange.create_order(
                        symbol=self.config.symbol,
                        type="market",
                        side=side,
                        amount=contracts,
                        params={"reduceOnly": True},
                    )
                    logger.info(f"Closed {pos['side']} {contracts}")
        except Exception as e:
            logger.error(f"Close position error: {e}")

    def _fetch_order(self, order_id: str) -> dict:
        return self.exchange.fetch_order(order_id, self.config.symbol)

    # ──── POSITION SIZING ─────────────────────────────────

    def _calc_size(self, price: float) -> float:
        """
        Size = (equity * leverage * utilization) / price
        Then round down to exchange precision.
        """
        equity = self._get_equity()
        max_notional = equity * self.config.leverage * self.config.position_utilization
        raw_size = max_notional / price
        size = float(
            self.exchange.amount_to_precision(self.config.symbol, raw_size)
        )
        return size

    # ──── DAY MANAGEMENT ──────────────────────────────────

    def _day_key(self) -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")

    def _reset_day_if_needed(self):
        today = self._day_key()
        if today != self._current_day:
            self._current_day = today
            self._equity_start = self._get_equity()
            self._equity_live = self._equity_start
            self.daily_volume = 0.0
            self.daily_pnl = 0.0
            self.trade_count = 0
            self.win_count = 0
            self.loss_count = 0
            self.consecutive_losses = 0
            logger.info(
                f"New day: {today} | Equity: ${self._equity_start:.2f} | "
                f"Volume target: ${self.config.daily_volume_target:,.0f}"
            )

    # ──── TRADE GATE ──────────────────────────────────────

    def _can_trade(self) -> tuple[bool, str]:
        equity = self._get_equity()

        if equity < self.config.min_equity_usd:
            return False, f"Equity too low: ${equity:.2f}"

        if self.daily_volume >= self.config.daily_volume_target:
            return False, f"Volume target reached: ${self.daily_volume:,.0f}"

        if self._equity_start > 0:
            max_loss = self._equity_start * self.config.max_daily_loss_pct
            if -self.daily_pnl >= max_loss:
                return False, f"Daily loss limit: ${self.daily_pnl:.2f}"

        if self.consecutive_losses >= self.config.max_consecutive_losses:
            return False, f"Consecutive losses: {self.consecutive_losses}"

        return True, "OK"

    # ──── CORE CYCLE ──────────────────────────────────────

    def _run_cycle(self):
        """
        One ping-pong cycle.
        If _next_is_long=True:  BUY entry -> SELL exit
        If _next_is_long=False: SELL entry -> BUY exit (short cycle)
        """
        mid = self._get_mid_price()

        # Check book liquidity
        spread_pct = self._get_book_spread_pct()
        if spread_pct > self.config.max_market_spread_pct:
            logger.info(f"Book spread too wide ({spread_pct:.3f}%), skipping")
            return

        size = self._calc_size(mid)
        notional = size * mid

        if notional < self.config.min_notional_usd:
            logger.warning(f"Notional ${notional:.2f} < min ${self.config.min_notional_usd}")
            return

        is_long = self._next_is_long
        direction = "LONG" if is_long else "SHORT"
        entry_side = "buy" if is_long else "sell"
        exit_side = "sell" if is_long else "buy"

        # Entry price: slightly favorable side of mid
        offset_mult = (1 - self.config.entry_offset_pct / 100) if is_long \
            else (1 + self.config.entry_offset_pct / 100)
        entry_price = mid * offset_mult

        # Exit price: entry + spread (long) or entry - spread (short)
        spread_mult = (1 + self.config.spread_pct / 100) if is_long \
            else (1 - self.config.spread_pct / 100)

        # Stop price: entry - stop (long) or entry + stop (short)
        stop_mult = (1 - self.config.stop_loss_pct / 100) if is_long \
            else (1 + self.config.stop_loss_pct / 100)

        logger.info(
            f"Cycle #{self.trade_count + 1} {direction} | "
            f"Mid: ${mid:,.2f} | Entry: ${entry_price:,.2f} | "
            f"Size: {size} ({notional:,.0f} USD)"
        )

        # ── PHASE 1: ENTRY ────────────────────────────────
        try:
            entry_order = self._place_limit(entry_side, entry_price, size)
        except Exception as e:
            logger.error(f"Entry order failed: {e}")
            return

        entry_filled = False
        actual_entry = entry_price
        t0 = time.time()

        while time.time() - t0 < self.config.entry_timeout_sec:
            try:
                o = self._fetch_order(entry_order["id"])
            except Exception:
                time.sleep(1)
                continue
            if o["status"] == "closed":
                entry_filled = True
                actual_entry = float(o.get("average") or entry_price)
                break
            if o["status"] in ("canceled", "cancelled", "rejected"):
                logger.info("Entry order was rejected/cancelled by exchange")
                return
            time.sleep(self.config.poll_interval_sec)

        if not entry_filled:
            self._cancel(entry_order["id"])
            logger.info("Entry timeout, cancelled")
            return

        # Entry filled
        self.daily_volume += notional
        exit_price = actual_entry * spread_mult
        stop_price = actual_entry * stop_mult

        logger.info(
            f"Entry FILLED at ${actual_entry:,.2f} | "
            f"Target: ${exit_price:,.2f} | Stop: ${stop_price:,.2f}"
        )

        # ── PHASE 2: EXIT ─────────────────────────────────
        try:
            exit_order = self._place_limit(exit_side, exit_price, size)
        except Exception as e:
            logger.error(f"Exit order failed: {e}, closing at market")
            self._close_position()
            self.daily_volume += notional
            self.trade_count += 1
            return

        t0 = time.time()
        exit_filled = False

        while time.time() - t0 < self.config.position_timeout_sec:
            # Check exit order status
            try:
                o = self._fetch_order(exit_order["id"])
            except Exception:
                time.sleep(1)
                continue

            if o["status"] == "closed":
                exit_filled = True
                break

            # Check stop loss via mark price
            try:
                current_mid = self._get_mid_price()
            except Exception:
                time.sleep(1)
                continue

            # Stop condition
            stop_hit = (current_mid <= stop_price) if is_long \
                else (current_mid >= stop_price)

            if stop_hit:
                self._cancel(exit_order["id"])
                self._close_position()
                # Estimate loss: stop distance + taker fee on exit
                loss = notional * (self.config.stop_loss_pct / 100) + \
                    notional * self.config.taker_fee_rate + \
                    notional * self.config.maker_fee_rate
                self.daily_pnl -= loss
                self.daily_volume += notional
                self.trade_count += 1
                self.loss_count += 1
                self.consecutive_losses += 1
                logger.warning(
                    f"STOP HIT | Loss: ~${loss:.2f} | "
                    f"Daily PnL: ${self.daily_pnl:.2f} | "
                    f"Consecutive: {self.consecutive_losses}"
                )
                time.sleep(self.config.cooldown_after_loss_sec)
                self._next_is_long = not self._next_is_long
                return

            time.sleep(self.config.poll_interval_sec)

        if exit_filled:
            # Profitable exit (maker-maker)
            gross = notional * (self.config.spread_pct / 100)
            fees = notional * self.config.maker_fee_rate * 2
            net = gross - fees
            self.daily_pnl += net
            self.daily_volume += notional
            self.trade_count += 1
            self.win_count += 1
            self.consecutive_losses = 0
            logger.info(
                f"EXIT FILLED | Net: ${net:.4f} | "
                f"Daily vol: ${self.daily_volume:,.0f} | "
                f"PnL: ${self.daily_pnl:.2f} | "
                f"W/L: {self.win_count}/{self.loss_count}"
            )
        else:
            # Position timeout
            self._cancel(exit_order["id"])
            self._close_position()
            # Timeout exit is roughly breakeven (taker fee on close)
            timeout_cost = notional * self.config.taker_fee_rate + \
                notional * self.config.maker_fee_rate
            self.daily_pnl -= timeout_cost
            self.daily_volume += notional
            self.trade_count += 1
            logger.info(
                f"TIMEOUT | Closed at market | Cost: ~${timeout_cost:.2f} | "
                f"Daily vol: ${self.daily_volume:,.0f}"
            )

        # Alternate direction for next cycle
        self._next_is_long = not self._next_is_long

    # ──── MAIN LOOP ───────────────────────────────────────

    def run(self):
        self.running = True

        # Graceful shutdown
        def _handle_signal(sig, frame):
            logger.info("Shutdown signal received")
            self.running = False
        signal.signal(signal.SIGINT, _handle_signal)
        signal.signal(signal.SIGTERM, _handle_signal)

        # Setup
        self._setup_leverage()
        self._equity_live = self._get_equity()
        self._reset_day_if_needed()

        logger.info(
            f"Volume Gen Bot STARTED | "
            f"Symbol: {self.config.symbol} | "
            f"Equity: ${self._equity_live:.2f} | "
            f"Leverage: {self.config.leverage}x | "
            f"Max position: ${self._equity_live * self.config.leverage * self.config.position_utilization:,.0f} | "
            f"Daily target: ${self.config.daily_volume_target:,.0f} | "
            f"Spread: {self.config.spread_pct:.3f}%"
        )

        if self._equity_live < 50:
            logger.warning(
                f"Very low equity (${self._equity_live:.2f}). "
                f"Risk per trade will be small. Bot will still run."
            )

        # Main loop
        while self.running:
            self._reset_day_if_needed()

            can, reason = self._can_trade()
            if not can:
                logger.info(f"Paused: {reason}")
                # If volume target met, wait for new day
                if "Volume target" in reason:
                    logger.info("Target reached for today. Sleeping until next UTC day.")
                    time.sleep(300)  # Check every 5 min
                else:
                    time.sleep(60)
                continue

            try:
                self._run_cycle()
            except KeyboardInterrupt:
                break
            except Exception as e:
                logger.error(f"Cycle error: {e}", exc_info=True)
                self._close_position()
                time.sleep(10)

            time.sleep(self.config.cycle_pause_sec)

        # Shutdown
        logger.info("Shutting down... closing any open positions")
        self._close_position()
        logger.info(
            f"Final stats | Volume: ${self.daily_volume:,.0f} | "
            f"PnL: ${self.daily_pnl:.2f} | "
            f"Trades: {self.trade_count} (W:{self.win_count} L:{self.loss_count})"
        )


# ──────────────────────────────────────────────────────────
# ENTRY POINT
# ──────────────────────────────────────────────────────────

def main():
    api_key = os.getenv("BYBIT_API_KEY", "")
    secret = os.getenv("BYBIT_SECRET", "")

    if not api_key or not secret:
        logger.error("Set BYBIT_API_KEY and BYBIT_SECRET env vars")
        sys.exit(1)

    config = BotConfig(
        symbol=os.getenv("BOT_SYMBOL", "BTC/USDT:USDT"),
        leverage=int(os.getenv("BOT_LEVERAGE", "30")),
        daily_volume_target=float(os.getenv("BOT_VOLUME_TARGET", "100000")),
        spread_pct=float(os.getenv("BOT_SPREAD_PCT", "0.05")),
        stop_loss_pct=float(os.getenv("BOT_STOP_LOSS_PCT", "0.10")),
        position_utilization=float(os.getenv("BOT_UTILIZATION", "0.60")),
    )

    bot = VolumeGenBot(config, api_key, secret)
    bot.run()


if __name__ == "__main__":
    main()
