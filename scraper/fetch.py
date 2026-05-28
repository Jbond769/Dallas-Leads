#!/usr/bin/env python3
"""
Dallas County, TX — Motivated Seller Lead Scraper v8
- Uses Playwright for search
- DCAD address lookup via owner name search
"""

import asyncio
import csv
import json
import logging
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

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

PORTAL_URL  = "https://dallas.tx.publicsearch.us/search/advanced"
DCAD_SEARCH = "https://www.dallascad.org/searchowner.aspx"

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

def _name_variants(name):
    parts=[p.strip() for p in re.split(r"[,\s]+",name) if p.strip()]
    v={name.upper()}
    if len(parts)>=2:
        v.add(f"{parts[0]} {parts[1]}".upper())
        v.add(f"{parts[1]} {parts[0]}".upper())
        v.add(f"{parts[1]}, {parts[0]}".upper())
    return v

def _is_date(s):
    return bool(re.match(r"^\d{1,2}/\d{1,2}/\d{4}$",s.strip()))

def _is_doc_num(s):
    return bool(re.match(r"^\d{10,}$",s.strip()))

def _is_person(name):
    u = name.upper()
    return not any(t in u for t in SKIP_TERMS)

def _dcad_query(name):
    parts = name.strip().upper().split()
    if not parts: return None
    last  = parts[0]
    first = parts[1] if len(parts) > 1 else ""
    return f"{last} {first}".strip() if first else last


async def _lookup_dcad(context, owner_name):
    """Query DCAD owner search and return address info. Uses a fresh page each call."""
    if not _is_person(owner_name):
        return {}
    query = _dcad_query(owner_name)
    if not query:
        return {}
    page = await context.new_page()
    try:
        await page.goto(DCAD_SEARCH, wait_until="networkidle", timeout=30_000)
        await asyncio.sleep(2)

        inp = page.locator("input[name*='owner' i], input[id*='owner' i], input[type='text']").first
        if await inp.count() == 0:
            log.debug(f"DCAD: no input found for {owner_name}")
            await page.close()
            return {}
        await inp.fill(query)

        btn = page.locator("input[type='submit'], button[type='submit']").first
        await btn.click()
        await page.wait_for_load_state("networkidle", timeout=20_000)
        await asyncio.sleep(2)

        content = await page.content()
        soup = BeautifulSoup(content, "lxml")

        # Find first AcctDetail link
        detail_link = None
        for a in soup.find_all("a", href=True):
            if "AcctDetail" in a["href"]:
                detail_link = a["href"]
                break
        if not detail_link:
            log.info(f"    DCAD no results for '{owner_name}' (query: {query})")
            await page.close()
            return {}

        full_url = detail_link if detail_link.startswith("http") else f"https://www.dallascad.org/{detail_link.lstrip('/')}"
        await page.goto(full_url, wait_until="networkidle", timeout=30_000)
        await asyncio.sleep(2)

        detail_content = await page.content()
        dsoup = BeautifulSoup(detail_content, "lxml")
        all_text = dsoup.get_text(" ", strip=True)
        # (debug snippet removed)
        # Also keep raw HTML for zip extraction (zip may be in attributes/links not in text)
        raw_html = detail_content

        info = {}

        # Normalize non-breaking spaces to regular spaces
        all_text = all_text.replace("\xa0", " ")

        # DCAD format: "Address: 1218  PATRICIA LN Neighborhood: ... Mapsco: 29-A (GARLAND) ..."
        # Strategy 1: grab street after "Address:" stop at Neighborhood/Mapsco/Suite/Bldg
        m = re.search(r"Address:\s*(\d+\s+[A-Z0-9][A-Z0-9 ]{3,}?)(?:\s{2,}|\s+(?:Neighborhood|Mapsco|Suite|Bldg|DCAD|Property))", all_text, re.I)
        if m:
            info["prop_address"] = re.sub(r"\s+", " ", m.group(1)).strip()

        # Strategy 2: scan for street number + words + known suffix
        if not info.get("prop_address"):
            m = re.search(
                r"\b(\d{3,5}\s+(?:[A-Z]+\s+){1,4}(?:LN|DR|ST|AVE|BLVD|RD|WAY|CT|CIR|PL|TRL|PKWY|HWY|LOOP|PASS|XING|TRCE|COVE|CV|RUN|BND|PARK|WALK))\b",
                all_text, re.I)
            if m:
                candidate = re.sub(r"\s+", " ", m.group(1)).strip()
                bad = ("NOTICE","PROTEST","APPRAISAL","REPORT","PROCESS","SYSTEM","ONLINE","CURRENT","ANNUAL")
                if not any(b in candidate.upper() for b in bad):
                    info["prop_address"] = candidate

        # City is in parentheses after Mapsco: "Mapsco: 29-A (GARLAND)" or "Mapsco: 64-G (DALLAS)"
        m_city = re.search(r"Mapsco:\s*[\w\-]+\s+\(([A-Z][A-Z ]+)\)", all_text, re.I)
        if m_city:
            info["prop_city"] = m_city.group(1).strip().title()

        # Zip: search visible text first, then raw HTML (zip may be in href/attributes)
        zip_matches = re.findall(r"\b(7[5-9]\d{3})\b", all_text)
        if not zip_matches:
            zip_matches = re.findall(r"\b(7[5-9]\d{3})\b", raw_html)
        if zip_matches:
            info["prop_zip"]   = zip_matches[0]
            info["prop_state"] = "TX"
        else:
            # Dump 500 chars of raw HTML around the address for diagnosis
            idx = raw_html.find("Patricia") 
            if idx < 0: idx = raw_html.find("PATRICIA")
            if idx < 0: idx = raw_html.find("Village Fair")
            if idx < 0: idx = raw_html.find("VILLAGE")
            if idx >= 0:
                log.info(f"    [HTML DUMP] {raw_html[max(0,idx-50):idx+300]!r}")
            else:
                log.info(f"    [HTML DUMP] address not found in HTML, len={len(raw_html)}")

        # Mailing address — look for owner mailing section
        # Format: "HENRY NYRONE L & VASQUEZ ARIEL C 1218 PATRICIA LN GARLAND, TEXAS  75042"
        # Try to get mail address from owner block if different from situs
        m3 = re.search(r"Owner \(Current \d{4}\)\s+(.+?)\s{3,}", all_text, re.I)
        if m3:
            owner_block = m3.group(1).strip()
            # Extract trailing address from owner block
            ma = re.search(r"(\d+\s+[A-Z0-9 ]+(?:LN|DR|ST|AVE|BLVD|RD|WAY|CT|CIR|PL|TRL)[^\d]*)", owner_block, re.I)
            if ma:
                info["mail_address"] = re.sub(r"\s+", " ", ma.group(1)).strip()

        await page.close()
        return info

    except Exception as exc:
        log.debug(f"DCAD lookup error for '{owner_name}': {exc}")
        try: await page.close()
        except: pass
        return {}


class DallasScraper:
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
                        link_url = h if h.startswith("http") else f"https://dallas.tx.publicsearch.us{h}"
                        m = re.search(r"/doc/(\d+)|/results/(\d+)", h)
                        if m: doc_num = m.group(1) or m.group(2)

                    for t in texts:
                        if _is_doc_num(t) and not doc_num: doc_num = t
                        elif _is_date(t) and not filed_iso:
                            try: filed_iso = datetime.strptime(t,"%m/%d/%Y").strftime("%Y-%m-%d")
                            except: pass
                        elif _classify(t) and not raw_type: raw_type = t
                        elif re.match(r"\$[\d,]+",t): amount = _parse_amount(t.replace("$",""))

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
                        "clerk_url": link_url or (f"https://dallas.tx.publicsearch.us/doc/{doc_num}" if doc_num else ""),
                        "prop_address":"","prop_city": prop_city or "Dallas","prop_state":"TX","prop_zip":"",
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

            for cal_aria, date_str in [("Open start date calendar", from_str),
                                        ("Open end date calendar",   to_str)]:
                cal = page.locator(f"[aria-label='{cal_aria}']").first
                if await cal.count() > 0:
                    await cal.click(); await asyncio.sleep(0.8)
                    inp = page.locator("input[class*='date'],input[aria-label*='start' i],input[aria-label*='end' i]").first
                    if await inp.count() > 0:
                        await inp.click(); await inp.press("Control+a")
                        await inp.type(date_str); await asyncio.sleep(0.3)
                    await page.keyboard.press("Escape"); await asyncio.sleep(0.5)

            inp = page.locator("#docTypes-input,[aria-label='Filter Document Types']").first
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

            btn = page.locator("#search-btn,button:has-text('Search'),button[type='submit']").first
            await btn.click()
            await page.wait_for_load_state("networkidle", timeout=30_000)
            await asyncio.sleep(3)

            pn = 1
            while pn <= 20:
                recs = await self._parse_table(page)
                records.extend(recs)
                if recs: log.info(f"    Page {pn}: {len(recs)} records")
                nxt = page.locator("button[aria-label='Next page'],button:has-text('Next')").first
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
                await page.goto("https://dallas.tx.publicsearch.us/", wait_until="networkidle", timeout=45_000)
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

            # DCAD address lookup — only real people, cap at 50
            person_records = [r for r in all_records if r.get("owner") and _is_person(r["owner"]) and not r.get("prop_address")]
            log.info(f"Looking up {len(person_records)} person records on DCAD ...")
            for i, r in enumerate(person_records[:50]):
                info = await _lookup_dcad(context, r["owner"])
                for f in ["prop_address","prop_city","prop_zip","mail_address","mail_city","mail_state","mail_zip"]:
                    if not r.get(f) and info.get(f):
                        r[f] = info[f]
                log.info(f"  DCAD {i+1}/{min(len(person_records),50)}: '{r['owner']}' → addr='{r.get('prop_address','')}' city='{r.get('prop_city','')}' zip='{r.get('prop_zip','')}'")
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
                "Property Address":r.get("prop_address",""),"Property City":r.get("prop_city","Dallas"),
                "Property State":r.get("prop_state","TX"),"Property Zip":r.get("prop_zip",""),
                "Lead Type":r.get("cat_label",""),"Document Type":r.get("doc_type",""),
                "Date Filed":r.get("filed",""),"Document Number":r.get("doc_num",""),
                "Amount/Debt Owed":r.get("amount",""),"Seller Score":r.get("score",0),
                "Motivated Seller Flags":"; ".join(r.get("flags",[])),"Source":"Dallas County Clerk",
                "Public Records URL":r.get("clerk_url","")
            })
    log.info(f"GHL CSV → {path}")


async def main():
    today    = datetime.utcnow()
    week_ago = today - timedelta(days=7)
    log.info(f"Dallas County Scraper v8 | {week_ago.date()} → {today.date()}")

    scraper = DallasScraper(date_from=week_ago, date_to=today)
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
        "source": "Dallas County Clerk – dallas.tx.publicsearch.us",
        "date_range": f"{week_ago.date()} to {today.date()}",
        "total": len(enriched), "with_address": wa, "records": enriched
    }
    for d in (DASHBOARD_DIR, DATA_DIR):
        p = d/"records.json"
        p.write_text(json.dumps(payload,indent=2,default=str),encoding="utf-8")
        log.info(f"JSON → {p}")

    write_ghl_csv(enriched, DATA_DIR/f"ghl_export_{today.strftime('%Y%m%d')}.csv")
    log.info(f"\n{'='*55}\n  Done. {len(enriched)} records | {wa} with address\n  Top score: {enriched[0]['score'] if enriched else 'N/A'}\n{'='*55}")

if __name__=="__main__":
    asyncio.run(main())
