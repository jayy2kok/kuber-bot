"""
Telegram message formatters — rich recommendation cards.

Reference: Plan.md P4.2

Generates formatted Telegram messages with:
  - Recommendation cards with emoji-coded signals
  - Daily digest summary
  - Trade execution confirmations
  - Error alerts
"""

from datetime import date
from typing import Optional

from src.db.models import Recommendation, Holding, SignalType, HoldingPeriod


# ─── Signal Emoji Mapping ────────────────────────────────────────────────────

SIGNAL_EMOJI = {
    SignalType.STRONG_BUY: "🟢🟢",
    SignalType.BUY: "🟢",
    SignalType.HOLD: "🟡",
    SignalType.SELL: "🔴",
    SignalType.STRONG_SELL: "🔴🔴",
}

SIGNAL_LABEL = {
    SignalType.STRONG_BUY: "STRONG BUY",
    SignalType.BUY: "BUY",
    SignalType.HOLD: "HOLD",
    SignalType.SELL: "SELL",
    SignalType.STRONG_SELL: "STRONG SELL",
}


def _classify_risk_reward(rr: float) -> tuple[str, str]:
    """
    Classify Risk/Reward ratio into a human-readable label and emoji.

    R:R Ranges:
      ≥ 2.0  → Excellent  (you stand to gain 2× what you risk)
      ≥ 1.5  → Good       (reward clearly outweighs risk)
      ≥ 1.0  → Fair       (reward equals risk — borderline)
      ≥ 0.5  → Poor       (risking more than you gain)
      < 0.5  → Avoid      (risk far outweighs reward)

    Returns: (label, emoji)
    """
    if rr >= 2.0:
        return "Excellent ✨", "⚖️"
    elif rr >= 1.5:
        return "Good 👍", "⚖️"
    elif rr >= 1.0:
        return "Fair ⚠️", "⚖️"
    elif rr >= 0.5:
        return "Poor 👎", "⚠️"
    else:
        return "Avoid ❌", "🚫"


def _get_title(article) -> str:
    """Extract title from an article (ORM object or dict)."""
    if hasattr(article, 'title'):
        return article.title or ""
    if isinstance(article, dict):
        return article.get('title', '')
    return str(article)


def _get_source(article) -> str:
    """Extract source from an article (ORM object or dict)."""
    if hasattr(article, 'source'):
        return article.source or ""
    if isinstance(article, dict):
        return article.get('source', '')
    return ""


def format_recommendation_card(
    rec: Recommendation, stock_symbol: str, stock_name: str,
    institutional_deals: list | None = None,
    news_articles: list | None = None,
) -> str:
    """
    Format a single recommendation as a Telegram message card.

    Example output:
    ━━━━━━━━━━━━━━━━━━━━━━
    🟢🟢 STRONG BUY — RELIANCE
    Reliance Industries Ltd.
    ━━━━━━━━━━━━━━━━━━━━━━
    💰 CMP: ₹2,450.00
    📍 Entry: ₹2,420.00
    🎯 Target 1: ₹2,904.00 (+20%)
    🎯 Target 2: ₹3,267.00 (+35%)
    🛑 Stop Loss: ₹2,057.00
    ⚖️ Risk/Reward: 1:2.40 — Excellent ✨

    📊 Score: 82/100
    📈 Fund: 75 | Tech: 87 | Inst: 70
    🕐 Horizon: Medium Term

    📰 Recent News:
      • Reliance reports record Q4 profit
      • Jio subscriber base crosses 500M

    🤖 AI Analysis:
    ...
    ━━━━━━━━━━━━━━━━━━━━━━
    """
    emoji = SIGNAL_EMOJI.get(rec.signal, "⚪")
    label = SIGNAL_LABEL.get(rec.signal, "UNKNOWN")
    horizon = "Super Long Term (1Y+)" if rec.holding_period == HoldingPeriod.SUPER_LONG_TERM else "Medium Term (3-6M)"

    is_sell = rec.signal in (SignalType.SELL, SignalType.STRONG_SELL)

    lines = [
        "━━━━━━━━━━━━━━━━━━━━━━",
        f"{emoji} {label} — {stock_symbol}",
        f"{stock_name}",
        "━━━━━━━━━━━━━━━━━━━━━━",
        f"💰 CMP: ₹{rec.cmp:,.2f}",
    ]

    if is_sell:
        # Sell cards: no buy-oriented targets
        lines.extend([
            "",
            "⚠️ Action: Consider exiting position",
        ])
    else:
        # Buy/Hold cards: show entry, targets, SL, R:R
        t1_pct = ((rec.target_1 - rec.entry_price) / rec.entry_price * 100) if rec.entry_price else 0
        t2_pct = ((rec.target_2 - rec.entry_price) / rec.entry_price * 100) if rec.entry_price and rec.target_2 else 0
        sl_pct = ((rec.stop_loss - rec.entry_price) / rec.entry_price * 100) if rec.entry_price else 0
        lines.extend([
            f"📍 Entry: ₹{rec.entry_price:,.2f}",
            f"🎯 Target 1: ₹{rec.target_1:,.2f} ({t1_pct:+.0f}%)",
        ])
        if rec.target_2:
            lines.append(f"🎯 Target 2: ₹{rec.target_2:,.2f} ({t2_pct:+.0f}%)")

        # Risk/Reward with clear guidance
        risk_per_share = abs(rec.entry_price - rec.stop_loss) if rec.entry_price and rec.stop_loss else 0
        reward_per_share = abs(rec.target_1 - rec.entry_price) if rec.entry_price else 0
        rr_label, rr_icon = _classify_risk_reward(rec.risk_reward)

        lines.extend([
            f"🛑 Stop Loss: ₹{rec.stop_loss:,.2f} ({sl_pct:+.0f}%)",
            f"{rr_icon} Risk/Reward: 1:{rec.risk_reward:.2f} — {rr_label}",
            f"   ↕️ Risk ₹{risk_per_share:,.0f} → Reward ₹{reward_per_share:,.0f} per share",
        ])

    lines.extend([
        "",
        f"📊 Score: {rec.composite_score:.0f}/100",
        f"📈 Fund: {rec.fundamental_score:.0f} | Tech: {rec.technical_score:.0f} | Inst: {rec.institutional_score:.0f}",
        f"🕐 Horizon: {horizon}",
    ])

    # Institutional activity section
    if institutional_deals:
        lines.append("")
        lines.append("🏦 Institutional Activity:")
        for deal in institutional_deals[:5]:  # Show max 5 deals
            side_icon = "🟢" if deal.buy_sell == "BUY" else "🔴"
            cat_label = deal.category.value.upper() if hasattr(deal.category, 'value') else str(deal.category).upper()
            # Shorten client name to keep the card readable
            client = deal.client_name[:35] + ".." if len(deal.client_name) > 35 else deal.client_name
            lines.append(
                f"  {side_icon} {cat_label} | {client}"
            )
            lines.append(
                f"      {deal.buy_sell} {deal.quantity:,} @ ₹{deal.price:,.0f} "
                f"(₹{deal.value_cr:.1f}Cr) — {deal.deal_date.strftime('%d %b')}"
            )

    # News headlines section — show actual headlines
    if news_articles:
        # Separate corporate actions from regular news
        corp_actions = [a for a in news_articles if _get_source(a) == "NSE Corporate Actions"]
        regular_news = [a for a in news_articles if _get_source(a) != "NSE Corporate Actions"]

        if regular_news:
            lines.append("")
            lines.append("📰 Recent News:")
            for article in regular_news[:4]:
                title = _get_title(article)
                # Truncate long titles
                if len(title) > 70:
                    title = title[:67] + "..."
                lines.append(f"  • {title}")

        if corp_actions:
            lines.append("")
            lines.append("🏢 Corporate Actions:")
            for ca in corp_actions[:3]:
                title = _get_title(ca)
                if len(title) > 70:
                    title = title[:67] + "..."
                lines.append(f"  • {title}")

    if rec.rationale:
        lines.append("")
        lines.append("🤖 AI Analysis:")
        lines.extend(_format_structured_rationale(rec.rationale))

    lines.append("━━━━━━━━━━━━━━━━━━━━━━")
    return "\n".join(lines)


def _format_structured_rationale(rationale: str) -> list[str]:
    """
    Format structured rationale into Telegram-friendly sections.

    Expected input format:
        FUNDAMENTAL:
        • bullet 1
        • bullet 2
        • bullet 3

        TECHNICAL:
        • bullet 1
        ...

    Falls back to raw lines for unstructured text.
    """
    # Map section headers to emoji labels
    section_map = {
        "FUNDAMENTAL": "📊 Fundamental:",
        "TECHNICAL": "📈 Technical:",
        "NEWS": "📰 News:",
    }

    lines_out: list[str] = []
    current_section = None
    has_sections = False

    for line in rationale.strip().splitlines():
        stripped = line.strip()
        if not stripped:
            continue

        # Check if this is a section header
        header_key = stripped.rstrip(":").upper()
        if header_key in section_map:
            if lines_out:  # Add spacing between sections
                lines_out.append("")
            lines_out.append(section_map[header_key])
            current_section = header_key
            has_sections = True
            continue

        # Bullet point under a section
        if current_section and (stripped.startswith("•") or stripped.startswith("-") or stripped.startswith("*")):
            # Clean up bullet and re-prefix
            bullet_text = stripped.lstrip("•-* ").strip()
            lines_out.append(f"  • {bullet_text}")
        elif current_section:
            # Non-bullet line under a section — treat as bullet
            lines_out.append(f"  • {stripped}")
        else:
            lines_out.append(stripped)

    # If no sections were found, fall back to simple display
    if not has_sections:
        return [f"💡 {rationale}"]

    return lines_out


def format_daily_digest_header(
    scan_date: date, buy_count: int, sell_count: int, total_scanned: int
) -> str:
    """Format the daily digest header message."""
    return (
        f"📊 *FBot Daily Digest — {scan_date.strftime('%d %b %Y')}*\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"Scanned: {total_scanned} stocks\n"
        f"🟢 Buy recommendations: {buy_count}\n"
        f"🔴 Sell alerts: {sell_count}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━"
    )


def format_trade_confirmation(
    symbol: str, side: str, qty: int, price: float, order_id: str, is_paper: bool
) -> str:
    """Format a trade execution confirmation."""
    mode = "📝 PAPER" if is_paper else "💵 LIVE"
    emoji = "🟢" if side.upper() == "BUY" else "🔴"
    return (
        f"{mode} Trade Executed\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"{emoji} {side.upper()} {symbol}\n"
        f"Qty: {qty} | Price: ₹{price:,.2f}\n"
        f"Value: ₹{qty * price:,.2f}\n"
        f"Order ID: {order_id}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━"
    )


def format_gtt_confirmation(
    symbol: str, trigger_type: str, trigger_price: float, qty: int
) -> str:
    """Format a GTT order set confirmation."""
    emoji = "🎯" if trigger_type == "target" else "🛑"
    label = "Target" if trigger_type == "target" else "Stop Loss"
    return (
        f"{emoji} GTT {label} Set\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"Stock: {symbol}\n"
        f"Trigger: ₹{trigger_price:,.2f}\n"
        f"Qty: {qty}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━"
    )


def format_portfolio_summary(
    holdings: list[Holding], total_value: float, total_pnl: float
) -> str:
    """Format the daily/weekly portfolio summary."""
    pnl_emoji = "📈" if total_pnl >= 0 else "📉"
    lines = [
        f"{pnl_emoji} *Portfolio Summary*",
        "━━━━━━━━━━━━━━━━━━━━━━",
        f"Total Value: ₹{total_value:,.2f}",
        f"Total P&L: ₹{total_pnl:+,.2f}",
        f"Holdings: {len(holdings)} stocks",
        "━━━━━━━━━━━━━━━━━━━━━━",
    ]

    for h in holdings[:15]:  # Show top 15
        pnl_icon = "🟢" if (h.pnl or 0) >= 0 else "🔴"
        pnl_pct = f"{h.pnl_pct:+.1f}%" if h.pnl_pct else "N/A"
        lines.append(f"{pnl_icon} {h.symbol}: ₹{h.last_price or 0:,.0f} ({pnl_pct})")

    if len(holdings) > 15:
        lines.append(f"... and {len(holdings) - 15} more")

    return "\n".join(lines)


def format_error_alert(error_type: str, message: str) -> str:
    """Format an error alert for the admin."""
    return (
        f"⚠️ *FBot Alert*\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"Type: {error_type}\n"
        f"Message: {message}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━"
    )
