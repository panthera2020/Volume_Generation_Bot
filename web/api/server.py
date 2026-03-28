"""
Volume Generation Bot — Web API
FastAPI server with control endpoints and static frontend serving.
"""
from __future__ import annotations

import os
import logging
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.responses import RedirectResponse

from volume_gen_bot import BotConfig, VolumeGenBot

logger = logging.getLogger(__name__)

app = FastAPI(title="Volume Generation Bot", version="1.0")

# ---------- Static frontend ----------
frontend_dir = Path(__file__).resolve().parents[1] / "frontend"
app.mount("/app", StaticFiles(directory=str(frontend_dir), html=True), name="frontend")


@app.get("/")
def root() -> RedirectResponse:
    return RedirectResponse(url="/app")


# ---------- Models ----------
class BotActionResponse(BaseModel):
    status: str


class StartBotRequest(BaseModel):
    symbol: str | None = None
    leverage: int | None = None
    volume_target: float | None = None


# ---------- Bot init ----------
api_key = os.getenv("BYBIT_API_KEY", "")
api_secret = os.getenv("BYBIT_SECRET", "")

if not api_key or not api_secret:
    logger.error(
        "BYBIT_API_KEY/BYBIT_SECRET not set. "
        "Bot will not connect. Set env vars and restart."
    )
    bot = None
else:
    cfg = BotConfig(
        symbol=os.getenv("BOT_SYMBOL", "BTC/USDT:USDT"),
        leverage=int(os.getenv("BOT_LEVERAGE", "30")),
        daily_volume_target=float(os.getenv("BOT_VOLUME_TARGET", "100000")),
        spread_pct=float(os.getenv("BOT_SPREAD_PCT", "0.05")),
        stop_loss_pct=float(os.getenv("BOT_STOP_LOSS_PCT", "0.10")),
        max_daily_loss_pct=float(os.getenv("BOT_MAX_DD_PCT", "0.03")),
        max_consecutive_losses=int(os.getenv("BOT_MAX_CONSEC_LOSSES", "5")),
        cooldown_after_loss_sec=int(os.getenv("BOT_COOLDOWN", "30")),
        entry_timeout_sec=int(os.getenv("BOT_ENTRY_TIMEOUT", "90")),
        position_timeout_sec=int(os.getenv("BOT_POS_TIMEOUT", "180")),
        position_utilization=float(os.getenv("BOT_UTILIZATION", "0.60")),
    )
    bot = VolumeGenBot(cfg, api_key, api_secret)


def _require_bot() -> VolumeGenBot:
    if bot is None:
        raise HTTPException(
            status_code=503,
            detail="Bot not configured. Set BYBIT_API_KEY and BYBIT_SECRET.",
        )
    return bot


# ---------- Endpoints ----------
@app.post("/bot/start", response_model=BotActionResponse)
def start_bot(payload: StartBotRequest) -> BotActionResponse:
    b = _require_bot()
    if payload.symbol:
        b.config.symbol = payload.symbol
    if payload.leverage:
        b.config.leverage = payload.leverage
    if payload.volume_target:
        b.config.daily_volume_target = payload.volume_target
    b.run()
    return BotActionResponse(status="ok")


@app.post("/bot/stop", response_model=BotActionResponse)
def stop_bot() -> BotActionResponse:
    b = _require_bot()
    b.running = False
    return BotActionResponse(status="ok")


@app.post("/bot/pause", response_model=BotActionResponse)
def pause_bot() -> BotActionResponse:
    b = _require_bot()
    b.running = False
    return BotActionResponse(status="ok")


@app.post("/bot/resume", response_model=BotActionResponse)
def resume_bot() -> BotActionResponse:
    b = _require_bot()
    b.run()
    return BotActionResponse(status="ok")


@app.get("/bot/status")
def bot_status() -> dict:
    if bot is None:
        return {
            "state": "not_configured",
            "last_error": "BYBIT_API_KEY/BYBIT_SECRET not set",
        }

    return {
        "state": "RUNNING" if bot.running else "STOPPED",
        "symbol": bot.config.symbol,
        "leverage": bot.config.leverage,
        "equity": bot._equity_live,
        "daily_volume": bot.daily_volume,
        "daily_volume_target": bot.config.daily_volume_target,
        "volume_pct": round(
            bot.daily_volume / bot.config.daily_volume_target * 100, 1
        ) if bot.config.daily_volume_target else 0,
        "round_trips": bot.trade_count,
        "total_fees": round(bot.daily_volume * bot.config.maker_fee_rate * 2, 4),
        "consecutive_losses": bot.consecutive_losses,
        "daily_pnl": round(bot.daily_pnl, 4),
        "spread_pct": bot.config.spread_pct,
        "stop_loss_pct": bot.config.stop_loss_pct,
        "last_direction": "LONG" if bot._next_is_long else "SHORT",
        "win_count": bot.win_count,
        "loss_count": bot.loss_count,
        "last_error": None,
    }
