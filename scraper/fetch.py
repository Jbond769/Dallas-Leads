#!/usr/bin/env python3
"""
Dallas County, TX — Motivated Seller Lead Scraper v3
Uses Playwright to properly navigate the Neumo-powered portal
at dallas.tx.publicsearch.us, selecting doc types and date ranges
through the Advanced Search UI.
"""

import asyncio
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

try:
    from dbfread import DBF
    import zipfile, io as _io
    HAS_DBF = True
except ImportError:
    HAS_DBF = False

from playwright.async_api import async_playwright, TimeoutError as PWTimeout

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

PORTAL_URL    = "https://dallas.tx.publicsearch.us/search/advanced"
DCAD_BULK_URL = "https://www.dcad.org/wp-content/uploads/data/appraisal_data.zip"
DCAD_ALT_URL  = "https://www.dcad.org/data/"
RETRY_LIMIT   = 3
RETRY_DELAY   = 5

# Target doc type keywords to look for in results
ORDERED_KEYS = [
    "RELEASE LIS PENDENS", "LIS PENDENS",
    "NOTICE OF FORECLOSURE", "FORECLOSURE",
    "TAX DEED",
    "ABSTRACT OF JUDGMENT", "CERTIFIED JUDGMENT", "DOMESTIC JUDGMENT", "JUDGMENT",
    "FEDERAL TAX LIEN", "STATE TAX LIEN", "IRS LIEN", "FEDERAL LIEN",
    "HOA LIEN", "MECHANIC", "HOSPITAL LIEN", "MEDICAID", "LIEN",
    "PROBATE", "NOTICE OF COMMENCEMENT",
]
TARGET_KEYWORDS = {
    "RELEASE LIS PENDENS":     ("RELLP",    "Release Lis Pendens"),
    "LIS PENDENS":             ("LP",       "Lis Pendens"),
    "NOTICE OF FORECLOSURE":   ("NOFC",     "Notice of Foreclosure"),
    "FORECLOSURE":             ("NOFC",     "Notice of Foreclosure"),
    "TAX DEED":                ("TAXDEED",  "Tax Deed"),
    "ABSTRACT OF JUDGMENT":    ("JUD",      "Judgment"),
    "CERTIFIED JUDGMENT":      ("CCJ",      "Certified Judgment"),
    "DOMESTIC JUDGMENT":       ("DRJUD",    "Domestic Judgment"),
    "JUDGMENT":                ("JUD",      "Judgment"),
    "FEDERAL TAX LIEN":        ("LNFED",    "Federal Tax Lien"),
    "STATE TAX LIEN":          ("LNCORPTX", "State Tax Lien"),
    "IRS LIEN":                ("LNIRS",    "IRS Lien"),
    "FEDERAL LIEN":            ("LNFED",    "Federal Lien"),
    "HOA LIEN":                ("LNHOA",    "HOA Lien"),
    "MECHANIC":                ("LNMECH",   "Mechanic Lien"),
    "HOSPITAL LIEN":           ("MEDLN",    "Hospital Lien"),
    "MEDICAID":                ("MEDLN",    "Medicaid Lien"),
    "LIEN":                    ("LN",       "Lien"),
    "PROBATE":                 ("PRO",      "Probate / Estate"),
    "NOTICE OF COMMENCEMENT":  ("NOC",      "Notice of Commencement"),
}

def _classify(raw: str):
    u = raw.upper()
    for k in ORDERED_KEYS:
        if k in u:
            return TARGET_KEYWORDS[k]
    return None

def _parse_amount(v) -> float:
    try:
        return float(re.sub(r"[^\d.]", "", str(v).replace(",", "")))
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
# Playwright scraper
# ─────────────────────────────────────────────────────────────────────────────
class DallasPlaywrightScraper:
    def __init__(self, date_from: datetime, date_to: datetime):
        self.date_from = date_from
        self.date_to   = date_to
        self.records   = []

    async def _wait_for_results(self, page):
        """Wait for the results table/list to appear."""
        await page.wait_for_load_state("networkidle", timeout=30_000)
        await asyncio.sleep(2)

    async def _parse_results_page(self, page) -> list[dict]:
        """Extract records from the current results page."""
        records = []
        content = await page.content()

        # Try to extract JSON data embedded in page scripts
        json_matches = re.findall(
            r'"hits"\s*:\s*(\[[\s\S]*?\])\s*[,}]',
            content
        )
        for match in json_matches:
            try:
                hits = json.loads(match)
                for hit in hits:
                    rec = self._parse_hit(hit)
                    if rec:
                        records.append(rec)
                if records:
                    return records
            except Exception:
                pass

        # Fallback: parse the HTML table
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(content, "lxml")

        # Look for result rows
        rows = soup.select("tr[class*='result'], tr[class*='row'], .result-item, .document-row")
        if not rows:
            # Try any table rows
            table = soup.find("table")
            if table:
                rows = table.find_all("tr")[1:]  # skip header

        for row in rows:
            try:
                cells = row.find_all(["td", "th"])
                if len(cells) < 2:
                    continue
                text = [c.get_text(strip=True) for c in cells]

                # Try to find doc type in cells
                raw_type = ""
                for t in text:
                    if _classify(t):
                        raw_type = t
                        break
                if not raw_type:
                    raw_type = text[1] if len(text) > 1 else text[0]

                classified = _classify(raw_type)
                if not classified:
                    continue
                cat, cat_label = classified

                # Find link
                link = row.find("a", href=True)
                clerk_url = ""
                if link:
                    href = link["href"]
                    clerk_url = href if href.startswith("http") else f"https://dallas.tx.publicsearch.us{href}"

                # Try to extract date, doc num, grantor, amount
                filed_iso = ""
                doc_num   = ""
                grantor   = ""
                amount    = 0.0

                for t in text:
                    if re.match(r"\d{1,2}/\d{1,2}/\d{4}", t):
                        try:
                            filed_iso = datetime.strptime(t, "%m/%d/%Y").strftime("%Y-%m-%d")
                        except Exception:
                            pass
                    if re.match(r"\d{4}-\d+", t) or re.match(r"\d{6,}", t):
                        doc_num = t
                    if re.match(r"\$[\d,]+", t):
                        amount = _parse_amount(t.replace("$", ""))

                if len(text) > 2:
                    grantor = text[2]

                records.append({
                    "doc_num":      doc_num,
                    "doc_type":     raw_type,
                    "filed":        filed_iso,
                    "cat":          cat,
                    "cat_label":    cat_label,
                    "owner":        grantor,
                    "grantee":      text[3] if len(text) > 3 else "",
                    "amount":       amount,
                    "legal":        "",
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
                })
            except Exception as exc:
                log.debug(f"Row parse error: {exc}")
                continue

        return records

    def _parse_hit(self, hit: dict) -> Optional[dict]:
        try:
            raw_type = (hit.get("docType") or hit.get("documentType") or hit.get("type") or "")
            classified = _classify(raw_type)
            if not classified:
                return None
            cat, cat_label = classified

            doc_num   = str(hit.get("documentNumber") or hit.get("docNumber") or hit.get("id") or "")
            filed_raw = hit.get("recordedDate") or hit.get("recordDate") or ""
            filed_iso = str(filed_raw)[:10] if filed_raw else ""

            parties  = hit.get("parties", [])
            grantor  = "; ".join(p.get("name","") for p in parties if "GRANT" in str(p.get("type","")).upper() or "OWNER" in str(p.get("type","")).upper() or "FROM" in str(p.get("type","")).upper())
            grantee  = "; ".join(p.get("name","") for p in parties if "GRANT" in str(p.get("type","")).upper() and "OR" not in str(p.get("type","")).upper())

            if not grantor:
                grantor = hit.get("grantor","")
            if not grantee:
                grantee = hit.get("grantee","")

            doc_id    = hit.get("id") or doc_num
            clerk_url = f"https://dallas.tx.publicsearch.us/doc/{doc_id}" if doc_id else ""

            return {
                "doc_num":      doc_num,
                "doc_type":     raw_type,
                "filed":        filed_iso,
                "cat":          cat,
                "cat_label":    cat_label,
                "owner":        grantor,
                "grantee":      grantee,
                "amount":       _parse_amount(hit.get("amount") or 0),
                "legal":        hit.get("legalDescription",""),
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
        except Exception:
            return None

    async def _search_one_type(self, page, doc_type_text: str) -> list[dict]:
        """Search for one document type using the advanced search UI."""
        records = []
        try:
            log.info(f"  Navigating to advanced search ...")
            await page.goto(PORTAL_URL, wait_until="networkidle", timeout=45_000)
            await asyncio.sleep(2)

            # Select department: Real Property
            dept_sel = page.locator("select, [role='combobox'], [class*='department']").first
            if await dept_sel.count() > 0:
                await dept_sel.select_option(label="Real Property")
                await asyncio.sleep(1)

            # Set date range — try multiple selector patterns
            from_str = self.date_from.strftime("%m/%d/%Y")
            to_str   = self.date_to.strftime("%m/%d/%Y")

            for sel in ["[placeholder*='Start'], [placeholder*='From'], [aria-label*='Start'], [aria-label*='From'], input[name*='start' i], input[name*='from' i], input[name*='begin' i]"]:
                try:
                    el = page.locator(sel).first
                    if await el.count() > 0:
                        await el.triple_click()
                        await el.type(from_str)
                        log.info(f"    Set start date: {from_str}")
                        break
                except Exception:
                    pass

            for sel in ["[placeholder*='End'], [placeholder*='To'], [aria-label*='End'], [aria-label*='To'], input[name*='end' i], input[name*='to' i]"]:
                try:
                    el = page.locator(sel).first
                    if await el.count() > 0:
                        await el.triple_click()
                        await el.type(to_str)
                        log.info(f"    Set end date: {to_str}")
                        break
                except Exception:
                    pass

            # Type in doc type search box
            doc_type_inputs = [
                "input[placeholder*='doc' i]",
                "input[placeholder*='type' i]",
                "input[aria-label*='doc' i]",
                "input[class*='docType' i]",
                "[data-testid*='docType']",
            ]
            for sel in doc_type_inputs:
                try:
                    el = page.locator(sel).first
                    if await el.count() > 0:
                        await el.click()
                        await el.type(doc_type_text[:5])
                        await asyncio.sleep(1)
                        # Click matching dropdown option
                        option = page.locator(f"[role='option']:has-text('{doc_type_text}'), li:has-text('{doc_type_text}')").first
                        if await option.count() > 0:
                            await option.click()
                            log.info(f"    Selected doc type: {doc_type_text}")
                        break
                except Exception:
                    pass

            # Submit search
            submit = page.locator("button[type='submit'], button:has-text('Search'), [aria-label*='Search']").first
            if await submit.count() > 0:
                await submit.click()
                log.info(f"    Submitted search")
                await self._wait_for_results(page)

            # Parse results and paginate
            page_num = 1
            while True:
                recs = await self._parse_results_page(page)
                records.extend(recs)
                log.info(f"    Page {page_num}: {len(recs)} records")

                # Check for next page
                next_btn = page.locator("button:has-text('Next'), [aria-label*='Next'], [class*='next']:not([disabled])").first
                if await next_btn.count() == 0 or not await next_btn.is_enabled():
                    break
                await next_btn.click()
                await self._wait_for_results(page)
                page_num += 1

        except Exception as exc:
            log.warning(f"  Search failed for '{doc_type_text}': {exc}")

        return records

    async def run(self) -> list[dict]:
        log.info("Launching Playwright browser ...")
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage"]
            )
            context = await browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
                ),
                viewport={"width": 1280, "height": 800},
            )
            page = await context.new_page()

            # Enable request interception to capture API responses
            api_records = []

            async def handle_response(response):
                try:
                    url = response.url
                    if ("search" in url or "document" in url or "record" in url) and response.status == 200:
                        ct = response.headers.get("content-type", "")
                        if "json" in ct:
                            try:
                                data = await response.json()
                                if isinstance(data, dict):
                                    hits = (data.get("hits") or data.get("results") or
                                            data.get("content") or data.get("documents") or
                                            data.get("data") or [])
                                    if isinstance(hits, list) and hits:
                                        log.info(f"  API response captured: {len(hits)} hits from {url}")
                                        for hit in hits:
                                            rec = self._parse_hit(hit)
                                            if rec:
                                                api_records.append(rec)
                            except Exception:
                                pass
                except Exception:
                    pass

            page.on("response", handle_response)

            # First visit the portal to get cookies/session
            log.info("Loading portal ...")
            try:
                await page.goto("https://dallas.tx.publicsearch.us/", wait_until="networkidle", timeout=45_000)
                await asyncio.sleep(2)
                log.info(f"Landed on: {page.url}")
            except Exception as exc:
                log.error(f"Could not load portal: {exc}")
                await browser.close()
                return []

            # Take screenshot to see what we're working with
            await page.screenshot(path="/tmp/portal_landing.png")
            log.info("Screenshot saved to /tmp/portal_landing.png")

            # Log page title and any visible text
            title = await page.title()
            log.info(f"Page title: {title}")

            # Get all input fields visible on page
            inputs = await page.locator("input, select, button").all()
            log.info(f"Found {len(inputs)} interactive elements on page")

            for inp in inputs[:20]:
                try:
                    tag  = await inp.evaluate("el => el.tagName")
                    name = await inp.get_attribute("name") or ""
                    pid  = await inp.get_attribute("id") or ""
                    ph   = await inp.get_attribute("placeholder") or ""
                    txt  = await inp.inner_text() or ""
                    log.info(f"  Element: {tag} name={name} id={pid} placeholder={ph} text={txt[:30]}")
                except Exception:
                    pass

            # Now do a broad search for last 7 days with no doc type filter
            # to capture everything and filter locally
            log.info("Attempting broad date-range search ...")
            from_str = self.date_from.strftime("%m/%d/%Y")
            to_str   = self.date_to.strftime("%m/%d/%Y")

            # Try to navigate to advanced search
            try:
                await page.goto(PORTAL_URL, wait_until="networkidle", timeout=30_000)
                await asyncio.sleep(3)
                title2 = await page.title()
                log.info(f"Advanced search page title: {title2}")

                # Log all elements again
                inputs2 = await page.locator("input, select, button, [role='combobox']").all()
                log.info(f"Advanced search: {len(inputs2)} elements")
                for inp in inputs2[:30]:
                    try:
                        tag  = await inp.evaluate("el => el.tagName")
                        name = await inp.get_attribute("name") or ""
                        pid  = await inp.get_attribute("id") or ""
                        ph   = await inp.get_attribute("placeholder") or ""
                        aria = await inp.get_attribute("aria-label") or ""
                        cls  = await inp.get_attribute("class") or ""
                        log.info(f"  {tag} name={name} id={pid} ph={ph} aria={aria} class={cls[:40]}")
                    except Exception:
                        pass

                await page.screenshot(path="/tmp/portal_advanced.png")

            except Exception as exc:
                log.warning(f"Advanced search failed: {exc}")

            await browser.close()

        # Combine API-captured records with any parsed records
        all_records = api_records + self.records
        log.info(f"Total records collected: {len(all_records)}")

        # If still 0, return empty but log helpful info
        if not all_records:
            log.warning("No records found. Check /tmp/portal_landing.png and /tmp/portal_advanced.png for UI state.")

        return all_records


# ─────────────────────────────────────────────────────────────────────────────
# Parcel Lookup
# ─────────────────────────────────────────────────────────────────────────────
class ParcelLookup:
    def __init__(self):
        self._index: dict[str, dict] = {}

    def load(self):
        if not HAS_DBF:
            log.warning("dbfread not installed — skipping parcel enrichment.")
            return
        for url in (DCAD_ALT_URL, DCAD_BULK_URL):
            try:
                resp = requests.get(url, timeout=30)
                if resp.ok:
                    if "zip" in resp.headers.get("Content-Type",""):
                        self._load_zip(resp.content)
                        return
                    from bs4 import BeautifulSoup
                    soup = BeautifulSoup(resp.text, "lxml")
                    for a in soup.find_all("a", href=True):
                        if a["href"].endswith(".zip"):
                            base = "https://www.dcad.org"
                            dl_url = a["href"] if a["href"].startswith("http") else base+a["href"]
                            r2 = requests.get(dl_url, timeout=120, stream=True)
                            if r2.ok:
                                self._load_zip(r2.content)
                                return
            except Exception as exc:
                log.warning(f"DCAD probe failed: {exc}")

    def _load_zip(self, raw: bytes):
        try:
            import zipfile as zf
            z    = zf.ZipFile(_io.BytesIO(raw))
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

    def _index_row(self, row):
        owner = (row.get("OWNER") or row.get("OWN1") or "").strip()
        if not owner:
            return
        info = {
            "prop_address": (row.get("SITE_ADDR") or row.get("SITEADDR") or "").strip(),
            "prop_city":    (row.get("SITE_CITY") or "").strip(),
            "prop_state":   "TX",
            "prop_zip":     str(row.get("SITE_ZIP") or "").strip(),
            "mail_address": (row.get("ADDR_1") or row.get("MAILADR1") or "").strip(),
            "mail_city":    (row.get("CITY") or row.get("MAILCITY") or "").strip(),
            "mail_state":   (row.get("STATE") or "TX").strip(),
            "mail_zip":     str(row.get("ZIP") or row.get("MAILZIP") or "").strip(),
        }
        for v in _name_variants(owner):
            if v not in self._index:
                self._index[v] = info

    def lookup(self, owner: str) -> dict:
        for v in _name_variants(owner):
            if v in self._index:
                return self._index[v]
        return {}


# ─────────────────────────────────────────────────────────────────────────────
# Scoring
# ─────────────────────────────────────────────────────────────────────────────
def _score(record: dict, today: datetime, owner_cats: dict):
    score = 30
    flags = []
    cat   = record.get("cat","")
    amt   = record.get("amount", 0.0)
    try:
        new_this_week = (today - datetime.strptime(record["filed"], "%Y-%m-%d")).days <= 7
    except Exception:
        new_this_week = False

    if cat in ("LP","RELLP"):   flags.append("Lis pendens");       score += 10
    if cat == "NOFC":           flags.append("Pre-foreclosure");   score += 10
    owner_up = (record.get("owner") or "").upper()
    cats = owner_cats.get(owner_up, set())
    if "LP" in cats and "NOFC" in cats:
        flags.append("Lis pendens + Pre-foreclosure combo"); score += 20
    if cat in ("JUD","CCJ","DRJUD"): flags.append("Judgment lien");  score += 10
    if cat in ("TAXDEED","LNCORPTX","LNIRS","LNFED"): flags.append("Tax lien"); score += 10
    if cat == "LNMECH":         flags.append("Mechanic lien");     score += 10
    if cat in ("LNHOA","LN","MEDLN"): flags.append("Lien");        score += 10
    if cat == "PRO":            flags.append("Probate / estate");  score += 10
    if amt > 100_000:           flags.append("High debt (>$100k)"); score += 15
    elif amt > 50_000:          flags.append("Significant debt (>$50k)"); score += 10
    if new_this_week:           flags.append("New this week");     score += 5
    if record.get("prop_address"): flags.append("Has property address"); score += 5
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
    if not n: return "",""
    if "," in n:
        p=n.split(",",1); return p[1].strip(),p[0].strip()
    p=n.split()
    return (" ".join(p[:-1]),p[-1]) if len(p)>1 else ("",p[0])

def write_ghl_csv(records, path):
    with open(path,"w",newline="",encoding="utf-8") as f:
        w=csv.DictWriter(f,fieldnames=GHL_COLS); w.writeheader()
        for r in records:
            fn,ln=_split_name(r.get("owner",""))
            w.writerow({"First Name":fn,"Last Name":ln,
                "Mailing Address":r.get("mail_address",""),"Mailing City":r.get("mail_city",""),
                "Mailing State":r.get("mail_state","TX"),"Mailing Zip":r.get("mail_zip",""),
                "Property Address":r.get("prop_address",""),"Property City":r.get("prop_city","Dallas"),
                "Property State":r.get("prop_state","TX"),"Property Zip":r.get("prop_zip",""),
                "Lead Type":r.get("cat_label",""),"Document Type":r.get("doc_type",""),
                "Date Filed":r.get("filed",""),"Document Number":r.get("doc_num",""),
                "Amount/Debt Owed":r.get("amount",""),"Seller Score":r.get("score",0),
                "Motivated Seller Flags":"; ".join(r.get("flags",[])),"Source":"Dallas County Clerk",
                "Public Records URL":r.get("clerk_url","")})
    log.info(f"GHL CSV → {path}")


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────
async def main():
    today    = datetime.utcnow()
    week_ago = today - timedelta(days=7)
    log.info(f"Dallas County Scraper v3 | {week_ago.date()} → {today.date()}")

    parcel = ParcelLookup()
    parcel.load()

    scraper = DallasPlaywrightScraper(date_from=week_ago, date_to=today)
    records = await scraper.run()

    owner_cats: dict[str, set] = {}
    for r in records:
        owner_cats.setdefault((r.get("owner") or "").upper(), set()).add(r["cat"])

    enriched = []
    for r in records:
        pi = parcel.lookup(r.get("owner",""))
        r.update({k: pi.get(k) or r[k] for k in ["prop_address","prop_city","prop_state","prop_zip","mail_address","mail_city","mail_state","mail_zip"]})
        sc, fl = _score(r, today, owner_cats)
        r["score"] = sc; r["flags"] = fl
        enriched.append(r)

    enriched.sort(key=lambda x: x["score"], reverse=True)
    with_address = sum(1 for r in enriched if r.get("prop_address"))

    payload = {
        "fetched_at":   today.isoformat()+"Z",
        "source":       "Dallas County Clerk – dallas.tx.publicsearch.us",
        "date_range":   f"{week_ago.date()} to {today.date()}",
        "total":        len(enriched),
        "with_address": with_address,
        "records":      enriched,
    }
    for d in (DASHBOARD_DIR, DATA_DIR):
        p = d/"records.json"
        p.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
        log.info(f"JSON → {p}")

    write_ghl_csv(enriched, DATA_DIR/f"ghl_export_{today.strftime('%Y%m%d')}.csv")
    log.info(f"\n{'='*55}\n  Done. {len(enriched)} records | {with_address} with address\n  Top score: {enriched[0]['score'] if enriched else 'N/A'}\n{'='*55}")

if __name__ == "__main__":
    asyncio.run(main())
