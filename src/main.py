"""
FBot — Application Entry Point.

Starts the FastAPI server with:
  - Database initialization
  - Health check endpoint
  - Graceful shutdown
"""

import asyncio
import logging
import sys

import structlog
import uvicorn
from fastapi import FastAPI, Query
from fastapi.responses import RedirectResponse
from contextlib import asynccontextmanager

from src.config import get_settings
from src.db.engine import init_db, close_db

# Module-level instances
_scheduler = None
_telegram_app = None

# ─── Logging Setup ────────────────────────────────────────────────────────────

settings = get_settings()

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    stream=sys.stdout,
)

structlog.configure(
    processors=[
        structlog.stdlib.filter_by_level,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.dev.ConsoleRenderer(),
    ],
    wrapper_class=structlog.stdlib.BoundLogger,
    context_class=dict,
    logger_factory=structlog.stdlib.LoggerFactory(),
)

# Suppress noisy third-party loggers
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

class HealthCheckFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if record.getMessage().find("GET /health") != -1:
            record.levelno = logging.DEBUG
            record.levelname = "DEBUG"
        return True

logging.getLogger("uvicorn.access").addFilter(HealthCheckFilter())

logger = logging.getLogger(__name__)


# ─── Application Lifecycle ────────────────────────────────────────────────────


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application startup and shutdown logic."""
    # Startup
    logger.info("=" * 60)
    logger.info("  📈 FBot — Indian Stock Market Scanner & Trading Bot")
    logger.info(f"  Mode: {settings.trading_mode.value.upper()}")
    logger.info(f"  Universe: {settings.scan_universe}")
    logger.info(f"  Daily buy limit: {settings.daily_buy_limit}")
    logger.info(f"  Telegram: {'✅' if settings.has_telegram else '❌ not configured'}")
    logger.info(f"  Zerodha: {'✅' if settings.has_kite else '❌ not configured'}")
    logger.info(f"  Ollama: {'✅' if settings.has_ollama else '❌ not configured'}")
    logger.info("=" * 60)

    # Initialize database tables
    logger.info("Initializing database...")
    await init_db()
    logger.info("Database ready ✅")

    # Sync stock universe
    from src.data.stock_universe import sync_stock_universe

    new_stocks = await sync_stock_universe()
    if new_stocks:
        logger.info(f"Added {new_stocks} new stocks to universe")

    # Start scheduler
    global _scheduler
    try:
        from apscheduler.schedulers.asyncio import AsyncIOScheduler
        from src.scheduler.jobs import register_jobs

        _scheduler = AsyncIOScheduler()
        register_jobs(_scheduler)
        _scheduler.start()
        logger.info("Scheduler started ✅")
    except Exception as e:
        logger.warning(f"Scheduler failed to start: {e}")

    # Start Telegram bot
    global _telegram_app
    try:
        from src.bot.handler import create_bot_application

        _telegram_app = create_bot_application()
        if _telegram_app:
            await _telegram_app.initialize()
            await _telegram_app.start()
            await _telegram_app.updater.start_polling(drop_pending_updates=True)
            logger.info("Telegram bot started ✅")
    except Exception as e:
        logger.warning(f"Telegram bot failed to start: {e}")

    yield

    # Shutdown
    if _telegram_app:
        try:
            await _telegram_app.updater.stop()
            await _telegram_app.stop()
            await _telegram_app.shutdown()
            logger.info("Telegram bot stopped")
        except Exception:
            pass
    if _scheduler and _scheduler.running:
        _scheduler.shutdown(wait=False)
        logger.info("Scheduler stopped")
    logger.info("Shutting down FBot...")
    await close_db()
    logger.info("Database connections closed. Goodbye! 👋")


# ─── FastAPI App ──────────────────────────────────────────────────────────────

app = FastAPI(
    title="FBot",
    description="Indian Stock Market Scanner & Trading Bot",
    version="0.1.0",
    lifespan=lifespan,
)


@app.get("/health")
async def health_check():
    """Health check endpoint for Docker / monitoring."""
    return {
        "status": "healthy",
        "mode": settings.trading_mode.value,
        "universe": settings.scan_universe,
        "telegram": settings.has_telegram,
        "kite": settings.has_kite,
    }


@app.get("/")
async def root():
    """Root endpoint."""
    return {
        "app": "FBot",
        "version": "0.1.0",
        "docs": "/docs",
    }


# ─── Kite Connect OAuth Endpoints ────────────────────────────────────────────


@app.get("/kite/login")
async def kite_login():
    """Redirect to Zerodha login page for OAuth2 authentication."""
    if not settings.has_kite:
        return {"error": "Kite Connect not configured"}
    from src.trading.kite_client import get_login_url
    return RedirectResponse(url=get_login_url())


@app.get("/kite/callback")
async def kite_callback(request_token: str = Query(...), status: str = Query(default="")):
    """
    OAuth2 callback — exchange request_token for access_token.

    After logging into Zerodha, you are redirected here with ?request_token=xxx&status=success.
    Automatically notifies Telegram and syncs holdings.
    """
    from fastapi.responses import HTMLResponse

    if status != "success":
        return {"error": "Login was not successful", "status": status}

    try:
        from src.trading.kite_client import generate_session, sync_holdings_from_kite, get_portfolio_value
        session_data = await generate_session(request_token)
        user_name = session_data.get("user_name", "User")

        # Auto-sync holdings after login
        synced = await sync_holdings_from_kite()
        portfolio = await get_portfolio_value() if synced > 0 else None

        # Auto-notify via Telegram
        if settings.has_telegram:
            try:
                from telegram import Bot
                bot = Bot(token=settings.telegram_bot_token)

                if portfolio and synced > 0:
                    msg = (
                        f"✅ *Zerodha Login Successful!*\n"
                        f"━━━━━━━━━━━━━━━━━━━━━━\n"
                        f"👤 User: {user_name}\n"
                        f"📂 Holdings synced: {portfolio['holdings_count']} stocks\n"
                        f"💰 Portfolio Value: ₹{portfolio['total_value']:,.2f}\n"
                        f"📊 Total P&L: ₹{portfolio['total_pnl']:+,.2f} "
                        f"({portfolio['pnl_pct']:+.1f}%)\n"
                        f"━━━━━━━━━━━━━━━━━━━━━━\n"
                        f"🟢 FBot is now fully operational for today."
                    )
                else:
                    msg = (
                        f"✅ *Zerodha Login Successful!*\n"
                        f"━━━━━━━━━━━━━━━━━━━━━━\n"
                        f"👤 User: {user_name}\n"
                        f"📂 No holdings found.\n"
                        f"━━━━━━━━━━━━━━━━━━━━━━\n"
                        f"🟢 FBot is now fully operational for today."
                    )

                await bot.send_message(
                    chat_id=settings.telegram_chat_id,
                    text=msg,
                    parse_mode="Markdown",
                )
            except Exception as e:
                logger.warning(f"Failed to send Telegram login notification: {e}")

        # Return a nice auto-closing HTML page
        return HTMLResponse(content=f"""
        <!DOCTYPE html>
        <html>
        <head>
            <title>FBot — Authenticated</title>
            <style>
                body {{ font-family: -apple-system, BlinkMacSystemFont, sans-serif;
                       display: flex; justify-content: center; align-items: center;
                       min-height: 100vh; margin: 0; background: #0d1117; color: #c9d1d9; }}
                .card {{ text-align: center; padding: 40px; border-radius: 16px;
                        background: #161b22; border: 1px solid #30363d; max-width: 400px; }}
                .check {{ font-size: 64px; margin-bottom: 16px; }}
                h1 {{ color: #58a6ff; font-size: 22px; margin: 8px 0; }}
                p {{ color: #8b949e; font-size: 14px; }}
                .holdings {{ color: #3fb950; font-weight: 600; font-size: 16px; margin: 12px 0; }}
            </style>
        </head>
        <body>
            <div class="card">
                <div class="check">✅</div>
                <h1>Welcome, {user_name}!</h1>
                <p>Kite Connect authenticated successfully.</p>
                <div class="holdings">Holdings synced: {synced if synced > 0 else 0} stocks</div>
                <p>Check Telegram for details.<br>You can close this tab.</p>
            </div>
        </body>
        </html>
        """)

    except Exception as e:
        logger.error(f"Kite callback failed: {e}")
        return {"error": str(e)}


# ─── CLI Entry Point ─────────────────────────────────────────────────────────

if __name__ == "__main__":
    uvicorn.run(
        "src.main:app",
        host=settings.host,
        port=settings.port,
        reload=False,
        log_level=settings.log_level.lower(),
    )
