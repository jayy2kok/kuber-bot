"""
FBot ORM Models — All database tables.

Models correspond to Phase 1 (P1.3) of the plan:
  Stock, StockPrice, Fundamental, BulkDeal, Recommendation,
  Trade, Holding, GTTOrder, NewsArticle
"""

from datetime import datetime, date
from enum import Enum as PyEnum
from typing import Optional

from sqlalchemy import (
    Boolean,
    Column,
    Date,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    """Base class for all ORM models."""
    pass


# ─── Enums ────────────────────────────────────────────────────────────────────


class SignalType(str, PyEnum):
    """Recommendation signal."""
    STRONG_BUY = "strong_buy"
    BUY = "buy"
    HOLD = "hold"
    SELL = "sell"
    STRONG_SELL = "strong_sell"


class HoldingPeriod(str, PyEnum):
    """Recommended holding duration."""
    MEDIUM_TERM = "medium_term"       # 3-6 months
    SUPER_LONG_TERM = "super_long"    # 1+ year


class OrderStatus(str, PyEnum):
    """Trade order lifecycle."""
    PENDING = "pending"
    PLACED = "placed"
    EXECUTED = "executed"
    CANCELLED = "cancelled"
    REJECTED = "rejected"
    FAILED = "failed"


class TradeSide(str, PyEnum):
    """Buy or sell."""
    BUY = "buy"
    SELL = "sell"


class DealCategory(str, PyEnum):
    """Institutional classification for bulk/block deals."""
    FII = "fii"
    DII = "dii"
    OTHER = "other"


class GTTStatus(str, PyEnum):
    """GTT order state."""
    ACTIVE = "active"
    TRIGGERED = "triggered"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


# ─── Stock Master ─────────────────────────────────────────────────────────────


class Stock(Base):
    """Master stock list — one row per NSE symbol."""

    __tablename__ = "stocks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(30), unique=True, nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    industry: Mapped[str] = mapped_column(String(100), nullable=False, default="Unknown")
    sector: Mapped[str] = mapped_column(String(100), nullable=False, default="Unknown")
    market_cap_cr: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    yahoo_symbol: Mapped[str] = mapped_column(String(30), nullable=False)

    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )

    # Relationships
    prices: Mapped[list["StockPrice"]] = relationship(back_populates="stock", lazy="selectin")
    fundamentals: Mapped[list["Fundamental"]] = relationship(
        back_populates="stock", lazy="selectin"
    )
    recommendations: Mapped[list["Recommendation"]] = relationship(
        back_populates="stock", lazy="selectin"
    )

    def __repr__(self) -> str:
        return f"<Stock {self.symbol} ({self.name})>"


# ─── Stock Price (OHLCV) ─────────────────────────────────────────────────────


class StockPrice(Base):
    """Daily OHLCV price data."""

    __tablename__ = "stock_prices"
    __table_args__ = (
        UniqueConstraint("stock_id", "date", name="uq_stock_price_date"),
        Index("ix_stock_prices_date", "date"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    stock_id: Mapped[int] = mapped_column(Integer, ForeignKey("stocks.id"), nullable=False)
    date: Mapped[date] = mapped_column(Date, nullable=False)
    open: Mapped[float] = mapped_column(Float, nullable=False)
    high: Mapped[float] = mapped_column(Float, nullable=False)
    low: Mapped[float] = mapped_column(Float, nullable=False)
    close: Mapped[float] = mapped_column(Float, nullable=False)
    volume: Mapped[int] = mapped_column(Integer, nullable=False)
    adj_close: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    stock: Mapped["Stock"] = relationship(back_populates="prices")


# ─── Fundamentals ─────────────────────────────────────────────────────────────


class Fundamental(Base):
    """Quarterly/annual fundamental snapshot per stock."""

    __tablename__ = "fundamentals"
    __table_args__ = (
        UniqueConstraint("stock_id", "as_of_date", name="uq_fundamental_date"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    stock_id: Mapped[int] = mapped_column(Integer, ForeignKey("stocks.id"), nullable=False)
    as_of_date: Mapped[date] = mapped_column(Date, nullable=False)

    # Core metrics (from Plan — Section 5A.1 GARP scanner)
    pe_ratio: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    forward_pe: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    trailing_eps: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    forward_eps: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    eps_growth_pct: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    roe_pct: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    roce_pct: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    debt_to_equity: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    revenue_growth_pct: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    market_cap_cr: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    book_value: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    pb_ratio: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    dividend_yield_pct: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    # Ownership
    promoter_holding_pct: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    fii_holding_pct: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    dii_holding_pct: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    # Industry context
    industry_pe_median: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    stock: Mapped["Stock"] = relationship(back_populates="fundamentals")


# ─── Bulk/Block Deals ─────────────────────────────────────────────────────────


class BulkDeal(Base):
    """Large deal records from NSE (bulk + block deals)."""

    __tablename__ = "bulk_deals"
    __table_args__ = (
        Index("ix_bulk_deals_date", "deal_date"),
        Index("ix_bulk_deals_symbol", "symbol"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(30), nullable=False)
    company_name: Mapped[str] = mapped_column(String(200), nullable=True)
    client_name: Mapped[str] = mapped_column(String(300), nullable=False)
    deal_type: Mapped[str] = mapped_column(String(10), nullable=False)  # BULK or BLOCK
    buy_sell: Mapped[str] = mapped_column(String(10), nullable=False)   # BUY or SELL
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    price: Mapped[float] = mapped_column(Float, nullable=False)
    value_cr: Mapped[float] = mapped_column(Float, nullable=False)
    category: Mapped[str] = mapped_column(
        Enum(DealCategory), nullable=False, default=DealCategory.OTHER
    )
    deal_date: Mapped[date] = mapped_column(Date, nullable=False)

    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


# ─── Recommendations ─────────────────────────────────────────────────────────


class Recommendation(Base):
    """Generated trade recommendations."""

    __tablename__ = "recommendations"
    __table_args__ = (
        Index("ix_recommendations_date", "scan_date"),
        Index("ix_recommendations_signal", "signal"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    stock_id: Mapped[int] = mapped_column(Integer, ForeignKey("stocks.id"), nullable=False)
    scan_date: Mapped[date] = mapped_column(Date, nullable=False)

    # Scores (0-100 scale)
    fundamental_score: Mapped[float] = mapped_column(Float, nullable=False, default=0)
    technical_score: Mapped[float] = mapped_column(Float, nullable=False, default=0)
    institutional_score: Mapped[float] = mapped_column(Float, nullable=False, default=0)
    sentiment_score: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    composite_score: Mapped[float] = mapped_column(Float, nullable=False, default=0)

    # Signal & classification
    signal: Mapped[str] = mapped_column(Enum(SignalType), nullable=False)
    holding_period: Mapped[str] = mapped_column(
        Enum(HoldingPeriod), nullable=False, default=HoldingPeriod.MEDIUM_TERM
    )

    # Prices
    cmp: Mapped[float] = mapped_column(Float, nullable=False)
    entry_price: Mapped[float] = mapped_column(Float, nullable=False)
    target_1: Mapped[float] = mapped_column(Float, nullable=False)
    target_2: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    stop_loss: Mapped[float] = mapped_column(Float, nullable=False)
    risk_reward: Mapped[float] = mapped_column(Float, nullable=False, default=0)

    # Position
    suggested_qty: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    suggested_value: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    # Rationale
    rationale: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # Delivery tracking
    is_delivered: Mapped[bool] = mapped_column(Boolean, default=False)
    is_approved: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    delivered_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    approved_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    stock: Mapped["Stock"] = relationship(back_populates="recommendations")

    def __repr__(self) -> str:
        return f"<Recommendation {self.stock_id} {self.signal} score={self.composite_score}>"


# ─── Trades ───────────────────────────────────────────────────────────────────


class Trade(Base):
    """Executed or paper trades."""

    __tablename__ = "trades"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    recommendation_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("recommendations.id"), nullable=True
    )
    symbol: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    side: Mapped[str] = mapped_column(Enum(TradeSide), nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    price: Mapped[float] = mapped_column(Float, nullable=False)
    order_id: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    status: Mapped[str] = mapped_column(
        Enum(OrderStatus), nullable=False, default=OrderStatus.PENDING
    )
    is_paper: Mapped[bool] = mapped_column(Boolean, default=True)

    executed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


# ─── Holdings ─────────────────────────────────────────────────────────────────


class Holding(Base):
    """Current portfolio holdings (synced from Zerodha or paper portfolio)."""

    __tablename__ = "holdings"
    __table_args__ = (
        UniqueConstraint("symbol", "is_paper", name="uq_holding_symbol_mode"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    average_price: Mapped[float] = mapped_column(Float, nullable=False)
    last_price: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    pnl: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    pnl_pct: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    is_paper: Mapped[bool] = mapped_column(Boolean, default=True)
    # Origin of this holding — e.g. "zerodha", "paper"
    source: Mapped[Optional[str]] = mapped_column(String(50), nullable=True, default="zerodha")

    synced_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )


# ─── GTT Orders ───────────────────────────────────────────────────────────────


class GTTOrder(Base):
    """Good Till Triggered orders (target + stop-loss)."""

    __tablename__ = "gtt_orders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    trade_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("trades.id"), nullable=True
    )
    symbol: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    trigger_type: Mapped[str] = mapped_column(
        String(20), nullable=False
    )  # "target" or "stoploss"
    trigger_price: Mapped[float] = mapped_column(Float, nullable=False)
    order_quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    kite_gtt_id: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    status: Mapped[str] = mapped_column(
        Enum(GTTStatus), nullable=False, default=GTTStatus.ACTIVE
    )
    is_paper: Mapped[bool] = mapped_column(Boolean, default=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )


# ─── News Articles ────────────────────────────────────────────────────────────


class NewsArticle(Base):
    """Scraped news with optional sentiment score."""

    __tablename__ = "news_articles"
    __table_args__ = (
        Index("ix_news_published", "published_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    url: Mapped[str] = mapped_column(String(1000), nullable=False, unique=True)
    source: Mapped[str] = mapped_column(String(100), nullable=False)
    summary: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    published_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    # Linked stock (if resolved)
    symbol: Mapped[Optional[str]] = mapped_column(String(30), nullable=True, index=True)

    # Sentiment (-100 to +100)
    sentiment_score: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    sentiment_label: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
