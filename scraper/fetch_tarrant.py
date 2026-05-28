#!/usr/bin/env python3
"""
Tarrant County, TX — Motivated Seller Lead Scraper v1
- Uses Playwright for search on tarrant.tx.publicsearch.us
- TAD (tad.org) address lookup via owner name search
- Same logic as Dallas County scraper
"""

import asyncio
import csv
import json
import logging
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

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
log = logging.getLogger("tarrant_scraper")

BASE_DIR      = Path(__file__).resolve().parent.parent
DASHBOARD_DIR = BASE_DIR / "dashboard"
DATA_DIR      = BASE_DIR / "data"
DASHBOARD_DIR.mkdir(parents=True, exist_ok=True)
DATA_DIR.mkdir(parents=True, exist_ok=True)

PORTAL_URL = "https://tarrant.tx.publicsearch.us/search/advanced"
TAD_SEARCH = "https://www.tad.org/search-results"

ORDERED_KEYS = [
    "RELEASE LIS PENDENS","LIS PENDENS",
    "NOTICE OF FORECLOSURE","FORECLOSURE","TAX DEED",
    "ABSTRACT OF JUDGMENT","CERTIFIED JUDGMENT","DOMESTIC JUDGMENT","JUDGMENT",
    "FEDERAL TAX LIEN","STATE TAX LIEN","IRS LIEN","FEDERAL LIEN",
    "HOA LIEN","MECHANIC","HOSPITAL LIEN","MEDICAID","LIEN",
    "PROBATE","NOTICE OF COMMENCEMENT",
]
TARGET_KEYWORDS = {
    "RELEASE LIS PENDENS":    ("RELLP","Release Lis Pendens"),
    "LIS PENDENS":            ("LP","Lis Pendens"),
    "NOTICE OF FORECLOSURE":  ("NOFC","Notice of Foreclosure"),
    "FORECLOSURE":            ("NOFC","Notice of Foreclosure"),
    "TAX DEED":               ("TAXDEED","Tax Deed"),
    "ABSTRACT OF JUDGMENT":   ("JUD","Judgment"),
    "CERTIFIED JUDGMENT":     ("CCJ","Certified Judgment"),
    "DOMESTIC JUDGMENT":      ("DRJUD","Domestic Judgment"),
    "JUDGMENT":               ("JUD","Judgment"),
    "FEDERAL TAX LIEN":       ("LNFED","Federal Tax Lien"),
    "STATE TAX LIEN":         ("LNCORPTX","State Tax Lien"),
    "IRS LIEN":               ("LNIRS","IRS Lien"),
    "FEDERAL LIEN":           ("LNFED","Federal Lien"),
    "HOA LIEN":               ("LNHOA","HOA Lien"),
    "MECHANIC":               ("LNMECH","Mechanic Lien"),
    "HOSPITAL LIEN":          ("MEDLN","Hospital Lien"),
    "MEDICAID":               ("MEDLN","Medicaid Lien"),
    "LIEN":                   ("LN","Lien"),
    "PROBATE":                ("PRO","Probate / Estate"),
    "NOTICE OF COMMENCEMENT": ("NOC","Notice of Commencement"),
}

SEARCH_DOC_TYPES = [
    "Lis Pendens","Notice of Foreclosure","Tax Deed","Judgment",
    "Federal Tax Lien","State Tax Lien","Mechanic Lien",
    "Hospital Lien","Lien","Probate","Notice of Commencement",
]

SKIP_TERMS = (
    "LLC","INC","CORP","TRUST","BANK","ASSOCIATION","ELECTRONIC",
    "MORTGAGE","CAPITAL","CHASE","STATE OF","SOLUTIONS","WHOLESALE",
    "CREDIT","HOMEOWNERS","JPMORGAN","MERS","TOLLESON","PROPERTY OWNERS",
    "LTD","FUND","FINANCIAL","SERVICES","HOLDINGS","GROUP","PARTNERS",
    "MUSTANG","AMIGOS","WESTGROVE","UNITED","ARIGLO","GLOBAL","ETS",
    " CITY"," COUNTY"," DISTRICT","MUNICIPALITY","DEPARTMENT","AUTHORITY",
)

def _classify(raw):
    u = raw.upper()
    for k in ORDERED_KEYS:
        if k in u:
            return TARGET_KEYWORDS[k]
    return None

def _parse_amount(v):
    try: return float(re.sub(r"[^\d.]","",str(v).replace(",","")))
    except: return 0.0

def _is_date(s):
    return bool(re.match(r"^\d{1,2}/\d{1,2}/\d{4}$",s.strip()))

def _is_doc_num(s):
    return bool(re.match(r"^\d{10,}$",s.strip()))

def _is_person(name):
    u = name.upper()
    return not any(t in u for t in SKIP_TERMS)

def _tad_query(name):
    """Format name for TAD search."""
    parts = name.strip().upper().split()
    if not parts: return None
    return parts[0] if len(parts) == 1 else f"{parts[0]} {parts[1]}"


async def _lookup_tad(context, owner_name):
    """Query TAD property search by owner name and return address info."""
    if not _is_person(owner_name):
        return {}
    query = _tad_query(owner_name)
    if not query:
        return {}

    page = await context.new_page()
    try:
        # TAD uses a search results page with owner name parameter
        search_url = f"https://www.tad.org/search-results/?search_type=owner&keyword={query.replace(' ', '+')}"
        await page.goto(search_url, wait_until="networkidle", timeout=30_000)
        await asyncio.sleep(2)

        content = await page.content()
        soup = BeautifulSoup(content, "lxml")

        # Find first property result link
        detail_link = None
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if "/property/" in href or "account" in href.lower() or "prop_id" in href.lower():
                detail_link = href
                break

        # Also try table rows with address data directly on results page
        info = {}
        all_text = soup.get_text(" ", strip=True)

        # TAD results page often shows address in table directly
        # Look for "OWNER ADDRESS" or situs address patterns
        m_addr = re.search(r"(\d{3,5}\s+[A-Z0-9 ]{3,}(?:LN|DR|ST|AVE|BLVD|RD|WAY|CT|CIR|PL|TRL|PKWY|LOOP))\s+([A-Z][A-Z ]+),?\s*TX\s*(7[5-9]\d{3})", all_text, re.I)
        if m_addr:
            info["prop_address"] = re.sub(r"\s+", " ", m_addr.group(1)).strip()
            info["prop_city"]    = m_addr.group(2).strip().title()
            info["prop_state"]   = "TX"
            info["prop_zip"]     = m_addr.group(3)

        if not info.get("prop_address") and detail_link:
            full_url = detail_link if detail_link.startswith("http") else f"https://www.tad.org{detail_link}"
            await page.goto(full_url, wait_until="networkidle", timeout=30_000)
            await asyncio.sleep(2)

            detail_content = await page.content()
            raw_html  = detail_content
            html_dec  = raw_html.replace("&nbsp;", " ")
            dsoup     = BeautifulSoup(detail_content, "lxml")
            all_text  = dsoup.get_text(" ", strip=True).replace("\xa0", " ")

            # Situs/property address
            m = re.search(r"(?:Situs|Site|Property)\s*Address[:\s]+(\d+\s+[A-Z0-9][A-Z0-9 ]{3,}?)(?:\s{2,}|\s+(?:Fort Worth|Arlington|Euless|Bedford|Hurst|Keller|Southlake|Grapevine|Mansfield|Grand Prairie|North Richland|Watauga|Haltom|Benbrook|Burleson|Crowley|Azle|Saginaw|Richland Hills|White Settlement|Colleyville|Flower Mound|TX|CITY|Suite|Apt|#))", all_text, re.I)
            if m:
                info["prop_address"] = re.sub(r"\s+", " ", m.group(1)).strip()

            # Street address pattern fallback
            if not info.get("prop_address"):
                m2 = re.search(r"(\d{3,5}\s+[A-Z][A-Z0-9 ]{2,}(?:LN|DR|ST|AVE|BLVD|RD|WAY|CT|CIR|PL|TRL|PKWY|LOOP|PASS|XING|TRCE|COVE|CV|RUN|BND))", all_text, re.I)
                if m2:
                    candidate = re.sub(r"\s+", " ", m2.group(1)).strip()
                    bad = ("NOTICE","PROTEST","APPRAISAL","REPORT","PROCESS","SYSTEM","ONLINE","CURRENT","ANNUAL")
                    if not any(b in candidate.upper() for b in bad):
                        info["prop_address"] = candidate

            # City from TX city patterns
            tarrant_cities = (
                "FORT WORTH","ARLINGTON","EULESS","BEDFORD","HURST","KELLER",
                "SOUTHLAKE","GRAPEVINE","MANSFIELD","GRAND PRAIRIE","NORTH RICHLAND HILLS",
                "WATAUGA","HALTOM CITY","BENBROOK","BURLESON","CROWLEY","AZLE",
                "SAGINAW","RICHLAND HILLS","WHITE SETTLEMENT","COLLEYVILLE","WESTLAKE",
                "TROPHY CLUB","ROANOKE","HASLET","LAKE WORTH","FOREST HILL","KENNEDALE",
                "EVERMAN","EDGECLIFF","PANTEGO","DALWORTHINGTON GARDENS",
            )
            for city in tarrant_cities:
                if city in all_text.upper():
                    info["prop_city"] = city.title()
                    break

            # Zip from HTML
            zip_matches = re.findall(r"\b(7[5-9]\d{3})\b", html_dec)
            if zip_matches:
                info["prop_zip"]   = zip_matches[0]
                info["prop_state"] = "TX"

        log.info(f"    TAD {'hit' if info.get('prop_address') else 'no addr'} for '{owner_name}': {info.get('prop_address','')}")
        await page.close()
        return info

    except Exception as exc:
        log.debug(f"TAD lookup error for '{owner_name}': {exc}")
        try: await page.close()
        except: pass
        return {}


class TarrantScraper:
    def __init__(self, date_from, date_to):
        self.date_from = date_from
        self.date_to   = date_to

    async def _parse_table(self, page):
        records = []
        try:
            content = await page.content()
            soup    = BeautifulSoup(content, "lxml")

            rows = (soup.select("tr.rt-tr-group") or
                    soup.select("tbody tr") or
                    soup.select("[class*='result-row']"))
            if not rows:
                tbl = soup.find("table")
                if tbl:
                    rows = tbl.find_all("tr")[1:]

            if rows:
                first_cells = [c.get_text(strip=True) for c in rows[0].find_all(["td","th"])]
                log.info(f"    First row cells: {first_cells}")

            for row in rows:
                try:
                    cells = row.find_all(["td","th"])
                    texts = [c.get_text(strip=True) for c in cells]
                    texts = [t for t in texts if t]
                    if len(texts) < 2: continue

                    doc_num=""; filed_iso=""; raw_type=""
                    grantor=""; grantee=""; amount=0.0; link_url=""

                    a = row.find("a", href=True)
                    if a:
                        h = a["href"]
                        link_url = h if h.startswith("http") else f"https://tarrant.tx.publicsearch.us{h}"
                        m = re.search(r"/doc/(\d+)|/results/(\d+)", h)
                        if m: doc_num = m.group(1) or m.group(2)

                    for t in texts:
                        if _is_doc_num(t) and not doc_num: doc_num = t
                        elif _is_date(t) and not filed_iso:
                            try: filed_iso = datetime.strptime(t,"%m/%d/%Y").strftime("%Y-%m-%d")
                            except: pass
                        elif _classify(t) and not raw_type: raw_type = t
                        elif re.match(r"\$[\d,]+",t): amount = _parse_amount(t.replace("$",""))

                    # Index-based: [0][1][2][3=GRANTOR][4=GRANTEE][5=DOC_TYPE][6=DATE][7=DOC_NUM][8=??][9=CITY]
                    raw_cells = [c.get_text(strip=True) for c in cells]
                    prop_city = ""
                    if len(raw_cells) >= 4:
                        g = raw_cells[3].strip()
                        if g and g not in ("N/A","--/--/--") and not _is_date(g):
                            grantor = g
                    if len(raw_cells) >= 5:
                        g2 = raw_cells[4].strip()
                        if g2 and g2 not in ("N/A","--/--/--") and not _is_date(g2):
                            grantee = g2
                    if len(raw_cells) >= 10:
                        city = raw_cells[9].strip()
                        if city and city not in ("N/A","--/--/--"):
                            prop_city = city

                    if not raw_type: continue
                    classified = _classify(raw_type)
                    if not classified: continue
                    cat, cat_label = classified

                    records.append({
                        "doc_num": doc_num, "doc_type": raw_type,
                        "filed": filed_iso, "cat": cat, "cat_label": cat_label,
                        "owner": grantor, "grantee": grantee,
                        "amount": amount, "legal": "",
                        "clerk_url": link_url or (f"https://tarrant.tx.publicsearch.us/doc/{doc_num}" if doc_num else ""),
                        "prop_address":"","prop_city": prop_city or "Fort Worth","prop_state":"TX","prop_zip":"",
                        "mail_address":"","mail_city":"","mail_state":"TX","mail_zip":"",
                        "flags":[],"score":30,
                    })
                except: continue
        except Exception as exc:
            log.debug(f"Table parse: {exc}")
        return records

    async def _search_one(self, page, doc_type, from_str, to_str):
        records = []
        try:
            await page.goto(PORTAL_URL, wait_until="networkidle", timeout=45_000)
            await asyncio.sleep(2)

            # Log all input fields for first run to understand form structure
            all_inputs = await page.locator("input").all()
            log.info(f"  [FORM] Found {len(all_inputs)} inputs on page")
            for idx, inp in enumerate(all_inputs[:8]):
                try:
                    ph = await inp.get_attribute("placeholder") or ""
                    nm = await inp.get_attribute("name") or ""
                    id_ = await inp.get_attribute("id") or ""
                    val = await inp.input_value() or ""
                    log.info(f"    input[{idx}] id={id_!r} name={nm!r} placeholder={ph!r} value={val!r}")
                except: pass

            # Try filling date range using input value directly
            # Tarrant date inputs: look for ones with date-like values
            for idx, inp_el in enumerate(all_inputs[:8]):
                try:
                    val = await inp_el.input_value()
                    if re.match(r"\d{1,2}/\d{1,2}/\d{4}", val or ""):
                        if idx == 0 or (val and "1900" in val):
                            await inp_el.click()
                            await inp_el.press("Control+a")
                            await inp_el.type(from_str, delay=50)
                            await asyncio.sleep(0.3)
                            log.info(f"    Filled start date in input[{idx}]: {from_str}")
                        elif "2026" in val or "2025" in val:
                            await inp_el.click()
                            await inp_el.press("Control+a")
                            await inp_el.type(to_str, delay=50)
                            await asyncio.sleep(0.3)
                            log.info(f"    Filled end date in input[{idx}]: {to_str}")
                except: pass

            await page.keyboard.press("Tab")
            await asyncio.sleep(0.5)

            # Doc type filter
            inp = page.locator("input[placeholder*='Filter Document'], #docTypes-input, [aria-label='Filter Document Types']").first
            if await inp.count() > 0:
                await inp.click(); await asyncio.sleep(0.3)
                await inp.type(doc_type[:4], delay=80); await asyncio.sleep(1.5)
                opt = page.locator(f"[role='option']:has-text('{doc_type}')").first
                if await opt.count() > 0:
                    await opt.click()
                else:
                    all_opts = await page.locator("[role='option']").all()
                    for o in all_opts:
                        t = await o.inner_text()
                        if doc_type.lower() in t.lower():
                            await o.click(); break
                    else:
                        await page.keyboard.press("Escape")
                await asyncio.sleep(0.5)

            # Submit
            btn = page.locator("#search-btn, button:has-text('Search'), button[type='submit']").first
            await btn.click()
            await page.wait_for_load_state("networkidle", timeout=30_000)
            await asyncio.sleep(3)

            pn = 1
            while pn <= 20:
                recs = await self._parse_table(page)
                records.extend(recs)
                if recs: log.info(f"    Page {pn}: {len(recs)} records")
                nxt = page.locator("button[aria-label='Next page'], button:has-text('Next')").first
                if await nxt.count() == 0 or not await nxt.is_enabled(): break
                await nxt.click()
                await page.wait_for_load_state("networkidle", timeout=20_000)
                await asyncio.sleep(2)
                pn += 1
        except Exception as exc:
            log.warning(f"  Search error '{doc_type}': {exc}")
        return records

    async def run(self):
        log.info("Launching browser ...")
        all_records=[]; seen=set()

        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True, args=["--no-sandbox","--disable-dev-shm-usage"])
            context = await browser.new_context(
                user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36",
                viewport={"width":1280,"height":900})
            page = await context.new_page()

            try:
                await page.goto("https://tarrant.tx.publicsearch.us/", wait_until="networkidle", timeout=45_000)
                await asyncio.sleep(2)
                log.info(f"Portal: {page.url}")
            except Exception as exc:
                log.error(f"Portal load failed: {exc}"); await browser.close(); return []

            from_str = self.date_from.strftime("%m/%d/%Y")
            to_str   = self.date_to.strftime("%m/%d/%Y")

            for dt in SEARCH_DOC_TYPES:
                recs = await self._search_one(page, dt, from_str, to_str)
                added = 0
                for r in recs:
                    key = r.get("doc_num") or f"{r['doc_type']}{r['filed']}{r['owner']}"
                    if key and key not in seen:
                        seen.add(key); all_records.append(r); added += 1
                log.info(f"  → {added} new for '{dt}' (total: {len(all_records)})")
                await asyncio.sleep(1)

            # TAD address lookup for real person owners
            person_records = [r for r in all_records if r.get("owner") and _is_person(r["owner"]) and not r.get("prop_address")]
            log.info(f"Looking up {len(person_records)} person records on TAD ...")
            for i, r in enumerate(person_records[:50]):
                info = await _lookup_tad(context, r["owner"])
                for f in ["prop_address","prop_city","prop_zip","mail_address","mail_city","mail_state","mail_zip"]:
                    if not r.get(f) and info.get(f):
                        r[f] = info[f]
                log.info(f"  TAD {i+1}/{min(len(person_records),50)}: '{r['owner']}' → addr='{r.get('prop_address','')}' city='{r.get('prop_city','')}' zip='{r.get('prop_zip','')}'")
                await asyncio.sleep(0.5)

            await browser.close()

        all_records = [r for r in all_records if r.get("cat") and r["cat"] != "OTHER"]
        log.info(f"Total: {len(all_records)}")
        return all_records


def _score(r, today, owner_cats):
    score=30; flags=[]; cat=r.get("cat",""); amt=r.get("amount",0.0)
    try: nw=(today-datetime.strptime(r["filed"],"%Y-%m-%d")).days<=7
    except: nw=False
    if cat in ("LP","RELLP"): flags.append("Lis pendens"); score+=10
    if cat=="NOFC": flags.append("Pre-foreclosure"); score+=10
    ou=(r.get("owner") or "").upper()
    cats=owner_cats.get(ou,set())
    if "LP" in cats and "NOFC" in cats: flags.append("LP+FC combo"); score+=20
    if cat in ("JUD","CCJ","DRJUD"): flags.append("Judgment lien"); score+=10
    if cat in ("TAXDEED","LNCORPTX","LNIRS","LNFED"): flags.append("Tax lien"); score+=10
    if cat=="LNMECH": flags.append("Mechanic lien"); score+=10
    if cat in ("LNHOA","LN","MEDLN"): flags.append("Lien"); score+=10
    if cat=="PRO": flags.append("Probate / estate"); score+=10
    if amt>100_000: flags.append("High debt (>$100k)"); score+=15
    elif amt>50_000: flags.append("Significant debt (>$50k)"); score+=10
    if nw: flags.append("New this week"); score+=5
    if r.get("prop_address"): flags.append("Has property address"); score+=5
    if any(kw in ou for kw in ("LLC","INC","CORP","LTD","TRUST","ESTATE")): flags.append("LLC/corp owner"); score+=10
    return min(score,100), list(dict.fromkeys(flags))


GHL_COLS = ["First Name","Last Name","Mailing Address","Mailing City","Mailing State","Mailing Zip",
            "Property Address","Property City","Property State","Property Zip","Lead Type",
            "Document Type","Date Filed","Document Number","Amount/Debt Owed","Seller Score",
            "Motivated Seller Flags","Source","Public Records URL"]

def _split_name(n):
    if not n: return "",""
    if "," in n: p=n.split(",",1); return p[1].strip(),p[0].strip()
    p=n.split(); return (" ".join(p[:-1]),p[-1]) if len(p)>1 else ("",p[0])

def write_ghl_csv(records, path):
    with open(path,"w",newline="",encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=GHL_COLS); w.writeheader()
        for r in records:
            fn,ln = _split_name(r.get("owner",""))
            w.writerow({
                "First Name":fn,"Last Name":ln,
                "Mailing Address":r.get("mail_address",""),"Mailing City":r.get("mail_city",""),
                "Mailing State":r.get("mail_state","TX"),"Mailing Zip":r.get("mail_zip",""),
                "Property Address":r.get("prop_address",""),"Property City":r.get("prop_city","Fort Worth"),
                "Property State":r.get("prop_state","TX"),"Property Zip":r.get("prop_zip",""),
                "Lead Type":r.get("cat_label",""),"Document Type":r.get("doc_type",""),
                "Date Filed":r.get("filed",""),"Document Number":r.get("doc_num",""),
                "Amount/Debt Owed":r.get("amount",""),"Seller Score":r.get("score",0),
                "Motivated Seller Flags":"; ".join(r.get("flags",[])),"Source":"Tarrant County Clerk",
                "Public Records URL":r.get("clerk_url","")
            })
    log.info(f"GHL CSV → {path}")


async def main():
    today    = datetime.utcnow()
    week_ago = today - timedelta(days=7)
    log.info(f"Tarrant County Scraper v1 | {week_ago.date()} → {today.date()}")

    scraper = TarrantScraper(date_from=week_ago, date_to=today)
    records = await scraper.run()

    owner_cats = {}
    for r in records:
        owner_cats.setdefault((r.get("owner") or "").upper(), set()).add(r["cat"])

    enriched = []
    for r in records:
        sc, fl = _score(r, today, owner_cats)
        r["score"] = sc; r["flags"] = fl
        enriched.append(r)

    enriched.sort(key=lambda x: x["score"], reverse=True)
    wa = sum(1 for r in enriched if r.get("prop_address"))

    payload = {
        "fetched_at": today.isoformat()+"Z",
        "source": "Tarrant County Clerk – tarrant.tx.publicsearch.us",
        "date_range": f"{week_ago.date()} to {today.date()}",
        "total": len(enriched), "with_address": wa, "records": enriched
    }
    for d in (DASHBOARD_DIR, DATA_DIR):
        p = d/"records_tarrant.json"
        p.write_text(json.dumps(payload,indent=2,default=str),encoding="utf-8")
        log.info(f"JSON → {p}")

    write_ghl_csv(enriched, DATA_DIR/f"ghl_tarrant_{today.strftime('%Y%m%d')}.csv")
    log.info(f"\n{'='*55}\n  Done. {len(enriched)} records | {wa} with address\n  Top score: {enriched[0]['score'] if enriched else 'N/A'}\n{'='*55}")

if __name__=="__main__":
    asyncio.run(main())
