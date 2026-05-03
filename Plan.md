# 📈 FBot — Indian Stock Market Scanner & Trading Bot

## 1. Project Overview

**FBot** is a personal automated stock market intelligence system that scans the **Nifty 500** universe on Indian stock markets (NSE/BSE), performs multi-dimensional analysis, and delivers actionable trade recommendations via a Telegram bot. The primary focus is **medium-term trades (3-6 months)**, with optional **"Super Long-term"** picks flagged for 1+ year horizons. Upon user approval, it executes trades automatically through the Zerodha Kite Connect API and sets up GTT (Good Till Triggered) orders for target prices.

### ✅ Finalized Design Decisions

| Decision | Choice |
|---|---|
| Stock Universe | **Nifty 500** (~500 stocks) |
| Holding Period | **3-6 months** (primary), **Super Long-term** tag for 1+ year |
| Budget/Sizing | Max **10% of current Zerodha holdings value** per stock |
| User Model | **Single user** — no auth/registration needed |
| Sentiment Analysis | **Optional** — uses Ollama (local) when available, system works without it |
| Trading Mode | **Paper trade first**, then switch to live |
| Notifications | **Daily digest** (9 AM IST) + **real-time alerts** on significant market events |
| Deployment | **Docker Compose** — multi-arch (amd64 + arm64), runs on Windows / Linux / Raspberry Pi |

---

## 2. High-Level Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│                         SCHEDULER (Cron Jobs)                       │
│   Daily: Fundamental Scan | Hourly: Technical Scan | Live: News     │
└───────────┬──────────────────────┬──────────────────────┬───────────┘
            │                      │                      │
            ▼                      ▼                      ▼
┌───────────────────┐  ┌───────────────────┐  ┌───────────────────────┐
│  Fundamental      │  │  Technical        │  │  News Sentiment       │
│  Analysis Engine  │  │  Analysis Engine  │  │  Analysis Engine      │
│  (P/E, EPS, ROE)  │  │  (MA, RSI, MACD)  │  │  (NLP / LLM based)   │
└───────────┬───────┘  └───────────┬───────┘  └───────────┬───────────┘
            │                      │                      │
            ▼                      ▼                      ▼
┌─────────────────────────────────────────────────────────────────────┐
│                    BULK DEAL MONITOR (NSE API)                      │
│         Institutional buying/selling activity tracker                │
└───────────────────────────────┬─────────────────────────────────────┘
                                │
                                ▼
┌─────────────────────────────────────────────────────────────────────┐
│                    RECOMMENDATION ENGINE                            │
│   Weighted scoring → Composite signal → Buy/Hold/Sell decision      │
│   Target price calculation │ Risk assessment │ Position sizing       │
└───────────────────────────────┬─────────────────────────────────────┘
                                │
                                ▼
┌─────────────────────────────────────────────────────────────────────┐
│                      TELEGRAM BOT INTERFACE                         │
│  Delivers recommendations │ Receives approval │ Status updates      │
└───────────────────────────────┬─────────────────────────────────────┘
                                │ (on approval)
                                ▼
┌─────────────────────────────────────────────────────────────────────┐
│                    ZERODHA KITE CONNECT INTEGRATION                  │
│   Place orders │ Set GTT orders │ Portfolio sync │ P&L tracking      │
└───────────────────────────────┬─────────────────────────────────────┘
                                │
                                ▼
┌─────────────────────────────────────────────────────────────────────┐
│               DATABASE (PostgreSQL — separate container)             │
│  Holdings │ Trade history │ Recommendations │ Watchlist │ Config     │
└─────────────────────────────────────────────────────────────────────┘
```

---

## 3. Tech Stack

| Layer             | Technology                  | Rationale                                                    |
| ----------------- | --------------------------- | ------------------------------------------------------------ |
| **Language**      | Python 3.12+                | Best ecosystem for financial analysis, NLP, and data science |
| **Web Framework** | FastAPI                     | Async support, lightweight, good for webhooks                |
| **Database**      | PostgreSQL 16               | Runs in separate Docker container, production-grade          |
| **ORM**           | SQLAlchemy 2.0 + asyncpg    | Mature, async PostgreSQL driver                              |
| **Scheduler**     | APScheduler                 | In-process job scheduling with cron-like syntax              |
| **Telegram**      | python-telegram-bot (v20+)  | Async, well-maintained, full Bot API support                 |
| **Trading**       | kiteconnect (Zerodha SDK)   | Official Python SDK for Kite Connect v3                      |
| **Data/Analysis** | pandas, numpy, ta (ta-lib)  | Industry standard for financial data analysis                |
| **NLP/Sentiment** | Ollama (local, optional)    | News sentiment classification — system works without it      |
| **HTTP Client**   | httpx (async)               | Async HTTP for NSE/news API calls                            |
| **Config**        | python-dotenv + pydantic    | Type-safe configuration management                           |
| **Containers**    | Docker + Docker Compose     | Machine-independent deployment, multi-arch (amd64 + arm64)   |
| **Base Image**    | `python:3.12-slim`          | Multi-arch official image, works on x86 and ARM/Raspberry Pi |

---

## 4. Data Sources

### 4.1 Stock Market Data
| Data Point            | Source                              | Endpoint / Method                          |
| --------------------- | ----------------------------------- | ------------------------------------------ |
| Live/Historical Price | Yahoo Finance (yfinance)            | `yfinance.download("RELIANCE.NS")`         |
| Fundamentals          | Yahoo Finance / Screener.in scrape  | P/E, EPS, ROE, Market Cap, Book Value      |
| Bulk/Block Deals      | NSE India API                       | `/api/snapshot-capital-market-largedeal`    |
| Delivery Data         | NSE India API                       | `/api/equity-stockIndices`                 |
| Corporate Actions     | NSE India API                       | Dividends, splits, bonuses                 |

### 4.2 News Data
| Source              | Method                         | Notes                                    |
| ------------------- | ------------------------------ | ---------------------------------------- |
| Google News RSS     | RSS feed parsing               | Free, real-time, no API key              |
| NewsAPI.org         | REST API                       | Free tier: 100 req/day                   |
| MoneyControl RSS    | RSS feed parsing               | Indian market focused                    |
| Economic Times RSS  | RSS feed parsing               | Indian market focused                    |

### 4.3 Institutional Activity
| Data Point            | Source     | Endpoint                                           |
| --------------------- | ---------- | -------------------------------------------------- |
| Large Deals           | NSE API    | `/api/snapshot-capital-market-largedeal`            |
| FII/DII Activity      | NSE API    | `/api/fiidiiTradeReact`                             |
| Promoter Holdings     | BSE/NSE    | Quarterly shareholding pattern                     |

---

## 5. Module Breakdown

### Phase 1: Foundation & Data Layer (Week 1-2)
> **Goal:** Project setup, database models, data fetching infrastructure

- [ ] **P1.1** Project scaffolding (directory structure, virtual env, dependencies)
- [ ] **P1.2** Configuration management (`.env`, Pydantic settings)
- [ ] **P1.3** Database models & migrations
  - `Stock` — master stock list (symbol, name, sector, market cap)
  - `StockPrice` — daily OHLCV data
  - `Fundamental` — quarterly fundamentals snapshot
  - `BulkDeal` — large deal records
  - `Recommendation` — generated recommendations
  - `Trade` — executed trades
  - `Holding` — current portfolio holdings
  - `GTTOrder` — active GTT orders
  - `NewsArticle` — scraped news with sentiment
- [ ] **P1.4** NSE API client (with rate limiting, session management, anti-bot headers)
- [ ] **P1.5** Stock universe builder (Nifty 500 as default universe)
- [ ] **P1.6** Historical data fetcher (yfinance integration)

### Phase 2: Analysis Engines (Week 3-4)
> **Goal:** Build the three core analysis pillars
> 
> ⚠️ **Reference:** Existing proven scanner logic in `external/long_term_scanner.py` and `external/fii_scanner.py` must be ported into the modular architecture below. See **Section 5A** for full logic breakdown.

- [ ] **P2.1** Fundamental Analysis Engine *(port from `long_term_scanner.get_fundamentals()`)*
  - Fetch & store: P/E, EPS (trailing + forward), ROE, Debt/Equity, Revenue Growth, Promoter Holding, FII/Institutional Holding, Market Cap
  - **Hard filters (gate checks):** ROE ≥ 10%, D/E ≤ 1.5, FII% > 0%, Market Cap ₹500Cr–₹15,000Cr
  - Industry-relative P/E valuation using per-sector median lookup table
  - EPS growth computed from trailing vs forward EPS (fallback to `earningsGrowth`)
  - Score stocks on value metrics (0-70 fundamental subscale — see Section 5A.1)
  - Peer comparison within sector
  - Growth trajectory analysis (QoQ, YoY)
- [ ] **P2.2** Technical Analysis Engine *(port from `long_term_scanner.get_technicals()`)*
  - Indicators: **EMA(50, 200)**, RSI(14), **ATR**, **Ichimoku Cloud**, 6M & 1Y returns
  - Also include for medium-term: SMA(50, 200), MACD, Bollinger Bands, ADX
  - **Hard filter:** Price must be above EMA200 (long-term uptrend)
  - Pattern detection: Golden Cross, Death Cross, Support/Resistance levels, Ichimoku cloud breakout
  - Trend scoring (0-30 technical subscale — see Section 5A.1)
  - Volume analysis and delivery percentage
  - **Entry/Target/SL Calculation** *(from `long_term_scanner.process_stock()`)*:
    - Entry: CMP or nearest EMA50 support (whichever is tighter)
    - Target 1: +20% from entry (conservative)
    - Target 2: +35% from entry (aggressive)
    - Stop Loss: max(EMA200 × 0.97, Entry × 0.85) — tighter of EMA200-based or 15% drawdown cap
    - Risk/Reward ratio computed from Entry–SL vs Entry–Target1
- [ ] **P2.3** News Sentiment Analysis Engine **(OPTIONAL MODULE)**
  - News aggregation from multiple RSS feeds
  - Sentiment classification using **Ollama** (local LLM) when available
  - Graceful degradation: scoring works without sentiment (weights auto-redistribute)
  - Entity extraction (map news to specific stocks)
  - Sentiment scoring per stock (-100 to +100)
  - Recency-weighted sentiment (recent news weighs more)
  - **Event Detection:** Significant market events trigger real-time alerts
  - ⚠️ **Scope:** Sentiment analysis is **NOT** run on all 500 stocks. It is triggered **only** for stocks that have already passed fundamental + technical + institutional screening (typically ~10-30 shortlisted candidates). This keeps LLM calls minimal and focused.
- [ ] **P2.4** Bulk Deal / FII-DII Monitor *(port from `fii_scanner.py`)*
  - Fetch bulk & block deals from NSE API (`/api/snapshot-capital-market-largedeal`)
  - **Client classification engine:** Identify FII/FPI, DII, and OTHER using:
    - Known name list (60+ global investment banks, asset managers, sovereign funds, PE/hedge funds)
    - Keyword-based fallback (FPI, FOREIGN, MAURITIUS, SINGAPORE, CAYMAN, etc.)
    - DII keyword matching (mutual funds, insurance, pension funds)
  - Per-stock aggregation: net buy/sell quantity & value (₹Cr) for FII and DII separately
  - Signal generation: 🟢 BUYING / 🔴 SELLING per institution type
  - Track entity names involved in each deal (top 5 per stock)
  - Output: `fii_deals.csv` with full institutional activity breakdown

### Phase 3: Recommendation Engine (Week 5)
> **Goal:** Combine all signals into actionable recommendations

- [ ] **P3.1** Adaptive Composite Scoring System
  ```
  Step 1: Pre-filter (runs on all 500 stocks, NO sentiment):
    Pre-Score = (0.40 × Fundamental) + (0.30 × Technical) 
              + (0.30 × Institutional)
    → Shortlist candidates with Pre-Score ≥ 50

  Step 2: Sentiment enrichment (runs ONLY on shortlisted stocks):
    WITH Sentiment (Ollama available):
    Final Score = (0.35 × Fundamental) + (0.25 × Technical) 
                + (0.20 × Sentiment) + (0.20 × Institutional)
  
    WITHOUT Sentiment (Ollama unavailable):
    Final Score = Pre-Score (unchanged)
  ```
- [ ] **P3.2** Signal Classification & Daily Limits
  - **Strong Buy:** Score ≥ 80
  - **Buy:** Score 65-79
  - **Hold:** Score 40-64
  - **Sell:** Score 20-39 — ⚠️ **Only generated for stocks currently in user's Zerodha holdings**
  - **Strong Sell:** Score < 20 — ⚠️ **Only generated for stocks currently in user's Zerodha holdings**
  - *Rationale: Sell/Strong Sell signals for stocks not held are meaningless — the system only recommends selling what the user actually owns.*
  - **Daily Buy Limit:** Only the **top 10** Buy/Strong Buy recommendations are delivered per day (ranked by Final Score descending). Remaining qualifying stocks are logged in DB but not pushed to Telegram — prevents information overload and forces the system to surface only the strongest conviction picks.
  - **No Sell Limit:** All Sell/Strong Sell signals for held stocks are delivered **without any cap**. If a held stock is at risk of value loss (score drops below threshold, technical breakdown, stop-loss proximity, or adverse institutional activity), the alert is always sent immediately — capital protection takes priority over notification limits.
- [ ] **P3.3** Target Price Calculation
  - DCF-lite model (simplified discounted cash flow)
  - Peer multiple comparison
  - Technical target (next resistance level)
  - Weighted average of methods
- [ ] **P3.4** Position Sizing
  - Fetch current holdings value from **Zerodha API** (`kite.holdings()`)
  - Max single stock exposure: **10% of total holdings value**
  - Kelly Criterion (modified) for optimal position size within the 10% cap
  - Consider existing holdings to avoid over-concentration
  - Auto-adjust quantity if stock already partially held
- [ ] **P3.5** Rationale Generator
  - Template-based rationale (always works, no LLM needed)
  - **Optional:** LLM-enhanced rationale via Ollama when available
  - Summarize key factors driving the recommendation
  - Include risk factors and catalysts
- [ ] **P3.6** Holding Period Classification
  - **Medium-term (3-6 months):** Primary recommendations
  - **🔷 Super Long-term (1+ year):** Flagged separately with distinct badge
  - Classification based on fundamental strength vs technical momentum

### Phase 4: Telegram Bot (Week 6)
> **Goal:** User interface via Telegram

- [ ] **P4.1** Bot setup & command handlers (single-user, no registration)
  - `/scan` — Trigger manual scan
  - `/portfolio` — View current Zerodha holdings & P&L
  - `/history` — View trade history
  - `/watchlist` — Manage watchlist
  - `/settings` — Configure preferences (risk, sectors)
  - `/status` — System health & last scan time
  - `/mode` — Toggle paper trade / live trade mode
- [ ] **P4.2** Recommendation Message Format
  ```
  📊 STOCK RECOMMENDATION
  ━━━━━━━━━━━━━━━━━━━━━━
  🏢 Company: Reliance Industries Ltd
  🔤 Symbol: RELIANCE
  💰 Current Price: ₹2,450.75
  📈 P/E Ratio: 28.5
  🎯 Target Price: ₹2,850.00 (+16.3%)
  ⏱️ Target Duration: 3-6 months
  🏷️ Category: Medium-term (or 🔷 Super Long-term)
  📦 Suggested Qty: 15 shares (₹36,761)
  ✅ Recommendation: BUY
  
  📝 Rationale:
  Strong fundamentals with ROE of 12.5% and 
  consistent earnings growth. Technical indicators 
  show golden cross formation. Positive news sentiment 
  around new energy ventures. FII buying observed 
  in recent bulk deals.
  
  ⚠️ Risk Factors:
  - Crude oil price volatility
  - Regulatory changes in telecom sector
  
  ━━━━━━━━━━━━━━━━━━━━━━
  ```
- [ ] **P4.3** Approval Flow (Inline Keyboard)
  ```
  [✅ Approve Trade]  [❌ Reject]
  [✏️ Modify Qty]     [⏸️ Defer]
  ```
- [ ] **P4.4** Trade Execution Notifications
  - Order placed confirmation
  - Order executed confirmation
  - GTT order set confirmation
  - Daily portfolio summary

### Phase 5: Zerodha Integration (Week 7)
> **Goal:** Automated trading via Kite Connect

- [ ] **P5.1** Kite Connect Authentication
  - OAuth2 login flow (manual initial login via browser)
  - Access token management & auto-refresh
  - Session validation
- [ ] **P5.2** Order Placement
  - Market/Limit orders based on recommendation
  - Order validation (market hours, circuit limits)
  - Retry logic for failed orders
- [ ] **P5.3** GTT Order Management
  - Set GTT on target price (take profit)
  - Set GTT on stop-loss price
  - OCO (One Cancels Other) for combined TP/SL
  - Auto-update GTT when recommendation changes
- [ ] **P5.4** Portfolio Sync
  - Sync holdings from Zerodha on startup
  - Real-time position tracking
  - P&L calculation per trade and overall

### Phase 6: Scheduling & Orchestration (Week 8)
> **Goal:** Automated scheduled runs

- [ ] **P6.1** Scheduler Configuration
  | Job                    | Schedule                | Description                           |
  | ---------------------- | ----------------------- | ------------------------------------- |
  | Full Scan              | Daily 6:00 AM IST       | Complete fundamental + technical      |
  | Technical Update       | Every 2 hours (market)  | Update technical indicators           |
  | News Scan              | Every 30 mins           | Fetch & analyze latest news           |
  | Bulk Deal Check        | Daily 4:00 PM IST       | After market close                    |
  | Portfolio Sync         | Daily 9:00 AM IST       | Sync with Zerodha                     |
  | **Daily Digest**       | **Daily 9:00 AM IST**   | **Compile & send top 10 buy + all sell recommendations before market open**|
  | Weekly Summary         | Sunday 10:00 AM IST     | Portfolio performance report          |
  | **Event Monitor**      | **Every 5 mins (market)**| **Detect significant events for real-time alerts** |
- [ ] **P6.2** Event-Driven Real-time Alerts
  - Circuit breaker hits (upper/lower circuit)
  - Sudden volume spike (>3x average)
  - Large institutional deal detected
  - Major news event affecting held stocks
  - Significant index movement (Nifty ±2% intraday)
- [ ] **P6.3** Job monitoring & alerting
- [ ] **P6.4** Graceful error handling & retry mechanisms

### Phase 7: Testing & Hardening (Week 9-10)
> **Goal:** Production readiness

- [ ] **P7.1** Unit tests for all analysis engines
- [ ] **P7.2** Integration tests for API clients
- [ ] **P7.3** Backtesting framework
  - Historical recommendation accuracy
  - Simulated portfolio performance
- [ ] **P7.4** Paper trading mode (log trades without executing)
  - **Default mode on first launch**
  - Track virtual portfolio with real market prices
  - Generate accuracy reports after 1 month
  - Telegram command `/mode` to switch between paper/live
- [ ] **P7.5** Rate limiting & API quota management
- [ ] **P7.6** Logging & monitoring (structured logs)
- [ ] **P7.7** Error alerting via Telegram

---

## 5A. Existing Scanner Logic Reference

> **Source files:** [`external/long_term_scanner.py`](file:///d:/git/fbot/external/long_term_scanner.py) and [`external/fii_scanner.py`](file:///d:/git/fbot/external/fii_scanner.py)
>
> These contain proven, working scanner logic that must be ported into the modular architecture. The logic below is extracted directly from the code and serves as the specification for Phase 2.

### 5A.1 Long-Term GARP Scanner (`long_term_scanner.py`)

**Strategy:** Growth At Reasonable Price (GARP) — targets 1+ year investment horizon.

#### Data & Dependencies
- **Data period:** 2 years of historical OHLCV
- **Stock universe:** NSE stocks loaded via `load_symbols_with_industry()` (Nifty 500)
- **External indicators module:** `add_emas()`, `add_rsi()`, `add_atr()`, `add_ichimoku()`, `add_returns()`
- **Fundamentals:** Fetched via `safe_get_info()` (yfinance `.info` dict with caching)
- **Processing:** Batch processing with `process_symbols_in_batches()` (0.1s delay per stock)

#### Hard Filters (Must Pass — Reject Otherwise)

| Filter | Condition | Rationale |
|---|---|---|
| Market Cap | ₹500 Cr – ₹15,000 Cr | Small/mid-cap growth sweet spot |
| ROE | ≥ 10% | Minimum capital efficiency |
| D/E Ratio | ≤ 1.5 | Conservative leverage |
| P/E | > 0 (must exist) | Eliminates loss-making companies |
| FII Holding | > 0% | Institutional confidence signal |
| Price vs EMA200 | Price > EMA200 | Must be in long-term uptrend |
| Composite Score | ≥ 60 / 100 | Quality threshold |

#### Composite Scoring System (100-point scale)

**Fundamental Score (70 points max):**

| Metric | Max Points | Scoring Tiers |
|---|---|---|
| ROE % | 10 | >25%→10, >20%→8, >15%→6, >10%→3 |
| Revenue Growth % | 10 | >25%→10, >15%→8, >10%→6, >5%→3 |
| Debt/Equity | 8 | <0.3→8, <0.5→6, <1.0→4, <1.5→1 |
| P/E vs Industry | 10 | <0.7×ind→10, <1.0×ind→8, <1.2×ind→5, <1.5×ind→2 |
| EPS Growth % | 10 | >25%→10, >18%→8, >12%→6, >5%→3 |
| Promoter Holding % | 8 | >60%→8, >50%→6, >40%→4, >30%→2 |
| Market Cap Sweet Spot | 5 | ₹1K-10KCr→5, ₹500-15KCr→3 |
| FII Holding % | 9 | >20%→9, >10%→7, >5%→5, >0%→2 |

**Technical Score (30 points max):**

| Metric | Max Points | Scoring Tiers |
|---|---|---|
| EMA Alignment | 10 | Close>EMA50>EMA200→10, Close>EMA200→5 |
| RSI (14) | 6 | 45-70→6, 40-75→3 |
| 6-Month Return | 8 | >30%→8, >20%→6, >15%→4, >10%→2 |
| Ichimoku Cloud | 6 | Price above cloud→6 |

#### Entry / Target / Stop Loss Calculation

```
Entry    = min(CMP, EMA50 × 1.02)     # Near EMA50 support, capped at CMP
Target 1 = Entry × 1.20               # +20% conservative
Target 2 = Entry × 1.35               # +35% aggressive
SL       = max(EMA200 × 0.97,         # 3% below EMA200
               Entry × 0.85)          # or 15% drawdown cap (tighter wins)
R:R      = (Target1 − Entry) / (Entry − SL)
```

#### Output Columns
`Stock, Industry, Score, CMP, Entry, StopLoss, Target1, Target2, RiskReward, MarketCap_Cr, ROE%, DE_Ratio, PE, Ind_PE, EPS_Growth%, Rev_Growth%, Promoter%, FII%, RSI, Return_6M%, Return_1Y%`

#### Industry P/E Median Lookup (Hardcoded)
| Sector | Median P/E |
|---|---|
| Financial Services | 18 |
| Information Technology | 28 |
| Healthcare | 30 |
| Automobile | 22 |
| FMCG | 45 |
| Consumer Durables | 35 |
| Capital Goods | 30 |
| Oil Gas & Consumable Fuels | 12 |
| Metals & Mining | 10 |
| Construction Materials | 20 |
| Construction | 18 |
| Chemicals | 25 |
| Power | 15 |
| Telecom | 25 |
| Realty | 25 |
| Consumer Services | 50 |
| Textiles | 18 |
| Default (unknown) | 25 |

---

### 5A.2 FII/DII Bulk & Block Deals Scanner (`fii_scanner.py`)

**Purpose:** Fetch daily bulk and block deal data from NSE India and classify trades by institutional type (FII/FPI vs DII vs Other).

#### Data Source
- **NSE API:** `https://www.nseindia.com/api/snapshot-capital-market-largedeal`
- **Library:** `nsepython.nsefetch()` for session-managed NSE requests
- **Response fields:** `BULK_DEALS_DATA[]` and `BLOCK_DEALS_DATA[]`
- **Per-deal fields:** `symbol`, `clientName`, `buySell`, `qty`, `watp` (weighted avg trade price), `name`, `date`

#### Client Classification Engine

Three-tier classification using uppercase name matching:

1. **Known FII Names (exact substring match):** 60+ entities including:
   - Global investment banks: Goldman Sachs, Morgan Stanley, JP Morgan, HSBC, UBS, etc.
   - Asset managers: BlackRock, Vanguard, Fidelity, Aberdeen, Templeton, etc.
   - Sovereign funds: GIC (Singapore), Temasek, Abu Dhabi (ADIA), Norway, Qatar, Kuwait, Saudi
   - PE/Hedge funds: Tiger Global, Warburg Pincus, KKR, Carlyle, Sequoia, SoftBank, etc.
   - Canada pension: CPPIB, Ontario Teachers, CDPQ
   - Specific FPIs: Elara Capital, Copthall Mauritius, Nalanda Capital, etc.

2. **FII Keywords (fallback):** FPI, FOREIGN, OVERSEAS, MAURITIUS, SINGAPORE, HONG KONG, CAYMAN, LUXEMBOURG, IRELAND, CYPRUS, GLOBAL FUND, OFFSHORE, etc.

3. **DII Keywords:** MUTUAL FUND, LIFE INSURANCE, PENSION FUND, PROVIDENT FUND, specific fund houses (SBI, HDFC, ICICI, Kotak, Axis, Nippon, LIC, etc.)

4. **Default:** `OTHER` if no pattern matches.

#### Per-Stock Aggregation Logic

For each stock with institutional deals:
- Aggregate **FII Buy Qty/Value** and **FII Sell Qty/Value** separately
- Aggregate **DII Buy Qty/Value** and **DII Sell Qty/Value** separately
- Compute **Net Qty** and **Net Value (₹Cr)** for each institution type
- Generate signal: `🟢 BUYING` (net > 0), `🔴 SELLING` (net < 0), or `—` (neutral)
- Collect entity names (up to 5 per stock)
- Track deal types (BULK / BLOCK) and deal dates
- Value computation: `value_cr = (qty × watp) / 1e7`
- Final sort: by absolute FII net value descending (biggest activity first)

#### Output Columns
`Stock, Company, FII_Signal, FII_NetQty, FII_NetValue_Cr, FII_BuyValue_Cr, FII_SellValue_Cr, DII_Signal, DII_NetQty, DII_NetValue_Cr, DII_BuyValue_Cr, DII_SellValue_Cr, FII_Entities, DII_Entities, DealType, DealDate`

---

### 5A.3 Integration Notes

| Aspect | Current (external/) | Target (src/) |
|---|---|---|
| Execution | Standalone Python scripts | Modular services called by scheduler |
| Data source (fundamentals) | `yfinance .info` via `safe_get_info()` | Same, wrapped in `yahoo_client.py` |
| Data source (OHLCV) | `yfinance.download()` via batch util | Same, wrapped in `yahoo_client.py` |
| Data source (deals) | `nsepython.nsefetch()` | Wrapped in `nse_client.py` |
| Indicators | `indicators.py` module | Port to `analysis/technical.py` |
| Stock list | `scanner_utils.load_symbols_with_industry()` | Port to `data/stock_universe.py` |
| Caching | File-based info cache | DB-backed cache with TTL |
| Output | CSV files | Database records + Telegram delivery |
| Industry P/E | Hardcoded dict | DB/config table (updateable) |
| FII name list | Hardcoded list | DB/config table (extensible) |
| Scoring | Single composite (70F+30T) | Adaptive composite (with optional sentiment) |

---

## 6. Project Structure

```
fbot/
├── .env                          # Environment variables (API keys, tokens)
├── .env.example                  # Template for .env
├── requirements.txt              # Python dependencies
├── pyproject.toml                # Project metadata
├── README.md
├── Plan.md                       # This file
│
├── Dockerfile                    # Multi-stage build for fbot app
├── docker-compose.yml            # Orchestrates fbot + PostgreSQL containers
├── .dockerignore                 # Excludes .env, __pycache__, .git, etc.
│
├── src/
│   ├── __init__.py
│   ├── main.py                   # Application entry point
│   ├── config.py                 # Pydantic settings & configuration
│   │
│   ├── db/
│   │   ├── __init__.py
│   │   ├── engine.py             # SQLAlchemy engine & session (async PostgreSQL)
│   │   ├── models.py             # All ORM models
│   │   └── migrations/           # Alembic migrations
│   │
│   ├── data/
│   │   ├── __init__.py
│   │   ├── nse_client.py         # NSE India API client
│   │   ├── yahoo_client.py       # yfinance wrapper
│   │   ├── news_client.py        # News RSS/API aggregator
│   │   └── stock_universe.py     # Stock universe management
│   │
│   ├── analysis/
│   │   ├── __init__.py
│   │   ├── fundamental.py        # Fundamental analysis engine
│   │   ├── technical.py          # Technical analysis engine
│   │   ├── sentiment.py          # News sentiment analysis
│   │   └── institutional.py      # Bulk deal / FII-DII analysis
│   │
│   ├── engine/
│   │   ├── __init__.py
│   │   ├── scorer.py             # Composite scoring system
│   │   ├── recommender.py        # Recommendation generator
│   │   ├── target_price.py       # Target price calculator
│   │   └── position_sizer.py     # Position sizing logic
│   │
│   ├── bot/
│   │   ├── __init__.py
│   │   ├── telegram_bot.py       # Telegram bot setup & handlers
│   │   ├── commands.py           # Command handlers
│   │   ├── callbacks.py          # Inline keyboard callbacks
│   │   └── formatters.py         # Message formatting utilities
│   │
│   ├── trading/
│   │   ├── __init__.py
│   │   ├── kite_client.py        # Zerodha Kite Connect wrapper
│   │   ├── order_manager.py      # Order placement & tracking
│   │   ├── gtt_manager.py        # GTT order management
│   │   └── portfolio.py          # Portfolio sync & tracking
│   │
│   └── scheduler/
│       ├── __init__.py
│       └── jobs.py               # Scheduled job definitions
│
├── tests/
│   ├── __init__.py
│   ├── test_fundamental.py
│   ├── test_technical.py
│   ├── test_sentiment.py
│   ├── test_recommender.py
│   └── test_order_manager.py
│
└── data/
    ├── nifty500.csv              # Stock universe master list
    └── sectors.json              # Sector classification
```

### 6.1 Docker Architecture

```
┌─────────────────────────────────────────────────────────┐
│                   docker-compose.yml                     │
│                                                         │
│  ┌─────────────────────┐    ┌────────────────────────┐  │
│  │   fbot (app)        │    │   postgres (db)        │  │
│  │   python:3.12-slim  │───▶│   postgres:16-alpine   │  │
│  │   Port: 5000        │    │   Port: 5432           │  │
│  │   Volume: ./data    │    │   Volume: pgdata       │  │
│  └─────────────────────┘    └────────────────────────┘  │
│                                                         │
│  Platform: linux/amd64, linux/arm64                     │
│  Works on: Windows (Docker Desktop), Linux, Raspberry Pi│
└─────────────────────────────────────────────────────────┘
```

**Key Docker design decisions:**
- **Multi-arch images:** Both `python:3.12-slim` and `postgres:16-alpine` have official ARM64 builds, so the same `docker-compose.yml` works on Raspberry Pi without changes
- **Separate DB container:** PostgreSQL runs in its own container with a named volume (`pgdata`) for data persistence across restarts/upgrades
- **App container:** Thin `python:3.12-slim` base, multi-stage build to keep image small (~200MB)
- **Environment variables:** All config via `.env` file mounted into the app container
- **Health checks:** PostgreSQL health check ensures the app waits for DB readiness
- **Data volume:** `./data` directory bind-mounted for CSV exports, cache files, and stock universe data
- **No host-specific paths:** All paths are relative/containerized — fully machine-independent

---

## 7. Configuration & API Keys Required

| Service           | Key/Token                  | How to Obtain                                     |
| ----------------- | -------------------------- | ------------------------------------------------- |
| Telegram Bot      | `TELEGRAM_BOT_TOKEN`       | Create bot via @BotFather on Telegram              |
| Telegram          | `TELEGRAM_CHAT_ID`         | Your personal chat ID                               |
| Zerodha Kite      | `KITE_API_KEY`             | See Section 8 below                                 |
| Zerodha Kite      | `KITE_API_SECRET`          | See Section 8 below                                 |
| Ollama            | (local, no key needed)     | `ollama pull llama3` — optional for sentiment        |
| NewsAPI           | `NEWS_API_KEY`             | https://newsapi.org/ (optional)                     |

---

## 8. Zerodha Kite Connect — Setup Guide

### Step 1: Prerequisites
- Active Zerodha trading account (Demat + Trading)
- PAN linked to your Zerodha account

### Step 2: Register as a Kite Connect Developer
1. Go to **https://developers.kite.trade/**
2. Click **"Sign up"** and log in with your Zerodha credentials
3. Fill the developer application form:
   - **App Name:** `FBot`
   - **App Type:** Select "Personal" (for personal use)
   - **Redirect URL:** `http://127.0.0.1:5000/callback` (we'll use this for OAuth)
   - **Description:** "Personal stock scanner and trading bot"
4. Accept the Terms & Conditions
5. **Pay the API subscription fee** (₹2,000/month as of 2024)

### Step 3: Get API Credentials
1. After approval, go to **My Apps** in the developer console
2. Note down:
   - **API Key** → put in `.env` as `KITE_API_KEY`
   - **API Secret** → put in `.env` as `KITE_API_SECRET`

### Step 4: Daily Authentication Flow
Kite Connect tokens expire daily. FBot handles this:
1. On startup, FBot opens a login URL in your browser
2. You log in to Zerodha (2FA with TOTP)
3. Browser redirects to `http://127.0.0.1:5000/callback?request_token=xxx`
4. FBot exchanges the request token for an access token
5. Access token is cached for the trading day

> **Note:** This manual login is required once per trading day. FBot will send a Telegram reminder if the token expires.

### Step 5: API Endpoints We'll Use
| Endpoint | Purpose |
|---|---|
| `kite.holdings()` | Fetch current holdings (for position sizing) |
| `kite.place_order()` | Execute approved trades |
| `kite.place_gtt()` | Set GTT orders (target + stop-loss) |
| `kite.positions()` | Intraday positions |
| `kite.orders()` | Order status tracking |
| `kite.instruments()` | Full instrument list for symbol mapping |

---

## 9. Finalized Design Decisions

| # | Decision | Choice | Rationale |
|---|----------|--------|-----------|
| DD-1 | Stock Universe | **Nifty 500** | Comprehensive coverage of Indian markets |
| DD-2 | Database | **PostgreSQL 16** (separate Docker container) | Production-grade, persistent named volume, same image on all platforms |
| DD-3 | Sentiment | **Ollama (optional)** | Free, local, system works without it |
| DD-4 | Deployment | **Docker Compose (multi-arch)** | Machine-independent — runs on Windows (Docker Desktop), Linux, and Raspberry Pi (ARM64) |
| DD-5 | Paper Trading | **Yes, default mode** | Validate accuracy before risking capital |
| DD-6 | Notifications | **Daily digest + event alerts** | EOD digest at 9 AM + real-time on significant events |
| DD-7 | Risk Mgmt | **10% max per stock** from Zerodha holdings | Prevents over-concentration |
| DD-8 | NSE API | **yfinance primary** + NSE as fallback | More reliable, less anti-bot issues |

---

## 9. Risk & Limitations

| Risk                              | Mitigation                                        |
| --------------------------------- | ------------------------------------------------- |
| NSE API blocking/changes          | Fallback to yfinance, cache aggressively           |
| Zerodha token expiry (daily)      | Auto-refresh flow, notify user if manual needed    |
| Bad recommendation → loss         | Paper trade mode, position size limits, stop-loss  |
| API rate limits                   | Exponential backoff, request queuing               |
| Market hours dependency           | Schedule jobs around market hours                  |
| News data quality                 | Multiple sources, relevance filtering              |
| Stale fundamental data            | Quarterly refresh, flag data age                   |
| Raspberry Pi resource limits      | Lightweight base images, batch processing with delays, PostgreSQL tuned for low RAM (shared_buffers=128MB) |
| Docker not installed on host      | Document install steps for Windows (Docker Desktop), Linux (`apt`), and Raspberry Pi OS |
| DB data loss on container removal | Named volume `pgdata` persists across container lifecycle; document backup strategy |

---

## 10. Future Enhancements (Post MVP)

- [ ] Web dashboard for portfolio visualization
- [ ] Backtesting engine with historical P&L
- [ ] Options strategy recommendations
- [ ] Sector rotation signals
- [ ] Mutual fund overlap analysis
- [ ] Multi-user support
- [ ] WhatsApp bot integration
- [ ] Alerting on corporate actions (dividends, splits, results)
- [ ] Integration with Groww/Angel One (alternate brokers)

---

## 11. Next Steps

1. ✅ ~~Design & plan~~ — Complete
2. ⬜ Set up Telegram bot (@BotFather)
3. ⬜ Set up Zerodha Kite Connect developer account
4. ⬜ Begin Phase 1: Foundation & Data Layer

---

> **The plan is finalized. Ready to start coding Phase 1 on your go!**
