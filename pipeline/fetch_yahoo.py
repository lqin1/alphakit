#!/usr/bin/env python
"""Fetch daily bars + corporate-action events from Yahoo for the S&P 500 pilot universe.

Owns (per docs/l2_schema.md sections 0, 1, 7):
  storage/data/base/l1/yahoo/chart/{YAHOO_SYMBOL}.json  append-only raw payloads, written VERBATIM
  storage/data/base/l1/yahoo/_fetch_manifest_*.csv      pipe-delimited fetch manifest

Deliberately stdlib-only (urllib): the project venv has pandas/numpy/pyarrow but NOT
requests, and pip is PEP-668 locked.

Notes
-----
* query1.finance.yahoo.com returns 429; query2 works. Do not "fix" the host.
* Yahoo needs '-' for class shares ('BRK-B'); the S&P constituents file uses '.'
  ('BRK.B', which returns 404 Not Found). Both forms are recorded in the manifest.
* Explicit period1/period2 epoch seconds (not range=1y) so the fetch is reproducible.
  The window is deliberately wider than the L2 delivery window 2025-08-29..2026-08-28;
  trimming happens downstream, not here.
* This module does NOT reconstruct raw prices and does not write anything under data/l2/.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import os
import random
import statistics
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

# --------------------------------------------------------------------------- config

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONSTITUENTS = os.path.join(REPO, "data", "raw", "ref", "sp500_constituents_20260830.csv")
RAW_DIR = os.path.join(REPO, "storage", "data", "base", "l1", "yahoo", "chart")
MANIFEST = os.path.join(REPO, "storage", "data", "base", "l1", "yahoo", "_fetch_manifest_20260830.csv")

HOST = "https://query2.finance.yahoo.com"
URL_TMPL = (
    HOST + "/v8/finance/chart/{sym}"
    "?period1={p1}&period2={p2}&interval=1d&events=div%2Csplit"
)
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 alphakit qinlincn@gmail.com"

# Fetch window (epoch seconds, UTC). Wider than the L2 window on purpose.
PERIOD1 = int(dt.datetime(2025, 8, 1, tzinfo=dt.timezone.utc).timestamp())   # 1754006400
PERIOD2 = int(dt.datetime(2026, 8, 30, tzinfo=dt.timezone.utc).timestamp())  # 1788048000

DEFAULT_WORKERS = 6          # hard ceiling; box has 2 CPUs and the job is network-bound
DEFAULT_RATE = 8.0           # req/s global cap; measured-safe sequential baseline is 6.3
MAX_RETRIES = 4              # -> 5 attempts total
BACKOFF = (1.0, 2.0, 4.0, 8.0)
TIMEOUT = 30.0

MANIFEST_COLUMNS = [
    "source_symbol", "yahoo_symbol", "http_status", "ok", "n_bars",
    "first_date", "last_date", "first_trade_date", "currency",
    "exchange_name", "instrument_type", "n_dividends", "n_splits", "error",
]

_print_lock = threading.Lock()


def log(msg: str) -> None:
    with _print_lock:
        print(msg, flush=True)


# ------------------------------------------------------------------- rate / concurrency

class RateLimiter:
    """Thread-safe token bucket, global req/s cap. Rate can be lowered at runtime."""

    def __init__(self, rate: float):
        self.lock = threading.Lock()
        self.rate = float(rate)
        # small bucket: allow a short burst, but never a 500-request stampede
        self.capacity = max(1.0, min(float(rate), 4.0))
        self.tokens = self.capacity
        self.stamp = time.monotonic()

    def acquire(self) -> None:
        while True:
            with self.lock:
                now = time.monotonic()
                self.tokens = min(self.capacity, self.tokens + (now - self.stamp) * self.rate)
                self.stamp = now
                if self.tokens >= 1.0:
                    self.tokens -= 1.0
                    return
                wait = (1.0 - self.tokens) / self.rate
            time.sleep(min(wait, 0.25))

    def scale(self, factor: float, floor: float = 1.0) -> float:
        with self.lock:
            self.rate = max(floor, self.rate * factor)
            self.capacity = max(1.0, min(self.rate, 4.0))
            self.tokens = min(self.tokens, self.capacity)
            return self.rate


class DynamicSemaphore:
    """Semaphore whose ceiling can be lowered while requests are in flight."""

    def __init__(self, limit: int):
        self.cv = threading.Condition()
        self.limit = int(limit)
        self.inflight = 0

    def __enter__(self):
        with self.cv:
            while self.inflight >= self.limit:
                self.cv.wait(timeout=0.5)
            self.inflight += 1
        return self

    def __exit__(self, *exc):
        with self.cv:
            self.inflight -= 1
            self.cv.notify()
        return False

    def reduce(self) -> int:
        with self.cv:
            if self.limit > 1:
                self.limit -= 1
            return self.limit


class Throttle:
    """Watches 429s and backs the whole job off when they are sustained."""

    def __init__(self, limiter: RateLimiter, sem: DynamicSemaphore):
        self.limiter = limiter
        self.sem = sem
        self.lock = threading.Lock()
        self.n_429 = 0
        self.next_step = 3       # first backdown after 3 rate-limit responses
        self.events: list[str] = []

    def note_429(self) -> None:
        with self.lock:
            self.n_429 += 1
            if self.n_429 < self.next_step:
                return
            self.next_step *= 2
            n = self.n_429
        rate = self.limiter.scale(0.5)
        workers = self.sem.reduce()
        msg = (f"SUSTAINED 429s ({n} so far) -> reducing to "
               f"{rate:.2f} req/s, {workers} workers")
        with self.lock:
            self.events.append(msg)
        log("  !! " + msg)


# --------------------------------------------------------------------------- helpers

def to_date(epoch) -> str:
    """Epoch seconds -> YYYY-MM-DD (UTC).

    Safe for US-equity daily bars: Yahoo stamps them at the 09:30 ET open, i.e.
    13:30/14:30 UTC, which never crosses a UTC date boundary.
    """
    if epoch is None:
        return ""
    try:
        return dt.datetime.fromtimestamp(int(epoch), dt.timezone.utc).strftime("%Y-%m-%d")
    except (ValueError, OverflowError, OSError, TypeError):
        return ""


def clean(value) -> str:
    """Render one manifest field: missing -> empty, and never emit a bare '|'."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    text = str(value)
    if text.lower() in ("nan", "none"):
        return ""
    for bad in ("|", "\r", "\n", "\t"):
        text = text.replace(bad, " ")
    return text.strip()


def yahoo_symbol(source_symbol: str) -> str:
    """S&P constituents use '.' for class shares; Yahoo needs '-'."""
    return source_symbol.strip().upper().replace(".", "-")


def load_symbols(path: str = CONSTITUENTS) -> list[tuple[str, str]]:
    """[(source_symbol, yahoo_symbol)], de-duplicated, in file order."""
    pairs: list[tuple[str, str]] = []
    seen: set[str] = set()
    with open(path, newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)  # comma-separated, quoted text fields
        if "Symbol" not in (reader.fieldnames or []):
            raise SystemExit(f"{path}: no 'Symbol' column (found {reader.fieldnames})")
        for row in reader:
            src = (row.get("Symbol") or "").strip()
            if not src:
                continue
            ysym = yahoo_symbol(src)
            if ysym in seen:
                continue
            seen.add(ysym)
            pairs.append((src, ysym))
    return pairs


def raw_path(ysym: str) -> str:
    return os.path.join(RAW_DIR, f"{ysym}.json")


def inspect(payload: bytes) -> dict:
    """Parse a chart payload into manifest fields. Raises ValueError if unusable."""
    doc = json.loads(payload.decode("utf-8"))
    chart = doc.get("chart") or {}
    err = chart.get("error")
    if err:
        code = err.get("code") if isinstance(err, dict) else ""
        desc = err.get("description") if isinstance(err, dict) else str(err)
        raise ValueError(f"chart.error {code}: {desc}")
    results = chart.get("result")
    if not results:
        raise ValueError("chart.result empty")
    res = results[0]
    meta = res.get("meta") or {}
    stamps = res.get("timestamp") or []
    indicators = res.get("indicators") or {}
    quote = (indicators.get("quote") or [None])[0]
    adj = (indicators.get("adjclose") or [None])[0]
    if not isinstance(quote, dict):
        raise ValueError("indicators.quote[0] missing")
    if not isinstance(adj, dict) or "adjclose" not in adj:
        raise ValueError("indicators.adjclose[0].adjclose missing")
    if not stamps:
        raise ValueError("timestamp empty")
    events = res.get("events") or {}
    return {
        "n_bars": len(stamps),
        "first_date": to_date(stamps[0]),
        "last_date": to_date(stamps[-1]),
        "first_trade_date": to_date(meta.get("firstTradeDate")),
        "currency": meta.get("currency"),
        "exchange_name": meta.get("exchangeName"),
        "instrument_type": meta.get("instrumentType"),
        "n_dividends": len(events.get("dividends") or {}),
        "n_splits": len(events.get("splits") or {}),
    }


def write_atomic(path: str, payload: bytes) -> None:
    """Verbatim write via temp + rename, so a crash never leaves a half file."""
    tmp = f"{path}.tmp.{os.getpid()}.{threading.get_ident()}"
    with open(tmp, "wb") as fh:
        fh.write(payload)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


# --------------------------------------------------------------------------- fetching

def http_get(url: str) -> tuple[int, bytes]:
    req = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:          # 4xx/5xx still carry a JSON body
        try:
            body = exc.read()
        except Exception:
            body = b""
        return exc.code, body


def fetch_symbol(src: str, ysym: str, limiter: RateLimiter,
                 sem: DynamicSemaphore, throttle: Throttle, force: bool) -> dict:
    row = {c: "" for c in MANIFEST_COLUMNS}
    row["source_symbol"] = src
    row["yahoo_symbol"] = ysym
    row["ok"] = False
    path = raw_path(ysym)

    # idempotent re-run: keep an existing payload that parses and is structurally sound
    if not force and os.path.exists(path):
        try:
            with open(path, "rb") as fh:
                cached = fh.read()
            row.update(inspect(cached))
            row["http_status"] = 200   # raw files are only ever written from a 200
            row["ok"] = True
            row["_cached"] = True
            return row
        except (ValueError, json.JSONDecodeError, OSError) as exc:
            log(f"  ~~ {ysym}: cached payload unusable ({exc}); refetching")

    url = URL_TMPL.format(sym=urllib.parse.quote(ysym, safe=""), p1=PERIOD1, p2=PERIOD2)
    last_err = ""
    status = None

    for attempt in range(MAX_RETRIES + 1):
        limiter.acquire()
        with sem:
            try:
                status, body = http_get(url)
                transport_err = ""
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                status, body, transport_err = None, b"", f"{type(exc).__name__}: {exc}"

        if transport_err:
            last_err = transport_err
            retryable = True
        elif status == 200:
            try:
                fields = inspect(body)
            except (ValueError, json.JSONDecodeError, UnicodeDecodeError) as exc:
                # 200 with an unusable body: keep the payload (append-only raw layer)
                # but mark the row not-ok so downstream never silently consumes it.
                write_atomic(path, body)
                row["http_status"] = status
                row["ok"] = False
                row["error"] = f"unusable 200 payload: {exc}"
                return row
            write_atomic(path, body)          # VERBATIM, no transform/prettify
            row.update(fields)
            row["http_status"] = status
            row["ok"] = True
            row["error"] = ""
            return row
        elif status == 429:
            throttle.note_429()
            last_err = "HTTP 429 rate limited"
            retryable = True
        elif status is not None and 500 <= status < 600:
            last_err = f"HTTP {status} server error"
            retryable = True
        else:
            desc = ""
            try:
                err = (json.loads(body.decode("utf-8")).get("chart") or {}).get("error")
                if isinstance(err, dict):
                    desc = f" {err.get('code','')}: {err.get('description','')}"
            except Exception:
                desc = ""
            row["http_status"] = status
            row["ok"] = False
            row["error"] = f"HTTP {status}{desc}".strip()
            return row                         # 404 & friends: terminal, do not retry

        if attempt < MAX_RETRIES and retryable:
            delay = BACKOFF[min(attempt, len(BACKOFF) - 1)] * (1.0 + random.random() * 0.25)
            time.sleep(delay)

    row["http_status"] = status
    row["ok"] = False
    row["error"] = f"gave up after {MAX_RETRIES + 1} attempts: {last_err}"
    return row


# --------------------------------------------------------------------------- manifest

def write_manifest(rows: list[dict], path: str = MANIFEST) -> None:
    """Pipe-delimited, header row, no quoting, empty field for missing (schema section 2)."""
    ordered = sorted(rows, key=lambda r: r["source_symbol"])
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("|".join(MANIFEST_COLUMNS) + "\n")
        for row in ordered:
            fh.write("|".join(clean(row.get(c)) for c in MANIFEST_COLUMNS) + "\n")
    os.replace(tmp, path)


# --------------------------------------------------------------------------- main

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Fetch Yahoo v8 daily chart payloads.")
    ap.add_argument("--workers", type=int, default=DEFAULT_WORKERS,
                    help=f"max concurrent requests (hard cap {DEFAULT_WORKERS})")
    ap.add_argument("--rate", type=float, default=DEFAULT_RATE, help="global req/s cap")
    ap.add_argument("--limit", type=int, default=0, help="only the first N symbols (smoke test)")
    ap.add_argument("--force", action="store_true", help="refetch even if the JSON exists")
    args = ap.parse_args(argv)

    workers = max(1, min(args.workers, DEFAULT_WORKERS))
    if args.workers > DEFAULT_WORKERS:
        log(f"clamping --workers {args.workers} -> {DEFAULT_WORKERS} (politeness ceiling)")

    os.makedirs(RAW_DIR, exist_ok=True)
    symbols = load_symbols()
    if args.limit:
        symbols = symbols[:args.limit]

    log(f"symbols={len(symbols)} workers={workers} rate<={args.rate} req/s")
    log(f"window period1={PERIOD1} ({to_date(PERIOD1)}) period2={PERIOD2} ({to_date(PERIOD2)})")

    limiter = RateLimiter(args.rate)
    sem = DynamicSemaphore(workers)
    throttle = Throttle(limiter, sem)

    rows: list[dict] = []
    started = time.monotonic()
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(fetch_symbol, src, ysym, limiter, sem, throttle, args.force): ysym
            for src, ysym in symbols
        }
        done = 0
        for fut in as_completed(futures):
            row = fut.result()
            rows.append(row)
            done += 1
            if not row["ok"]:
                log(f"  FAIL {row['yahoo_symbol']}: {row['error']}")
            if done % 100 == 0 or done == len(symbols):
                log(f"  .. {done}/{len(symbols)}  {done / (time.monotonic() - started):.2f} req/s")

    elapsed = time.monotonic() - started
    write_manifest(rows)

    n_cached = sum(1 for r in rows if r.get("_cached"))
    n_ok = sum(1 for r in rows if r["ok"])
    fetched = len(rows) - n_cached
    bars = sorted(r["n_bars"] for r in rows if r["ok"])

    log("")
    log(f"elapsed {elapsed:.1f}s  |  {len(rows)} symbols  "
        f"({fetched} fetched, {n_cached} cached)  |  "
        f"{fetched / elapsed if elapsed else 0:.2f} req/s observed")
    log(f"ok {n_ok}  failed {len(rows) - n_ok}  |  429s seen: {throttle.n_429}")
    if bars:
        log(f"n_bars min/median/max {bars[0]}/{int(statistics.median(bars))}/{bars[-1]}")
    log(f"manifest -> {MANIFEST}")
    for msg in throttle.events:
        log(f"THROTTLE: {msg}")
    for row in sorted((r for r in rows if not r["ok"]), key=lambda r: r["source_symbol"]):
        log(f"FAILED {row['source_symbol']} ({row['yahoo_symbol']}): {row['error']}")

    return 0 if n_ok == len(rows) else 1


if __name__ == "__main__":
    sys.exit(main())
