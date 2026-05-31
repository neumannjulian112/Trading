# 📈 Tagesaktie

Eine kleine Web-App, die **jeden Handelstag automatisch eine Beispielaktie** zum
Daytrading-Üben auswählt – **morgens Europa, mittags USA**. Komplett kostenlos auf
**GitHub Actions** + **GitHub Pages**.

> ⚠️ **Reines Lern-/Demo-Projekt für ein fiktives Depot. Keine Anlageempfehlung.**

## Was die App macht

**Phase 1 – vorbörslich (`screen`):**
- Screent die Watchlist und bildet die **Top 10** nach `Bewegung × relativem Volumen`.
- Holt für die Top 10 die aktuellen **Schlagzeilen** (über `yfinance`) und lässt sie
  per **Claude-API** bewerten: Stimmung, Richtung, Wichtigkeit, Katalysator (ja/nein)
  – inklusive einer kurzen Begründung.
- Werte mit **starkem frischem Katalysator** bekommen eine **kürzere 5-Minuten-Eröffnungs­spanne**
  (bei solchen News startet die Bewegung oft sofort), alle anderen die übliche **15-Minuten-Spanne**.
- Schreibt `watch.json` (Beobachtungsliste) und einen vorbörslichen Favoriten in `pick.json`.

**Phase 2 – nach Eröffnung (`breakout`):**
- Holt für die 10 Werte die **Intraday-Daten**, berechnet je Wert die Eröffnungsspanne
  und prüft, **wer nach oben ausgebrochen ist**.
- Bewertet die **Signal-Qualität** über drei Filter, die Fehlausbrüche reduzieren:
  **VWAP** (Kurs über dem VWAP?), **Ausbruch-Volumen** (mehr Volumen nach der Spanne als
  währenddessen?) und **Tagestrend** (Schluss über der 20-Tage-Linie?). Empfohlen wird der
  Ausbruch mit der höchsten Qualität.
- Liefert einen konkreten **Trade-Plan**: Einstieg, Stop (= Spannen-Tief), Ziele (1,5R & 2R)
  und die **Positionsgröße** aus einem festen Demo-Risiko je Trade. Auf der Seite lassen
  sich Depotgröße und Risiko-% live anpassen.

Die Seite (`index.html`) zeigt den Pick, die KI-Nachrichten­lage, den Trading-Plan,
den Ausbruch-Status und die Top-10-Liste.

## Wie funktioniert die Daytrading-Idee

Opening Range Breakout: die ersten Minuten nach Eröffnung Hoch/Tief der Kursspanne
merken, dann **long gehen, wenn der Kurs über das Spannen-Hoch ausbricht** – Stop-Loss
knapp unter das Spannen-Tief, Ziel ca. 1,5–2× des Risikos, spätestens zum Schluss
schließen. Die App liefert Kandidat + Plan; den eigentlichen Einstieg liest du **live**
am Demo-Depot ab.

---

## Einrichtung

### 1. Repo anlegen
Alle Dateien in ein **neues GitHub-Repository** hochladen (Struktur behalten,
besonders `.github/workflows/`).

### 2. Schreibrechte für Actions
**Settings → Actions → General → Workflow permissions → „Read and write permissions".**

### 3. Claude-API-Key hinterlegen (für die News-Bewertung)
- Key erstellen unter **console.anthropic.com → API Keys**.
- Im Repo unter **Settings → Secrets and variables → Actions → New repository secret**
  einen Eintrag anlegen:
  - **Name:** `ANTHROPIC_API_KEY`
  - **Secret:** dein Key
- Kosten sind minimal: ein paar Schlagzeilen × 10 Werte × 2 Läufe/Tag mit dem
  günstigen Haiku-Modell → wenige Cent im Monat.
- **Ohne** diesen Key läuft alles trotzdem – nur ohne News-Bewertung (Auswahl dann
  rein technisch).

### 4. GitHub Pages aktivieren
**Settings → Pages → Source: „Deploy from a branch"**, Branch `main`, Ordner `/ (root)`.
Seite: `https://DEIN-NAME.github.io/DEIN-REPO/`

### 5. Testlauf
**Actions → „Tagesaktie" → „Run workflow"** → Region + Phase wählen.
Zum Testen am besten erst `screen`, dann `breakout` (während/nach Börsenzeit, damit
Intraday-Daten existieren).

---

## Zeitpläne (UTC, in `screen.yml`)

| Lauf | Cron (UTC) | ~Deutsche Zeit (Sommer) | Zweck |
|------|-----------|--------------------------|-------|
| Europa screen | `30 6 * * 1-5` | 08:30 | Top 10 + News, vor Xetra-Eröffnung |
| Europa breakout | `30 7 * * 1-5` | 09:30 | Ausbruch-Check nach der Spanne |
| USA screen | `0 13 * * 1-5` | 15:00 | Top 10 + News, vor US-Eröffnung |
| USA breakout | `0 14 * * 1-5` | 16:00 | Ausbruch-Check nach der Spanne |

---

## Anpassen

- **Watchlist:** `watchlists.json` (Ticker im Yahoo-Format: `.DE` Xetra, `.PA` Paris,
  `.AS` Amsterdam, ohne Suffix = USA).
- **Auswahllogik:** `screener.py` → `screen_top10` (Score) bzw. `combined_favorite`
  (Katalysator-Gewichtung).
- **Katalysator-Regel:** `assign_or_window` entscheidet 5 vs. 15 Min.
- **Demo-Risiko / Trade-Plan:** `DEMO_ACCOUNT`, `RISK_PCT` und `TARGET_R` oben in `screener.py`
  (Depotgröße, Risiko je Trade, Gewinnziele als R-Vielfaches).
- **Zeiten:** `cron`-Zeilen in `screen.yml`.

## Gut zu wissen

- **Daten:** `yfinance` ist eine inoffizielle Yahoo-Schnittstelle und meist ~15 Min
  verzögert. Für ein Übungs-Depot völlig ausreichend; der Auto-Check bildet die ersten
  Minuten nach Eröffnung aber nicht live ab – dein Live-Depot ist nicht verzögert.
- **Nachrichten** sind ein Kontext-/Katalysator-Layer, **kein Prognose-Orakel**.
- **Zeitzone:** Cron läuft in UTC ohne Sommer-/Winterzeit-Anpassung (±1 h, Puffer reicht).
- **Pünktlichkeit:** Geplante Workflows können bei hoher Last einige Minuten später starten.
- **Inaktivität:** GitHub deaktiviert geplante Workflows nach 60 Tagen ohne Aktivität –
  ein manueller „Run workflow" reaktiviert sie.
- **Resilienz:** Bekommt ein Lauf keine Daten, bleibt die letzte `pick.json` stehen.

## Dateien

| Datei | Zweck |
|-------|-------|
| `index.html` | Frontend |
| `screener.py` | Screening + News + Ausbruch-Check (beide Phasen) |
| `watchlists.json` | Handelbare Werte je Region |
| `watch.json` | Beobachtungsliste (Top 10, von Phase 1) |
| `pick.json` | Aktueller Pick (Anzeige) |
| `requirements.txt` | Python-Abhängigkeiten (`yfinance`, `anthropic`) |
| `.github/workflows/screen.yml` | Zeitpläne + Automatik |

Viel Spaß auf dem Weg zur ersten (fiktiven) Million. 🏴‍☠️
