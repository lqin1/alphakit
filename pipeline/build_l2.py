#!/usr/bin/env python
"""Build the L2 layer from raw Yahoo chart payloads.

Contract: docs/l2_schema.md.  Reads storage/data/base/l1/yahoo/chart/*.json and
emits storage/data/base/l2/us/{daily_bar,corp_action,sec_master,calendar}.YYYYMMDD plus _meta.json.

The load-bearing step is the raw-price reconstruction in §7 of the contract.
Yahoo's quote.* arrays are normally split-adjusted but dividend-unadjusted, so
as-traded prices are recovered by re-applying the splits that happened *after*
each bar -- NVDA reads 120.89 on 2024-06-07 and 121.79 on 2024-06-10 across a
10:1 split, i.e. no discontinuity in the vendor series.

Two things make that harder than it sounds, both found by measurement:

  * The vendor is not uniform.  16 of the pilot's 17 split events are already
    back-adjusted, but MNST's 2:1 of 2026-08-11 is not, and still carries its
    full 1.985x step.  Each event is therefore classified individually
    (classify_splits) instead of assumed.

  * adj_factor must not be read back from the vendor.  Defining it as
    adjclose/raw_close is a tautology and inherits the vendor's mistakes, so it
    is derived from the corp_action event log instead (compute_adj_factor).
    Cross-checked on returns rather than levels, that derivation reproduces the
    vendor to within 1bp on 502 of 503 names; the one disagreement is MNST,
    where the vendor is the one that is wrong.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import statistics
import sys
from collections import defaultdict
from pathlib import Path
from zoneinfo import ZoneInfo

NY = ZoneInfo("America/New_York")

REPO = Path(__file__).resolve().parent.parent
DATASET = "base"
COUNTRY = "us"
RAW_CHART = REPO / "storage" / "data" / DATASET / "l1" / "yahoo" / "chart"
L2 = REPO / "storage" / "data" / DATASET / "l2" / COUNTRY
REGISTRY = REPO / "registry" / f"security_id.{COUNTRY}.csv"
REGISTRY_COLS = ["security_id", "cik", "ticker_yahoo", "name",
                 "first_trade_date", "added_asof"]

SD = dt.date(2025, 8, 29)
ED = dt.date(2026, 8, 28)

PV_COLS = ["date", "security_id", "ticker", "open", "high", "low",
                  "close", "volume", "adj_factor", "adj_close_vendor"]
CAX_COLS = ["date", "security_id", "ticker", "event_type",
                    "div_amount", "split_num", "split_den", "split_ratio"]
# sec_master and industry are POINT IN TIME: one file per trading session, listing
# the securities known to be listed on that date.  Reading today's snapshot to
# interpret last year's panel is look-ahead, which is what architecture.md §3.4
# ("ticker 为带生效区间的属性") and §十一 exist to prevent.
#
# Coverage (first_session / last_session / n_sessions) is deliberately ABSENT: in a
# per-date row it is itself look-ahead -- on 2025-09-01 "this security has 250
# sessions" is a fact about the future.  It is trivially recomputed from pv, and a
# summary lives in _meta.json instead.
SEC_MASTER_COLS = ["date", "security_id", "ticker", "ticker_nasdaq", "ticker_cqs",
                   "ticker_yahoo", "name", "exchange", "cik", "is_etf", "round_lot",
                   "financial_status", "first_trade_date", "currency", "ref_asof", "source"]
# Industry classification is a separate table: it is a time-varying PIT attribute
# from a different source (S&P/GICS) than the securities master (NasdaqTrader/SEC),
# and `neutralize: sector` wants it as its own date x instrument field.
INDUSTRY_COLS = ["date", "security_id", "ticker", "gics_sector_code", "gics_sector",
                 "gics_sub_industry", "ref_asof", "source"]
# Official GICS sector codes -- stable integers, and they fit the `dtype: i1`
# that architecture.md §5.1 declares for a sector field.
GICS_SECTOR_CODE = {
    "Energy": 10, "Materials": 15, "Industrials": 20,
    "Consumer Discretionary": 25, "Consumer Staples": 30, "Health Care": 35,
    "Financials": 40, "Information Technology": 45,
    "Communication Services": 50, "Utilities": 55, "Real Estate": 60,
}
CALENDAR_COLS = ["session", "date", "is_half_day", "n_securities"]


# ---------------------------------------------------------------- pipe csv io

def clean(v) -> str:
    """Render one field.  Contract §2: missing -> empty, no quoting, no raw pipes."""
    if v is None:
        return ""
    s = str(v)
    if s in ("nan", "NaN", "None"):
        return ""
    return s.replace("|", " ").replace("\r", " ").replace("\n", " ")


def write_pipe(path: Path, cols: list[str], rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as fh:
        fh.write("|".join(cols) + "\n")
        for r in rows:
            fh.write("|".join(clean(r.get(c)) for c in cols) + "\n")


def read_pipe(path: Path) -> list[dict]:
    with path.open(encoding="utf-8") as fh:
        header = fh.readline().rstrip("\n").split("|")
        out = []
        for line in fh:
            parts = line.rstrip("\n").split("|")
            if len(parts) != len(header):
                raise ValueError(f"{path}: field count {len(parts)} != header {len(header)}")
            out.append(dict(zip(header, parts)))
        return out


def px(v) -> str:
    return "" if v is None else f"{v:.6f}"


def fac(v) -> str:
    return "" if v is None else f"{v:.10f}"


# ------------------------------------------------------------------- parsing

def ts_to_date(epoch: int) -> dt.date:
    return dt.datetime.fromtimestamp(epoch, NY).date()


def parse_chart(path: Path) -> dict | None:
    """Raw payload -> {symbol, first_trade_date, currency, bars[], divs[], splits[]}.

    Bars carry the vendor's split-adjusted values; the inversion happens in
    reconstruct().  Bars with a null close are dropped: without a close there is
    no anchor for adj_factor, and the contract says a security with no quote
    simply does not appear that day rather than appearing as an all-NaN row.
    """
    try:
        doc = json.loads(path.read_text())
    except Exception as exc:
        return {"symbol": path.stem, "error": f"unparseable json: {exc}"}

    chart = doc.get("chart") or {}
    results = chart.get("result")
    if not results:
        err = (chart.get("error") or {}).get("description") or "no result"
        return {"symbol": path.stem, "error": err}

    r = results[0]
    meta = r.get("meta") or {}
    stamps = r.get("timestamp") or []
    quote = ((r.get("indicators") or {}).get("quote") or [{}])[0]
    adjblk = ((r.get("indicators") or {}).get("adjclose") or [{}])[0]
    adjclose = adjblk.get("adjclose") or [None] * len(stamps)

    bars, dropped = [], []
    for i, epoch in enumerate(stamps):
        close = quote.get("close", [None] * len(stamps))[i]
        if close is None:
            # Observed: the vendor publishes the newest session with o/h/l/volume but a
            # null close until it settles.  Tracked by date so a vendor-wide incomplete
            # session is distinguishable from a scattered per-name gap.
            dropped.append(ts_to_date(epoch))
            continue
        bars.append({
            "date": ts_to_date(epoch),
            "open": quote.get("open", [None] * len(stamps))[i],
            "high": quote.get("high", [None] * len(stamps))[i],
            "low": quote.get("low", [None] * len(stamps))[i],
            "close": close,
            "volume": quote.get("volume", [None] * len(stamps))[i],
            "adjclose": adjclose[i] if i < len(adjclose) else None,
        })

    events = r.get("events") or {}
    divs = [{"date": ts_to_date(v["date"]), "amount": v["amount"]}
            for v in (events.get("dividends") or {}).values()]
    splits = [{"date": ts_to_date(v["date"]),
               "num": float(v["numerator"]),
               "den": float(v["denominator"]),
               "ratio_str": v.get("splitRatio", "")}
              for v in (events.get("splits") or {}).values()]
    divs.sort(key=lambda d: d["date"])
    splits.sort(key=lambda d: d["date"])

    ftd = meta.get("firstTradeDate")
    return {
        "symbol": meta.get("symbol") or path.stem,
        "first_trade_date": ts_to_date(ftd) if ftd else None,
        "currency": meta.get("currency"),
        "bars": bars, "divs": divs, "splits": splits,
        "dropped_null_close_dates": dropped, "error": None,
    }


# -------------------------------------------------------------- reconstruction

def classify_splits(rec: dict) -> None:
    """Decide, per split event, whether the vendor already applied it to `quote`.

    The vendor is NOT uniform about this.  Measured across all 17 split events in
    the pilot: 16 are back-adjusted in the quote array, but MNST's 2:1 of
    2026-08-11 -- the most recent one -- is not, and shows its full 1.985x jump
    still sitting in the series.  Blindly re-applying the ratio there would have
    doubled every MNST price before the split.

    Two hypotheses about the observed jump across the ex-date:
        applied     -> quote is continuous, jump ~ 1
        not applied -> quote still holds the step, jump ~ ratio
    Pick whichever is nearer in log space.  When the ratio is close to 1 (the
    spin-off pseudo-splits below) the two are not separable, and this correctly
    falls through to `applied`, which is both the common case and the one whose
    error is bounded by a few percent.
    """
    bars = rec["bars"]
    for sp in rec["splits"]:
        sp["ratio"] = sp["num"] / sp["den"]
        ex = sp["date"]
        pre = next((b for b in reversed(bars) if b["date"] < ex), None)
        post = next((b for b in bars if b["date"] >= ex), None)
        if pre is None or post is None or not post["close"] or sp["ratio"] <= 0:
            sp["vendor_applied"] = True
            sp["evidence"] = "no bar on one side of the ex-date; assumed applied"
            continue
        jump = pre["close"] / post["close"]
        d_applied = abs(math.log(jump))
        d_notapplied = abs(math.log(jump / sp["ratio"]))
        sp["vendor_applied"] = d_applied <= d_notapplied
        sp["evidence"] = f"jump={jump:.4f} vs ratio={sp['ratio']:.4f}"


def reconstruct(rec: dict) -> None:
    """Recover as-traded prices.  Contract §7.

    S(t) = product of the ratios of splits the vendor ACTUALLY applied, with
    ex_date STRICTLY AFTER t.  Strictly, because on the ex-date the vendor price
    is already the post-split price.
    """
    classify_splits(rec)
    applied = [sp for sp in rec["splits"] if sp["vendor_applied"]]
    for bar in rec["bars"]:
        s = 1.0
        for sp in applied:
            if sp["date"] > bar["date"]:
                s *= sp["ratio"]
        bar["split_cum"] = s
        bar["raw_open"] = bar["open"] * s if bar["open"] is not None else None
        bar["raw_high"] = bar["high"] * s if bar["high"] is not None else None
        bar["raw_low"] = bar["low"] * s if bar["low"] is not None else None
        bar["raw_close"] = bar["close"] * s
        bar["raw_volume"] = round(bar["volume"] / s) if bar["volume"] is not None else None


def compute_adj_factor(rec: dict) -> None:
    """Build our own cumulative adjustment factor from the event log.

    Defining adj_factor as adjclose/raw_close would make the V1 identity a
    tautology (measured round-trip error 1e-16) and would silently inherit every
    vendor mistake -- MNST's unprocessed split leaves a fake -50% return in the
    vendor's own adjusted series.  Deriving the factor from the events instead
    makes corp_action the PIT authority the contract claims it is, and turns the
    comparison against adj_close_vendor into a real cross-check.

        adj_close(t) = raw_close(t) * DF(t) / SF(t)
        SF(t) = product of ratios of ALL splits with ex_date > t
        DF(t) = product of (1 - D_raw / C_raw(prev session)) over dividends with ex_date > t

    Dividend amounts are restated by the vendor in current-share terms (NVDA
    paid 0.04/sh before its 10:1 and the feed reports 0.004), so each amount is
    un-restated by the split factor in force at its own ex-date before being
    divided by a raw close.
    """
    bars = rec["bars"]
    if not bars:
        return
    splits_on = defaultdict(list)
    for sp in rec["splits"]:
        splits_on[sp["date"]].append(sp["ratio"])
    divs_on = {d["date"]: d["amount"] for d in rec["divs"]}

    acc = 1.0
    bars[-1]["adj_factor"] = acc
    for i in range(len(bars) - 2, -1, -1):
        d_next = bars[i + 1]["date"]
        for ratio in splits_on.get(d_next, []):
            acc /= ratio
        if d_next in divs_on:
            prev_close = bars[i]["raw_close"]
            d_raw = divs_on[d_next] * bars[i + 1]["split_cum"]
            if prev_close:
                acc *= (1.0 - d_raw / prev_close)
        bars[i]["adj_factor"] = acc


# ------------------------------------------------------------- security_id axis

def assign_security_ids(recs, ref, path, asof):
    """Append-only security_id assignment against a persistent registry.

    architecture.md §3.4 requires an internal id that is NEVER reused (US tickers
    get recycled, so keying on ticker silently welds a dead company's history onto
    whoever inherits its symbol), and §3.3 requires the column axis to grow
    monotonically at the tail.  Deriving ids by sorting whatever happens to be in
    this run satisfies neither: adding one security, or backfilling delisted names,
    renumbers everything, and id 42 stops meaning what it meant yesterday.

    So ids live in a registry file that outlives any single build.  Existing
    entries are never renumbered; genuinely new securities take max+1.  The key is
    (CIK, ticker) rather than ticker alone -- CIK survives a rename, and pairing it
    with the ticker keeps share classes apart (GOOGL and GOOG share CIK 0001652044,
    which is exactly the company-vs-listing distinction §3.4 calls out).
    """
    def key(rec):
        cik = (ref.get(rec["symbol"], {}) or {}).get("cik", "")
        return (cik, rec["symbol"])

    existing, maxid, rows = {}, 0, []
    if path.exists():
        rows = read_pipe(path)
        existing = {(r["cik"], r["ticker_yahoo"]): int(r["security_id"]) for r in rows}
        maxid = max((int(r["security_id"]) for r in rows), default=0)

    fresh = [r for r in recs if key(r) not in existing]
    # Seed order only: listing order, ties by ticker.  Applies to new entries, never
    # a re-sort of what is already registered.
    fresh.sort(key=lambda r: (r["first_trade_date"] or dt.date(1900, 1, 1), r["symbol"]))
    for rec in fresh:
        maxid += 1
        cik, tic = key(rec)
        existing[(cik, tic)] = maxid
        rows.append({"security_id": maxid, "cik": cik, "ticker_yahoo": tic,
                     "name": (ref.get(tic, {}) or {}).get("name", ""),
                     "first_trade_date": rec["first_trade_date"].isoformat()
                                         if rec["first_trade_date"] else "",
                     "added_asof": asof})
    for rec in recs:
        rec["security_id"] = existing[key(rec)]
    if fresh:
        write_pipe(path, REGISTRY_COLS, sorted(rows, key=lambda r: int(r["security_id"])))
    return len(fresh), len(existing)


# ------------------------------------------------------------------- calendar

def known_half_day(d: dt.date) -> bool:
    """NYSE early closes: day after Thanksgiving, Dec 24, July 3 (when trading)."""
    if d.month == 11 and d.weekday() == 4:            # Friday after 4th Thursday
        thanksgiving = max(day for day in range(22, 29)
                           if dt.date(d.year, 11, day).weekday() == 3)
        return d.day == thanksgiving + 1
    if d.month == 12 and d.day == 24:
        return True
    if d.month == 7 and d.day == 3:
        return True
    return False


def trim_trailing_degenerate(bars_by_date: dict[dt.date, list]) -> list[dt.date]:
    """Drop trailing sessions the vendor has not finished publishing.

    The newest bar arrives with o/h/l/volume but a null close, so nearly every
    security gets dropped and the session survives with a handful of names — a
    cross-sectional booby trap.  Only *trailing* sessions are trimmed: a thin
    session in the middle of the history is a real signal and is kept.
    """
    dates = sorted(bars_by_date)
    if not dates:
        return []
    med = statistics.median(len(bars_by_date[d]) for d in dates)
    dropped = []
    while dates and len(bars_by_date[dates[-1]]) < 0.5 * med:
        d = dates.pop()
        dropped.append(d)
        del bars_by_date[d]
    return list(reversed(dropped))


def build_calendar(bars_by_date: dict[dt.date, list]) -> tuple[list[dict], list[str]]:
    """Session axis.  The half-day RULE is authoritative — an early close is a
    published exchange-calendar fact, not something to infer.  Volume is used
    only to contradict it: a rule-predicted half day trading at normal volume is
    suspicious and gets reported.  The converse (a quiet full session) is not —
    the Christmas-to-New-Year week runs at 0.5x volume without any early close.
    """
    dates = sorted(bars_by_date)
    vols = {d: sum(b["raw_volume"] or 0 for _, b in bars_by_date[d]) for d in dates}
    notes, rows = [], []
    for i, d in enumerate(dates):
        window = [vols[x] for x in dates[max(0, i - 20):i]]
        med = statistics.median(window) if window else 0
        half = int(known_half_day(d))
        if half and med and vols[d] > 0.8 * med:
            notes.append(f"{d}: flagged half-day by calendar rule but volume is "
                         f"{vols[d] / med:.2f}x the 20-session median — verify")
        rows.append({"session": i, "date": d.isoformat(), "is_half_day": half,
                     "n_securities": len(bars_by_date[d])})
    return rows, notes


# ----------------------------------------------------------------------- main

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--raw-dir", type=Path, default=RAW_CHART)
    ap.add_argument("--out", type=Path, default=L2)
    ap.add_argument("--registry", type=Path, default=REGISTRY)
    ap.add_argument("--sd", type=dt.date.fromisoformat, default=SD)
    ap.add_argument("--ed", type=dt.date.fromisoformat, default=ED)
    args = ap.parse_args()

    asof = dt.date.today().isoformat()
    asof_compact = dt.date.today().strftime("%Y%m%d")

    files = sorted(args.raw_dir.glob("*.json"))
    if not files:
        print(f"FATAL: no payloads in {args.raw_dir} — run pipeline/fetch_yahoo.py first",
              file=sys.stderr)
        return 2
    # The reference join is consumed in memory; it is a computation, not an
    # artifact, so there is no interim layer to keep in sync or to stale out.
    try:
        from build_ref_join import join_rows
        ref = {r["ticker_yahoo"]: r for r in join_rows()[0]}
    except Exception as exc:
        ref = {}
        print(f"WARN: reference join unavailable ({exc}) — sec_master reference "
              f"columns will be empty", file=sys.stderr)

    recs, failed = [], []
    for f in files:
        rec = parse_chart(f)
        if rec.get("error"):
            failed.append((rec["symbol"], rec["error"]))
            continue
        reconstruct(rec)
        compute_adj_factor(rec)
        recs.append(rec)
    print(f"parsed {len(recs)} payloads, {len(failed)} failed")
    for sym, err in failed[:10]:
        print(f"  FAILED {sym}: {err}")

    # security_id: monotonic by listing order (architecture.md §3.4).  Appending
    # delisted names later must extend the tail, never renumber existing ids.
    n_new, n_total = assign_security_ids(recs, ref, args.registry, asof)
    print(f"security_id registry {args.registry}: {n_total} entries "
          f"({n_new} newly assigned this run; existing ids never renumbered)")
    recs.sort(key=lambda r: r["security_id"])

    in_window = lambda d: args.sd <= d <= args.ed
    bars_by_date: dict[dt.date, list] = defaultdict(list)
    events_by_date: dict[dt.date, list] = defaultdict(list)
    dropped_by_date: dict[dt.date, int] = defaultdict(int)
    for rec in recs:
        for d in rec["dropped_null_close_dates"]:
            dropped_by_date[d] += 1
        for bar in rec["bars"]:
            if in_window(bar["date"]):
                bars_by_date[bar["date"]].append((rec, bar))
        for d in rec["divs"]:
            if in_window(d["date"]):
                events_by_date[d["date"]].append((rec, "div", d))
        for s in rec["splits"]:
            if in_window(s["date"]):
                events_by_date[s["date"]].append((rec, "split", s))

    if not bars_by_date:
        print("FATAL: no bars inside the window", file=sys.stderr)
        return 2

    trimmed = trim_trailing_degenerate(bars_by_date)

    # Everything downstream is judged against the SESSION AXIS that survives the
    # trim, never against the requested window.  Those differ whenever a trailing
    # session is dropped, and conflating them put a corp_action file on a date the
    # calendar does not contain (unreachable: architecture.md §5.1 renders that
    # path per session) and credited HUBB -- the one name that did have a close on
    # the dropped session -- with 251 sessions and an empty last_session.
    on_axis = bars_by_date.__contains__
    off_axis = defaultdict(list)
    for d in list(events_by_date):
        if not on_axis(d):
            off_axis[d] = sorted(rec["symbol"] for rec, _, _ in events_by_date[d])
            del events_by_date[d]

    cal_rows, cal_notes = build_calendar(bars_by_date)

    # ---- daily_bar
    n_bar_rows = 0
    for d, items in bars_by_date.items():
        items.sort(key=lambda t: t[0]["security_id"])
        rows = [{
            "date": d.isoformat(), "security_id": rec["security_id"], "ticker": rec["symbol"],
            "open": px(b["raw_open"]), "high": px(b["raw_high"]), "low": px(b["raw_low"]),
            "close": px(b["raw_close"]),
            "volume": "" if b["raw_volume"] is None else int(b["raw_volume"]),
            "adj_factor": fac(b["adj_factor"]), "adj_close_vendor": px(b["adjclose"]),
        } for rec, b in items]
        write_pipe(args.out / "pv" / f"{d:%Y}" / f"{d:%m}" / f"pv.{d:%Y%m%d}", PV_COLS, rows)
        n_bar_rows += len(rows)

    # ---- corp_action (sparse: only event days produce a file)
    n_ca_rows = 0
    for d, items in events_by_date.items():
        items.sort(key=lambda t: t[0]["security_id"])
        rows = []
        for rec, kind, ev in items:
            row = {"date": d.isoformat(), "security_id": rec["security_id"],
                   "ticker": rec["symbol"], "event_type": kind}
            if kind == "div":
                row["div_amount"] = f"{ev['amount']:.6f}"
            else:
                row["split_num"] = f"{ev['num']:g}"
                row["split_den"] = f"{ev['den']:g}"
                row["split_ratio"] = f"{ev['num'] / ev['den']:.10f}"
            rows.append(row)
        write_pipe(args.out / "cax" / f"{d:%Y}" / f"{d:%m}" / f"cax.{d:%Y%m%d}", CAX_COLS, rows)
        n_ca_rows += len(rows)

    # ---- calendar + sec_master
    # One calendar file per year.  `session` is the GLOBAL axis index and keeps
    # counting across the year boundary -- it is architecture.md §3.3's
    # `_axes/sessions.json` in L2 form, so restarting it per file would sever the
    # very thing it exists to provide.
    by_year = defaultdict(list)
    for r in cal_rows:
        by_year[r["date"][:4]].append(r)
    for year, rows_y in sorted(by_year.items()):
        write_pipe(args.out / "calendar" / year / f"calendar.{year}", CALENDAR_COLS, rows_y)
    # --- sec_master + industry, one file per session, PIT row set
    # A security appears on date d only if it was already listed on d.  That is
    # genuine PIT content, not 250 copies of one snapshot: Q (2025-10-27),
    # FDXF (2026-05-27) and HONA (2026-06-15) listed inside the window, so early
    # files carry 500 rows and late ones 503.
    #
    # `ref_asof` is the reference SNAPSHOT date and is deliberately a separate
    # column from `date`.  Our reference sources (NasdaqTrader, SEC, S&P/GICS) are
    # current snapshots with no history, so name/exchange/sector on a 2025 row are
    # backfilled from a 2026 observation.  ref_asof != date makes that visible in
    # the data itself rather than only in a document -- a row that claims to be PIT
    # while silently carrying future attributes is the failure worth preventing.
    ind_rows_last, sm_rows_last = [], []
    for d in sorted(bars_by_date):
        listed = [rec for rec in recs
                  if rec["first_trade_date"] and rec["first_trade_date"] <= d]
        iso = d.isoformat()
        sm_rows = []
        ind_rows = []
        for rec in listed:
            r = ref.get(rec["symbol"], {}) or {}
            sm_rows.append({
                "date": iso, "security_id": rec["security_id"], "ticker": rec["symbol"],
                "ticker_nasdaq": r.get("ticker_nasdaq"), "ticker_cqs": r.get("ticker_cqs"),
                "ticker_yahoo": rec["symbol"], "name": r.get("name"),
                "exchange": r.get("exchange"), "cik": r.get("cik"),
                "is_etf": r.get("is_etf"), "round_lot": r.get("round_lot"),
                "financial_status": r.get("financial_status"),
                "first_trade_date": rec["first_trade_date"].isoformat(),
                "currency": rec["currency"], "ref_asof": asof, "source": "yahoo_v8_chart",
            })
            ind_rows.append({
                "date": iso, "security_id": rec["security_id"], "ticker": rec["symbol"],
                "gics_sector_code": GICS_SECTOR_CODE.get(r.get("gics_sector", "")),
                "gics_sector": r.get("gics_sector"),
                "gics_sub_industry": r.get("gics_sub_industry"),
                "ref_asof": asof, "source": "sp500_constituents",
            })
        write_pipe(args.out / "sec_master" / f"{d:%Y}" / f"{d:%m}" / f"sec_master.{d:%Y%m%d}",
                   SEC_MASTER_COLS, sm_rows)
        write_pipe(args.out / "industry" / f"{d:%Y}" / f"{d:%m}" / f"industry.{d:%Y%m%d}",
                   INDUSTRY_COLS, ind_rows)
        sm_rows_last, ind_rows_last = sm_rows, ind_rows
    n_sm = sum(1 for d in bars_by_date
               for rec in recs if rec["first_trade_date"] and rec["first_trade_date"] <= d)
    unmapped = sorted({r["gics_sector"] for r in ind_rows_last
                       if r["gics_sector"] and r["gics_sector_code"] is None})
    if unmapped:
        print(f"  WARN: GICS sector(s) with no numeric code: {unmapped}")

    # Coverage is derivable from pv and is look-ahead inside a PIT row, so it is
    # reported here rather than stored per date.
    session_of = {r["date"]: r["session"] for r in cal_rows}
    coverage = {}
    for rec in recs:
        mine = sorted(b["date"].isoformat() for b in rec["bars"] if on_axis(b["date"]))
        coverage[rec["symbol"]] = {
            "security_id": rec["security_id"], "n_sessions": len(mine),
            "first_session": session_of.get(mine[0]) if mine else None,
            "last_session": session_of.get(mine[-1]) if mine else None,
        }
    partial = {k: v for k, v in coverage.items() if v["n_sessions"] < len(cal_rows)}

    # Cross-check ours against the vendor on RETURNS, not levels.  An adjustment
    # factor is only defined up to a multiplicative constant, and the vendor
    # anchors its series at a later date than we do -- six names carry a dividend
    # with ex-date on the trailing session we trim, which shifts the whole level
    # by a constant while leaving every return identical.  Comparing levels
    # reports those as divergences; comparing returns tests what actually matters.
    div = []
    for rec in recs:
        usable = [b for b in rec["bars"]
                  if on_axis(b["date"]) and b["adjclose"] and b["adj_factor"] is not None]
        worst, worst_d = 0.0, None
        for a, b in zip(usable, usable[1:]):
            prev_ours = a["raw_close"] * a["adj_factor"]
            if not prev_ours or not a["adjclose"]:
                continue
            r_ours = (b["raw_close"] * b["adj_factor"]) / prev_ours - 1.0
            r_vendor = b["adjclose"] / a["adjclose"] - 1.0
            gap = abs(r_ours - r_vendor)
            if gap > worst:
                worst, worst_d = gap, b["date"]
        if worst > 1e-4:
            div.append((worst, rec["symbol"], worst_d))
    div.sort(reverse=True)

    # Unexplained jumps: a large adjusted return on a date carrying no corporate
    # action is a data-quality flag, not a price.  MNST is the motivating case --
    # the vendor applied its 2:1 to the single bar of 2026-08-06 and to none of
    # its neighbours, leaving a fake -50%/+92% round trip five sessions before
    # the actual ex-date.  Real 40% moves do occur, so this is a review list.
    JUMP = 0.40
    jumps = []
    for rec in recs:
        ev_dates = {e["date"] for e in rec["divs"]} | {e["date"] for e in rec["splits"]}
        usable = [b for b in rec["bars"]
                  if on_axis(b["date"]) and b["adj_factor"] is not None]
        for a, b in zip(usable, usable[1:]):
            prev = a["raw_close"] * a["adj_factor"]
            if not prev:
                continue
            r = (b["raw_close"] * b["adj_factor"]) / prev - 1.0
            if abs(r) > JUMP and b["date"] not in ev_dates:
                jumps.append({"symbol": rec["symbol"], "date": b["date"].isoformat(),
                              "adj_return": round(r, 6)})
    jumps.sort(key=lambda j: -abs(j["adj_return"]))

    # One big jump is usually a real event (MRNA +177% on 199M shares against a
    # 5M average; FISV -44% on a guidance cut).  Several in one name is a broken
    # series: MNST's vendor prices flip between pre- and post-split scale on
    # individual bars for the month before its ex-date.  Aggregate so consumers
    # can exclude a suspect name without re-deriving this.
    per_sym = defaultdict(list)
    for j in jumps:
        per_sym[j["symbol"]].append(j["date"])
    suspects = [{"symbol": sym, "n_unexplained_jumps": len(ds),
                 "first": min(ds), "last": max(ds)}
                for sym, ds in per_sym.items() if len(ds) >= 2]
    suspects.sort(key=lambda x: -x["n_unexplained_jumps"])

    meta = {
        "dataset": "us_daily_pv_pilot", "version": 1, "asof": asof,
        "sd": args.sd.isoformat(), "ed": args.ed.isoformat(),
        "sd_actual": cal_rows[0]["date"], "ed_actual": cal_rows[-1]["date"],
        "n_sessions": len(cal_rows), "n_securities": len(recs),
        "sources": {"prices": "yahoo v8 chart",
                    "ref": "nasdaqtrader nasdaqtraded.txt",
                    "gics": "datasets/s-and-p-500-companies",
                    "cik": "SEC company_tickers_exchange"},
        "known_defects": ["survivorship_bias_no_delisted", "no_vwap", "adj_factor_not_pit"],
        "row_counts": {"pv": n_bar_rows, "cax": n_ca_rows,
                       "sec_master": n_sm, "calendar": len(cal_rows),
                       "industry": n_sm},
        "dropped_null_close_by_date": {d.isoformat(): n for d, n in
                                       sorted(dropped_by_date.items()) if in_window(d)},
        "failed_symbols": [{"symbol": s, "error": e} for s, e in failed],
        "half_day_notes": cal_notes,
        "trimmed_trailing_sessions": [d.isoformat() for d in trimmed],
        "off_axis_events_dropped": {d.isoformat(): syms for d, syms in sorted(off_axis.items())},
        "vendor_return_divergence": [
            {"symbol": sym, "worst_return_gap": round(w, 6), "date": d.isoformat()}
            for w, sym, d in div],
        "splits_vendor_had_not_applied": [
            {"symbol": r["symbol"], "ex_date": sp["date"].isoformat(),
             "ratio": sp["ratio_str"], "evidence": sp["evidence"]}
            for r in recs for sp in r["splits"]
            if not sp["vendor_applied"] and on_axis(sp["date"])],
        "unexplained_jumps_gt_40pct": jumps,
        "suspect_securities": suspects,
        "coverage_full": len(recs) - len(partial),
        "coverage_partial": {k: v for k, v in sorted(
            partial.items(), key=lambda kv: kv[1]["n_sessions"])},
        "validation": "not_run",
    }
    (args.out / "_meta.json").write_text(json.dumps(meta, indent=1) + "\n")

    n_split = sum(1 for r in recs for s in r["splits"] if on_axis(s["date"]))
    n_div = sum(1 for r in recs for d in r["divs"] if on_axis(d["date"]))
    print(f"sessions={len(cal_rows)} securities={len(recs)} "
          f"daily_bar_rows={n_bar_rows} corp_action_rows={n_ca_rows} "
          f"(splits={n_split} divs={n_div})")
    print(f"window requested {args.sd}..{args.ed}  ACTUAL {cal_rows[0]['date']}..{cal_rows[-1]['date']}")
    for d in trimmed:
        print(f"  TRIMMED trailing session {d}: vendor had not settled a close for most names")
    for d, syms in sorted(off_axis.items()):
        print(f"  dropped {len(syms)} corp action(s) on {d} — off the session axis: "
              f"{', '.join(syms)}")
    notapplied = [(r["symbol"], sp) for r in recs for sp in r["splits"]
                  if not sp["vendor_applied"] and in_window(sp["date"])]
    for sym, sp in notapplied:
        print(f"  split NOT pre-applied by vendor: {sym} {sp['date']} {sp['ratio_str']} "
              f"({sp['evidence']}) — quote series left as-traded, not re-inverted")
    if div:
        print(f"  our vs vendor DAILY RETURNS: {len(div)}/{len(recs)} names diverge >1bp")
        for w, sym, d in div[:8]:
            print(f"    {sym:<6} worst return gap {w:.2%} on {d}")
    else:
        print(f"  our adjusted returns match the vendor to within 1bp on all {len(recs)} names")
    if cal_rows[-1]["date"] != args.ed.isoformat():
        print(f"  NOTE: requested ed={args.ed} not in the data — the vendor had not settled a "
              f"close for it; _meta.json records ed_actual={cal_rows[-1]['date']}")
    for d, n in sorted(dropped_by_date.items()):
        if in_window(d):
            share = n / len(recs)
            tag = "vendor-wide incomplete session" if share > 0.5 else "scattered per-name gap"
            print(f"  dropped null-close: {d} {n}/{len(recs)} securities ({share:.0%}) — {tag}")
    for n in cal_notes:
        print(f"  half-day: {n}")
    if jumps:
        print(f"  unexplained |adj return| > {JUMP:.0%} on a date with no corp action: "
              f"{len(jumps)} occurrence(s) — review list, not all are errors")
        for j in jumps[:10]:
            print(f"    {j['symbol']:<6} {j['date']}  {j['adj_return']:+.2%}")
    for sp in suspects:
        print(f"  SUSPECT SERIES {sp['symbol']}: {sp['n_unexplained_jumps']} unexplained jumps "
              f"between {sp['first']} and {sp['last']} — treat this name as unusable in that "
              f"window rather than as price action")
    return 0


if __name__ == "__main__":
    sys.exit(main())
