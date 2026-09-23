"""Read-only local web page for run directories. It never writes run state.

The server listens on 127.0.0.1 only and serves nothing but the runs it lists
under one directory. Ledgers are opened with SQLite's read-only mode; .env and
key files are never read.
"""

import dataclasses
import json
import sqlite3
import time
import webbrowser
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .config import D, Settings

PAGE = Path(__file__).with_name("monitor.html")
HISTORY = 720  # observations charted: 12 hours at one per minute


def runs(root):
    """Run directories under root, most recently written by a worker first."""
    root = Path(root)
    if not root.is_dir():
        return []

    def written(run):
        # SQLite's -wal/-shm files change whenever anyone reads the ledger,
        # this monitor included, so they say nothing about the worker.
        return max(
            f.stat().st_mtime
            for f in run.iterdir()
            if not f.name.endswith(("-wal", "-shm"))
        )

    found = [p for p in root.iterdir() if (p / "ledger.sqlite").is_file()]
    return sorted(found, key=written, reverse=True)


def events(path, limit=HISTORY):
    rows = deque(maxlen=limit)
    if path.exists():
        with path.open() as f:
            for line in f:
                if not line.endswith("\n"):
                    break  # The worker is still appending this line.
                rows.append(json.loads(line))
    return list(rows)


def point(e):
    """The fields the page charts from one events.jsonl row."""
    n, x = e["neural"], e["execution"]
    return {
        "tick": e["tick"],
        "time": e["wall_time"],
        "product": e["product"],
        "bid": e["quote"]["bid"],
        "equity": e["equity_usdc"],
        "side": n["side"],
        "left_hz": n["left_hz"],
        "right_hz": n["right_hz"],
        "gate_spikes": n["gate_spikes"],
        "kc_spikes": n["KC_spikes"],
        "reward_spikes": n["reward_spikes"],
        "aversive_spikes": n["aversive_spikes"],
        "stimulus": n["stimulus"],
        "changed_edges": n["memory"]["changed_edges"],
        "execution": x.get("status"),
        "reason": x.get("reason"),
    }


def order(row):
    cid, status, created, plan, exchange_id, settlement = row
    plan = json.loads(plan)
    fill = json.loads(settlement) if settlement else {}
    return {
        "id": cid,
        "status": status,
        "time": created,
        "product": plan["product"],
        "side": plan["side"],
        "size": plan["base_size"],
        "limit": plan["limit_price"],
        "exchange_id": exchange_id,
        "filled": fill.get("base"),
        "quote": fill.get("quote"),
        "fee": fill.get("fee"),
        "fee_asset": fill.get("fee_asset", "quote") if fill else None,
    }


def state(run, now=None):
    """Everything the page shows for one run, from files the worker writes."""
    run = Path(run)
    now = time.time() if now is None else now
    db = sqlite3.connect(f"file:{run / 'ledger.sqlite'}?mode=ro", uri=True)
    try:
        meta = {k: json.loads(v) for k, v in db.execute("SELECT key,value FROM meta")}
        orders = db.execute(
            "SELECT id,status,created,plan,exchange_id,settlement FROM orders "
            "ORDER BY created DESC LIMIT 100"
        ).fetchall()
        # Same UTC day boundary as Ledger.attempts_today.
        attempts = db.execute(
            "SELECT COUNT(*) FROM orders WHERE created>=?", (now - now % 86400,)
        ).fetchone()[0]
    finally:
        db.close()
    provenance = run / "provenance.json"
    provenance = json.loads(provenance.read_text()) if provenance.exists() else {}
    settings = provenance.get("settings") or dataclasses.asdict(Settings())
    history = events(run / "events.jsonl")
    bids = {e["product"]: D(e["quote"]["bid"]) for e in history}  # latest wins
    cash = D(meta["cash"])
    positions = {p: D(v) for p, v in meta["positions"].items()}
    value = (
        None
        if any(v and p not in bids for p, v in positions.items())
        else cash + sum((v * bids[p] for p, v in positions.items() if v), D(0))
    )
    error = run / "error.json"
    return {
        "run": run.name,
        "mode": meta["mode"],
        "feed": provenance.get("feed"),
        "products": settings["products"],
        "tick": meta["tick"],
        "halted": meta["halted"],
        "stop_file": (run / "STOP").exists(),
        "error": json.loads(error.read_text()) if error.exists() else None,
        "account": {
            "cash": str(cash),
            "positions": {p: str(v) for p, v in positions.items()},
            "initial_cash": meta["initial_cash"],
            "value": None if value is None else str(value),
            "pnl": None if value is None else str(value - D(meta["initial_cash"])),
        },
        "orders": [order(r) for r in orders],
        "attempts_today": attempts,
        "daily_orders": settings["daily_orders"],
        "cooldown_seconds": max(
            0, meta["last_attempt"] + settings["interval_seconds"] - now
        ),
        "interval_seconds": settings["interval_seconds"],
        "decoder_threshold_hz": settings["decoder_threshold_hz"],
        "history": [point(e) for e in history],
        "latest": history[-1] if history else None,
        "image": (run / "latest-input.png").exists(),
        "now": now,
    }


def server(root, port=8765):
    root = Path(root)

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            url = urlsplit(self.path)
            if url.path == "/":
                return self.reply(200, "text/html; charset=utf-8", PAGE.read_bytes())
            if url.path == "/api/runs":
                names = [p.name for p in runs(root)]
                return self.reply(200, "application/json", json.dumps(names).encode())
            # Only runs this server lists are reachable, by name.
            name = parse_qs(url.query).get("run", [None])[0]
            run = next((p for p in runs(root) if p.name == name), None)
            if run is not None and url.path == "/api/state":
                try:
                    body = json.dumps(state(run)).encode()
                except Exception as e:
                    # Local files only; show why rather than a stale page.
                    message = f"{type(e).__name__}: {e}".encode()
                    return self.reply(500, "text/plain; charset=utf-8", message)
                return self.reply(200, "application/json", body)
            image = run / "latest-input.png" if run is not None else None
            if url.path == "/image" and image and image.exists():
                return self.reply(200, "image/png", image.read_bytes())
            self.reply(404, "text/plain; charset=utf-8", b"Not found")

        def reply(self, status, kind, body):
            self.send_response(status)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass  # The page polls every few seconds; keep the terminal quiet.

    return ThreadingHTTPServer(("127.0.0.1", port), Handler)


def serve(out=None, port=8765, browser=True):
    root = Path(out).parent if out else Path("runs")
    if out and not (Path(out) / "ledger.sqlite").is_file():
        raise SystemExit(f"No run ledger at {Path(out) / 'ledger.sqlite'}")
    listed = runs(root)
    if not listed:
        raise SystemExit(f"No run directories with a ledger under {root}/")
    name = Path(out).name if out else listed[0].name
    try:
        httpd = server(root, port)
    except OSError as e:
        raise SystemExit(
            f"Cannot listen on 127.0.0.1:{port} ({e}); pass --port"
        ) from None
    url = f"http://127.0.0.1:{httpd.server_address[1]}/?run={name}"
    print(f"Read-only monitor for {root / name}: {url}  (Ctrl-C stops)", flush=True)
    if browser:
        webbrowser.open(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
