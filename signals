"""
P/D + order-block + OANDA-book signal scanner -> Telegram.

Runs once per invocation (GitHub Actions calls it every 30 minutes). For each instrument:
  1. Bias      H4 structure (break of structure, else HH/HL vs LH/LL), daily 50 SMA veto
  2. Location  % position in the H4 dealing range (last swing high / low)
               FX: longs < 38.2%, shorts > 61.8%   Metals: longs < 50%, shorts > 50%
  3. Setup     unmitigated H1 order blocks in the bias direction
  4. Entry     OANDA order book, latest snapshot:
               SWEEP  stop cluster at/just beyond the OB, M15 candle runs it and closes back
               SHELF  (metals only) heavy limit orders overlapping the OB, touched and held
  5. TPs       room check (nearest opposing limit wall >= 1.5R), TP1 = that wall,
               TP2 = next stop cluster beyond (FX capped at 3R)
Sends ZONE alerts (price first enters a qualifying OB) and SIGNAL alerts (trigger fired).
Read-only: only GET requests to OANDA. Never places trades. Not financial advice.

Env: OANDA_TOKEN, OANDA_ENV (practice|live), TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
Optional: INSTRUMENTS (comma list), ZONE_ALERTS (true/false), SENT_FILE, DRY_RUN (true prints only)
"""

from __future__ import annotations

import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import httpx
import numpy as np
import pandas as pd

# ------------------------------------------------------------------ settings
INSTRUMENTS = os.environ.get(
    "INSTRUMENTS",
    # USD pairs (majors + liquid exotics)
    "EUR_USD,GBP_USD,AUD_USD,NZD_USD,USD_JPY,USD_CHF,USD_CAD,"
    "USD_MXN,USD_ZAR,USD_SEK,USD_NOK,USD_SGD,"
    # metals
    "XAU_USD,XAG_USD,XPT_USD,XPD_USD,XCU_USD,"
    # energy
    "WTICO_USD,BCO_USD,NATGAS_USD",
).split(",")
COMMODITY_PREFIXES = ("XAU", "XAG", "XPT", "XPD", "XCU", "WTICO", "BCO", "NATGAS",
                      "CORN", "WHEAT", "SOYBN", "SUGAR")
AGRI = ("CORN", "WHEAT", "SOYBN", "SUGAR")
ENERGY = ("WTICO", "BCO", "NATGAS")
ZONE_ALERTS = os.environ.get("ZONE_ALERTS", "true").lower() == "true"
DRY_RUN = os.environ.get("DRY_RUN", "false").lower() == "true"
SENT_FILE = os.environ.get("SENT_FILE", "sent.json")
# Big-picture gate: only trade when the daily trend is clear and NOT choppy
CHOP_ER = float(os.environ.get("CHOP_ER", "0.25"))   # daily efficiency ratio(20) needed
SLOPE_DAYS = 10                                      # 50 SMA must be rising/falling over this many days
M15_LOOKBACK = 2           # completed M15 candles checked per run (covers a 30-min schedule)
BIN_PCT = 0.05             # order-book bin width, % of price
WIN_PCT = 1.0              # look for clusters within +/- this % of price
CLUSTER_MULT = 2.0         # a cluster must be >= this x the average bin of its type
WALL_MULT = 1.5            # an opposing wall for TP1 must be >= this x average
UK = ZoneInfo("Europe/London")

TOKEN = os.environ.get("OANDA_TOKEN", "")
BASE = ("https://api-fxtrade.oanda.com" if os.environ.get("OANDA_ENV", "practice") == "live"
        else "https://api-fxpractice.oanda.com")


def is_metal(ins: str) -> bool:
    """True for every commodity: metals, energy and agriculture follow the 'trending' rules."""
    return ins.startswith(COMMODITY_PREFIXES)


def session_ok(ins: str, now_uk: datetime) -> bool:
    h = now_uk.hour + now_uk.minute / 60
    if 21.5 <= h < 23.0:                       # rollover: spreads widen
        return False
    if ins.startswith(AGRI):
        return 14.5 <= h < 19.25                # US grain/softs hours
    if ins.startswith(ENERGY):
        return 7 <= h < 19.5                    # London + New York
    if is_metal(ins):
        return 7 <= h < 10.5 or 13 <= h < 17
    if any(c in ins for c in ("MXN", "ZAR", "SEK", "NOK")):
        return 7 <= h < 17
    if "SGD" in ins:
        return h < 10 or h >= 23
    if "CAD" in ins:
        return 13 <= h < 21
    if any(c in ins for c in ("JPY", "AUD", "NZD")):
        return h < 10 or h >= 23
    return 7 <= h < 17                          # EUR / GBP / CHF


# ------------------------------------------------------------------ data
def oanda(path: str, params: dict | None = None) -> dict:
    r = httpx.get(BASE + path, params=params, timeout=20,
                  headers={"Authorization": f"Bearer {TOKEN}"})
    if r.status_code != 200:
        raise RuntimeError(f"{r.status_code} {r.text[:120]}")
    return r.json()


def candles(ins: str, gran: str, count: int) -> pd.DataFrame:
    js = oanda(f"/v3/instruments/{ins}/candles", {"granularity": gran, "count": count, "price": "M"})
    rows = [{"time": pd.Timestamp(c["time"]), "open": float(c["mid"]["o"]), "high": float(c["mid"]["h"]),
             "low": float(c["mid"]["l"]), "close": float(c["mid"]["c"])}
            for c in js["candles"] if c["complete"]]
    return pd.DataFrame(rows)


def book_bins(ins: str, kind: str) -> dict:
    """Bin an order or position book. Returns price, time and per-category lists of (level, pct)."""
    b = oanda(f"/v3/instruments/{ins}/{kind}")["orderBook" if kind == "orderBook" else "positionBook"]
    price = float(b["price"])
    width = price * BIN_PCT / 100
    lo, hi = price * (1 - WIN_PCT / 100), price * (1 + WIN_PCT / 100)
    cats = {"buy_above": {}, "buy_below": {}, "sell_above": {}, "sell_below": {}}
    for x in b["buckets"]:
        p = float(x["price"])
        if not lo <= p <= hi:
            continue
        k = int((p - lo) // width)
        side = "above" if p > price else "below"
        cats[f"buy_{side}"][k] = cats[f"buy_{side}"].get(k, 0) + float(x["longCountPercent"])
        cats[f"sell_{side}"][k] = cats[f"sell_{side}"].get(k, 0) + float(x["shortCountPercent"])
    out = {"price": price, "time": b["time"]}
    for c, d in cats.items():
        vals = [v for v in d.values() if v > 0]
        out[c] = [(lo + (k + 0.5) * width, v) for k, v in d.items() if v > 0]
        out[c + "_avg"] = (sum(vals) / len(vals)) if vals else 0.0
    return out


# ------------------------------------------------------------------ analysis
def atr(df: pd.DataFrame, n: int = 14) -> float:
    pc = df["close"].shift()
    tr = pd.concat([df["high"] - df["low"], (df["high"] - pc).abs(), (df["low"] - pc).abs()], axis=1).max(axis=1)
    return float(tr.ewm(alpha=1 / n, adjust=False).mean().iloc[-1])


def swings(df: pd.DataFrame, k: int = 2) -> tuple[list, list]:
    hs, ls = [], []
    h, l = df["high"].to_numpy(), df["low"].to_numpy()
    for i in range(k, len(df) - k):
        if h[i] == h[i - k:i + k + 1].max():
            hs.append((i, h[i]))
        if l[i] == l[i - k:i + k + 1].min():
            ls.append((i, l[i]))
    return hs, ls


def h4_bias(h4: pd.DataFrame) -> tuple[int, float, float]:
    hs, ls = swings(h4)
    if len(hs) < 2 or len(ls) < 2:
        return 0, np.nan, np.nan
    last = h4["close"].iloc[-1]
    rhi, rlo = hs[-1][1], ls[-1][1]
    if last > rhi:
        bias = 1
    elif last < rlo:
        bias = -1
    elif hs[-1][1] > hs[-2][1] and ls[-1][1] > ls[-2][1]:
        bias = 1
    elif hs[-1][1] < hs[-2][1] and ls[-1][1] < ls[-2][1]:
        bias = -1
    else:
        bias = 0
    return bias, rhi, rlo


def order_blocks(h1: pd.DataFrame, side: int, lookback: int = 80) -> list[dict]:
    o, h, l, c = (h1[k].to_numpy() for k in ("open", "high", "low", "close"))
    body = np.abs(c - o)
    avg_body = pd.Series(body).rolling(20).mean().to_numpy()
    obs = []
    start = max(25, len(h1) - lookback)
    for j in range(start, len(h1) - 3):
        if side == 1 and c[j] >= o[j]:
            continue
        if side == -1 and c[j] <= o[j]:
            continue
        ref = h[j - 10:j].max() if side == 1 else l[j - 10:j].min()
        for k in range(1, 4):
            d = j + k
            broke = c[d] > ref if side == 1 else c[d] < ref
            # the OB is the LAST opposing candle before the displacement
            between_opposing = any((c[m] < o[m]) if side == 1 else (c[m] > o[m]) for m in range(j + 1, d))
            if between_opposing:
                break
            if broke and body[d] >= 1.5 * avg_body[d]:
                after = c[d + 1:]
                mitigated = (after < l[j]).any() if side == 1 else (after > h[j]).any()
                if not mitigated:
                    obs.append({"lo": l[j], "hi": h[j], "time": h1["time"].iloc[j]})
                break
    return obs[-3:]


def fmt(ins: str, x: float) -> str:
    if is_metal(ins):
        return f"{x:.2f}" if x > 20 else f"{x:.4f}"
    if any(c in ins for c in ("JPY", "MXN", "ZAR", "SEK", "NOK")):
        return f"{x:.4f}" if "JPY" not in ins else f"{x:.3f}"
    return f"{x:.5f}"


def chart_levels(h1: pd.DataFrame, d1: pd.DataFrame, price: float) -> dict:
    """Stand-in for the order book when OANDA has none: recent H1 swing highs/lows and the
    previous day's high/low act as both stop clusters and opposing walls."""
    hs, ls = swings(h1.iloc[-60:])
    highs = [v for _, v in hs] + [float(d1["high"].iloc[-1])]
    lows = [v for _, v in ls] + [float(d1["low"].iloc[-1])]
    above = [(v, 1.0) for v in highs if v > price]
    below = [(v, 1.0) for v in lows if v < price]
    # in chart mode every listed level qualifies, so averages are set low
    return {"price": price, "time": None, "chart": True,
            "sell_above": above, "sell_above_avg": 0.1, "buy_above": above, "buy_above_avg": 0.1,
            "buy_below": below, "buy_below_avg": 0.1, "sell_below": below, "sell_below_avg": 0.1}


def plan_targets(ins: str, side: int, entry: float, stop: float, ob: dict) -> dict | None:
    risk = abs(entry - stop)
    walls = ob["sell_above"] if side == 1 else ob["buy_below"]
    wavg = ob["sell_above_avg"] if side == 1 else ob["buy_below_avg"]
    walls = [(p, v) for p, v in walls if v >= WALL_MULT * wavg and side * (p - entry) > 0]
    walls.sort(key=lambda t: side * (t[0] - entry))
    if walls:
        tp1 = walls[0][0]
        if side * (tp1 - entry) < 1.5 * risk:
            return {"room_fail": True, "wall": tp1, "r": side * (tp1 - entry) / risk}
        tp1_note = "opposing swing level" if ob.get("chart") else "opposing limit wall"
    else:
        tp1, tp1_note = entry + side * 2 * risk, "no wall within 1% (default 2R)"
    stops = ob["buy_above"] if side == 1 else ob["sell_below"]
    savg = ob["buy_above_avg"] if side == 1 else ob["sell_below_avg"]
    beyond = [(p, v) for p, v in stops if v >= CLUSTER_MULT * savg and side * (p - tp1) > 0]
    beyond.sort(key=lambda t: side * (t[0] - tp1))
    tp2 = beyond[0][0] if beyond else entry + side * 3 * risk
    if not is_metal(ins):
        tp2 = entry + side * min(side * (tp2 - entry), 3 * risk)
    if not is_metal(ins) and side * (tp1 - entry) > 3 * risk:
        tp1, tp1_note = entry + side * 3 * risk, "capped at 3R (FX)"
    if side * (tp2 - tp1) <= 0:          # FX cap landed at or before TP1: single target
        tp2, tp1_note = tp1, tp1_note + "; single target"
    return {"tp1": tp1, "tp1_note": tp1_note, "tp2": tp2,
            "r1": side * (tp1 - entry) / risk, "r2": side * (tp2 - entry) / risk}


def trapped_note(ins: str, side: int) -> str:
    try:
        pb = book_bins(ins, "positionBook")
    except Exception:
        return "position book unavailable"
    losing_shorts = sum(v for _, v in pb["sell_below"])
    losing_longs = sum(v for _, v in pb["buy_above"])
    other = "shorts" if side == 1 else "longs"
    trapped = losing_shorts > losing_longs if side == 1 else losing_longs > losing_shorts
    return f"{other} {'trapped' if trapped else 'not trapped'} (losing shorts {losing_shorts:.1f}% vs losing longs {losing_longs:.1f}%)"


def scan(ins: str, now_uk: datetime) -> list[tuple[str, str]]:
    """Return a list of (dedupe_key, message)."""
    d1, h4, h1, m15 = (candles(ins, g, n) for g, n in (("D", 80), ("H4", 200), ("H1", 200), ("M15", 10)))
    datr = atr(d1)
    sma50 = d1["close"].rolling(50).mean().iloc[-1]
    price = m15["close"].iloc[-1]

    bias, rhi, rlo = h4_bias(h4)
    if bias == 0 or not rhi > rlo:
        return []

    # ---- big-picture gate: clear daily trend, not choppy, H4 agreeing ----
    dc = d1["close"]
    sma = dc.rolling(50).mean()
    slope = sma.iloc[-1] - sma.iloc[-1 - SLOPE_DAYS]
    side_of_ma = np.sign(dc.iloc[-1] - sma.iloc[-1])
    daily_dir = int(side_of_ma) if np.sign(slope) == side_of_ma else 0
    er = abs(dc.iloc[-1] - dc.iloc[-21]) / dc.diff().abs().iloc[-20:].sum()
    if daily_dir == 0 or er < CHOP_ER or bias != daily_dir:
        return []
    big_picture = (f"Big picture: daily {'uptrend' if daily_dir == 1 else 'downtrend'}, 50SMA "
                   f"{'rising' if slope > 0 else 'falling'}, efficiency {er:.2f} (trending), H4 agrees")

    deep = 0.382 if not is_metal(ins) else 0.5
    obs = order_blocks(h1, bias)
    if not obs:
        return []
    try:
        ob = book_bins(ins, "orderBook")
        snap_age = (datetime.now(timezone.utc) - pd.Timestamp(ob["time"])).total_seconds() / 60
    except RuntimeError:
        ob = chart_levels(h1, d1, price)
        snap_age = None
    chart_mode = bool(ob.get("chart"))

    out = []
    side = bias
    name = ins.replace("_", "/")
    for blk in obs:
        mid_pos = ((blk["lo"] + blk["hi"]) / 2 - rlo) / (rhi - rlo)
        in_zone = mid_pos < deep if side == 1 else mid_pos > 1 - deep
        if not in_zone:
            continue
        # stop clusters at / just beyond the OB
        clusters = ob["sell_below"] if side == 1 else ob["buy_above"]
        cavg = ob["sell_below_avg"] if side == 1 else ob["buy_above_avg"]
        band_lo = blk["lo"] - 0.3 * datr if side == 1 else blk["lo"]
        band_hi = blk["hi"] if side == 1 else blk["hi"] + 0.3 * datr
        sweep_lv = [p for p, v in clusters if v >= CLUSTER_MULT * cavg and band_lo <= p <= band_hi]
        shelves = []
        if is_metal(ins) and not chart_mode:
            limits = ob["buy_below"] if side == 1 else ob["sell_above"]
            lavg = ob["buy_below_avg"] if side == 1 else ob["sell_above_avg"]
            shelves = [p for p, v in limits if v >= CLUSTER_MULT * lavg and blk["lo"] <= p <= blk["hi"]]

        recent = m15.iloc[-M15_LOOKBACK:]
        prev = m15.iloc[-M15_LOOKBACK - 1]
        for _, cdl in recent.iterrows():
            touched = cdl["low"] <= blk["hi"] if side == 1 else cdl["high"] >= blk["lo"]
            if not touched:
                continue
            trig, level = None, None
            for lv in sweep_lv:
                if (side == 1 and cdl["low"] < lv < cdl["close"] and cdl["close"] >= blk["lo"]) or \
                   (side == -1 and cdl["high"] > lv > cdl["close"] and cdl["close"] <= blk["hi"]):
                    trig, level = "SWEEP", lv
                    break
            if not trig:
                for lv in shelves:
                    if (side == 1 and cdl["low"] <= lv < cdl["close"]) or (side == -1 and cdl["high"] >= lv > cdl["close"]):
                        trig, level = "SHELF", lv
                        break
            if trig:
                entry = cdl["close"]
                stop = (min(cdl["low"], blk["lo"]) - 0.05 * datr) if side == 1 else (max(cdl["high"], blk["hi"]) + 0.05 * datr)
                tg = plan_targets(ins, side, entry, stop, ob)
                key = f"SIG|{ins}|{side}|{blk['time']}|{cdl['time']}"
                if tg is None:
                    continue
                if tg.get("room_fail"):
                    msg = (f"NO TRADE {name} {'LONG' if side == 1 else 'SHORT'}: {trig} fired but room check "
                           f"failed (wall at {fmt(ins, tg['wall'])} is only {tg['r']:.1f}R away)")
                else:
                    msg = "\n".join([
                        f"SIGNAL {name} {'LONG' if side == 1 else 'SHORT'} ({trig})"
                        + (" [chart levels, no OANDA book]" if chart_mode else ""),
                        f"Entry ~{fmt(ins, entry)} | Stop {fmt(ins, stop)}",
                        f"TP1 {fmt(ins, tg['tp1'])} ({tg['r1']:.1f}R, {tg['tp1_note']}) close half, stop to BE",
                        f"TP2 {fmt(ins, tg['tp2'])} ({tg['r2']:.1f}R)",
                        f"OB {fmt(ins, blk['lo'])}-{fmt(ins, blk['hi'])} | {trig.lower()} level {fmt(ins, level)}",
                        big_picture,
                        f"Range pos {mid_pos:.0%} | 50SMA {fmt(ins, sma50)}",
                        f"Positioning: {trapped_note(ins, side) if not chart_mode else 'n/a (no OANDA book)'}",
                        (f"Book snapshot {snap_age:.0f} min old" if snap_age is not None else "Levels from H1 swings + prior day")
                        + f" | M15 candle {cdl['time']:%H:%M} UTC",
                        "Check news + open USD exposure before entering. Not financial advice.",
                    ])
                out.append((key, msg))
            elif ZONE_ALERTS:
                was_out = prev["low"] > blk["hi"] if side == 1 else prev["high"] < blk["lo"]
                if was_out:
                    key = f"ZONE|{ins}|{side}|{blk['time']}"
                    lv_txt = ", ".join(fmt(ins, p) for p in sweep_lv + shelves) or "none near the OB yet"
                    out.append((key, "\n".join([
                        f"ZONE {name}{' [chart levels]' if chart_mode else ''}: price entered {'bullish' if side == 1 else 'bearish'} OB "
                        f"{fmt(ins, blk['lo'])}-{fmt(ins, blk['hi'])} ({'discount' if side == 1 else 'premium'}, {mid_pos:.0%})",
                        big_picture,
                        f"Levels to watch: {lv_txt}",
                        "Wait for the M15 trigger.",
                    ])))
            prev = cdl
    return out


# ------------------------------------------------------------------ telegram + state
def send(text: str) -> None:
    if DRY_RUN:
        print("---\n" + text)
        return
    tok, chat = os.environ.get("TELEGRAM_BOT_TOKEN", ""), os.environ.get("TELEGRAM_CHAT_ID", "")
    if not tok or not chat:
        sys.exit("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set")
    r = httpx.post(f"https://api.telegram.org/bot{tok}/sendMessage",
                   json={"chat_id": chat, "text": text, "disable_web_page_preview": True}, timeout=20)
    if r.status_code != 200:
        print("Telegram error:", r.text[:200])


def main() -> None:
    if not TOKEN:
        sys.exit("OANDA_TOKEN not set")
    sent = json.load(open(SENT_FILE)) if os.path.exists(SENT_FILE) else {}
    now_uk = datetime.now(UK)
    live = [i.strip() for i in INSTRUMENTS if i.strip() and session_ok(i.strip(), now_uk)]

    def safe_scan(ins):
        try:
            return scan(ins, now_uk)
        except Exception as e:  # one bad instrument shouldn't stop the rest
            print(f"{ins}: {e}")
            return []

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(safe_scan, live))
    for alerts in results:
        for key, msg in alerts:
            if key not in sent:
                send(msg)
                sent[key] = now_uk.isoformat()
    print(f"scanned {len(live)} instruments in session: {', '.join(live) or 'none'}")
    # keep the state file small: drop keys older than 3 days
    cutoff = pd.Timestamp.now(tz=UK) - pd.Timedelta(days=3)
    sent = {k: v for k, v in sent.items() if pd.Timestamp(v) > cutoff}
    json.dump(sent, open(SENT_FILE, "w"))
    print(f"scan done {now_uk:%Y-%m-%d %H:%M} UK, {len(sent)} recent alerts tracked")


if __name__ == "__main__":
    main()
