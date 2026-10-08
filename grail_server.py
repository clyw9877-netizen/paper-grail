"""
Бумажный грааль: Nasdaq (NQ=F) и золото (GC=F).
Цены: публичный chart Yahoo, без ключа. Если Yahoo не ответил — фейк и пометка.
Не брокер. 1 позиция на инструмент. Полка, дырка слева, пробой одной свечой,
вход лимиткой на тень пробойной, стоп за тень, тейк 1:1, риск $200.
Два стопа и депозит $500 почти мёртв.
"""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

START = 500.0
RISK_USD = 200.0
COMMISSION = 1.40
DAILY_STOP = -200.0
SYMBOLS = ("NQ=F", "GC=F")

equity = START
day_pnl = 0.0
positions: dict[str, dict] = {}
closed: list[dict] = []
candles: dict[str, list[dict]] = {s: [] for s in SYMBOLS}
source = "нет данных"
seen: dict[str, object] = {}
day_key = time.strftime("%Y-%m-%d", time.gmtime())
live: dict[str, float] = {}
updated = "—"
lock = threading.Lock()


def fetch_yahoo(symbol: str, host: str) -> list[dict]:
    url = (
        f"https://{host}/v8/finance/chart/"
        + urllib.request.quote(symbol)
        + "?range=5d&interval=15m"
    )
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=12) as resp:
        data = json.loads(resp.read().decode())
    r = data["chart"]["result"][0]
    q = r["indicators"]["quote"][0]
    out = []
    for t, o, h, l, c in zip(r["timestamp"], q["open"], q["high"], q["low"], q["close"]):
        if None in (o, h, l, c):
            continue
        out.append({"t": t, "o": o, "h": h, "l": l, "c": c})
    return out[-41:]  # последняя свеча формируется: она идёт в живую цену


def fetch_stooq(symbol: str) -> list[dict]:
    # запасной источник, дневки, без ключа. NQ.F / GC.F.
    code = "nq.f" if symbol.startswith("NQ") else "gc.f"
    url = f"https://stooq.com/q/d/l/?s={code}&i=d"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=12) as resp:
        text = resp.read().decode()
    rows = []
    for line in text.strip().splitlines()[1:]:
        parts = line.split(",")
        if len(parts) < 5:
            continue
        o, h, l, c = map(float, parts[1:5])
        rows.append({"t": parts[0], "o": o, "h": h, "l": l, "c": c})
    return rows[-40:]


def fetch(symbol: str) -> tuple[list[dict], str]:
    errors = []
    for host in ("query1.finance.yahoo.com", "query2.finance.yahoo.com"):
        try:
            return fetch_yahoo(symbol, host), f"Yahoo {host} 15m"
        except Exception as exc:  # noqa: BLE001
            errors.append(str(exc))
    try:
        return fetch_stooq(symbol), "Stooq daily fallback"
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError("; ".join(errors + [str(exc)])) from exc


def signal(symbol: str) -> dict | None:
    """Полка, дырка слева, пробой одной свечой, вход на её тень, стоп за тенью, тейк 1:1."""
    cs = candles.get(symbol) or []
    if len(cs) < 12 or symbol in positions or day_pnl <= DAILY_STOP:
        return None
    br = cs[-2]
    cur = cs[-1]
    shelf = cs[-8:-2]
    gap = cs[-12:-8]
    body = abs(br["c"] - br["o"])
    wick_up = br["h"] - max(br["o"], br["c"])
    wick_dn = min(br["o"], br["c"]) - br["l"]
    if body <= 0:
        return None
    # шорт: полка сверху, дырка ниже полки, пробой вниз, тень не длиннее тела
    level = min(c["l"] for c in shelf)
    hole = max(c["h"] for c in gap) < level
    if br["c"] < level and br["o"] >= level and wick_up <= body and hole:
        entry = max(br["o"], br["c"])
        stop = br["h"]
        risk = stop - entry
        if risk <= 0 or cur["h"] < entry:
            return None
        return {"symbol": symbol, "side": "short", "entry": entry, "stop": stop, "take": entry - risk, "u": 0.0}
    # лонг: полка снизу, дырка выше, пробой вверх
    level = max(c["h"] for c in shelf)
    hole = min(c["l"] for c in gap) > level
    if br["c"] > level and br["o"] <= level and wick_dn <= body and hole:
        entry = min(br["o"], br["c"])
        stop = br["l"]
        risk = entry - stop
        if risk <= 0 or cur["l"] > entry:
            return None
        return {"symbol": symbol, "side": "long", "entry": entry, "stop": stop, "take": entry + risk, "u": 0.0}
    return None


def refresh() -> None:
    global source, equity, day_pnl, day_key, updated
    got = {}
    srcs = []
    for s in SYMBOLS:
        try:
            cs, src = fetch(s)
            if src.startswith("Yahoo") and len(cs) > 1:
                live[s] = cs[-1]["c"]  # живая цена из формирующейся свечи
                cs = cs[:-1]  # сделки — только по закрытым свечам
            elif cs:
                live[s] = cs[-1]["c"]
            got[s] = cs
            srcs.append(f"{s}: {src}")
        except Exception:  # noqa: BLE001
            srcs.append(f"{s}: источники молчат, лента старая")
    with lock:
        source = " | ".join(srcs)
        updated = time.strftime("%H:%M:%S UTC", time.gmtime())
        for s, p in positions.items():  # плавающий PnL каждый цикл
            if s in live:
                d = 1 if p["side"] == "long" else -1
                p["u"] = (live[s] - p["entry"]) / abs(p["entry"] - p["stop"]) * RISK_USD * d
                p["price"] = live[s]
        today = time.strftime("%Y-%m-%d", time.gmtime())
        if today != day_key:
            day_key, day_pnl = today, 0.0
        for s, cs in got.items():
            candles[s] = cs
        for s, cs in candles.items():
            if not cs or seen.get(s) == cs[-1]["t"]:
                continue  # новой закрытой свечи нет — ничего не делаем
            seen[s] = cs[-1]["t"]
            last = cs[-1]
            if s in positions:
                p = positions[s]
                hit_stop = last["l"] <= p["stop"] if p["side"] == "long" else last["h"] >= p["stop"]
                hit_take = last["h"] >= p["take"] if p["side"] == "long" else last["l"] <= p["take"]
                direction = 1 if p["side"] == "long" else -1
                if hit_stop or hit_take:
                    pnl = (-RISK_USD if hit_stop else RISK_USD) - COMMISSION
                    equity += pnl
                    day_pnl += pnl
                    closed.insert(0, {"symbol": s, "side": p["side"], "pnl": round(pnl, 2), "why": "стоп" if hit_stop else "тейк"})
                    del closed[30:]
                    del positions[s]
            if s not in positions:
                sig = signal(s)
                if sig:
                    equity -= COMMISSION
                    day_pnl -= COMMISSION
                    # лимитка налилась на этой свече; если та же свеча достала стоп — сразу стоп
                    if (last["h"] >= sig["stop"]) if sig["side"] == "short" else (last["l"] <= sig["stop"]):
                        pnl = -RISK_USD - COMMISSION
                        equity += pnl
                        day_pnl += pnl
                        closed.insert(0, {"symbol": s, "side": sig["side"], "pnl": round(pnl, 2), "why": "стоп на свече входа"})
                        del closed[30:]
                    else:
                        positions[s] = sig


def snapshot() -> dict:
    with lock:
        unreal = sum(p["u"] for p in positions.values())
        return {
            "equity": round(equity + unreal, 2),
            "pnl": round(equity + unreal - START, 2),
            "day_pnl": round(day_pnl, 2),
            "source": source,
            "updated": updated,
            "open": list(positions.values()),
            "closed": closed[:12],
            "note": "paper only",
        }


def loop() -> None:
    while True:
        try:
            refresh()
        except Exception as exc:  # noqa: BLE001
            print("refresh error:", exc, flush=True)
        time.sleep(20)


PAGE = """<!DOCTYPE html>
<html lang="ru"><head><meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>Paper Graal NQ GC</title>
<style>
body{margin:0;font-family:-apple-system,sans-serif;background:#101010;color:#f3f3f3}
main{max-width:760px;margin:0 auto;padding:18px 14px 40px}
.muted{color:#9a9a9a}.up{color:#3dd68c}.down{color:#ff5d5d}
.grid{display:grid;grid-template-columns:repeat(3,1fr);gap:8px}
.card{background:#1b1b1b;border-radius:14px;padding:12px;margin:8px 0}
.row{display:flex;justify-content:space-between;margin-top:4px;color:#ccc}
</style></head><body><main>
<h1>Paper Graal</h1>
<p class="muted">Полка, имбаланс, пробой, вход на тень, стоп за тенью, тейк 1:1. Риск $200. Не как на видео.</p>
<div class="grid">
<div class="card">депозит<b id="eq">—</b></div>
<div class="card">pnl<b id="pnl">—</b></div>
<div class="card">день<b id="day">—</b></div>
</div>
<p class="muted" id="src"></p>
<h2>Висит</h2><div id="live"></div>
<h2>Закрытые</h2><div id="hist"></div>
<script>
async function refresh(){
  const s = await (await fetch('/api/state')).json();
  document.getElementById('eq').textContent = s.equity.toFixed(2);
  const p = document.getElementById('pnl');
  p.textContent = (s.pnl>=0?'+':'') + s.pnl.toFixed(2);
  p.className = s.pnl>=0?'up':'down';
  const d = document.getElementById('day');
  d.textContent = (s.day_pnl>=0?'+':'') + s.day_pnl.toFixed(2);
  document.getElementById('src').textContent = s.source + ' · обновлено ' + s.updated;
  document.getElementById('live').innerHTML = s.open.map(x =>
    `<div class="card"><div class="row"><b class="${x.side=='long'?'up':'down'}">${x.symbol} ${x.side}</b><b class="${x.u>=0?'up':'down'}">${(x.u>=0?'+':'') + x.u.toFixed(2)}$</b></div>
     <div class="row"><span>сейчас</span><span>${(x.price ?? x.entry).toFixed(2)}</span></div>
     <div class="row"><span>вход</span><span>${x.entry.toFixed(2)}</span></div>
     <div class="row"><span>стоп</span><span>${x.stop.toFixed(2)}</span></div>
     <div class="row"><span>тейк 1:1</span><span>${x.take.toFixed(2)}</span></div></div>`).join('') || '<div class="card">скип</div>';
  document.getElementById('hist').innerHTML = s.closed.map(x =>
    `<div class="card"><div class="row"><span>${x.symbol} ${x.side} ${x.why}</span><b class="${x.pnl>=0?'up':'down'}">${x.pnl.toFixed(2)}</b></div></div>`).join('');
}
refresh(); setInterval(refresh, 5000);
</script></main></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        if self.path.startswith("/api/state"):
            body = json.dumps(snapshot()).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(PAGE.encode())

    def log_message(self, fmt: str, *args) -> None:
        return


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8080"))
    threading.Thread(target=loop, daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()
