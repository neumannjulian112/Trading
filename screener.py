#!/usr/bin/env python3
"""
Tagesaktie-Screener (v3)
========================
Zwei Phasen (screen / breakout) wie zuvor, jetzt zusaetzlich mit:

  * Signal-Qualitaet beim Ausbruch  ->  drei Filter, die Fehlausbrueche reduzieren:
      - VWAP-Filter          : Long nur, wenn Kurs ueber dem VWAP liegt
      - Ausbruch-Volumen     : Volumen nach der Spanne hoeher als waehrend der Spanne
      - Tagestrend           : Tagesschluss ueber der 20-Tage-Linie (Aufwaertstrend)
  * Trade-Plan               ->  Einstieg / Stop / Ziele (1,5R & 2R) / Positionsgroesse
                                 aus einem fixen Demo-Risiko je Trade.

Datenquelle Kurse: yfinance. News-Bewertung: Anthropic-API (optional, via Key).
Reines Lern-/Demo-Projekt. KEINE Anlageempfehlung.
"""

import argparse
import json
import math
import os
import re
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import yfinance as yf

BASE_DIR = Path(__file__).resolve().parent
WATCHLIST_FILE = BASE_DIR / "watchlists.json"
WATCH_FILE = BASE_DIR / "watch.json"
PICK_FILE = BASE_DIR / "pick.json"
TZ = ZoneInfo("Europe/Berlin")

TOP_N = 10
NEWS_MAX_AGE_HOURS = 36
AI_MODEL = "claude-haiku-4-5-20251001"

# --- Demo-Risiko fuer den Trade-Plan (frei anpassbar) ---------------------- #
DEMO_ACCOUNT = 100_000.0   # fiktive Depotgroesse
RISK_PCT = 1.0             # Risiko je Trade in Prozent des Depots
TARGET_R = (1.5, 2.0)      # Gewinnziele als Vielfaches des Risikos

REGION_META = {
    "europe": {"label": "Europa", "flag": "EU", "currency": "EUR", "market": "Xetra",
               "market_tz": "Europe/Berlin", "open_time": (9, 0), "de_open": (9, 0),
               "open_local": "09:00 Uhr"},
    "usa": {"label": "USA", "flag": "US", "currency": "USD", "market": "NYSE / Nasdaq",
            "market_tz": "America/New_York", "open_time": (9, 30), "de_open": (15, 30),
            "open_local": "15:30 Uhr"},
}

ORB_LOGIC = (
    "Eroeffnungsspanne (Hoch/Tief des Zeitfensters) merken. Long-Signal beim Bruch "
    "ueber das Spannen-Hoch, Stop-Loss knapp unter das Spannen-Tief, Ziel rund "
    "1,5\u20132\u00d7 des Risikos. Position spaetestens zum Handelsschluss schliessen."
)


# --------------------------------------------------------------------------- #
# Hilfen
# --------------------------------------------------------------------------- #
def now_utc():
    return datetime.now(timezone.utc)


def local_stamp():
    return now_utc().astimezone(TZ).strftime("%d.%m.%Y, %H:%M Uhr")


def detect_region_by_time():
    return "europe" if now_utc().hour < 12 else "usa"


def load_watchlist(region):
    data = json.loads(WATCHLIST_FILE.read_text(encoding="utf-8"))
    if region not in data:
        sys.exit(f"Region '{region}' nicht in {WATCHLIST_FILE.name} gefunden.")
    return {k: v for k, v in data[region].items() if not k.startswith("_")}


def build_orb(region, window_min):
    meta = REGION_META[region]
    h, m = meta["de_open"]
    end_total = h * 60 + m + window_min
    end_txt = f"{end_total // 60:02d}:{end_total % 60:02d} Uhr"
    return {"market": meta["market"], "open_local": meta["open_local"], "window_min": window_min,
            "range_window": f"{meta['open_local'].replace(' Uhr', '')}\u2013{end_txt}",
            "entry_decision": f"ab {end_txt}", "logic": ORB_LOGIC}


# --------------------------------------------------------------------------- #
# Phase 1: Screening + Tagestrend
# --------------------------------------------------------------------------- #
def screen_top10(region):
    names = load_watchlist(region)
    tickers = list(names.keys())
    print(f"[{region}/screen] Lade Tagesdaten fuer {len(tickers)} Werte ...")
    raw = yf.download(tickers, period="2mo", interval="1d", group_by="ticker",
                      auto_adjust=False, progress=False, threads=True)
    rows = []
    for t in tickers:
        try:
            df = raw[t].dropna(subset=["Close", "Volume"])
        except Exception:
            continue
        if len(df) < 2:
            continue
        last, prev = df.iloc[-1], df.iloc[-2]
        prev_close = float(prev["Close"])
        if prev_close <= 0:
            continue
        last_close = float(last["Close"])
        change_pct = (last_close / prev_close - 1.0) * 100.0
        last_vol = float(last["Volume"])
        avg_vol = float(df["Volume"].iloc[:-1].mean())
        if avg_vol <= 0 or last_vol <= 0:
            continue
        rel_vol = last_vol / avg_vol
        sma20 = float(df["Close"].tail(20).mean())
        rows.append({
            "ticker": t, "name": names[t], "last_price": round(last_close, 2),
            "previous_close": round(prev_close, 2), "change_pct": round(change_pct, 2),
            "volume": int(last_vol), "avg_volume": int(avg_vol), "rel_volume": round(rel_vol, 2),
            "score": round(abs(change_pct) * rel_vol, 2),
            "sma20": round(sma20, 2), "trend_up": bool(last_close > sma20),
            "session_date": df.index[-1].date().isoformat(),
        })
    rows.sort(key=lambda r: r["score"], reverse=True)
    return rows[:TOP_N]


# --------------------------------------------------------------------------- #
# News (yfinance) + KI-Bewertung (Anthropic)
# --------------------------------------------------------------------------- #
def fetch_headlines(ticker, limit=5):
    try:
        items = yf.Ticker(ticker).news or []
    except Exception:
        return []
    cutoff = now_utc() - timedelta(hours=NEWS_MAX_AGE_HOURS)
    out = []
    for it in items:
        title, ts, url = None, None, None
        if "title" in it:
            title = it.get("title"); url = it.get("link")
            if it.get("providerPublishTime"):
                ts = datetime.fromtimestamp(it["providerPublishTime"], tz=timezone.utc)
        elif isinstance(it.get("content"), dict):
            c = it["content"]; title = c.get("title")
            url = (c.get("canonicalUrl") or {}).get("url") or (c.get("clickThroughUrl") or {}).get("url")
            pub = c.get("pubDate") or c.get("displayTime")
            if pub:
                try:
                    ts = datetime.fromisoformat(pub.replace("Z", "+00:00"))
                except Exception:
                    ts = None
        if not title:
            continue
        if ts and ts < cutoff:
            continue
        out.append({"title": title.strip(), "url": url})
        if len(out) >= limit:
            break
    return out


def _parse_json(text):
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.MULTILINE).strip()
    m = re.search(r"\{.*\}", text, flags=re.DOTALL)
    return json.loads(m.group(0) if m else text)


def rank_news_with_ai(stocks):
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("Hinweis: ANTHROPIC_API_KEY nicht gesetzt -> ueberspringe News-Bewertung.")
        return {}
    payload = {}
    for s in stocks:
        hl = fetch_headlines(s["ticker"])
        if hl:
            payload[s["ticker"]] = {"name": s["name"], "headlines": [h["title"] for h in hl],
                                    "_url": hl[0]["url"], "_headline": hl[0]["title"]}
    if not payload:
        print("Keine frischen Schlagzeilen gefunden.")
        return {}
    compact = {k: {"name": v["name"], "headlines": v["headlines"]} for k, v in payload.items()}
    system = ("Du bist ein nuechterner Finanz-Analyst. Du bewertest Schlagzeilen zu Aktien "
              "ausschliesslich nach Kursrelevanz und Richtung. Antworte AUSSCHLIESSLICH mit "
              "gueltigem JSON, ohne Erklaerung und ohne Markdown-Zaeune.")
    user = ("Bewerte fuer JEDEN Ticker die Schlagzeilen. Gib ein JSON-Objekt zurueck, das jeden "
            "Ticker auf ein Objekt mit diesen Feldern abbildet:\n"
            '  "sentiment": "positiv" | "negativ" | "neutral",\n'
            '  "direction": "auf" | "ab" | "unklar",\n'
            '  "materiality": "hoch" | "mittel" | "gering",\n'
            '  "catalyst": true | false   (true NUR bei frischer, wesentlicher, kursbewegender '
            "Nachricht: Zahlen, Guidance, Uebernahme, Grossauftrag, Rueckruf o.ae.),\n"
            '  "summary": "ein kurzer deutscher Satz, warum".\n\n'
            "Daten:\n" + json.dumps(compact, ensure_ascii=False))
    try:
        import anthropic
        client = anthropic.Anthropic()
        msg = client.messages.create(model=AI_MODEL, max_tokens=1500, system=system,
                                     messages=[{"role": "user", "content": user}])
        text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
        parsed = _parse_json(text)
    except Exception as e:
        print(f"News-Bewertung fehlgeschlagen ({e}) -> ohne News weiter.")
        return {}
    for tk, v in parsed.items():
        if tk in payload and isinstance(v, dict):
            v["headline"] = payload[tk]["_headline"]; v["url"] = payload[tk]["_url"]
    print(f"News-Bewertung erhalten fuer {len(parsed)} Werte.")
    return parsed


def assign_or_window(news):
    if news and news.get("catalyst") and news.get("materiality") == "hoch":
        return 5
    return 15


def combined_favorite(stocks, news_by_ticker):
    def boost(tk):
        n = news_by_ticker.get(tk, {}) or {}
        if n.get("catalyst") and n.get("direction") == "auf":
            return 1.5 if n.get("materiality") == "hoch" else 1.2
        return 1.0
    return sorted(stocks, key=lambda s: s["score"] * boost(s["ticker"]), reverse=True)[0]["ticker"]


# --------------------------------------------------------------------------- #
# Phase 2: Ausbruch-Check inkl. VWAP & Ausbruch-Volumen
# --------------------------------------------------------------------------- #
def intraday_metrics(ticker, region, window_min):
    """Eroeffnungsspanne, aktueller Kurs, VWAP und Ausbruch-Volumen-Verhaeltnis."""
    meta = REGION_META[region]
    mtz = ZoneInfo(meta["market_tz"])
    try:
        df = yf.download(ticker, period="1d", interval="1m", auto_adjust=False,
                         progress=False, threads=False)
    except Exception:
        return None
    if df is None or df.empty:
        return None
    if df.columns.nlevels > 1:
        df.columns = df.columns.get_level_values(-1)
    try:
        idx = df.index.tz_convert(mtz)
    except Exception:
        try:
            idx = df.index.tz_localize("UTC").tz_convert(mtz)
        except Exception:
            return None
    df = df.copy(); df.index = idx

    oh, om = meta["open_time"]
    today = idx[-1].date()
    start = datetime(today.year, today.month, today.day, oh, om, tzinfo=mtz)
    end = start + timedelta(minutes=window_min)
    win = df[(df.index >= start) & (df.index < end)]
    if win.empty:
        return None

    # VWAP ueber den bisherigen Handelstag
    tp = (df["High"] + df["Low"] + df["Close"]) / 3.0
    vol_sum = float(df["Volume"].sum())
    vwap = float((tp * df["Volume"]).sum() / vol_sum) if vol_sum > 0 else None

    # Ausbruch-Volumen: Bars nach der Spanne vs. Bars in der Spanne
    after = df[df.index >= end]
    rng_vol = float(win["Volume"].mean())
    vol_ratio = None
    if not after.empty and rng_vol > 0:
        vol_ratio = round(float(after["Volume"].mean()) / rng_vol, 2)

    return {
        "range_high": round(float(win["High"].max()), 2),
        "range_low": round(float(win["Low"].min()), 2),
        "current_price": round(float(df["Close"].iloc[-1]), 2),
        "vwap": round(vwap, 2) if vwap else None,
        "breakout_vol_ratio": vol_ratio,
    }


def build_trade_plan(entry, stop, currency):
    risk_ps = round(entry - stop, 2)
    if risk_ps <= 0:
        return None
    risk_amount = DEMO_ACCOUNT * RISK_PCT / 100.0
    size = int(math.floor(risk_amount / risk_ps))
    return {
        "entry": round(entry, 2), "stop": round(stop, 2), "risk_per_share": risk_ps,
        "target_1": round(entry + TARGET_R[0] * risk_ps, 2),
        "target_2": round(entry + TARGET_R[1] * risk_ps, 2),
        "target_r": list(TARGET_R),
        "account": DEMO_ACCOUNT, "risk_pct": RISK_PCT,
        "position_size": size, "capital": round(size * entry, 0), "currency": currency,
    }


def _watch_summary(stocks):
    return [{"ticker": s["ticker"], "name": s["name"], "change_pct": s["change_pct"],
             "rel_volume": s["rel_volume"], "catalyst": bool((s.get("news") or {}).get("catalyst")),
             "status": s.get("status")} for s in stocks[:6]]


def breakout_check(region):
    if not WATCH_FILE.exists():
        print("watch.json fehlt - bitte zuerst die screen-Phase laufen lassen.")
        return None
    watch = json.loads(WATCH_FILE.read_text(encoding="utf-8"))
    stocks = watch.get("watchlist", [])
    print(f"[{region}/breakout] Pruefe {len(stocks)} Werte ...")

    results = []
    for s in stocks:
        m = intraday_metrics(s["ticker"], region, s.get("or_window_min", 15))
        if not m:
            continue
        cur, hi, lo = m["current_price"], m["range_high"], m["range_low"]
        span = max(hi - lo, 1e-6)
        if cur > hi:
            status, strength = "ausbruch_auf", (cur - hi) / span
        elif cur < lo:
            status, strength = "ausbruch_ab", (lo - cur) / span
        else:
            status, strength = "in_spanne", 0.0
        # Signal-Qualitaet (None = unbekannt)
        above_vwap = (cur > m["vwap"]) if m["vwap"] is not None else None
        vol_conf = (m["breakout_vol_ratio"] >= 1.0) if m["breakout_vol_ratio"] is not None else None
        trend_up = s.get("trend_up")
        quality = sum(1 for c in (above_vwap, vol_conf, trend_up) if c is True)
        results.append({**s, **m, "status": status, "breakout_strength": round(strength, 2),
                        "checks": {"vwap": above_vwap, "volume": vol_conf, "trend": trend_up},
                        "quality": quality})

    if not results:
        print("WARNUNG: keine Intraday-Daten - pick.json bleibt unveraendert.")
        return None

    longs = [r for r in results if r["status"] == "ausbruch_auf"]
    if longs:
        # erst nach Signal-Qualitaet, dann nach Staerke x Volumen
        longs.sort(key=lambda r: (r["quality"], r["breakout_strength"] * r["rel_volume"]), reverse=True)
        winner, overall = longs[0], "ausbruch_bestaetigt"
        passed = winner["quality"]
        rationale = (
            f"Long-Ausbruch ueber dem Spannen-Hoch ({winner['range_high']}); "
            f"{passed}/3 Qualitaetsfilter erfuellt (VWAP / Volumen / Trend)."
        )
        entry = max(winner["current_price"], winner["range_high"])
    else:
        results.sort(key=lambda r: (r["current_price"] - r["range_high"]), reverse=True)
        winner, overall = results[0], "kein_setup"
        rationale = (
            "Noch kein Long-Ausbruch in der Beobachtungsliste. Naechster Kandidat am "
            f"Spannen-Hoch: {winner['name']} (Spanne {winner['range_low']}\u2013"
            f"{winner['range_high']}, aktuell {winner['current_price']})."
        )
        entry = winner["range_high"]

    meta = REGION_META[region]
    trade_plan = build_trade_plan(entry, winner["range_low"], meta["currency"])
    orb = build_orb(region, winner.get("or_window_min", 15))
    orb.update({"range_high": winner["range_high"], "range_low": winner["range_low"],
                "current_price": winner["current_price"], "vwap": winner.get("vwap"),
                "breakout_status": winner["status"]})

    return {
        "region": region, "region_label": meta["label"], "flag": meta["flag"],
        "currency": meta["currency"], "phase": "breakout", "status": overall,
        "generated_at": now_utc().isoformat(), "generated_at_local": local_stamp(),
        "session_date": winner.get("session_date", ""),
        "ticker": winner["ticker"], "name": winner["name"], "last_price": winner["last_price"],
        "previous_close": winner["previous_close"], "change_pct": winner["change_pct"],
        "volume": winner["volume"], "avg_volume": winner["avg_volume"], "rel_volume": winner["rel_volume"],
        "rationale": rationale, "news": winner.get("news"),
        "or_window_min": winner.get("or_window_min", 15), "orb": orb,
        "checks": winner["checks"], "quality": winner["quality"], "trade_plan": trade_plan,
        "watchlist_top": _watch_summary(results),
        "disclaimer": "Beispielaktie zum \u00dcben \u2013 keine Anlageempfehlung.",
    }


# --------------------------------------------------------------------------- #
# Schreiben
# --------------------------------------------------------------------------- #
def run_screen(region):
    stocks = screen_top10(region)
    if not stocks:
        print("WARNUNG: keine Kursdaten - Dateien bleiben unveraendert.")
        sys.exit(0)
    news_by_ticker = rank_news_with_ai(stocks)
    for s in stocks:
        s["news"] = news_by_ticker.get(s["ticker"])
        s["or_window_min"] = assign_or_window(s["news"])
    fav = next(s for s in stocks if s["ticker"] == combined_favorite(stocks, news_by_ticker))
    meta = REGION_META[region]

    WATCH_FILE.write_text(json.dumps({
        "region": region, "region_label": meta["label"], "flag": meta["flag"],
        "currency": meta["currency"], "phase": "screen",
        "generated_at": now_utc().isoformat(), "generated_at_local": local_stamp(),
        "session_date": fav["session_date"], "favorite": fav["ticker"], "watchlist": stocks,
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    pick = {
        "region": region, "region_label": meta["label"], "flag": meta["flag"],
        "currency": meta["currency"], "phase": "screen", "status": "vorboerslich",
        "generated_at": now_utc().isoformat(), "generated_at_local": local_stamp(),
        "session_date": fav["session_date"], "ticker": fav["ticker"], "name": fav["name"],
        "last_price": fav["last_price"], "previous_close": fav["previous_close"],
        "change_pct": fav["change_pct"], "volume": fav["volume"], "avg_volume": fav["avg_volume"],
        "rel_volume": fav["rel_volume"],
        "rationale": ("Vorboerslicher Favorit aus der Top-10-Beobachtungsliste. Der bestaetigte "
                      "Pick mit Signal-Qualitaet und Trade-Plan folgt nach Eroeffnung."),
        "news": fav.get("news"), "or_window_min": fav["or_window_min"],
        "orb": build_orb(region, fav["or_window_min"]),
        "checks": {"vwap": None, "volume": None, "trend": fav.get("trend_up")},
        "quality": None, "trade_plan": None,
        "watchlist_top": _watch_summary(stocks),
        "disclaimer": "Beispielaktie zum \u00dcben \u2013 keine Anlageempfehlung.",
    }
    PICK_FILE.write_text(json.dumps(pick, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Top 10 + Favorit ({fav['ticker']}) geschrieben.")


def run_breakout(region):
    pick = breakout_check(region)
    if pick is None:
        sys.exit(0)
    PICK_FILE.write_text(json.dumps(pick, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Ausbruch-Check fertig: {pick['ticker']} ({pick['status']}, Qualitaet {pick['quality']}/3).")


def main():
    p = argparse.ArgumentParser(description="Tagesaktie-Screener")
    p.add_argument("--region", choices=["europe", "usa"], default=None)
    p.add_argument("--phase", choices=["screen", "breakout"], default="screen")
    args = p.parse_args()
    region = args.region or detect_region_by_time()
    run_screen(region) if args.phase == "screen" else run_breakout(region)


if __name__ == "__main__":
    main()
