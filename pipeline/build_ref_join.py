#!/usr/bin/env python
"""Build data/interim/ref_join.csv -- reference attributes for every S&P 500 constituent.

Keyed by ``ticker_yahoo``.  A downstream component adds ``security_id`` + coverage
columns and emits ``data/l2/sec_master.csv``; this module does NOT write sec_master.

Sources (all read-only, already on disk -- nothing is downloaded here):
  A. data/raw/ref/sp500_constituents_{ASOF}.csv          GICS + CIK + date added
  B. data/raw/ref/nasdaqtraded_{ASOF}.txt                listing attrs, pipe-delimited
  C. data/raw/ref/sec_company_tickers_exchange_{ASOF}.json   CIK cross-check

Output format follows docs/l2_schema.md 2: pipe-delimited, UTF-8, LF, header row,
no quoting, missing = empty field, and every text field has ``|`` / CR / LF stripped
out before writing.

--------------------------------------------------------------------------------
SYMBOL FORMATS -- read before touching the join
--------------------------------------------------------------------------------
Four conventions coexist across these three files:

    entity          S&P `Symbol`   NasdaqTrader `Symbol` / `CQS` / `NASDAQ`   Yahoo
    class share     BRK.B          BRK.B  / BRK.B  / BRK.B                    BRK-B
    preferred       (n/a)          ABR$D  / ABRpD  / ABR-D                    ABR-D

The `-` in NasdaqTrader's `NASDAQ Symbol` column encodes only the PREFERRED-share
suffix (translated from `$`).  CLASS shares keep their `.` in that column -- all 37
dotted `NASDAQ Symbol` values in the 2026-08-30 snapshot (BRK.B, BF.B, BIO.B,
HEI.A, MOG.A, ...) carry a dot, and `BRK-B` / `BF-B` appear nowhere in the file.

Yahoo, by contrast, accepts only `-` (BRK-B works, BRK.B returns Not Found).

Consequence: a verbatim equality join of ``ticker_yahoo`` against `NASDAQ Symbol`
silently drops exactly the class shares -- verified: BRK-B and BF-B, the only two
dotted S&P constituents, both miss.  This module therefore joins on a NORMALISED
key (``.`` -> ``-`` applied to BOTH sides), which matches 503/503, and is asserted
collision-free at load time.  The ``ticker_nasdaq`` / ``ticker_cqs`` columns still
carry NasdaqTrader's raw values per l2_schema.md 3 ("...  "), so
``ticker_nasdaq`` reads ``BRK.B`` while ``ticker_yahoo`` reads ``BRK-B``.

NOTE for the schema owner: l2_schema.md 3 states
``ticker_yahoo`` = "NasdaqTrader  `NASDAQ Symbol` " and "class share  `-`
BRK-B".  The second half does not hold for this snapshot; the fetch key must be
derived (``.`` -> ``-``), not read verbatim from that column.
"""

from __future__ import annotations

import csv
import datetime as dt
import json
import os
import sys
from collections import Counter

ASOF = "20260830"

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SP500_PATH = os.path.join(REPO, "storage", "data", "base", "l1", "ref", f"sp500_constituents_{ASOF}.csv")
NASDAQ_PATH = os.path.join(REPO, "storage", "data", "base", "l1", "ref", f"nasdaqtraded_{ASOF}.txt")
SEC_PATH = os.path.join(REPO, "storage", "data", "base", "l1", "ref", f"sec_company_tickers_exchange_{ASOF}.json")
# No interim artifact: build_l2.py imports build_rows() and consumes the join in
# memory.  Running this module standalone writes a report only, for verification.
OUT_PATH = None

DELIM = "|"
HEADER = [
    "ticker_yahoo",
    "ticker_sp",
    "ticker_nasdaq",
    "ticker_cqs",
    "name",
    "exchange",
    "cik",
    "gics_sector",
    "gics_sub_industry",
    "is_etf",
    "round_lot",
    "financial_status",
    "sp500_date_added",
]

# ``|`` is the delimiter and we do not quote, so it can never survive into a field.
# CR/LF would forge a record boundary.  l2_schema.md 2 mandates replacing all
# three with a space.
_FORBIDDEN = str.maketrans({"|": " ", "\r": " ", "\n": " "})


def clean(value) -> str:
    """Make any value safe for an unquoted pipe-delimited field."""
    if value is None:
        return ""
    return str(value).translate(_FORBIDDEN).strip()


def to_yahoo(sp_symbol: str) -> str:
    """S&P `Symbol` -> Yahoo fetch key.  BRK.B -> BRK-B (verified against Yahoo)."""
    return sp_symbol.strip().replace(".", "-")


def norm_key(symbol: str) -> str:
    """Join key: fold the class-share `.` onto `-` so both conventions meet."""
    return symbol.strip().replace(".", "-")


def parse_date(value: str) -> str:
    """S&P `Date added` -> YYYY-MM-DD, or "" when unparseable."""
    value = (value or "").strip()
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%Y/%m/%d", "%d-%b-%Y", "%B %d, %Y"):
        try:
            return dt.datetime.strptime(value, fmt).date().isoformat()
        except ValueError:
            continue
    return ""


def pad_cik(value) -> str:
    """SEC CIK -> zero-padded 10 digits, or "" when absent/non-numeric."""
    text = str(value).strip() if value is not None else ""
    if not text or not text.isdigit():
        return ""
    return text.zfill(10)


# --------------------------------------------------------------------------- load


def load_sp500(path):
    with open(path, encoding="utf-8", newline="") as fh:
        rows = list(csv.DictReader(fh))
    if not rows:
        raise SystemExit(f"FATAL: no rows in {path}")
    return rows


def load_nasdaqtraded(path):
    """Return (index_by_normalised_NASDAQ_Symbol, stats).

    The trailing "File Creation Time: ..." line is not data; per the contract we
    drop every line whose field count differs from the header rather than
    special-casing that one string.  The file is CRLF, so read with universal
    newlines and strip any stray CR.
    """
    with open(path, encoding="utf-8", newline=None) as fh:
        lines = [ln.rstrip("\r\n") for ln in fh.read().split("\n")]
    lines = [ln for ln in lines if ln != ""]
    header = lines[0].split(DELIM)

    rows, malformed = [], []
    for line in lines[1:]:
        parts = line.split(DELIM)
        if len(parts) != len(header):
            malformed.append(line)
            continue
        rows.append(dict(zip(header, parts)))

    index, collisions = {}, []
    for row in rows:
        key = norm_key(row["NASDAQ Symbol"])
        if key in index:
            collisions.append((key, index[key]["Symbol"], row["Symbol"]))
        index[key] = row

    verbatim = {row["NASDAQ Symbol"].strip() for row in rows}
    stats = {
        "header": header,
        "n_rows": len(rows),
        "n_malformed": len(malformed),
        "malformed": malformed,
        "collisions": collisions,
        "verbatim": verbatim,
    }
    return index, stats


def load_sec(path):
    with open(path, encoding="utf-8") as fh:
        payload = json.load(fh)
    fields = payload["fields"]
    col = {name: i for i, name in enumerate(fields)}
    index = {}
    for record in payload["data"]:
        ticker = record[col["ticker"]]
        if not ticker:
            continue
        # SEC uses Yahoo's `-` convention for class shares (BRK-B, BF-B, MOG-A),
        # so its ticker keys line up with ticker_yahoo directly.
        index[str(ticker).strip()] = {
            "cik": record[col["cik"]],
            "name": record[col["name"]],
            "exchange": record[col["exchange"]],
        }
    return index


# --------------------------------------------------------------------------- build


def build_rows(sp500, nasdaq_index, sec_index):
    out_rows = []
    report = {
        "no_nasdaq_join": [],
        "no_sec_match": [],
        "cik_mismatch": [],
        "cik_missing": [],
        "date_unparseable": [],
        "etf_flagged": [],
        "financial_status_flagged": [],
    }

    for sp in sp500:
        sp_symbol = sp["Symbol"].strip()
        yahoo = to_yahoo(sp_symbol)
        nas = nasdaq_index.get(norm_key(yahoo))

        if nas is None:
            report["no_nasdaq_join"].append((yahoo, sp_symbol, sp.get("Security", "")))

        # --- cik: S&P is preferred; SEC is a cross-check, never a silent override.
        sp_cik = pad_cik(sp.get("CIK"))
        sec = sec_index.get(yahoo)
        sec_cik = pad_cik(sec["cik"]) if sec else ""
        if sec is None:
            report["no_sec_match"].append(yahoo)
        elif sp_cik and sec_cik and sp_cik != sec_cik:
            report["cik_mismatch"].append((yahoo, sp_cik, sec_cik, clean(sec["name"])))

        cik = sp_cik or sec_cik  # fall back to SEC only when S&P has nothing
        if not cik:
            report["cik_missing"].append(yahoo)

        date_added = parse_date(sp.get("Date added", ""))
        if not date_added and (sp.get("Date added") or "").strip():
            report["date_unparseable"].append((yahoo, sp.get("Date added")))

        if nas is not None:
            etf_raw = nas["ETF"].strip()
            is_etf = {"Y": "1", "N": "0"}.get(etf_raw, "")
            round_lot = clean(nas["Round Lot Size"])
            fin_status = clean(nas["Financial Status"])
            exchange = clean(nas["Listing Exchange"])
            name = clean(nas["Security Name"]) or clean(sp.get("Security"))
            ticker_nasdaq = clean(nas["NASDAQ Symbol"])
            ticker_cqs = clean(nas["CQS Symbol"])
            if is_etf == "1":
                report["etf_flagged"].append((yahoo, name))
            if fin_status not in ("N", ""):
                report["financial_status_flagged"].append((yahoo, fin_status, name))
        else:
            is_etf = round_lot = fin_status = exchange = ""
            ticker_nasdaq = ticker_cqs = ""
            name = clean(sp.get("Security"))

        out_rows.append(
            {
                "ticker_yahoo": clean(yahoo),
                "ticker_sp": clean(sp_symbol),
                "ticker_nasdaq": ticker_nasdaq,
                "ticker_cqs": ticker_cqs,
                "name": name,
                "exchange": exchange,
                "cik": cik,
                "gics_sector": clean(sp.get("GICS Sector")),
                "gics_sub_industry": clean(sp.get("GICS Sub-Industry")),
                "is_etf": is_etf,
                "round_lot": round_lot,
                "financial_status": fin_status,
                "sp500_date_added": date_added,
            }
        )

    return out_rows, report


def write_output(path, rows):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(DELIM.join(HEADER) + "\n")
        for row in rows:
            fh.write(DELIM.join(row[c] for c in HEADER) + "\n")


# ---------------------------------------------------------------------- verify


def verify(path, rows, sp500, nasdaq_stats, report):
    """Re-read the written file and check it.  Returns list of failure strings."""
    failures = []
    out = []

    def say(line=""):
        out.append(line)

    with open(path, encoding="utf-8", newline="") as fh:
        text = fh.read()
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()

    say("=" * 78)
    say("ref_join.csv -- verification report")
    say("=" * 78)

    # -- 0. source ingest
    say()
    say("[0] SOURCE INGEST")
    say(f"    S&P constituents read            : {len(sp500)}")
    say(f"    NasdaqTrader data rows kept      : {nasdaq_stats['n_rows']}")
    say(f"    NasdaqTrader rows dropped        : {nasdaq_stats['n_malformed']} "
        f"(field count != {len(nasdaq_stats['header'])})")
    for bad in nasdaq_stats["malformed"]:
        say(f"        dropped: {bad[:70]!r}")
    if nasdaq_stats["collisions"]:
        failures.append("normalised NASDAQ Symbol collisions in source")
        for key, a, b in nasdaq_stats["collisions"]:
            say(f"    !! COLLISION on normalised key {key}: {a} vs {b}")
    else:
        say("    normalised-key collisions        : 0")

    # -- 1. row count + uniqueness
    say()
    say("[1] ROW COUNT / KEY UNIQUENESS")
    n_data = len(lines) - 1
    ok = n_data == len(sp500)
    say(f"    data rows written                : {n_data} "
        f"({'OK' if ok else 'FAIL'} vs {len(sp500)} constituents)")
    if not ok:
        failures.append(f"row count {n_data} != {len(sp500)} constituents")
    keys = [r["ticker_yahoo"] for r in rows]
    dupes = [k for k, v in Counter(keys).items() if v > 1]
    say(f"    ticker_yahoo unique              : {'OK' if not dupes else 'FAIL ' + str(dupes)}")
    if dupes:
        failures.append(f"duplicate ticker_yahoo: {dupes}")
    blank = [r["ticker_sp"] for r in rows if not r["ticker_yahoo"]]
    if blank:
        failures.append(f"empty ticker_yahoo for: {blank}")

    # -- 2. nasdaq join
    say()
    say("[2] NASDAQTRADER JOIN  (normalised `.`->`-` on both sides)")
    joined = sum(1 for r in rows if r["ticker_nasdaq"])
    say(f"    joined                           : {joined}/{len(rows)}")
    if report["no_nasdaq_join"]:
        say(f"    NOT joined ({len(report['no_nasdaq_join'])}):")
        for yahoo, sp_sym, sec_name in report["no_nasdaq_join"]:
            say(f"        {yahoo:<8} (S&P symbol {sp_sym}) {sec_name}")
    else:
        say("    NOT joined                       : none")
    # contrast: what a verbatim join would have lost
    verbatim_miss = [r["ticker_yahoo"] for r in rows
                     if r["ticker_yahoo"] not in nasdaq_stats["verbatim"]]
    say(f"    (verbatim join would have missed : {len(verbatim_miss)} -> "
        f"{verbatim_miss if verbatim_miss else 'none'})")

    # -- 3. cik
    say()
    say("[3] CIK")
    with_cik = sum(1 for r in rows if r["cik"])
    say(f"    rows with a CIK                  : {with_cik}/{len(rows)}")
    say(f"    all 10-digit zero-padded         : "
        f"{all(len(r['cik']) == 10 and r['cik'].isdigit() for r in rows if r['cik'])}")
    if report["cik_missing"]:
        say(f"    MISSING cik ({len(report['cik_missing'])}): {report['cik_missing']}")
    say(f"    matched in SEC file              : {len(rows) - len(report['no_sec_match'])}/{len(rows)}")
    if report["no_sec_match"]:
        say(f"    not in SEC file ({len(report['no_sec_match'])}): {report['no_sec_match']}")
    if report["cik_mismatch"]:
        say(f"    S&P-vs-SEC MISMATCHES ({len(report['cik_mismatch'])}) "
            f"-- S&P value kept, not silently resolved:")
        for yahoo, sp_cik, sec_cik, sec_name in report["cik_mismatch"]:
            say(f"        {yahoo:<8} sp={sp_cik} sec={sec_cik}  ({sec_name})")
    else:
        say("    S&P-vs-SEC mismatches            : 0")
    shared = Counter(r["cik"] for r in rows if r["cik"])
    multi = sorted((c, n) for c, n in shared.items() if n > 1)
    if multi:
        say(f"    CIKs shared by >1 constituent    : {len(multi)} (dual-class, expected)")
        for cik, n in multi:
            tk = [r["ticker_yahoo"] for r in rows if r["cik"] == cik]
            say(f"        {cik} x{n}: {tk}")

    # -- 4. distributions
    say()
    say("[4] DISTRIBUTIONS")
    ex = Counter(r["exchange"] or "(empty)" for r in rows)
    say("    exchange:")
    for k, v in sorted(ex.items(), key=lambda kv: -kv[1]):
        say(f"        {k:<10} {v}")
    say("    gics_sector:")
    gs = Counter(r["gics_sector"] or "(empty)" for r in rows)
    for k, v in sorted(gs.items(), key=lambda kv: -kv[1]):
        say(f"        {k:<24} {v}")
    say("    round_lot:")
    for k, v in sorted(Counter(r["round_lot"] or "(empty)" for r in rows).items(),
                       key=lambda kv: -kv[1]):
        say(f"        {k:<10} {v}")
    say("    financial_status:")
    for k, v in sorted(Counter(r["financial_status"] or "(empty)" for r in rows).items(),
                       key=lambda kv: -kv[1]):
        say(f"        {k:<10} {v}")

    # -- 5. anomaly flags
    say()
    say("[5] ANOMALY FLAGS")
    if report["etf_flagged"]:
        say(f"    is_etf=1 ({len(report['etf_flagged'])}):")
        for yahoo, name in report["etf_flagged"]:
            say(f"        {yahoo:<8} {name}")
    else:
        say("    is_etf=1                         : none")
    if report["financial_status_flagged"]:
        say(f"    financial_status not in (N, empty) ({len(report['financial_status_flagged'])}):")
        for yahoo, status, name in report["financial_status_flagged"]:
            say(f"        {yahoo:<8} [{status}] {name}")
    else:
        say("    financial_status not in (N, empty): none")
    if report["date_unparseable"]:
        say(f"    unparseable Date added ({len(report['date_unparseable'])}):")
        for yahoo, raw in report["date_unparseable"]:
            say(f"        {yahoo:<8} {raw!r}")
    else:
        say("    unparseable sp500_date_added     : none")
    empty_dates = [r["ticker_yahoo"] for r in rows if not r["sp500_date_added"]]
    say(f"    empty sp500_date_added           : {len(empty_dates)}"
        + (f" {empty_dates}" if empty_dates else ""))

    # -- 6. format self-consistency (l2_schema.md 8 V8)
    say()
    say("[6] FORMAT (l2_schema.md 2 / V8)")
    n_fields = len(HEADER)
    say(f"    header field count               : {len(lines[0].split(DELIM))} "
        f"(expected {n_fields})")
    if lines[0] != DELIM.join(HEADER):
        failures.append("header does not match contract")
    bad_count = [(i + 2, len(l.split(DELIM))) for i, l in enumerate(lines[1:])
                 if len(l.split(DELIM)) != n_fields]
    say(f"    lines with wrong field count     : {len(bad_count)} "
        f"{'OK' if not bad_count else 'FAIL ' + str(bad_count[:5])}")
    if bad_count:
        failures.append(f"{len(bad_count)} lines with wrong field count")
    # a raw `|` inside a field is unrepresentable once field counts are right,
    # but assert on the values themselves too.
    raw_pipe = [(r["ticker_yahoo"], c) for r in rows for c in HEADER if "|" in r[c]]
    say(f"    fields containing a raw '|'      : {len(raw_pipe)} "
        f"{'OK' if not raw_pipe else 'FAIL ' + str(raw_pipe)}")
    if raw_pipe:
        failures.append(f"raw pipe in fields: {raw_pipe}")
    ctrl = [(r["ticker_yahoo"], c) for r in rows for c in HEADER
            if "\r" in r[c] or "\n" in r[c]]
    say(f"    fields containing CR/LF          : {len(ctrl)} "
        f"{'OK' if not ctrl else 'FAIL ' + str(ctrl)}")
    if ctrl:
        failures.append(f"CR/LF in fields: {ctrl}")
    say(f"    quote characters in file         : {text.count(chr(34))} (contract: no quoting)")
    has_cr = "\r" in text
    say("    line terminator                  : LF only "
        + ("(OK)" if not has_cr else "(FAIL: CR present)"))
    if has_cr:
        failures.append("CR present in output file")
    say(f"    bytes                            : {len(text.encode('utf-8'))}")

    say()
    say("=" * 78)
    say(f"RESULT: {'PASS -- all checks green' if not failures else 'FAIL'}")
    for f in failures:
        say(f"    FAILED: {f}")
    say("=" * 78)

    print("\n".join(out))
    return failures


def join_rows():
    """The reference join, in memory.  Consumed directly by build_l2.py."""
    sp500 = load_sp500(SP500_PATH)
    nasdaq_index, nasdaq_stats = load_nasdaqtraded(NASDAQ_PATH)
    sec_index = load_sec(SEC_PATH)
    rows, report = build_rows(sp500, nasdaq_index, sec_index)
    return rows, sp500, nasdaq_stats, report


def main():
    import tempfile
    rows, sp500, nasdaq_stats, report = join_rows()
    with tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False) as fh:
        tmp = fh.name
    write_output(tmp, rows)
    failures = verify(tmp, rows, sp500, nasdaq_stats, report)
    os.unlink(tmp)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
