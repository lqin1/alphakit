#!/usr/bin/env python3
"""alphakit L2 correctness validator.

Implements every assertion of ``docs/l2_schema.md`` §8 (V1..V8) plus a set of
cheap structural checks (X-series) and advisory checks (W-series).

Design notes
------------
* The validator is written against the *contract*, not against whatever happens
  to be on disk.  Absent or unreadable data is a loud FAIL, never a silent pass.
* Files are parsed by hand (byte read -> strict UTF-8 -> split on ``|``) rather
  than with a CSV/dataframe reader, because a tolerant reader would silently
  repair exactly the corruption V8 exists to catch (ragged rows, ``NaN``
  literals, embedded delimiters, CRLF).
* Every check owns the columns whose semantics it asserts; a parse error on a
  column is reported by the owning check so no corruption is reported as PASS.
* V1 compares daily *returns*, not price levels: §7.3 derives ``adj_factor``
  from the corp_action event log, and such a factor is only defined up to a
  multiplicative constant.  The one allowed disagreement with the vendor is
  granted per (security, ex-date) and only when the vendor payload itself
  proves the vendor never back-adjusted that split (§7.1) — never by relaxing
  the 1bp threshold, and never on the producer's say-so alone.
* Exit code: 0 only if V1..V8 all PASS *and* no X-check fails.  W-series
  findings never affect the exit code.

Usage
-----
    .venv/bin/python pipeline/validate_l2.py [--l2-dir data/l2]
                                             [--raw-dir data/raw/yahoo/chart]
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

# --------------------------------------------------------------------------
# contract constants (docs/l2_schema.md §2..§6, §8)
# --------------------------------------------------------------------------

DELIM = "|"

SCHEMA: dict[str, list[str]] = {
    # §3
    # §3 — now PIT: one file per session, leading `date`, coverage columns moved
    # to _meta.json (inside a per-date row they were look-ahead), `asof` renamed
    # `ref_asof` to make the reference-snapshot gap visible.
    "sec_master": [
        "date", "security_id", "ticker", "ticker_nasdaq", "ticker_cqs",
        "ticker_yahoo", "name", "exchange", "cik", "is_etf", "round_lot",
        "financial_status", "first_trade_date", "currency", "ref_asof", "source",
    ],
    # §3.2 — GICS lives in its own table: it is a time-varying PIT attribute
    # from a different source than the securities master.
    "industry": [
        "date", "security_id", "ticker", "gics_sector_code", "gics_sector",
        "gics_sub_industry", "ref_asof", "source",
    ],
    # registry/security_id.{country}.csv — the persistent id axis, outside storage/
    "registry": [
        "security_id", "cik", "ticker_yahoo", "name", "first_trade_date",
        "added_asof",
    ],
    # §4
    "calendar": ["session", "date", "is_half_day", "n_securities"],
    # §5
    "daily_bar": [
        "date", "security_id", "ticker", "open", "high", "low", "close",
        "volume", "adj_factor", "adj_close_vendor",
    ],
    # §6
    "corp_action": [
        "date", "security_id", "ticker", "event_type", "div_amount",
        "split_num", "split_den", "split_ratio",
    ],
}

# §5 / §6 numeric columns, used to decide how strictly a field is parsed.
NUMERIC_COLS: dict[str, set[str]] = {
    "sec_master": {"security_id", "round_lot", "n_sessions"},
    "calendar": {"session", "is_half_day", "n_securities"},
    "daily_bar": {"security_id", "open", "high", "low", "close", "volume",
                  "adj_factor", "adj_close_vendor"},
    "corp_action": {"security_id", "div_amount", "split_num", "split_den",
                    "split_ratio"},
    "industry": {"security_id", "gics_sector_code"},
    "registry": {"security_id"},
}

# §1: on-disk file stem -> logical table kind.  The delivery renamed daily_bar ->
# pv and corp_action -> cax; the older stems stay readable so a stale tree is
# diagnosed rather than reported as "no data".
TABLE_KIND: dict[str, str] = {
    "pv": "daily_bar", "daily_bar": "daily_bar",
    "cax": "corp_action", "corp_action": "corp_action",
    "sec_master": "sec_master", "calendar": "calendar", "industry": "industry",
}
CURRENT_STEM = {"daily_bar": "pv", "corp_action": "cax", "sec_master": "sec_master",
                "calendar": "calendar", "industry": "industry"}
# §1 partitioning: {category}/{YYYY}/{mm}/{subdata}.{YYYYMMDD} for every table
# except calendar, which is {YYYY}/calendar.{YYYY} — one file per year, no month
# level, and a 4-digit year suffix instead of a date.
# every table except calendar is PIT: one file per session under {YYYY}/{mm}/
PIT_TABLES = ("daily_bar", "corp_action", "sec_master", "industry")
YEAR_RE = re.compile(r"^\d{4}$")
MONTH_RE = re.compile(r"^(0[1-9]|1[0-2])$")

# §3.2 official GICS sector codes; max 60 fits architecture.md §5.1's dtype i1.
GICS_SECTOR_CODES = {10: "Energy", 15: "Materials", 20: "Industrials",
                     25: "Consumer Discretionary", 30: "Consumer Staples",
                     35: "Health Care", 40: "Financials",
                     45: "Information Technology", 50: "Communication Services",
                     55: "Utilities", 60: "Real Estate"}
INT8_MIN, INT8_MAX = -128, 127

# §2: "NaN / 缺失 = 空字段 ... 不写 NaN/NULL/nan".  Anything in this set is a
# producer bug regardless of column.
MISSING_LITERALS = {"nan", "null", "none", "nat", "<na>", "n/a", "nil", "(null)"}
# additionally rejected inside numeric columns
NONFINITE_LITERALS = {"inf", "-inf", "+inf", "infinity", "-infinity", "1.#inf"}

V1_TOL = 1e-4          # §8 V1, 1bp on daily *returns* (not levels — §7.3)
V2_TOL = 0.15          # §8 V2, ±15%
# §7.3 recomputation of adj_factor from the event log: ratio-space tolerance.
X10_TOL = 1e-6
# raw_close vs vendor quote.close equality for a split the vendor never applied
# (§7.1); loose enough for the contract's 6-decimal price rounding.
V2_EQ_TOL = 2e-6
V7_COVERAGE_FRAC = 0.5  # §8 V7
W_RETURN_TOL = 0.50    # task §2: |1-day return| > 50% -> WARNING

# A real S&P-500 year always contains split events; if a dataset this large has
# none at all, V2 would pass vacuously.  Guard against that.
V2_VACUOUS_MIN_SESSIONS = 60
V2_VACUOUS_MIN_SECURITIES = 50

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
# alternate delivery layout emitted by pipeline/build_l2.py: flat, extension-less,
# `{table}.{YYYYMMDD}` under one directory.  Recognised, but flagged by X1 while
# docs/l2_schema.md §1 still specifies the nested `.csv` tree.
FLATFILE_RE = re.compile(r"^([a-z_]+)\.(\d{8})$")
INT_RE = re.compile(r"^[+-]?\d+$")
PLAIN_DECIMAL_RE = re.compile(r"^[+-]?(\d+)(\.\d+)?$")

MAX_SHOW_DEFAULT = 10


# --------------------------------------------------------------------------
# check / report plumbing
# --------------------------------------------------------------------------

@dataclass
class Check:
    """One reported assertion line."""

    cid: str
    title: str
    hard: bool = True            # hard -> a violation makes the run exit non-zero
    status: str = "PASS"         # PASS | FAIL | WARN
    n_checked: int = 0
    n_violations: int = 0
    _shown: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    max_show: int = MAX_SHOW_DEFAULT

    def violation(self, msg: str) -> None:
        self.n_violations += 1
        if len(self._shown) < self.max_show:
            self._shown.append(msg)
        self.status = "FAIL" if self.hard else "WARN"

    def note(self, msg: str) -> None:
        """Contextual line always printed under the check (not a violation)."""
        if msg not in self.notes:
            self.notes.append(msg)

    def warn(self, msg: str) -> None:
        """Downgrade-only finding: never turns a hard check into FAIL."""
        self.note("WARN: " + msg)
        if self.status == "PASS" and not self.hard:
            self.status = "WARN"

    @property
    def failed(self) -> bool:
        return self.status == "FAIL"


class Report:
    def __init__(self, max_show: int = MAX_SHOW_DEFAULT) -> None:
        self.checks: list[Check] = []
        self.by_id: dict[str, Check] = {}
        self.max_show = max_show

    def add(self, cid: str, title: str, hard: bool = True) -> Check:
        c = Check(cid=cid, title=title, hard=hard, max_show=self.max_show)
        self.checks.append(c)
        self.by_id[cid] = c
        return c

    def __getitem__(self, cid: str) -> Check:
        return self.by_id[cid]

    # -- rendering ---------------------------------------------------------
    def render(self, header_lines: list[str]) -> str:
        out: list[str] = []
        out.extend(header_lines)
        out.append("")
        out.append("=" * 78)
        out.append("SUMMARY")
        out.append("=" * 78)
        width = max(len(c.cid) for c in self.checks) if self.checks else 3
        for c in self.checks:
            counted = f"{c.n_checked:,}" if c.n_checked else "-"
            viol = f"{c.n_violations:,}" if c.n_violations else "0"
            out.append(
                f"[{c.status:<4}] {c.cid:<{width}}  {c.title:<62.62s} "
                f"checked={counted:>9s} violations={viol:>7s}"
            )
        out.append("")
        out.append("=" * 78)
        out.append("DETAIL")
        out.append("=" * 78)
        any_detail = False
        for c in self.checks:
            if c.status == "PASS" and not c.notes:
                continue
            any_detail = True
            out.append("")
            out.append(f"[{c.status}] {c.cid} — {c.title}")
            for n in c.notes:
                out.append(f"    . {n}")
            if c.n_violations:
                shown = len(c._shown)
                out.append(
                    f"    {c.n_violations:,} violation(s); showing first {shown}:"
                )
                for m in c._shown:
                    out.append(f"      - {m}")
        if not any_detail:
            out.append("")
            out.append("    (nothing to report — all checks clean)")
        out.append("")
        out.append("=" * 78)
        hard_failed = [c.cid for c in self.checks if c.hard and c.failed]
        v_failed = [c.cid for c in self.checks
                    if c.cid.startswith("V") and c.status != "PASS"]
        warned = [c.cid for c in self.checks if c.status == "WARN"]
        out.append(f"contract assertions V1..V8 failing : "
                   f"{', '.join(v_failed) if v_failed else 'none'}")
        out.append(f"all blocking checks failing        : "
                   f"{', '.join(hard_failed) if hard_failed else 'none'}")
        out.append(f"advisory (WARN) checks             : "
                   f"{', '.join(warned) if warned else 'none'}")
        out.append(f"RESULT: {'PASS' if not hard_failed else 'FAIL'}")
        out.append("=" * 78)
        return "\n".join(out) + "\n"


# --------------------------------------------------------------------------
# small parse helpers
# --------------------------------------------------------------------------

def rel(p: Path) -> str:
    try:
        r = os.path.relpath(p, os.getcwd())
    except ValueError:
        return str(p)
    return r if not r.startswith("..") else str(p)


def parse_float(s: str) -> tuple[Optional[float], Optional[str]]:
    if s == "":
        return None, "empty"
    if s.strip().lower() in MISSING_LITERALS | NONFINITE_LITERALS:
        return None, f"missing/non-finite literal {s!r}"
    if s != s.strip():
        return None, f"padded with whitespace {s!r}"
    try:
        v = float(s)
    except ValueError:
        return None, f"not a number {s!r}"
    if not math.isfinite(v):
        return None, f"non-finite {s!r}"
    return v, None


def parse_int(s: str) -> tuple[Optional[int], Optional[str]]:
    if s == "":
        return None, "empty"
    if not INT_RE.match(s):
        return None, f"not an integer literal {s!r}"
    return int(s), None


def parse_date(s: str) -> tuple[Optional[date], Optional[str]]:
    if s == "":
        return None, "empty"
    if not DATE_RE.match(s):
        return None, f"not YYYY-MM-DD {s!r}"
    try:
        return date.fromisoformat(s), None
    except ValueError:
        return None, f"not a valid calendar date {s!r}"


def decimals(s: str) -> Optional[int]:
    m = PLAIN_DECIMAL_RE.match(s)
    if not m:
        return None
    return len(m.group(2)) - 1 if m.group(2) else 0


def pct(a: float, b: float) -> float:
    return abs(a - b) / abs(b) if b else float("inf")


def quantile(sorted_vals: list[float], q: float) -> float:
    if not sorted_vals:
        return float("nan")
    if len(sorted_vals) == 1:
        return float(sorted_vals[0])
    pos = q * (len(sorted_vals) - 1)
    lo = int(math.floor(pos))
    hi = int(math.ceil(pos))
    if lo == hi:
        return float(sorted_vals[lo])
    return float(sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (pos - lo))


# --------------------------------------------------------------------------
# raw file reading + V8 (structural) pass
# --------------------------------------------------------------------------

@dataclass
class PipeFile:
    path: Path
    kind: str
    header: list[str]
    rows: list[dict[str, str]]     # structurally valid rows only
    linenos: list[int]
    readable: bool = True


def read_pipe_file(path: Path, kind: str, v8: Check, x_hdr: Check) -> Optional[PipeFile]:
    """Byte-level read + every structural assertion of §2 / §8."""
    try:
        data = path.read_bytes()
    except OSError as e:
        v8.violation(f"{rel(path)}: unreadable ({e})")
        return None

    if data.startswith(b"\xef\xbb\xbf"):
        v8.violation(f"{rel(path)}: starts with a UTF-8 BOM (§2 requires plain UTF-8)")
        data = data[3:]
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as e:
        v8.violation(f"{rel(path)}: not valid UTF-8 ({e})")
        return None

    if data and not data.endswith(b"\n"):
        v8.note(f"{rel(path)}: no trailing LF on the final line (§2 LF line endings)")

    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    if not lines:
        v8.violation(f"{rel(path)}: file is empty (no header row)")
        return None

    hdr_line = lines[0]
    if "\r" in hdr_line:
        v8.violation(f"{rel(path)}:1: CR in header (§2 requires LF line endings)")
    header = hdr_line.replace("\r", "").split(DELIM)
    expect = SCHEMA.get(kind)
    if expect is not None and header != expect:
        x_hdr.violation(
            f"{rel(path)}:1: header does not match the {kind} contract schema\n"
            f"          expected: {DELIM.join(expect)}\n"
            f"          actual  : {DELIM.join(header)}"
        )
    dup_hdr = [h for h, n in Counter(header).items() if n > 1]
    if dup_hdr:
        x_hdr.violation(f"{rel(path)}:1: duplicate column name(s) in header: {dup_hdr}")

    ncol = len(header)
    rows: list[dict[str, str]] = []
    linenos: list[int] = []
    numeric = NUMERIC_COLS.get(kind, set())
    for i, raw in enumerate(lines[1:], start=2):
        v8.n_checked += 1
        line = raw
        if "\r" in line:
            v8.violation(f"{rel(path)}:{i}: CR inside line (§2 requires LF only)")
            line = line.replace("\r", "")
        fields = line.split(DELIM)
        if len(fields) != ncol:
            if line.strip() == "":
                v8.violation(f"{rel(path)}:{i}: blank line (expected {ncol} fields)")
            elif len(fields) > ncol:
                v8.violation(
                    f"{rel(path)}:{i}: {len(fields)} fields but header has {ncol} — "
                    f"a text field almost certainly contains a raw '{DELIM}' "
                    f"(§2 requires it be replaced with a space); line={line[:160]!r}"
                )
            else:
                v8.violation(
                    f"{rel(path)}:{i}: {len(fields)} fields but header has {ncol}; "
                    f"line={line[:160]!r}"
                )
            continue
        row = dict(zip(header, fields))
        bad = False
        for col, val in row.items():
            low = val.strip().lower()
            if low in MISSING_LITERALS:
                v8.violation(
                    f"{rel(path)}:{i}: column {col!r} holds the literal {val!r}; "
                    f"§2 requires missing values be EMPTY fields"
                )
                bad = True
            elif col in numeric and low in NONFINITE_LITERALS:
                v8.violation(
                    f"{rel(path)}:{i}: numeric column {col!r} holds {val!r}"
                )
                bad = True
            elif val != val.strip() and val.strip() != "":
                v8.violation(
                    f"{rel(path)}:{i}: column {col!r} has leading/trailing "
                    f"whitespace {val!r}"
                )
                bad = True
        if bad:
            continue
        rows.append(row)
        linenos.append(i)

    return PipeFile(path=path, kind=kind, header=header, rows=rows, linenos=linenos)


# --------------------------------------------------------------------------
# typed models
# --------------------------------------------------------------------------

@dataclass
class Bar:
    path: Path
    lineno: int
    date_s: str
    d: Optional[date]
    sid: Optional[int]
    ticker: str
    o: Optional[float]
    h: Optional[float]
    lo: Optional[float]
    c: Optional[float]
    vol: Optional[float]
    af: Optional[float]
    acv: Optional[float]
    err: dict[str, str]
    raw: dict[str, str]

    def where(self) -> str:
        return (f"{rel(self.path)}:{self.lineno} date={self.date_s} "
                f"sid={self.raw.get('security_id','')} "
                f"ticker={self.raw.get('ticker','')}")


@dataclass
class CorpAction:
    path: Path
    lineno: int
    date_s: str
    d: Optional[date]
    sid: Optional[int]
    ticker: str
    event_type: str
    div_amount: Optional[float]
    split_num: Optional[float]
    split_den: Optional[float]
    split_ratio: Optional[float]
    err: dict[str, str]
    raw: dict[str, str]

    def where(self) -> str:
        return (f"{rel(self.path)}:{self.lineno} date={self.date_s} "
                f"sid={self.raw.get('security_id','')} "
                f"ticker={self.raw.get('ticker','')} "
                f"event={self.event_type!r}")


@dataclass
class SecRow:
    path: Path
    lineno: int
    sid: Optional[int]
    raw: dict[str, str]
    err: dict[str, str]

    def where(self) -> str:
        return (f"{rel(self.path)}:{self.lineno} "
                f"security_id={self.raw.get('security_id','')!r} "
                f"ticker_yahoo={self.raw.get('ticker_yahoo','')!r}")


@dataclass
class CalRow:
    path: Path
    lineno: int
    session: Optional[int]
    d: Optional[date]
    is_half_day: str
    n_securities: Optional[int]
    raw: dict[str, str]
    err: dict[str, str]


def type_bar(path: Path, lineno: int, r: dict[str, str]) -> Bar:
    err: dict[str, str] = {}

    def f(col: str) -> Optional[float]:
        v, e = parse_float(r.get(col, ""))
        if e:
            err[col] = e
        return v

    d, e = parse_date(r.get("date", ""))
    if e:
        err["date"] = e
    sid, e = parse_int(r.get("security_id", ""))
    if e:
        err["security_id"] = e
    return Bar(path=path, lineno=lineno, date_s=r.get("date", ""), d=d, sid=sid,
               ticker=r.get("ticker", ""), o=f("open"), h=f("high"), lo=f("low"),
               c=f("close"), vol=f("volume"), af=f("adj_factor"),
               acv=f("adj_close_vendor"), err=err, raw=r)


def type_ca(path: Path, lineno: int, r: dict[str, str]) -> CorpAction:
    err: dict[str, str] = {}

    def f(col: str) -> Optional[float]:
        s = r.get(col, "")
        if s == "":
            return None
        v, e = parse_float(s)
        if e:
            err[col] = e
        return v

    d, e = parse_date(r.get("date", ""))
    if e:
        err["date"] = e
    sid, e = parse_int(r.get("security_id", ""))
    if e:
        err["security_id"] = e
    return CorpAction(path=path, lineno=lineno, date_s=r.get("date", ""), d=d,
                      sid=sid, ticker=r.get("ticker", ""),
                      event_type=r.get("event_type", ""),
                      div_amount=f("div_amount"), split_num=f("split_num"),
                      split_den=f("split_den"), split_ratio=f("split_ratio"),
                      err=err, raw=r)


# --------------------------------------------------------------------------
# vendor (Yahoo v8 chart) reader — read-only
# --------------------------------------------------------------------------

@dataclass
class RefSlice:
    """One PIT reference file (sec_master or industry) for a single session."""

    path: Path
    d: date
    n_rows: int = 0
    ids: list[int] = field(default_factory=list)
    ticker: dict[int, str] = field(default_factory=dict)
    yahoo: dict[int, str] = field(default_factory=dict)
    ftd: dict[int, str] = field(default_factory=dict)
    cik: dict[int, str] = field(default_factory=dict)
    ref_asofs: set[str] = field(default_factory=set)


@dataclass
class SplitClass:
    """One split event classified per §7.1 (did the vendor back-adjust it?)."""

    ca: "CorpAction"
    ratio: float
    applied: Optional[bool] = None   # None -> could not be classified
    jump: Optional[float] = None     # quote.close(prev) / quote.close(ex)
    vprev: Optional[date] = None
    reason: str = ""                 # why it could not be classified
    indistinct: bool = False         # ratio ~ 1 -> both hypotheses coincide


@dataclass
class VendorSeries:
    symbol: str
    close_by_date: dict[date, float]
    adjclose_by_date: dict[date, float]
    splits: list[tuple[date, float, float]]      # (ex_date, numerator, denominator)
    dividends: list[tuple[date, float]]          # (ex_date, amount)
    problems: list[str]


def _epoch_to_date(ts: int, offset: int) -> date:
    return datetime.fromtimestamp(int(ts) + offset, tz=timezone.utc).date()


def load_vendor_chart(path: Path,
                      calendar_dates: set[date]) -> tuple[Optional[VendorSeries], Optional[str]]:
    try:
        with path.open("rb") as fh:
            payload = json.load(fh)
    except (OSError, ValueError) as e:
        return None, f"{rel(path)}: unreadable/invalid JSON ({e})"

    chart = payload.get("chart") if isinstance(payload, dict) else None
    if not isinstance(chart, dict):
        return None, f"{rel(path)}: no 'chart' object in payload"
    if chart.get("error"):
        return None, f"{rel(path)}: vendor payload carries an error: {chart['error']!r}"
    results = chart.get("result")
    if not results:
        return None, f"{rel(path)}: chart.result is empty/null"
    res = results[0]
    meta = res.get("meta") or {}
    symbol = str(meta.get("symbol", path.stem))
    problems: list[str] = []

    ts = res.get("timestamp") or []
    indicators = res.get("indicators") or {}
    qlist = indicators.get("quote") or [{}]
    quote = qlist[0] if qlist else {}
    closes = quote.get("close") or []
    alist = indicators.get("adjclose") or [{}]
    adjclose = (alist[0] if alist else {}).get("adjclose") or []

    # pick the epoch->date offset that best lines up with the trading calendar
    cands: list[int] = []
    gmt = meta.get("gmtoffset")
    if isinstance(gmt, int):
        cands.append(gmt)
    cands.extend([0, -14400, -18000])
    best_off, best_hits = 0, -1
    for off in dict.fromkeys(cands):
        hits = sum(1 for t in ts[:400]
                   if _epoch_to_date(t, off) in calendar_dates) if calendar_dates else 0
        if hits > best_hits:
            best_off, best_hits = off, hits
    if calendar_dates and ts and best_hits == 0:
        problems.append(
            f"{rel(path)}: none of {len(ts)} vendor timestamps map onto a "
            f"calendar session under any tried gmt offset"
        )

    close_by_date: dict[date, float] = {}
    adj_by_date: dict[date, float] = {}
    for i, t in enumerate(ts):
        try:
            d = _epoch_to_date(t, best_off)
        except (OverflowError, OSError, ValueError, TypeError):
            continue
        if i < len(closes) and isinstance(closes[i], (int, float)):
            close_by_date[d] = float(closes[i])
        if i < len(adjclose) and isinstance(adjclose[i], (int, float)):
            adj_by_date[d] = float(adjclose[i])

    events = res.get("events") or {}

    def ev_values(node: Any) -> Iterable[dict]:
        if isinstance(node, dict):
            return [v for v in node.values() if isinstance(v, dict)]
        if isinstance(node, list):
            return [v for v in node if isinstance(v, dict)]
        return []

    splits: list[tuple[date, float, float]] = []
    for ev in ev_values(events.get("splits")):
        try:
            d = _epoch_to_date(int(ev["date"]), best_off)
        except (KeyError, TypeError, ValueError, OverflowError, OSError):
            problems.append(f"{rel(path)}: split event with unusable date: {ev!r}")
            continue
        num = ev.get("numerator")
        den = ev.get("denominator")
        if not isinstance(num, (int, float)) or not isinstance(den, (int, float)):
            sr = str(ev.get("splitRatio", ""))
            if ":" in sr:
                try:
                    num, den = (float(x) for x in sr.split(":", 1))
                except ValueError:
                    problems.append(f"{rel(path)}: unparseable splitRatio {sr!r}")
                    continue
            else:
                problems.append(f"{rel(path)}: split event without num/den: {ev!r}")
                continue
        splits.append((d, float(num), float(den)))

    dividends: list[tuple[date, float]] = []
    for ev in ev_values(events.get("dividends")):
        try:
            d = _epoch_to_date(int(ev["date"]), best_off)
            amt = float(ev["amount"])
        except (KeyError, TypeError, ValueError, OverflowError, OSError):
            problems.append(f"{rel(path)}: dividend event with unusable fields: {ev!r}")
            continue
        dividends.append((d, amt))

    return VendorSeries(symbol=symbol, close_by_date=close_by_date,
                        adjclose_by_date=adj_by_date, splits=splits,
                        dividends=dividends, problems=problems), None


# --------------------------------------------------------------------------
# main validation
# --------------------------------------------------------------------------

class Validator:
    def __init__(self, l2_dir: Path, raw_dir: Path, max_show: int, skip_raw: bool,
                 registry: Optional[Path] = None):
        self.l2 = l2_dir
        self.raw = raw_dir
        self.registry = registry or (l2_dir / "registry" / "security_id.us.csv")
        self.skip_raw = skip_raw
        self.rep = Report(max_show=max_show)
        r = self.rep
        # contract assertions (§8)
        self.v1 = r.add("V1", "adjusted daily RETURNS match the vendor within 1bp per security")
        self.v2 = r.add("V2", "split inversion direction, conditional on vendor-applied state (§7.1)")
        self.v3 = r.add("V3", "date axis: calendar sessions <-> pv files, no orphans/gaps")
        self.v4 = r.add("V4", "referential integrity: no dup security_id per pv/cax file, all in sec_master")
        self.v5 = r.add("V5", "OHLC sanity: low<=min(o,c), high>=max(o,c), high>=low, vol>=0, px>0")
        self.v6 = r.add("V6", "sec_master ids unique, contiguous from 1; (security_id,ticker_yahoo) bijective")
        self.v7 = r.add("V7", "coverage: per-security session counts; list n_sessions < 0.5*total")
        self.v8 = r.add("V8", "format: field count == header count, no raw '|', missing = empty field")
        # extra structural checks (blocking)
        self.x1 = r.add("X1", "header of every L2 file matches the contract schema")
        self.x2 = r.add("X2", "filename YYYYMMDD == date column in every row; cax on the session axis")
        self.x3 = r.add("X3", "rows sorted by security_id ascending (§2)")
        self.x4 = r.add("X4", "no duplicate (date, security_id) across the whole pv set")
        self.x5 = r.add("X5", "cax: split_ratio==num/den, div_amount>0, fields mutually exclusive")
        self.x6 = r.add("X6", "coverage recomputed from pv == _meta.json coverage_full/coverage_partial")
        self.x7 = r.add("X7", "ticker in pv/cax matches sec_master")
        self.x8 = r.add("X8", "calendar internal consistency (session ids, dates, n_securities)")
        self.x9 = r.add("X9", "cax events reconcile with raw vendor payload events")
        self.x10 = r.add("X10", "adj_factor recomputed from the cax event log (§7.3)")
        self.x11 = r.add("X11", "_meta.json splits_vendor_had_not_applied == observed (§7.1)")
        self.x12 = r.add("X12", "industry: sec_master parity, GICS code<->name 1:1, int8 range")
        self.x13 = r.add("X13", "session axis is GLOBAL across calendar year files (no reset)")
        self.x14 = r.add("X14", "sec_master/industry are PIT: row set == {first_trade_date <= d}")
        self.x15 = r.add("X15", "security_id registry: unique, contiguous, superset of the data")
        # advisory
        self.w1 = r.add("W1", "single-day |return| > 50% on adj_close_vendor (possible missed cax)",
                        hard=False)
        self.w2 = r.add("W2", "numeric formatting (§2: px 6dp, factor 10dp, volume integer)",
                        hard=False)
        self.w3 = r.add("W3", "misc data smells (zero volume, id ordering rule, _meta.json)",
                        hard=False)

        # state
        self.sec_rows: list[SecRow] = []
        self.by_sid: dict[int, SecRow] = {}
        self.cal_rows: list[CalRow] = []
        self.cal_dates: list[date] = []
        self.cal_date_set: set[date] = set()
        self.bars: list[Bar] = []
        self.bars_by_sid: dict[int, dict[date, Bar]] = defaultdict(dict)
        self.cas: list[CorpAction] = []
        self.bar_files: dict[date, Path] = {}
        self.n_bar_rows_by_date: dict[date, int] = defaultdict(int)
        self.sm_files: dict[date, Path] = {}
        self.ind_files: dict[date, Path] = {}
        self.sm_slices: dict[date, RefSlice] = {}
        self.ind_slices: dict[date, RefSlice] = {}
        self.sec_union: dict[int, dict[str, str]] = {}
        self.registry_rows: list[tuple[int, dict[str, str]]] = []
        self.cal_files: list[tuple[int, Path]] = []
        self.ca_files: list[tuple[date, Path]] = []
        self.meta: Optional[dict] = None
        self.meta_err: Optional[str] = None
        self._vendor: dict[str, VendorSeries] = {}
        self._splits: list[SplitClass] = []


    # -- PIT reference slices ----------------------------------------------
    def read_ref_slice(self, path: Path, d: date, kind: str) -> Optional[RefSlice]:
        """Read one per-session sec_master/industry file into a compact slice.

        Only the columns other checks need are retained: 250 files x ~503 rows is
        too much to keep as raw dicts, and every value here repeats across files,
        so strings are interned.
        """
        pf = read_pipe_file(path, kind, self.v8, self.x1)
        if pf is None:
            return None
        sl = RefSlice(path=path, d=d)
        owner = self.v6 if kind == "sec_master" else self.x12
        prev_sid: Optional[int] = None
        seen: dict[int, int] = {}
        for ln, row in zip(pf.linenos, pf.rows):
            sl.n_rows += 1
            sid, e = parse_int(row.get("security_id", ""))
            if e:
                owner.violation(f"{rel(path)}:{ln}: security_id {e}")
                continue
            assert sid is not None
            if sid in seen:
                owner.violation(
                    f"{rel(path)}:{ln}: duplicate security_id {sid} (first seen at "
                    f"line {seen[sid]}); §2 requires it be unique per file")
            else:
                seen[sid] = ln
            if prev_sid is not None and sid <= prev_sid:
                self.x3.violation(
                    f"{rel(path)}:{ln}: security_id {sid} not > previous {prev_sid} "
                    f"(§2 requires ascending order)")
            prev_sid = sid
            sl.ids.append(sid)
            sl.ticker[sid] = sys.intern(row.get("ticker", ""))
            if kind == "sec_master":
                sl.yahoo[sid] = sys.intern(row.get("ticker_yahoo", ""))
                sl.ftd[sid] = sys.intern(row.get("first_trade_date", ""))
                sl.cik[sid] = sys.intern(row.get("cik", ""))
            else:
                sl.yahoo[sid] = sys.intern(row.get("gics_sector_code", ""))
                sl.ftd[sid] = sys.intern(row.get("gics_sector", ""))
                sl.cik[sid] = sys.intern(row.get("gics_sub_industry", ""))
            sl.ref_asofs.add(sys.intern(row.get("ref_asof", "")))
            # every PIT row carries the session it describes (§2)
            rd, e = parse_date(row.get("date", ""))
            if e:
                self.x2.violation(f"{rel(path)}:{ln}: date column {e}")
            elif rd != d:
                self.x2.violation(
                    f"{rel(path)}:{ln}: date column {rd} != filename date {d}")
        if not sl.ids:
            owner.violation(f"{rel(path)} has a header but zero usable data rows")
        return sl

    # -- layout discovery ---------------------------------------------------
    def discover(self) -> None:
        """Locate the five L2 tables under the §1 partitioned layout.

            {category}/{YYYY}/{mm}/{subdata}.{YYYYMMDD}    pv, cax, sec_master, industry
            calendar/{YYYY}/calendar.{YYYY}                one file per year, no mm
            _meta.json                                     at the country root

        `category` and `subdata` are the same table name, so the partition
        directory and the filename must agree with each other and with the date
        in the suffix.  A table left at an older path — un-partitioned at the
        root, or still carrying the pre-rename daily_bar/corp_action stem — is an
        X1 failure rather than silently-missing data.
        """
        l2 = self.l2
        if not l2.is_dir():
            return
        found: dict[str, list[tuple[date, Path]]] = defaultdict(list)
        stems_seen: set[str] = set()

        def note_stem(stem: str, kind: str, where: Path) -> None:
            stems_seen.add(stem)
            want = CURRENT_STEM[kind]
            if stem != want:
                self.x1.violation(
                    f"{rel(where)}: file stem `{stem}`, but §1 names this table "
                    f"`{want}` — a stale delivery, or the rename was applied to only "
                    f"some tables")

        for entry in sorted(l2.iterdir()):
            name = entry.name
            if name in ("_meta.json", "_validation_report.txt"):
                continue
            if entry.is_file():
                m = FLATFILE_RE.match(name)
                if m and TABLE_KIND.get(m.group(1)):
                    self.x1.violation(
                        f"{rel(entry)}: table file sitting un-partitioned at the L2 "
                        f"root; §1 requires "
                        f"{TABLE_KIND[m.group(1)] and CURRENT_STEM[TABLE_KIND[m.group(1)]]}"
                        f"/{{YYYY}}/{{mm}}/{name} — a half-applied restructure hides "
                        f"data from every per-session check")
                else:
                    self.w3.warn(f"{rel(entry)}: unexpected file in the L2 root")
                continue
            kind = TABLE_KIND.get(name)
            if kind is None:
                self.w3.warn(f"{rel(entry)}: unexpected directory in the L2 root — §1 "
                             f"partitions by {sorted(set(CURRENT_STEM.values()))}")
                continue
            if name != CURRENT_STEM[kind]:
                self.x1.violation(
                    f"{rel(entry)}: partition directory named `{name}`, but §1 names "
                    f"this table `{CURRENT_STEM[kind]}`")

            for ydir in sorted(entry.iterdir()):
                if ydir.is_file():
                    self.x1.violation(
                        f"{rel(ydir)}: file directly under {name}/; §1 requires a "
                        f"{{YYYY}}/ partition level")
                    continue
                if not YEAR_RE.match(ydir.name):
                    self.x1.violation(f"{rel(ydir)}: not a 4-digit year partition (§1)")
                    continue
                year = int(ydir.name)

                if kind == "calendar":
                    # calendar/{YYYY}/calendar.{YYYY} — no month level
                    for q in sorted(ydir.iterdir()):
                        if not q.is_file():
                            self.x1.violation(
                                f"{rel(q)}: calendar has no {{mm}} partition level; §1 "
                                f"is calendar/{{YYYY}}/calendar.{{YYYY}}")
                            continue
                        stem, _, suffix = q.name.rpartition(".")
                        if not stem or not YEAR_RE.match(suffix):
                            self.x1.violation(
                                f"{rel(q)}: expected calendar.{{YYYY}} (a 4-digit year "
                                f"suffix, not a date)")
                            continue
                        k = TABLE_KIND.get(stem)
                        if k != "calendar":
                            self.x1.violation(f"{rel(q)}: stem {stem!r} under "
                                              f"calendar/{ydir.name}/")
                            continue
                        note_stem(stem, k, q)
                        if int(suffix) != year:
                            self.x2.violation(
                                f"{rel(q)}: filename year {suffix} != partition "
                                f"directory {ydir.name}")
                        self.cal_files.append((int(suffix), q))
                    continue

                for mdir in sorted(ydir.iterdir()):
                    if mdir.is_file():
                        self.x1.violation(
                            f"{rel(mdir)}: file directly under {name}/{ydir.name}/; §1 "
                            f"requires a {{mm}} partition level")
                        continue
                    if not MONTH_RE.match(mdir.name):
                        self.x1.violation(
                            f"{rel(mdir)}: not a 2-digit month partition 01..12 (§1)")
                        continue
                    month = int(mdir.name)
                    for q in sorted(mdir.iterdir()):
                        if not q.is_file():
                            self.w3.warn(f"{rel(q)}: unexpected directory below {{mm}}")
                            continue
                        m = FLATFILE_RE.match(q.name)
                        k = TABLE_KIND.get(m.group(1)) if m else None
                        if not m or k is None:
                            self.x1.violation(
                                f"{rel(q)}: expected {CURRENT_STEM[kind]}.{{YYYYMMDD}}")
                            continue
                        if k != kind:
                            self.x1.violation(
                                f"{rel(q)}: a {CURRENT_STEM[k]} file inside the "
                                f"{name}/ partition")
                            continue
                        note_stem(m.group(1), k, q)
                        try:
                            d = datetime.strptime(m.group(2), "%Y%m%d").date()
                        except ValueError:
                            self.x2.violation(
                                f"{rel(q)}: {m.group(2)!r} is not a valid YYYYMMDD date "
                                f"(§2: filenames are always YYYYMMDD)")
                            continue
                        if (d.year, d.month) != (year, month):
                            self.x2.violation(
                                f"{rel(q)}: date {d} does not match its partition "
                                f"{name}/{ydir.name}/{mdir.name}")
                        found[kind].append((d, q))

        # sec_master and industry are PIT: one file per session, like pv/cax
        for kind, target in (("sec_master", self.sm_files),
                             ("industry", self.ind_files)):
            for d, q in found.get(kind, []):
                if d in target:
                    self.x1.violation(f"{rel(q)}: duplicate {CURRENT_STEM[kind]} file "
                                      f"for {d}")
                target[d] = q
            if not target:
                who = (self.v4, self.v6, self.v7, self.x7, self.x14) \
                    if kind == "sec_master" else (self.x12, self.x14)
                for c in who:
                    c.violation(
                        f"{rel(l2)} contains no {CURRENT_STEM[kind]}/{{YYYY}}/{{mm}}/"
                        f"{CURRENT_STEM[kind]}.{{YYYYMMDD}} files (§1/§3) — every table "
                        f"except calendar is point-in-time")

        for d, q in found.get("daily_bar", []):
            if d in self.bar_files:
                self.v3.violation(f"{rel(q)}: duplicate pv file for {d}")
            self.bar_files[d] = q
        self.ca_files = sorted(found.get("corp_action", []))
        self.cal_files.sort()
        seen_year: dict[int, Path] = {}
        for y, q in self.cal_files:
            if y in seen_year:
                self.x1.violation(f"{rel(q)}: duplicate calendar file for {y} "
                                  f"(also {rel(seen_year[y])})")
            seen_year[y] = q

        if not self.bar_files:
            for c in (self.v1, self.v3, self.v5, self.v7):
                c.violation(f"{rel(l2)} contains no pv/{{YYYY}}/{{mm}}/pv.{{YYYYMMDD}} "
                            f"files (§1/§5)")
        if not self.ca_files:
            self.x5.note(f"{rel(l2)} has no cax files — cax is sparse (§6), but a "
                         f"12-month S&P universe always has events")
            self.v2.note("no cax files: zero split events available to check")

    # -- discovery / load -------------------------------------------------
    def load(self) -> None:
        l2 = self.l2
        if not l2.is_dir():
            msg = f"L2 directory {rel(l2)} does not exist — nothing to validate"
            for c in self.rep.checks:
                if c.hard:
                    c.violation(msg)
            return

        self.discover()
        # --- _meta.json (§9); parsed once, consumed by X11 and W3
        mp = l2 / "_meta.json"
        if mp.is_file():
            try:
                loaded = json.loads(mp.read_text(encoding="utf-8"))
            except (OSError, ValueError) as e:
                self.meta_err = f"{rel(mp)}: unreadable/invalid JSON ({e})"
            else:
                if isinstance(loaded, dict):
                    self.meta = loaded
                else:
                    self.meta_err = f"{rel(mp)}: top level is not a JSON object"
        else:
            self.meta_err = (f"{rel(mp)} is missing (§1/§9 require it: range, sources, "
                             f"asof, known defects, row counts)")


        # --- sec_master: one PIT file per session (§3)
        for d in sorted(self.sm_files):
            sl = self.read_ref_slice(self.sm_files[d], d, "sec_master")
            if sl is not None:
                self.sm_slices[d] = sl
        if self.sm_files and not self.sm_slices:
            for c in (self.v4, self.v6, self.v7, self.x7):
                c.violation("no sec_master file yielded usable rows")
        # union view: every id ever registered, carrying its most recent PIT row
        for d in sorted(self.sm_slices):
            sl = self.sm_slices[d]
            for sid in sl.ids:
                self.sec_union[sid] = {"ticker": sl.ticker.get(sid, ""),
                                       "ticker_yahoo": sl.yahoo.get(sid, ""),
                                       "first_trade_date": sl.ftd.get(sid, ""),
                                       "cik": sl.cik.get(sid, ""),
                                       "date": str(d)}
        last_sm = self.sm_slices[max(self.sm_slices)] if self.sm_slices else None
        for sid, info in sorted(self.sec_union.items()):
            path = last_sm.path if last_sm else Path("sec_master")
            self.sec_rows.append(SecRow(path=path, lineno=0, sid=sid,
                                        raw=dict(info), err={}))

        # --- calendar: one file per year (§1), concatenated in year order.  The
        #     session axis is global and must survive that concatenation (X13).
        if not self.cal_files:
            for c in (self.v3, self.v7, self.x8, self.x13):
                c.violation(f"no calendar/{{YYYY}}/calendar.{{YYYY}} file under "
                            f"{rel(l2)} (§1/§4)")
        for year, cal_path in self.cal_files:
            pf = read_pipe_file(cal_path, "calendar", self.v8, self.x1)
            if not pf:
                continue
            n_before = len(self.cal_rows)
            for ln, row in zip(pf.linenos, pf.rows):
                err = {}
                sess, e = parse_int(row.get("session", ""))
                if e:
                    err["session"] = e
                d, e = parse_date(row.get("date", ""))
                if e:
                    err["date"] = e
                nsec, e = parse_int(row.get("n_securities", ""))
                if e:
                    err["n_securities"] = e
                if d is not None and d.year != year:
                    # calendar's suffix is a YEAR, not a date: assert containment
                    self.x2.violation(
                        f"{rel(cal_path)}:{ln}: date {d} is not inside the file's year "
                        f"{year} (§1: calendar.{{YYYY}} holds that year's sessions)")
                self.cal_rows.append(CalRow(path=cal_path, lineno=ln, session=sess,
                                            d=d, is_half_day=row.get("is_half_day", ""),
                                            n_securities=nsec, raw=row, err=err))
            if len(self.cal_rows) == n_before:
                for c in (self.v3, self.v7, self.x8):
                    c.violation(f"{rel(cal_path)} has a header but zero data rows")
        self.cal_dates = sorted({r.d for r in self.cal_rows if r.d})
        self.cal_date_set = set(self.cal_dates)

        # --- industry: one PIT file per session (§3.2)
        for d in sorted(self.ind_files):
            sl = self.read_ref_slice(self.ind_files[d], d, "industry")
            if sl is not None:
                self.ind_slices[d] = sl

        # --- daily_bar rows (paths come from discover())
        if self.bar_files:
            for fdate, p in sorted(self.bar_files.items()):
                pf = read_pipe_file(p, "daily_bar", self.v8, self.x1)
                if not pf:
                    continue
                prev_sid: Optional[int] = None
                seen_sid: dict[int, int] = {}
                for ln, row in zip(pf.linenos, pf.rows):
                    bar = type_bar(p, ln, row)
                    self.bars.append(bar)
                    self.n_bar_rows_by_date[fdate] += 1
                    # X2 filename/date agreement
                    if bar.d is None:
                        self.x2.violation(f"{bar.where()}: unparseable date column "
                                          f"({bar.err.get('date')})")
                    elif bar.d != fdate:
                        self.x2.violation(
                            f"{bar.where()}: date column {bar.d} != filename date {fdate}")
                    # X3 ordering
                    if bar.sid is not None:
                        if prev_sid is not None and bar.sid <= prev_sid:
                            self.x3.violation(
                                f"{bar.where()}: security_id {bar.sid} not > previous "
                                f"{prev_sid} (§2 requires ascending order)")
                        prev_sid = bar.sid
                        # V4 in-file duplicate
                        if bar.sid in seen_sid:
                            self.v4.violation(
                                f"{bar.where()}: duplicate security_id {bar.sid} "
                                f"(first seen at line {seen_sid[bar.sid]})")
                        else:
                            seen_sid[bar.sid] = ln
                        if bar.d is not None:
                            self.bars_by_sid[bar.sid][bar.d] = bar

        # --- corp_action rows (paths come from discover())
        if self.ca_files:
            for fdate, p in self.ca_files:
                if self.cal_date_set and fdate not in self.cal_date_set:
                    # Not V3: §8 V3 is worded about daily_bar <-> calendar only.  This
                    # is still dead data — architecture.md §5.1 renders the corp_action
                    # path per session, so a file off the axis is never read.
                    extra = ""
                    trimmed = (self.meta or {}).get("trimmed_trailing_sessions") or []
                    if str(fdate) in [str(x) for x in trimmed]:
                        extra = (f"; _meta.json lists {fdate} under "
                                 f"trimmed_trailing_sessions, so the session was "
                                 f"deliberately dropped but this event file was left "
                                 f"behind")
                    self.x2.violation(
                        f"{rel(p)}: corp_action file dated {fdate} is not a calendar "
                        f"session — the engine renders this path per session (§5.1), so "
                        f"the file is unreachable{extra}")
                pf = read_pipe_file(p, "corp_action", self.v8, self.x1)
                if not pf:
                    continue
                prev_sid = None
                seen_key: dict[tuple[int, str], int] = {}
                seen_sid_ca: dict[int, list[str]] = defaultdict(list)
                for ln, row in zip(pf.linenos, pf.rows):
                    ca = type_ca(p, ln, row)
                    self.cas.append(ca)
                    if ca.d is None:
                        self.x2.violation(f"{ca.where()}: unparseable date column "
                                          f"({ca.err.get('date')})")
                    elif ca.d != fdate:
                        self.x2.violation(
                            f"{ca.where()}: date column {ca.d} != filename date {fdate}")
                    if ca.sid is not None:
                        if prev_sid is not None and ca.sid < prev_sid:
                            self.x3.violation(
                                f"{ca.where()}: security_id {ca.sid} < previous "
                                f"{prev_sid} (§2 requires ascending order)")
                        prev_sid = ca.sid
                        key = (ca.sid, ca.event_type)
                        if key in seen_key:
                            self.v4.violation(
                                f"{ca.where()}: duplicate (security_id, event_type) "
                                f"{key} (first seen at line {seen_key[key]})")
                        else:
                            seen_key[key] = ln
                        seen_sid_ca[ca.sid].append(ca.event_type)
                for sid, evs in seen_sid_ca.items():
                    if len(evs) > 1:
                        self.v4.note(
                            f"{rel(p)}: security_id {sid} appears {len(evs)}x as "
                            f"{sorted(evs)} — allowed by §6 (div+split same day), which "
                            f"overrides §2's per-file uniqueness rule for corp_action")


    # -- split classification (§7.1) ---------------------------------------
    def sid_to_symbol(self) -> dict[int, str]:
        return {sr.sid: sr.raw.get("ticker_yahoo", "")
                for sr in self.sec_rows if sr.sid is not None}

    def split_ratio_of(self, ca: CorpAction) -> Optional[float]:
        r = ca.split_ratio
        if r is None and ca.split_num is not None and ca.split_den:
            r = ca.split_num / ca.split_den
        return r if (r is not None and r > 0) else None

    def prev_bar(self, sid: int, d: date) -> Optional[Bar]:
        series = self.bars_by_sid.get(sid, {})
        prior = [x for x in series if x < d]
        return series[max(prior)] if prior else None

    def classify_splits(self, vendor: dict[str, VendorSeries]) -> list[SplitClass]:
        """§7.1: decide per event whether the vendor already back-adjusted it.

        jump = quote.close(bar before ex) / quote.close(ex bar)
        applied  <=> |log(jump)| <= |log(jump/ratio)|   (log-space nearest hypothesis)
        Computed purely from the immutable vendor payload, so a producer bug in the
        L2 files cannot influence the classification.
        """
        sid2sym = self.sid_to_symbol()
        out: list[SplitClass] = []
        for ca in self.cas:
            if ca.event_type != "split":
                continue
            ratio = self.split_ratio_of(ca)
            if ratio is None or ca.sid is None or ca.d is None:
                out.append(SplitClass(ca=ca, ratio=ratio or float("nan"),
                                      reason="unusable split_ratio/security_id/date"))
                continue
            sc = SplitClass(ca=ca, ratio=ratio,
                            indistinct=pct(1.0, ratio) <= V2_TOL)
            sym = sid2sym.get(ca.sid, "")
            vs = vendor.get(sym) if sym else None
            if vs is None:
                sc.reason = (f"no vendor payload for ticker_yahoo={sym!r} under "
                             f"{rel(self.raw)}")
                out.append(sc)
                continue
            qc = vs.close_by_date.get(ca.d)
            # prefer the same t-1 the L2 series uses; fall back to the vendor's own
            pb = self.prev_bar(ca.sid, ca.d)
            vprev: Optional[date] = None
            if pb is not None and pb.d in vs.close_by_date:
                vprev = pb.d
            else:
                prior = [x for x in vs.close_by_date if x < ca.d]
                vprev = max(prior) if prior else None
            if qc is None or qc <= 0 or vprev is None:
                sc.reason = f"vendor quote.close missing at the ex-date or before it"
                out.append(sc)
                continue
            qp = vs.close_by_date[vprev]
            if qp <= 0:
                sc.reason = "vendor quote.close at t-1 is non-positive"
                out.append(sc)
                continue
            sc.vprev = vprev
            sc.jump = qp / qc
            sc.applied = abs(math.log(sc.jump)) <= abs(math.log(sc.jump / ratio))
            out.append(sc)
        return out

    # -- V1 (§8 V1 as rewritten: returns, not levels) -----------------------
    def check_v1(self, splits: list[SplitClass]) -> None:
        c = self.v1
        if not self.bars:
            c.violation("no daily_bar rows available — V1 cannot be evaluated")
            return
        sid2sym = self.sid_to_symbol()

        # §7.3/§8: a security whose vendor series still contains an unapplied split
        # disagrees with us on exactly that one session — allowed BY NAME, and only
        # for that (security, ex-date) pair, never by relaxing the threshold.
        exempt: dict[tuple[int, date], str] = {}
        classified = any(sc.applied is not None for sc in splits)
        for sc in splits:
            if sc.applied is False and sc.ca.sid is not None and sc.ca.d is not None:
                exempt[(sc.ca.sid, sc.ca.d)] = (
                    f"vendor never back-adjusted the {sc.ca.raw.get('split_num','')}:"
                    f"{sc.ca.raw.get('split_den','')} split (§7.1, jump={sc.jump:.4f} "
                    f"vs ratio={sc.ratio:.4f}) — our series is right, the vendor's is not")
        if not classified:
            # no vendor evidence available: fall back to the producer's declaration,
            # loudly, because such an exemption cannot be corroborated here.
            for sym, d in self.declared_not_applied():
                for sid, s in sid2sym.items():
                    if s == sym:
                        exempt[(sid, d)] = ("declared in _meta.json "
                                            "splits_vendor_had_not_applied — NOT "
                                            "corroborated (no vendor payload available)")
                        c.warn(f"{sym} {d}: V1 exemption taken from _meta.json without "
                               f"vendor corroboration")

        worst_overall = 0.0
        worst_where = ""
        n_sec = 0
        failing: dict[int, tuple[float, str]] = {}
        for sid, series in sorted(self.bars_by_sid.items()):
            days = sorted(series)
            if len(days) < 2:
                continue
            n_sec += 1
            sym = sid2sym.get(sid, "")
            for i in range(1, len(days)):
                prev, cur = series[days[i - 1]], series[days[i]]
                c.n_checked += 1
                bad = [f"{col}: {e}" for col, e in
                       [(col, b.err.get(col)) for b in (prev, cur)
                        for col in ("close", "adj_factor", "adj_close_vendor")]
                       if e]
                if bad:
                    c.violation(f"{cur.where()}: cannot form the return pair with "
                                f"{days[i-1]} — " + "; ".join(sorted(set(bad))))
                    continue
                if (prev.c is None or cur.c is None or prev.af is None
                        or cur.af is None or prev.acv is None or cur.acv is None):
                    c.violation(f"{cur.where()}: missing close/adj_factor/"
                                f"adj_close_vendor in the {days[i-1]} -> {days[i]} pair")
                    continue
                den_ours = prev.c * prev.af
                if den_ours == 0 or prev.acv == 0:
                    c.violation(f"{cur.where()}: zero denominator at t-1={days[i-1]} "
                                f"(close*adj_factor={den_ours!r}, "
                                f"adj_close_vendor={prev.acv!r})")
                    continue
                r_ours = (cur.c * cur.af) / den_ours - 1.0
                r_vendor = cur.acv / prev.acv - 1.0
                gap = abs(r_ours - r_vendor)
                key = (sid, days[i])
                if key in exempt:
                    c.note(f"EXEMPT {sym} (security_id {sid}) {days[i]}: "
                           f"|r_ours - r_vendor| = {gap:.6f} — {exempt[key]}")
                    continue
                if gap > worst_overall:
                    worst_overall, worst_where = gap, f"{sym} {days[i]}"
                if not (gap < V1_TOL):
                    if sid not in failing or gap > failing[sid][0]:
                        failing[sid] = (gap, f"{cur.where()}")
                    c.violation(
                        f"{cur.where()}: adjusted return mismatch vs vendor over "
                        f"{days[i-1]} -> {days[i]}: r_ours={r_ours:+.8f} "
                        f"(close {prev.c!r}*{prev.af!r} -> {cur.c!r}*{cur.af!r}) vs "
                        f"r_vendor={r_vendor:+.8f} (adj_close_vendor {prev.acv!r} -> "
                        f"{cur.acv!r}); |gap|={gap:.8f} >= {V1_TOL:g}")
        c.note(f"{n_sec} security(ies) with >=2 sessions compared in return space "
               f"(§7.3: factors are only defined up to a constant, so levels are not "
               f"compared)")
        if failing:
            c.note(f"{len(failing)} security(ies) exceed the 1bp return tolerance; "
                   f"worst per security: " + ", ".join(
                       f"sid={s}/{sid2sym.get(s,'')} gap={g:.6f}"
                       for s, (g, _) in sorted(failing.items(),
                                               key=lambda kv: -kv[1][0])[:10]))
        else:
            c.note(f"max |r_ours - r_vendor| over all non-exempt pairs = "
                   f"{worst_overall:.3e} ({worst_where or 'n/a'})")

    def declared_not_applied(self) -> list[tuple[str, date]]:
        """(symbol, ex_date) pairs the producer declared in _meta.json (§7.1)."""
        out: list[tuple[str, date]] = []
        if not isinstance(self.meta, dict):
            return out
        for item in self.meta.get("splits_vendor_had_not_applied") or []:
            if not isinstance(item, dict):
                continue
            sym = str(item.get("symbol", ""))
            d, _ = parse_date(str(item.get("ex_date", "")))
            if sym and d:
                out.append((sym, d))
        return out

    # -- V2 (§8 V2 as rewritten: conditional on §7.1 classification) --------
    def check_v2(self, splits: list[SplitClass]) -> None:
        c = self.v2
        if not splits:
            if (len(self.cal_dates) >= V2_VACUOUS_MIN_SESSIONS
                    and len(self.sec_rows) >= V2_VACUOUS_MIN_SECURITIES):
                c.violation(
                    f"zero split events found in corp_action across "
                    f"{len(self.cal_dates)} sessions x {len(self.sec_rows)} securities "
                    f"— a US equity universe this size always has splits; the producer "
                    f"has almost certainly dropped split events (V2 refuses to pass "
                    f"vacuously)")
            else:
                c.note("no split events in corp_action; dataset is too small for the "
                       "'a real universe always has splits' guard to apply")
            return

        sid2sym = self.sid_to_symbol()
        n_applied = n_not = 0
        for sc in splits:
            ca = sc.ca
            c.n_checked += 1
            ratio = sc.ratio
            if ca.sid is None or ca.d is None or not math.isfinite(ratio):
                c.violation(f"{ca.where()}: unusable split row ({sc.reason})")
                continue
            sym = sid2sym.get(ca.sid, "")
            cur = self.bars_by_sid.get(ca.sid, {}).get(ca.d)
            if cur is None:
                c.violation(f"{ca.where()}: no daily_bar row on the split ex-date "
                            f"for security_id {ca.sid}")
                continue
            prev = self.prev_bar(ca.sid, ca.d)
            if prev is None:
                c.note(f"{ca.where()}: split falls on this security's first session in "
                       f"the dataset — no t-1 bar, jump check skipped")
                continue
            if self.cal_dates:
                idx = {d: i for i, d in enumerate(self.cal_dates)}
                if ca.d in idx and prev.d in idx and idx[ca.d] - idx[prev.d] != 1:
                    c.note(f"{ca.where()}: previous available bar is {prev.d}, "
                           f"{idx[ca.d]-idx[prev.d]} sessions before the ex-date "
                           f"(security did not trade on the adjacent session)")
            if prev.c is None or cur.c is None or cur.c <= 0:
                c.violation(f"{ca.where()}: close missing/non-positive at "
                            f"t-1={prev.d} or t={ca.d} — cannot verify the split jump")
                continue
            obs = prev.c / cur.c

            if sc.applied is None:
                c.violation(
                    f"{ca.where()}: cannot classify this split as vendor-applied or "
                    f"not (§7.1) — {sc.reason}; V2 requires the vendor quote.close "
                    f"series to prove the inversion direction")
                continue

            if sc.applied:
                n_applied += 1
                # (a) our raw series must carry the step
                if pct(obs, ratio) > V2_TOL:
                    hint = ""
                    if obs and pct(1.0 / obs, ratio) <= V2_TOL:
                        hint = ("  <-- observed ratio is the RECIPROCAL of split_ratio: "
                                "the §7 raw-price inversion runs in the wrong direction")
                    elif pct(obs, 1.0) <= V2_TOL:
                        hint = ("  <-- observed ratio ~1: close still equals the "
                                "vendor's split-adjusted price, i.e. the §7 inversion "
                                "was never applied")
                    c.violation(
                        f"{ca.where()} [vendor-APPLIED, jump={sc.jump:.4f}]: "
                        f"raw_close({prev.d})={prev.c!r} / raw_close({ca.d})={cur.c!r} "
                        f"= {obs:.6g}, expected split_ratio={ratio:.6g} +/-{V2_TOL:.0%} "
                        f"(rel dev {pct(obs, ratio):.1%}){hint}")
                # (b) the vendor series must NOT carry it
                if sc.indistinct:
                    c.warn(f"{ca.where()}: split_ratio={ratio:.6g} is within "
                           f"+/-{V2_TOL:.0%} of 1.0 (§7.2 spin-off encoded as a split), "
                           f"so 'jump' and 'no jump' are indistinguishable — the vendor "
                           f"no-jump half is skipped and §7.1 defaults to 'applied'")
                else:
                    if sc.jump is not None and pct(sc.jump, ratio) <= V2_TOL:
                        c.violation(
                            f"{ca.where()}: classified vendor-APPLIED yet quote.close "
                            f"still shows the jump ({sc.jump:.6g} ~ {ratio:.6g})")
                    if prev.acv not in (None, 0) and cur.acv is not None:
                        aobs = prev.acv / cur.acv
                        if pct(aobs, ratio) <= V2_TOL:
                            c.violation(
                                f"{ca.where()}: adj_close_vendor shows the split jump "
                                f"({prev.acv!r}/{cur.acv!r}={aobs:.6g} ~ {ratio:.6g}) — "
                                f"a vendor series that applied the split must be "
                                f"continuous across it")
            else:
                n_not += 1
                c.note(f"{ca.where()}: vendor had NOT applied this split "
                       f"(§7.1 jump={sc.jump:.4f} vs ratio={ratio:.4f}); asserting the "
                       f"opposite: raw_close == quote.close and both keep the step")
                # (a) raw_close must EQUAL quote.close (S(t) must exclude this split)
                qc = self._vendor_close(sym, ca.d)
                qp = self._vendor_close(sym, prev.d)
                for label, ours, theirs, when in (("t", cur.c, qc, ca.d),
                                                  ("t-1", prev.c, qp, prev.d)):
                    if theirs is None:
                        c.violation(f"{ca.where()}: vendor quote.close missing at "
                                    f"{when} — cannot check raw==quote")
                    elif pct(ours, theirs) > V2_EQ_TOL:
                        extra = ""
                        if pct(ours, theirs * ratio) <= V2_EQ_TOL:
                            extra = ("  <-- raw_close = quote.close x split_ratio: the "
                                     "§7 inversion was applied to a split the vendor "
                                     "never applied, doubling every pre-split price")
                        c.violation(
                            f"{ca.where()}: at {label}={when} raw_close={ours!r} != "
                            f"vendor quote.close={theirs!r} (rel dev "
                            f"{pct(ours, theirs):.3%}); §7.1 says S(t) must exclude a "
                            f"split the vendor never applied{extra}")
                # (b) BOTH series must retain the step
                if pct(obs, ratio) > V2_TOL:
                    c.violation(
                        f"{ca.where()} [vendor-NOT-applied]: raw_close({prev.d})="
                        f"{prev.c!r} / raw_close({ca.d})={cur.c!r} = {obs:.6g} does not "
                        f"retain the {ratio:.6g} step (+/-{V2_TOL:.0%})")
                if sc.jump is not None and pct(sc.jump, ratio) > V2_TOL:
                    c.violation(
                        f"{ca.where()} [vendor-NOT-applied]: vendor quote.close ratio "
                        f"{sc.jump:.6g} does not retain the {ratio:.6g} step")
                if prev.acv not in (None, 0) and cur.acv is not None:
                    aobs = prev.acv / cur.acv
                    if pct(aobs, ratio) > V2_TOL:
                        c.note(f"{ca.where()}: adj_close_vendor ratio {aobs:.6g} does "
                               f"not retain the step although quote.close does")
        c.note(f"{n_applied} split event(s) classified vendor-applied, {n_not} "
               f"not-applied (§7.1)")
        if n_not:
            c.note("not-applied events: " + ", ".join(
                f"{sid2sym.get(sc.ca.sid,'')} {sc.ca.d} ratio={sc.ratio:g}"
                for sc in splits if sc.applied is False))

    def _vendor_close(self, sym: str, d: date) -> Optional[float]:
        vs = self._vendor.get(sym) if sym else None
        return vs.close_by_date.get(d) if vs else None

    # -- X10: recompute adj_factor from the event log (§7.3) ---------------
    def check_x10(self, splits: list[SplitClass]) -> None:
        c = self.x10
        if not self.bars:
            c.violation("no daily_bar rows — §7.3 recomputation impossible")
            return
        applied_ratio: dict[int, list[tuple[date, float]]] = defaultdict(list)
        all_ratio: dict[int, list[tuple[date, float]]] = defaultdict(list)
        unclassified = 0
        for sc in splits:
            if sc.ca.sid is None or sc.ca.d is None or not math.isfinite(sc.ratio):
                continue
            all_ratio[sc.ca.sid].append((sc.ca.d, sc.ratio))
            if sc.applied is None:
                unclassified += 1
                applied_ratio[sc.ca.sid].append((sc.ca.d, sc.ratio))   # assume applied
            elif sc.applied:
                applied_ratio[sc.ca.sid].append((sc.ca.d, sc.ratio))
        if unclassified:
            c.warn(f"{unclassified} split event(s) could not be classified per §7.1; "
                   f"assumed vendor-applied when un-restating dividends")

        divs: dict[int, list[tuple[date, float]]] = defaultdict(list)
        for ca in self.cas:
            if ca.event_type == "div" and ca.sid is not None and ca.d is not None \
                    and ca.div_amount is not None:
                divs[ca.sid].append((ca.d, ca.div_amount))

        sid2sym = self.sid_to_symbol()
        skipped: list[str] = []
        for sid, series in sorted(self.bars_by_sid.items()):
            days = sorted(series)
            if len(days) < 2:
                continue
            sym = sid2sym.get(sid, "")

            def S(t: date) -> float:                       # §7 (vendor-applied only)
                p = 1.0
                for ex, r in applied_ratio.get(sid, ()):
                    if ex > t:
                        p *= r
                return p

            def SF(t: date) -> float:                      # §7.3 (all splits)
                p = 1.0
                for ex, r in all_ratio.get(sid, ()):
                    if ex > t:
                        p *= r
                return p

            # dividend terms: D_raw = D_reported * S(ex_date); denominator is the raw
            # close of the session preceding the ex-date
            terms: list[tuple[date, float]] = []
            for ex, amt in divs.get(sid, ()):
                pb = self.prev_bar(sid, ex)
                if pb is None or pb.c is None or pb.c <= 0:
                    skipped.append(f"{sym} div {ex}: no usable prior close, term dropped")
                    continue
                d_raw = amt * S(ex)
                terms.append((ex, 1.0 - d_raw / pb.c))

            def DF(t: date) -> float:
                p = 1.0
                for ex, f in terms:
                    if ex > t:
                        p *= f
                return p

            for i in range(1, len(days)):
                prev, cur = series[days[i - 1]], series[days[i]]
                if prev.af is None or cur.af is None or prev.af == 0:
                    continue
                c.n_checked += 1
                want_prev = DF(days[i - 1]) / SF(days[i - 1])
                want_cur = DF(days[i]) / SF(days[i])
                if want_prev == 0:
                    continue
                want = want_cur / want_prev
                got = cur.af / prev.af
                if pct(got, want) > X10_TOL:
                    c.violation(
                        f"{cur.where()}: adj_factor ratio {prev.af!r} -> {cur.af!r} = "
                        f"{got:.12g}, but the §7.3 event log implies {want:.12g} "
                        f"(rel dev {pct(got, want):.3e}) over {days[i-1]} -> {days[i]}")
        if skipped:
            for s in skipped[:5]:
                c.warn(s)
            if len(skipped) > 5:
                c.warn(f"... {len(skipped)-5} more dividend term(s) dropped for lack of "
                       f"a prior close (ex-date at the start of the range)")

    # -- X11: declared vs observed not-applied splits (§7.1) ---------------
    def check_x11(self, splits: list[SplitClass]) -> None:
        c = self.x11
        sid2sym = self.sid_to_symbol()
        observed = {(sid2sym.get(sc.ca.sid, ""), sc.ca.d)
                    for sc in splits if sc.applied is False}
        classified = any(sc.applied is not None for sc in splits)
        declared = set(self.declared_not_applied())
        c.n_checked = len(splits)
        if self.meta is None:
            if observed:
                c.violation(
                    f"{len(observed)} split(s) were observed to be un-back-adjusted by "
                    f"the vendor ({sorted((s, str(d)) for s, d in observed)}) but "
                    f"_meta.json is unavailable ({self.meta_err}); §7.1 requires them "
                    f"to be recorded there")
            else:
                c.warn(f"_meta.json unavailable ({self.meta_err}); nothing to reconcile")
            return
        if not classified:
            c.warn("no split could be classified from vendor payloads; the declaration "
                   "in _meta.json cannot be corroborated")
            return
        for sym, d in sorted(declared - observed):
            c.violation(
                f"_meta.json declares {sym} {d} in splits_vendor_had_not_applied, but "
                f"the vendor payload shows the split IS already applied (or the event "
                f"is unclassifiable) — an unearned V1 exemption")
        for sym, d in sorted(observed - declared):
            c.violation(
                f"vendor payload shows {sym} {d} was NOT back-adjusted by the vendor, "
                f"but _meta.json does not declare it (§7.1 requires it be recorded in "
                f"splits_vendor_had_not_applied)")
        if not (declared - observed) and not (observed - declared):
            c.note(f"declared == observed: "
                   f"{sorted((s, str(d)) for s, d in observed) or 'empty set'}")

    # -- V3 ---------------------------------------------------------------
    def check_v3(self) -> None:
        c = self.v3
        if not self.cal_rows or not self.bar_files:
            if not self.cal_rows:
                c.violation("calendar.csv produced no usable sessions — V3 cannot pass")
            if not self.bar_files:
                c.violation("no daily_bar files found — V3 cannot pass")
            return
        cal = set(self.cal_dates)
        have = set(self.bar_files)
        c.n_checked = len(cal | have)
        for d in sorted(cal - have):
            c.violation(f"calendar session {d} has no "
                        f"pv/{d:%Y}/{d:%m}/pv.{d:%Y%m%d} (gap in the date axis)")
        for d in sorted(have - cal):
            c.violation(f"{rel(self.bar_files[d])}: pv file for {d}, which is not a "
                        f"calendar session (orphan file)")

    # -- V4 ---------------------------------------------------------------
    def check_v4(self) -> None:
        c = self.v4
        # in-file duplicates were collected during load(); here: id resolution
        known = {sr.sid for sr in self.sec_rows if sr.sid is not None}
        if not known:
            c.violation("sec_master yielded no usable security_id values — no id in "
                        "pv/cax can be resolved")
            return
        unresolved: Counter[str] = Counter()
        for b in self.bars:
            c.n_checked += 1
            if b.sid is None:
                c.violation(f"{b.where()}: unusable security_id "
                            f"({b.err.get('security_id')})")
            elif b.sid not in known:
                key = f"pv sid={b.sid}"
                unresolved[key] += 1
                if unresolved[key] == 1:
                    c.violation(f"{b.where()}: security_id {b.sid} does not resolve in "
                                f"any sec_master file")
            elif b.d is not None and b.d in self.sm_slices \
                    and b.sid not in self.sm_slices[b.d].ticker:
                key = f"pv sid={b.sid} not PIT-present"
                unresolved[key] += 1
                if unresolved[key] == 1:
                    c.violation(
                        f"{b.where()}: security_id {b.sid} is absent from the "
                        f"sec_master for that very session "
                        f"({rel(self.sm_slices[b.d].path)}) — a pv row for a security "
                        f"the PIT master says did not exist yet")
        for ca in self.cas:
            c.n_checked += 1
            if ca.sid is None:
                c.violation(f"{ca.where()}: unusable security_id "
                            f"({ca.err.get('security_id')})")
            elif ca.sid not in known:
                key = f"corp_action sid={ca.sid}"
                unresolved[key] += 1
                if unresolved[key] == 1:
                    c.violation(f"{ca.where()}: security_id {ca.sid} does not resolve "
                                f"in any sec_master file")
        multi = {k: n for k, n in unresolved.items() if n > 1}
        if multi:
            c.note(f"unresolved ids repeat across files: "
                   f"{dict(sorted(multi.items())[:10])}")

    # -- V5 ---------------------------------------------------------------
    def check_v5(self) -> None:
        c = self.v5
        if not self.bars:
            c.violation("no daily_bar rows available — V5 cannot be evaluated")
            return
        for b in self.bars:
            c.n_checked += 1
            bad = [f"{col}: {b.err[col]}" for col in ("open", "high", "low", "close",
                                                      "volume") if col in b.err]
            if bad:
                c.violation(f"{b.where()}: unusable OHLCV field(s) — " + "; ".join(bad))
                continue
            o, h, lo, cl, v = b.o, b.h, b.lo, b.c, b.vol
            assert o is not None and h is not None and lo is not None
            assert cl is not None and v is not None
            probs: list[str] = []
            for name, val in (("open", o), ("high", h), ("low", lo), ("close", cl)):
                if not val > 0:
                    probs.append(f"{name}={val!r} is not > 0")
            if b.acv is not None and not b.acv > 0:
                probs.append(f"adj_close_vendor={b.acv!r} is not > 0")
            if b.af is not None and not b.af > 0:
                probs.append(f"adj_factor={b.af!r} is not > 0")
            if v < 0:
                probs.append(f"volume={v!r} is negative")
            if lo > min(o, cl):
                probs.append(f"low={lo!r} > min(open,close)={min(o, cl)!r}")
            if h < max(o, cl):
                probs.append(f"high={h!r} < max(open,close)={max(o, cl)!r}")
            if h < lo:
                probs.append(f"high={h!r} < low={lo!r}")
            if probs:
                c.violation(f"{b.where()}: " + "; ".join(probs)
                            + f"  [o={o!r} h={h!r} l={lo!r} c={cl!r} v={v!r}]")
            elif v == 0:
                self.w3.warn(f"{b.where()}: volume is 0 (halted/stale?) "
                             f"[o={o!r} h={h!r} l={lo!r} c={cl!r}]")

    # -- V6 ---------------------------------------------------------------
    def check_v6(self) -> None:
        c = self.v6
        if not self.sec_rows:
            c.violation("sec_master.csv missing or empty — V6 cannot be evaluated")
            return
        c.n_checked = len(self.sec_rows)
        seen: dict[int, int] = {}
        ids: list[int] = []
        for sr in self.sec_rows:
            if sr.sid is None:
                c.violation(f"{sr.where()}: unusable security_id "
                            f"({sr.err.get('security_id')})")
                continue
            if sr.sid in seen:
                c.violation(f"{sr.where()}: duplicate security_id {sr.sid} "
                            f"(first seen at line {seen[sr.sid]})")
            else:
                seen[sr.sid] = sr.lineno
                ids.append(sr.sid)
        if ids:
            ids_sorted = sorted(ids)
            if ids_sorted[0] != 1:
                c.violation(f"security_id must start at 1 (§3); the minimum present is "
                            f"{ids_sorted[0]}")
            expected = set(range(ids_sorted[0], ids_sorted[-1] + 1))
            holes = sorted(expected - set(ids_sorted))
            if holes:
                c.violation(
                    f"security_id is not contiguous: {len(holes)} missing id(s) between "
                    f"{ids_sorted[0]} and {ids_sorted[-1]}, e.g. {holes[:20]}")
        # bijection with ticker_yahoo
        sid_by_tick: dict[str, int] = {}
        for sr in self.sec_rows:
            tick = sr.raw.get("ticker_yahoo", "")
            if tick == "":
                c.violation(f"{sr.where()}: ticker_yahoo is empty — "
                            f"(security_id, ticker_yahoo) cannot be a bijection")
                continue
            if tick in sid_by_tick and sid_by_tick[tick] != sr.sid:
                c.violation(f"{sr.where()}: ticker_yahoo {tick!r} maps to security_id "
                            f"{sid_by_tick[tick]} and {sr.sid} — not injective")
            else:
                sid_by_tick[tick] = sr.sid if sr.sid is not None else -1
            if sr.raw.get("ticker", "") not in ("", tick):
                self.w3.warn(f"{sr.where()}: ticker={sr.raw.get('ticker')!r} != "
                             f"ticker_yahoo={tick!r} (§3 says ticker = ticker_yahoo)")
        # ordering discipline (advisory: appended delisted names may break it, §3)
        keyed: list[tuple[str, str, int]] = []
        for sr in self.sec_rows:
            ftd = sr.raw.get("first_trade_date", "")
            if sr.sid is not None and ftd:
                keyed.append((ftd, sr.raw.get("ticker_yahoo", ""), sr.sid))
        if keyed:
            want = [sid for _, _, sid in sorted(keyed)]
            got = [sid for _, _, sid in sorted(keyed, key=lambda t: t[2])]
            if want != got:
                n_bad = sum(1 for a, b in zip(want, got) if a != b)
                self.w3.warn(
                    f"sec_master: security_id order does not follow "
                    f"(first_trade_date asc, ticker_yahoo asc) for {n_bad} row(s) — "
                    f"§3's assignment rule; legitimate only for ids appended later")

    # -- V7 ---------------------------------------------------------------
    def check_v7(self) -> None:
        c = self.v7
        total = len(self.cal_dates)
        if total == 0:
            c.violation("no calendar sessions — coverage cannot be computed")
            return
        if not self.sec_rows:
            c.violation("sec_master.csv missing or empty — coverage cannot be computed "
                        "per security")
            return
        counts: dict[int, int] = {}
        for sr in self.sec_rows:
            if sr.sid is None:
                continue
            counts[sr.sid] = len(self.bars_by_sid.get(sr.sid, {}))
        if not counts:
            c.violation("no resolvable securities — coverage cannot be computed")
            return
        c.n_checked = len(counts)
        vals = sorted(counts.values())
        qs = [("min", 0.0), ("p01", 0.01), ("p05", 0.05), ("p25", 0.25),
              ("p50", 0.50), ("p75", 0.75), ("p95", 0.95), ("max", 1.0)]
        dist = "  ".join(f"{name}={quantile(vals, q):.0f}" for name, q in qs)
        c.note(f"total sessions in calendar: {total}; securities in sec_master: "
               f"{len(counts)}")
        c.note(f"session-count distribution: {dist}  mean="
               f"{sum(vals)/len(vals):.1f}")
        buckets = [(0, 0), (1, int(0.25 * total)), (int(0.25 * total) + 1, int(0.5 * total)),
                   (int(0.5 * total) + 1, int(0.9 * total)),
                   (int(0.9 * total) + 1, total - 1), (total, total)]
        hist = []
        for lo_b, hi_b in buckets:
            if hi_b < lo_b:
                continue
            n = sum(1 for v in vals if lo_b <= v <= hi_b)
            hist.append(f"[{lo_b}..{hi_b}]={n}")
        c.note("histogram: " + "  ".join(hist))
        thresh = V7_COVERAGE_FRAC * total
        low = sorted(((n, sid) for sid, n in counts.items() if n < thresh))
        tick = {sr.sid: sr.raw.get("ticker_yahoo", "") for sr in self.sec_rows}
        if low:
            c.note(f"{len(low)} security(ies) with n_sessions < {V7_COVERAGE_FRAC:g} * "
                   f"{total} = {thresh:g}:")
            for n, sid in low[:200]:
                c.note(f"    security_id={sid} ticker_yahoo={tick.get(sid,'')!r} "
                       f"n_sessions={n} ({n/total:.1%} of the range)")
            if len(low) > 200:
                c.note(f"    ... and {len(low)-200} more")
            c.warn(f"{len(low)} security(ies) below the {V7_COVERAGE_FRAC:g} coverage "
                   f"threshold — listed above (advisory, not a V7 failure)")
        else:
            c.note("every security covers at least "
                   f"{V7_COVERAGE_FRAC:g} of the session range")
        # bars whose security is absent from sec_master are a V4 problem, but a
        # coverage report that silently drops them would be misleading:
        orphan = set(self.bars_by_sid) - set(counts)
        if orphan:
            c.note(f"{len(orphan)} security_id(s) appear in daily_bar but not in "
                   f"sec_master and are excluded from the distribution "
                   f"(see V4): {sorted(orphan)[:20]}")

    # -- X2/X4/X5/X6/X7/X8 -------------------------------------------------
    def check_extras(self) -> None:
        # X4 global (date, security_id) duplicates
        seen: dict[tuple[date, int], Bar] = {}
        for b in self.bars:
            if b.d is None or b.sid is None:
                continue
            self.x4.n_checked += 1
            key = (b.d, b.sid)
            if key in seen:
                first = seen[key]
                self.x4.violation(
                    f"{b.where()}: (date, security_id)={key} already written at "
                    f"{rel(first.path)}:{first.lineno}")
            else:
                seen[key] = b

        # X5 corp_action semantics (§6)
        for ca in self.cas:
            self.x5.n_checked += 1
            probs: list[str] = []
            for col, e in ca.err.items():
                probs.append(f"{col}: {e}")
            et = ca.event_type
            if et not in ("div", "split"):
                probs.append(f"event_type={et!r} is not one of 'div'|'split' (§6)")
            elif et == "split":
                if ca.split_num is None or ca.split_den is None:
                    probs.append("split row without split_num/split_den (§6)")
                elif ca.split_den == 0:
                    probs.append("split_den is 0")
                elif ca.split_ratio is None:
                    probs.append("split row without split_ratio (§6)")
                else:
                    want = ca.split_num / ca.split_den
                    if pct(ca.split_ratio, want) > 1e-6:
                        probs.append(
                            f"split_ratio={ca.split_ratio!r} != split_num/split_den="
                            f"{ca.split_num!r}/{ca.split_den!r}={want:.10g}")
                    if want <= 0:
                        probs.append(f"split_ratio {want:.10g} is not positive")
                if ca.raw.get("div_amount", "") != "":
                    probs.append(f"split row carries div_amount="
                                 f"{ca.raw['div_amount']!r}; §6 requires it be empty")
            else:  # div
                if ca.div_amount is None:
                    probs.append("div row without div_amount (§6)")
                elif not ca.div_amount > 0:
                    probs.append(f"div_amount={ca.div_amount!r} is not > 0")
                for col in ("split_num", "split_den", "split_ratio"):
                    if ca.raw.get(col, "") != "":
                        probs.append(f"div row carries {col}={ca.raw[col]!r}; "
                                     f"§6 requires the split columns be empty")
            if probs:
                self.x5.violation(f"{ca.where()}: " + "; ".join(probs))

        # X6 coverage now lives in _meta.json: first_session/last_session/n_sessions
        # were removed from sec_master because inside a per-date PIT row they are
        # look-ahead ("this security has 250 sessions" is a fact about the future
        # when read on 2025-09-01).  Recompute from pv and reconcile against meta.
        self.check_x6_coverage()

        # X7 ticker agreement — PIT: compare against the sec_master for the row's
        # own session, since ticker is an attribute with a validity interval (§3.4)
        fallback = {sr.sid: sr.raw.get("ticker", "") for sr in self.sec_rows
                    if sr.sid is not None}
        if fallback:
            for rows in (self.bars, self.cas):
                for r in rows:
                    if r.sid is None:
                        continue
                    sl = self.sm_slices.get(r.d) if r.d is not None else None
                    want = sl.ticker.get(r.sid) if sl else fallback.get(r.sid)
                    if want is None:
                        continue
                    self.x7.n_checked += 1
                    if r.ticker != want:
                        where = "for that session" if sl else "(no PIT slice)"
                        self.x7.violation(
                            f"{r.where()}: ticker={r.ticker!r} but the sec_master "
                            f"{where} says {want!r} for security_id {r.sid}")

        # X8 calendar internal consistency
        if self.cal_rows:
            self.x8.n_checked = len(self.cal_rows)
            prev_sess: Optional[int] = None
            prev_date: Optional[date] = None
            seen_dates: dict[date, int] = {}
            for r in self.cal_rows:
                if r.session is None:
                    self.x8.violation(f"{rel(r.path)}:{r.lineno}: session "
                                      f"{r.err.get('session')}")
                else:
                    if prev_sess is None:
                        if r.session != 0:
                            self.x8.violation(
                                f"{rel(r.path)}:{r.lineno}: first session is "
                                f"{r.session}; §4 requires the axis to start at 0")
                    elif r.session != prev_sess + 1:
                        self.x8.violation(
                            f"{rel(r.path)}:{r.lineno}: session {r.session} does not "
                            f"follow {prev_sess} (§4 monotone +1 from 0)")
                    prev_sess = r.session
                if r.d is None:
                    self.x8.violation(f"{rel(r.path)}:{r.lineno}: date "
                                      f"{r.err.get('date')}")
                else:
                    if r.d in seen_dates:
                        self.x8.violation(
                            f"{rel(r.path)}:{r.lineno}: duplicate session date {r.d} "
                            f"(also line {seen_dates[r.d]})")
                    seen_dates[r.d] = r.lineno
                    if prev_date is not None and r.d <= prev_date:
                        self.x8.violation(
                            f"{rel(r.path)}:{r.lineno}: date {r.d} is not after the "
                            f"previous session's {prev_date}")
                    prev_date = r.d
                    if r.d.weekday() >= 5:
                        self.w3.warn(f"{rel(r.path)}:{r.lineno}: session {r.d} falls on "
                                     f"a {r.d:%A} — NYSE does not trade weekends")
                if r.is_half_day not in ("0", "1"):
                    self.x8.violation(
                        f"{rel(r.path)}:{r.lineno}: is_half_day={r.is_half_day!r} is "
                        f"not 0/1")
                if r.d is not None and self.bar_files:
                    obs = self.n_bar_rows_by_date.get(r.d)
                    if r.n_securities is None:
                        self.x8.violation(f"{rel(r.path)}:{r.lineno}: n_securities "
                                          f"{r.err.get('n_securities')}")
                    elif obs is not None and r.n_securities != obs:
                        self.x8.violation(
                            f"{rel(r.path)}:{r.lineno}: n_securities={r.n_securities} "
                            f"but daily_bar/{r.d:%Y%m%d}.csv holds {obs} row(s)")

        # W1 large single-day moves on adj_close_vendor.  A split the vendor never
        # back-adjusted (§7.1) leaves a fully-explained step in its own series on
        # exactly the ex-date; that is documented, not an unhandled corp action.
        explained = {(sc.ca.sid, sc.ca.d) for sc in self._splits
                     if sc.applied is False}
        for sid, series in self.bars_by_sid.items():
            days = sorted(series)
            for i in range(1, len(days)):
                a, b = series[days[i - 1]], series[days[i]]
                if a.acv is None or b.acv is None or a.acv <= 0:
                    continue
                self.w1.n_checked += 1
                ret = b.acv / a.acv - 1.0
                if abs(ret) > W_RETURN_TOL and (sid, days[i]) in explained:
                    self.w1.note(
                        f"{b.where()}: {ret:+.1%} on adj_close_vendor is the step of a "
                        f"split the vendor never applied (§7.1) — explained, not flagged")
                elif abs(ret) > W_RETURN_TOL:
                    self.w1.violation(
                        f"{b.where()}: adj_close_vendor {a.acv!r} ({days[i-1]}) -> "
                        f"{b.acv!r} ({days[i]}) = {ret:+.1%} — verify no corp action "
                        f"was missed (real 50% moves do exist)")

        # W2 numeric formatting (§2)
        fmt_bad: Counter[str] = Counter()
        first_ex: dict[str, str] = {}
        for b in self.bars:
            self.w2.n_checked += 1
            for col in ("open", "high", "low", "close", "adj_close_vendor"):
                s = b.raw.get(col, "")
                if s == "":
                    continue
                d_ = decimals(s)
                if d_ is None:
                    fmt_bad[f"{col}: not a plain decimal literal"] += 1
                    first_ex.setdefault(f"{col}: not a plain decimal literal",
                                        f"{b.where()} {col}={s!r}")
                elif d_ != 6:
                    fmt_bad[f"{col}: {d_} decimal places (§2 says 6)"] += 1
                    first_ex.setdefault(f"{col}: {d_} decimal places (§2 says 6)",
                                        f"{b.where()} {col}={s!r}")
            s = b.raw.get("adj_factor", "")
            if s:
                d_ = decimals(s)
                if d_ is None:
                    fmt_bad["adj_factor: not a plain decimal literal"] += 1
                    first_ex.setdefault("adj_factor: not a plain decimal literal",
                                        f"{b.where()} adj_factor={s!r}")
                elif d_ != 10:
                    fmt_bad[f"adj_factor: {d_} decimal places (§2 says 10)"] += 1
                    first_ex.setdefault(f"adj_factor: {d_} decimal places (§2 says 10)",
                                        f"{b.where()} adj_factor={s!r}")
            s = b.raw.get("volume", "")
            if s and not INT_RE.match(s):
                fmt_bad["volume: not an integer literal"] += 1
                first_ex.setdefault("volume: not an integer literal",
                                    f"{b.where()} volume={s!r}")
        for kind, n in fmt_bad.most_common():
            self.w2.warn(f"{n:,} row(s) — {kind}; e.g. {first_ex[kind]}")

        # W3 architecture.md still shows the pre-move path template
        self.w3.warn(
            "docs/architecture.md §4.5/§5.1 still show `/data/l2/daily_bar/{date}."
            "parquet|csv` as the L2 source path; the current contract (l2_schema.md "
            "§1) delivers `storage/data/base/l2/us/pv.{YYYYMMDD}` and `cax.{YYYYMMDD}` "
            "with no extension — the upstream doc is stale")

        # W3 _meta.json (§9)
        meta_path = self.l2 / "_meta.json"
        if self.meta is None:
            self.w3.warn(self.meta_err or f"{rel(meta_path)} unavailable")
        else:
            meta = self.meta
            if isinstance(meta, dict):
                    if (isinstance(meta.get("n_sessions"), int)
                            and self.cal_dates
                            and meta["n_sessions"] != len(self.cal_dates)):
                        self.w3.warn(
                            f"{rel(meta_path)}: n_sessions={meta['n_sessions']} but "
                            f"calendar.csv has {len(self.cal_dates)} sessions")
                    if (isinstance(meta.get("n_securities"), int) and self.sec_rows
                            and meta["n_securities"] != len(self.sec_rows)):
                        self.w3.warn(
                            f"{rel(meta_path)}: n_securities={meta['n_securities']} but "
                            f"sec_master has {len(self.sec_rows)} rows")
                    rc = meta.get("row_counts")
                    if isinstance(rc, dict):
                        observed = {"daily_bar": len(self.bars), "pv": len(self.bars),
                                    "corp_action": len(self.cas), "cax": len(self.cas),
                                    "sec_master": sum(sl.n_rows for sl in
                                                      self.sm_slices.values()),
                                    "calendar": len(self.cal_rows),
                                    "industry": sum(sl.n_rows for sl in
                                                    self.ind_slices.values())}
                        for key, got in sorted(observed.items()):
                            if isinstance(rc.get(key), int) and rc[key] != got:
                                self.w3.warn(
                                    f"{rel(meta_path)}: row_counts.{key}={rc[key]} but "
                                    f"{got} rows read")
                    for d in ("survivorship_bias_no_delisted", "no_vwap",
                              "adj_factor_not_pit"):
                        if d not in (meta.get("known_defects") or []):
                            self.w3.warn(f"{rel(meta_path)}: known_defects does not list "
                                         f"{d!r} (§0.1 forbids staying silent about it)")

    # -- X9 vendor event reconciliation ------------------------------------
    def check_x9(self, vendor: dict[str, VendorSeries], loaded_all: bool) -> None:
        c = self.x9
        if self.skip_raw:
            c.warn("--skip-raw: vendor reconciliation not run")
            c.hard = False
            c.status = "WARN"
            return
        if not vendor:
            c.violation(f"no usable vendor payloads under {rel(self.raw)} — corp_action "
                        f"cannot be reconciled against the source of truth")
            return
        if not loaded_all:
            c.warn("only the payloads needed by V2 were loaded; reconciliation is "
                   "partial")
        if not self.cal_dates:
            c.violation("no calendar sessions — cannot bound the reconciliation window")
            return
        lo, hi = self.cal_dates[0], self.cal_dates[-1]
        sym_of = {sr.sid: sr.raw.get("ticker_yahoo", "")
                  for sr in self.sec_rows if sr.sid is not None}
        l2_split: dict[tuple[int, date], CorpAction] = {}
        l2_div: dict[tuple[int, date], CorpAction] = {}
        for ca in self.cas:
            if ca.sid is None or ca.d is None:
                continue
            (l2_split if ca.event_type == "split" else l2_div)[(ca.sid, ca.d)] = ca
        for sid, sym in sorted(sym_of.items()):
            vs = vendor.get(sym)
            if vs is None:
                continue
            for d, num, den in vs.splits:
                if not (lo <= d <= hi):
                    continue
                if self.cal_date_set and d not in self.cal_date_set:
                    c.warn(f"{sym}: vendor split on {d} is not a calendar session; "
                           f"excluded from reconciliation")
                    continue
                c.n_checked += 1
                ca = l2_split.get((sid, d))
                if ca is None:
                    c.violation(
                        f"vendor {sym}.json has a {num:g}:{den:g} split on {d} "
                        f"(security_id {sid}) with no matching corp_action row — "
                        f"a dropped split silently breaks the §7 raw-price inversion")
                elif (ca.split_num, ca.split_den) != (num, den):
                    c.violation(
                        f"{ca.where()}: split_num/split_den = "
                        f"{ca.split_num!r}/{ca.split_den!r} but vendor {sym}.json says "
                        f"{num:g}/{den:g}")
            for d, amt in vs.dividends:
                if not (lo <= d <= hi):
                    continue
                if self.cal_date_set and d not in self.cal_date_set:
                    c.warn(f"{sym}: vendor dividend on {d} is not a calendar session; "
                           f"excluded from reconciliation")
                    continue
                c.n_checked += 1
                ca = l2_div.get((sid, d))
                if ca is None:
                    c.violation(
                        f"vendor {sym}.json has a dividend of {amt:g} on {d} "
                        f"(security_id {sid}) with no matching corp_action row")
                elif ca.div_amount is None or pct(ca.div_amount, amt) > 1e-4:
                    c.violation(
                        f"{ca.where()}: div_amount={ca.div_amount!r} but vendor "
                        f"{sym}.json says {amt:g}")
        # the other direction: L2 events the vendor does not have
        v_split = {(sid, d) for sid, sym in sym_of.items()
                   if sym in vendor for d, _, _ in vendor[sym].splits}
        v_div = {(sid, d) for sid, sym in sym_of.items()
                 if sym in vendor for d, _ in vendor[sym].dividends}
        for (sid, d), ca in sorted(l2_split.items()):
            if sym_of.get(sid, "") in vendor and (sid, d) not in v_split:
                c.violation(f"{ca.where()}: split is not present in the vendor payload "
                            f"{sym_of.get(sid)}.json (fabricated or mis-dated event)")
        for (sid, d), ca in sorted(l2_div.items()):
            if sym_of.get(sid, "") in vendor and (sid, d) not in v_div:
                c.violation(f"{ca.where()}: dividend is not present in the vendor "
                            f"payload {sym_of.get(sid)}.json (fabricated or mis-dated)")

    # -- X6: coverage recomputed from pv, reconciled against _meta.json -------
    def check_x6_coverage(self) -> None:
        c = self.x6
        total = len(self.cal_dates)
        if total == 0 or not self.bars_by_sid:
            c.violation("no calendar sessions or no pv rows — coverage cannot be "
                        "recomputed")
            return
        sess_of = {d: i for i, d in enumerate(self.cal_dates)}
        tick = {sid: info.get("ticker_yahoo") or info.get("ticker", "")
                for sid, info in self.sec_union.items()}

        full: set[int] = set()
        partial: dict[str, dict[str, int]] = {}
        for sid, series in self.bars_by_sid.items():
            days = sorted(series)
            c.n_checked += 1
            if len(days) == total:
                full.add(sid)
                continue
            name = tick.get(sid, "")
            if not name:
                c.violation(f"security_id {sid} has pv rows but no ticker in any "
                            f"sec_master — cannot key it into coverage_partial")
                continue
            partial[name] = {"security_id": sid, "n_sessions": len(days),
                             "first_session": sess_of.get(days[0], -1),
                             "last_session": sess_of.get(days[-1], -1)}

        if not isinstance(self.meta, dict):
            c.violation(f"coverage now lives in _meta.json (coverage_full / "
                        f"coverage_partial) but it is unavailable ({self.meta_err})")
            return
        want_full = self.meta.get("coverage_full")
        want_part = self.meta.get("coverage_partial")
        if not isinstance(want_full, int):
            c.violation("_meta.json has no integer coverage_full")
        elif want_full != len(full):
            c.violation(
                f"_meta.json coverage_full={want_full} but {len(full)} security(ies) "
                f"actually cover all {total} sessions")
        if not isinstance(want_part, dict):
            c.violation("_meta.json has no coverage_partial map")
            return
        for name in sorted(set(want_part) - set(partial)):
            c.violation(f"_meta.json coverage_partial lists {name!r}, which actually "
                        f"covers every one of the {total} sessions")
        for name in sorted(set(partial) - set(want_part)):
            got = partial[name]
            c.violation(
                f"{name} (security_id {got['security_id']}) covers "
                f"{got['n_sessions']}/{total} sessions but is absent from "
                f"_meta.json coverage_partial — partial coverage must be declared")
        for name in sorted(set(partial) & set(want_part)):
            got, want = partial[name], want_part[name]
            if not isinstance(want, dict):
                c.violation(f"coverage_partial[{name!r}] is not an object")
                continue
            for k in ("security_id", "n_sessions", "first_session", "last_session"):
                if want.get(k) != got[k]:
                    c.violation(
                        f"coverage_partial[{name!r}].{k} = {want.get(k)!r} but "
                        f"recomputed {got[k]} from pv")
        # 内层表达式提出来: f-string 里复用外层引号是 PEP 701, 3.12 才有, 而本项目
        # 承诺 3.11+（pyproject.requires-python 与 manual §2.1）
        detail = ", ".join(f"{k}={v['n_sessions']}" for k, v in sorted(partial.items()))
        c.note(f"recomputed from pv: {len(full)} security(ies) with full "
               f"{total}-session coverage, {len(partial)} partial ({detail})")

    # -- X12: industry table (§3.2), now PIT ---------------------------------
    def check_x12(self) -> None:
        c = self.x12
        if not self.ind_slices:
            return                      # absence already reported by discover()
        c.n_checked = sum(sl.n_rows for sl in self.ind_slices.values())

        code_to_name: dict[int, tuple[str, str]] = {}
        name_to_code: dict[str, tuple[int, str]] = {}
        n_empty = 0
        for d in sorted(self.ind_slices):
            sl = self.ind_slices[d]
            sm = self.sm_slices.get(d)
            # referential integrity is per session: the two PIT views of the same
            # day must describe exactly the same population
            if sm is None:
                c.violation(f"{rel(sl.path)}: no sec_master file for {d} to reconcile "
                            f"the industry population against")
            else:
                only_ind = sorted(set(sl.ids) - set(sm.ids))
                only_sm = sorted(set(sm.ids) - set(sl.ids))
                for sid in only_ind[:3]:
                    c.violation(f"{rel(sl.path)}: security_id {sid} "
                                f"({sl.ticker.get(sid,'')!r}) is not in the "
                                f"sec_master for {d}")
                for sid in only_sm[:3]:
                    c.violation(
                        f"{rel(sl.path)}: sec_master security_id {sid} "
                        f"({sm.ticker.get(sid,'')!r}) has no industry row for {d} — "
                        f"`neutralize: sector` would silently drop it")
                if len(only_ind) > 3 or len(only_sm) > 3:
                    c.n_violations += max(0, len(only_ind) - 3) + max(0, len(only_sm) - 3)
                for sid in sl.ids:
                    want = sm.ticker.get(sid)
                    if want is not None and sl.ticker.get(sid) != want:
                        c.violation(f"{rel(sl.path)}: security_id {sid} ticker "
                                    f"{sl.ticker.get(sid)!r} but sec_master for {d} "
                                    f"says {want!r}")
                        break
            for sid in sl.ids:
                where = (f"{rel(sl.path)} security_id={sid} "
                         f"ticker={sl.ticker.get(sid,'')!r}")
                raw_code, name, sub = sl.yahoo.get(sid, ""), sl.ftd.get(sid, ""), \
                    sl.cik.get(sid, "")
                if raw_code == "" and name == "":
                    n_empty += 1
                    continue
                if raw_code == "" or name == "":
                    c.violation(f"{where}: gics_sector_code={raw_code!r} and "
                                f"gics_sector={name!r} — one is set, the other is not")
                    continue
                code, e = parse_int(raw_code)
                if e:
                    c.violation(f"{where}: gics_sector_code {e}")
                    continue
                assert code is not None
                if not (INT8_MIN <= code <= INT8_MAX):
                    c.violation(
                        f"{where}: gics_sector_code={code} is outside int8 "
                        f"[{INT8_MIN}, {INT8_MAX}] — architecture.md §5.1 declares "
                        f"dtype i1 for a sector field")
                if code not in GICS_SECTOR_CODES:
                    c.violation(f"{where}: gics_sector_code={code} is not an official "
                                f"GICS sector code (§3.2: {sorted(GICS_SECTOR_CODES)})")
                elif GICS_SECTOR_CODES[code] != name:
                    c.violation(f"{where}: gics_sector_code={code} is officially "
                                f"{GICS_SECTOR_CODES[code]!r} but gics_sector={name!r}")
                if code in code_to_name and code_to_name[code][0] != name:
                    c.violation(
                        f"{where}: gics_sector_code={code} maps to {name!r} here and to "
                        f"{code_to_name[code][0]!r} in {code_to_name[code][1]} — the "
                        f"code<->name mapping must be 1:1")
                else:
                    code_to_name.setdefault(code, (name, rel(sl.path)))
                if name in name_to_code and name_to_code[name][0] != code:
                    c.violation(
                        f"{where}: gics_sector={name!r} maps to code {code} here and to "
                        f"{name_to_code[name][0]} in {name_to_code[name][1]} — the "
                        f"code<->name mapping must be 1:1")
                else:
                    name_to_code.setdefault(name, (code, rel(sl.path)))
                if sub == "":
                    self.w3.warn(f"{where}: gics_sub_industry is empty")
        if n_empty:
            self.w3.warn(f"{n_empty} industry row(s) carry no GICS classification")
        if code_to_name:
            c.note("sectors present: " + ", ".join(
                f"{k}={v[0]}" for k, v in sorted(code_to_name.items())))
        c.note(f"{len(self.ind_slices)} PIT industry file(s) reconciled against "
               f"sec_master session by session")

    # -- X13: the session axis is global, not per-file -----------------------
    def check_x13(self) -> None:
        """architecture.md §3.3's `_axes/sessions.json` in L2 form.

        `calendar` is split into one file per year, but `session` indexes the
        whole dataset.  A per-file restart is the dangerous failure here: each
        file still looks internally perfect (0..n contiguous, dates ascending),
        so only the concatenation can catch it.
        """
        c = self.x13
        if not self.cal_files or not self.cal_rows:
            if self.cal_files and not self.cal_rows:
                c.violation("calendar files present but no usable rows — the session "
                            "axis cannot be reconstructed")
            return

        by_file: dict[Path, list[CalRow]] = defaultdict(list)
        for r in self.cal_rows:
            by_file[r.path].append(r)
        order = [(y, q) for y, q in self.cal_files if by_file.get(q)]
        c.n_checked = len(self.cal_rows)

        spans: list[str] = []
        prev_year: Optional[int] = None
        prev_last: Optional[CalRow] = None
        expect = 0
        for year, q in order:
            rows = by_file[q]
            usable = [r for r in rows if r.session is not None and r.d is not None]
            if not usable:
                c.violation(f"{rel(q)}: no row with a usable session/date")
                continue
            first, last = usable[0], usable[-1]
            spans.append(f"{rel(q)}: session {first.session} ({first.d}) .. "
                         f"{last.session} ({last.d}), {len(usable)} rows")

            if prev_last is not None:
                assert prev_last.session is not None and prev_last.d is not None
                assert first.session is not None and first.d is not None
                if first.session != prev_last.session + 1:
                    hint = ""
                    if first.session == 0:
                        hint = ("  <-- the axis RESTARTED at 0 in this file; session is "
                                "the global dataset index (architecture.md §3.3), not a "
                                "within-file row number, so every consumer joining on it "
                                "would silently collide year against year")
                    c.violation(
                        f"{rel(q)}:{first.lineno}: first session {first.session} does "
                        f"not continue {rel(prev_last.path)}, which ended at "
                        f"{prev_last.session} ({prev_last.d}); expected "
                        f"{prev_last.session + 1}{hint}")
                if first.d <= prev_last.d:
                    c.violation(
                        f"{rel(q)}:{first.lineno}: first date {first.d} is not after "
                        f"{prev_last.d}, the last date of {rel(prev_last.path)}")
                if prev_year is not None and year <= prev_year:
                    c.violation(f"{rel(q)}: year {year} does not follow {prev_year}")
            prev_year, prev_last = year, last

        # the concatenation must be 0..N-1 with strictly increasing dates
        prev_date: Optional[date] = None
        for r in self.cal_rows:
            if r.session is None or r.d is None:
                continue        # X8 owns the per-row parse failures
            if r.session != expect:
                c.violation(
                    f"{rel(r.path)}:{r.lineno}: session {r.session} where the "
                    f"concatenated axis expects {expect} (§4: int, from 0, monotone +1 "
                    f"across the whole dataset)")
                expect = r.session
            if prev_date is not None and r.d <= prev_date:
                c.violation(f"{rel(r.path)}:{r.lineno}: date {r.d} does not advance "
                            f"past the previous session's {prev_date}")
            prev_date = r.d
            expect += 1

        for s in spans:
            c.note(s)
        usable_all = [r for r in self.cal_rows
                      if r.session is not None and r.d is not None]
        if usable_all:
            c.note(f"global axis: {len(usable_all)} sessions, "
                   f"{usable_all[0].session} ({usable_all[0].d}) .. "
                   f"{usable_all[-1].session} ({usable_all[-1].d}) across "
                   f"{len(order)} year file(s)")
            c.note("year files cover only the trading days inside the data window, so "
                   "a partial first/last year is expected and not asserted against")

    # -- X14: the PIT reference tables actually vary with the session ---------
    def check_x14(self) -> None:
        """A security appears in the file for date d iff first_trade_date <= d.

        The failure this exists to catch is a producer writing 250 identical
        copies of today's master: every per-file check still passes, and the
        look-ahead that PIT exists to remove is silently back.
        """
        c = self.x14
        if not self.sm_slices:
            return                      # absence already reported by discover()

        # first_trade_date is a fact about the security, so it must not move
        ftd: dict[int, date] = {}
        for d in sorted(self.sm_slices):
            sl = self.sm_slices[d]
            for sid in sl.ids:
                raw = sl.ftd.get(sid, "")
                got, e = parse_date(raw)
                if e:
                    c.violation(f"{rel(sl.path)}: security_id {sid} first_trade_date {e}")
                    continue
                assert got is not None
                if sid in ftd and ftd[sid] != got:
                    c.violation(
                        f"{rel(sl.path)}: security_id {sid} first_trade_date {got} "
                        f"contradicts {ftd[sid]} seen in an earlier session — it is a "
                        f"property of the security, not of the observation date")
                ftd.setdefault(sid, got)

        prev_n: Optional[int] = None
        prev_d: Optional[date] = None
        counts: list[tuple[date, int]] = []
        for d in sorted(self.sm_slices):
            sl = self.sm_slices[d]
            c.n_checked += 1
            counts.append((d, len(sl.ids)))

            expect = {sid for sid, f in ftd.items() if f <= d}
            actual = set(sl.ids)
            early = sorted(actual - expect)
            late = sorted(expect - actual)
            for sid in early[:3]:
                c.violation(
                    f"{rel(sl.path)}: security_id {sid} "
                    f"({sl.ticker.get(sid,'')!r}) is present on {d} but its "
                    f"first_trade_date is {ftd.get(sid)} — look-ahead: the PIT master "
                    f"lists a security that had not started trading")
            for sid in late[:3]:
                c.violation(
                    f"{rel(sl.path)}: security_id {sid} "
                    f"({self.sec_union.get(sid, {}).get('ticker', '')!r}) is missing on "
                    f"{d} although its first_trade_date {ftd.get(sid)} is on or before "
                    f"it")
            if len(early) > 3 or len(late) > 3:
                c.n_violations += max(0, len(early) - 3) + max(0, len(late) - 3)

            if prev_n is not None and len(sl.ids) < prev_n:
                c.violation(
                    f"{rel(sl.path)}: {len(sl.ids)} rows on {d}, down from {prev_n} on "
                    f"{prev_d} — nothing delists in this dataset, so the PIT population "
                    f"must be non-decreasing")
            prev_n, prev_d = len(sl.ids), d

            # ref_asof: single-valued, and never earlier than the session it
            # describes.  It is deliberately NOT equal to date: the reference
            # sources are current snapshots, so a 2025 row is backfilled from a
            # later observation and ref_asof is what makes that visible.
            for sl2, label in ((sl, "sec_master"),
                               (self.ind_slices.get(d), "industry")):
                if sl2 is None:
                    continue
                if len(sl2.ref_asofs) > 1:
                    c.violation(f"{rel(sl2.path)}: {len(sl2.ref_asofs)} distinct "
                                f"ref_asof values in one file: "
                                f"{sorted(sl2.ref_asofs)[:5]}")
                for raw in sl2.ref_asofs:
                    got, e = parse_date(raw)
                    if e:
                        c.violation(f"{rel(sl2.path)}: ref_asof {e}")
                    elif got < d:
                        c.violation(
                            f"{rel(sl2.path)}: ref_asof {got} is BEFORE the session {d} "
                            f"it describes — reference data cannot predate the row it "
                            f"annotates")

        if counts:
            steps = [f"{d}={n}" for i, (d, n) in enumerate(counts)
                     if i == 0 or n != counts[i - 1][1]]
            c.note(f"PIT population over {len(counts)} sessions: "
                   + " -> ".join(steps[:12])
                   + (f" ... (+{len(steps)-12} more steps)" if len(steps) > 12 else ""))
            c.note(f"{len(ftd)} distinct securities ever listed; row count runs "
                   f"{counts[0][1]} -> {counts[-1][1]}")

    # -- X15: the persistent security_id registry ----------------------------
    def check_x15(self) -> None:
        """registry/security_id.{country}.csv — the append-only id axis.

        architecture.md §3.4 requires an id that is never reused and §3.3 an
        append-only column axis.  Deriving ids by sorting each run's population
        satisfies neither, so they live in a registry outside storage/.
        """
        c = self.x15
        path = self.registry
        if not path.is_file():
            c.violation(
                f"missing {rel(path)} — security_id is assigned from this persistent "
                f"registry; without it ids are only as stable as the last run")
            return
        pf = read_pipe_file(path, "registry", c, c)
        if pf is None or not pf.rows:
            c.violation(f"{rel(path)}: no usable rows")
            return
        c.n_checked = len(pf.rows)

        ids: dict[int, int] = {}
        keys: dict[tuple[str, str], int] = {}
        yahoo: dict[int, str] = {}
        for ln, row in zip(pf.linenos, pf.rows):
            sid, e = parse_int(row.get("security_id", ""))
            if e:
                c.violation(f"{rel(path)}:{ln}: security_id {e}")
                continue
            assert sid is not None
            if sid in ids:
                c.violation(f"{rel(path)}:{ln}: duplicate security_id {sid} (first at "
                            f"line {ids[sid]}) — §3.4 ids are never reused")
            else:
                ids[sid] = ln
            key = (row.get("cik", ""), row.get("ticker_yahoo", ""))
            if key in keys:
                c.violation(
                    f"{rel(path)}:{ln}: duplicate (cik, ticker_yahoo) {key} (first at "
                    f"line {keys[key]}) — the assignment key must identify one entry, "
                    f"or a rebuild would hand the same security two ids")
            else:
                keys[key] = ln
            yahoo[sid] = row.get("ticker_yahoo", "")
            if row.get("ticker_yahoo", "") == "":
                c.violation(f"{rel(path)}:{ln}: empty ticker_yahoo")

        if ids:
            lo, hi = min(ids), max(ids)
            if lo != 1:
                c.violation(f"{rel(path)}: ids start at {lo}, not 1 (§3.4)")
            holes = sorted(set(range(lo, hi + 1)) - set(ids))
            if holes:
                c.violation(
                    f"{rel(path)}: {len(holes)} id(s) missing between {lo} and {hi}, "
                    f"e.g. {holes[:20]} — a retired security must keep its row so the "
                    f"id is never handed out again; a hole means an entry was deleted "
                    f"and the next rebuild will reuse or skip that number")

        # every id used anywhere in L2 must resolve here, and the registry is a
        # superset: retired entries may legitimately linger with no data
        used: set[int] = set()
        used.update(sid for sid in self.sec_union)
        used.update(b.sid for b in self.bars if b.sid is not None)
        used.update(ca.sid for ca in self.cas if ca.sid is not None)
        missing = sorted(used - set(ids))
        for sid in missing[:self.rep.max_show]:
            c.violation(
                f"security_id {sid} ({self.sec_union.get(sid, {}).get('ticker', '')!r}) "
                f"appears in the L2 data but has no registry row — the id did not come "
                f"from the registry, so nothing guarantees it is stable")
        if len(missing) > self.rep.max_show:
            c.n_violations += len(missing) - self.rep.max_show
        extra = sorted(set(ids) - used)
        if extra:
            sample = [f"{s}:{yahoo.get(s, '')}" for s in extra[:10]]
            c.note(f"{len(extra)} registry entry(ies) carry no data in this delivery "
                   f"(allowed — retired securities keep their id): {sample}")
        # the registry is the id authority: its ticker must match the master's
        mism = 0
        for sid, info in sorted(self.sec_union.items()):
            want = yahoo.get(sid)
            got = info.get("ticker_yahoo", "")
            if want is not None and got and want != got:
                mism += 1
                if mism <= 3:
                    c.violation(
                        f"security_id {sid}: registry says ticker_yahoo {want!r}, "
                        f"sec_master says {got!r} — the join key disagrees with the "
                        f"axis that assigned the id")
        if not missing and ids:
            c.note(f"{len(used)} security_id(s) used in L2, all resolved; registry "
                   f"holds {len(ids)} entry(ies) spanning 1..{max(ids)}")

    # -- vendor loading ----------------------------------------------------
    def load_vendor(self) -> tuple[dict[str, VendorSeries], bool]:
        if self.skip_raw:
            self.v2.note("--skip-raw was passed: the vendor half of V2 is unavailable")
            return {}, False
        if not self.raw.is_dir():
            msg = (f"raw vendor directory {rel(self.raw)} does not exist; V2 requires "
                   f"the vendor quote.close series to prove the adjustment direction")
            self.v2.violation(msg)
            self.x9.violation(msg)
            return {}, False
        wanted = {sr.raw.get("ticker_yahoo", "") for sr in self.sec_rows}
        wanted.discard("")
        out: dict[str, VendorSeries] = {}
        n_err = 0
        for p in sorted(self.raw.iterdir()):
            if p.suffix != ".json" or not p.is_file():
                continue
            if wanted and p.stem not in wanted:
                self.w3.warn(f"{rel(p)}: vendor payload for {p.stem!r} which is not a "
                             f"ticker_yahoo in sec_master")
                continue
            vs, err = load_vendor_chart(p, self.cal_date_set)
            if err:
                n_err += 1
                self.w3.warn(err)
                continue
            assert vs is not None
            for prob in vs.problems[:3]:
                self.w3.warn(prob)
            out[p.stem] = vs
        if not out:
            self.v2.violation(
                f"{rel(self.raw)} yielded no usable vendor payloads "
                f"({n_err} unreadable) — V2's vendor cross-check cannot run")
        else:
            missing = sorted(wanted - set(out))
            if missing:
                self.w3.warn(
                    f"{len(missing)} sec_master ticker_yahoo value(s) have no vendor "
                    f"payload under {rel(self.raw)}, e.g. {missing[:10]}")
        return out, True

    # -- driver ------------------------------------------------------------
    def run(self) -> Report:
        self.load()
        vendor, loaded_all = self.load_vendor()
        self._vendor = vendor
        splits = self.classify_splits(vendor)
        self._splits = splits
        self.check_v1(splits)
        self.check_v3()
        self.check_v4()
        self.check_v5()
        self.check_v6()
        self.check_v7()
        self.check_extras()
        self.check_v2(splits)
        self.check_x9(vendor, loaded_all)
        self.check_x10(splits)
        self.check_x11(splits)
        self.check_x12()
        self.check_x13()
        self.check_x14()
        self.check_x15()
        # V8 has no separate pass: it is asserted while reading every file.
        if self.v8.n_checked == 0 and self.v8.status == "PASS":
            self.v8.violation("no L2 data lines were read at all — V8 has nothing to "
                              "assert, which cannot count as a pass")
        return self.rep


# --------------------------------------------------------------------------
# entry point
# --------------------------------------------------------------------------

def main(argv: Optional[list[str]] = None) -> int:
    repo = Path(__file__).resolve().parent.parent
    ap = argparse.ArgumentParser(
        description="Validate the alphakit L2 dataset against docs/l2_schema.md §8.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    ap.add_argument("--l2-dir", type=Path,
                    default=repo / "storage" / "data" / "base" / "l2" / "us",
                    help="L2 delivery directory (§1)")
    ap.add_argument("--raw-dir", type=Path,
                    default=(repo / "storage" / "data" / "base" / "l1" / "yahoo"
                             / "chart"),
                    help="raw Yahoo chart payloads (read-only; needed by V2)")
    ap.add_argument("--registry", type=Path,
                    default=repo / "registry" / "security_id.us.csv",
                    help="persistent security_id registry (outside storage/)")
    ap.add_argument("--report", type=Path, default=None,
                    help="report path (default: <l2-dir>/_validation_report.txt)")
    ap.add_argument("--max-show", type=int, default=MAX_SHOW_DEFAULT,
                    help="offending rows printed per failing check")
    ap.add_argument("--skip-raw", action="store_true",
                    help="do not read the vendor payloads (V2 then FAILS by design)")
    args = ap.parse_args(argv)

    l2 = args.l2_dir.expanduser()
    raw = args.raw_dir.expanduser()
    report_path = args.report or (l2 / "_validation_report.txt")

    started = datetime.now(timezone.utc)
    try:
        validator = Validator(l2, raw, max_show=max(1, args.max_show),
                              skip_raw=args.skip_raw,
                              registry=args.registry.expanduser())
        rep = validator.run()
        header = [
            "alphakit L2 validation report",
            f"generated   : {started.isoformat(timespec='seconds')}",
            "contract    : docs/l2_schema.md §8 (V1..V8) + X-series structural checks",
            f"l2_dir      : {l2}",
            f"raw_dir     : {raw}"
            + ("  [SKIPPED via --skip-raw]" if args.skip_raw else ""),
            f"registry    : {args.registry}",
            f"tables      : {len(validator.bar_files)} pv, "
            f"{len(validator.ca_files)} cax, "
            f"{len(validator.sm_files)} sec_master, "
            f"{len(validator.ind_files)} industry, "
            f"{len(validator.cal_files)} calendar",
            "exit policy : 0 only if V1..V8 all PASS and no X-check FAILs; "
            "W-series findings never fail the run",
        ]
        text = rep.render(header)
    except Exception as e:  # never exit 0 on an internal error
        import traceback
        sys.stderr.write("validator crashed:\n" + traceback.format_exc())
        try:
            report_path.parent.mkdir(parents=True, exist_ok=True)
            report_path.write_text(
                f"VALIDATOR CRASHED: {e!r}\n{traceback.format_exc()}", encoding="utf-8")
        except OSError:
            pass
        return 2


    ok = not any(c.hard and c.failed for c in rep.checks)
    try:
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(text, encoding="utf-8")
    except OSError as e:
        sys.stderr.write(f"could not write {report_path}: {e}\n")
        sys.stdout.write(text)
        return 2

    sys.stdout.write(text)
    sys.stdout.write(f"\nreport written to {report_path}\n")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
