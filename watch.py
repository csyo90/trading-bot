#!/usr/bin/env python3
"""
13F Watcher — polls SEC EDGAR for the latest 13F filings from a fixed
watchlist of funds and prints a digest.

Stdlib only. No API key. No card. Safe to run on a schedule.
Data path (verified 2026-11-08):
  hardcoded CIK (verified)        -> filings page HTML (browse-edgar)
  filings page (HTML)             -> recent 13F-HR filings + index URL
  filing index page (HTML)        -> list of XML docs
  info-table XML                  -> holdings rows
                                    (new schema: <infoTable> rows,
                                     old schema: <info> rows — both handled)
  cover XML (new schema)          -> periodOfReport

Exit code 0 on success (even if some funds fail); 1 only when ALL
funds fail (network/EDGAR unreachable).
"""
import re
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

UA = "Casey Research Bot contact@example.com"
TIMEOUT = 20
NEW_WINDOW_HOURS = 72   # filings this fresh get a [NEW] tag
TOP_HOLDINGS = 8
PER_FUND = 1            # how many recent filings to show per fund

# CIKs verified against EDGAR filings pages 2026-11-08.
FUNDS = [
    ("Berkshire Hathaway",      "1067983"),
    ("Renaissance Technologies","1037389"),
    ("Citadel Advisors",        "1423053"),
    ("Third Point",             "1040273"),
    ("D.E. Shaw",               "1009207"),
    ("Greenlight Capital",      "1079114"),
    ("Duquesne (Druckenmiller)","1536411"),
]

# --- EDGAR HTML structure (verified 2026-11-08) ---------------------------

FILING_ROW_RE = re.compile(
    r'<td[^>]*>13F-HR/?</td>\s*'
    r'<td[^>]*><a href="(/Archives/edgar/data/\d+/\d+/[^"]+-index\.htm)"[\s\S]*?'
    r'<td>(\d{4}-\d{2}-\d{2})</td>', re.S)

INDEX_XML_RE = re.compile(
    r'href="(/Archives/edgar/data/\d+/\d+/[^"]+\.xml)"')


def get(url):
    if not url.startswith("http"):
        url = "https://www.sec.gov" + url
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept-Encoding": "identity",
    })
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return r.read().decode("utf-8", errors="replace")


def recent_13f_filings(cik):
    """Filings page -> [(index_url, filing_date)] newest first."""
    url = (f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany"
           f"&CIK={cik}&type=13F&count=10")
    html = get(url)
    out = []
    for idx_url, date in FILING_ROW_RE.findall(html):
        out.append((idx_url, date))
    # dedupe, keep order
    seen, unique = set(), []
    for item in out:
        if item[0] not in seen:
            seen.add(item[0])
            unique.append(item)
    return unique


def index_xml_candidates(index_url):
    html = get(index_url)
    cands, seen = [], set()
    for link in INDEX_XML_RE.findall(html):
        if "xslForm" in link or link in seen:
            continue
        seen.add(link)
        cands.append(link)
    return cands


def strip_ns(tag):
    return tag.split("}")[-1]


def parse_xml_doc(xml_text):
    """
    Parse one filing XML doc.
    Returns (rows, period): rows = [(issuer, title, value, shrs)] (possibly
    empty), period = periodOfReport if present else None.
    Handles both the new schema (<infoTable> rows) and legacy (<info> rows).
    """
    root = ET.fromstring(xml_text)
    rows = []
    period = None
    for el in root.iter():
        t = strip_ns(el.tag)
        if t == "periodOfReport" and period is None:
            period = (el.text or "?").strip()
        elif t in ("infoTable", "info"):
            # Flat, namespace-agnostic dict over the row and its children
            # (new schema nests sshPrnamt inside shrsOrPrnAmt).
            d = {}
            for c in el.iter():
                key = strip_ns(c.tag)
                if key != t and c.text and key not in d:
                    d[key] = c.text.strip()
            try:
                value = float(d.get("value", 0) or 0)
            except ValueError:
                value = 0.0
            shrs_raw = (d.get("sshPrnamt")
                        or d.get("shrs")
                        or d.get("prnamt") or "0")
            try:
                shrs = float(shrs_raw or 0)
            except ValueError:
                shrs = 0.0
            rows.append((d.get("nameOfIssuer", "?"),
                         d.get("titleOfClass", ""), value, shrs))
    return rows, period


def parse_filing(index_url):
    """
    Walk the filing's XML docs until one yields holdings rows.
    Returns (period, total, rows) or raises ValueError.
    """
    period = None
    last_err = None
    for cand in index_xml_candidates(index_url)[:4]:
        try:
            xml_text = get(cand)
            rows, p = parse_xml_doc(xml_text)
        except (urllib.error.URLError, ET.ParseError) as e:
            last_err = e
            continue
        if p:
            period = period or p
        if rows:
            rows.sort(key=lambda r: r[2], reverse=True)
            total = sum(r[2] for r in rows)
            return period or "?", total, rows
    if last_err:
        raise ValueError(f"no holdings rows (last error: {last_err})")
    raise ValueError("no holdings rows in any XML doc")


def usd(v):
    if v >= 1e9:
        return f"${v/1e9:.2f}B"
    if v >= 1e6:
        return f"${v/1e6:.1f}M"
    return f"${v:,.0f}"


def fmt_shrs(s):
    if s >= 1e6:
        return f"{s/1e6:.1f}M"
    return f"{s:,.0f}"


def main():
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=NEW_WINDOW_HOURS)
    failures = 0

    print("=" * 64)
    print(f"13F WATCH DIGEST  -  {now:%Y-%m-%d %H:%M} UTC")
    print("=" * 64)

    for label, cik in FUNDS:
        try:
            print(f"\n### {label}  (CIK {cik})")
            filings = recent_13f_filings(cik)[:PER_FUND]
            if not filings:
                raise ValueError("no 13F-HR filings found")

            for index_url, filed in filings:
                filed_dt = datetime.strptime(filed, "%Y-%m-%d").replace(
                    tzinfo=timezone.utc)
                fresh = " [NEW]" if filed_dt >= cutoff else ""
                print(f"  13F  filed {filed}{fresh}")
                period, total, rows = parse_filing(index_url)
                print(f"    period {period}  |  total {usd(total)}  |  "
                      f"{len(rows)} positions")
                for issuer, title, value, shrs in rows[:TOP_HOLDINGS]:
                    print(f"      {usd(value):>9}  {fmt_shrs(shrs):>8}  "
                          f"{issuer}{' - ' + title if title else ''}")
                rest = len(rows) - TOP_HOLDINGS
                if rest > 0:
                    print(f"      ... +{rest} more positions")
                break  # PER_FUND = 1 for now
            time.sleep(0.2)  # stay well under SEC 10 req/s limit
        except Exception as e:
            failures += 1
            print(f"\n### {label}  - FAILED: {e}")

    print("\n" + "=" * 64)
    if failures == len(FUNDS):
        print("TOTAL FAILURE - network/EDGAR unreachable")
        return 1
    print(f"Done. {len(FUNDS) - failures}/{len(FUNDS)} funds OK, "
          f"{failures} failed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
