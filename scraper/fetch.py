#!/usr/bin/env python3
"""
Dallas County, TX — Motivated Seller Lead Scraper v8
- Uses Playwright for both search AND detail pages
- Extracts owner/address from detail pages via browser (bypasses JS challenge)
- Limits detail fetches to 60s timeout per page
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

PORTAL_URL = "https://dallas.tx.publicsearch.us/search/advanced"
DCAD_BULK_URL = "https://www.dcad.org/wp-content/uploads/data/appraisal_data.zip"
DCAD_ALT_URL  = "https://www.dcad.org/data/"

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


class DallasScraper:
    def __init__(self, date_from, date_to):
        self.date_from = date_from
        self.date_to   = date_to

    async def _parse_table(self, page):
        """Log ALL cell values from first row to understand table structure,
        then extract doc_num, doc_type, filed_date from all rows."""
        records = []
        try:
            content = await page.content()
            soup    = BeautifulSoup(content, "lxml")

            # Find result rows
            rows = (soup.select("tr.rt-tr-group") or
                    soup.select("tbody tr") or
                    soup.select("[class*='result-row']"))
            if not rows:
                tbl = soup.find("table")
                if tbl:
                    rows = tbl.find_all("tr")[1:]

            if rows:
                # Log first row structure
                first_cells = [c.get_text(strip=True) for c in rows[0].find_all(["td","th"])]
                log.info(f"    First row cells: {first_cells}")

            for row in rows:
                try:
                    cells = row.find_all(["td","th"])
                    texts = [c.get_text(strip=True) for c in cells]
                    texts = [t for t in texts if t]
                    if len(texts)<2: continue

                    doc_num=""
                    filed_iso=""
                    raw_type=""
                    grantor=""
                    grantee=""
                    amount=0.0
                    link_url=""

                    # Find link
                    a = row.find("a", href=True)
                    if a:
                        h = a["href"]
                        link_url = h if h.startswith("http") else f"https://dallas.tx.publicsearch.us{h}"
                        m = re.search(r"/doc/(\d+)|/results/(\d+)", h)
                        if m:
                            doc_num = m.group(1) or m.group(2)

                    for t in texts:
                        if _is_doc_num(t) and not doc_num:
                            doc_num = t
                        elif _is_date(t) and not filed_iso:
                            try: filed_iso = datetime.strptime(t,"%m/%d/%Y").strftime("%Y-%m-%d")
                            except: pass
                        elif _classify(t) and not raw_type:
                            raw_type = t
                        elif re.match(r"\$[\d,]+",t):
                            amount = _parse_amount(t.replace("$",""))

                    # Use index-based extraction based on known column order:
                    # [0,1,2, GRANTOR, GRANTEE, DOC_TYPE, DATE, DOC_NUM, ??, CITY, LEGAL]
                    # First 3 cols are empty/checkbox. Indices are from raw cells list.
                    raw_cells = [c.get_text(strip=True) for c in cells]
                    # Index-based extraction — confirmed column order from logs:
                    # [0][1][2][3=GRANTOR][4=GRANTEE][5=DOC_TYPE][6=DATE][7=DOC_NUM][8=??][9=CITY][10=LEGAL]
                    prop_city = ""
                    if len(raw_cells) >= 4:
                        g = raw_cells[3].strip()
                        if g and g not in ("N/A", "--/--/--") and not _is_date(g):
                            grantor = g
                    if len(raw_cells) >= 5:
                        g2 = raw_cells[4].strip()
                        if g2 and g2 not in ("N/A", "--/--/--") and not _is_date(g2):
                            grantee = g2
                    if len(raw_cells) >= 10:
                        city = raw_cells[9].strip()
                        if city and city not in ("N/A", "--/--/--"):
                            prop_city = city              if not raw_type: continue
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

    async def _fetch_detail_pw(self, page, doc_num):
        """Use Playwright to load detail page and extract owner + address."""
        info = {"owner":"","grantee":"","prop_address":"","prop_city":"",
                "prop_zip":"","mail_address":"","mail_city":"","mail_state":"TX",
                "mail_zip":"","amount":0.0,"legal":""}
        try:
            url = f"https://dallas.tx.publicsearch.us/doc/{doc_num}"
            await page.goto(url, wait_until="networkidle", timeout=30_000)
            await asyncio.sleep(1)

            content = await page.content()
            soup    = BeautifulSoup(content, "lxml")

            # Extract all text pairs from page
            pairs = {}

            # dt/dd pairs
            for dt in soup.find_all("dt"):
                dd = dt.find_next_sibling("dd")
                if dd: pairs[dt.get_text(strip=True).upper()] = dd.get_text(strip=True)

            # table th/td pairs
            for tr in soup.find_all("tr"):
                th = tr.find("th")
                td = tr.find("td")
                if th and td: pairs[th.get_text(strip=True).upper()] = td.get_text(strip=True)

            # Any labeled divs
            for div in soup.find_all(["div","span"],
                                      class_=re.compile(r"label|field.?name|title|heading", re.I)):
                nxt = div.find_next_sibling(["div","span","p"])
                if nxt: pairs[div.get_text(strip=True).upper()] = nxt.get_text(strip=True)

            # Also try JSON embedded in page
            for script in soup.find_all("script"):
                txt = script.string or ""
                if "parties" in txt.lower() or "grantor" in txt.lower():
                    try:
                        # Find JSON object in script
                        m = re.search(r'\{["\']?parties["\']?\s*:[\s\S]{0,5000}?\}', txt)
                        if m:
                            data = json.loads(m.group())
                            parties = data.get("parties",[])
                            for p in parties:
                                ptype = str(p.get("type","")).upper()
                                name  = p.get("name","").strip()
                                if any(x in ptype for x in ["GRANTOR","OWNER","DEBTOR","SELLER"]) and not info["owner"]:
                                    info["owner"] = name
                                if any(x in ptype for x in ["GRANTEE","BUYER","CREDITOR"]) and not info["grantee"]:
                                    info["grantee"] = name
                    except: pass

            # Window.__data or similar embedded state
            for script in soup.find_all("script"):
                txt = script.string or ""
                if "__NEXT_DATA__" in txt or "window.__data" in txt:
                    try:
                        m = re.search(r'=\s*(\{[\s\S]+?\});?\s*(?:$|\n)', txt)
                        if m:
                            data = json.loads(m.group(1))
                            # Deep search for parties
                            def find_parties(obj):
                                if isinstance(obj, dict):
                                    if "parties" in obj:
                                        return obj["parties"]
                                    for v in obj.values():
                                        r = find_parties(v)
                                        if r: return r
                                elif isinstance(obj, list):
                                    for item in obj:
                                        r = find_parties(item)
                                        if r: return r
                                return None
                            parties = find_parties(data)
                            if parties:
                                for p in parties:
                                    ptype = str(p.get("type","")).upper()
                                    name  = p.get("name","").strip()
                                    if any(x in ptype for x in ["GRANTOR","OWNER","DEBTOR","SELLER"]) and not info["owner"]:
                                        info["owner"] = name
                    except: pass

            # Map pairs to fields
            GRANTOR_K = ["GRANTOR","SELLER","OWNER","DEBTOR","DEFENDANT","FROM"]
            GRANTEE_K = ["GRANTEE","BUYER","CREDITOR","PLAINTIFF","TO"]
            ADDR_K    = ["SITE ADDRESS","PROPERTY ADDRESS","SITUS","SITE ADDR"]
            LEGAL_K   = ["LEGAL DESCRIPTION","LEGAL"]
            AMOUNT_K  = ["AMOUNT","CONSIDERATION","DEBT","BALANCE DUE"]

            for k,v in pairs.items():
                if any(gk in k for gk in GRANTOR_K) and not info["owner"] and v and not _is_date(v):
                    info["owner"] = v
                if any(gk in k for gk in GRANTEE_K) and not info["grantee"] and v:
                    info["grantee"] = v
                if any(ak in k for ak in ADDR_K) and not info["prop_address"] and v:
                    info["prop_address"] = v
                if any(lk in k for lk in LEGAL_K) and not info["legal"] and v:
                    info["legal"] = v
                if any(amk in k for amk in AMOUNT_K) and not info["amount"] and v:
                    info["amount"] = _parse_amount(v)

            # Last resort — look for party section
            if not info["owner"]:
                party_section = soup.find(string=re.compile(r"grantor|debtor|seller|owner", re.I))
                if party_section:
                    container = party_section.parent.parent if party_section.parent else None
                    if container:
                        sibling = party_section.parent.find_next_sibling()
                        if sibling:
                            candidate = sibling.get_text(strip=True)
                            if candidate and not _is_date(candidate) and len(candidate)>3:
                                info["owner"] = candidate

            # Parse address parts from prop_address
            if info["prop_address"] and "," in info["prop_address"]:
                parts = [p.strip() for p in info["prop_address"].split(",")]
                if len(parts)>=2:
                    info["prop_city"] = parts[-2] if len(parts)>=2 else ""
                    m = re.search(r"\d{5}", parts[-1])
                    if m: info["prop_zip"] = m.group()

        except Exception as exc:
            log.debug(f"Detail PW error {doc_num}: {exc}")
        return info

    async def _search_one(self, page, doc_type, from_str, to_str):
        records = []
        try:
            await page.goto(PORTAL_URL, wait_until="networkidle", timeout=45_000)
            await asyncio.sleep(2)

            # Date pickers
            for cal_aria, date_str in [("Open start date calendar", from_str),
                                        ("Open end date calendar",   to_str)]:
                cal = page.locator(f"[aria-label='{cal_aria}']").first
                if await cal.count()>0:
                    await cal.click(); await asyncio.sleep(0.8)
                    inp = page.locator("input[class*='date'],input[aria-label*='start' i],input[aria-label*='end' i]").first
                    if await inp.count()>0:
                        await inp.click(); await inp.press("Control+a")
                        await inp.type(date_str); await asyncio.sleep(0.3)
                    await page.keyboard.press("Escape"); await asyncio.sleep(0.5)

            # Doc type
            inp = page.locator("#docTypes-input,[aria-label='Filter Document Types']").first
            if await inp.count()>0:
                await inp.click(); await asyncio.sleep(0.3)
                await inp.type(doc_type[:4], delay=80); await asyncio.sleep(1.5)
                opt = page.locator(f"[role='option']:has-text('{doc_type}')").first
                if await opt.count()>0:
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
            btn = page.locator("#search-btn,button:has-text('Search'),button[type='submit']").first
            await btn.click()
            await page.wait_for_load_state("networkidle", timeout=30_000)
            await asyncio.sleep(3)

            # Collect pages
            pn = 1
            while pn<=20:
                recs = await self._parse_table(page)
                records.extend(recs)
                if recs: log.info(f"    Page {pn}: {len(recs)} records")
                nxt = page.locator("button[aria-label='Next page'],button:has-text('Next')").first
                if await nxt.count()==0 or not await nxt.is_enabled(): break
                await nxt.click()
                await page.wait_for_load_state("networkidle",timeout=20_000)
                await asyncio.sleep(2)
                pn+=1
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

            # Load portal
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
                added=0
                for r in recs:
                    key = r.get("doc_num") or f"{r['doc_type']}{r['filed']}{r['owner']}"
                    if key and key not in seen:
                        seen.add(key); all_records.append(r); added+=1
                log.info(f"  → {added} new for '{dt}' (total: {len(all_records)})")
                await asyncio.sleep(1)

            # Enrich with detail pages via Playwright (max 30 records to stay within time limit)
            to_enrich = [r for r in all_records if r.get("doc_num") and not r.get("owner")][:30]
            log.info(f"Fetching detail pages for {len(to_enrich)} records via browser ...")
            for i, r in enumerate(to_enrich):
                detail = await self._fetch_detail_pw(page, r["doc_num"])
                for f in ["owner","grantee","prop_address","prop_city","prop_zip",
                           "mail_address","mail_city","mail_state","mail_zip","legal"]:
                    if not r.get(f) and detail.get(f):
                        r[f] = detail[f]
                if not r.get("amount") and detail.get("amount"):
                    r["amount"] = detail["amount"]
                log.info(f"  Detail {i+1}/{len(to_enrich)}: {r['doc_num']} owner='{r.get('owner','')}' addr='{r.get('prop_address','')}'")
                await asyncio.sleep(0.5)

            await browser.close()

        all_records = [r for r in all_records if r.get("cat") and r["cat"]!="OTHER"]
        log.info(f"Total: {len(all_records)}")
        return all_records


class ParcelLookup:
    def __init__(self): self._index={}

    def load(self):
        if not HAS_DBF: log.warning("dbfread not installed"); return
        for url in (DCAD_ALT_URL, DCAD_BULK_URL):
            try:
                resp=requests.get(url,timeout=30)
                if not resp.ok: continue
                if "zip" in resp.headers.get("Content-Type",""):
                    self._load_zip(resp.content); return
                soup=BeautifulSoup(resp.text,"lxml")
                for a in soup.find_all("a",href=True):
                    if a["href"].endswith(".zip"):
                        base="https://www.dcad.org"
                        r2=requests.get(a["href"] if a["href"].startswith("http") else base+a["href"],timeout=120)
                        if r2.ok: self._load_zip(r2.content); return
            except Exception as exc: log.warning(f"DCAD: {exc}")

    def _load_zip(self,raw):
        try:
            import zipfile as zf
            z=zf.ZipFile(_io.BytesIO(raw)); dbfs=[n for n in z.namelist() if n.lower().endswith(".dbf")]
            if not dbfs: return
            tmp=Path("/tmp/dcad.dbf"); tmp.write_bytes(z.read(dbfs[0]))
            for row in DBF(str(tmp),encoding="latin-1",ignore_missing_memofile=True): self._idx(dict(row))
            log.info(f"DCAD: {len(self._index):,} variants")
        except Exception as exc: log.error(f"DBF: {exc}")

    def _idx(self,row):
        owner=(row.get("OWNER") or row.get("OWN1") or "").strip()
        if not owner: return
        info={"prop_address":(row.get("SITE_ADDR") or row.get("SITEADDR") or "").strip(),
              "prop_city":(row.get("SITE_CITY") or "").strip(),"prop_state":"TX",
              "prop_zip":str(row.get("SITE_ZIP") or "").strip(),
              "mail_address":(row.get("ADDR_1") or row.get("MAILADR1") or "").strip(),
              "mail_city":(row.get("CITY") or row.get("MAILCITY") or "").strip(),
              "mail_state":(row.get("STATE") or "TX").strip(),
              "mail_zip":str(row.get("ZIP") or row.get("MAILZIP") or "").strip()}
        for v in _name_variants(owner):
            if v not in self._index: self._index[v]=info

    def lookup(self,owner):
        for v in _name_variants(owner):
            if v in self._index: return self._index[v]
        return {}


def _score(r,today,owner_cats):
    score=30;flags=[];cat=r.get("cat","");amt=r.get("amount",0.0)
    try: nw=(today-datetime.strptime(r["filed"],"%Y-%m-%d")).days<=7
    except: nw=False
    if cat in ("LP","RELLP"): flags.append("Lis pendens");score+=10
    if cat=="NOFC": flags.append("Pre-foreclosure");score+=10
    ou=(r.get("owner") or "").upper()
    cats=owner_cats.get(ou,set())
    if "LP" in cats and "NOFC" in cats: flags.append("LP+FC combo");score+=20
    if cat in ("JUD","CCJ","DRJUD"): flags.append("Judgment lien");score+=10
    if cat in ("TAXDEED","LNCORPTX","LNIRS","LNFED"): flags.append("Tax lien");score+=10
    if cat=="LNMECH": flags.append("Mechanic lien");score+=10
    if cat in ("LNHOA","LN","MEDLN"): flags.append("Lien");score+=10
    if cat=="PRO": flags.append("Probate / estate");score+=10
    if amt>100_000: flags.append("High debt (>$100k)");score+=15
    elif amt>50_000: flags.append("Significant debt (>$50k)");score+=10
    if nw: flags.append("New this week");score+=5
    if r.get("prop_address"): flags.append("Has property address");score+=5
    if any(kw in ou for kw in ("LLC","INC","CORP","LTD","TRUST","ESTATE")): flags.append("LLC/corp owner");score+=10
    return min(score,100),list(dict.fromkeys(flags))


GHL_COLS=["First Name","Last Name","Mailing Address","Mailing City","Mailing State","Mailing Zip","Property Address","Property City","Property State","Property Zip","Lead Type","Document Type","Date Filed","Document Number","Amount/Debt Owed","Seller Score","Motivated Seller Flags","Source","Public Records URL"]

def _split_name(n):
    if not n: return "",""
    if "," in n: p=n.split(",",1); return p[1].strip(),p[0].strip()
    p=n.split(); return (" ".join(p[:-1]),p[-1]) if len(p)>1 else ("",p[0])

def write_ghl_csv(records,path):
    with open(path,"w",newline="",encoding="utf-8") as f:
        w=csv.DictWriter(f,fieldnames=GHL_COLS);w.writeheader()
        for r in records:
            fn,ln=_split_name(r.get("owner",""))
            w.writerow({"First Name":fn,"Last Name":ln,"Mailing Address":r.get("mail_address",""),"Mailing City":r.get("mail_city",""),"Mailing State":r.get("mail_state","TX"),"Mailing Zip":r.get("mail_zip",""),"Property Address":r.get("prop_address",""),"Property City":r.get("prop_city","Dallas"),"Property State":r.get("prop_state","TX"),"Property Zip":r.get("prop_zip",""),"Lead Type":r.get("cat_label",""),"Document Type":r.get("doc_type",""),"Date Filed":r.get("filed",""),"Document Number":r.get("doc_num",""),"Amount/Debt Owed":r.get("amount",""),"Seller Score":r.get("score",0),"Motivated Seller Flags":"; ".join(r.get("flags",[])),"Source":"Dallas County Clerk","Public Records URL":r.get("clerk_url","")})
    log.info(f"GHL CSV → {path}")


async def main():
    today=datetime.utcnow();week_ago=today-timedelta(days=7)
    log.info(f"Dallas County Scraper v8 | {week_ago.date()} → {today.date()}")

    parcel=ParcelLookup();parcel.load()
    scraper=DallasScraper(date_from=week_ago,date_to=today)
    records=await scraper.run()

    owner_cats={}
    for r in records: owner_cats.setdefault((r.get("owner") or "").upper(),set()).add(r["cat"])

    enriched=[]
    for r in records:
        pi=parcel.lookup(r.get("owner",""))
        for k in ["prop_address","prop_city","prop_state","prop_zip","mail_address","mail_city","mail_state","mail_zip"]:
            if not r.get(k) and pi.get(k): r[k]=pi[k]
        sc,fl=_score(r,today,owner_cats);r["score"]=sc;r["flags"]=fl
        enriched.append(r)

    enriched.sort(key=lambda x:x["score"],reverse=True)
    wa=sum(1 for r in enriched if r.get("prop_address"))

    payload={"fetched_at":today.isoformat()+"Z","source":"Dallas County Clerk – dallas.tx.publicsearch.us","date_range":f"{week_ago.date()} to {today.date()}","total":len(enriched),"with_address":wa,"records":enriched}
    for d in (DASHBOARD_DIR,DATA_DIR):
        p=d/"records.json";p.write_text(json.dumps(payload,indent=2,default=str),encoding="utf-8");log.info(f"JSON → {p}")

    write_ghl_csv(enriched,DATA_DIR/f"ghl_export_{today.strftime('%Y%m%d')}.csv")
    log.info(f"\n{'='*55}\n  Done. {len(enriched)} records | {wa} with address\n  Top score: {enriched[0]['score'] if enriched else 'N/A'}\n{'='*55}")

if __name__=="__main__":
    asyncio.run(main())
