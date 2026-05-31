#!/usr/bin/env python3
"""
Backtest fuer das Tagesaktie-Tool
=================================
Rechnet nach, wie das Tool in den letzten N Handelstagen performt haette.

WICHTIG - kein Lookahead:
  * Die Auswahl der Top 10 fuer Tag D nutzt AUSSCHLIESSLICH Tagesdaten bis
    einschliesslich D-1 (Vortagesschluss, Volumen, 20-Tage-Linie).
  * Erst danach wird Tag D selbst gehandelt: zur "Check-Zeit" (kurz nach der
    Eroeffnungsspanne) wird geschaut, welcher der 10 ueber sein Spannen-Hoch
    ausgebrochen ist -> der staerkste Ausbruch wird gekauft. Der Verlauf danach
    (Stop / Ziel / Schluss) wird Bar fuer Bar in zeitlicher Reihenfolge simuliert.

Annahme laut Vorgabe: Start 10.000, jeden Tag ALLES auf eine Karte (eine Aktie).
Bei "kein Ausbruch" bleibt das Kapital an dem Tag unveraendert (kein Trade).

EHRLICHE EINSCHRAENKUNGEN:
  * Die News-/Katalysator-Schicht des Live-Tools fehlt hier (historische
    Schlagzeilen sind nicht verfuegbar) -> Auswahl rein technisch, Spanne immer 15 Min.
  * Intraday wird mit 5-Minuten-Bars simuliert (yfinance-Limit fuer Historie).
    Echte Fills, Spreads und Slippage sind NICHT modelliert -> reale Ergebnisse
    waeren tendenziell etwas schlechter.
  * "Alles auf eine Karte" ist maximale Schwankung. Der Stop begrenzt den
    Tagesverlust, aber das ist KEINE realistische Risikosteuerung.

Aufruf:
  python backtest.py                  # Europa, letzte 10 Handelstage, 10.000
  python backtest.py --region usa --days 10 --capital 10000 --target-r 2.0
"""

import argparse
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import yfinance as yf

WATCHLIST = {
    "europe": ["RHM.DE","ENR.DE","SAP.DE","SIE.DE","ALV.DE","MBG.DE","BMW.DE","VOW3.DE",
               "P911.DE","BAS.DE","BAYN.DE","DTE.DE","DBK.DE","IFX.DE","ADS.DE","DHL.DE",
               "MUV2.DE","AIR.PA","ASML.AS","MC.PA"],
    "usa": ["NVDA","TSLA","AAPL","MSFT","AMD","AMZN","META","GOOGL","NFLX","COIN","PLTR",
            "MSTR","MARA","SMCI","SOFI","INTC","BA","F","NIO","GME"],
}
MARKET = {
    "europe": {"tz": "Europe/Berlin", "open": (9, 0), "ccy": "EUR"},
    "usa":    {"tz": "America/New_York", "open": (9, 30), "ccy": "USD"},
}
OR_WINDOW_MIN = 15        # Eroeffnungsspanne (ohne News immer 15 Min)
CHECK_OFFSET_MIN = 15     # Ausbruch-Check ~15 Min nach Ende der Spanne (wie ~09:30 live)
TOP_N = 10


def screen_topN(daily_by_ticker, day, n=TOP_N):
    """Top N nach |Tagesbewegung| x rel. Volumen, NUR mit Daten < day."""
    rows = []
    for tk, df in daily_by_ticker.items():
        sub = df[df.index.date < day]
        if len(sub) < 22:
            continue
        closes, vols = sub["Close"], sub["Volume"]
        prev_close = float(closes.iloc[-2])
        last_close = float(closes.iloc[-1])
        if prev_close <= 0:
            continue
        change = (last_close / prev_close - 1) * 100
        avg_vol = float(vols.iloc[-21:-1].mean())
        last_vol = float(vols.iloc[-1])
        if avg_vol <= 0 or last_vol <= 0:
            continue
        rel_vol = last_vol / avg_vol
        sma20 = float(closes.iloc[-20:].mean())
        rows.append({"ticker": tk, "score": abs(change) * rel_vol,
                     "trend_up": last_close > sma20})
    rows.sort(key=lambda r: r["score"], reverse=True)
    return rows[:n]


def day_bars(intraday_by_ticker, tk, day, tz):
    df = intraday_by_ticker.get(tk)
    if df is None or df.empty:
        return None
    idx = df.index.tz_convert(tz)
    d = df.copy(); d.index = idx
    d = d[d.index.date == day]
    return d if not d.empty else None


def simulate_day(intraday_by_ticker, candidates, day, mkt, target_r):
    """Waehlt den staerksten Ausbruch zur Check-Zeit und simuliert den Trade."""
    tz = ZoneInfo(mkt["tz"])
    oh, om = mkt["open"]
    open_dt = datetime(day.year, day.month, day.day, oh, om, tzinfo=tz)
    or_end = open_dt + timedelta(minutes=OR_WINDOW_MIN)
    check_dt = or_end + timedelta(minutes=CHECK_OFFSET_MIN)

    best = None
    for c in candidates:
        bars = day_bars(intraday_by_ticker, c["ticker"], day, tz)
        if bars is None:
            continue
        orb = bars[(bars.index >= open_dt) & (bars.index < or_end)]
        post = bars[bars.index >= check_dt]
        if orb.empty or post.empty:
            continue
        or_high = float(orb["High"].max())
        or_low = float(orb["Low"].min())
        price_at_check = float(post["Open"].iloc[0])
        if or_high <= or_low:
            continue
        if price_at_check > or_high:   # Ausbruch nach oben zur Check-Zeit
            strength = (price_at_check - or_high) / (or_high - or_low)
            cand = {"ticker": c["ticker"], "entry": price_at_check, "stop": or_low,
                    "strength": strength, "trend_up": c["trend_up"],
                    "post": post, "or_high": or_high}
            # bevorzuge hoehere Staerke, Trend als Tiebreak
            key = (round(strength, 4), 1 if c["trend_up"] else 0)
            if best is None or key > best["_key"]:
                cand["_key"] = key
                best = cand
    if best is None:
        return None

    entry = best["entry"]; stop = best["stop"]
    risk = entry - stop
    target = entry + target_r * risk
    exit_price, reason = None, None
    for _, bar in best["post"].iterrows():
        if float(bar["Low"]) <= stop:
            exit_price, reason = stop, "Stop"
            break
        if float(bar["High"]) >= target:
            exit_price, reason = target, "Ziel"
            break
    if exit_price is None:
        exit_price = float(best["post"]["Close"].iloc[-1])
        reason = "Schluss"
    return {"ticker": best["ticker"], "entry": round(entry, 2), "exit": round(exit_price, 2),
            "reason": reason, "ret": exit_price / entry - 1, "trend_up": best["trend_up"]}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--region", choices=["europe", "usa"], default="europe")
    ap.add_argument("--days", type=int, default=10)
    ap.add_argument("--capital", type=float, default=10000.0)
    ap.add_argument("--target-r", type=float, default=2.0)
    args = ap.parse_args()

    region, mkt = args.region, MARKET[args.region]
    tickers = WATCHLIST[region]
    tz = ZoneInfo(mkt["tz"])

    print(f"Lade Tagesdaten ({len(tickers)} Werte) ...")
    daily = yf.download(tickers, period="4mo", interval="1d", group_by="ticker",
                        auto_adjust=False, progress=False, threads=True)
    daily_by = {}
    for tk in tickers:
        try:
            daily_by[tk] = daily[tk].dropna(subset=["Close", "Volume"])
        except Exception:
            pass

    print("Lade 5-Minuten-Daten (letzte ~60 Tage) ...")
    intra = yf.download(tickers, period="60d", interval="5m", group_by="ticker",
                        auto_adjust=False, progress=False, threads=True)
    intra_by = {}
    for tk in tickers:
        try:
            d = intra[tk].dropna(subset=["Open", "High", "Low", "Close"])
            intra_by[tk] = d if not d.empty else None
        except Exception:
            intra_by[tk] = None

    # Handelstage aus den Intraday-Daten ableiten (nur Tage, fuer die wir Intraday haben)
    all_days = sorted({ts.tz_convert(tz).date()
                       for tk in tickers if intra_by.get(tk) is not None
                       for ts in intra_by[tk].index})
    test_days = all_days[-args.days:]

    print("\n" + "=" * 74)
    print(f"BACKTEST  |  {region.upper()}  |  Start {args.capital:,.0f} {mkt['ccy']}  |  "
          f"all-in/Tag  |  Ziel {args.target_r}R")
    print("Auswahl rein technisch (ohne News), Spanne 15 Min, 5-Min-Sim, ohne Slippage.")
    print("=" * 74)
    hdr = f"{'Datum':<11}{'Aktie':<9}{'Einstieg':>10}{'Ausstieg':>10}{'Exit':>9}{'Tag %':>9}{'Kapital':>13}"
    print(hdr); print("-" * 74)

    equity = args.capital
    wins = losses = flats = 0
    rets = []
    for day in test_days:
        top = screen_topN(daily_by, day)
        trade = simulate_day(intra_by, top, day, mkt, args.target_r) if top else None
        if trade is None:
            flats += 1
            print(f"{day.isoformat():<11}{'(flat)':<9}{'':>10}{'':>10}{'kein A.':>9}{'0.00%':>9}{equity:>13,.0f}")
            continue
        equity *= (1 + trade["ret"])
        rets.append(trade["ret"])
        if trade["ret"] > 0: wins += 1
        else: losses += 1
        print(f"{day.isoformat():<11}{trade['ticker']:<9}{trade['entry']:>10.2f}"
              f"{trade['exit']:>10.2f}{trade['reason']:>9}{trade['ret']*100:>8.2f}%{equity:>13,.0f}")

    print("-" * 74)
    total = equity / args.capital - 1
    print(f"\nEndkapital: {equity:,.0f} {mkt['ccy']}   (Gesamt {total*100:+.1f} %)")
    n_tr = wins + losses
    if n_tr:
        print(f"Trades: {n_tr}  |  Gewinner: {wins}  Verlierer: {losses}  "
              f"Trefferquote: {wins/n_tr*100:.0f}%  |  flache Tage: {flats}")
        print(f"Bester Tag: {max(rets)*100:+.2f}%   Schlechtester Tag: {min(rets)*100:+.2f}%")
    else:
        print(f"Keine Trades in diesem Zeitraum (alle Tage flach: {flats}).")
    print("\nHinweis: reines Lern-/Demo-Ergebnis, keine Anlageempfehlung. Eine kurze "
          "Stichprobe ist statistisch NICHT aussagekraeftig.")


if __name__ == "__main__":
    main()
