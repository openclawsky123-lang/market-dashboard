#!/usr/bin/env python3
"""
Market Dashboard Generator
Runs at 10am and 10pm HKT daily via cron.
Fetches live data and writes a self-contained HTML file to ~/Desktop/market_dashboard.html
"""

import requests
import json
import os
import sys
from datetime import datetime
from zoneinfo import ZoneInfo
import traceback

HKT = ZoneInfo("Asia/Hong_Kong")
OUTPUT_PATH = os.path.expanduser("~/Desktop/market_dashboard.html")
TIMEOUT = 15

def safe_get(url, params=None, headers=None):
    try:
        r = requests.get(url, params=params, headers=headers, timeout=TIMEOUT)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print(f"  WARN: {url[:60]}... failed: {e}")
        return None

# ── Crypto (Binance) ──────────────────────────────────────────────────────────
def fetch_crypto():
    symbols = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "HYPEUSDT"]
    data = {}
    for sym in symbols:
        d = safe_get("https://api.binance.com/api/v3/ticker/24hr", params={"symbol": sym})
        if d:
            key = sym.replace("USDT", "")
            data[key] = {
                "price": float(d["lastPrice"]),
                "change_pct": float(d["priceChangePercent"]),
                "high": float(d["highPrice"]),
                "low": float(d["lowPrice"]),
                "volume": float(d["quoteVolume"]),
                "change_7d_pct": None,
            }
        else:
            if sym == "HYPEUSDT":
                d2 = safe_get(
                    "https://api.coingecko.com/api/v3/simple/price",
                    params={"ids": "hyperliquid", "vs_currencies": "usd", "include_24hr_change": "true"}
                )
                if d2 and "hyperliquid" in d2:
                    data["HYPE"] = {
                        "price": d2["hyperliquid"]["usd"],
                        "change_pct": d2["hyperliquid"].get("usd_24h_change", 0),
                        "high": None, "low": None, "volume": None,
                        "change_7d_pct": None,
                    }

    # 7-day change via Binance klines for BTC/ETH/SOL
    for sym in ["BTCUSDT", "ETHUSDT", "SOLUSDT"]:
        key = sym.replace("USDT", "")
        if key not in data:
            continue
        klines = safe_get("https://api.binance.com/api/v3/klines",
                          params={"symbol": sym, "interval": "1d", "limit": 8})
        if klines and len(klines) >= 2:
            closes = [float(c[4]) for c in klines]
            data[key]["change_7d_pct"] = (closes[-1] - closes[0]) / closes[0] * 100

    # 7-day change for HYPE via CoinGecko market_chart
    if "HYPE" in data:
        mc = safe_get("https://api.coingecko.com/api/v3/coins/hyperliquid/market_chart",
                      params={"vs_currency": "usd", "days": "7", "interval": "daily"})
        if mc:
            prices = mc.get("prices", [])
            if len(prices) >= 2:
                data["HYPE"]["change_7d_pct"] = (prices[-1][1] - prices[0][1]) / prices[0][1] * 100

    return data

# ── Metals (CoinGecko PAXG proxy + fallback) ─────────────────────────────────
def fetch_metals():
    data = {}
    # Gold via PAXG (1 PAXG = 1 troy oz gold)
    d = safe_get(
        "https://api.coingecko.com/api/v3/simple/price",
        params={"ids": "pax-gold,tether-silver", "vs_currencies": "usd", "include_24hr_change": "true"}
    )
    if d:
        if "pax-gold" in d:
            data["gold"] = {"price": d["pax-gold"]["usd"], "change_pct": d["pax-gold"].get("usd_24h_change")}
        if "tether-silver" in d:
            data["silver"] = {"price": d["tether-silver"]["usd"], "change_pct": d["tether-silver"].get("usd_24h_change")}

    # WTI Oil via Alpha Vantage free (no key needed for commodity endpoint fallback)
    # Use a reliable free source: commodities API via exchangerate.host
    oil = safe_get("https://api.api-ninjas.com/v1/commodityprice?name=crude_oil")
    if oil and "price" in oil:
        data["oil"] = {"price": oil["price"], "change_pct": None}
    else:
        data["oil"] = {"price": None, "change_pct": None}

    return data

# ── Equities & Yields (yfinance) ──────────────────────────────────────────────
def fetch_equities():
    try:
        import yfinance as yf
    except ImportError:
        print("  yfinance not installed, run: pip install yfinance")
        return {}

    tickers = ["GOOGL", "TSLA", "NVDA", "0981.HK", "BE", "MU",
               "QQQ", "VOO", "^HSI", "000001.SS", "^N225", "^KS11",
               "MSTR", "^IRX", "^TYX", "SI=F", "CL=F"]
    import pandas as pd
    data = {}

    # Step 1: current price + daily change via fast_info (real-time, works for Asian markets)
    for t in tickers:
        try:
            info  = yf.Ticker(t).fast_info
            price = info.last_price
            prev  = info.previous_close
            chg   = (price - prev) / prev * 100 if prev else None
            data[t] = {"price": price, "change_pct": chg, "change_7d_pct": None}
        except Exception:
            pass

    # Step 2: 7-day change via batch historical download
    try:
        raw    = yf.download(tickers, period="12d", interval="1d",
                             auto_adjust=True, progress=False, threads=True)
        closes = raw["Close"]
        for t in tickers:
            try:
                series = closes[t].dropna()
                if len(series) == 0 or t not in data:
                    continue
                target = series.index[-1] - pd.Timedelta(days=7)
                hist   = series[series.index <= target]
                if len(hist) > 0:
                    data[t]["change_7d_pct"] = (data[t]["price"] - float(hist.iloc[-1])) / float(hist.iloc[-1]) * 100
            except Exception:
                pass
    except Exception as e:
        print(f"  yfinance 7d history failed: {e}")

    return data

# ── Deribit DVOL ──────────────────────────────────────────────────────────────
def fetch_dvol():
    import time
    now_ms = int(time.time() * 1000)
    from_ms = now_ms - 48 * 3600 * 1000
    d = safe_get(
        "https://www.deribit.com/api/v2/public/get_volatility_index_data",
        params={"currency": "BTC", "resolution": "3600",
                "start_timestamp": from_ms, "end_timestamp": now_ms}
    )
    if not d or "result" not in d:
        return None
    arr = d["result"].get("data", [])
    if not arr:
        return None
    latest = arr[-1]
    prev   = arr[-2] if len(arr) > 1 else None
    val    = float(latest[4])
    prev_val = float(prev[4]) if prev else None
    import math

    history_12h = []
    for pt in arr[-12:]:
        ts_ms = pt[0]
        v = float(pt[4])
        dt_hkt = datetime.fromtimestamp(ts_ms / 1000, tz=HKT)
        history_12h.append({"time": dt_hkt.strftime("%H:%M"), "val": v})

    return {
        "value": val,
        "change": round(val - prev_val, 2) if prev_val else None,
        "daily_move": round(val / math.sqrt(365), 2),
        "zone": "Very low — calm" if val < 40 else "Low-moderate" if val < 60 else "Elevated — watch" if val < 80 else "High fear",
        "bar_pct": min(val / 120 * 100, 100),
        "bar_color": "#1d9e75" if val < 40 else "#ba7517" if val < 80 else "#d85a30",
        "history_12h": history_12h,
    }

# ── PreStocks pre-IPO tokens (DexScreener, free) ─────────────────────────────
def fetch_prestocks():
    # Solana SPL mints + val multiplier (baselineValBn / baselinePrice — static)
    TOKENS = {
        "ANTHROPIC": {
            "name": "Anthropic",
            "mint": "Pren1FvFX6J3E4kXhJuCiAD5aDmGEb7qJRncwA8Lkhw",
            "val_mult": 380 / 259.14,
        },
        "SPACEX": {
            "name": "SpaceX",
            "mint": "PreANxuXjsy2pvisWWMNB6YaJNzr7681wJJr2rHsfTh",
            "val_mult": 1750 / 705.631,
        },
        "OPENAI": {
            "name": "OpenAI",
            "mint": "PreweJYECqtQwBtpxHL171nL2K6umo692gTm7Q3rpgF",
            "val_mult": 852 / 1022,
        },
        "ANDURIL": {
            "name": "Anduril",
            "mint": "PresTj4Yc2bAR197Er7wz4UUKSfqt6FryBEdAriBoQB",
            "val_mult": 56 / 75.05,
        },
        "NEURALINK": {
            "name": "Neuralink",
            "mint": "PrekqLJvJ3qVdXmBGDiexvwUTF4rLFDa6HWS4HJbw9S",
            "val_mult": 9.65 / 50.5,
        },
    }

    mints = ",".join(t["mint"] for t in TOKENS.values())
    d = safe_get(f"https://api.dexscreener.com/latest/dex/tokens/{mints}")
    if not d:
        return {}

    # Group by baseToken address; pick highest-liquidity pair per token
    from collections import defaultdict
    by_mint = defaultdict(list)
    for pair in (d.get("pairs") or []):
        by_mint[pair["baseToken"]["address"]].append(pair)

    result = {}
    for sym, meta in TOKENS.items():
        pairs = sorted(by_mint.get(meta["mint"], []),
                       key=lambda p: float((p.get("liquidity") or {}).get("usd") or 0),
                       reverse=True)
        if not pairs:
            continue
        p = pairs[0]
        price = float(p.get("priceUsd") or 0)
        chg24 = p.get("priceChange", {}).get("h24")
        implied_val_b = price * meta["val_mult"]
        result[sym] = {
            "name":         meta["name"],
            "price":        round(price, 2),
            "change_pct":   float(chg24) if chg24 is not None else None,
            "implied_val_b": round(implied_val_b, 1),
        }
    return result

# ── BTC on-chain / cycle indices (Binance klines, free) ──────────────────────
def fetch_btc_indices(btc_price):
    import statistics
    from datetime import datetime, timezone

    result = {}

    # Daily klines (1000 candles) → 200d MA + halving performance
    daily = safe_get("https://api.binance.com/api/v3/klines",
                     params={"symbol": "BTCUSDT", "interval": "1d", "limit": 1000})
    if daily:
        closes = [float(c[4]) for c in daily]
        times  = [int(c[0])   for c in daily]
        if len(closes) >= 200:
            import math
            ma_200d = statistics.mean(closes[-200:])
            result["ma_200d"] = round(ma_200d, 0)
            if btc_price:
                # Real Ahr999 formula: (price/200d_MA) × (price/power-law model price)
                # Power-law model anchored to Bitcoin genesis block (2009-01-03)
                genesis = datetime(2009, 1, 3, tzinfo=timezone.utc)
                days_since = (datetime.now(timezone.utc) - genesis).days
                model_price = 10 ** (5.84 * math.log10(days_since) - 17.01)
                ahr999 = (btc_price / ma_200d) * (btc_price / model_price)
                result["ahr999"] = round(ahr999, 4)
                result["ahr999_zone"] = (
                    "Heavy accumulation" if ahr999 < 0.45 else
                    "Accumulation zone" if ahr999 < 1.2 else
                    "Caution — take profit"
                )
        HALVING_TS = 1713484800000  # Apr 19 2024 00:00 UTC
        halving_price = next((closes[i] for i, t in enumerate(times) if t >= HALVING_TS), None)
        if halving_price and btc_price:
            days_since = (datetime.now(timezone.utc) - datetime(2024, 4, 19, tzinfo=timezone.utc)).days
            result["halving"] = {
                "date": "Apr 19, 2024",
                "days": days_since,
                "price_at": round(halving_price, 0),
                "pct_gain": round((btc_price - halving_price) / halving_price * 100, 1),
            }

    # Weekly klines (210 candles) → 200-week MA
    weekly = safe_get("https://api.binance.com/api/v3/klines",
                      params={"symbol": "BTCUSDT", "interval": "1w", "limit": 210})
    if weekly:
        w_closes = [float(c[4]) for c in weekly]
        if len(w_closes) >= 200:
            ma_200w = statistics.mean(w_closes[-200:])
            result["ma_200w"] = round(ma_200w, 0)
            if btc_price:
                result["price_to_200w"] = round(btc_price / ma_200w, 2)

    return result

# ── MSTR treasury ─────────────────────────────────────────────────────────────
def fetch_mstr(btc_price, mstr_stock):
    holdings = 818869
    avg_cost  = 75540
    total_cost = avg_cost * holdings
    nav = btc_price * holdings if btc_price else None
    pnl = nav - total_cost if nav else None
    return {
        "holdings": holdings,
        "avg_cost": avg_cost,
        "total_cost_b": total_cost / 1e9,
        "nav_b": nav / 1e9 if nav else None,
        "pnl_b": pnl / 1e9 if pnl else None,
        "pnl_pct": pnl / total_cost * 100 if pnl else None,
        "mnav": 1.19,
        "stock_price": mstr_stock,
        "latest_purchase": "535 BTC · May 11 · avg $80,340",
    }

# ── HTML renderer ─────────────────────────────────────────────────────────────
def fmt_usd(v, decimals=2):
    if v is None: return "—"
    if v >= 1000:
        return f"${v:,.0f}"
    return f"${v:,.{decimals}f}"

def fmt_pct(v):
    if v is None: return "—"
    sign = "+" if v >= 0 else ""
    cls = "up" if v > 0 else "dn" if v < 0 else "ne"
    return f'<span class="{cls}">{sign}{v:.2f}%</span>'

def card(ticker, label, price_str, sub_html):
    return f"""
    <div class="card">
      <div class="ctk">{ticker}</div>
      <div class="clb">{label}</div>
      <div class="cv">{price_str}</div>
      <div class="cs">{sub_html}</div>
    </div>"""

def render_html(crypto, metals, equities, dvol, mstr, btc_indices, prestocks, generated_at):
    # helper
    def eq(t): return equities.get(t, {})
    def cr(t): return crypto.get(t, {})

    gold   = metals.get("gold",   {})
    silver = metals.get("silver", {})
    oil    = metals.get("oil",    {})

    btc_price = cr("BTC").get("price")
    dvol_bar  = dvol or {}

    # DVOL bar color gradient string
    dv_val  = dvol_bar.get("value", 0)
    dv_pct  = dvol_bar.get("bar_pct", 0)
    dv_col  = dvol_bar.get("bar_color", "#888")
    dv_zone = dvol_bar.get("zone", "—")
    dv_chg  = dvol_bar.get("change")
    dv_daily= dvol_bar.get("daily_move")
    dv_chg_str = (f'{"+" if dv_chg >= 0 else ""}{dv_chg:.1f} pts' if dv_chg is not None else "—")
    dv_chg_cls = "up" if (dv_chg or 0) < 0 else "dn" if (dv_chg or 0) > 0 else "ne"

    # Build DVOL 12h sparkline (separate string to avoid f-string nesting)
    dv_hist = dvol_bar.get("history_12h", [])
    dvol_sparkline = ""
    if len(dv_hist) >= 2:
        hvals = [pt["val"] for pt in dv_hist]
        htimes = [pt["time"] for pt in dv_hist]
        vmin = min(hvals) - 1
        vmax = max(hvals) + 1
        W, H = 200, 44
        def _px(i, v):
            x = i / (len(hvals) - 1) * W
            y = H - (v - vmin) / (vmax - vmin) * H
            return "%.1f,%.1f" % (x, y)
        pts_str = " ".join(_px(i, v) for i, v in enumerate(hvals))
        tick_idx = sorted(set([0, len(hvals)//4, len(hvals)//2, 3*len(hvals)//4, len(hvals)-1]))
        ticks = ""
        for i in tick_idx:
            left_pct = i / (len(hvals) - 1) * 100
            ticks += (
                '<div style="position:absolute;left:%.1f%%;transform:translateX(-50%%);'
                'text-align:center;font-size:9px;color:var(--hint);white-space:nowrap">'
                '%s<br><b style="color:var(--text);font-size:10px">%.1f</b></div>'
                % (left_pct, htimes[i], hvals[i])
            )
        dvol_sparkline = (
            '<div style="margin-top:14px;border-top:1px solid var(--border);padding-top:12px">'
            '<div class="ctk" style="margin-bottom:8px">DVOL — last 12 hours</div>'
            '<svg viewBox="0 0 %d %d" width="100%%" height="44" preserveAspectRatio="none" style="display:block">'
            '<polyline points="%s" fill="none" stroke="%s" stroke-width="2" '
            'stroke-linejoin="round" stroke-linecap="round"/>'
            '</svg>'
            '<div style="position:relative;height:30px;margin-top:2px">%s</div>'
            '</div>'
        ) % (W, H, pts_str, dv_col, ticks)

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Market Dashboard · {generated_at}</title>
<style>
  *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
  :root {{
    --bg: #f8f7f4;
    --surface: #ffffff;
    --surface2: #f1efe8;
    --border: rgba(0,0,0,0.1);
    --text: #1a1a1a;
    --muted: #6b6b68;
    --hint: #999996;
    --up: #1d9e75;
    --dn: #d85a30;
    --accent: #534ab7;
  }}
  @media (prefers-color-scheme: dark) {{
    :root {{
      --bg: #1a1a18;
      --surface: #242422;
      --surface2: #2e2e2c;
      --border: rgba(255,255,255,0.1);
      --text: #f0ede8;
      --muted: #a0a09d;
      --hint: #666663;
    }}
  }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background: var(--bg); color: var(--text); padding: 2rem; font-size: 14px; line-height: 1.5; }}
  .wrap {{ max-width: 900px; margin: 0 auto; }}
  .hdr {{ display: flex; justify-content: space-between; align-items: flex-end; margin-bottom: 1.5rem; border-bottom: 1px solid var(--border); padding-bottom: 1rem; }}
  .hdr h1 {{ font-size: 22px; font-weight: 600; }}
  .hdr-meta {{ font-size: 11px; color: var(--hint); text-align: right; line-height: 1.8; }}
  .sec {{ font-size: 10px; font-weight: 600; letter-spacing: .1em; text-transform: uppercase; color: var(--hint); margin: 1.5rem 0 .625rem; padding-bottom: 5px; border-bottom: 1px solid var(--border); }}
  .grid {{ display: grid; gap: 10px; margin-bottom: 6px; }}
  .g2 {{ grid-template-columns: repeat(2, 1fr); }}
  .g3 {{ grid-template-columns: repeat(3, 1fr); }}
  .g4 {{ grid-template-columns: repeat(4, 1fr); }}
  @media (max-width: 600px) {{ .g3, .g4 {{ grid-template-columns: repeat(2, 1fr); }} .g2 {{ grid-template-columns: 1fr; }} }}
  .card {{ background: var(--surface2); border-radius: 10px; padding: 12px 14px; }}
  .ctk {{ font-size: 10px; color: var(--hint); font-weight: 600; letter-spacing: .04em; margin-bottom: 1px; }}
  .clb {{ font-size: 11px; color: var(--muted); margin-bottom: 4px; }}
  .cv  {{ font-size: 20px; font-weight: 600; color: var(--text); line-height: 1.2; }}
  .cs  {{ font-size: 11px; margin-top: 3px; color: var(--muted); }}
  .up {{ color: var(--up); }} .dn {{ color: var(--dn); }} .ne {{ color: var(--hint); }}
  .wide {{ background: var(--surface2); border-radius: 10px; padding: 12px 16px; margin-bottom: 6px; }}
  .mrow {{ display: flex; justify-content: space-between; align-items: baseline; padding: 6px 0; border-bottom: 1px solid var(--border); }}
  .mrow:last-child {{ border-bottom: none; }}
  .ml {{ font-size: 12px; color: var(--muted); }}
  .mv {{ font-size: 13px; font-weight: 600; color: var(--text); }}
  .ms {{ font-size: 11px; color: var(--hint); margin-left: 8px; }}
  .dvol-block {{ background: var(--surface2); border-radius: 10px; padding: 14px 16px; }}
  .dvol-num {{ font-size: 36px; font-weight: 700; line-height: 1; margin-bottom: 4px; }}
  .dvol-zone {{ font-size: 12px; color: var(--muted); margin-bottom: 10px; }}
  .bar-bg {{ height: 8px; background: var(--border); border-radius: 99px; margin-bottom: 4px; }}
  .bar-fill {{ height: 8px; border-radius: 99px; }}
  .bar-labels {{ display: flex; justify-content: space-between; font-size: 10px; color: var(--hint); margin-bottom: 10px; }}
  .dvol-stats {{ display: grid; grid-template-columns: 1fr 1fr; gap: 8px; margin-top: 8px; }}
  .ds {{ background: var(--surface); border-radius: 8px; padding: 8px 10px; }}
  .ds-l {{ font-size: 10px; color: var(--hint); margin-bottom: 2px; }}
  .ds-v {{ font-size: 15px; font-weight: 600; color: var(--text); }}
  .hl-note {{ font-size: 11px; color: var(--muted); background: var(--surface); border: 1px solid var(--border); border-radius: 8px; padding: 8px 12px; margin-bottom: 8px; line-height: 1.6; }}
  .pill {{ display: inline-block; font-size: 9px; padding: 1px 6px; border-radius: 99px; border: 1px solid var(--border); color: var(--hint); margin-left: 4px; vertical-align: middle; }}
  .link {{ color: var(--accent); text-decoration: none; font-size: 10px; }}
  .link:hover {{ text-decoration: underline; }}
  .footer {{ margin-top: 2rem; padding-top: 1rem; border-top: 1px solid var(--border); font-size: 10px; color: var(--hint); line-height: 1.8; }}
  .tag {{ display: inline-block; font-size: 9px; padding: 1px 7px; border-radius: 99px; font-weight: 600; margin-left: 6px; }}
  .tag-bull {{ background: #e1f5ee; color: #085041; }}
  .tag-bear {{ background: #faece7; color: #712b13; }}
  .tag-neu  {{ background: var(--surface); color: var(--hint); border: 1px solid var(--border); }}
</style>
</head>
<body>
<div class="wrap">

<div class="hdr">
  <div>
    <h1>Market Dashboard</h1>
    <div style="font-size:12px;color:var(--hint);margin-top:4px">Auto-generated · 10am &amp; 10pm HKT daily</div>
  </div>
  <div class="hdr-meta">
    <div>{generated_at}</div>
    <div>Hong Kong Time</div>
  </div>
</div>

<!-- COMMODITIES -->
<div class="sec">Commodities</div>
<div class="grid g3">
  {card("XAU/USD", "Gold", fmt_usd(gold.get("price"), 0), fmt_pct(gold.get("change_pct")) + " 24h")}
  {card("WTI crude", "Oil", fmt_usd(oil.get("price"), 2) + "/bbl", '<span class="dn">Hormuz risk premium</span>' if not oil.get("price") else "WTI spot")}
  {card("XAG/USD", "Silver", fmt_usd(silver.get("price"), 2), fmt_pct(silver.get("change_pct")) + " 24h")}
</div>

<!-- TREASURIES -->
<div class="sec">US Treasuries</div>
<div class="grid g2">
  {card("US 1Y CMT", "1-Year yield", f'{eq("^IRX").get("price", 0):.2f}%' if eq("^IRX").get("price") else "—", "Fed cuts priced out")}
  {card("US 30Y CMT", "30-Year yield", f'{eq("^TYX").get("price", 0):.2f}%' if eq("^TYX").get("price") else "—", fmt_pct(eq("^TYX").get("change_pct")) + " today")}
</div>

<!-- STOCKS -->
<div class="sec">Stocks</div>
<div class="grid g3">
  {card("GOOGL", "Google", fmt_usd(eq("GOOGL").get("price"), 2), fmt_pct(eq("GOOGL").get("change_pct")) + " today · " + fmt_pct(eq("GOOGL").get("change_7d_pct")) + " 7d")}
  {card("TSLA", "Tesla", fmt_usd(eq("TSLA").get("price"), 2), fmt_pct(eq("TSLA").get("change_pct")) + " today · " + fmt_pct(eq("TSLA").get("change_7d_pct")) + " 7d")}
  {card("NVDA", "NVIDIA", fmt_usd(eq("NVDA").get("price"), 2), fmt_pct(eq("NVDA").get("change_pct")) + " today · " + fmt_pct(eq("NVDA").get("change_7d_pct")) + " 7d")}
</div>
<div class="grid g3">
  {card("00981.HK", "SMIC", f'HK${eq("0981.HK").get("price", 0):,.2f}' if eq("0981.HK").get("price") else "—", fmt_pct(eq("0981.HK").get("change_pct")) + " today · " + fmt_pct(eq("0981.HK").get("change_7d_pct")) + " 7d")}
  {card("BE", "Bloom Energy", fmt_usd(eq("BE").get("price"), 2), fmt_pct(eq("BE").get("change_pct")) + " today · " + fmt_pct(eq("BE").get("change_7d_pct")) + " 7d")}
  {card("MU", "Micron Tech", fmt_usd(eq("MU").get("price"), 2), fmt_pct(eq("MU").get("change_pct")) + " today · " + fmt_pct(eq("MU").get("change_7d_pct")) + " 7d")}
</div>

<!-- COUNTRY INDICES -->
<div class="sec">Country Indices</div>
<div class="grid g3">
  {card("QQQ", "Nasdaq-100 ETF", fmt_usd(eq("QQQ").get("price"), 2), fmt_pct(eq("QQQ").get("change_pct")) + " today · " + fmt_pct(eq("QQQ").get("change_7d_pct")) + " 7d")}
  {card("VOO", "S&amp;P 500 ETF", fmt_usd(eq("VOO").get("price"), 2), fmt_pct(eq("VOO").get("change_pct")) + " today · " + fmt_pct(eq("VOO").get("change_7d_pct")) + " 7d")}
  {card("HSI", "Hang Seng", f'{eq("^HSI").get("price", 0):,.0f}' if eq("^HSI").get("price") else "—", fmt_pct(eq("^HSI").get("change_pct")) + " today · " + fmt_pct(eq("^HSI").get("change_7d_pct")) + " 7d")}
</div>
<div class="grid g3">
  {card("SSE", "SSE Composite", f'{eq("000001.SS").get("price", 0):,.2f}' if eq("000001.SS").get("price") else "—", fmt_pct(eq("000001.SS").get("change_pct")) + " today · " + fmt_pct(eq("000001.SS").get("change_7d_pct")) + " 7d")}
  {card("N225", "Nikkei 225", f'{eq("^N225").get("price", 0):,.0f}' if eq("^N225").get("price") else "—", fmt_pct(eq("^N225").get("change_pct")) + " today · " + fmt_pct(eq("^N225").get("change_7d_pct")) + " 7d")}
  {card("KOSPI", "Korea KOSPI", f'{eq("^KS11").get("price", 0):,.2f}' if eq("^KS11").get("price") else "—", fmt_pct(eq("^KS11").get("change_pct")) + " today · " + fmt_pct(eq("^KS11").get("change_7d_pct")) + " 7d")}
</div>

<!-- CRYPTO -->
<div class="sec">Crypto — token prices</div>
<div class="grid g4">
  {card("BTC", "Bitcoin", fmt_usd(cr("BTC").get("price"), 0), fmt_pct(cr("BTC").get("change_pct")) + " 24h · " + fmt_pct(cr("BTC").get("change_7d_pct")) + " 7d")}
  {card("ETH", "Ethereum", fmt_usd(cr("ETH").get("price"), 2), fmt_pct(cr("ETH").get("change_pct")) + " 24h · " + fmt_pct(cr("ETH").get("change_7d_pct")) + " 7d")}
  {card("SOL", "Solana", fmt_usd(cr("SOL").get("price"), 2), fmt_pct(cr("SOL").get("change_pct")) + " 24h · " + fmt_pct(cr("SOL").get("change_7d_pct")) + " 7d")}
  {card("HYPE", "Hyperliquid", fmt_usd(cr("HYPE").get("price"), 2), fmt_pct(cr("HYPE").get("change_pct")) + " 24h · " + fmt_pct(cr("HYPE").get("change_7d_pct")) + " 7d")}
</div>

<!-- DVOL -->
<div class="sec">BTC Volatility — Deribit DVOL</div>
<div class="dvol-block">
  <div style="display:grid;grid-template-columns:1fr 1fr;gap:16px;align-items:start">
    <div>
      <div class="ctk">30-day implied volatility</div>
      <div class="dvol-num" style="color:{'#1d9e75' if dv_val < 40 else '#ba7517' if dv_val < 80 else '#d85a30'}">{f'{dv_val:.1f}' if dv_val else '—'}</div>
      <div class="dvol-zone">{dv_zone}</div>
      <div class="bar-bg"><div class="bar-fill" style="width:{dv_pct:.1f}%;background:{dv_col}"></div></div>
      <div class="bar-labels"><span>0</span><span>Low 40</span><span>Mid 60</span><span>High 80</span><span>120</span></div>
    </div>
    <div class="dvol-stats">
      <div class="ds"><div class="ds-l">Expected daily move</div><div class="ds-v">~{dv_daily}%</div></div>
      <div class="ds"><div class="ds-l">Annualized implied vol</div><div class="ds-v">{f'{dv_val:.1f}%' if dv_val else '—'}</div></div>
      <div class="ds"><div class="ds-l">1h change</div><div class="ds-v {dv_chg_cls}">{dv_chg_str}</div></div>
      <div class="ds"><div class="ds-l">Source</div><div class="ds-v" style="font-size:11px">Deribit API</div></div>
    </div>
  </div>
  {dvol_sparkline}
</div>

<!-- CRYPTO INDICES -->
<div class="sec">Crypto indices <span class="pill">Binance klines</span></div>
<div class="grid g3">
  <div class="card">
    <div class="ctk">AHR999</div>
    <div class="clb">BTC accumulation index</div>
    <div class="cv" style="color:{'#1d9e75' if (btc_indices.get('ahr999') or 1) < 0.45 else '#ba7517' if (btc_indices.get('ahr999') or 1) < 1.2 else '#d85a30'}">{btc_indices.get('ahr999', '—')}</div>
    <div class="cs">{btc_indices.get('ahr999_zone', '—')} &nbsp;<a class="link" href="https://www.coinglass.com/pro/i/ahr999" target="_blank">chart ↗</a></div>
  </div>
  <div class="card">
    <div class="ctk">200W MA</div>
    <div class="clb">BTC vs 200-week moving avg</div>
    <div class="cv">{btc_indices.get('price_to_200w', '—')}×</div>
    <div class="cs">200W MA ${btc_indices.get('ma_200w', 0):,.0f} &nbsp;<a class="link" href="https://www.coinglass.com/pro/i/200WMA" target="_blank">heatmap ↗</a></div>
  </div>
  <div class="card">
    <div class="ctk">SINCE HALVING</div>
    <div class="clb">Apr 19 2024 · day {btc_indices.get('halving', {}).get('days', '—')}</div>
    <div class="cv {'up' if (btc_indices.get('halving') or {}).get('pct_gain', 0) > 0 else 'dn'}">{('+' if (btc_indices.get('halving') or {}).get('pct_gain', 0) > 0 else '') + str(btc_indices.get('halving', {}).get('pct_gain', '—')) + '%'}</div>
    <div class="cs">at halving ${btc_indices.get('halving', {}).get('price_at', 0):,.0f} &nbsp;<a class="link" href="https://www.coinglass.com/pro/i/bitcoin-price-performance-since-halving" target="_blank">cycles ↗</a></div>
  </div>
</div>

<!-- PRE-IPO (PreStocks) -->
<div class="sec">Pre-IPO market <span class="pill">PreStocks · Solana</span></div>
<div class="hl-note">Token prices implying company valuations via SPV exposure. Speculative — not equity ownership. <a class="link" href="https://prestocks.com/products" target="_blank">prestocks.com ↗</a></div>
<div class="grid g3">
  {card("ANTHROPIC", "Anthropic", fmt_usd(prestocks.get("ANTHROPIC", {}).get("price"), 2), fmt_pct(prestocks.get("ANTHROPIC", {}).get("change_pct")) + " 24h · impl. val $" + f'{prestocks.get("ANTHROPIC", {}).get("implied_val_b", 0)/1000:.2f}T' if prestocks.get("ANTHROPIC") else "—")}
  {card("SPACEX", "SpaceX", fmt_usd(prestocks.get("SPACEX", {}).get("price"), 2), fmt_pct(prestocks.get("SPACEX", {}).get("change_pct")) + " 24h · impl. val $" + f'{prestocks.get("SPACEX", {}).get("implied_val_b", 0)/1000:.2f}T' if prestocks.get("SPACEX") else "—")}
  {card("OPENAI", "OpenAI", fmt_usd(prestocks.get("OPENAI", {}).get("price"), 2), fmt_pct(prestocks.get("OPENAI", {}).get("change_pct")) + " 24h · impl. val $" + f'{prestocks.get("OPENAI", {}).get("implied_val_b", 0)/1000:.2f}T' if prestocks.get("OPENAI") else "—")}
  {card("ANDURIL", "Anduril", fmt_usd(prestocks.get("ANDURIL", {}).get("price"), 2), fmt_pct(prestocks.get("ANDURIL", {}).get("change_pct")) + " 24h · impl. val $" + f'{prestocks.get("ANDURIL", {}).get("implied_val_b", 0):.1f}B' if prestocks.get("ANDURIL") else "—")}
  {card("NEURALINK", "Neuralink", fmt_usd(prestocks.get("NEURALINK", {}).get("price"), 2), fmt_pct(prestocks.get("NEURALINK", {}).get("change_pct")) + " 24h · impl. val $" + f'{prestocks.get("NEURALINK", {}).get("implied_val_b", 0):.1f}B' if prestocks.get("NEURALINK") else "—")}
</div>

<!-- MSTR -->
<div class="sec">Strategy (MSTR) — Bitcoin treasury</div>
<div class="wide">
  <div class="mrow"><span class="ml">BTC holdings</span><span><span class="mv">818,869 BTC</span><span class="ms">~3.9% of all BTC ever</span></span></div>
  <div class="mrow"><span class="ml">Avg cost basis</span><span><span class="mv">$75,540</span><span class="ms">Total ~${mstr["total_cost_b"]:.2f}B spent</span></span></div>
  <div class="mrow"><span class="ml">mNAV (EV basis)</span><span><a class="link" href="https://www.strategy.com/" target="_blank" style="font-size:13px;font-weight:600;color:var(--accent)">Live on strategy.com ↗</a></span></div>
  <div class="mrow"><span class="ml">MSTR stock</span><span><span class="mv">{fmt_usd(mstr.get("stock_price"), 2)}</span><span class="ms">{fmt_pct(eq("MSTR").get("change_pct")) + " today" if eq("MSTR").get("change_pct") else ""}</span></span></div>
  <div class="mrow"><span class="ml">Latest purchase</span><span><span class="mv">535 BTC</span><span class="ms">{mstr["latest_purchase"]}</span></span></div>
</div>
<div style="margin-top:8px;font-size:11px">
  <a class="link" href="https://www.strategy.com/purchases" target="_blank">strategy.com/purchases ↗</a>
  &nbsp;·&nbsp;
  <a class="link" href="https://www.strategy.com/" target="_blank">strategy.com (live mNAV) ↗</a>
</div>

<div class="footer">
  Generated: {generated_at} HKT &nbsp;·&nbsp;
  Sources: Binance (crypto) · CoinGecko (metals/HYPE) · yfinance (equities/yields) · Deribit public API (DVOL) · Strategy.com (MSTR) &nbsp;·&nbsp;
  Not financial advice.
</div>

</div>
</body>
</html>"""
    return html


# ── Main ───────────────────────────────────────────────────────────────────────
def main():
    now_hkt = datetime.now(HKT)
    generated_at = now_hkt.strftime("%Y-%m-%d %H:%M HKT")
    print(f"\n{'='*55}")
    print(f"  Market Dashboard Generator — {generated_at}")
    print(f"{'='*55}")

    print("\n[1/7] Fetching crypto prices (Binance)...")
    crypto = fetch_crypto()
    print(f"      BTC={crypto.get('BTC',{}).get('price')} ETH={crypto.get('ETH',{}).get('price')} SOL={crypto.get('SOL',{}).get('price')} HYPE={crypto.get('HYPE',{}).get('price')}")

    print("\n[2/7] Fetching metals (CoinGecko)...")
    metals = fetch_metals()
    print(f"      Gold={metals.get('gold',{}).get('price')} Silver={metals.get('silver',{}).get('price')} Oil={metals.get('oil',{}).get('price')}")

    print("\n[3/7] Fetching equities & yields (yfinance)...")
    equities = fetch_equities()
    print(f"      GOOGL={equities.get('GOOGL',{}).get('price')} NVDA={equities.get('NVDA',{}).get('price')} HSI={equities.get('^HSI',{}).get('price')}")

    # Patch metals with yfinance futures if CoinGecko didn't provide them
    if metals.get("silver", {}).get("price") is None and equities.get("SI=F", {}).get("price"):
        metals["silver"] = {"price": equities["SI=F"]["price"], "change_pct": equities["SI=F"].get("change_pct")}
    if metals.get("oil", {}).get("price") is None and equities.get("CL=F", {}).get("price"):
        metals["oil"] = {"price": equities["CL=F"]["price"], "change_pct": equities["CL=F"].get("change_pct")}
    print(f"      Silver(patched)={metals.get('silver',{}).get('price')} Oil(patched)={metals.get('oil',{}).get('price')}")

    print("\n[4/7] Fetching Deribit DVOL...")
    dvol = fetch_dvol()
    print(f"      DVOL={dvol.get('value') if dvol else 'unavailable'}")

    print("\n[5/7] Fetching BTC cycle indices (Binance klines)...")
    btc_price  = crypto.get("BTC", {}).get("price")
    btc_indices = fetch_btc_indices(btc_price)
    print(f"      ahr999={btc_indices.get('ahr999')} 200wMA={btc_indices.get('ma_200w')} halving+{btc_indices.get('halving', {}).get('pct_gain')}%")

    print("\n[6/7] Fetching PreStocks pre-IPO token prices (DexScreener)...")
    prestocks = fetch_prestocks()
    for sym, v in prestocks.items():
        print(f"      {sym}=${v['price']} impl.val=${v['implied_val_b']}B chg={v['change_pct']}%")

    print("\n[7/7] Building MSTR treasury & rendering HTML...")
    mstr_stock = equities.get("MSTR", {}).get("price")
    mstr = fetch_mstr(btc_price, mstr_stock)

    html = render_html(crypto, metals, equities, dvol, mstr, btc_indices, prestocks, generated_at)

    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        f.write(html)

    print(f"\n  Dashboard saved to: {OUTPUT_PATH}")
    print(f"  File size: {len(html):,} bytes")
    print(f"\n{'='*55}\n")

if __name__ == "__main__":
    main()
