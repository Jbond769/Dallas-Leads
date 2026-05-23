#!/usr/bin/env python3
"""
Dallas County, TX — Motivated Seller Lead Scraper
Scrapes the County Clerk portal for the last 7 days of filings,
enriches with parcel data, scores each lead, and writes JSON + CSV output.
"""

import asyncio
import csv
import io
import json
import logging
import os
import re
import sys
import time
import zipfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import requests
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright, TimeoutError as PWTimeout

# ── optional dbfread ──────────────────────────────────────────────────────────
try:
    from dbfread import DBF
    HAS_DBF = True
except ImportError:
    HAS_DBF = False

# ─────────────────────────────────────────────────────────────────────────────
# Config / constants
# ─────────────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("dallas_scraper")

BASE_DIR        = Path(__file__).resolve().parent.parent
DASHBOARD_DIR   = BASE_DIR / "dashboard"
DATA_DIR        = BASE_DIR / "data"
DASHBOARD_DIR.mkdir(parents=True, exist_ok=True)
DATA_DIR.mkdir(parents=True, exist_ok=True)

CLERK_BASE      = "https://www.dallascounty.org/government/county-clerk/"
CLERK_SEARCH    = "https://ccting.dallascounty.org/CCRecordSearch/search"  # main search endpoint
CLERK_RESULTS   = "https://ccting.dallascounty.org/CCRecordSearch/results"

# Dallas CAD bulk data — DCAD publishes a zipped DBF/shapefile set
DCAD_BULK_URL   = "https://www.dcad.org/wp-content/uploads/data/appraisal_data.zip"
DCAD_ALT_URL    = "https://www.dcad.org/data/"  # fallback — parse HTML for link

RETRY_LIMIT     = 3
RETRY_DELAY     = 4  # seconds between retries

# Document-type categories we care about
DOC_TYPE_MAP = {
    # (substring_or_exact → (cat_code, cat_label))
    "LIS PENDENS":              ("LP",      "Lis Pendens"),
    "NOTICE OF FORECLOSURE":    ("NOFC",    "Notice of Foreclosure"),
    "TAX DEED":                 ("TAXDEED", "Tax Deed"),
    "JUDGMENT":                 ("JUD",     "Judgment"),
    "CERTIFIED JUDGMENT":       ("CCJ",     "Certified Judgment"),
    "DOMESTIC JUDGMENT":        ("DRJUD",   "Domestic Judgment"),
    "CORP TAX LIEN":            ("LNCORPTX","Corp Tax Lien"),
    "IRS LIEN":                 ("LNIRS",   "IRS Lien"),
    "FEDERAL LIEN":             ("LNFED",   "Federal Lien"),
    "MECHANIC LIEN":            ("LNMECH",  "Mechanic Lien"),
    "HOA LIEN":                 ("LNHOA",   "HOA Lien"),
    "MEDICAID LIEN":            ("MEDLN",   "Medicaid Lien"),
    "LIEN":                     ("LN",      "Lien"),
    "PROBATE":                  ("PRO",     "Probate / Estate"),
    "NOTICE OF COMMENCEMENT":   ("NOC",     "Notice of Commencement"),
    "RELEASE LIS PENDENS":      ("RELLP",   "Release Lis Pendens"),
}

# Ordered so longer / more-specific strings match first
ORDERED_DOC_KEYS = [
    "RELEASE LIS PENDENS",
    "LIS PENDENS",
    "NOTICE OF FORECLOSURE",
    "TAX DEED",
    "CERTIFIED JUDGMENT",
    "DOMESTIC JUDGMENT",
    "CORP TAX LIEN",
    "IRS LIEN",
    "FEDERAL LIEN",
    "MECHANIC LIEN",
    "HOA LIEN",
    "MEDICAID LIEN",
    "LIEN",
    "JUDGMENT",
    "PROBATE",
    "NOTICE OF COMMENCEMENT",
]


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────
def _retry(fn, *args, attempts=RETRY_LIMIT, delay=RETRY_DELAY, label="call", **kwargs):
    """Call fn(*args, **kwargs) up to `attempts` times; return result or None."""
    for attempt in range(1, attempts + 1):
        try:
            return fn(*args, **kwargs)
        except Exception as exc:
            log.warning(f"[{label}] attempt {attempt}/{attempts} failed: {exc}")
            if attempt < attempts:
                time.sleep(delay)
    log.error(f"[{label}] all {attempts} attempts failed — skipping.")
    return None


def _parse_amount(text: str) -> float:
    """Extract a dollar amount from a raw string, return 0.0 if none found."""
    if not text:
        return 0.0
    clean = re.sub(r"[^\d.]", "", text.replace(",", ""))
    try:
        return float(clean)
    except ValueError:
        return 0.0


def _classify_doc_type(raw_type: str):
    """Return (cat, cat_label) for a raw document-type string."""
    upper = raw_type.upper()
    for key in ORDERED_DOC_KEYS:
        if key in upper:
            return DOC_TYPE_MAP[key]
    return ("OTHER", raw_type)


def _name_variants(full_name: str):
    """Return a set of name variants for fuzzy owner lookup."""
    parts = [p.strip() for p in re.split(r"[,\s]+", full_name) if p.strip()]
    variants = {full_name.upper()}
    if len(parts) >= 2:
        variants.add(f"{parts[0]} {parts[1]}".upper())
        variants.add(f"{parts[1]} {parts[0]}".upper())
        variants.add(f"{parts[1]}, {parts[0]}".upper())
    return variants


def _score_lead(record: dict, today: datetime) -> tuple[int, list[str]]:
    """Compute seller score (0-100) and flag list."""
    score = 30
    flags = []
    cat   = record.get("cat", "")
    amt   = record.get("amount", 0.0)

    filed_str = record.get("filed", "")
    try:
        filed_dt = datetime.strptime(filed_str, "%Y-%m-%d")
        new_this_week = (today - filed_dt).days <= 7
    except Exception:
        new_this_week = False

    # LP flag
    if cat in ("LP", "RELLP"):
        flags.append("Lis pendens")
        score += 10
    # Pre-foreclosure
    if cat == "NOFC":
        flags.append("Pre-foreclosure")
        score += 10
    # LP + Foreclosure combo bonus
    if cat in ("LP", "NOFC") and any(
        r.get("owner") == record.get("owner") and r.get("cat") in ("LP", "NOFC")
        for r in []   # placeholder — full combo check done post-collection
    ):
        score += 20
    # Judgment
    if cat in ("JUD", "CCJ", "DRJUD"):
        flags.append("Judgment lien")
        score += 10
    # Tax lien
    if cat in ("TAXDEED", "LNCORPTX", "LNIRS", "LNFED"):
        flags.append("Tax lien")
        score += 10
    # Mechanic lien
    if cat == "LNMECH":
        flags.append("Mechanic lien")
        score += 10
    # HOA / general lien
    if cat in ("LNHOA", "LN", "MEDLN"):
        flags.append("Mechanic lien" if cat == "LNMECH" else "Lien")
        score += 10
    # Probate
    if cat == "PRO":
        flags.append("Probate / estate")
        score += 10
    # Amount bonuses
    if amt > 100_000:
        flags.append("High debt (>$100k)")
        score += 15
    elif amt > 50_000:
        flags.append("Significant debt (>$50k)")
        score += 10
    # New this week
    if new_this_week:
        flags.append("New this week")
        score += 5
    # Has address
    if record.get("prop_address"):
        flags.append("Has property address")
        score += 5
    # LLC / Corp owner
    owner_up = (record.get("owner") or "").upper()
    if any(kw in owner_up for kw in ("LLC", "INC", "CORP", "LTD", "TRUST", "ESTATE")):
        flags.append("LLC / corp owner")
        score += 10

    return min(score, 100), list(dict.fromkeys(flags))


# ─────────────────────────────────────────────────────────────────────────────
# DCAD Parcel Lookup
# ─────────────────────────────────────────────────────────────────────────────
class ParcelLookup:
    """Downloads the DCAD bulk DBF, builds an owner-name index."""

    def __init__(self):
        self._index: dict[str, dict] = {}   # OWNER_VARIANT_UPPER → parcel row

    # -- download helpers --------------------------------------------------
    def _find_bulk_url(self) -> Optional[str]:
        """Try to discover the current bulk-data download URL from DCAD's data page."""
        for url in (DCAD_ALT_URL, DCAD_BULK_URL):
            try:
                resp = requests.get(url, timeout=30)
                if resp.ok and "zip" in resp.headers.get("Content-Type", ""):
                    return url
                soup = BeautifulSoup(resp.text, "lxml")
                for a in soup.find_all("a", href=True):
                    href = a["href"]
                    if href.endswith(".zip") and "apprais" in href.lower():
                        base = "https://www.dcad.org"
                        return href if href.startswith("http") else base + href
            except Exception as exc:
                log.warning(f"DCAD URL probe failed ({url}): {exc}")
        return None

    def _download_zip(self, url: str) -> Optional[bytes]:
        log.info(f"Downloading DCAD bulk data from {url} …")
        try:
            resp = requests.get(url, timeout=120, stream=True)
            resp.raise_for_status()
            return resp.content
        except Exception as exc:
            log.warning(f"DCAD download failed: {exc}")
            return None

    def _load_dbf(self, raw_zip: bytes):
        """Extract and parse the first .dbf found inside the zip."""
        try:
            zf = zipfile.ZipFile(io.BytesIO(raw_zip))
            dbf_names = [n for n in zf.namelist() if n.lower().endswith(".dbf")]
            if not dbf_names:
                log.warning("No .dbf found inside DCAD zip.")
                return
            dbf_bytes = zf.read(dbf_names[0])
            # Write temp file because dbfread needs a path
            tmp = Path("/tmp/dcad_parcel.dbf")
            tmp.write_bytes(dbf_bytes)
            table = DBF(str(tmp), encoding="latin-1", ignore_missing_memofile=True)
            for row in table:
                self._index_row(dict(row))
            log.info(f"DCAD index built: {len(self._index):,} owner name variants")
        except Exception as exc:
            log.error(f"DBF parse error: {exc}")

    def _index_row(self, row: dict):
        """Index a parcel row by every owner-name variant."""
        owner = (
            row.get("OWNER") or row.get("OWN1") or row.get("OWNER_NAME") or ""
        ).strip()
        if not owner:
            return
        parcel_info = {
            "prop_address": " ".join(filter(None, [
                row.get("SITE_ADDR") or row.get("SITEADDR") or row.get("SITE_ADDRESS") or "",
            ])).strip(),
            "prop_city":    (row.get("SITE_CITY") or row.get("SITECITY") or "").strip(),
            "prop_state":   "TX",
            "prop_zip":     str(row.get("SITE_ZIP") or row.get("SITEZIP") or "").strip(),
            "mail_address": (row.get("ADDR_1") or row.get("MAILADR1") or row.get("MAIL_ADDR") or "").strip(),
            "mail_city":    (row.get("CITY") or row.get("MAILCITY") or "").strip(),
            "mail_state":   (row.get("STATE") or row.get("MAILSTATE") or "TX").strip(),
            "mail_zip":     str(row.get("ZIP") or row.get("MAILZIP") or "").strip(),
        }
        for variant in _name_variants(owner):
            if variant not in self._index:
                self._index[variant] = parcel_info

    # -- public API --------------------------------------------------------
    def load(self):
        if not HAS_DBF:
            log.warning("dbfread not installed — parcel enrichment skipped.")
            return
        url = _retry(self._find_bulk_url, label="DCAD-URL-find")
        if not url:
            log.warning("Could not locate DCAD bulk data URL.")
            return
        raw_zip = _retry(self._download_zip, url, label="DCAD-download")
        if raw_zip:
            self._load_dbf(raw_zip)

    def lookup(self, owner_name: str) -> dict:
        """Return parcel address dict or empty dict if not found."""
        for variant in _name_variants(owner_name):
            if variant in self._index:
                return self._index[variant]
        return {}


# ─────────────────────────────────────────────────────────────────────────────
# Clerk Portal Scraper  (Playwright async)
# ─────────────────────────────────────────────────────────────────────────────
class ClerkScraper:
    """
    Scrapes the Dallas County Clerk recording index.
    The portal at ccting.dallascounty.org uses ASP.NET __doPostBack forms.
    """

    def __init__(self, date_from: datetime, date_to: datetime):
        self.date_from = date_from
        self.date_to   = date_to
        self.records: list[dict] = []

    # -- search page interaction ------------------------------------------
    async def _fill_and_submit(self, page, doc_type_filter: str = ""):
        """Fill the search form and submit."""
        from_str = self.date_from.strftime("%m/%d/%Y")
        to_str   = self.date_to.strftime("%m/%d/%Y")

        # Wait for form to be ready
        await page.wait_for_selector("input[name*='DateFrom'], input[id*='DateFrom']",
                                      timeout=15_000)

        # Clear & set date fields (try common field patterns)
        for sel in ["DateFrom", "txtDateFrom", "BeginDate"]:
            try:
                await page.fill(f"[name='{sel}']", from_str)
                break
            except Exception:
                pass
        for sel in ["DateTo", "txtDateTo", "EndDate"]:
            try:
                await page.fill(f"[name='{sel}']", to_str)
                break
            except Exception:
                pass

        # Document type dropdown if we have a filter
        if doc_type_filter:
            try:
                await page.select_option(
                    "select[name*='DocType'], select[id*='DocType']",
                    label=doc_type_filter,
                )
            except Exception:
                pass  # leave as "All" if dropdown not found

        # Submit
        try:
            await page.click("input[type='submit'], button[type='submit']")
        except Exception:
            # Fallback: trigger __doPostBack manually
            await page.evaluate("__doPostBack('btnSearch','')")

        await page.wait_for_load_state("networkidle", timeout=30_000)

    # -- parse a results page ---------------------------------------------
    async def _parse_results_page(self, page) -> list[dict]:
        """Extract records from the current results table."""
        content = await page.content()
        soup    = BeautifulSoup(content, "lxml")
        rows    = []

        # The results grid is typically a table with class GridView or similar
        table = (
            soup.find("table", {"class": re.compile(r"grid|result|record", re.I)})
            or soup.find("table", id=re.compile(r"grid|result|record", re.I))
            or soup.find("table")  # fallback to first table
        )
        if not table:
            return rows

        headers = [th.get_text(strip=True).upper()
                   for th in table.find_all("th")]

        def _col(cells, *names):
            for name in names:
                for i, h in enumerate(headers):
                    if name in h and i < len(cells):
                        return cells[i].get_text(strip=True)
            return ""

        current_url = page.url

        for tr in table.find_all("tr")[1:]:
            cells = tr.find_all(["td", "th"])
            if len(cells) < 3:
                continue
            try:
                raw_type = _col(cells,
                                "DOC TYPE", "DOCUMENT TYPE", "TYPE", "INSTRUMENT")
                if not raw_type:
                    continue
                cat, cat_label = _classify_doc_type(raw_type)
                if cat == "OTHER":
                    continue  # not a target document type

                # Try to find a direct link
                link_tag = tr.find("a", href=True)
                if link_tag:
                    href = link_tag["href"]
                    clerk_url = (href if href.startswith("http")
                                 else f"https://ccting.dallascounty.org{href}")
                else:
                    clerk_url = current_url

                filed_raw  = _col(cells, "DATE", "FILED", "RECORD DATE", "INST DATE")
                try:
                    filed_dt  = datetime.strptime(filed_raw, "%m/%d/%Y")
                    filed_iso = filed_dt.strftime("%Y-%m-%d")
                except Exception:
                    filed_iso = filed_raw

                amount_raw = _col(cells, "AMOUNT", "CONSID", "CONSIDERATION", "PRICE")
                doc_num    = _col(cells, "DOC #", "DOC NUM", "INSTRUMENT #",
                                  "INSTRUMENT NUMBER", "DOCUMENT")
                grantor    = _col(cells, "GRANTOR", "SELLER", "OWNER", "FROM")
                grantee    = _col(cells, "GRANTEE", "BUYER", "TO")
                legal      = _col(cells, "LEGAL", "DESCRIPTION")

                rows.append({
                    "doc_num":   doc_num,
                    "doc_type":  raw_type,
                    "filed":     filed_iso,
                    "cat":       cat,
                    "cat_label": cat_label,
                    "owner":     grantor,
                    "grantee":   grantee,
                    "amount":    _parse_amount(amount_raw),
                    "legal":     legal,
                    "clerk_url": clerk_url,
                    # address fields filled later by parcel lookup
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
                })
            except Exception as exc:
                log.debug(f"Row parse error (skipping): {exc}")
                continue

        return rows

    # -- paginate ---------------------------------------------------------
    async def _collect_all_pages(self, page) -> list[dict]:
        """Follow 'Next' pagination until exhausted."""
        all_records = []
        page_num    = 1
        while True:
            log.info(f"  Parsing results page {page_num} …")
            recs = await self._parse_results_page(page)
            all_records.extend(recs)
            log.info(f"  Found {len(recs)} records on page {page_num}")

            # Look for Next button / link
            next_btn = await page.query_selector(
                "a:has-text('Next'), input[value='Next'], "
                "[id*='Next']:not([disabled]), [class*='next']:not([disabled])"
            )
            if not next_btn:
                break
            try:
                await next_btn.click()
                await page.wait_for_load_state("networkidle", timeout=20_000)
                page_num += 1
            except Exception as exc:
                log.warning(f"Pagination stopped: {exc}")
                break

        return all_records

    # -- main async entry point ------------------------------------------
    async def run(self) -> list[dict]:
        log.info("Launching Playwright browser …")
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            context = await browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
                )
            )
            page = await context.new_page()

            # Navigate to the clerk recording search
            search_urls = [
                "https://ccting.dallascounty.org/CCRecordSearch/",
                "https://www.dallascounty.org/government/county-clerk/",
            ]
            landed = False
            for url in search_urls:
                try:
                    await page.goto(url, wait_until="networkidle", timeout=45_000)
                    landed = True
                    log.info(f"Landed on: {page.url}")
                    break
                except PWTimeout:
                    log.warning(f"Timeout loading {url}")

            if not landed:
                log.error("Could not reach clerk portal.")
                await browser.close()
                return []

            # Attempt to search without doc-type filter (all target types at once)
            try:
                await self._fill_and_submit(page)
                self.records = await self._collect_all_pages(page)
            except Exception as exc:
                log.error(f"Search/parse failed: {exc}")

            await browser.close()

        # Filter to only our target categories
        self.records = [
            r for r in self.records
            if r["cat"] != "OTHER"
        ]
        log.info(f"Total raw records collected: {len(self.records)}")
        return self.records


# ─────────────────────────────────────────────────────────────────────────────
# LP + Foreclosure combo bonus (post-collection)
# ─────────────────────────────────────────────────────────────────────────────
def _apply_combo_bonus(records: list[dict]) -> list[dict]:
    """Add +20 if the same owner has both LP and NOFC."""
    owner_cats: dict[str, set] = {}
    for r in records:
        owner = (r.get("owner") or "").upper()
        if owner:
            owner_cats.setdefault(owner, set()).add(r["cat"])

    for r in records:
        owner = (r.get("owner") or "").upper()
        cats  = owner_cats.get(owner, set())
        if "LP" in cats and "NOFC" in cats:
            if "Lis pendens + Pre-foreclosure combo" not in r["flags"]:
                r["flags"].append("Lis pendens + Pre-foreclosure combo")
                r["score"] = min(r["score"] + 20, 100)
    return records


# ─────────────────────────────────────────────────────────────────────────────
# GHL CSV export
# ─────────────────────────────────────────────────────────────────────────────
GHL_COLUMNS = [
    "First Name", "Last Name",
    "Mailing Address", "Mailing City", "Mailing State", "Mailing Zip",
    "Property Address", "Property City", "Property State", "Property Zip",
    "Lead Type", "Document Type", "Date Filed", "Document Number",
    "Amount/Debt Owed", "Seller Score", "Motivated Seller Flags",
    "Source", "Public Records URL",
]


def _split_name(full_name: str) -> tuple[str, str]:
    """Best-effort split into (first, last)."""
    if not full_name:
        return "", ""
    # Handle "LAST, FIRST"
    if "," in full_name:
        parts = [p.strip() for p in full_name.split(",", 1)]
        return parts[1], parts[0]
    parts = full_name.split()
    if len(parts) == 1:
        return "", parts[0]
    return " ".join(parts[:-1]), parts[-1]


def write_ghl_csv(records: list[dict], output_path: Path):
    with open(output_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=GHL_COLUMNS)
        writer.writeheader()
        for r in records:
            first, last = _split_name(r.get("owner", ""))
            writer.writerow({
                "First Name":            first,
                "Last Name":             last,
                "Mailing Address":       r.get("mail_address", ""),
                "Mailing City":          r.get("mail_city", ""),
                "Mailing State":         r.get("mail_state", "TX"),
                "Mailing Zip":           r.get("mail_zip", ""),
                "Property Address":      r.get("prop_address", ""),
                "Property City":         r.get("prop_city", "Dallas"),
                "Property State":        r.get("prop_state", "TX"),
                "Property Zip":          r.get("prop_zip", ""),
                "Lead Type":             r.get("cat_label", ""),
                "Document Type":         r.get("doc_type", ""),
                "Date Filed":            r.get("filed", ""),
                "Document Number":       r.get("doc_num", ""),
                "Amount/Debt Owed":      r.get("amount", ""),
                "Seller Score":          r.get("score", 0),
                "Motivated Seller Flags": "; ".join(r.get("flags", [])),
                "Source":                "Dallas County Clerk",
                "Public Records URL":    r.get("clerk_url", ""),
            })
    log.info(f"GHL CSV written → {output_path}")


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────
async def main():
    today    = datetime.utcnow()
    week_ago = today - timedelta(days=7)
    log.info(
        f"Dallas County Motivated Seller Scraper | "
        f"{week_ago.date()} → {today.date()}"
    )

    # 1. Build parcel lookup
    parcel = ParcelLookup()
    parcel.load()

    # 2. Scrape clerk portal
    scraper = ClerkScraper(date_from=week_ago, date_to=today)
    records = await scraper.run()

    # 3. Enrich with parcel data + score
    log.info("Enriching records with parcel data and scoring …")
    enriched = []
    for r in records:
        owner = r.get("owner", "")
        parcel_info = parcel.lookup(owner) if owner else {}
        r.update({
            "prop_address": parcel_info.get("prop_address") or r.get("prop_address", ""),
            "prop_city":    parcel_info.get("prop_city")    or r.get("prop_city",    "Dallas"),
            "prop_state":   parcel_info.get("prop_state")   or "TX",
            "prop_zip":     parcel_info.get("prop_zip")     or r.get("prop_zip",    ""),
            "mail_address": parcel_info.get("mail_address") or r.get("mail_address", ""),
            "mail_city":    parcel_info.get("mail_city")    or r.get("mail_city",   ""),
            "mail_state":   parcel_info.get("mail_state")   or "TX",
            "mail_zip":     parcel_info.get("mail_zip")     or r.get("mail_zip",    ""),
        })
        score, flags = _score_lead(r, today)
        r["score"] = score
        r["flags"] = flags
        enriched.append(r)

    # 4. LP+FC combo bonus
    enriched = _apply_combo_bonus(enriched)

    # Sort by score desc
    enriched.sort(key=lambda x: x["score"], reverse=True)

    with_address = sum(1 for r in enriched if r.get("prop_address"))

    # 5. Build output payload
    payload = {
        "fetched_at":   today.isoformat() + "Z",
        "source":       "Dallas County Clerk – ccting.dallascounty.org",
        "date_range":   f"{week_ago.date()} to {today.date()}",
        "total":        len(enriched),
        "with_address": with_address,
        "records":      enriched,
    }

    # 6. Write JSON
    for out_dir in (DASHBOARD_DIR, DATA_DIR):
        path = out_dir / "records.json"
        path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
        log.info(f"JSON written → {path}")

    # 7. Write GHL CSV
    csv_path = DATA_DIR / f"ghl_export_{today.strftime('%Y%m%d')}.csv"
    write_ghl_csv(enriched, csv_path)

    log.info(
        f"\n{'='*60}\n"
        f"  Done. {len(enriched)} records | {with_address} with address\n"
        f"  Top score: {enriched[0]['score'] if enriched else 'N/A'}\n"
        f"{'='*60}"
    )


if __name__ == "__main__":
    asyncio.run(main())
