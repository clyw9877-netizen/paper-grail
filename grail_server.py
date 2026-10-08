"""
Бумажный грааль: Nasdaq (NQ=F) и золото (GC=F).
Цены: публичный chart Yahoo, без ключа. Если Yahoo не ответил — фейк и пометка.
Не брокер. 1 позиция на инструмент, тейк 1:1, риск $10, комиссия $0.70 сторона.
"""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

START = 500.0
RISK_USD = 10.0
COMMISSION = 0.70
SYMBOLS = ("NQ=F", "GC=F")

equity = START
day_pnl = 0.0
positions: dict[str, dict] = {}
closed: list[dict] = []
candles: dict[str, list[dict]] = {s: [] for s in SYMBOLS}
source = "нет данных"
seen: dict[str, object] = {}
day_key = time.strftime("%Y-%m-%d", time.gmtime())
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
    return out[-41:-1]  # последняя 15m свеча ещё формируется — не берём


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
    cs = candles.get(symbol) or []
    if len(cs) < 6 or symbol in positions or day_pnl <= -50:
        return None
    level = round(cs[-4]["c"] / (25 if symbol.startswith("NQ") else 5)) * (25 if symbol.startswith("NQ") else 5)
    br, retest, trigger = cs[-3], cs[-2], cs[-1]
    if br["c"] > level and retest["l"] <= level + (level * 0.0004) and trigger["c"] > trigger["o"]:
        risk = trigger["c"] - retest["l"]
        if risk <= 0:
            return None
        return {"symbol": symbol, "side": "long", "entry": trigger["c"], "stop": retest["l"], "take": trigger["c"] + risk, "u": 0.0}
    if br["c"] < level and retest["h"] >= level - (level * 0.0004) and trigger["c"] < trigger["o"]:
        risk = retest["h"] - trigger["c"]
        if risk <= 0:
            return None
        return {"symbol": symbol, "side": "short", "entry": trigger["c"], "stop": retest["h"], "take": trigger["c"] - risk, "u": 0.0}
    return None


def refresh() -> None:
    global source, equity, day_pnl, day_key
    got = {}
    srcs = []
    for s in SYMBOLS:
        try:
            cs, src = fetch(s)
            got[s] = cs
            srcs.append(f"{s}: {src}")
        except Exception:  # noqa: BLE001
            srcs.append(f"{s}: источники молчат, лента старая")
    with lock:
        source = " | ".join(srcs)
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
                p["u"] = (last["c"] - p["entry"]) / abs(p["entry"] - p["stop"]) * RISK_USD * direction
                if hit_stop or hit_take:
                    pnl = (RISK_USD if hit_take else -RISK_USD) - COMMISSION
                    equity += pnl
                    day_pnl += pnl
                    closed.insert(0, {"symbol": s, "side": p["side"], "pnl": round(pnl, 2), "why": "тейк" if hit_take else "стоп"})
                    del closed[30:]
                    del positions[s]
            if s not in positions:
                sig = signal(s)
                if sig:
                    equity -= COMMISSION
                    day_pnl -= COMMISSION
                    positions[s] = sig


def snapshot() -> dict:
    with lock:
        unreal = sum(p["u"] for p in positions.values())
        return {
            "equity": round(equity + unreal, 2),
            "pnl": round(equity + unreal - START, 2),
            "day_pnl": round(day_pnl, 2),
            "source": source,
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
        time.sleep(60)


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
<p class="muted">Nasdaq NQ=F и золото GC=F. Цены Yahoo, 15 минут. Бумага $500, не брокер.</p>
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
  document.getElementById('src').textContent = s.source;
  document.getElementById('live').innerHTML = s.open.map(x =>
    `<div class="card"><div class="row"><b class="${x.side=='long'?'up':'down'}">${x.symbol} ${x.side}</b><b>${x.u.toFixed(2)}</b></div>
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
