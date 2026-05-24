#!/usr/bin/env python3
"""
Dallas County, TX — Motivated Seller Lead Scraper
Uses the dallas.tx.publicsearch.us REST API directly to fetch
the last 7 days of motivated-seller document types.
"""

import csv
import json
import logging
import re
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import requests

# ── optional dbfread ──────────────────────────────────────────────────────────
try:
    from dbfread import DBF
    import zipfile, io
    HAS_DBF = True
except ImportError:
    HAS_DBF = False

# ─────────────────────────────────────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("dallas_scraper")

BASE_DIR      = Path(__file__).resolve().parent.parent
DASHBOARD_DIR = BASE_DIR / "dashboard"
DATA_DIR      = BASE_DIR / "data"
DASHBOARD_DIR.mkdir(parents=True, exist_ok=True)
DATA_DIR.mkdir(parents=True, exist_ok=True)

# Public Search API base — discovered from network traffic on the portal
API_BASE      = "https://dallas.tx.publicsearch.us"
SEARCH_EP     = f"{API_BASE}/api/document/search"
DOC_URL_BASE  = f"{API_BASE}/results"

# DCAD parcel data
DCAD_BULK_URL = "https://www.dcad.org/wp-content/uploads/data/appraisal_data.zip"
DCAD_ALT_URL  = "https://www.dcad.org/data/"

RETRY_LIMIT   = 3
RETRY_DELAY   = 5

# ── Target document type keywords (matched against portal doc type strings) ──
TARGET_KEYWORDS = {
    "LIS PENDENS":             ("LP",       "Lis Pendens"),
    "RELEASE LIS PENDENS":     ("RELLP",    "Release Lis Pendens"),
    "NOTICE OF FORECLOSURE":   ("NOFC",     "Notice of Foreclosure"),
    "FORECLOSURE":             ("NOFC",     "Notice of Foreclosure"),
    "TAX DEED":                ("TAXDEED",  "Tax Deed"),
    "ABSTRACT OF JUDGMENT":    ("JUD",      "Judgment"),
    "JUDGMENT":                ("JUD",      "Judgment"),
    "CERTIFIED JUDGMENT":      ("CCJ",      "Certified Judgment"),
    "DOMESTIC JUDGMENT":       ("DRJUD",    "Domestic Judgment"),
    "FEDERAL TAX LIEN":        ("LNFED",    "Federal Tax Lien"),
    "STATE TAX LIEN":          ("LNCORPTX", "State Tax Lien"),
    "IRS LIEN":                ("LNIRS",    "IRS Lien"),
    "FEDERAL LIEN":            ("LNFED",    "Federal Lien"),
    "MECHANIC":                ("LNMECH",   "Mechanic Lien"),
    "HOA LIEN":                ("LNHOA",    "HOA Lien"),
    "HOSPITAL LIEN":           ("MEDLN",    "Hospital/Medicaid Lien"),
    "MEDICAID":                ("MEDLN",    "Hospital/Medicaid Lien"),
    "LIEN":                    ("LN",       "Lien"),
    "PROBATE":                 ("PRO",      "Probate / Estate"),
    "NOTICE OF COMMENCEMENT":  ("NOC",      "Notice of Commencement"),
}

# Ordered so more-specific strings match first
ORDERED_KEYS = [
    "RELEASE LIS PENDENS", "LIS PENDENS",
    "NOTICE OF FORECLOSURE", "FORECLOSURE",
    "TAX DEED",
    "ABSTRACT OF JUDGMENT", "CERTIFIED JUDGMENT", "DOMESTIC JUDGMENT", "JUDGMENT",
    "FEDERAL TAX LIEN", "STATE TAX LIEN", "IRS LIEN", "FEDERAL LIEN",
    "HOA LIEN", "MECHANIC", "HOSPITAL LIEN", "MEDICAID", "LIEN",
    "PROBATE", "NOTICE OF COMMENCEMENT",
]

# Doc types to search for on the portal (these are real Dallas County doc type names)
SEARCH_DOC_TYPES = [
    "Lis Pendens",
    "Release of Lis Pendens",
    "Notice of Foreclosure",
    "Tax Deed",
    "Abstract of Judgment",
    "Certified Abstract of Judgment",
    "Federal Tax Lien",
    "State Tax Lien",
    "Mechanic Lien",
    "Hospital Lien",
    "Lien",
    "Probate",
    "Notice of Commencement",
]

# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────
def _retry(fn, *args, attempts=RETRY_LIMIT, delay=RETRY_DELAY, label="", **kwargs):
    for i in range(1, attempts + 1):
        try:
            return fn(*args, **kwargs)
        except Exception as exc:
            log.warning(f"[{label}] attempt {i}/{attempts} failed: {exc}")
            if i < attempts:
                time.sleep(delay)
    log.error(f"[{label}] all {attempts} attempts failed.")
    return None


def _classify(raw_type: str):
    upper = raw_type.upper()
    for key in ORDERED_KEYS:
        if key in upper:
            return TARGET_KEYWORDS[key]
    return None


def _parse_amount(val) -> float:
    if not val:
        return 0.0
    try:
        return float(re.sub(r"[^\d.]", "", str(val).replace(",", "")))
    except Exception:
        return 0.0


def _name_variants(name: str):
    parts = [p.strip() for p in re.split(r"[,\s]+", name) if p.strip()]
    v = {name.upper()}
    if len(parts) >= 2:
        v.add(f"{parts[0]} {parts[1]}".upper())
        v.add(f"{parts[1]} {parts[0]}".upper())
        v.add(f"{parts[1]}, {parts[0]}".upper())
    return v


# ─────────────────────────────────────────────────────────────────────────────
# API Scraper
# ─────────────────────────────────────────────────────────────────────────────
class DallasAPIScraper:
    """
    Calls the dallas.tx.publicsearch.us search API directly.
    The portal is powered by a REST JSON API — no browser needed.
    """

    HEADERS = {
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Origin": "https://dallas.tx.publicsearch.us",
        "Referer": "https://dallas.tx.publicsearch.us/",
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ),
    }

    def __init__(self, date_from: datetime, date_to: datetime):
        self.date_from = date_from
        self.date_to   = date_to
        self.session   = requests.Session()
        self.session.headers.update(self.HEADERS)

    def _search_page(self, doc_type: str, page: int = 0, size: int = 100) -> dict:
        """Call the search API for one document type, one page."""
        params = {
            "department":    "RP",          # Real Property
            "docTypes":      doc_type,
            "dateType":      "RecordedDate",
            "startDate":     self.date_from.strftime("%Y-%m-%d"),
            "endDate":       self.date_to.strftime("%Y-%m-%d"),
            "page":          page,
            "size":          size,
            "sort":          "desc",
        }
        resp = self.session.get(SEARCH_EP, params=params, timeout=30)
        resp.raise_for_status()
        return resp.json()

    def _fetch_doc_type(self, doc_type: str) -> list[dict]:
        """Fetch all pages for a given document type."""
        records = []
        page = 0
        while True:
            data = _retry(
                self._search_page,
                doc_type, page,
                label=f"search:{doc_type}:p{page}"
            )
            if not data:
                break
            hits = data.get("hits", data.get("results", data.get("content", [])))
            if not hits:
                break
            for hit in hits:
                rec = self._parse_hit(hit)
                if rec:
                    records.append(rec)
            # Check if more pages
            total   = data.get("totalElements", data.get("total", len(hits)))
            fetched = (page + 1) * 100
            if fetched >= total or len(hits) < 100:
                break
            page += 1
            time.sleep(0.5)
        return records

    def _parse_hit(self, hit: dict) -> Optional[dict]:
        """Parse a single API result into our record format."""
        try:
            raw_type = (
                hit.get("docType") or
                hit.get("documentType") or
                hit.get("type") or ""
            )
            classified = _classify(raw_type)
            if not classified:
                return None
            cat, cat_label = classified

            doc_num = (
                hit.get("documentNumber") or
                hit.get("docNumber") or
                hit.get("instrumentNumber") or
                hit.get("id") or ""
            )

            filed_raw = (
                hit.get("recordedDate") or
                hit.get("recordDate") or
                hit.get("filedDate") or
                hit.get("date") or ""
            )
            try:
                if "T" in str(filed_raw):
                    filed_iso = str(filed_raw)[:10]
                else:
                    filed_iso = str(filed_raw)[:10]
            except Exception:
                filed_iso = str(filed_raw)

            grantor = ""
            grantee = ""
            parties = hit.get("parties", hit.get("names", []))
            if isinstance(parties, list):
                grantors = [p.get("name","") for p in parties
                            if str(p.get("type","")).upper() in ("GRANTOR","SELLER","FROM","OWNER")]
                grantees = [p.get("name","") for p in parties
                            if str(p.get("type","")).upper() in ("GRANTEE","BUYER","TO")]
                grantor = "; ".join(filter(None, grantors))
                grantee = "; ".join(filter(None, grantees))
            if not grantor:
                grantor = hit.get("grantor", hit.get("grantorName", ""))
            if not grantee:
                grantee = hit.get("grantee", hit.get("granteeName", ""))

            amount   = _parse_amount(hit.get("amount") or hit.get("consideration") or 0)
            legal    = hit.get("legalDescription", hit.get("legal", ""))
            book     = hit.get("book", "")
            vol      = hit.get("volume", "")
            pg       = hit.get("page", "")

            # Build direct URL
            doc_id = hit.get("id") or hit.get("documentId") or doc_num
            clerk_url = f"{DOC_URL_BASE}/{doc_id}" if doc_id else DOC_URL_BASE

            return {
                "doc_num":      str(doc_num),
                "doc_type":     raw_type,
                "filed":        filed_iso,
                "cat":          cat,
                "cat_label":    cat_label,
                "owner":        grantor,
                "grantee":      grantee,
                "amount":       amount,
                "legal":        legal,
                "clerk_url":    clerk_url,
                "prop_address": "",
                "prop_city":    "Dallas",
                "prop_state":   "TX",
                "prop_zip":     "",
                "mail_address": "",
                "mail_city":    "",
                "mail_state":   "TX",
                "mail_zip":     "",
                "flags":        [],
                "score":        30,
            }
        except Exception as exc:
            log.debug(f"Hit parse error: {exc}")
            return None

    def run(self) -> list[dict]:
        all_records = []
        seen_docs   = set()
        for dt in SEARCH_DOC_TYPES:
            log.info(f"  Searching: {dt} ...")
            recs = self._fetch_doc_type(dt)
            for r in recs:
                key = r["doc_num"] or r["clerk_url"]
                if key not in seen_docs:
                    seen_docs.add(key)
                    all_records.append(r)
            log.info(f"  → {len(recs)} records for '{dt}'")
            time.sleep(1)
        log.info(f"Total unique records: {len(all_records)}")
        return all_records


# ─────────────────────────────────────────────────────────────────────────────
# Parcel Lookup (DCAD)
# ─────────────────────────────────────────────────────────────────────────────
class ParcelLookup:
    def __init__(self):
        self._index: dict[str, dict] = {}

    def _find_bulk_url(self) -> Optional[str]:
        for url in (DCAD_ALT_URL, DCAD_BULK_URL):
            try:
                resp = requests.get(url, timeout=30)
                if resp.ok and "zip" in resp.headers.get("Content-Type", ""):
                    return url
                from bs4 import BeautifulSoup
                soup = BeautifulSoup(resp.text, "lxml")
                for a in soup.find_all("a", href=True):
                    href = a["href"]
                    if href.endswith(".zip") and "apprais" in href.lower():
                        base = "https://www.dcad.org"
                        return href if href.startswith("http") else base + href
            except Exception as exc:
                log.warning(f"DCAD probe failed ({url}): {exc}")
        return None

    def _download_zip(self, url: str) -> Optional[bytes]:
        log.info(f"Downloading DCAD data from {url} ...")
        try:
            resp = requests.get(url, timeout=120, stream=True)
            resp.raise_for_status()
            return resp.content
        except Exception as exc:
            log.warning(f"DCAD download failed: {exc}")
            return None

    def _load_dbf(self, raw_zip: bytes):
        try:
            import zipfile as zf, io as _io
            z    = zf.ZipFile(_io.BytesIO(raw_zip))
            dbfs = [n for n in z.namelist() if n.lower().endswith(".dbf")]
            if not dbfs:
                return
            tmp = Path("/tmp/dcad.dbf")
            tmp.write_bytes(z.read(dbfs[0]))
            table = DBF(str(tmp), encoding="latin-1", ignore_missing_memofile=True)
            for row in table:
                self._index_row(dict(row))
            log.info(f"DCAD index: {len(self._index):,} variants")
        except Exception as exc:
            log.error(f"DBF error: {exc}")

    def _index_row(self, row: dict):
        owner = (row.get("OWNER") or row.get("OWN1") or "").strip()
        if not owner:
            return
        info = {
            "prop_address": (row.get("SITE_ADDR") or row.get("SITEADDR") or "").strip(),
            "prop_city":    (row.get("SITE_CITY") or row.get("SITECITY") or "").strip(),
            "prop_state":   "TX",
            "prop_zip":     str(row.get("SITE_ZIP") or row.get("SITEZIP") or "").strip(),
            "mail_address": (row.get("ADDR_1") or row.get("MAILADR1") or "").strip(),
            "mail_city":    (row.get("CITY") or row.get("MAILCITY") or "").strip(),
            "mail_state":   (row.get("STATE") or "TX").strip(),
            "mail_zip":     str(row.get("ZIP") or row.get("MAILZIP") or "").strip(),
        }
        for variant in _name_variants(owner):
            if variant not in self._index:
                self._index[variant] = info

    def load(self):
        if not HAS_DBF:
            log.warning("dbfread not installed — skipping parcel enrichment.")
            return
        url = _retry(self._find_bulk_url, label="DCAD-url")
        if not url:
            return
        raw = _retry(self._download_zip, url, label="DCAD-dl")
        if raw:
            self._load_dbf(raw)

    def lookup(self, owner: str) -> dict:
        for v in _name_variants(owner):
            if v in self._index:
                return self._index[v]
        return {}


# ─────────────────────────────────────────────────────────────────────────────
# Scoring
# ─────────────────────────────────────────────────────────────────────────────
def _score(record: dict, today: datetime, owner_cats: dict) -> tuple[int, list[str]]:
    score = 30
    flags = []
    cat   = record.get("cat", "")
    amt   = record.get("amount", 0.0)

    try:
        new_this_week = (today - datetime.strptime(record["filed"], "%Y-%m-%d")).days <= 7
    except Exception:
        new_this_week = False

    if cat in ("LP", "RELLP"):
        flags.append("Lis pendens"); score += 10
    if cat == "NOFC":
        flags.append("Pre-foreclosure"); score += 10
    owner_up = (record.get("owner") or "").upper()
    cats = owner_cats.get(owner_up, set())
    if "LP" in cats and "NOFC" in cats and "Lis pendens + Pre-foreclosure combo" not in flags:
        flags.append("Lis pendens + Pre-foreclosure combo"); score += 20
    if cat in ("JUD", "CCJ", "DRJUD"):
        flags.append("Judgment lien"); score += 10
    if cat in ("TAXDEED", "LNCORPTX", "LNIRS", "LNFED"):
        flags.append("Tax lien"); score += 10
    if cat == "LNMECH":
        flags.append("Mechanic lien"); score += 10
    if cat in ("LNHOA", "LN", "MEDLN"):
        flags.append("Lien"); score += 10
    if cat == "PRO":
        flags.append("Probate / estate"); score += 10
    if amt > 100_000:
        flags.append("High debt (>$100k)"); score += 15
    elif amt > 50_000:
        flags.append("Significant debt (>$50k)"); score += 10
    if new_this_week:
        flags.append("New this week"); score += 5
    if record.get("prop_address"):
        flags.append("Has property address"); score += 5
    if any(kw in owner_up for kw in ("LLC","INC","CORP","LTD","TRUST","ESTATE")):
        flags.append("LLC / corp owner"); score += 10

    return min(score, 100), list(dict.fromkeys(flags))


# ─────────────────────────────────────────────────────────────────────────────
# GHL CSV
# ─────────────────────────────────────────────────────────────────────────────
GHL_COLS = [
    "First Name","Last Name","Mailing Address","Mailing City","Mailing State","Mailing Zip",
    "Property Address","Property City","Property State","Property Zip",
    "Lead Type","Document Type","Date Filed","Document Number",
    "Amount/Debt Owed","Seller Score","Motivated Seller Flags","Source","Public Records URL",
]

def _split_name(n):
    if not n: return "", ""
    if "," in n:
        p = n.split(",", 1); return p[1].strip(), p[0].strip()
    p = n.split()
    return (" ".join(p[:-1]), p[-1]) if len(p) > 1 else ("", p[0])

def write_ghl_csv(records, path):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=GHL_COLS)
        w.writeheader()
        for r in records:
            fn, ln = _split_name(r.get("owner",""))
            w.writerow({
                "First Name": fn, "Last Name": ln,
                "Mailing Address": r.get("mail_address",""),
                "Mailing City": r.get("mail_city",""),
                "Mailing State": r.get("mail_state","TX"),
                "Mailing Zip": r.get("mail_zip",""),
                "Property Address": r.get("prop_address",""),
                "Property City": r.get("prop_city","Dallas"),
                "Property State": r.get("prop_state","TX"),
                "Property Zip": r.get("prop_zip",""),
                "Lead Type": r.get("cat_label",""),
                "Document Type": r.get("doc_type",""),
                "Date Filed": r.get("filed",""),
                "Document Number": r.get("doc_num",""),
                "Amount/Debt Owed": r.get("amount",""),
                "Seller Score": r.get("score",0),
                "Motivated Seller Flags": "; ".join(r.get("flags",[])),
                "Source": "Dallas County Clerk",
                "Public Records URL": r.get("clerk_url",""),
            })
    log.info(f"GHL CSV → {path}")


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────
def main():
    today    = datetime.utcnow()
    week_ago = today - timedelta(days=7)
    log.info(f"Dallas County Scraper | {week_ago.date()} → {today.date()}")

    # 1. Parcel lookup
    parcel = ParcelLookup()
    parcel.load()

    # 2. Scrape via API
    scraper = DallasAPIScraper(date_from=week_ago, date_to=today)
    records = scraper.run()

    # 3. Enrich + score
    owner_cats: dict[str, set] = {}
    for r in records:
        owner_cats.setdefault((r.get("owner") or "").upper(), set()).add(r["cat"])

    enriched = []
    for r in records:
        pi = parcel.lookup(r.get("owner",""))
        r.update({
            "prop_address": pi.get("prop_address") or r["prop_address"],
            "prop_city":    pi.get("prop_city")    or r["prop_city"],
            "prop_state":   pi.get("prop_state")   or "TX",
            "prop_zip":     pi.get("prop_zip")     or r["prop_zip"],
            "mail_address": pi.get("mail_address") or r["mail_address"],
            "mail_city":    pi.get("mail_city")    or r["mail_city"],
            "mail_state":   pi.get("mail_state")   or "TX",
            "mail_zip":     pi.get("mail_zip")     or r["mail_zip"],
        })
        sc, fl = _score(r, today, owner_cats)
        r["score"] = sc; r["flags"] = fl
        enriched.append(r)

    enriched.sort(key=lambda x: x["score"], reverse=True)
    with_address = sum(1 for r in enriched if r.get("prop_address"))

    # 4. Write outputs
    payload = {
        "fetched_at":   today.isoformat() + "Z",
        "source":       "Dallas County Clerk – dallas.tx.publicsearch.us",
        "date_range":   f"{week_ago.date()} to {today.date()}",
        "total":        len(enriched),
        "with_address": with_address,
        "records":      enriched,
    }
    for d in (DASHBOARD_DIR, DATA_DIR):
        p = d / "records.json"
        p.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
        log.info(f"JSON → {p}")

    csv_path = DATA_DIR / f"ghl_export_{today.strftime('%Y%m%d')}.csv"
    write_ghl_csv(enriched, csv_path)

    log.info(
        f"\n{'='*55}\n"
        f"  Done. {len(enriched)} records | {with_address} with address\n"
        f"  Top score: {enriched[0]['score'] if enriched else 'N/A'}\n"
        f"{'='*55}"
    )

if __name__ == "__main__":
    main()
