# app_v6_openai.py – Paperscout mit Crossref + Semantic Scholar + OpenAlex + OpenAI-Fallback + optionalem TOC-Filter
import os, re, html, json, smtplib, ssl
from email.mime.text import MIMEText
from email.utils import formataddr
import streamlit as st
import pandas as pd
import httpx
from functools import lru_cache
from io import BytesIO
from datetime import date, datetime, timedelta
from typing import List, Optional, Dict, Any
from urllib.parse import quote_plus

# --- SMTP aus Secrets/Env laden (robust, auch wenn keine secrets.toml vorhanden ist) ---
def setup_smtp_from_secrets_or_env():
    # Versuche, st.secrets zu verwenden – aber ohne Exceptions nach außen
    try:
        import streamlit as st  # ist ja ohnehin im Projekt
        secrets_obj = getattr(st, "secrets", None)
        # Versuch, ein Item zu lesen -> löst ggf. StreamlitSecretNotFoundError aus
        try:
            _ = secrets_obj.get("_probe_", None) if hasattr(secrets_obj, "get") else None
        except Exception:
            secrets_obj = None
    except Exception:
        secrets_obj = None

    def read_secret(key: str) -> Optional[str]:
        """Sicheres Lesen eines Keys aus st.secrets (falls vorhanden)."""
        if secrets_obj is None:
            return None
        try:
            # .get existiert bei Secrets-Objekten nicht immer stabil -> try/except
            val = secrets_obj[key]  # kann StreamlitSecretNotFoundError werfen
            val = str(val).strip()
            return val if val else None
        except Exception:
            return None

    def setdef(key: str, default: Optional[str] = None):
        """Setze os.environ[key] aus Secrets -> Env -> Default (ohne zu crashen)."""
        val = read_secret(key)
        if val is None:
            val = os.environ.get(key)
        if val is None:
            val = default
        if val is not None:
            os.environ[key] = str(val)

    # Sinnvolle Defaults + ggf. Override aus Secrets/Env
    setdef("EMAIL_HOST", "smtp.gmail.com")
    setdef("EMAIL_PORT", "587")
    setdef("EMAIL_USE_TLS", "true")
    setdef("EMAIL_USE_SSL", "false")
    setdef("EMAIL_FROM")
    setdef("EMAIL_USER")
    setdef("EMAIL_PASSWORD")
    setdef("EMAIL_SENDER_NAME", "paperscout")

# Aufruf früh im Skript lassen:
setup_smtp_from_secrets_or_env()



# =========================
# App-Konfiguration
# =========================
st.set_page_config(page_title="paperscout UI", layout="wide")

HARDCODED_KEY = "sk-proj..."
HARDCODED_CROSSREF_MAIL = ""
if HARDCODED_KEY:
    os.environ["PAPERSCOUT_OPENAI_API_KEY"] = HARDCODED_KEY
if HARDCODED_CROSSREF_MAIL:
    os.environ["CROSSREF_MAILTO"] = HARDCODED_CROSSREF_MAIL

# =========================
# HTTP Basics
# =========================
def _headers(extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    mailto = os.getenv("CROSSREF_MAILTO") or "you@example.com"
    base = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/json;q=0.9,*/*;q=0.8",
        "Accept-Language": "de-DE,de;q=0.9,en;q=0.8",
        "Referer": "https://www.google.com/",
        "From": mailto,
    }
    if extra:
        base.update(extra)
    return base

def fetch_html(url: str, timeout: float = 25.0) -> Optional[str]:
    try:
        with httpx.Client(timeout=timeout, headers=_headers(), follow_redirects=True) as c:
            r = c.get(url)
            if r.status_code == 403 and "onlinelibrary.wiley.com" in url:
                r = c.get(url, headers=_headers({"Referer": "https://onlinelibrary.wiley.com/"}))
            if r.status_code == 403 and "journals.sagepub.com" in url:
                r = c.get(url, headers=_headers({"Referer": "https://journals.sagepub.com/"}))
            if r.status_code == 403 and "sciencedirect.com" in url:
                r = c.get(url, headers=_headers({"Referer": "https://www.sciencedirect.com/"}))
            if r.status_code == 403 and "journals.aom.org" in url:
                r = c.get(url, headers=_headers({"Referer": "https://journals.aom.org/"}))
            r.raise_for_status()
            return r.text or ""
    except Exception:
        return None

TAG_STRIP = re.compile(r"<[^>]+>")
def _clean_text(s: str) -> str:
    s = html.unescape(s or "")
    s = TAG_STRIP.sub(" ", s)
    s = re.sub(r"\s+", " ", s).strip()
    s = re.sub(r"^(abstract|zusammenfassung)\s*[:\-]\s*", "", s, flags=re.I)
    return s

def parse_date_any(s: Optional[str]) -> Optional[str]:
    if not s: return None
    s = s.strip()
    fmts = ["%Y-%m-%d","%Y/%m/%d","%d %B %Y","%B %Y","%Y-%m","%Y"]
    for f in fmts:
        try: return datetime.strptime(s,f).strftime("%Y-%m-%d")
        except Exception: pass
    m=re.search(r"(\d{4})",s)
    return f"{m.group(1)}-01-01" if m else None

# =========================
# API-Schnittstellen
# =========================
CR_BASE = "https://api.crossref.org"

JOURNAL_ISSN: Dict[str, str] = {
    "The Leadership Quarterly": "1048-9843",
    "Human Relations": "0018-7267",
    "Organization Studies": "0170-8406",
    "Organizational Research Methods": "1094-4281",
    "Journal of Organizational Behavior": "0894-3796",
    "Journal of Management Studies": "0022-2380",
    "Personnel Psychology": "0031-5826",
    "European Management Review": "1740-4754",
    "Organization Science": "1047-7039",
    "Management Science": "0025-1909",
    "Academy of Management Journal": "0001-4273",
    "Zeitschrift für Arbeits- und Organisationspsychologie": "0932-4089",
    "Journal of Applied Psychology": "0021-9010",
    "Journal of Personality and Social Psychology": "0022-3514",
    "Journal of Occupational Health Psychology": "1076-8998",
    "Journal of Management": "0149-2063",
    "Strategic Management Journal": "0143-2095",
}

# =========================
# Crossref – erweiterte Fallbacks (ALT_ISSN + flexibler fetch_crossref_any)
# =========================
ALT_ISSN: Dict[str, List[str]] = {
    "Journal of Applied Psychology": ["1939-1854"],
    "Journal of Personality and Social Psychology": ["1939-1315"],
    "Journal of Occupational Health Psychology": ["1939-1307"],
    "Journal of Management": ["1557-1211"],
    "Human Relations": ["1741-282X"],
    "Personnel Psychology": ["1744-6570"],
    "Journal of Management Studies": ["1467-6486"],
    "European Management Review": ["1740-4762"],
    "Academy of Management Journal": ["1948-0989"],
    "The Leadership Quarterly": ["1873-3409"],
    "Organizational Research Methods": ["1552-7425"],
}

def fetch_crossref_any(journal: str, issn: str, since: str, until: str, rows: int) -> List[Dict[str, Any]]:
    """
    Robustere Crossref-Abfrage:
    - Probiert nacheinander verschiedene Datumsfilter (pub/online/print).
    - Fällt zurück auf Container-Title-Query und ALT_ISSN.
    - Letzter Notanker: ohne Datumsfilter (wir filtern client-seitig).
    - Filtert auf type:journal-article (keine Issue-Infos/Editorials).
    """
    mailto = os.getenv("CROSSREF_MAILTO") or "you@example.com"
    base_filters = [  # Reihenfolge wichtig
        ("from-pub-date", "until-pub-date"),
        ("from-online-pub-date", "until-online-pub-date"),
        ("from-print-pub-date", "until-print-pub-date"),
    ]

    def _mk_urls(_issn: str, with_dates: bool) -> List[str]:
        if with_dates:
            url_list: List[str] = []
            for f_from, f_until in base_filters:
                filt = f"{f_from}:{since},{f_until}:{until},type:journal-article"
                url_list.extend([
                    f"{CR_BASE}/journals/{_issn}/works?filter={filt}&sort=published&order=desc&rows={rows}&mailto={mailto}",
                    f"{CR_BASE}/works?filter=issn:{_issn},{filt}&sort=published&order=desc&rows={rows}&mailto={mailto}",
                ])
            # Container-Title mit Datumsfiltern
            for f_from, f_until in base_filters:
                filt = f"{f_from}:{since},{f_until}:{until},type:journal-article"
                url_list.append(
                    f"{CR_BASE}/works?query.container-title={quote_plus(journal)}&filter={filt}&sort=published&order=desc&rows={rows}&mailto={mailto}"
                )
            return url_list
        else:
            # ohne Datum, aber type-Filter – wir filtern später lokal nach since/until
            return [
                f"{CR_BASE}/journals/{_issn}/works?filter=type:journal-article&sort=published&order=desc&rows={rows}&mailto={mailto}",
                f"{CR_BASE}/works?filter=issn:{_issn},type:journal-article&sort=published&order=desc&rows={rows}&mailto={mailto}",
                f"{CR_BASE}/works?query.container-title={quote_plus(journal)}&filter=type:journal-article&sort=published&order=desc&rows={rows}&mailto={mailto}",
            ]

    issn_candidates = [issn] + ALT_ISSN.get(journal, [])

    urls: List[str] = []
    for issn_try in issn_candidates:
        urls.extend(_mk_urls(issn_try, with_dates=True))
    for issn_try in issn_candidates:
        urls.extend(_mk_urls(issn_try, with_dates=False))

    def _row_from_item(it: Dict[str, Any]) -> Dict[str, Any]:
        issued = None
        dp = (it.get("issued", {}) or {}).get("date-parts", [])
        if dp and dp[0]:
            issued = "-".join(map(str, dp[0]))
        if not issued:
            issued = parse_date_any(it.get("created", {}).get("date-time", "")) or ""
        return {
            "title": " ".join(it.get("title") or []),
            "doi": it.get("DOI", ""),
            "issued": parse_date_any(issued) or "",
            "journal": " ".join(it.get("container-title") or []),
            "authors": ", ".join(
                " ".join([a.get("given", ""), a.get("family", "")]).strip()
                for a in it.get("author", [])
            ),
            "abstract": _clean_text(it.get("abstract", "")),
            "url": it.get("URL", ""),
        }

    def _within(d: str) -> bool:
        try:
            return (since <= d <= until)
        except Exception:
            return True

    for url in urls:
        try:
            with httpx.Client(timeout=30, headers=_headers()) as c:
                r = c.get(url)
                r.raise_for_status()
                items = r.json().get("message", {}).get("items", [])
                if not items:
                    continue
                rows_out = [_row_from_item(it) for it in items]
                # lokale Datumsfilterung, falls ohne from/until
                if "type:journal-article" in url and "from-" not in url:
                    rows_out = [x for x in rows_out if x.get("issued") and _within(x["issued"])]
                if rows_out:
                    return rows_out
        except Exception as e:
            if st.session_state.get("debug_mode"):
                st.error(f"Crossref-Fehler: {e} @ {url}")

    return []

# =========================
# Aktuelles Heft (TOC) – Registry & Tools
# =========================
JOURNAL_REGISTRY: Dict[str, Dict[str, Any]] = {
    # Elsevier / ScienceDirect
    "The Leadership Quarterly": {
        "publisher": "sciencedirect",
        "issues": "https://www.sciencedirect.com/journal/the-leadership-quarterly/issues",
        "journal_slug": "the-leadership-quarterly",
    },
    # SAGE
    "Human Relations": {"publisher": "sage", "toc": "https://journals.sagepub.com/toc/HUMR/current"},
    "Organization Studies": {"publisher": "sage", "toc": "https://journals.sagepub.com/toc/OSS/current"},
    "Organizational Research Methods": {"publisher": "sage", "toc": "https://journals.sagepub.com/toc/ORM/current"},
    # Wiley
    "Journal of Organizational Behavior": {"publisher": "wiley", "toc": "https://onlinelibrary.wiley.com/toc/10991379/current"},
    "Journal of Management Studies": {"publisher": "wiley", "toc": "https://onlinelibrary.wiley.com/toc/14676486/current"},
    "Personnel Psychology": {"publisher": "wiley", "toc": "https://onlinelibrary.wiley.com/toc/17446570/current"},
    # INFORMS
    "Organization Science": {"publisher": "informs", "toc": "https://pubsonline.informs.org/toc/orsc/current"},
    "Management Science": {"publisher": "informs", "toc": "https://pubsonline.informs.org/toc/mnsc/current"},
    # AOM
    "Academy of Management Journal": {"publisher": "aom", "toc": "https://journals.aom.org/toc/amj/current"},
    # Hogrefe
    "Zeitschrift für Arbeits- und Organisationspsychologie": {"publisher": "hogrefe", "toc": "https://econtent.hogrefe.com/toc/zao/current"},
    # APA
    "Journal of Applied Psychology": {"publisher": "apa", "toc": "https://psycnet.apa.org/PsycARTICLES/journal/apl"},
    "Journal of Personality and Social Psychology": {"publisher": "apa", "toc": "https://psycnet.apa.org/PsycARTICLES/journal/psp"},
    "Journal of Occupational Health Psychology": {"publisher": "apa", "toc": "https://psycnet.apa.org/PsycARTICLES/journal/ocp"},
    # SAGE
    "Journal of Management": {"publisher": "sage", "toc": "https://journals.sagepub.com/toc/jom/current"},
    "Strategic Management Journal": {"publisher": "wiley","toc": "https://onlinelibrary.wiley.com/toc/10970266/current"},
}

def _dedupe_keep_order(urls: List[str]) -> List[str]:
    seen: set = set()
    out: List[str] = []
    for u in urls:
        if u not in seen:
            seen.add(u); out.append(u)
    return out

def _links_sciencedirect_issue(html_text: str) -> List[str]:
    hrefs = re.findall(r'href=["\'](?:https?:\/\/www\.sciencedirect\.com)?(\/science\/article\/pii\/[A-Z0-9]+)["\']', html_text, flags=re.I)
    return ["https://www.sciencedirect.com"+h for h in _dedupe_keep_order(hrefs)]

def _pick_latest_sciencedirect_issue(issues_html: str, journal_slug: str) -> Optional[str]:
    m = re.findall(rf'href=["\'](\/journal\/{journal_slug}\/vol\/(\d+)\/issue\/(\d+))["\']', issues_html, flags=re.I)
    if not m:  # Fallback auf /latest
        return f"https://www.sciencedirect.com/journal/{journal_slug}/latest"
    tuples = [(int(v), int(i), p) for (p, v, i) in m]
    tuples.sort(reverse=True)
    return "https://www.sciencedirect.com" + tuples[0][2]

def _links_sage_toc(html_text: str) -> List[str]:
    hrefs = re.findall(r'href=["\'](\/doi\/(?:full|abs|epub)\/[^"\']+)["\']', html_text, flags=re.I)
    hrefs = [h.replace("/abs/","/full/") for h in hrefs]
    return ["https://journals.sagepub.com"+h for h in _dedupe_keep_order(hrefs)]

def _links_wiley_toc(html_text: str) -> List[str]:
    hrefs = re.findall(r'href=["\'](\/doi\/(?:full|abs)\/[^"\']+)["\']', html_text, flags=re.I)
    hrefs = [h.replace("/abs/","/full/") for h in hrefs]
    return ["https://onlinelibrary.wiley.com"+h for h in _dedupe_keep_order(hrefs)]

def _links_informs_toc(html_text: str) -> List[str]:
    hrefs = re.findall(r'href=["\'](\/doi\/(?:full|abs)\/[^"\']+)["\']', html_text, flags=re.I)
    hrefs = [h.replace("/abs/","/full/") for h in hrefs]
    return ["https://pubsonline.informs.org"+h for h in _dedupe_keep_order(hrefs)]

def _links_aom_toc(html_text: str) -> List[str]:
    hrefs = re.findall(r'href=["\'](\/doi\/(?:full|abs)\/[^"\']+)["\']', html_text, flags=re.I)
    hrefs = [h.replace("/abs/","/full/") for h in hrefs]
    return ["https://journals.aom.org"+h for h in _dedupe_keep_order(hrefs)]

def fetch_aom_toc_fallback(journal_name: str) -> List[Dict[str, Any]]:
    """
    Direkter Fallback für AOM-Journale (z.B. Academy of Management Journal),
    wenn Crossref keine Ergebnisse liefert.
    """
    cfg = JOURNAL_REGISTRY.get(journal_name)
    if not cfg or "aom" not in cfg.get("publisher", ""):
        return []

    toc_url = cfg.get("toc")
    html_text = fetch_html(toc_url)
    if not html_text:
        return []

    links = _links_aom_toc(html_text)
    if not links:
        return []

    records: List[Dict[str, Any]] = []
    for url in links:
        html_art = fetch_html(url)
        if not html_art:
            continue
        doi = _extract_doi_from_html_fast(html_art)
        title = re.search(r'<title>(.*?)</title>', html_art, flags=re.I|re.S)
        title = _clean_text(title.group(1)) if title else ""
        abs_text = extract_abstract_from_html_simple(html_art)
        records.append({
            "title": title,
            "doi": doi or "",
            "issued": str(date.today()),
            "journal": journal_name,
            "authors": "",
            "abstract": abs_text or "",
            "url": url
        })
    return records

def _links_hogrefe_toc(html_text: str) -> List[str]:
    hrefs = re.findall(r'href=["\'](\/doi\/(?:full|abs)\/[^"\']+)["\']', html_text, flags=re.I)
    hrefs = [h.replace("/abs/","/full/") for h in hrefs]
    return ["https://econtent.hogrefe.com"+h for h in _dedupe_keep_order(hrefs)]

def _links_apa_toc(html_text: str) -> List[str]:
    hrefs = re.findall(r'href=["\'](https:\/\/psycnet\.apa\.org\/(?:record|fulltext)\/[^"\']+)["\']', html_text, flags=re.I)
    return _dedupe_keep_order(hrefs)

def _fetch_current_issue_links(journal_name: str) -> List[str]:
    """Gibt alle Artikel-Links der aktuellen Ausgabe zurück."""
    cfg = JOURNAL_REGISTRY.get(journal_name)
    if not cfg:
        return []
    pub = cfg["publisher"]

    if pub == "sciencedirect":
        issues_url = cfg["issues"]
        issues_html = fetch_html(issues_url)
        if not issues_html:
            return []
        issue_url = _pick_latest_sciencedirect_issue(issues_html, cfg["journal_slug"])
        toc_html = fetch_html(issue_url) if issue_url else None
        if not toc_html:
            return []
        links = _links_sciencedirect_issue(toc_html)
    else:
        toc_url = cfg["toc"]
        toc_html = fetch_html(toc_url)
        if not toc_html:
            return []
        if pub == "sage":
            links = _links_sage_toc(toc_html)
        elif pub == "wiley":
            links = _links_wiley_toc(toc_html)
        elif pub == "informs":
            links = _links_informs_toc(toc_html)
        elif pub == "aom":
            links = _links_aom_toc(toc_html)
        elif pub == "hogrefe":
            links = _links_hogrefe_toc(toc_html)
        elif pub == "apa":
            links = _links_apa_toc(toc_html)
        else:
            links = []

    return links

# -------------------------
# Crossref / Semantic Scholar / OpenAlex / OpenAI
# -------------------------
def fetch_crossref(issn: str, since: str, until: str, rows: int) -> List[Dict[str, Any]]:
    url=f"{CR_BASE}/journals/{issn}/works?filter=from-pub-date:{since},until-pub-date:{until}&sort=published&order=desc&rows={rows}"
    try:
        with httpx.Client(timeout=30,headers=_headers()) as c:
            r=c.get(url);r.raise_for_status()
            items=r.json().get("message",{}).get("items",[])
    except Exception as e:
        if st.session_state.get("debug_mode"): st.error(f"Crossref-Fehler ({issn}): {e}")
        return []
    out: List[Dict[str, Any]] = []
    for it in items:
        out.append({
            "title":" ".join(it.get("title") or []),
            "doi":it.get("DOI",""),
            "issued":parse_date_any(it.get("created",{}).get("date-time","")) or "",
            "journal":" ".join(it.get("container-title") or []),
            "authors":", ".join(" ".join([a.get("given",""),a.get("family","")]).strip() for a in it.get("author",[])),
            "abstract":_clean_text(it.get("abstract","")),
            "url":it.get("URL","")
        })
    return out

def fetch_semantic(doi:str)->Optional[Dict[str, Any]]:
    api=f"https://api.semanticscholar.org/graph/v1/paper/DOI:{doi}?fields=title,abstract,authors,year,venue,url"
    try:
        r=httpx.get(api,timeout=15)
        if r.status_code!=200:return None
        js=r.json()
        return {
            "title":js.get("title",""),
            "abstract":js.get("abstract",""),
            "authors":", ".join(a.get("name","") for a in js.get("authors",[])),
            "issued":str(js.get("year",""))+"-01-01",
            "journal":js.get("venue",""),
            "url":js.get("url","")
        }
    except Exception:return None

def fetch_openalex(doi:str)->Optional[Dict[str, Any]]:
    api=f"https://api.openalex.org/works/DOI:{doi}"
    try:
        r=httpx.get(api,timeout=15)
        if r.status_code!=200:return None
        js=r.json()
        abs_text=""
        if "abstract_inverted_index" in js:
            abs_text=" ".join(sum(js["abstract_inverted_index"].values(),[]))
        return {
            "title":js.get("title",""),
            "abstract":_clean_text(abs_text),
            "authors":", ".join(a.get("author",{}).get("display_name","") for a in js.get("authorships",[])),
            "issued":str(js.get("publication_year",""))+"-01-01",
            "journal":js.get("host_venue",{}).get("display_name",""),
            "url":js.get("doi","")
        }
    except Exception:return None

def ai_extract_metadata_from_html(html_text:str,model:str)->Optional[Dict[str, Any]]:
    key=os.getenv("PAPERSCOUT_OPENAI_API_KEY") or os.getenv("OPENAI_API_KEY")
    if not key:return None
    try:
        from openai import OpenAI
        client=OpenAI(api_key=key)
        prompt=("Extract JSON with keys {title,doi,authors,issued,journal,abstract}. "
                "Abstract only from given HTML, no guessing. HTML:\n\n")
        resp=client.chat.completions.create(
            model=model,
            messages=[
                {"role":"system","content":"You extract clean metadata from article HTML."},
                {"role":"user","content":prompt+html_text[:100000]}
            ],
            temperature=0,
            response_format={"type":"json_object"}
        )
        data=json.loads(resp.choices[0].message.content)
        for k,v in data.items():
            data[k]=_clean_text(str(v))
        data["issued"]=parse_date_any(data.get("issued","")) or ""
        return data
    except Exception:return None

# -------------------------
# GENERISCHE ABSTRACT-EXTRAKTION AUS HTML (AMJ/Highwire, Wiley, SAGE, APA)
# -------------------------
def extract_abstract_from_html_simple(html_text: str) -> Optional[str]:
    if not html_text:
        return None
    # Meta-Tags (häufigster, sauberster Weg)
    m = re.search(r'<meta[^>]+name=["\']citation_abstract["\'][^>]+content=["\']([^"\']+)["\']', html_text, flags=re.I)
    if m:
        return _clean_text(m.group(1))
    m = re.search(r'<meta[^>]+name=["\']dc\.description["\'][^>]+content=["\']([^"\']+)["\']', html_text, flags=re.I)
    if m:
        return _clean_text(m.group(1))
    m = re.search(r'<meta[^>]+property=["\']og:description["\'][^>]+content=["\']([^"\']+)["\']', html_text, flags=re.I)
    if m:
        return _clean_text(m.group(1))

    # Highwire/AMJ typische Container
    m = re.search(r'<div[^>]+class=["\'][^"\']*hlFld-Abstract[^"\']*["\'][^>]*>(.*?)</div>', html_text, flags=re.I|re.S)
    if m:
        return _clean_text(m.group(1))
    m = re.search(r'<section[^>]+class=["\'][^"\']*abstract[^"\']*["\'][^>]*>(.*?)</section>', html_text, flags=re.I|re.S)
    if m:
        return _clean_text(m.group(1))
    m = re.search(r'<div[^>]+id=["\']abstract["\'][^>]*>(.*?)</div>', html_text, flags=re.I|re.S)
    if m:
        return _clean_text(m.group(1))

    # Fallback: nichts gefunden
    return None

# -------------------------
# ScienceDirect / Elsevier – direkter JSON-Endpoint
# -------------------------
def fetch_sciencedirect_abstract(doi_or_url: str) -> Optional[str]:
    """
    Holt Abstracts direkt von ScienceDirect (Elsevier),
    indem der PII-Endpunkt abgefragt wird.
    """
    m = re.search(r"(S\d{16,})", doi_or_url)
    pii = m.group(1) if m else None
    if not pii:
        html_text = fetch_html(doi_or_url)
        if html_text:
            m = re.search(r'/pii/(S\d{16,})', html_text)
            if m:
                pii = m.group(1)
    if not pii:
        return None

    api_url = f"https://www.sciencedirect.com/sdfe/arp/pii/{pii}"
    try:
        r = httpx.get(api_url, headers=_headers(), timeout=15)
        if r.status_code != 200:
            return None
        js = r.json()
        abstract_html = js.get("abstracts", [{}])[0].get("content", "")
        return _clean_text(abstract_html)
    except Exception:
        return None

# -------------------------
# ROBUSTE TOC-FILTER-TOOLS
# -------------------------
_DOI_RE = re.compile(r'\b10\.\d{4,9}/\S+\b', flags=re.I)
_PII_RE = re.compile(r'(S\d{16,})', flags=re.I)

def _norm_url(u: str) -> str:
    u = (u or "").strip()
    if not u:
        return ""
    # Query/Fragment ab
    u = re.sub(r'[#?].*$', '', u)
    # konsistent lower, trailing slash weg
    return u.rstrip('/').lower()

def _norm_doi(doi_or_url: str) -> str:
    s = (doi_or_url or "").strip()
    if not s:
        return ""
    s = s.replace("https://doi.org/", "").replace("http://doi.org/", "")
    s = s.replace("doi:", "").replace("DOI:", "")
    return s.strip().rstrip('/').lower()

def _extract_doi(text_or_html: str) -> Optional[str]:
    if not text_or_html:
        return None
    m = _DOI_RE.search(text_or_html)
    return _norm_doi(m.group(0)) if m else None

def _extract_pii(text_or_url: str) -> Optional[str]:
    if not text_or_url:
        return None
    m = _PII_RE.search(text_or_url)
    return m.group(1) if m else None

def _extract_doi_from_html_fast(html_text: str) -> Optional[str]:
    """Versucht DOI aus gängigen Meta-Tags zu ziehen (Wiley, SAGE, AOM, INFORMS, APA, Elsevier)."""
    if not html_text:
        return None
    # Häufige Meta-Varianten
    # <meta name="citation_doi" content="10.xxxx/....">
    m = re.search(r'<meta[^>]+name=["\']citation_doi["\'][^>]+content=["\']([^"\']+)["\']', html_text, flags=re.I)
    if m:
        return _norm_doi(m.group(1))
    # <meta property="og:doi" content="10.xxxx/...">
    m = re.search(r'<meta[^>]+property=["\']og:doi["\'][^>]+content=["\']([^"\']+)["\']', html_text, flags=re.I)
    if m:
        return _norm_doi(m.group(1))
    # Fallback: RegEx im Fließtext
    return _extract_doi(html_text)

def _build_current_issue_index(journal_name: str) -> Dict[str, Any]:
    """
    Liefert Sets von DOIs, PIIs und kanonischen URLs für die aktuelle Ausgabe eines Journals.
    Nutzt vorhandene _fetch_current_issue_links + holt bei Bedarf HTML für DOI.
    """
    links = _fetch_current_issue_links(journal_name)
    dois: set = set()
    piis: set = set()
    urls: set = set()

    for link in links:
        link_norm = _norm_url(link)
        if not link_norm:
            continue
        urls.add(link_norm)

        # 1) DOI direkt aus Link, falls enthalten (Wiley/SAGE/AOM/... haben oft /doi/10....)
        doi = _extract_doi(link_norm)

        # 2) ScienceDirect: PII aus Link holen
        pii = _extract_pii(link_norm)

        # 3) Falls DOI noch unbekannt: HTML holen und DOI aus Metatags fischen
        if not doi:
            html_text = fetch_html(link_norm)
            if html_text:
                # PII ggf. aus HTML (Elsevier Artikel-HTML hat /pii/S... im Code)
                if not pii:
                    pii = _extract_pii(html_text)
                # DOI aus Meta
                doi = _extract_doi_from_html_fast(html_text)

        if doi:
            dois.add(doi)
        if pii:
            piis.add(pii)

    return {"dois": dois, "piis": piis, "urls": urls}

# =========================
# Hauptpipeline
# =========================
def collect_all(journal: str, since: str, until: str, rows: int, ai_model: str) -> List[Dict[str, Any]]:
    issn = JOURNAL_ISSN.get(journal)
    if not issn:
        return []

    # 1) Crossref (robuste Fallbacks)
    base = fetch_crossref_any(journal, issn, since, until, rows)
    out: List[Dict[str, Any]] = []

    # 2) AOM-Fallback, falls Crossref leer ist
    if not base:
        cfg = JOURNAL_REGISTRY.get(journal, {})
        if cfg.get("publisher") == "aom" or "academy of management" in journal.lower():
            base = fetch_aom_toc_fallback(journal)

    if not base:
        return []

    for rec in base:
        # 1) Wenn Crossref bereits Abstract hat → aufnehmen
        if rec.get("abstract"):
            out.append(rec)
            continue

        doi = rec.get("doi", "")

        # 2) Semantic Scholar & OpenAlex prüfen
        for fn in (fetch_semantic, fetch_openalex):
            if not doi:
                break
            data = fn(doi)
            if data and data.get("abstract"):
                for k in ["title", "authors", "journal", "issued", "abstract", "url"]:
                    if not rec.get(k):
                        rec[k] = data.get(k)
                break

        # 3) ScienceDirect (Elsevier) Fallback
        if not rec.get("abstract"):
            is_sd_url = "sciencedirect.com" in (rec.get("url","") or "")
            is_sd_journal = JOURNAL_REGISTRY.get(journal, {}).get("publisher") == "sciencedirect"
            if is_sd_url or is_sd_journal:
                abs_text = fetch_sciencedirect_abstract(rec.get("url") or rec.get("doi",""))
                if abs_text:
                    rec["abstract"] = abs_text

        # 4) Direkte HTML-Extraktion
        if not rec.get("abstract") and rec.get("url"):
            html_text = fetch_html(rec["url"])
            if html_text:
                abs_simple = extract_abstract_from_html_simple(html_text)
                if abs_simple:
                    rec["abstract"] = abs_simple

        # 5) KI-Fallback
        if not rec.get("abstract") and rec.get("url"):
            html_text = fetch_html(rec["url"])
            if html_text:
                ai = ai_extract_metadata_from_html(html_text, ai_model)
                if ai:
                    for k in ["title", "authors", "journal", "issued", "abstract", "doi", "url"]:
                        if not rec.get(k) and ai.get(k):
                            rec[k] = ai.get(k)

        out.append(rec)

    # --- DOI normalisieren: immer vollständige https://doi.org/... Links ---
    for r in out:
        d = (r.get("doi") or "").strip()
        if d.startswith("10."):
            r["doi"] = f"https://doi.org/{d}"
        elif d.startswith("http://doi.org/"):
            r["doi"] = "https://" + d[len("http://"):]  # http -> https
        # Falls keine URL vorhanden: URL auf DOI-Link setzen
        if not r.get("url"):
            r["url"] = r.get("doi", "")

    return out


def dedup(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    seen=set();out=[]
    for a in items:
        d=(a.get("doi") or "").lower()
        if d in seen: continue
        seen.add(d); out.append(a)
    return out

def _canon(u: str) -> str:
    return (u or "").strip().rstrip("/").lower()

def filter_to_current_issue(records: List[Dict[str, Any]], journal_name: str) -> List[Dict[str, Any]]:
    """
    Strenger TOC-Match:
    - DOI-Match (bevorzugt)
    - PII-Match (Elsevier)
    - URL-Match (kanonisch, /abs -> /full normalisiert)
    """
    idx = _build_current_issue_index(journal_name)
    if not any(idx.values()):
        return []  # Keine TOC-Daten -> dann fällt der Filter für dieses Journal leer aus

    dois = idx["dois"]
    piis = idx["piis"]
    urls = idx["urls"]

    out: List[Dict[str, Any]] = []
    for r in records:
        url_raw = r.get("url", "") or ""
        doi_raw = r.get("doi", "") or ""

        # Normalize
        url = _norm_url(url_raw)
        doi = _norm_doi(doi_raw)

        # Wiley/SAGE/INFORMS/AOM: /abs/ -> /full/ angleichen
        url_norm = url.replace("/abs/", "/full/")

        # ScienceDirect: PII aus Record-URL oder ggf. DOI/HTML
        pii = _extract_pii(url) or _extract_pii(doi)

        keep = False

        # 1) DOI-Match
        if doi and doi in dois:
            keep = True

        # 2) PII-Match (Elsevier)
        if not keep and pii and pii in piis:
            keep = True

        # 3) URL-Match
        if not keep:
            if url_norm in urls or url in urls:
                keep = True

        if keep:
            out.append(r)

    return out

# =========================
# E-Mail Versand (SMTP)
# =========================
def send_doi_email(to_email: str, dois: List[str]) -> tuple[bool, str]:
    """Sendet eine einfache Textmail mit DOI-Liste via SMTP. Konfiguration über ENV:
    EMAIL_HOST, EMAIL_PORT, EMAIL_USER, EMAIL_PASSWORD, EMAIL_FROM, EMAIL_SENDER_NAME (optional),
    EMAIL_USE_TLS (default True), EMAIL_USE_SSL (False).
    """
    host = os.getenv("EMAIL_HOST")
    port = int(os.getenv("EMAIL_PORT", "587"))
    user = os.getenv("EMAIL_USER")
    password = os.getenv("EMAIL_PASSWORD")
    sender = os.getenv("EMAIL_FROM") or user
    sender_name = os.getenv("EMAIL_SENDER_NAME", "paperscout")
    use_tls = os.getenv("EMAIL_USE_TLS", "true").lower() in ("1","true","yes","y")
    use_ssl = os.getenv("EMAIL_USE_SSL", "false").lower() in ("1","true","yes","y")

    if not (host and port and sender and user and password):
        return False, "SMTP nicht konfiguriert (EMAIL_HOST/PORT/USER/PASSWORD/EMAIL_FROM)."

    body_lines = [
        "Hallo,",
        "",
        "hier ist die Liste der ausgewählten DOIs:",
        "",
        *[f"- {d if d.startswith('10.') else d}" for d in dois],
        "",
        "Viele Grüße",
        sender_name,
    ]
    msg = MIMEText("\n".join(body_lines), _charset="utf-8")
    msg["Subject"] = f"[paperscout] {len(dois)} DOI(s)"
    msg["From"] = formataddr((sender_name, sender))
    msg["To"] = to_email

    try:
        if use_ssl:
            context = ssl.create_default_context()
            with smtplib.SMTP_SSL(host, port, context=context) as server:
                server.login(user, password)
                server.sendmail(sender, [to_email], msg.as_string())
        else:
            with smtplib.SMTP(host, port) as server:
                server.ehlo()
                if use_tls:
                    server.starttls(context=ssl.create_default_context())
                    server.ehlo()
                server.login(user, password)
                server.sendmail(sender, [to_email], msg.as_string())
        return True, "E-Mail gesendet."
    except Exception as e:
        return False, f"E-Mail Versand fehlgeschlagen: {e}"

# =========================
# UI
# =========================
st.title("🕵🏻 paperscout – Journal Service")

# Init Session State für Auswahl
if "selected_dois" not in st.session_state:
    st.session_state["selected_dois"] = set()

col1, col2 = st.columns([2, 1])
with col1:
    st.markdown("### ✅ Journals auswählen")

    journals = sorted(JOURNAL_ISSN.keys())

    # stabiler Key je Journal
    def _chk_key(name: str) -> str:
        return "chk_" + re.sub(r"\W+", "_", name.lower()).strip("_")

    if not journals:
        st.info("Keine Journals gefunden.")
        chosen: List[str] = []
    else:
        st.markdown("**Wähle Journals (Checkboxen):**")

        sel_all_col, _, _ = st.columns([1, 3, 3])
        with sel_all_col:
            select_all_clicked = st.button("Alle auswählen", use_container_width=True)
            deselect_all_clicked = st.button("Alle abwählen", use_container_width=True)

        if select_all_clicked:
            for j in journals:
                st.session_state[_chk_key(j)] = True
        if deselect_all_clicked:
            for j in journals:
                st.session_state[_chk_key(j)] = False

        chosen: List[str] = []
        cols = st.columns(3)

        # ✨ WICHTIG: Keine TOC-Vorprüfung mehr – nichts wird ausgegraut/ausgebaut
        for idx, j in enumerate(journals):
            k = _chk_key(j)
            current_val = st.session_state.get(k, False)
            with cols[idx % 3]:
                if st.checkbox(j, value=current_val, key=k):
                    chosen.append(j)

        st.markdown(f"**{len(chosen)}** ausgewählt")

with col2:
    st.markdown("### ⏰ Zeitraum & Optionen")
    today = date.today()
    since = st.date_input("Seit (inkl.)", value=date(today.year, 1, 1))
    only_current_issue = st.checkbox("Nur aktuelles Heft (TOC-Filter)", value=False)
    st.session_state["only_current_issue"] = only_current_issue
    until = st.date_input("Bis (inkl.)", value=today)

    # 🔁 NEU: „letzte 30 Tage“
    last30 = st.checkbox("Nur letzte 30 Tage (ignoriert 'Seit/Bis')", value=False)
    if last30:
        st.caption(f"Aktiv: Zeitraum { (today - timedelta(days=30)).isoformat() } bis { today.isoformat() }")

    rows = st.number_input("Max. Treffer pro Journal", min_value=5, max_value=200, step=5, value=50)
    debug = st.checkbox("Debug anzeigen", value=False)
    st.session_state["debug_mode"] = debug
    ai_model = st.text_input("OpenAI Modell", value="gpt-4o-mini")
    api_key_input = st.text_input("🔑 OpenAI API-Key", type="password", value="")
    if api_key_input:
        os.environ["PAPERSCOUT_OPENAI_API_KEY"] = api_key_input
        st.caption("API-Key gesetzt.")
    crossref_mail = st.text_input("📧 Crossref Mailto (empfohlen)", value=os.getenv("CROSSREF_MAILTO", ""))
    if crossref_mail:
        os.environ["CROSSREF_MAILTO"] = crossref_mail
        st.caption("Crossref-Mailto gesetzt (bessere Crossref-Ergebnisse).")

    # --- SMTP-Status (ohne Eingabefelder) ---
    with st.expander("✉️ E-Mail Versand (Status)", expanded=False):
        ok = all(os.getenv(k) for k in ["EMAIL_HOST","EMAIL_PORT","EMAIL_USER","EMAIL_PASSWORD","EMAIL_FROM"])
        if ok:
            st.success(f"SMTP konfiguriert für: {os.getenv('EMAIL_FROM')}")
        else:
            st.error("SMTP nicht vollständig konfiguriert. Bitte Secrets/Env setzen.")
        st.caption("Hinweis: Absenderdaten werden aus Secrets/Umgebungsvariablen geladen und nicht im UI angezeigt.")

st.markdown("---")
run = st.button("🚀 Let´s go! Metadaten ziehen")

if run:
    # Falls chosen in diesem Scope nicht existiert
    if "chosen" not in locals():
        chosen = []
    if not chosen:
        st.warning("Bitte mindestens ein Journal auswählen.")
    else:
        st.info("Starte Abruf — Crossref, Semantic Scholar, OpenAlex, KI-Fallback.")

        all_rows: List[Dict[str, Any]] = []
        progress = st.progress(0)
        n = len(chosen)

        # Zeitraum abhängig von der Checkbox bestimmen (30 Tage Option)
        if last30:
            s_since = (today - timedelta(days=30)).isoformat()
            s_until = today.isoformat()
        else:
            s_since, s_until = str(since), str(until)

        for i, j in enumerate(chosen, 1):
            st.write(f"Quelle: {j}")
            rows_j = collect_all(j, s_since, s_until, int(rows), ai_model)

            # Optionaler TOC-Filter nach der Auswahl
            if st.session_state.get("only_current_issue"):
                rows_j = filter_to_current_issue(rows_j, j)

            # Dedup und aufsammeln
            rows_j = dedup(rows_j)
            all_rows.extend(rows_j)

            # Fortschritt aktualisieren
            progress.progress(min(i / max(n, 1), 1.0))

        # --- Nach der Schleife: Anzeigen ---
        progress.empty()
        if not all_rows:
            st.warning("Keine Treffer im gewählten Zeitraum/Journals.")
        else:
            df = pd.DataFrame(all_rows)
            # sinnvolle Spaltenreihenfolge, wo vorhanden
            cols = [c for c in ["title", "doi", "issued", "journal", "authors", "abstract", "url"] if c in df.columns]
            if cols:
                df = df[cols]

            # Ergebnisse puffern + Auswahl zurücksetzen
            st.session_state["results_df"] = df
            st.session_state["selected_dois"] = set()
            st.success(f"{len(df)} Treffer geladen.")

# --- Persistente Ergebnisanzeige, unabhängig vom Button ---
st.markdown("---")
st.subheader("Ergebnisse")

if "results_df" in st.session_state and not st.session_state["results_df"].empty:
    df = st.session_state["results_df"].copy()

    # Helper: DOI/URL -> klickbarer Link
    def _to_http(u: str) -> str:
        if not isinstance(u, str):
            return ""
        u = u.strip()
        if u.startswith("http://doi.org/"):
            return "https://" + u[len("http://"):]
        if u.startswith("http"):
            return u
        if u.startswith("10."):
            return "https://doi.org/" + u
        return u

    # Linkspalte sicherstellen
    if "url" in df.columns:
        df["link"] = df["url"].apply(_to_http)
    elif "doi" in df.columns:
        df["link"] = df["doi"].apply(_to_http)
    else:
        df["link"] = ""

    # Session-State für Auswahl
    if "selected_dois" not in st.session_state:
        st.session_state["selected_dois"] = set()

    if "doi" not in df.columns:
        st.info("Hinweis: Keine DOI-Spalte gefunden – Auswahl/Versand übersprungen.")
        st.dataframe(df, use_container_width=True)
    else:
        # ✅ Größere Checkboxen, leichter zu klicken
        st.markdown("""
        <style>
        div[data-testid="stCheckbox"] input { transform: scale(1.3); }
        </style>
        """, unsafe_allow_html=True)

# --- Persistente Ergebnisanzeige, 1-Spalten-Layout mit Row-Expandern ---
if "results_df" in st.session_state and not st.session_state["results_df"].empty:
    df = st.session_state["results_df"].copy()

    # Helper: DOI/URL -> klickbarer Link
    def _to_http(u: str) -> str:
        if not isinstance(u, str):
            return ""
        u = u.strip()
        if u.startswith("http://doi.org/"):
            return "https://" + u[len("http://"):]
        if u.startswith("http"):
            return u
        if u.startswith("10."):
            return "https://doi.org/" + u
        return u

    # Linkspalte sicherstellen
    if "url" in df.columns:
        df["link"] = df["url"].apply(_to_http)
    elif "doi" in df.columns:
        df["link"] = df["doi"].apply(_to_http)
    else:
        df["link"] = ""

    # Session-State für Auswahl
    if "selected_dois" not in st.session_state:
        st.session_state["selected_dois"] = set()

    # Anzeige-Tabelle vorbereiten
    current_set = set(map(str.lower, filter(None, st.session_state["selected_dois"])))
    df_show = df.copy()

    # Auswahl-Checkbox vorn
    df_show.insert(0, "✓", df_show["doi"].fillna("").astype(str).str.lower().isin(current_set))
    # Row-Expander-Checkbox (pro Zeile aufklappen)
    if "↕︎" not in df_show.columns:
        df_show.insert(1, "↕︎", False)

    # Spaltenreihenfolge: Abstract direkt nach Titel sichtbar lassen
    col_order = [c for c in ["✓", "↕︎", "title", "abstract", "doi", "issued", "journal", "authors", "link"] if c in df_show.columns]

    # Größere Checkboxen für leichteres Anklicken
    st.markdown("""
    <style>
      div[data-testid="stCheckbox"] input { transform: scale(1.2); }
    </style>
    """, unsafe_allow_html=True)

    # --- Haupttabelle (einspaltig)
    edited = st.data_editor(
        df_show,
        use_container_width=True,
        hide_index=True,
        column_order=col_order,
        column_config={
            "✓": st.column_config.CheckboxColumn(
                "Auswahl",
                help="Markiere Einträge, die du per E-Mail als DOI-Liste versenden willst."
            ),
            "↕︎": st.column_config.CheckboxColumn(
                "Aufklappen",
                help="Zeige diese Zeile unter der Tabelle in voller Größe an."
            ),
            "abstract": st.column_config.TextColumn(
                "abstract",
                help="Paper-Abstract",
                max_chars=100000,
                width="large",      # Abstract in der Tabelle breiter
            ),
            "link": st.column_config.LinkColumn("URL", display_text="Öffnen"),
        },
        key="table_editor_singlecol",
    )

    # --- Auswahl DIREKT aus dem Editor lesen
    sel = (
        edited.loc[edited["✓"] == True, "doi"]
        .dropna().astype(str).str.lower().tolist()
        if ("✓" in edited.columns and "doi" in edited.columns) else []
    )
    st.session_state["selected_dois"] = set(sel)

    # --- Ausklappen: alle Zeilen mit ↕︎ == True als Vollansicht rendern
    if "↕︎" in edited.columns:
        expanded_rows = edited.index[edited["↕︎"] == True].tolist()
    else:
        expanded_rows = []

    # Voll-Detail für jede expandierte Zeile
    for idx in expanded_rows:
        row = edited.loc[idx]
        # Normierte DOI
        doi_val = str(row.get("doi", "") or "")
        doi_link = _to_http(doi_val)
        title = row.get("title", "") or "(ohne Titel)"
        journal = row.get("journal", "") or ""
        issued = row.get("issued", "") or ""
        authors = row.get("authors", "") or ""
        link_val = row.get("link", "") or ""
        abstract = row.get("abstract", "") or ""

        st.markdown("---")
        # Checkbox oben auch hier spiegeln (optional), sonst nur Anzeige
        st.markdown(f"### {title}")
        meta_line = " · ".join([x for x in [journal, issued] if x])
        if meta_line:
            st.caption(meta_line)
        if authors:
            st.markdown(f"**Autor:innen:** {authors}")
        if doi_link:
            st.markdown(f"**DOI:** {doi_link}")
        if link_val and link_val != doi_link:
            st.markdown(f"**URL:** {link_val}")
        if abstract:
            st.markdown("**Abstract**")
            st.write(abstract)
        else:
            st.info("Kein Abstract vorhanden.")

    # --- Aktionen unter der Tabelle
    b1, b2, b3 = st.columns([1, 1, 4])
    with b1:
        if st.button("Alles auswählen (sichtbar)"):
            all_vis = set(edited["doi"].dropna().astype(str).str.lower())
            st.session_state["selected_dois"].update(all_vis)
            st.rerun()
    with b2:
        if st.button("Alle abwählen"):
            st.session_state["selected_dois"].clear()
            st.rerun()
    with b3:
        st.caption(f"Aktuell ausgewählt: **{len(st.session_state['selected_dois'])}** DOI(s)")

    st.markdown("### 📧 DOI-Liste per E-Mail senden")
    to_email = st.text_input("E-Mail-Adresse", key="doi_email_to")
    if st.button("DOI-Liste senden"):
        if not st.session_state["selected_dois"]:
            st.warning("Bitte wähle mindestens eine DOI aus.")
        elif not to_email or "@" not in to_email:
            st.warning("Bitte gib eine gültige E-Mail-Adresse ein.")
        else:
            ok, msg = send_doi_email(to_email, sorted(st.session_state["selected_dois"]))
            st.success(msg) if ok else st.error(msg)
else:
    st.info("Noch keine Ergebnisse geladen. Wähle Journals und klicke auf „Let’s go!“")
