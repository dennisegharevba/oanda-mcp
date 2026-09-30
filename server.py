"""
OANDA read-only MCP server for Claude (remote / Streamable HTTP).

Exposes OANDA order book, position book, prices and candles as MCP tools,
with book data pre-summarised into the zones used for retail-positioning reads:
trapped longs/shorts, stop clusters, limit walls.

READ-ONLY BY DESIGN: this server only ever sends GET requests to OANDA.
There is no code path that can place, modify or close orders.

Env vars:
  OANDA_TOKEN       v20 personal access token              (required)
  OANDA_ACCOUNT_ID  account id, e.g. 101-004-1234567-001   (needed for prices/account tools)
  OANDA_ENV         "practice" (default) or "live"
  MCP_SECRET        long random string; becomes part of the URL path (required)
  PUBLIC_HOST       your deployed hostname, e.g. oanda-mcp.onrender.com (recommended)
  PORT              port to listen on (hosting platforms set this)
"""

from __future__ import annotations

import os
from typing import Any

import httpx
import uvicorn
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings

# ---------------------------------------------------------------- config
TOKEN = os.environ.get("OANDA_TOKEN", "")
ACCOUNT_ID = os.environ.get("OANDA_ACCOUNT_ID", "")
ENV = os.environ.get("OANDA_ENV", "practice").lower()
SECRET = os.environ.get("MCP_SECRET", "")
PUBLIC_HOST = os.environ.get("PUBLIC_HOST", "")
PORT = int(os.environ.get("PORT", "8000"))

BASE_URL = (
    "https://api-fxtrade.oanda.com" if ENV == "live" else "https://api-fxpractice.oanda.com"
)

mcp = MCPServer(
    name="oanda-readonly",
    instructions=(
        "Read-only access to OANDA retail positioning (order book, position book), "
        "prices and candles. Instruments use OANDA format, e.g. EUR_USD, USD_CHF, XAU_USD. "
        "Book data is a 20-30 minute snapshot of OANDA clients only, not the whole market. "
        "Use read_books for a combined, summarised read of both books."
    ),
)


# ---------------------------------------------------------------- OANDA client (GET only)
async def _get(path: str, params: dict[str, Any] | None = None) -> dict:
    if not TOKEN:
        raise RuntimeError("OANDA_TOKEN is not set on the server.")
    headers = {"Authorization": f"Bearer {TOKEN}", "Accept-Datetime-Format": "RFC3339"}
    async with httpx.AsyncClient(base_url=BASE_URL, headers=headers, timeout=20) as client:
        r = await client.get(path, params=params)
    if r.status_code != 200:
        # Surface OANDA's error message (e.g. instrument not supported for books)
        raise RuntimeError(f"OANDA {r.status_code}: {r.text[:300]}")
    return r.json()


def _norm(instrument: str) -> str:
    """Accept EURUSD, EUR/USD, eur_usd, XAUUSD -> EUR_USD / XAU_USD."""
    s = instrument.upper().replace("/", "_").replace("-", "_").strip()
    if "_" not in s and len(s) == 6:
        s = f"{s[:3]}_{s[3:]}"
    return s


def _need_account() -> None:
    if not ACCOUNT_ID:
        raise RuntimeError("OANDA_ACCOUNT_ID is not set on the server.")


# ---------------------------------------------------------------- book summarising
def _summarise(book: dict, kind: str, window_pct: float, bins: int) -> dict:
    """Condense an OANDA book (hundreds of buckets) into the zones that matter.

    kind: "position" or "order"
    Bucket price is the bucket's lower edge; "above"/"below" are relative to book price.
    """
    price = float(book["price"])
    rows = [
        (float(b["price"]), float(b["longCountPercent"]), float(b["shortCountPercent"]))
        for b in book["buckets"]
    ]

    long_total = sum(r[1] for r in rows)
    short_total = sum(r[2] for r in rows)
    long_above = sum(l for p, l, s in rows if p > price)
    long_below = sum(l for p, l, s in rows if p <= price)
    short_above = sum(s for p, l, s in rows if p > price)
    short_below = sum(s for p, l, s in rows if p <= price)

    if kind == "position":
        zones = {
            "longs_above_price_LOSING": round(long_above, 2),
            "shorts_below_price_LOSING": round(short_below, 2),
            "longs_below_price_winning": round(long_below, 2),
            "shorts_above_price_winning": round(short_above, 2),
        }
        share = long_total / (long_total + short_total) * 100 if (long_total + short_total) else 0
        headline = {
            "pct_long": round(share, 1),
            "pct_short": round(100 - share, 1),
            "trapped_side": (
                "longs" if long_above > short_below else "shorts" if short_below > long_above else "balanced"
            ),
        }
    else:
        zones = {
            "buy_stops_above_price": round(long_above, 2),     # mostly shorts' stop-losses / breakout buys
            "sell_limits_above_price": round(short_above, 2),  # resistance / profit-taking
            "buy_limits_below_price": round(long_below, 2),    # support
            "sell_stops_below_price": round(short_below, 2),   # mostly longs' stop-losses / breakdown sells
        }
        headline = {
            "buy_orders_total": round(long_total, 2),
            "sell_orders_total": round(short_total, 2),
        }

    # Re-bin a window around price so the profile is readable
    lo, hi = price * (1 - window_pct / 100), price * (1 + window_pct / 100)
    win = [r for r in rows if lo <= r[0] <= hi]
    width = (hi - lo) / bins
    profile = []
    for i in range(bins):
        a, b = lo + i * width, lo + (i + 1) * width
        in_bin = [r for r in win if a <= r[0] < b]
        profile.append(
            {
                "from": round(a, 5),
                "to": round(b, 5),
                "long" if kind == "position" else "buy": round(sum(r[1] for r in in_bin), 2),
                "short" if kind == "position" else "sell": round(sum(r[2] for r in in_bin), 2),
                "contains_price": a <= price < b,
            }
        )
    profile.reverse()  # top of the list = highest price, like the chart

    # Biggest individual clusters in the window
    top_long = sorted(win, key=lambda r: r[1], reverse=True)[:5]
    top_short = sorted(win, key=lambda r: r[2], reverse=True)[:5]
    lab_l, lab_s = ("long", "short") if kind == "position" else ("buy", "sell")

    return {
        "instrument": book.get("instrument"),
        "snapshot_time": book.get("time"),
        "price": price,
        "bucket_width": float(book.get("bucketWidth", 0)),
        "headline": headline,
        "zones_pct_of_total": zones,
        f"top_{lab_l}_clusters": [
            {"price": round(p, 5), "pct": round(l, 2), "side_of_price": "above" if p > price else "below"}
            for p, l, s in top_long
        ],
        f"top_{lab_s}_clusters": [
            {"price": round(p, 5), "pct": round(s, 2), "side_of_price": "above" if p > price else "below"}
            for p, l, s in top_short
        ],
        "profile_window_pct": window_pct,
        "profile_high_to_low": profile,
    }


# ---------------------------------------------------------------- tools
@mcp.tool()
async def read_books(instrument: str, window_pct: float = 1.5, bins: int = 20) -> dict:
    """Combined read of OANDA's position book AND order book for one instrument.

    Returns, for each book: headline split, the four price zones (trapped/winning
    positions; stop vs limit orders above/below price), the largest clusters, and
    a re-binned price profile within +/- window_pct of current price.
    Use this first for any positioning question.
    """
    ins = _norm(instrument)
    pos = await _get(f"/v3/instruments/{ins}/positionBook")
    ords = await _get(f"/v3/instruments/{ins}/orderBook")
    return {
        "position_book": _summarise(pos["positionBook"], "position", window_pct, bins),
        "order_book": _summarise(ords["orderBook"], "order", window_pct, bins),
        "note": "OANDA clients only; snapshot updates every ~20-30 min. Pressure map, not a forecast.",
    }


@mcp.tool()
async def get_position_book(instrument: str, time: str | None = None,
                            window_pct: float = 1.5, bins: int = 20) -> dict:
    """Summarised OANDA position book. Optional `time` (RFC3339) for a past snapshot."""
    ins = _norm(instrument)
    data = await _get(f"/v3/instruments/{ins}/positionBook", {"time": time} if time else None)
    return _summarise(data["positionBook"], "position", window_pct, bins)


@mcp.tool()
async def get_order_book(instrument: str, time: str | None = None,
                         window_pct: float = 1.5, bins: int = 20) -> dict:
    """Summarised OANDA order book. Optional `time` (RFC3339) for a past snapshot."""
    ins = _norm(instrument)
    data = await _get(f"/v3/instruments/{ins}/orderBook", {"time": time} if time else None)
    return _summarise(data["orderBook"], "order", window_pct, bins)


@mcp.tool()
async def usd_positioning_scan(
    instruments: list[str] = ["EUR_USD", "GBP_USD", "USD_JPY", "USD_CHF", "AUD_USD", "USD_CAD", "XAU_USD"],
) -> dict:
    """Quick position-book headline for several instruments at once, to check whether
    retail positioning tells a consistent story (e.g. dollar strength). Instruments the
    books don't cover are reported as errors rather than failing the whole scan."""
    out = {}
    for raw in instruments:
        ins = _norm(raw)
        try:
            pb = (await _get(f"/v3/instruments/{ins}/positionBook"))["positionBook"]
            s = _summarise(pb, "position", 1.5, 10)
            out[ins] = {"price": s["price"], "time": s["snapshot_time"], **s["headline"],
                        **s["zones_pct_of_total"]}
        except Exception as e:  # noqa: BLE001
            out[ins] = {"error": str(e)[:200]}
    return out


@mcp.tool()
async def get_prices(instruments: list[str]) -> dict:
    """Current bid/ask for one or more instruments."""
    _need_account()
    ins = ",".join(_norm(i) for i in instruments)
    data = await _get(f"/v3/accounts/{ACCOUNT_ID}/pricing", {"instruments": ins})
    return {
        p["instrument"]: {
            "bid": p["bids"][0]["price"] if p.get("bids") else None,
            "ask": p["asks"][0]["price"] if p.get("asks") else None,
            "time": p.get("time"),
            "tradeable": p.get("tradeable"),
        }
        for p in data.get("prices", [])
    }


@mcp.tool()
async def get_candles(instrument: str, granularity: str = "H1", count: int = 100) -> dict:
    """Mid-price OHLC candles. granularity: M5, M15, M30, H1, H4, D, W. count max 500."""
    ins = _norm(instrument)
    data = await _get(
        f"/v3/instruments/{ins}/candles",
        {"granularity": granularity, "count": min(max(count, 1), 500), "price": "M"},
    )
    return {
        "instrument": ins,
        "granularity": granularity,
        "candles": [
            {"t": c["time"], "o": c["mid"]["o"], "h": c["mid"]["h"], "l": c["mid"]["l"],
             "c": c["mid"]["c"], "v": c["volume"], "complete": c["complete"]}
            for c in data.get("candles", [])
        ],
    }


@mcp.tool()
async def get_account_overview() -> dict:
    """Read-only account summary plus open trades (no ability to change anything)."""
    _need_account()
    summ = (await _get(f"/v3/accounts/{ACCOUNT_ID}/summary"))["account"]
    trades = (await _get(f"/v3/accounts/{ACCOUNT_ID}/openTrades")).get("trades", [])
    return {
        "currency": summ.get("currency"),
        "balance": summ.get("balance"),
        "nav": summ.get("NAV"),
        "unrealized_pl": summ.get("unrealizedPL"),
        "margin_used": summ.get("marginUsed"),
        "open_trade_count": summ.get("openTradeCount"),
        "open_trades": [
            {"instrument": t["instrument"], "units": t["currentUnits"], "price": t["price"],
             "unrealized_pl": t.get("unrealizedPL"), "opened": t.get("openTime")}
            for t in trades
        ],
    }


# ---------------------------------------------------------------- app
def build_app():
    if not SECRET or len(SECRET) < 24:
        raise SystemExit("Set MCP_SECRET to a long random string (24+ chars).")
    security = (
        TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=[PUBLIC_HOST, f"{PUBLIC_HOST}:*"],
            allowed_origins=[f"https://{PUBLIC_HOST}"],
        )
        if PUBLIC_HOST
        else TransportSecuritySettings(enable_dns_rebinding_protection=False)
    )
    # The secret lives in the path, so only someone with the full URL can reach the tools.
    return mcp.streamable_http_app(
        streamable_http_path=f"/{SECRET}/mcp",
        stateless_http=True,
        json_response=True,
        transport_security=security,
        host="0.0.0.0",
    )


if __name__ == "__main__":
    uvicorn.run(build_app(), host="0.0.0.0", port=PORT)
