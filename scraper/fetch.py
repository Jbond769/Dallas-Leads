#!/usr/bin/env python3
"""
Dallas County, TX — Motivated Seller Lead Scraper v6
- Uses Playwright to search the portal and collect doc numbers
- Then fetches each document detail page to get owner name + address
- Falls back to DCAD parcel lookup for mailing address
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
from bs4 import BeautifulSoup

try:
    from dbfread import DBF
    import zipfile, io as _io
    HAS_DBF = True
except ImportError:
    HAS_DBF = False

from playwright.async_api import async_playwright, TimeoutError as PWTimeout

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
DETAIL_URL    = "https://dallas.tx.publicsearch.us/doc/{doc_num}"
DCAD_BULK_URL = "https://www.dcad.org/wp-content/uploads/data/appraisal_data.zip"
DCAD_ALT_URL  = "https://www.dcad.org/data/"

ORDERED_KEYS = [
    "RELEASE LIS PENDENS","LIS PENDENS",
    "NOTICE OF FORECLOSURE","FORECLOSURE",
    "TAX DEED",
    "ABSTRACT OF JUDGMENT","CERTIFIED JUDGMENT","DOMESTIC JUDGMENT","JUDGMENT",
    "FEDERAL TAX LIEN","STATE TAX LIEN","IRS LIEN","FEDERAL LIEN",
    "HOA LIEN","MECHANIC","HOSPITAL LIEN","MEDICAID","LIEN",
    "PROBATE","NOTICE OF COMMENCEMENT",
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

SEARCH_DOC_TYPES = [
    "Lis Pendens",
    "Notice of Foreclosure",
    "Tax Deed",
    "Judgment",
    "Federal Tax Lien",
    "State Tax Lien",
    "Mechanic Lien",
    "Hospital Lien",
    "Lien",
    "Probate",
    "Notice of Commencement",
]

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

def _is_date(s: str) -> bool:
    return bool(re.match(r"\d{1,2}/\d{1,2}/\d{4}", s.strip()))

def _is_doc_num(s: str) -> bool:
    return bool(re.match(r"^\d{10,}$", s.strip()))


class DallasScraper:
    def __init__(self, date_from: datetime, date_to: datetime):
        self.date_from   = date_from
        self.date_to     = date_to
        self.api_records = []
        self.session     = requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        })

    # ── API response interceptor ──────────────────────────────────────────────
    async def _capture_response(self, response):
        try:
            if response.status != 200:
                return
            ct = response.headers.get("content-type", "")
            if "json" not in ct:
                return
            url = response.url
            if not any(kw in url for kw in ["search","result","document","record","hits","api"]):
                return
            data = await response.json()
            if not isinstance(data, dict):
                return
            hits = (data.get("hits") or data.get("results") or data.get("content") or
                    data.get("documents") or data.get("data") or data.get("records") or [])
            if not isinstance(hits, list) or not hits:
                return
            log.info(f"  API captured {len(hits)} records from {url}")
            for hit in hits:
                rec = self._parse_api_hit(hit)
                if rec:
                    self.api_records.append(rec)
        except Exception:
            pass

    def _parse_api_hit(self, hit: dict) -> Optional[dict]:
        try:
            raw_type = (hit.get("docType") or hit.get("documentType") or hit.get("type") or "")
            classified = _classify(raw_type)
            if not classified:
                return None
            cat, cat_label = classified

            doc_num = str(hit.get("documentNumber") or hit.get("docNumber") or
                          hit.get("instrumentNumber") or hit.get("id") or "")
            filed_raw = (hit.get("recordedDate") or hit.get("recordDate") or
                         hit.get("filedDate") or "")
            filed_iso = str(filed_raw)[:10] if filed_raw else ""

            parties = hit.get("parties", hit.get("names", []))
            grantor = grantee = ""
            if isinstance(parties, list):
                for p in parties:
                    ptype = str(p.get("type","")).upper()
                    name  = p.get("name","").strip()
                    if any(x in ptype for x in ["GRANTOR","SELLER","FROM","OWNER","DEBTOR"]):
                        grantor = (grantor + "; " + name).strip("; ")
                    elif any(x in ptype for x in ["GRANTEE","BUYER","TO","CREDITOR"]):
                        grantee = (grantee + "; " + name).strip("; ")
            if not grantor:
                grantor = hit.get("grantor", hit.get("grantorName","")).strip()
            if not grantee:
                grantee = hit.get("grantee", hit.get("granteeName","")).strip()

            doc_id    = hit.get("id") or hit.get("documentId") or doc_num
            clerk_url = f"https://dallas.tx.publicsearch.us/doc/{doc_id}" if doc_id else ""

            return {
                "doc_num": doc_num, "doc_type": raw_type,
                "filed": filed_iso, "cat": cat, "cat_label": cat_label,
                "owner": grantor, "grantee": grantee,
                "amount": _parse_amount(hit.get("amount") or hit.get("consideration") or 0),
                "legal": hit.get("legalDescription", hit.get("legal","")),
                "clerk_url": clerk_url,
                "prop_address":"", "prop_city":"Dallas", "prop_state":"TX", "prop_zip":"",
                "mail_address":"", "mail_city":"", "mail_state":"TX", "mail_zip":"",
                "flags":[], "score":30,
            }
        except Exception:
            return None

    # ── Fetch detail page for a doc number ────────────────────────────────────
    def _fetch_detail(self, doc_num: str) -> dict:
        """Fetch the document detail page and extract owner + address."""
        info = {"owner":"", "grantee":"", "prop_address":"", "prop_city":"",
                "prop_zip":"", "mail_address":"", "mail_city":"",
                "mail_state":"TX", "mail_zip":"", "amount":0.0, "legal":"", "clerk_url":""}
        try:
            url  = DETAIL_URL.format(doc_num=doc_num)
            info["clerk_url"] = url
            resp = self.session.get(url, timeout=20)
            if not resp.ok:
                return info
            soup = BeautifulSoup(resp.text, "lxml")

            # Extract all label→value pairs from the detail page
            # Neumo uses dl/dt/dd or table rows or labeled divs
            pairs = {}

            # Try dl > dt + dd
            for dt in soup.find_all("dt"):
                dd = dt.find_next_sibling("dd")
                if dd:
                    pairs[dt.get_text(strip=True).upper()] = dd.get_text(strip=True)

            # Try table rows with th + td
            for tr in soup.find_all("tr"):
                th = tr.find("th")
                td = tr.find("td")
                if th and td:
                    pairs[th.get_text(strip=True).upper()] = td.get_text(strip=True)

            # Try divs with class containing 'label' next to value divs
            for div in soup.find_all("div", class_=re.compile(r"label|field-name|title", re.I)):
                sibling = div.find_next_sibling()
                if sibling:
                    pairs[div.get_text(strip=True).upper()] = sibling.get_text(strip=True)

            log.debug(f"Detail page pairs for {doc_num}: {list(pairs.keys())[:10]}")

            # Map known field names
            GRANTOR_KEYS = ["GRANTOR","SELLER","OWNER","DEBTOR","FROM","DEFENDANT"]
            GRANTEE_KEYS = ["GRANTEE","BUYER","CREDITOR","TO","PLAINTIFF","LIENEE"]
            ADDR_KEYS    = ["SITE ADDRESS","PROPERTY ADDRESS","SITUS","ADDRESS","SITE"]
            LEGAL_KEYS   = ["LEGAL DESCRIPTION","LEGAL"]
            AMOUNT_KEYS  = ["AMOUNT","CONSIDERATION","DEBT","BALANCE"]

            for k,v in pairs.items():
                if any(gk in k for gk in GRANTOR_KEYS) and not info["owner"]:
                    info["owner"] = v
                if any(gk in k for gk in GRANTEE_KEYS) and not info["grantee"]:
                    info["grantee"] = v
                if any(ak in k for ak in ADDR_KEYS) and not info["prop_address"]:
                    info["prop_address"] = v
                if any(lk in k for lk in LEGAL_KEYS) and not info["legal"]:
                    info["legal"] = v
                if any(amk in k for amk in AMOUNT_KEYS) and not info["amount"]:
                    info["amount"] = _parse_amount(v)

            # Also look for party names in any list items or spans
            if not info["owner"]:
                # Find any element labeled grantor/debtor/seller
                for el in soup.find_all(string=re.compile(r"grantor|debtor|seller|owner", re.I)):
                    parent = el.parent
                    nxt    = parent.find_next_sibling()
                    if nxt:
                        candidate = nxt.get_text(strip=True)
                        if candidate and not _is_date(candidate) and len(candidate) > 2:
                            info["owner"] = candidate
                            break

            # Try JSON-LD or embedded JSON
            for script in soup.find_all("script", type="application/json"):
                try:
                    data = json.loads(script.string)
                    if isinstance(data, dict):
                        parties = data.get("parties", [])
                        for p in parties:
                            ptype = str(p.get("type","")).upper()
                            name  = p.get("name","").strip()
                            if any(x in ptype for x in ["GRANTOR","OWNER","DEBTOR","SELLER"]) and not info["owner"]:
                                info["owner"] = name
                            if any(x in ptype for x in ["GRANTEE","BUYER","CREDITOR"]) and not info["grantee"]:
                                info["grantee"] = name
                        if not info["prop_address"]:
                            info["prop_address"] = data.get("siteAddress","") or data.get("propertyAddress","")
                        if not info["legal"]:
                            info["legal"] = data.get("legalDescription","")
                        if not info["amount"]:
                            info["amount"] = _parse_amount(data.get("amount",""))
                except Exception:
                    pass

        except Exception as exc:
            log.debug(f"Detail fetch error for {doc_num}: {exc}")
        return info

    # ── HTML table parser ─────────────────────────────────────────────────────
    async def _parse_results_table(self, page) -> list[dict]:
        records = []
        try:
            content = await page.content()
            soup    = BeautifulSoup(content, "lxml")

            # Find all rows
            rows = (
                soup.select("tr.rt-tr-group") or
                soup.select("tbody tr") or
                soup.select("[class*='result-row']")
            )
            if not rows:
                table = soup.find("table")
                if table:
                    rows = table.find_all("tr")[1:]

            for row in rows:
                try:
                    cells = row.find_all(["td","th"])
                    texts = [c.get_text(strip=True) for c in cells]
                    texts = [t for t in texts if t]
                    if len(texts) < 2:
                        continue

                    # Find doc number (long numeric string)
                    doc_num = ""
                    filed_iso = ""
                    raw_type = ""

                    for t in texts:
                        if _is_doc_num(t) and not doc_num:
                            doc_num = t
                        elif _is_date(t) and not filed_iso:
                            try:
                                filed_iso = datetime.strptime(t, "%m/%d/%Y").strftime("%Y-%m-%d")
                            except Exception:
                                pass
                        elif _classify(t) and not raw_type:
                            raw_type = t

                    if not doc_num and not raw_type:
                        continue

                    # Find link for clerk_url
                    link = row.find("a", href=True)
                    clerk_url = ""
                    if link:
                        h = link["href"]
                        clerk_url = h if h.startswith("http") else f"https://dallas.tx.publicsearch.us{h}"
                        # Extract doc num from URL if not found
                        if not doc_num:
                            m = re.search(r"/doc/(\d+)", h)
                            if m:
                                doc_num = m.group(1)

                    if not raw_type:
                        # Try to classify any text cell
                        for t in texts:
                            if _classify(t):
                                raw_type = t
                                break

                    classified = _classify(raw_type) if raw_type else None
                    if not classified:
                        continue
                    cat, cat_label = classified

                    records.append({
                        "doc_num": doc_num, "doc_type": raw_type,
                        "filed": filed_iso, "cat": cat, "cat_label": cat_label,
                        "owner": "", "grantee": "",
                        "amount": 0.0, "legal": "",
                        "clerk_url": clerk_url or (DETAIL_URL.format(doc_num=doc_num) if doc_num else ""),
                        "prop_address":"", "prop_city":"Dallas", "prop_state":"TX", "prop_zip":"",
                        "mail_address":"", "mail_city":"", "mail_state":"TX", "mail_zip":"",
                        "flags":[], "score":30,
                    })
                except Exception:
                    continue
        except Exception as exc:
            log.debug(f"Table parse error: {exc}")
        return records

    # ── Search one doc type ───────────────────────────────────────────────────
    async def _search_one_type(self, page, doc_type: str) -> list[dict]:
        records  = []
        from_str = self.date_from.strftime("%m/%d/%Y")
        to_str   = self.date_to.strftime("%m/%d/%Y")

        try:
            await page.goto(PORTAL_URL, wait_until="networkidle", timeout=45_000)
            await asyncio.sleep(2)

            # ── Date pickers ──────────────────────────────────────────────
            # Open start date calendar and type date
            start_cal = page.locator("[aria-label='Open start date calendar']").first
            if await start_cal.count() > 0:
                await start_cal.click()
                await asyncio.sleep(0.8)
                # The date input should now be visible
                date_inp = page.locator("input[class*='date'], input[aria-label*='start' i], input[placeholder*='date' i]").first
                if await date_inp.count() > 0:
                    await date_inp.click()
                    await date_inp.press("Control+a")
                    await date_inp.type(from_str)
                    await asyncio.sleep(0.3)
                await page.keyboard.press("Escape")
                await asyncio.sleep(0.5)

            end_cal = page.locator("[aria-label='Open end date calendar']").first
            if await end_cal.count() > 0:
                await end_cal.click()
                await asyncio.sleep(0.8)
                date_inp = page.locator("input[class*='date'], input[aria-label*='end' i]").first
                if await date_inp.count() > 0:
                    await date_inp.click()
                    await date_inp.press("Control+a")
                    await date_inp.type(to_str)
                    await asyncio.sleep(0.3)
                await page.keyboard.press("Escape")
                await asyncio.sleep(0.5)

            # ── Doc type tokenized input ──────────────────────────────────
            inp = page.locator("#docTypes-input").first
            if await inp.count() == 0:
                inp = page.locator("[aria-label='Filter Document Types']").first
            if await inp.count() > 0:
                await inp.click()
                await asyncio.sleep(0.3)
                await inp.type(doc_type[:4], delay=80)
                await asyncio.sleep(1.5)
                # Pick matching option
                option = page.locator(f"[role='option']:has-text('{doc_type}')").first
                if await option.count() > 0:
                    await option.click()
                    log.info(f"    Selected: {doc_type}")
                else:
                    # Try any visible option
                    all_opts = await page.locator("[role='option']").all()
                    matched = False
                    for opt in all_opts:
                        txt = await opt.inner_text()
                        if doc_type.lower() in txt.lower():
                            await opt.click()
                            log.info(f"    Selected: {txt.strip()}")
                            matched = True
                            break
                    if not matched:
                        log.warning(f"    No dropdown for: {doc_type} — searching without filter")
                        await page.keyboard.press("Escape")
                await asyncio.sleep(0.5)

            # ── Submit ────────────────────────────────────────────────────
            btn = page.locator("#search-btn, button:has-text('Search'), button[type='submit']").first
            await btn.click()
            await page.wait_for_load_state("networkidle", timeout=30_000)
            await asyncio.sleep(3)

            # ── Paginate and collect ──────────────────────────────────────
            page_num = 1
            while page_num <= 20:
                html_recs = await self._parse_results_table(page)
                records.extend(html_recs)
                # Also grab any API-captured
                records.extend(self.api_records)
                self.api_records = []

                if html_recs:
                    log.info(f"    Page {page_num}: {len(html_recs)} records")

                next_btn = page.locator("button[aria-label='Next page'], button:has-text('Next')").first
                if await next_btn.count() == 0 or not await next_btn.is_enabled():
                    break
                await next_btn.click()
                await page.wait_for_load_state("networkidle", timeout=20_000)
                await asyncio.sleep(2)
                page_num += 1

        except Exception as exc:
            log.warning(f"  Search error '{doc_type}': {exc}")

        return records

    # ── Main run ──────────────────────────────────────────────────────────────
    async def run(self) -> list[dict]:
        log.info("Launching browser ...")
        all_records = []
        seen_keys   = set()

        async with async_playwright() as pw:
            browser = await pw.chromium.launch(
                headless=True,
                args=["--no-sandbox","--disable-dev-shm-usage"]
            )
            context = await browser.new_context(
                user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36",
                viewport={"width": 1280, "height": 900},
            )
            page = await context.new_page()
            page.on("response", self._capture_response)

            # Warm up
            try:
                await page.goto("https://dallas.tx.publicsearch.us/", wait_until="networkidle", timeout=45_000)
                await asyncio.sleep(2)
                log.info(f"Portal loaded: {page.url}")
            except Exception as exc:
                log.error(f"Portal load failed: {exc}")
                await browser.close()
                return []

            for doc_type in SEARCH_DOC_TYPES:
                self.api_records = []
                recs = await self._search_one_type(page, doc_type)
                added = 0
                for r in recs:
                    key = r.get("doc_num") or f"{r['doc_type']}{r['filed']}"
                    if key and key not in seen_keys:
                        seen_keys.add(key)
                        all_records.append(r)
                        added += 1
                log.info(f"  → {added} new records for '{doc_type}' (total: {len(all_records)})")
                await asyncio.sleep(1)

            await browser.close()

        # Filter non-targets
        all_records = [r for r in all_records if r.get("cat") and r["cat"] != "OTHER"]

        # ── Enrich with detail page ────────────────────────────────────────
        log.info(f"Fetching detail pages for {len(all_records)} records ...")
        for i, r in enumerate(all_records):
            if r.get("doc_num"):
                detail = self._fetch_detail(r["doc_num"])
                # Only update fields that are still empty
                for field in ["owner","grantee","prop_address","prop_city","prop_zip",
                              "mail_address","mail_city","mail_state","mail_zip","legal","clerk_url"]:
                    if not r.get(field) and detail.get(field):
                        r[field] = detail[field]
                if not r.get("amount") or r["amount"] == 0:
                    r["amount"] = detail.get("amount", 0.0)
                if i % 5 == 0:
                    log.info(f"  Detail {i+1}/{len(all_records)}: {r['doc_num']} → owner='{r.get('owner','')}' addr='{r.get('prop_address','')}'")
                time.sleep(0.5)  # be polite

        log.info(f"Total records: {len(all_records)}")
        return all_records


# ─────────────────────────────────────────────────────────────────────────────
# Parcel Lookup (DCAD)
# ─────────────────────────────────────────────────────────────────────────────
class ParcelLookup:
    def __init__(self): self._index = {}

    def load(self):
        if not HAS_DBF:
            log.warning("dbfread not installed — skipping parcel enrichment.")
            return
        for url in (DCAD_ALT_URL, DCAD_BULK_URL):
            try:
                resp = requests.get(url, timeout=30)
                if not resp.ok: continue
                ct = resp.headers.get("Content-Type","")
                if "zip" in ct:
                    self._load_zip(resp.content); return
                soup = BeautifulSoup(resp.text, "lxml")
                for a in soup.find_all("a", href=True):
                    if a["href"].endswith(".zip"):
                        base = "https://www.dcad.org"
                        r2   = requests.get(a["href"] if a["href"].startswith("http") else base+a["href"], timeout=120)
                        if r2.ok: self._load_zip(r2.content); return
            except Exception as exc:
                log.warning(f"DCAD probe: {exc}")

    def _load_zip(self, raw):
        try:
            import zipfile as zf
            z    = zf.ZipFile(_io.BytesIO(raw))
            dbfs = [n for n in z.namelist() if n.lower().endswith(".dbf")]
            if not dbfs: return
            tmp  = Path("/tmp/dcad.dbf"); tmp.write_bytes(z.read(dbfs[0]))
            for row in DBF(str(tmp), encoding="latin-1", ignore_missing_memofile=True):
                self._idx(dict(row))
            log.info(f"DCAD index: {len(self._index):,} variants")
        except Exception as exc: log.error(f"DBF: {exc}")

    def _idx(self, row):
        owner = (row.get("OWNER") or row.get("OWN1") or "").strip()
        if not owner: return
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
            if v not in self._index: self._index[v] = info

    def lookup(self, owner):
        for v in _name_variants(owner):
            if v in self._index: return self._index[v]
        return {}


# ─────────────────────────────────────────────────────────────────────────────
# Scoring
# ─────────────────────────────────────────────────────────────────────────────
def _score(record, today, owner_cats):
    score=30; flags=[]; cat=record.get("cat",""); amt=record.get("amount",0.0)
    try: new_week=(today-datetime.strptime(record["filed"],"%Y-%m-%d")).days<=7
    except Exception: new_week=False
    if cat in ("LP","RELLP"): flags.append("Lis pendens"); score+=10
    if cat=="NOFC": flags.append("Pre-foreclosure"); score+=10
    ou=(record.get("owner") or "").upper()
    cats=owner_cats.get(ou,set())
    if "LP" in cats and "NOFC" in cats: flags.append("Lis pendens + Pre-foreclosure combo"); score+=20
    if cat in ("JUD","CCJ","DRJUD"): flags.append("Judgment lien"); score+=10
    if cat in ("TAXDEED","LNCORPTX","LNIRS","LNFED"): flags.append("Tax lien"); score+=10
    if cat=="LNMECH": flags.append("Mechanic lien"); score+=10
    if cat in ("LNHOA","LN","MEDLN"): flags.append("Lien"); score+=10
    if cat=="PRO": flags.append("Probate / estate"); score+=10
    if amt>100_000: flags.append("High debt (>$100k)"); score+=15
    elif amt>50_000: flags.append("Significant debt (>$50k)"); score+=10
    if new_week: flags.append("New this week"); score+=5
    if record.get("prop_address"): flags.append("Has property address"); score+=5
    if any(kw in ou for kw in ("LLC","INC","CORP","LTD","TRUST","ESTATE")): flags.append("LLC / corp owner"); score+=10
    return min(score,100), list(dict.fromkeys(flags))


GHL_COLS=["First Name","Last Name","Mailing Address","Mailing City","Mailing State","Mailing Zip","Property Address","Property City","Property State","Property Zip","Lead Type","Document Type","Date Filed","Document Number","Amount/Debt Owed","Seller Score","Motivated Seller Flags","Source","Public Records URL"]

def _split_name(n):
    if not n: return "",""
    if "," in n: p=n.split(",",1); return p[1].strip(),p[0].strip()
    p=n.split(); return (" ".join(p[:-1]),p[-1]) if len(p)>1 else ("",p[0])

def write_ghl_csv(records, path):
    with open(path,"w",newline="",encoding="utf-8") as f:
        w=csv.DictWriter(f,fieldnames=GHL_COLS); w.writeheader()
        for r in records:
            fn,ln=_split_name(r.get("owner",""))
            w.writerow({"First Name":fn,"Last Name":ln,"Mailing Address":r.get("mail_address",""),"Mailing City":r.get("mail_city",""),"Mailing State":r.get("mail_state","TX"),"Mailing Zip":r.get("mail_zip",""),"Property Address":r.get("prop_address",""),"Property City":r.get("prop_city","Dallas"),"Property State":r.get("prop_state","TX"),"Property Zip":r.get("prop_zip",""),"Lead Type":r.get("cat_label",""),"Document Type":r.get("doc_type",""),"Date Filed":r.get("filed",""),"Document Number":r.get("doc_num",""),"Amount/Debt Owed":r.get("amount",""),"Seller Score":r.get("score",0),"Motivated Seller Flags":"; ".join(r.get("flags",[])),"Source":"Dallas County Clerk","Public Records URL":r.get("clerk_url","")})
    log.info(f"GHL CSV → {path}")


async def main():
    today=datetime.utcnow(); week_ago=today-timedelta(days=7)
    log.info(f"Dallas County Scraper v6 | {week_ago.date()} → {today.date()}")

    parcel=ParcelLookup(); parcel.load()
    scraper=DallasScraper(date_from=week_ago,date_to=today)
    records=await scraper.run()

    owner_cats={}
    for r in records: owner_cats.setdefault((r.get("owner") or "").upper(),set()).add(r["cat"])

    enriched=[]
    for r in records:
        pi=parcel.lookup(r.get("owner",""))
        for k in ["prop_address","prop_city","prop_state","prop_zip","mail_address","mail_city","mail_state","mail_zip"]:
            if not r.get(k) and pi.get(k): r[k]=pi[k]
        sc,fl=_score(r,today,owner_cats); r["score"]=sc; r["flags"]=fl
        enriched.append(r)

    enriched.sort(key=lambda x:x["score"],reverse=True)
    with_address=sum(1 for r in enriched if r.get("prop_address"))

    payload={"fetched_at":today.isoformat()+"Z","source":"Dallas County Clerk – dallas.tx.publicsearch.us","date_range":f"{week_ago.date()} to {today.date()}","total":len(enriched),"with_address":with_address,"records":enriched}
    for d in (DASHBOARD_DIR,DATA_DIR):
        p=d/"records.json"; p.write_text(json.dumps(payload,indent=2,default=str),encoding="utf-8"); log.info(f"JSON → {p}")

    write_ghl_csv(enriched,DATA_DIR/f"ghl_export_{today.strftime('%Y%m%d')}.csv")
    log.info(f"\n{'='*55}\n  Done. {len(enriched)} records | {with_address} with address\n  Top score: {enriched[0]['score'] if enriched else 'N/A'}\n{'='*55}")

if __name__=="__main__":
    asyncio.run(main())
