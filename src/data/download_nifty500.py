"""
Download the full Nifty 500 stock list from NSE India.

Usage (run inside Docker container):
    python -m src.data.download_nifty500

Or from host:
    docker compose exec fbot python -m src.data.download_nifty500
"""

import csv
import io
import os
import sys
import time

import requests

NSE_NIFTY500_URL = "https://www.nseindia.com/api/equity-stockIndices?index=NIFTY%20500"
NSE_BASE_URL = "https://www.nseindia.com"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.nseindia.com/market-data/live-equity-market",
}

OUTPUT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "data")
OUTPUT_FILE = os.path.join(OUTPUT_DIR, "nifty500.csv")


def get_nse_session() -> requests.Session:
    """Create an NSE session with proper cookies."""
    session = requests.Session()
    session.headers.update(HEADERS)
    # Hit the main page first to get cookies
    session.get(NSE_BASE_URL, timeout=10)
    time.sleep(1)
    return session


def fetch_nifty500_from_nse() -> list[dict]:
    """Fetch the Nifty 500 constituents from NSE API."""
    print("🔄 Fetching Nifty 500 list from NSE India...")

    session = get_nse_session()

    try:
        response = session.get(NSE_NIFTY500_URL, timeout=15)
        response.raise_for_status()
        data = response.json()
    except Exception as e:
        print(f"❌ NSE API failed: {e}")
        print("   Trying alternative method...")
        return fetch_nifty500_from_csv_download(session)

    stocks = []
    for item in data.get("data", []):
        symbol = item.get("symbol", "").strip()
        if not symbol or symbol == "NIFTY 500":
            continue

        stocks.append({
            "Symbol": symbol,
            "Company Name": item.get("meta", {}).get("companyName", symbol),
            "Industry": item.get("meta", {}).get("industry", "Unknown"),
            "Sector": item.get("meta", {}).get("sector", "Unknown"),
        })

    print(f"✅ Fetched {len(stocks)} stocks from NSE API")
    return stocks


def fetch_nifty500_from_csv_download(session: requests.Session) -> list[dict]:
    """
    Alternative: download the Nifty 500 CSV from NSE.
    
    Falls back to this if the JSON API fails.
    """
    csv_url = "https://www.nseindia.com/api/equity-stockIndices?csv=true&index=NIFTY%20500"

    try:
        response = session.get(csv_url, timeout=15)
        response.raise_for_status()

        reader = csv.DictReader(io.StringIO(response.text))
        stocks = []
        for row in reader:
            symbol = row.get("Symbol", "").strip()
            if not symbol:
                continue
            stocks.append({
                "Symbol": symbol,
                "Company Name": row.get("Company Name", symbol),
                "Industry": row.get("Industry", "Unknown"),
                "Sector": row.get("Sector", "Unknown"),
            })

        print(f"✅ Fetched {len(stocks)} stocks from NSE CSV")
        return stocks

    except Exception as e:
        print(f"❌ CSV download also failed: {e}")
        return []


def save_to_csv(stocks: list[dict]) -> str:
    """Save stock list to CSV file."""
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    # Sort by symbol
    stocks.sort(key=lambda s: s["Symbol"])

    with open(OUTPUT_FILE, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["Symbol", "Company Name", "Industry", "Sector"])
        writer.writeheader()
        writer.writerows(stocks)

    print(f"💾 Saved {len(stocks)} stocks to {OUTPUT_FILE}")
    return OUTPUT_FILE


def main():
    stocks = fetch_nifty500_from_nse()

    if not stocks:
        print("❌ Could not fetch stock list. Please try again later.")
        print("   NSE may be blocking requests. Try during market hours.")
        sys.exit(1)

    if len(stocks) < 400:
        print(f"⚠️  Only got {len(stocks)} stocks (expected ~500). NSE may have returned partial data.")

    save_to_csv(stocks)
    print(f"\n🎉 Done! {len(stocks)} Nifty 500 stocks saved.")
    print(f"   Restart FBot to load the new universe: docker compose restart fbot")


if __name__ == "__main__":
    main()
