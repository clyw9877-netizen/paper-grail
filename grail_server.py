"""
Бумажный грааль по разбору Мусы. NQ=F и GC=F, Yahoo 15m, без ключа.
Ордеров нет. Депозит $500, риск $200, тейк строго 1:1, комиссия $1.40.

Уровень: 2-3 свечи, тела не закрылись, фитили в одно место.
Слева пустота. Проторговка слева — скип.
Пробой телом, ретест не дальше бокса, вход когда свеча переоделась.
Свеча без фитиля или с огромным фитилем — скип.
Стоп за фитиль пробоя. Тейк 1:1. Азию не торгуем.
Deep Gamma и Big Trades тут нет: их Yahoo не отдаёт.
"""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.request
from datetime import datetime, timezone
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
    return out[-121:]  # последняя свеча формируется: она идёт в живую цену


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
    raise RuntimeError("; ".join(errors))


def session_ok(ts: int) -> bool:
    hour = datetime.fromtimestamp(ts, timezone.utc).hour
    # Лондон и Нью-Йорк по UTC. Азия, примерно 0-7 UTC, скип.
    return 7 <= hour < 21


def wick_ok(c: dict) -> bool:
    body = abs(c["c"] - c["o"])
    rng = c["h"] - c["l"]
    if rng <= 0 or body <= 0:
        return False
    upper = c["h"] - max(c["o"], c["c"])
    lower = min(c["o"], c["c"]) - c["l"]
    if upper < rng * 0.05 and lower < rng * 0.05:
        return False
    if upper > body * 1.5 or lower > body * 1.5:
        return False
    return True


def setup(cs: list[dict]) -> dict | None:
    if len(cs) < 15 or not session_ok(cs[-1]["t"]):
        return None
    box = cs[-6:-3]
    left = cs[-14:-6]
    br, retest, cur = cs[-3], cs[-2], cs[-1]
    if not wick_ok(br):
        return None
    top = max(c["h"] for c in box)
    bot = min(c["l"] for c in box)
    if top <= bot:
        return None
    poked = sum(1 for c in box if c["h"] >= top - (top - bot) * 0.25 or c["l"] <= bot + (top - bot) * 0.25)
    bodies_inside = all(min(c["o"], c["c"]) >= bot and max(c["o"], c["c"]) <= top for c in box)
    if poked < 2 or not bodies_inside:
        return None
    # слева пустота, не проторговка
    if min(c["l"] for c in left) > bot and max(c["h"] for c in left) < top:
        return None
    impulse = abs(br["c"] - br["o"]) > (top - bot) * 2
    if impulse:
        return None
    # шорт: тело закрылось под боксом, ретест не выше бокса, свеча переоделась вниз
    if br["c"] < bot and br["o"] >= bot and retest["h"] <= top and retest["l"] <= bot and cur["c"] < cur["o"] and cur["c"] < retest["c"]:
        entry = max(br["o"], br["c"])
        stop = br["h"]
        risk = stop - entry
        if risk <= 0 or cur["h"] < entry:
            return None
        return {"side": "short", "entry": entry, "stop": stop, "take": entry - risk, "u": 0.0}
    # лонг
    if br["c"] > top and br["o"] <= top and retest["l"] >= bot and retest["h"] >= top and cur["c"] > cur["o"] and cur["c"] > retest["c"]:
        entry = min(br["o"], br["c"])
        stop = br["l"]
        risk = entry - stop
        if risk <= 0 or cur["l"] > entry:
            return None
        return {"side": "long", "entry": entry, "stop": stop, "take": entry + risk, "u": 0.0}
    return None

def signal(symbol: str) -> dict | None:
    cs = candles.get(symbol) or []
    if symbol in positions or day_pnl <= DAILY_STOP:
        return None
    sig = setup(cs)  # только закрытые свечи: cur = последняя закрытая
    if sig:
        sig["symbol"] = symbol
    return sig


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
            "note": "grail Musa, no gamma, paper only",
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
<title>Grail paper</title>
<style>
body{margin:0;font-family:-apple-system,sans-serif;background:#101010;color:#f3f3f3}
main{max-width:760px;margin:0 auto;padding:18px 14px 40px}
.muted{color:#9a9a9a}.up{color:#3dd68c}.down{color:#ff5d5d}
.grid{display:grid;grid-template-columns:repeat(3,1fr);gap:8px}
.card{background:#1b1b1b;border-radius:14px;padding:12px;margin:8px 0}
.row{display:flex;justify-content:space-between;margin-top:4px;color:#ccc}
</style></head><body><main>
<h1>Грааль paper</h1>
<p class="muted">Бокс, пустота слева, пробой телом, ретест, стоп за фитиль, тейк 1:1. Азия скип. Гаммы нет. Риск $200.</p>
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
     <div class="row"><span>тейк 1:1</span><span>${x.take.toFixed(2)}</span></div></div>`).join('') || '<div class="card">скип, нет бокса или ретеста</div>';
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
