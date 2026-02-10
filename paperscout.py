# UI-Update: Modernes Design mit CSS-Karten und Tabs.
# LOGIC-RESTORE: API-Key-Logik exakt wie in Version 6 (Hard Environment Set).
# FEATURE: Zentrale Render-Funktion für konsistente Karten (Relevanz, Cluster, Hauptliste).
# FEATURE: Checkboxen und Klapp-Abstracts überall verfügbar.
# FIX: Synchronisierung der Auswahl.

import os, re, html, json, smtplib, ssl, hashlib
from math import ceil
from email.mime.text import MIMEText
from email.utils import formataddr
import streamlit as st
import streamlit.components.v1 as components
import pandas as pd
import httpx
from functools import lru_cache
from io import BytesIO
from datetime import date, datetime, timedelta
from typing import List, Optional, Dict, Any
from urllib.parse import quote_plus

# ==========================================
# 1. API-KEY LOGIK (WIEDERHERGESTELLT VON V6)
# ==========================================
# Dieser Block muss ganz oben stehen, bevor irgendwelche Funktionen definiert werden.
# Er zwingt den Secret-Key in die Umgebungsvariablen, genau wie im alten Code.
# ------------------------------------------
try:
    if "PAPERSCOUT_OPENAI_API_KEY" in st.secrets:
        key_val = str(st.secrets["PAPERSCOUT_OPENAI_API_KEY"]).strip()
        # Entferne eventuelle Anführungszeichen, falls sie fälschlicherweise im Wert stehen
        key_val = key_val.strip('"').strip("'")
        if key_val:
            os.environ["PAPERSCOUT_OPENAI_API_KEY"] = key_val
            # WICHTIG: Auch die Standard-Variable setzen für Libraries
            os.environ["OPENAI_API_KEY"] = key_val
except Exception:
    pass

# Fallback: Falls der User "OPENAI_API_KEY" direkt in den Secrets nutzt
try:
    if "OPENAI_API_KEY" in st.secrets:
        key_val = str(st.secrets["OPENAI_API_KEY"]).strip()
        key_val = key_val.strip('"').strip("'")
        if key_val:
            os.environ["OPENAI_API_KEY"] = key_val
            os.environ["PAPERSCOUT_OPENAI_API_KEY"] = key_val
except Exception:
    pass
# ==========================================


# --- Excel-Engine Detection (xlsxwriter / openpyxl) ---
try:
    import xlsxwriter  # noqa: F401
    _HAS_XLSXWRITER = True
except Exception:
    _HAS_XLSXWRITER = False

try:
    import openpyxl  # noqa: F401
    _HAS_OPENPYXL = True
except Exception:
    _HAS_OPENPYXL = False

def _pick_excel_engine() -> str | None:
    """Bevorzugt xlsxwriter; fällt auf openpyxl zurück; None wenn beides fehlt."""
    if _HAS_XLSXWRITER:
        return "xlsxwriter"
    if _HAS_OPENPYXL:
        return "openpyxl"
    return None

def _stable_sel_key(r: dict, suffix: str) -> str:
    """Erzeugt einen eindeutigen Key für Widgets basierend auf DOI und einem Suffix."""
    # robuste Basis: DOI -> URL -> Titel
    basis = (str(r.get("doi") or "") + "|" +
             str(r.get("url") or "") + "|" +
             str(r.get("title") or "")).lower()
    # kurze, saubere ID
    h = hashlib.sha1(basis.encode("utf-8")).hexdigest()[:12]
    return f"sel_card_{h}_{suffix}"

def _chk_key(name: str) -> str:
    return "chk_" + re.sub(r"\W+", "_", name.lower()).strip("_")

# --- SMTP aus Secrets/Env laden (robust) ---
def setup_smtp_from_secrets_or_env():
    try:
        import streamlit as st
        secrets_obj = getattr(st, "secrets", None)
        try:
            _ = secrets_obj.get("_probe_", None) if hasattr(secrets_obj, "get") else None
        except Exception:
            secrets_obj = None
    except Exception:
        secrets_obj = None

    def read_secret(key: str) -> Optional[str]:
        if secrets_obj is None:
            return None
        try:
            val = secrets_obj[key]
            val = str(val).strip()
            return val if val else None
        except Exception:
            return None

    def setdef(key: str, default: Optional[str] = None):
        val = read_secret(key)
        if val is None:
            val = os.environ.get(key)
        if val is None:
            val = default
        if val is not None:
            os.environ[key] = str(val)

    setdef("EMAIL_HOST", "smtp.gmail.com")
    setdef("EMAIL_PORT", "587")
    setdef("EMAIL_USE_TLS", "true")
    setdef("EMAIL_USE_SSL", "false")
    setdef("EMAIL_FROM")
    setdef("EMAIL_USER")
    setdef("EMAIL_PASSWORD")
    setdef("EMAIL_SENDER_NAME", "paperscout")

setup_smtp_from_secrets_or_env()

# =========================
# App-Konfiguration
# =========================
st.set_page_config(page_title="paperscout UI", layout="wide")

HARDCODED_KEY = ""
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
        base_headers = _headers({
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
            "Upgrade-Insecure-Requests": "1",
        })
        with _http_client(timeout=timeout, headers=base_headers) as c:
            r = c.get(url)
            if r.status_code == 403:
                # Domain-spezifische Referrer als Retry
                domain_ref = None
                if "wiley.com" in url: domain_ref = "https://onlinelibrary.wiley.com/"
                elif "sagepub.com" in url: domain_ref = "https://journals.sagepub.com/"
                elif "sciencedirect.com" in url: domain_ref = "https://www.sciencedirect.com/"
                elif "journals.aom.org" in url: domain_ref = "https://journals.aom.org/"
                if domain_ref:
                    r = c.get(url, headers=_headers({"Referer": domain_ref}))
            if r.status_code in (403, 429):
                alt_headers = dict(base_headers)
                alt_headers["User-Agent"] = (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/117 Safari/537.36"
                )
                r = c.get(url, headers=alt_headers)
            r.raise_for_status()
            return r.text or ""
    except Exception:
        return None

# --- Proxy-Unterstützung (HTTP/HTTPS/SOCKS) ---
def _proxy_dict() -> Optional[dict]:
    p = (st.session_state.get("proxy_url") or
         os.getenv("PAPERSCOUT_PROXY") or "").strip()
    if not p:
        return None
    return {"http": p, "https": p}

def _http_client(timeout: float, headers: dict | None = None) -> httpx.Client:
    return httpx.Client(
        timeout=timeout,
        headers=headers or _headers(),
        follow_redirects=True,
        http2=False,
        proxies=_proxy_dict(),
        cookies=httpx.Cookies(),
    )

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

# --- Text/Trend Utilities (Relevance & Intelligence) ---
STOPWORDS = set("""
a an and are as at be by for from has have in is it its of on or that the to was were will with
about above after again against all am among an any are aren't as at because been before being
below between both but by can't cannot could couldn't did didn't do does doesn't doing don't down
during each few for from further had hadn't has hasn't have haven't having he he'd he'll he's her
here here's hers herself him himself his how how's i i'd i'll i'm i've if in into is isn't it it's
its itself just me more most mustn't my myself no nor not of off on once only or other ought our
ours ourselves out over own same shan't she she'd she'll she's should shouldn't so some such than
that that's the their theirs them themselves then there there's these they they'd they'll they're
they've this those through to too under until up very was wasn't we we'd we'll we're we've were
weren't what what's when when's where where's which while who who's whom why why's with won't would
wouldn't you you'd you'll you're you've your yours yourself yourselves
der die das und ist im in den von mit auf als auch bei für des dem ein eine einer einem einen
wie zu zur zum aus über unter nach vor nicht kein keine einer eines wurden wird werden
""".split())

def _tokenize(text: str) -> List[str]:
    if not text:
        return []
    tokens = re.findall(r"[A-Za-zÄÖÜäöüß\-]{3,}", text.lower())
    return [t for t in tokens if t not in STOPWORDS and not t.isdigit()]

def _top_terms(texts: List[str], n: int = 8) -> List[str]:
    freq: Dict[str, int] = {}
    for t in texts:
        for tok in _tokenize(t):
            freq[tok] = freq.get(tok, 0) + 1
    return [t for t, _ in sorted(freq.items(), key=lambda kv: kv[1], reverse=True)[:n]]

def _why_relevant(query: str, text: str, max_terms: int = 4) -> str:
    q_terms = set(_tokenize(query))
    if not q_terms:
        return ""
    t_terms = _tokenize(text)
    overlap = [t for t in t_terms if t in q_terms]
    if not overlap:
        return ""
    # Keep order but unique
    seen = set()
    uniq = []
    for t in overlap:
        if t in seen:
            continue
        seen.add(t)
        uniq.append(t)
        if len(uniq) >= max_terms:
            break
    return ", ".join(uniq)

def _safe_parse_date(s: str) -> Optional[datetime]:
    try:
        return datetime.strptime(s, "%Y-%m-%d")
    except Exception:
        return None

def _trend_summary(df: pd.DataFrame, recent_days: int = 30) -> Dict[str, Any]:
    if df.empty or "issued" not in df.columns:
        return {}
    dates = [_safe_parse_date(str(d)) for d in df["issued"].dropna().astype(str)]
    dates = [d for d in dates if d]
    if not dates:
        return {}
    ref_date = max(dates)
    recent_start = ref_date - timedelta(days=recent_days)
    prior_start = recent_start - timedelta(days=recent_days)
    recent_mask = df["issued"].astype(str).apply(lambda s: (_safe_parse_date(s) or datetime.min) >= recent_start)
    prior_mask = df["issued"].astype(str).apply(lambda s: prior_start <= (_safe_parse_date(s) or datetime.min) < recent_start)

    def _texts(mask):
        sub = df[mask]
        return (sub.get("abstract", "").fillna("") + " " + sub.get("title", "").fillna("")).tolist()

    recent_terms = _top_terms(_texts(recent_mask), n=8)
    prior_terms = _top_terms(_texts(prior_mask), n=8)
    emerging = [t for t in recent_terms if t not in prior_terms][:6]
    return {
        "recent_start": recent_start.date().isoformat(),
        "recent_end": ref_date.date().isoformat(),
        "recent_terms": recent_terms,
        "prior_terms": prior_terms,
        "emerging": emerging,
    }

# =========================
# API-Schnittstellen
# =========================
CR_BASE = "https://api.crossref.org"

JOURNAL_ISSN: Dict[str, str] = {
    "The Leadership Quarterly": "1048-9843",
    "Human Relations": "0018-7267",
    "Organization Studies": "0170-8406",
    "Organizational Research Methods": "1094-4281",
    "Journal of Leadership and Organizational Studies": "1939-7089",
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

    # NEU:
    "Science": "0036-8075",
    "Nature": "0028-0836",
    "Administrative Science Quarterly": "0001-8392",
    "Management Teaching Review": "2379-2981",
}

ALT_ISSN: Dict[str, List[str]] = {
    "Journal of Applied Psychology": ["1939-1854"],
    "Journal of Personality and Social Psychology": ["1939-1315"],
    "Journal of Leadership and Organizational Studies": ["1939-7089"],
    "Journal of Occupational Health Psychology": ["1939-1307"],
    "Journal of Management": ["1557-1211"],
    "Human Relations": ["1741-282X"],
    "Personnel Psychology": ["1744-6570"],
    "Journal of Management Studies": ["1467-6486"],
    "European Management Review": ["1740-4762"],
    "Academy of Management Journal": ["1948-0989"],
    "The Leadership Quarterly": ["1873-3409"],
    "Organizational Research Methods": ["1552-7425"],

    # NEU:
    "Science": ["1095-9203"],
    "Nature": ["1476-4687"],
    "Administrative Science Quarterly": ["1930-3815"],
}

def fetch_crossref_any(journal: str, issn: str, since: str, until: str, rows: int, query: Optional[str] = None) -> List[Dict[str, Any]]:
    mailto = os.getenv("CROSSREF_MAILTO") or "you@example.com"
    base_filters = [
        ("from-pub-date", "until-pub-date"),
        ("from-online-pub-date", "until-online-pub-date"),
        ("from-print-pub-date", "until-print-pub-date"),
    ]

    q = f"&query.title={quote_plus(query)}" if query else ""

    def _mk_urls(_issn: str, with_dates: bool) -> List[str]:
        if with_dates:
            url_list: List[str] = []
            for f_from, f_until in base_filters:
                filt = f"{f_from}:{since},{f_until}:{until},type:journal-article"
                url_list.extend([
                    f"{CR_BASE}/journals/{_issn}/works?filter={filt}&sort=published&order=desc&rows={rows}&mailto={mailto}{q}",
                    f"{CR_BASE}/works?filter=issn:{_issn},{filt}&sort=published&order=desc&rows={rows}&mailto={mailto}{q}",
                ])
            for f_from, f_until in base_filters:
                filt = f"{f_from}:{since},{f_until}:{until},type:journal-article"
                url_list.append(
                    f"{CR_BASE}/works?query.container-title={quote_plus(journal)}&filter={filt}&sort=published&order=desc&rows={rows}&mailto={mailto}{q}"
                )
            return url_list
        else:
            return [
                f"{CR_BASE}/journals/{_issn}/works?filter=type:journal-article&sort=published&order=desc&rows={rows}&mailto={mailto}{q}",
                f"{CR_BASE}/works?filter=issn:{_issn},type:journal-article&sort=published&order=desc&rows={rows}&mailto={mailto}{q}",
                f"{CR_BASE}/works?query.container-title={quote_plus(journal)}&filter=type:journal-article&sort=published&order=desc&rows={rows}&mailto={mailto}{q}",
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

    j_norm = re.sub(r"\s+", " ", (journal or "")).strip().lower()
    issn_set = set(issn_candidates)

    def _same_journal(it: Dict[str, Any]) -> bool:
        ct = " ".join(it.get("container-title") or [])
        ct_norm = re.sub(r"\s+", " ", ct).strip().lower()
        it_issn = set(it.get("ISSN") or [])
        return (ct_norm == j_norm) or bool(it_issn & issn_set)

    for url in urls:
        try:
            with httpx.Client(timeout=30, headers=_headers()) as c:
                r = c.get(url)
                r.raise_for_status()
                items = r.json().get("message", {}).get("items", [])
                if not items:
                    continue

                items = [it for it in items if _same_journal(it)]
                if not items:
                    continue

                rows_out = [_row_from_item(it) for it in items]

                if "type:journal-article" in url and "from-" not in url:
                    rows_out = [x for x in rows_out if x.get("issued") and _within(x["issued"])]

                if rows_out:
                    return rows_out
        except Exception:
            pass

    return []

# -------------------------
# Crossref / Semantic Scholar / OpenAlex / OpenAI
# -------------------------
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
    # KEY-CHANGE: Hier wieder direkt auf os.environ zugreifen
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
# GENERISCHE ABSTRACT-EXTRAKTION AUS HTML
# -------------------------
def extract_abstract_from_html_simple(html_text: str) -> Optional[str]:
    if not html_text:
        return None
    m = re.search(r'<meta[^>]+name=["\']citation_abstract["\'][^>]+content=["\']([^"\']+)["\']', html_text, flags=re.I)
    if m:
        return _clean_text(m.group(1))
    m = re.search(r'<meta[^>]+name=["\']dc\.description["\'][^>]+content=["\']([^"\']+)["\']', html_text, flags=re.I)
    if m:
        return _clean_text(m.group(1))
    m = re.search(r'<meta[^>]+property=["\']og:description["\'][^>]+content=["\']([^"\']+)["\']', html_text, flags=re.I)
    if m:
        return _clean_text(m.group(1))

    m = re.search(r'<div[^>]+class=["\'][^"\']*hlFld-Abstract[^"\']*["\'][^>]*>(.*?)</div>', html_text, flags=re.I|re.S)
    if m:
        return _clean_text(m.group(1))
    m = re.search(r'<section[^>]+class=["\'][^"\']*abstract[^"\']*["\'][^>]*>(.*?)</section>', html_text, flags=re.I|re.S)
    if m:
        return _clean_text(m.group(1))
    m = re.search(r'<div[^>]+id=["\']abstract["\'][^>]*>(.*?)</div>', html_text, flags=re.I|re.S)
    if m:
        return _clean_text(m.group(1))
    return None

def fetch_sciencedirect_abstract(doi_or_url: str) -> Optional[str]:
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

# =========================
# Hauptpipeline
# =========================
def collect_all(
    journal: str,
    since: str,
    until: str,
    rows: int,
    ai_model: str,
    topic_query: Optional[str] = None,
    options: Optional[Dict[str, bool]] = None,
) -> List[Dict[str, Any]]:
    opts = {
        "use_semantic": True,
        "use_openalex": True,
        "use_html": True,
        "use_ai": True,
        "use_scidir": True,
    }
    if options:
        opts.update(options)
    issn = JOURNAL_ISSN.get(journal)
    if not issn:
        return []

    base = fetch_crossref_any(journal, issn, since, until, rows, query=topic_query)
    out: List[Dict[str, Any]] = []

    if not base:
        return []

    for rec in base:
        if rec.get("abstract"):
            rec["abstract_source"] = "crossref"
            out.append(rec)
            continue

        doi = rec.get("doi", "")

        for fn in (fetch_semantic, fetch_openalex):
            if fn == fetch_semantic and not opts.get("use_semantic", True):
                continue
            if fn == fetch_openalex and not opts.get("use_openalex", True):
                continue
            if not doi:
                break
            data = fn(doi)
            if data and data.get("abstract"):
                rec["abstract_source"] = "semantic" if fn == fetch_semantic else "openalex"
                for k in ["title", "authors", "journal", "issued", "abstract", "url"]:
                    if not rec.get(k):
                        rec[k] = data.get(k)
                break

        if not rec.get("abstract") and opts.get("use_scidir", True):
            is_sd_url = "sciencedirect.com" in (rec.get("url","") or "")
            is_sd_journal = (issn == "1048-9843") 
            
            if is_sd_url or is_sd_journal:
                abs_text = fetch_sciencedirect_abstract(rec.get("url") or rec.get("doi",""))
                if abs_text:
                    rec["abstract"] = abs_text
                    rec["abstract_source"] = "sciencedirect"

        if not rec.get("abstract") and rec.get("url") and opts.get("use_html", True):
            html_text = fetch_html(rec["url"])
            if html_text:
                abs_simple = extract_abstract_from_html_simple(html_text)
                if abs_simple:
                    rec["abstract"] = abs_simple
                    rec["abstract_source"] = "html"

        if not rec.get("abstract") and rec.get("url") and opts.get("use_ai", True):
            html_text = fetch_html(rec["url"])
            if html_text:
                ai = ai_extract_metadata_from_html(html_text, ai_model)
                if ai:
                    for k in ["title", "authors", "journal", "issued", "abstract", "doi", "url"]:
                        if not rec.get(k) and ai.get(k):
                            rec[k] = ai.get(k)
                    if ai.get("abstract"):
                        rec["abstract_source"] = "ai"

        if not rec.get("abstract"):
            rec["abstract_source"] = rec.get("abstract_source") or "none"

        out.append(rec)

    for r in out:
        d = (r.get("doi") or "").strip()
        if d.startswith("10."):
            r["doi"] = f"https://doi.org/{d}"
        elif d.startswith("http://doi.org/"):
            r["doi"] = "https://" + d[len("http://"):]
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

# =========================
# Themencluster mit OpenAI-Embeddings (ohne sklearn)
# =========================
def _get_embedding(text: str, model: str = "text-embedding-3-small") -> List[float]:
    # KEY-CHANGE: Hier wieder direkt auf os.environ zugreifen
    key=os.getenv("PAPERSCOUT_OPENAI_API_KEY") or os.getenv("OPENAI_API_KEY")
    if not key:
        return []
    try:
        from openai import OpenAI
        client = OpenAI(api_key=key)
        # Text etwas begrenzen
        text_short = text[:4000]
        resp = client.embeddings.create(
            model=model,
            input=text_short
        )
        return list(resp.data[0].embedding)
    except Exception:
        return []

def _kmeans(vectors: List[List[float]], k: int, max_iter: int = 20) -> List[int]:
    import random
    if not vectors or k <= 0:
        return []

    n = len(vectors)
    k = max(1, min(k, n))

    # Zufällige Startzentren
    centers = random.sample(vectors, k)

    labels = [0] * n
    for _ in range(max_iter):
        # Zuweisung
        changed = False
        for i, v in enumerate(vectors):
            dists = [sum((vi - ci) ** 2 for vi, ci in zip(v, c)) for c in centers]
            new_label = dists.index(min(dists))
            if new_label != labels[i]:
                labels[i] = new_label
                changed = True

        if not changed:
            break

        # Neue Zentren
        new_centers: List[List[float]] = []
        for cluster_id in range(k):
            members = [vectors[i] for i, lab in enumerate(labels) if lab == cluster_id]
            if not members:
                new_centers.append(random.choice(vectors))
                continue
            dim = len(members[0])
            avg = [sum(vec[d] for vec in members) / len(members) for d in range(dim)]
            new_centers.append(avg)
        centers = new_centers

    return labels

def _ai_name_cluster(examples: List[str], model: str = "gpt-4o-mini") -> Optional[str]:
    # KEY-CHANGE: Hier wieder direkt auf os.environ zugreifen
    key=os.getenv("PAPERSCOUT_OPENAI_API_KEY") or os.getenv("OPENAI_API_KEY")
    if not key:
        return None

    # Nur ein paar Beispiele und pro Text begrenzen, damit der Prompt klein bleibt
    snippets = [(t or "").strip()[:600] for t in examples[:5] if t and t.strip()]
    if not snippets:
        return None

    try:
        from openai import OpenAI
        client = OpenAI(api_key=key)

        joined = "\n\n---\n\n".join(snippets)

        system_msg = (
            "Du bist eine wissenschaftliche Assistentin, die Themencluster aus "
            "Forschungsartikeln benennt. "
            "Deine Aufgabe ist es, einen sehr kurzen, prägnanten Titel (3–6 Wörter) "
            "für das Thema zu vergeben. Schreibe auf Deutsch, ohne Anführungszeichen."
        )
        user_msg = (
            "Hier sind einige Abstracts oder Titel von Artikeln, die zum selben Themencluster gehören:\n\n"
            f"{joined}\n\n"
            "Gib mir bitte NUR einen kurzen, sprechenden Namen für das Thema (3–6 Wörter, Deutsch), "
            "ohne Anführungszeichen, ohne zusätzliche Erklärung."
        )

        resp = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system_msg},
                {"role": "user", "content": user_msg},
            ],
            temperature=0.2,
        )
        label = (resp.choices[0].message.content or "").strip()
        label = re.sub(r'^[\"“”]+|[\"“”]+$', '', label).strip()
        return label or None
    except Exception:
        return None

def ai_generate_digest(records: List[Dict[str, Any]], model: str = "gpt-4o-mini", lang: str = "Deutsch") -> Optional[str]:
    key = os.getenv("PAPERSCOUT_OPENAI_API_KEY") or os.getenv("OPENAI_API_KEY")
    if not key or not records:
        return None
    try:
        from openai import OpenAI
        client = OpenAI(api_key=key)
        items = []
        for r in records[:12]:
            title = _clean_text(str(r.get("title","")))
            journal = _clean_text(str(r.get("journal","")))
            issued = _clean_text(str(r.get("issued","")))
            abstract = _clean_text(str(r.get("abstract","")))[:900]
            items.append(f"TITLE: {title}\nJOURNAL: {journal}\nDATE: {issued}\nABSTRACT: {abstract}")
        payload = "\n\n---\n\n".join(items)
        system_msg = (
            "You are a research analyst who produces concise, high-signal digests of recent papers."
        )
        user_msg = (
            f"Language: {lang}. Create a digest with these sections:\n"
            "1) Executive summary (4-6 bullets)\n"
            "2) Emerging themes (3 bullets)\n"
            "3) Open questions (3 bullets)\n"
            "4) Recommended papers (5 bullets, include title + one-line why)\n\n"
            f"PAPERS:\n{payload}"
        )
        resp = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system_msg},
                {"role": "user", "content": user_msg},
            ],
            temperature=0.3,
        )
        return (resp.choices[0].message.content or "").strip()
    except Exception:
        return None


def build_clusters_openai(df: pd.DataFrame, k: int = 5, min_docs: int = 5) -> Optional[List[Dict[str, Any]]]:
    if df.empty:
        return None

    texts: List[str] = []
    indices: List[int] = []

    for idx, row in df.iterrows():
        abstract = str(row.get("abstract", "") or "").strip()
        title = str(row.get("title", "") or "").strip()
        text = abstract if len(abstract) > 40 else title
        if len(text) < 20:
            continue
        texts.append(text)
        indices.append(idx)

    if len(texts) < min_docs:
        return None

    embeddings: List[List[float]] = []
    clean_indices: List[int] = []
    clean_texts: List[str] = []

    for txt, idx in zip(texts, indices):
        emb = _get_embedding(txt)
        if emb:
            embeddings.append(emb)
            clean_indices.append(idx)
            clean_texts.append(txt)

    if len(embeddings) < min_docs:
        return None

    k = max(2, min(k, len(embeddings)))
    labels = _kmeans(embeddings, k=k)

    clusters: List[Dict[str, Any]] = []
    for cluster_id in range(k):
        member_positions = [i for i, lab in enumerate(labels) if lab == cluster_id]
        if not member_positions:
            continue
        member_indices = [clean_indices[i] for i in member_positions]
        sample_text = clean_texts[member_positions[0]]
        clusters.append(
            {
                "cluster_id": cluster_id,
                "label": f"Cluster {cluster_id+1}",
                "sample_text": (sample_text[:240] + "...") if len(sample_text) > 240 else sample_text,
                "indices": member_indices,
            }
        )

    if not clusters:
        return None

    # KEY-CHANGE: Hier wieder direkt auf os.environ zugreifen
    key=os.getenv("PAPERSCOUT_OPENAI_API_KEY") or os.getenv("OPENAI_API_KEY")
    if key:
        # Mapping Index -> Text, damit wir pro Cluster die Beispiele holen können
        idx_to_text = {idx: txt for idx, txt in zip(clean_indices, clean_texts)}

        for cluster in clusters:
            ex_texts = [idx_to_text.get(i, "") for i in cluster.get("indices", [])]
            ex_texts = [t for t in ex_texts if t]
            if not ex_texts:
                continue
            ai_label = _ai_name_cluster(ex_texts)
            if ai_label:
                cluster["label"] = f"Cluster {cluster['cluster_id']+1}: {ai_label}"
                
    return clusters

# =========================
# Relevanz-Rating mit OpenAI-Embeddings (Berechnung & UI)
# =========================
def _to_http(u: str) -> str:
    if not isinstance(u, str): return ""
    u = u.strip()
    if u.startswith("http://doi.org/"): return "https://" + u[len("http://"):]
    if u.startswith("http"): return u
    if u.startswith("10."): return "https://doi.org/" + u
    return u

def _cosine_sim(v1: List[float], v2: List[float]) -> float:
    if not v1 or not v2 or len(v1) != len(v2):
        return 0.0
    num = sum(a * b for a, b in zip(v1, v2))
    den1 = sum(a * a for a in v1) ** 0.5
    den2 = sum(b * b for b in v2) ** 0.5
    if den1 == 0 or den2 == 0:
        return 0.0
    return num / (den1 * den2)

def compute_relevance_scores(
    df: pd.DataFrame,
    query_text: str,
    min_text_len: int = 30,
    model: str = "text-embedding-3-small",
) -> Optional[pd.Series]:
    query_text = (query_text or "").strip()
    if not query_text:
        return None

    q_emb = _get_embedding(query_text, model=model)
    if not q_emb:
        return None

    scores: Dict[int, float] = {}
    for idx, row in df.iterrows():
        abstract = str(row.get("abstract", "") or "").strip()
        title = str(row.get("title", "") or "").strip()
        text = abstract if len(abstract) >= min_text_len else title
        
        if len(text) < min_text_len:
            scores[idx] = 0.0
            continue

        emb = _get_embedding(text, model=model)
        if not emb:
            scores[idx] = 0.0
            continue

        sim = _cosine_sim(q_emb, emb)
        sim = max(sim, 0.0)
        scores[idx] = round(sim * 100, 1)

    return pd.Series(scores, name="relevance_score") if scores else None

def compute_relevance_scores_multi(
    df: pd.DataFrame,
    queries: List[Dict[str, Any]],
    min_text_len: int = 30,
    model: str = "text-embedding-3-small",
) -> Optional[pd.Series]:
    clean = [(q.get("text","").strip(), float(q.get("weight", 1.0))) for q in queries if q.get("text","").strip()]
    if not clean:
        return None
    # Weighted query embedding
    emb_sum = None
    weight_sum = 0.0
    for text, w in clean:
        emb = _get_embedding(text, model=model)
        if not emb:
            continue
        if emb_sum is None:
            emb_sum = [0.0] * len(emb)
        for i, v in enumerate(emb):
            emb_sum[i] += v * w
        weight_sum += w
    if not emb_sum or weight_sum == 0:
        return None
    q_emb = [v / weight_sum for v in emb_sum]

    scores: Dict[int, float] = {}
    for idx, row in df.iterrows():
        abstract = str(row.get("abstract", "") or "").strip()
        title = str(row.get("title", "") or "").strip()
        text = abstract if len(abstract) >= min_text_len else title
        if len(text) < min_text_len:
            scores[idx] = 0.0
            continue
        emb = _get_embedding(text, model=model)
        if not emb:
            scores[idx] = 0.0
            continue
        sim = _cosine_sim(q_emb, emb)
        sim = max(sim, 0.0)
        scores[idx] = round(sim * 100, 1)
    return pd.Series(scores, name="relevance_score") if scores else None

def add_signal_scores(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty or "issued" not in df.columns:
        return df
    dates = []
    for d in df["issued"].astype(str).tolist():
        dt = _safe_parse_date(d)
        dates.append(dt)
    valid_dates = [d for d in dates if d]
    if not valid_dates:
        return df
    ref_date = max(valid_dates)
    days_ago = []
    for d in dates:
        if not d:
            days_ago.append(None)
        else:
            days_ago.append((ref_date - d).days)
    max_days = max([d for d in days_ago if d is not None] or [1])
    recency_scores = []
    for d in days_ago:
        if d is None:
            recency_scores.append(0.0)
        else:
            recency_scores.append(round((1 - (d / max_days)) * 100, 1))
    df["days_ago"] = days_ago
    if "relevance_score" in df.columns:
        rel = df["relevance_score"].fillna(0.0)
        df["signal_score"] = (rel * 0.6 + pd.Series(recency_scores) * 0.4).round(1)
    else:
        df["signal_score"] = pd.Series(recency_scores).round(1)
    return df


# =========================
# E-Mail Versand (SMTP) - JETZT MIT HTML-DESIGN
# =========================
def send_doi_email(
    to_email: str,
    records: List[Dict[str, Any]],
    sender_display: Optional[str] = None
) -> tuple[bool, str]:
    import smtplib
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText
    
    host = os.getenv("EMAIL_HOST")
    port = int(os.getenv("EMAIL_PORT", "587"))
    user = os.getenv("EMAIL_USER")
    password = os.getenv("EMAIL_PASSWORD")
    sender_addr = os.getenv("EMAIL_FROM") or user
    default_name = os.getenv("EMAIL_SENDER_NAME", "paperscout")
    use_tls = os.getenv("EMAIL_USE_TLS", "true").lower() in ("1","true","yes","y")
    use_ssl = os.getenv("EMAIL_USE_SSL", "false").lower() in ("1","true","yes","y")

    if not (host and port and sender_addr and user and password):
        return False, "SMTP nicht konfiguriert."

    display_name = (sender_display or "").strip() or default_name

    # --- HTML Tabellen-Inhalt generieren (modernes Design) ---
    table_rows = ""
    for i, rec in enumerate(records):
        title = html.escape(str(rec.get("title", "(ohne Titel)")))
        authors = html.escape(str(rec.get("authors", "Autor:innen unbekannt")))
        journal = html.escape(str(rec.get("journal", "Journal unbekannt")))
        issued = html.escape(str(rec.get("issued", "")))
        doi_url = str(rec.get("doi", ""))
        
        table_rows += f"""
        <tr>
            <td style="padding: 10px 0;">
                <div style="border:1px solid #e7e7ec; border-radius:14px; padding:16px; background:#ffffff;">
                    <div style="font-weight:700; color:#101217; font-size:16px; margin-bottom:6px;">{title}</div>
                    <div style="font-size:13px; color:#3a3f4b; margin-bottom:8px;">{authors}</div>
                    <div style="font-size:12px; color:#6a7282; margin-bottom:12px;">
                        <span style="font-weight:600;">{journal}</span> {f'· {issued}' if issued else ''}
                    </div>
                    <a href="{doi_url}" style="display:inline-block; padding:8px 12px; border-radius:999px; background:#ff6b35; color:#ffffff; text-decoration:none; font-size:12px; font-weight:700;">
                        DOI öffnen
                    </a>
                </div>
            </td>
        </tr>
        """

    # --- Das HTML-Template ---
    html_body = f"""
    <html>
    <body style="margin:0; padding:0; background:#f7f3ef; font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; color:#101217;">
        <div style="max-width: 720px; margin: 24px auto; padding: 0 16px;">
            <div style="border-radius: 18px; overflow: hidden; border:1px solid #e7e7ec; background:#ffffff;">
                <div style="padding: 20px; background: linear-gradient(135deg, #ff6b35, #ff9f2e); color: #ffffff;">
                    <div style="font-size: 20px; font-weight: 800; letter-spacing:-0.02em;">paperscout</div>
                    <div style="font-size: 12px; opacity:0.9;">Research Digest · {len(records)} Artikel</div>
                </div>
                <div style="padding: 20px;">
                    <p style="margin:0 0 10px 0;">Hallo,</p>
                    <p style="margin:0 0 14px 0; color:#3a3f4b;">
                        hier ist deine kuratierte Übersicht der ausgewählten Artikel.
                    </p>
                    <div style="font-size:12px; color:#6a7282; margin-bottom:12px;">
                        Ausgewählt von: <strong>{display_name}</strong>
                    </div>
                    <table style="width:100%; border-collapse: collapse;">
                        {table_rows}
                    </table>
                    <div style="margin-top: 18px; padding-top: 14px; border-top: 1px solid #eeeeee; font-size: 11px; color: #8b92a1;">
                        Gesendet via paperscout · {datetime.now().strftime('%d.%m.%Y %H:%M')}
                    </div>
                </div>
            </div>
        </div>
    </body>
    </html>
    """

    # E-Mail Objekt erstellen
    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"[paperscout] {len(records)} Artikel — {display_name}"
    msg["From"] = formataddr((display_name, sender_addr))
    msg["To"] = to_email

    # Plaintext-Fallback für alte E-Mail-Clients
    text_fallback = f"Hallo,\n\nhier sind {len(records)} Artikel für dich.\n(Bitte HTML-Ansicht aktivieren für das volle Design.)"
    
    msg.attach(MIMEText(text_fallback, "plain"))
    msg.attach(MIMEText(html_body, "html"))

    try:
        if use_ssl:
            context = ssl.create_default_context()
            with smtplib.SMTP_SSL(host, port, context=context) as server:
                server.login(user, password)
                server.sendmail(sender_addr, [to_email], msg.as_string())
        else:
            with smtplib.SMTP(host, port) as server:
                server.ehlo()
                if use_tls:
                    server.starttls(context=ssl.create_default_context())
                    server.ehlo()
                server.login(user, password)
                server.sendmail(sender_addr, [to_email], msg.as_string())
        return True, "E-Mail mit neuem Design gesendet."
    except Exception as e:
        return False, f"E-Mail Versand fehlgeschlagen: {e}"
# =========================
# =========================
# NEUE UI (v3) - JETZT MIT DARK MODE
# =========================
# =========================
st.markdown(
    """
    <style>
    :root {
        --ps-bg: radial-gradient(1200px 700px at 10% -10%, #ffe8c7 0%, rgba(255,232,199,0.0) 55%),
                 radial-gradient(900px 600px at 90% 0%, #d8f0ff 0%, rgba(216,240,255,0.0) 55%),
                 linear-gradient(180deg, #f7f3ef 0%, #f3f6f9 45%, #f7f7fb 100%);
        --ps-ink: #101217;
        --ps-ink-2: #3a3f4b;
        --ps-ink-3: #6a7282;
        --ps-accent: #ff6b35;
        --ps-accent-2: #2d7ff9;
        --ps-card: rgba(255,255,255,0.7);
        --ps-card-border: rgba(16,18,23,0.08);
        --ps-shadow: 0 12px 30px rgba(16,18,23,0.12);
    }
    @media (prefers-color-scheme: dark) {
        :root {
            --ps-bg: radial-gradient(1200px 700px at 10% -10%, #141821 0%, rgba(20,24,33,0.0) 55%),
                     radial-gradient(900px 600px at 90% 0%, #0c1f2e 0%, rgba(12,31,46,0.0) 55%),
                     linear-gradient(180deg, #0b0f16 0%, #0f1520 45%, #0b0f16 100%);
            --ps-ink: #eef2f8;
            --ps-ink-2: #cfd6e6;
            --ps-ink-3: #98a3b8;
            --ps-accent: #ff9b6a;
            --ps-accent-2: #7ab0ff;
            --ps-card: rgba(18,22,32,0.85);
            --ps-card-border: rgba(255,255,255,0.08);
            --ps-shadow: 0 14px 34px rgba(0,0,0,0.45);
        }
    }
    html[data-theme="dark"] {
        --ps-bg: radial-gradient(1200px 700px at 10% -10%, #1b1e2a 0%, rgba(27,30,42,0.0) 55%),
                 radial-gradient(900px 600px at 90% 0%, #102132 0%, rgba(16,33,50,0.0) 55%),
                 linear-gradient(180deg, #0f1117 0%, #111827 45%, #0f1117 100%);
        --ps-ink: #f2f4f8;
        --ps-ink-2: #d0d6e2;
        --ps-ink-3: #9aa3b2;
        --ps-accent: #ff8b5e;
        --ps-accent-2: #6aa5ff;
        --ps-card: rgba(20,24,35,0.75);
        --ps-card-border: rgba(255,255,255,0.08);
        --ps-shadow: 0 12px 30px rgba(0,0,0,0.35);
    }
    @import url('https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@400;500;600;700&family=Manrope:wght@400;500;600&display=swap');
    html, body, [class*="stApp"] {
        background: var(--ps-bg);
        color: var(--ps-ink);
        font-family: 'Manrope', sans-serif;
    }
    h1, h2, h3, h4, h5 {
        font-family: 'Space Grotesk', sans-serif;
        letter-spacing: -0.02em;
    }
    .ps-hero {
        border-radius: 18px;
        padding: 1.4rem 1.6rem;
        background: linear-gradient(135deg, rgba(255,255,255,0.85), rgba(255,255,255,0.6));
        border: 1px solid var(--ps-card-border);
        box-shadow: var(--ps-shadow);
        margin-bottom: 1rem;
    }
    @media (prefers-color-scheme: dark) {
        .ps-hero {
            background: linear-gradient(135deg, rgba(20,24,35,0.95), rgba(14,18,28,0.75));
        }
    }
    html[data-theme="dark"] .ps-hero {
        background: linear-gradient(135deg, rgba(24,28,40,0.9), rgba(20,24,35,0.7));
    }
    .ps-hero-title {
        font-size: 2rem;
        font-weight: 700;
        margin: 0 0 0.2rem 0;
    }
    .ps-hero-sub {
        color: var(--ps-ink-2);
        font-size: 1rem;
        margin: 0;
    }
    .stTabs [data-baseweb="tab"] {
        font-family: 'Space Grotesk', sans-serif;
        font-weight: 600;
    }
    .stButton > button {
        border-radius: 12px;
        border: 1px solid var(--ps-card-border);
        background: linear-gradient(180deg, #ffffff, #f3f6fb);
        box-shadow: 0 6px 14px rgba(16,18,23,0.08);
        transition: transform 0.12s ease, box-shadow 0.12s ease;
    }
    .stButton > button:hover {
        transform: translateY(-1px);
        box-shadow: 0 10px 20px rgba(16,18,23,0.12);
    }
    .stButton > button[kind="primary"] {
        background: linear-gradient(135deg, var(--ps-accent), #ff9f2e);
        color: #fff;
        border: none;
    }
    .stTextInput input, .stTextArea textarea, .stNumberInput input, .stSelectbox select, .stMultiSelect div {
        border-radius: 12px !important;
        border: 1px solid var(--ps-card-border) !important;
        background: rgba(255,255,255,0.85) !important;
        color: var(--ps-ink) !important;
    }
    @media (prefers-color-scheme: dark) {
        .stTextInput input,
        .stTextArea textarea,
        .stNumberInput input,
        .stSelectbox select,
        .stMultiSelect div {
            background: rgba(20,24,35,0.95) !important;
            color: var(--ps-ink) !important;
            border-color: rgba(255,255,255,0.08) !important;
        }
    }
    html[data-theme="dark"] .stTextInput input,
    html[data-theme="dark"] .stTextArea textarea,
    html[data-theme="dark"] .stNumberInput input,
    html[data-theme="dark"] .stSelectbox select,
    html[data-theme="dark"] .stMultiSelect div {
        background: rgba(24,28,40,0.9) !important;
        color: var(--ps-ink) !important;
    }
    /* Checkbox labels (mobile-safe, BaseWeb) */
    div[data-baseweb="checkbox"] * {
        color: var(--ps-ink) !important;
    }
    div[data-baseweb="checkbox"] label span {
        color: var(--ps-ink) !important;
    }
    @media (prefers-color-scheme: dark) {
        div[data-baseweb="checkbox"] * {
            color: var(--ps-ink) !important;
        }
    }
    html[data-theme="dark"] div[data-baseweb="checkbox"] * {
        color: var(--ps-ink) !important;
    }
    .stExpander {
        border-radius: 14px;
        border: 1px solid var(--ps-card-border);
        background: rgba(255,255,255,0.7);
    }
    </style>
    """,
    unsafe_allow_html=True
)

st.markdown(
    """
    <div class="ps-hero">
        <div class="ps-hero-title">🕵🏻‍♀️ Dein paperscout</div>
        <p class="ps-hero-sub">Frische Forschungsartikel, kuratiert in wenigen Sekunden.</p>
    </div>
    """,
    unsafe_allow_html=True
)

# Init Session State für Auswahl
if "selected_dois" not in st.session_state:
    st.session_state["selected_dois"] = set()
if "saved_searches" not in st.session_state:
    st.session_state["saved_searches"] = []
if "collections" not in st.session_state:
    st.session_state["collections"] = {}
if "last_run_df" not in st.session_state:
    st.session_state["last_run_df"] = None
if "embedding_cache" not in st.session_state:
    st.session_state["embedding_cache"] = {}

# --- KORREKTUR: CSS-Block (v3) für Dark Mode ---
# Verwendet jetzt Streamlit CSS-Variablen für dynamische Farben
CARD_STYLE_V3 = """
<style>
    /*
    NEUE THEME-AWARE KARTEN (v3)
    Verwendet Streamlit CSS-Variablen, um sich an Light/Dark-Mode anzupassen.
    */
    .result-card {
        background: var(--ps-card);
        border: 1px solid var(--ps-card-border);
        border-left: 8px solid var(--ps-accent-2);
        border-radius: 16px;
        padding: 1.2rem;
        margin-bottom: 1rem;
        box-shadow: var(--ps-shadow);
        transition: transform 0.15s ease, box-shadow 0.15s ease;
        backdrop-filter: blur(6px);
    }
    .result-card:hover {
        transform: translateY(-3px);
        box-shadow: 0 18px 35px rgba(16,18,23,0.16);
    }
    .result-card h3 {
        color: var(--ps-ink);
        margin-top: 0;
        margin-bottom: 0.25rem;
        font-weight: 700;
    }
    .result-card .meta {
        color: var(--ps-ink-3);
        font-size: 0.9rem;
        margin-bottom: 0.6rem;
    }
    .result-card .authors {
        color: var(--ps-ink-2);
        font-size: 0.95rem;
        font-weight: 600;
    }
    .result-card details {
        margin-top: 1rem;
    }
    .result-card details summary {
        cursor: pointer;
        font-weight: 700;
        color: var(--ps-accent);
        font-size: 0.95rem;
        list-style-type: '✦ ';
    }
    .result-card details[open] summary {
        list-style-type: '▾ ';
    }
    .result-card details > div {
        background: rgba(255,255,255,0.8);
        border-radius: 10px;
        padding: 0.75rem 1rem;
        margin-top: 0.6rem;
        border: 1px solid var(--ps-card-border);
    }
    .result-card details .abstract {
        color: var(--ps-ink-2);
        white-space: pre-wrap;
        font-size: 0.92rem;
        line-height: 1.6;
    }
    .result-card details a {
        color: var(--ps-accent-2);
        text-decoration: none;
        font-weight: 600;
    }
    .result-card details a:hover {
        text-decoration: underline;
    }

    .cluster-card {
        background: rgba(255,255,255,0.7);
        padding: 12px;
        border-radius: 12px;
        margin-bottom: 10px;
        border: 1px solid var(--ps-card-border);
        box-shadow: 0 6px 12px rgba(16,18,23,0.08);
    }
    .ps-chip {
        display: inline-block;
        padding: 0.12rem 0.5rem;
        border-radius: 999px;
        font-size: 0.72rem;
        font-weight: 700;
        background: var(--ps-accent-2);
        color: #fff;
        margin-left: 0.35rem;
    }
    .ps-chip.hot {
        background: var(--ps-accent);
    }
    .ps-callout {
        display: inline-block;
        padding: 0.18rem 0.6rem;
        border-radius: 999px;
        background: linear-gradient(135deg, var(--ps-accent), #ff9f2e);
        color: #fff;
        font-size: 0.72rem;
        font-weight: 700;
        margin-bottom: 0.4rem;
    }
</style>
"""
st.markdown(CARD_STYLE_V3, unsafe_allow_html=True)


# --- Command Center (ohne Tabs) ---
journals = sorted(JOURNAL_ISSN.keys())
today = date.today()

# --- Apply saved search before widgets are created ---
if "preset_to_apply" in st.session_state:
    preset_name = st.session_state.pop("preset_to_apply")
    preset = next((p for p in st.session_state.get("saved_searches", []) if p["name"] == preset_name), None)
    if preset:
        for j in journals:
            st.session_state[_chk_key(j)] = j in preset.get("journals", [])
        st.session_state["since_input"] = datetime.strptime(preset["since"], "%Y-%m-%d").date()
        st.session_state["until_input"] = datetime.strptime(preset["until"], "%Y-%m-%d").date()
        st.session_state["last7_input"] = preset.get("last7", False)
        st.session_state["last30_input"] = preset.get("last30", False)
        st.session_state["last1_input"] = preset.get("last1", False)
        st.session_state["rows_input"] = preset.get("rows", 100)
        st.session_state["ai_model_input"] = preset.get("ai_model", "gpt-4.1")
        st.session_state["topic_query_input"] = preset.get("topic_query", "")
        st.session_state["relevance_query_input"] = preset.get("relevance_query", "")
        st.session_state["brief_lang"] = preset.get("brief_lang", "Deutsch")

st.markdown("## Command Center")

with st.expander("🧭 Scope & Journals", expanded=True):
    with st.container(border=True):
        st.markdown("### 🧭 Scope & Journals")
        journal_filter = st.text_input("Journal suchen (Filter)", value="", key="journal_filter_input")

        sel_all_col, desel_all_col = st.columns([1, 1])
        with sel_all_col:
            select_all_clicked = st.button("Alle auswählen", use_container_width=True)
        with desel_all_col:
            deselect_all_clicked = st.button("Alle abwählen", use_container_width=True)

        if select_all_clicked:
            for j in journals:
                st.session_state[_chk_key(j)] = True
        if deselect_all_clicked:
            for j in journals:
                st.session_state[_chk_key(j)] = False

        filtered = [j for j in journals if journal_filter.lower().strip() in j.lower()] if journal_filter.strip() else journals
        cols = st.columns(2)
        for idx, j in enumerate(filtered):
            k = _chk_key(j)
            current_val = st.session_state.get(k, False)
            with cols[idx % 2]:
                if st.checkbox(j, value=current_val, key=k):
                    pass

        chosen = [j for j in journals if st.session_state.get(_chk_key(j), False)]
        st.markdown(f"**{len(chosen)}** Journal(s) ausgewählt.")
        st.session_state["chosen_journals"] = chosen

with st.expander("🗓️ Zeitfenster", expanded=True):
    with st.container(border=True):
        st.markdown("### 🗓️ Zeitfenster")
        date_col1, date_col2, date_col3 = st.columns(3)
        with date_col1:
            if "since_input" not in st.session_state:
                st.session_state["since_input"] = date(today.year, 1, 1)
            since = st.date_input("Seit (inkl.)", value=st.session_state["since_input"], key="since_input")
        with date_col2:
            if "until_input" not in st.session_state:
                st.session_state["until_input"] = today
            until = st.date_input("Bis (inkl.)", value=st.session_state["until_input"], key="until_input")
        with date_col3:
            st.markdown("<br>", unsafe_allow_html=True)
            last30 = st.checkbox("Letzte 30 Tage", value=False, key="last30_input")
            last7 = st.checkbox("Letzte 7 Tage", value=False, key="last7_input")
            last1 = st.checkbox("Letzter Tag", value=False, key="last1_input")
        if last30:
            st.caption(f"Aktiv: {(today - timedelta(days=30)).isoformat()} bis {today.isoformat()}")
        if last7:
            st.caption(f"Aktiv: {(today - timedelta(days=7)).isoformat()} bis {today.isoformat()}")
        if last1:
            st.caption(f"Aktiv: {(today - timedelta(days=1)).isoformat()} bis {today.isoformat()}")

with st.expander("🎯 Ziel & Fokus", expanded=True):
    with st.container(border=True):
        st.markdown("<div class='ps-callout'>Empfohlen</div>", unsafe_allow_html=True)
        st.markdown("### 🎯 Ziel & Fokus")
        st.caption("Optionaler Fokustext für Relevanz-Rating & Briefing.")
        st.text_area(
            "Forschungsinteresse",
            value=st.session_state.get("relevance_query_input", ""),
            height=120,
            key="relevance_query_input",
        )
        st.caption("Wenn ausgefüllt, werden Ergebnisse automatisch nach Relevanz bewertet und ein Briefing erzeugt.")
        st.selectbox("Briefing-Sprache", ["Deutsch", "English"], index=0, key="brief_lang")

with st.expander("💾 Gespeicherte Suchen", expanded=False):
    with st.container(border=True):
        st.markdown("### 💾 Gespeicherte Suchen")
        ss_cols = st.columns([2, 1, 1])
        with ss_cols[0]:
            save_name = st.text_input("Name", value="")
        with ss_cols[1]:
            if st.button("Speichern", use_container_width=True):
                if not save_name.strip():
                    st.warning("Bitte einen Namen angeben.")
                else:
                    preset = {
                        "name": save_name.strip(),
                        "journals": st.session_state.get("chosen_journals", []),
                        "since": str(st.session_state.get("since_input")),
                        "until": str(st.session_state.get("until_input")),
                        "last7": bool(st.session_state.get("last7_input")),
                        "last30": bool(st.session_state.get("last30_input")),
                        "last1": bool(st.session_state.get("last1_input")),
                        "rows": int(st.session_state.get("rows_input", 100)),
                        "ai_model": st.session_state.get("ai_model_input", "gpt-4.1"),
                        "topic_query": st.session_state.get("topic_query_input", ""),
                        "relevance_query": st.session_state.get("relevance_query_input", ""),
                        "brief_lang": st.session_state.get("brief_lang", "Deutsch"),
                    }
                    st.session_state["saved_searches"] = [p for p in st.session_state["saved_searches"] if p["name"] != preset["name"]]
                    st.session_state["saved_searches"].append(preset)
                    st.success("Gespeichert.")
        with ss_cols[2]:
            if st.session_state["saved_searches"]:
                if st.button("Löschen", use_container_width=True):
                    st.session_state["saved_searches"] = []
                    st.success("Gelöscht.")

        if st.session_state["saved_searches"]:
            names = [p["name"] for p in st.session_state["saved_searches"]]
            pick = st.selectbox("Laden", options=names, index=0)
            if st.button("Anwenden", use_container_width=True):
                st.session_state["preset_to_apply"] = pick
                st.rerun()

with st.expander("🚀 Discovery Mode (optional)", expanded=False):
    with st.container(border=True):
        st.markdown("### 🚀 Discovery Mode")
        mode = st.radio(
            "Modus",
            ["Scout (schnell)", "Focus (balanciert)", "Deep (maximale Abdeckung)"],
            index=1,
            key="discovery_mode",
        )

        if "rows_input" not in st.session_state:
            st.session_state["rows_input"] = 100
        if "ai_model_input" not in st.session_state:
            st.session_state["ai_model_input"] = "gpt-4.1"

        if "use_semantic" not in st.session_state:
            st.session_state["use_semantic"] = True
        if "use_openalex" not in st.session_state:
            st.session_state["use_openalex"] = True
        if "use_html" not in st.session_state:
            st.session_state["use_html"] = True
        if "use_ai" not in st.session_state:
            st.session_state["use_ai"] = False
        if "use_scidir" not in st.session_state:
            st.session_state["use_scidir"] = True

        if st.button("Modus übernehmen"):
            if mode.startswith("Scout"):
                st.session_state["rows_input"] = 60
                st.session_state["use_semantic"] = True
                st.session_state["use_openalex"] = False
                st.session_state["use_html"] = False
                st.session_state["use_ai"] = False
                st.session_state["use_scidir"] = False
            elif mode.startswith("Focus"):
                st.session_state["rows_input"] = 100
                st.session_state["use_semantic"] = True
                st.session_state["use_openalex"] = True
                st.session_state["use_html"] = True
                st.session_state["use_ai"] = False
                st.session_state["use_scidir"] = True
            else:
                st.session_state["rows_input"] = 150
                st.session_state["use_semantic"] = True
                st.session_state["use_openalex"] = True
                st.session_state["use_html"] = True
                st.session_state["use_ai"] = True
                st.session_state["use_scidir"] = True
            st.success("Modus angewendet.")

        rows = st.number_input("Max. Treffer pro Journal", min_value=5, max_value=300, step=5, value=st.session_state["rows_input"], key="rows_input")
        ai_model = st.text_input("OpenAI Modell", value=st.session_state["ai_model_input"], key="ai_model_input")
        max_total = st.slider("Max. Treffer gesamt", min_value=100, max_value=2000, value=800, step=50, key="max_total_input")
        topic_query = st.text_input("Fokus-Keywords (optional, Crossref Filter)", value="", key="topic_query_input")

        st.markdown("**Quellen & Fallbacks**")
        st.checkbox("Semantic Scholar", value=st.session_state["use_semantic"], key="use_semantic")
        st.checkbox("OpenAlex", value=st.session_state["use_openalex"], key="use_openalex")
        st.checkbox("HTML-Abstracts", value=st.session_state["use_html"], key="use_html")
        st.checkbox("AI-Extraktion (Fallback)", value=st.session_state["use_ai"], key="use_ai")
        st.checkbox("ScienceDirect Spezial", value=st.session_state["use_scidir"], key="use_scidir")

with st.expander("🔑 Keys & Netzwerk (optional)", expanded=False):
    with st.container(border=True):
        st.markdown("### 🔑 Keys & Netzwerk")
        api_key_input = st.text_input("OpenAI API-Key", type="password", value="", help="Optional. Wird für KI-Funktionen benötigt.")
        if api_key_input:
            os.environ["PAPERSCOUT_OPENAI_API_KEY"] = api_key_input
            os.environ["OPENAI_API_KEY"] = api_key_input
            st.caption("API-Key für diese Sitzung gesetzt.")

        crossref_mail = st.text_input("Crossref Mailto", value=os.getenv("CROSSREF_MAILTO", ""), help="Empfohlen für stabilere Crossref-API.")
        if crossref_mail:
            os.environ["CROSSREF_MAILTO"] = crossref_mail
            st.caption("Crossref-Mailto gesetzt.")

        proxy_url = st.text_input("Proxy (optional)", value=os.getenv("PAPERSCOUT_PROXY", ""), help="Format: http://user:pass@host:port")
        if proxy_url:
            st.session_state["proxy_url"] = proxy_url.strip()
            st.success("Proxy aktiv.")
        else:
            st.session_state["proxy_url"] = ""

        with st.expander("E-Mail Versand (Status)", expanded=False):
            ok = all(os.getenv(k) for k in ["EMAIL_HOST","EMAIL_PORT","EMAIL_USER","EMAIL_PASSWORD","EMAIL_FROM"])
            if ok:
                st.success(f"SMTP konfiguriert: {os.getenv('EMAIL_FROM')}")
            else:
                st.error("SMTP nicht vollständig konfiguriert.")

st.divider()

# --- Start-Button ---
run_col1, run_col2, run_col3 = st.columns([2, 1, 2])
with run_col2:
    run = st.button("🚀 Let´s go! Metadaten ziehen", use_container_width=True, type="primary")

# Sync relevance query from input
st.session_state["relevance_query"] = st.session_state.get("relevance_query_input", "")

if run:
    if not chosen:
        st.warning("Bitte mindestens ein Journal auswählen.")
    else:
        st.info("Starte Abruf — Crossref, Semantic Scholar, OpenAlex, Fallbacks...")

        # Vorherige Ergebnisse merken für "Compare Runs"
        st.session_state["last_run_df"] = st.session_state.get("results_df", None)

        all_rows: List[Dict[str, Any]] = []
        progress = st.progress(0, "Starte...")
        n = len(chosen)
        if last7:
            s_since = (today - timedelta(days=7)).isoformat()
            s_until = today.isoformat()
        elif last30:
            s_since = (today - timedelta(days=30)).isoformat()
            s_until = today.isoformat()
        elif last1:
            s_since = (today - timedelta(days=1)).isoformat()
            s_until = today.isoformat()
        else:
            s_since, s_until = str(since), str(until)

        options = {
            "use_semantic": st.session_state.get("use_semantic", True),
            "use_openalex": st.session_state.get("use_openalex", True),
            "use_html": st.session_state.get("use_html", True),
            "use_ai": st.session_state.get("use_ai", True),
            "use_scidir": st.session_state.get("use_scidir", True),
        }

        for i, j in enumerate(chosen, 1):
            progress.progress(min(i / max(n, 1), 1.0), f"({i}/{n}) Verarbeite: {j}")
            rows_j = collect_all(
                j,
                s_since,
                s_until,
                int(rows),
                ai_model,
                topic_query=st.session_state.get("topic_query_input","").strip() or None,
                options=options,
            )
            rows_j = dedup(rows_j)
            all_rows.extend(rows_j)

        progress.empty()
        status_box = st.empty()
        status_box.info("Finalisiere Ergebnisse: Deduplizierung & Aufbereitung …")
        if not all_rows:
            st.warning("Keine Treffer im gewählten Zeitraum/Journals gefunden.")
            status_box.empty()
        else:
            # globale Deduplizierung + Limit
            status_box.info("Bereinige und aggregiere Treffer …")
            all_rows = dedup(all_rows)
            max_total_val = int(st.session_state.get("max_total_input", 0) or 0)
            if max_total_val > 0:
                all_rows = all_rows[:max_total_val]

            status_box.info("Baue Ergebnis-DataFrame …")
            df = pd.DataFrame(all_rows)
            cols = [c for c in ["title", "doi", "issued", "journal", "authors", "abstract", "url", "abstract_source"] if c in df.columns]
            if cols:
                df = df[cols]

            # --- Auto-Relevanz & Auto-Briefing (One-Step Workflow) ---
            rel_query = (st.session_state.get("relevance_query_input", "") or "").strip()
            rel_key = os.getenv("PAPERSCOUT_OPENAI_API_KEY") or os.getenv("OPENAI_API_KEY")
            if rel_query:
                if rel_key:
                    status_box.info("Berechne Relevanz (Embeddings) …")
                    min_len = int(st.session_state.get("relevance_min_text_len", 30) or 30)
                    rel_series = compute_relevance_scores(
                        df,
                        rel_query,
                        min_text_len=min_len,
                    )
                    if rel_series is not None:
                        df["relevance_score"] = rel_series
                        why_list = []
                        for _, row in df.iterrows():
                            text = (str(row.get("abstract","")) + " " + str(row.get("title",""))).strip()
                            why_list.append(_why_relevant(rel_query, text))
                        df["relevance_why"] = why_list

                        # Auto-Briefing: Top Relevanz
                        status_box.info("Erzeuge Research Brief …")
                        top_n = int(st.session_state.get("brief_n", 8) or 8)
                        top_n = max(3, min(top_n, 12))
                        top_df = df.sort_values("relevance_score", ascending=False).head(top_n)
                        brief_lang = st.session_state.get("brief_lang", "Deutsch")
                        brief = ai_generate_digest(top_df.to_dict(orient="records"), model=ai_model, lang=brief_lang)
                        if brief:
                            st.session_state["research_brief"] = brief
                    else:
                        st.warning("Relevanz konnte nicht automatisch berechnet werden.")
                else:
                    st.info("Relevanz & Briefing übersprungen: Bitte OpenAI API-Key im Command Center setzen.")

            st.session_state["results_df"] = df
            st.session_state["selected_dois"] = set() # Auswahl zurücksetzen
            
            # Alle Checkbox-States löschen/zurücksetzen, falls alte Keys von einem früheren Lauf existieren
            for key in list(st.session_state.keys()):
                if key.startswith("sel_card_"):
                    del st.session_state[key]
            
            # Compare Runs: neue DOIs seit letztem Lauf
            prev = st.session_state.get("last_run_df")
            if isinstance(prev, pd.DataFrame) and not prev.empty:
                prev_dois = set(prev.get("doi", pd.Series(dtype=str)).astype(str).str.lower())
                new_mask = ~df["doi"].astype(str).str.lower().isin(prev_dois)
                st.session_state["new_since_last_run"] = df[new_mask].copy()
            else:
                st.session_state["new_since_last_run"] = None

            status_box.success("Ergebnisse bereit.")
            st.success(f"🎉 {len(df)} Treffer geladen!")

# ================================
# --- NEUE ERGEBNISANZEIGE (v2) ---
# ================================

# --- NEU: Anker für "Hoch" ---
st.markdown("<a id='results_top'></a>", unsafe_allow_html=True) 


# --- KORREKTUR 1 (Sync-Fix): Angepasste Callback-Funktion ---
def toggle_doi(doi, key):
    # Diese Funktion wird *nach* dem Klick ausgeführt.
    # st.session_state[key] enthält jetzt den *neuen* Status.
    is_checked = st.session_state.get(key, False)
    if is_checked:
        st.session_state["selected_dois"].add(doi)
    else:
        st.session_state["selected_dois"].discard(doi)
# --- ENDE KORREKTUR 1 ---


if "results_df" in st.session_state and not st.session_state["results_df"].empty:
    st.divider()
    st.subheader("📚 Ergebnisse")

    
    # --- NEU: Link für "Runter" ---
    st.markdown(
        """
        <style>
            .link-container {
                text-align: right;
                margin-top: -2.5rem; 
                margin-bottom: 1rem;
            }
            .link-container a {
                text-decoration: none;
                font-size: 0.9rem;
            }
        </style>
        <div class="link-container">
            <a href='#actions_bottom'>⬇️ Zum E-Mail Versand springen</a>
        </div>
        """, 
        unsafe_allow_html=True
    )

    # --- Basisdaten (für Analyse + Ergebnisliste) ---
    df = add_signal_scores(st.session_state["results_df"].copy())

    # --- Research Question & Briefing (oben, ohne Karten) ---
    rel_query_display = (st.session_state.get("relevance_query_input", "") or "").strip()
    st.markdown("### Research Question")
    if rel_query_display:
        st.markdown(f"**{rel_query_display}**")
    else:
        st.caption("Noch keine Research Question angegeben.")

    brief_text = st.session_state.get("research_brief", "")
    st.markdown("### Briefing")
    if brief_text:
        st.markdown(brief_text)
    else:
        st.caption("Noch kein Briefing. Starte einen Run mit Research Question und API‑Key.")

    def _to_http(u: str) -> str:
        if not isinstance(u, str): return ""
        u = u.strip()
        if u.startswith("http://doi.org/"): return "https://" + u[len("http://"):]
        if u.startswith("http"): return u
        if u.startswith("10."): return "https://doi.org/" + u
        return u

    if "url" in df.columns:
        df["link"] = df["url"].apply(_to_http)
    elif "doi" in df.columns:
        df["link"] = df["doi"].apply(_to_http)
    else:
        df["link"] = ""

    if "selected_dois" not in st.session_state:
        st.session_state["selected_dois"] = set()
        
    # --- Helper Funktion zum Rendern einer Artikel-Karte mit Checkbox ---
    def render_row_ui(row: pd.Series, unique_suffix: str):
        """
        Rendert eine einzelne Zeile bestehend aus Checkbox (links) und Karte (rechts).
        Die Karte enthält Details und Abstract als Klapptext.
        """
        doi_val = str(row.get("doi", "") or "")
        doi_norm = doi_val.lower()
        link_val = _to_http(row.get("link", "") or doi_val)
        title = row.get("title", "") or "(ohne Titel)"
        journal = row.get("journal", "") or ""
        issued = row.get("issued", "") or ""
        authors = row.get("authors", "") or ""
        relevance = row.get("relevance_score", None)
        signal_score = row.get("signal_score", None)
        days_ago = row.get("days_ago", None)
        why = row.get("relevance_why", "") or ""
        abstract = row.get("abstract", "") or ""
        
        left, right = st.columns([0.07, 0.93])
        
        # Checkbox links
        with left:
            sel_key = _stable_sel_key(row.to_dict(), unique_suffix)
            if doi_norm:
                # Value wird berechnet aus dem globalen State, um Sync zu garantieren
                is_selected = doi_norm in st.session_state["selected_dois"]
                st.checkbox(
                    " ",
                    value=is_selected,
                    key=sel_key,
                    label_visibility="hidden",
                    on_change=toggle_doi,
                    args=(doi_norm, sel_key)
                )

        # Karte rechts
        with right:
            title_safe = html.escape(title)
            authors_safe = html.escape(authors)
            
            meta_parts = [journal, issued]
            # NEU: Relevanz prominent in der Meta-Zeile
            if relevance is not None and relevance != "" and not pd.isna(relevance):
                meta_parts.append(f"<b>Relevanz: {relevance}/100</b>")
            if signal_score is not None and signal_score != "" and not pd.isna(signal_score):
                meta_parts.append(f"<b>Signal: {signal_score}/100</b>")
            
            meta_text = " · ".join([x for x in meta_parts if x])
            # Wir nutzen hier kein html.escape für meta_text komplett, weil wir <b> Tags drin haben wollen
            # Daher müssen die Einzelteile sicher sein. journal/issued sind meist safe, aber zur Sicherheit:
            
            # HTML-Sichere Links
            doi_safe = _to_http(doi_val)
            link_safe = link_val
            doi_val_safe = html.escape(doi_val)
            link_val_safe = html.escape(link_val)

            doi_html = ""
            if doi_val:
                doi_html = '<b>DOI:</b> <a href="' + doi_safe + '" target="_blank">' + doi_val_safe + '</a><br>'
                
            link_html = ""
            if link_val and link_val != doi_safe:
                link_html = '<b>URL:</b> <a href="' + link_safe + '" target="_blank">' + link_val_safe + '</a><br>'

            src = row.get("abstract_source", "") or ""
            src_html = ""
            if src:
                src_html = "<b>Abstract-Quelle:</b> " + html.escape(str(src)) + "<br>"
            
            if why and relevance is not None and not pd.isna(relevance):
                why_html = f"<div class='meta'><b>Warum relevant:</b> {html.escape(str(why))}</div>"
            else:
                why_html = ""

            if abstract:
                abstract_safe = html.escape(abstract)
                abstract_html = '<b>Abstract</b><br><p class="abstract">' + abstract_safe + '</p>'
            else:
                abstract_html = "<i>Kein Abstract vorhanden.</i>"

            chip_html = ""
            if days_ago is not None and not pd.isna(days_ago) and isinstance(days_ago, (int, float)):
                if days_ago <= 7:
                    chip_html += "<span class='ps-chip'>NEW</span>"
            if relevance is not None and not pd.isna(relevance) and float(relevance) >= 80:
                chip_html += "<span class='ps-chip hot'>HOT</span>"

            card_html = (
                '<div class="result-card">'
                f'<h3>{title_safe}{chip_html}</h3>'
                f'<div class="meta">{meta_text}</div>'
                f'<div class="authors">{authors_safe}</div>'
                f'{why_html}'
                '<details>'
                '<summary>Details anzeigen</summary>'
                '<div>' +
                doi_html +
                link_html +
                src_html +
                '<br>' +
                abstract_html +
                '</div>'
                '</details>'
                '</div>'
            )
            st.markdown(card_html, unsafe_allow_html=True)

    def section_card(title: str, desc: str, key: str, default_open: bool = False, accent: bool = False, show_toggle: bool = True):
        if key not in st.session_state:
            st.session_state[key] = default_open
        with st.container(border=True):
            if accent:
                st.markdown("<div class='ps-callout'>Empfohlen</div>", unsafe_allow_html=True)
            st.markdown(f"### {title}")
            st.caption(desc)
            if show_toggle:
                st.toggle("Optionen anzeigen", key=key)
            else:
                st.session_state[key] = True
            body = st.container()
        return st.session_state[key], body

    analysis_open, analysis_body = section_card(
        "Weitere Analysemöglichkeiten ausklappen",
        "Empfehlungen, Cluster, Trends und manuelle Relevanz-Bewertung.",
        "exp_more",
    )
    if analysis_open:
        with analysis_body:
            # --- Compare Runs ---
            new_df = st.session_state.get("new_since_last_run")
            if isinstance(new_df, pd.DataFrame) and not new_df.empty:
                with st.expander(f"🆕 Neu seit letztem Lauf ({len(new_df)})", expanded=False):
                    for r_idx, (_, row) in enumerate(new_df.head(10).iterrows()):
                        render_row_ui(row, f"new_{r_idx}")
                    if len(new_df) > 10:
                        st.caption(f"Noch {len(new_df) - 10} weitere neue Ergebnisse.")

            # --------------------------------------
            # 🧩 Themencluster (Beta) – OpenAI-Embeddings
            # --------------------------------------
            cluster_open, cluster_body = section_card(
                "🧩 Themencluster (Beta)",
                "Findet thematische Gruppen in den Abstracts und vergibt automatische Clusternamen.",
                "exp_cluster",
            )
            if cluster_open:
                with cluster_body:
                    key_openai = os.getenv("PAPERSCOUT_OPENAI_API_KEY") or os.getenv("OPENAI_API_KEY")
                    if not key_openai:
                        st.info("Bitte trage einen OpenAI API-Key ein (oben im Command Center), um Themencluster zu berechnen.")
                    else:
                        # Layout für Controls: Links Slider, Rechts Button
                        with st.container(border=True):
                            cl_controls_1, cl_controls_2, cl_controls_3 = st.columns([1, 1, 1])
                            with cl_controls_1:
                                 cluster_k = st.slider(
                                    "Anzahl Cluster",
                                    min_value=2,
                                    max_value=10,
                                    value=5,
                                    step=1,
                                    key="cluster_k_slider",
                                )
                            with cl_controls_2:
                                 cluster_min_docs = st.slider(
                                    "Min. Artikel/Cluster",
                                    min_value=3,
                                    max_value=20,
                                    value=5,
                                    step=1,
                                    key="cluster_min_docs_slider",
                                )
                            with cl_controls_3:
                                st.write("") # Spacer
                                st.write("") # Spacer
                                if st.button("🔍 Themencluster berechnen", use_container_width=True, key="btn_cluster_compute"):
                                    clusters = build_clusters_openai(df, k=cluster_k, min_docs=cluster_min_docs)
                                    if not clusters:
                                        st.warning("Konnte keine sinnvollen Themencluster bilden (zu wenig Text oder technische Probleme).")
                                    else:
                                        st.session_state["topic_clusters_openai"] = clusters
                                        st.success(f"{len(clusters)} Cluster erstellt.")

                        # --- UPDATE: Karten-Design (Expander untereinander) ---
                        clusters = st.session_state.get("topic_clusters_openai") or []
                        if clusters:
                            for c_idx, cluster in enumerate(clusters):
                                label_text = cluster["label"]
                                # Wir nutzen st.expander für das "Aufklappen"
                                with st.expander(label_text, expanded=False):
                                    # Inhalt in Container für Styling
                                    st.markdown(f"**Beispieltext:** *{cluster['sample_text']}*")
                                    
                                    sub_df = df.loc[cluster["indices"]] if cluster["indices"] else pd.DataFrame()
                                    st.caption(f"{len(sub_df)} Artikel in diesem Cluster:")
                                    
                                    # Hier rendern wir nun auch die vollwertigen Karten mit Checkboxen und Abstract
                                    for r_idx, (_, row) in enumerate(sub_df.iterrows()):
                                        render_row_ui(row, f"clus_{c_idx}_{r_idx}")
                        else:
                            if key_openai:
                                st.caption("Noch keine Cluster berechnet. Wähle Parameter und klicke auf „Themencluster berechnen“.")

            # --------------------------------------
            # 🧭 Trends & Insights (Zeitvergleich + Journal Trends)
            # --------------------------------------
            trends_open, trends_body = section_card(
                "🧭 Trends & Insights",
                "Zeigt Trend-Themen, Publikationen pro Monat und Abstract-Quellen.",
                "exp_trends",
            )
            if trends_open:
                with trends_body:
                    trend = _trend_summary(df, recent_days=30)
                    if trend:
                        st.caption(f"Zeitraum: {trend['recent_start']} bis {trend['recent_end']} (letzte 30 Tage)")
                        t_cols = st.columns(3)
                        with t_cols[0]:
                            st.markdown("**Top-Themen (letzte 30 Tage)**")
                            st.write(", ".join(trend.get("recent_terms", [])) or "–")
                        with t_cols[1]:
                            st.markdown("**Top-Themen (davor)**")
                            st.write(", ".join(trend.get("prior_terms", [])) or "–")
                        with t_cols[2]:
                            st.markdown("**Emerging Terms**")
                            st.write(", ".join(trend.get("emerging", [])) or "–")
                    else:
                        st.caption("Nicht genügend Datumsangaben für Trend-Analyse.")

                    # Journal-Trends
                    journal_counts = df.get("journal", pd.Series(dtype=str)).value_counts().head(5)
                    if not journal_counts.empty:
                        st.markdown("**Top Journals (Anzahl Treffer)**")
                        st.write(", ".join([f"{j} ({c})" for j, c in journal_counts.items()]))

                    # Zeitverlauf (Monat)
                    month_counts = df.get("issued", pd.Series(dtype=str)).dropna().astype(str).str[:7]
                    month_counts = month_counts[month_counts.str.match(r"\d{4}-\d{2}")]
                    if not month_counts.empty:
                        st.markdown("**Publikationen pro Monat**")
                        trend_series = month_counts.value_counts().sort_index()
                        st.bar_chart(trend_series)

                    if trend:
                        emerging = trend.get("emerging", [])
                        if emerging:
                            st.markdown("**Query-Ideen (automatisch)**")
                            suggestions = []
                            if len(emerging) >= 3:
                                suggestions.append(", ".join(emerging[:3]))
                            if len(emerging) >= 2:
                                suggestions.append(" ".join(emerging[:2]))
                            suggestions.append(emerging[0])
                            st.write(" · ".join(suggestions[:3]))

                    source_counts = df.get("abstract_source", pd.Series(dtype=str)).value_counts()
                    if not source_counts.empty:
                        st.markdown("**Abstract-Quellen**")
                        st.bar_chart(source_counts)

            # --------------------------------------
            # 🔮 Empfehlungen (ähnliche Reads zu Auswahl)
            # --------------------------------------
            rec_open, rec_body = section_card(
                "🔮 Empfohlene nächste Reads",
                "Schlägt Paper vor, die deiner Auswahl semantisch ähneln.",
                "exp_recs",
            )
            if rec_open:
                with rec_body:
                    rec_key = os.getenv("PAPERSCOUT_OPENAI_API_KEY") or os.getenv("OPENAI_API_KEY")
                    if not rec_key:
                        st.info("Für Empfehlungen wird ein OpenAI API-Key benötigt (oben im Command Center).")
                    else:
                        rec_cols = st.columns([1, 3])
                        with rec_cols[0]:
                            rec_n = st.slider("Anzahl Empfehlungen", min_value=3, max_value=15, value=6, step=1)
                            if st.button("Empfehlungen berechnen", use_container_width=True):
                                sel = st.session_state.get("selected_dois", set())
                                if not sel:
                                    st.warning("Bitte wähle mindestens eine DOI aus.")
                                else:
                                    # Build embeddings cache
                                    cache = st.session_state.get("embedding_cache", {})
                                    def _embed_text(text: str) -> List[float]:
                                        key = hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]
                                        if key in cache:
                                            return cache[key]
                                        emb = _get_embedding(text)
                                        if emb:
                                            cache[key] = emb
                                        return emb

                                    # Mean embedding of selected
                                    sel_rows = df[df["doi"].astype(str).str.lower().isin(sel)]
                                    sel_vecs = []
                                    for _, r in sel_rows.iterrows():
                                        text = (str(r.get("abstract","")) + " " + str(r.get("title",""))).strip()
                                        emb = _embed_text(text)
                                        if emb:
                                            sel_vecs.append(emb)
                                    if not sel_vecs:
                                        st.warning("Keine Embeddings für Auswahl verfügbar.")
                                    else:
                                        # Average
                                        dim = len(sel_vecs[0])
                                        mean = [0.0] * dim
                                        for v in sel_vecs:
                                            for i in range(dim):
                                                mean[i] += v[i]
                                        mean = [v / len(sel_vecs) for v in mean]

                                        # Score others
                                        scores = []
                                        for idx, r in df.iterrows():
                                            if str(r.get("doi","")).lower() in sel:
                                                continue
                                            text = (str(r.get("abstract","")) + " " + str(r.get("title",""))).strip()
                                            emb = _embed_text(text)
                                            if not emb:
                                                continue
                                            scores.append((idx, _cosine_sim(mean, emb)))
                                        scores = sorted(scores, key=lambda x: x[1], reverse=True)[:rec_n]
                                        st.session_state["rec_indices"] = [i for i, _ in scores]
                                        st.session_state["embedding_cache"] = cache

                        with rec_cols[1]:
                            rec_idx = st.session_state.get("rec_indices", [])
                            if rec_idx:
                                sub_df = df.loc[rec_idx]
                                for r_idx, (_, row) in enumerate(sub_df.iterrows()):
                                    render_row_ui(row, f"rec_{r_idx}")
                            else:
                                st.caption("Wähle DOIs und berechne Empfehlungen.")

            # --------------------------------------
            # 🧠 Research Brief (KI) - Regenerieren
            # --------------------------------------
            brief_open, brief_body = section_card(
                "🧠 Research Brief (Regenerieren)",
                "Erzeuge das Briefing erneut (z. B. mit anderem Umfang).",
                "exp_brief",
            )
            if brief_open:
                with brief_body:
                    brief_key = os.getenv("PAPERSCOUT_OPENAI_API_KEY") or os.getenv("OPENAI_API_KEY")
                    if not brief_key:
                        st.info("Für den Research Brief wird ein OpenAI API-Key benötigt (oben im Command Center).")
                    else:
                        b_cols = st.columns([1, 2])
                        with b_cols[0]:
                            brief_source = st.radio(
                                "Quelle",
                                ["Auswahl", "Top Relevanz", "Alle (Limit)"],
                                index=0,
                                key="brief_source",
                            )
                            brief_n = st.slider("Anzahl Papers", min_value=3, max_value=12, value=8, step=1, key="brief_n")
                            if st.button("Briefing erzeugen", use_container_width=True):
                                if brief_source == "Auswahl":
                                    sel = st.session_state.get("selected_dois", set())
                                    if not sel:
                                        st.warning("Bitte wähle mindestens eine DOI aus.")
                                    else:
                                        sub = df[df["doi"].astype(str).str.lower().isin(sel)].head(brief_n)
                                        brief = ai_generate_digest(sub.to_dict(orient="records"), model=ai_model, lang=st.session_state.get("brief_lang", "Deutsch"))
                                        st.session_state["research_brief"] = brief
                                elif brief_source == "Top Relevanz" and "relevance_score" in df.columns:
                                    sub = df.sort_values("relevance_score", ascending=False).head(brief_n)
                                    brief = ai_generate_digest(sub.to_dict(orient="records"), model=ai_model, lang=st.session_state.get("brief_lang", "Deutsch"))
                                    st.session_state["research_brief"] = brief
                                else:
                                    sub = df.head(brief_n)
                                    brief = ai_generate_digest(sub.to_dict(orient="records"), model=ai_model, lang=st.session_state.get("brief_lang", "Deutsch"))
                                    st.session_state["research_brief"] = brief
                        with b_cols[1]:
                            brief_text = st.session_state.get("research_brief", "")
                            if brief_text:
                                st.markdown(brief_text)
                            else:
                                st.caption("Noch kein Briefing. Quelle wählen und generieren.")

            # --------------------------------------
            # 🎯 Relevanz-Rating (Beta) - Manuell
            # --------------------------------------
            rel_open, rel_body = section_card(
                "🎯 Relevanz-Rating (Beta)",
                "Bewertet Papers nach semantischer Nähe zu deinem Forschungsfokus.",
                "exp_relevance",
                default_open=True,
                accent=True,
            )
            if rel_open:
                with rel_body:
                    rel_key = os.getenv("PAPERSCOUT_OPENAI_API_KEY") or os.getenv("OPENAI_API_KEY")
                    if not rel_key:
                        st.info("Für das Relevanz-Rating wird ein OpenAI API-Key benötigt (oben im Command Center).")
                    else:
                        # Layout Aufteilung: Links Eingabe, Rechts Ergebnisse
                        # Damit die Ergebnisse nicht "gequetscht" wirken, geben wir rechts mehr Platz
                        rel_col_left, rel_col_right = st.columns([1, 2])
                        
                        with rel_col_left:
                            st.markdown("#### Eingabe")
                            advanced_rel = st.checkbox("Mehrere Queries (gewichtet)", value=False, key="advanced_relevance")
                            if advanced_rel:
                                if "rel_queries" not in st.session_state:
                                    st.session_state["rel_queries"] = [{"text": "", "weight": 1.0}]
                                for i, q in enumerate(st.session_state["rel_queries"]):
                                    row_cols = st.columns([3, 1, 1])
                                    with row_cols[0]:
                                        st.text_input("Query", value=q.get("text",""), key=f"rel_q_{i}")
                                    with row_cols[1]:
                                        st.number_input("Gewicht", min_value=0.1, max_value=5.0, value=float(q.get("weight",1.0)), step=0.1, key=f"rel_w_{i}")
                                    with row_cols[2]:
                                        if st.button("Entfernen", key=f"rel_rm_{i}"):
                                            st.session_state["rel_queries"].pop(i)
                                            st.rerun()
                                if st.button("Query hinzufügen"):
                                    st.session_state["rel_queries"].append({"text": "", "weight": 1.0})
                                # Sync back inputs
                                synced = []
                                for i in range(len(st.session_state["rel_queries"])):
                                    synced.append({
                                        "text": st.session_state.get(f"rel_q_{i}", ""),
                                        "weight": st.session_state.get(f"rel_w_{i}", 1.0),
                                    })
                                st.session_state["rel_queries"] = synced
                                relevance_query = " ".join([q["text"] for q in synced if q.get("text")])
                            else:
                                relevance_query = st.text_area(
                                    "Forschungsinteresse / Fragestellung:",
                                    value=st.session_state.get("relevance_query_input", ""),
                                    height=150,
                                    help="Beispiel: 'transformational leadership, follower well-being, mediated by trust'",
                                    key="relevance_query_detail",
                                )

                            min_len = st.slider(
                                "Min. Textlänge (Zeichen)",
                                min_value=20,
                                max_value=100,
                                value=30,
                                step=5,
                                key="relevance_min_text_len",
                            )

                            if st.button("⭐ Relevanz berechnen", use_container_width=True, key="btn_compute_relevance"):
                                if not relevance_query.strip():
                                    st.warning("Bitte gib eine Beschreibung ein.")
                                else:
                                    st.session_state["relevance_query"] = relevance_query.strip()
                                    if advanced_rel:
                                        rel_series = compute_relevance_scores_multi(
                                            df,
                                            st.session_state.get("rel_queries", []),
                                            min_text_len=min_len,
                                        )
                                    else:
                                        rel_series = compute_relevance_scores(
                                            df,
                                            relevance_query.strip(),
                                            min_text_len=min_len,
                                        )
                                    if rel_series is None:
                                        st.warning("Konnte keine Werte berechnen.")
                                    else:
                                        # In DataFrame übernehmen
                                        df["relevance_score"] = rel_series
                                        # Warum relevant? (simple term overlap)
                                        combined_query = relevance_query.strip()
                                        why_list = []
                                        for _, row in df.iterrows():
                                            text = (str(row.get("abstract","")) + " " + str(row.get("title",""))).strip()
                                            why_list.append(_why_relevant(combined_query, text))
                                        df["relevance_why"] = why_list
                                        st.session_state["results_df"] = df
                                        st.success("Berechnet!")
                                        st.rerun()

                        with rel_col_right:
                            st.markdown("#### Top 10 Ergebnisse")
                            if "relevance_score" in df.columns:
                                top_df = df.sort_values("relevance_score", ascending=False).head(10)
                                # Hier rendern wir ebenfalls die vollwertigen Karten
                                for r_idx, (_, row) in enumerate(top_df.iterrows()):
                                    render_row_ui(row, f"rel_top_{r_idx}")
                            else:
                                st.caption("Gib links dein Thema ein und klicke auf Berechnen, um die Top 10 zu sehen.")

    # --- Inline Filter & Sort (für Ergebnisliste) ---
    base_df = df.copy()
    f_cols = st.columns([2, 1, 1, 1, 1])
    with f_cols[0]:
        filter_keyword = st.text_input("🔎 Keyword in Titel/Abstract", value="", key="filter_keyword")
    with f_cols[1]:
        filter_author = st.text_input("👤 Autor enthält", value="", key="filter_author")
    with f_cols[2]:
        filter_has_abs = st.checkbox("Nur mit Abstract", value=False, key="filter_has_abs")
    with f_cols[3]:
        filter_journals = st.multiselect("📘 Journals", options=sorted(base_df.get("journal", pd.Series(dtype=str)).dropna().unique().tolist()))
    with f_cols[4]:
        min_rel = 0.0
        if "relevance_score" in base_df.columns:
            min_rel = st.slider("⭐ Min. Relevanz", min_value=0.0, max_value=100.0, value=0.0, step=5.0)

    df = base_df.copy()
    if filter_keyword.strip():
        kw = filter_keyword.strip().lower()
        df = df[
            df.get("title", "").astype(str).str.lower().str.contains(kw, na=False) |
            df.get("abstract", "").astype(str).str.lower().str.contains(kw, na=False)
        ]
    if filter_author.strip():
        au = filter_author.strip().lower()
        df = df[df.get("authors", "").astype(str).str.lower().str.contains(au, na=False)]
    if filter_has_abs:
        df = df[df.get("abstract", "").astype(str).str.len() > 0]
    if filter_journals:
        df = df[df.get("journal", "").isin(filter_journals)]
    if "relevance_score" in df.columns:
        df = df[df["relevance_score"].fillna(0.0) >= min_rel]

    sort_col1, sort_col2 = st.columns([1, 3])
    with sort_col1:
        sort_options = ["Neueste zuerst", "Älteste zuerst", "Signal-Score", "Relevanz", "Titel (A-Z)"]
        if "relevance_score" in df.columns and df["relevance_score"].notna().any():
            default_sort = "Relevanz"
        else:
            default_sort = "Neueste zuerst"
        sort_by = st.selectbox(
            "Sortieren",
            sort_options,
            index=sort_options.index(default_sort),
        )
    if sort_by == "Neueste zuerst":
        df["_issued_dt"] = df.get("issued", "").astype(str).apply(_safe_parse_date)
        df = df.sort_values("_issued_dt", ascending=False, na_position="last").drop(columns=["_issued_dt"])
    elif sort_by == "Älteste zuerst":
        df["_issued_dt"] = df.get("issued", "").astype(str).apply(_safe_parse_date)
        df = df.sort_values("_issued_dt", ascending=True, na_position="last").drop(columns=["_issued_dt"])
    elif sort_by == "Signal-Score":
        if "signal_score" in df.columns:
            df = df.sort_values("signal_score", ascending=False, na_position="last")
        else:
            df["_issued_dt"] = df.get("issued", "").astype(str).apply(_safe_parse_date)
            df = df.sort_values("_issued_dt", ascending=False, na_position="last").drop(columns=["_issued_dt"])
    elif sort_by == "Relevanz":
        if "relevance_score" in df.columns:
            df = df.sort_values("relevance_score", ascending=False, na_position="last")
        else:
            df["_issued_dt"] = df.get("issued", "").astype(str).apply(_safe_parse_date)
            df = df.sort_values("_issued_dt", ascending=False, na_position="last").drop(columns=["_issued_dt"])
    elif sort_by == "Titel (A-Z)":
        df = df.sort_values("title", ascending=True, na_position="last")

    # --- Pagination ---
    p_cols = st.columns([1, 1, 2])
    with p_cols[0]:
        page_size = st.selectbox("Ergebnisse pro Seite", [25, 50, 100], index=1, key="page_size")
    total_pages = max(1, ceil(len(df) / page_size))
    current_page = st.session_state.get("page_num", 1)
    if current_page > total_pages:
        current_page = total_pages
    with p_cols[1]:
        page_num = st.selectbox("Seite", list(range(1, total_pages + 1)), index=list(range(1, total_pages + 1)).index(current_page), key="page_num")
    start_idx = (page_num - 1) * page_size
    end_idx = start_idx + page_size
    df_page = df.iloc[start_idx:end_idx].copy()
    with p_cols[2]:
        st.caption(f"Zeige {start_idx + 1}-{min(end_idx, len(df))} von {len(df)}")

    st.caption("Klicke auf die Checkboxen (egal in welcher Liste), um Einträge für den E-Mail-Versand auszuwählen.")

    # --- Fixierte Pfeil-Navigation (Start/Ende) ---
    FIXED_NAV_HTML = """
    <style>
    .fixed-nav {
        position: fixed;
        bottom: 1.5rem;
        left: 50%;
        transform: translateX(-50%);
        background-color: var(--secondary-background-color);
        border: 1px solid var(--border-color, var(--gray-300));
        border-radius: 25px;
        padding: 0.5rem 1rem;
        box-shadow: 0 4px 12px rgba(0,0,0,0.1);
        z-index: 9999;
        opacity: 0.9;
    }
    html.dark .fixed-nav {
         border: 1px solid var(--border-color, var(--gray-800));
    }
    .fixed-nav a {
        display: inline-block;
        text-decoration: none;
        color: var(--text-color);
        font-size: 1.25rem;
        margin: 0 0.75rem;
        transition: transform 0.1s ease-in-out;
    }
    .fixed-nav a:hover {
        transform: scale(1.2);
        color: var(--primary-color);
    }
    </style>
    
    <div class="fixed-nav">
        <a href="#results_top" title="Zum Anfang der Liste">⬆️</a>
        <a href="#actions_bottom" title="Zum E-Mail Versand">⬇️</a>
    </div>
    """
    st.markdown(FIXED_NAV_HTML, unsafe_allow_html=True)

    # --- Keyboard Shortcuts (G / Shift+G) ---
    components.html(
        """
        <script>
        document.addEventListener('keydown', function(e) {
          if (e.key === 'g' && !e.shiftKey) {
            window.location.hash = '#results_top';
          }
          if (e.key === 'G' || (e.key === 'g' && e.shiftKey)) {
            window.location.hash = '#actions_bottom';
          }
        });
        </script>
        """,
        height=0,
    )
    st.caption("Shortcuts: `g` zum Anfang, `Shift+g` zum E-Mail-Versand.")

    # --- Ergebnis-Loop (paged) ---
    for i, (_, r) in enumerate(df_page.iterrows(), start=start_idx + 1):
        render_row_ui(r, str(i))

    st.markdown("---")

    # --- KORREKTUR 2 (Sync-Fix): Logik für "Alle auswählen/abwählen" ---
    # Wir müssen *vor* den Buttons eine Map aller DOIs und Keys erstellen.
    doi_key_map = {}
    for i, (_, r) in enumerate(df.iterrows(), start=1):
        doi_norm = (r.get("doi", "") or "").lower()
        if doi_norm:
            sel_key = _stable_sel_key(r, str(i))
            doi_key_map[doi_norm] = sel_key
    # --- ENDE KORREKTUR 2 ---


    # --- Aktionen: Auswahl & Download ---
    action_col1, action_col2, action_col3 = st.columns([1, 1, 1])
    with action_col1:
        st.metric(label="Aktuell ausgewählt", value=f"{len(st.session_state['selected_dois'])} / {len(df)}")
    
    with action_col2:
        if st.button("Alle **Ergebnisse** auswählen", use_container_width=True):
            # --- KORREKTUR 3 (Sync-Fix): Button-Logik aktualisiert ---
            # Wir setzen alle DOIs in die Selected List
            st.session_state["selected_dois"] = set(doi_key_map.keys())
            st.rerun()
            # --- ENDE KORREKTUR 3 ---

    with action_col3:
        if st.button("Alle **Ergebnisse** abwählen", use_container_width=True):
            # --- KORREKTUR 4 (Sync-Fix): Button-Logik aktualisiert ---
            st.session_state["selected_dois"].clear()
            st.rerun()
            # --- ENDE KORREKTUR 4 ---
    
    qa_cols = st.columns([1, 1, 1])
    with qa_cols[0]:
        quick_n = st.slider("Quick-Pick Anzahl", min_value=5, max_value=50, value=10, step=5, key="quick_pick_n")
    with qa_cols[1]:
        if st.button("Top Relevanz hinzufügen", use_container_width=True):
            if "relevance_score" in df.columns:
                top = df.sort_values("relevance_score", ascending=False).head(quick_n)
                st.session_state["selected_dois"] |= set(top["doi"].astype(str).str.lower())
                st.rerun()
            else:
                st.warning("Bitte zuerst Relevanz berechnen.")
    with qa_cols[2]:
        if st.button("Neueste hinzufügen", use_container_width=True):
            temp = df.copy()
            temp["_issued_dt"] = temp.get("issued", "").astype(str).apply(_safe_parse_date)
            top = temp.sort_values("_issued_dt", ascending=False).head(quick_n)
            st.session_state["selected_dois"] |= set(top["doi"].astype(str).str.lower())
            st.rerun()

    collections_open, collections_body = section_card(
        "📁 Collections",
        "Gruppiere ausgewählte Papers in benannten Sammlungen für spätere Arbeit.",
        "exp_collections",
    )
    if collections_open:
        with collections_body:
            col_cols = st.columns([2, 1, 1])
            with col_cols[0]:
                col_name = st.text_input("Collection-Name", value="")
            with col_cols[1]:
                if st.button("Zur Collection hinzufügen", use_container_width=True):
                    if not col_name.strip():
                        st.warning("Bitte einen Collection-Namen angeben.")
                    elif not st.session_state["selected_dois"]:
                        st.warning("Bitte zuerst DOIs auswählen.")
                    else:
                        coll = st.session_state["collections"].get(col_name.strip(), set())
                        coll = set(coll) | set(st.session_state["selected_dois"])
                        st.session_state["collections"][col_name.strip()] = coll
                        st.success(f"{len(st.session_state['selected_dois'])} DOI(s) hinzugefügt.")
            with col_cols[2]:
                if st.session_state["collections"]:
                    if st.button("Alle Collections löschen", use_container_width=True):
                        st.session_state["collections"] = {}
                        st.success("Collections gelöscht.")

            if st.session_state["collections"]:
                for name, doi_set in st.session_state["collections"].items():
                    with st.expander(f"📁 {name} ({len(doi_set)})", expanded=False):
                        sub_df = df[df["doi"].astype(str).str.lower().isin({d.lower() for d in doi_set})]
                        for r_idx, (_, row) in enumerate(sub_df.iterrows()):
                            render_row_ui(row, f"coll_{name}_{r_idx}")

    st.divider()
    # --- NEU: Link "Hoch" und Anker "Unten" ---
    st.markdown(
        """
        <style>
            .link-container-bottom {
                text-align: right;
                margin-bottom: -1.5rem;
            }
            .link-container-bottom a {
                text-decoration: none;
                font-size: 0.9rem;
            }
        </style>
        <div class="link-container-bottom">
            <a href='#results_top'>⬆️ Zum Anfang der Liste springen</a>
        </div>
        """, 
        unsafe_allow_html=True
    )
    # Der Anker, zu dem der "Runter"-Link springt
    st.markdown("<a id='actions_bottom'></a>", unsafe_allow_html=True)
    # --- ENDE NEU ---

    # --- Download & E-Mail (neu gruppiert) ---
    actions_open, actions_body = section_card(
        "🏁 Aktionen: Download & Versand",
        "Exportiere Ergebnisse oder sende DOI-Listen per E-Mail.",
        "exp_actions",
        default_open=True,
        show_toggle=False,
    )
    if actions_open:
        with actions_body:
            dl_col, mail_col = st.columns(2)

            with dl_col:
                st.markdown("#### ⬇️ Download")
                def df_to_excel_bytes(df_in: pd.DataFrame) -> BytesIO | None:
                    engine = _pick_excel_engine()
                    if engine is None: return None
                    out = BytesIO()
                    with pd.ExcelWriter(out, engine=engine) as writer:
                        df_in.to_excel(writer, index=False, sheet_name="results")
                    out.seek(0)
                    return out

                def _df_to_csv_bytes(df_in: pd.DataFrame) -> BytesIO:
                    b = BytesIO()
                    b.write(df_in.to_csv(index=False).encode("utf-8"))
                    b.seek(0)
                    return b

                x_all = df_to_excel_bytes(df)
                if x_all is not None:
                    st.download_button(
                        "Excel — alle Ergebnisse",
                        data=x_all,
                        file_name="paperscout_results.xlsx",
                        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        use_container_width=True
                    )
                else:
                    st.download_button(
                        "CSV — alle Ergebnisse",
                        data=_df_to_csv_bytes(df),
                        file_name="paperscout_results.csv",
                        mime="text/csv",
                        use_container_width=True
                    )

                if st.session_state["selected_dois"]:
                    df_sel = df[df["doi"].astype(str).str.lower().isin(st.session_state["selected_dois"])].copy()
                    x_sel = df_to_excel_bytes(df_sel)
                    if x_sel is not None:
                        st.download_button(
                            f"Excel — {len(st.session_state['selected_dois'])} ausgewählte",
                            data=x_sel,
                            file_name="paperscout_selected.xlsx",
                            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                            use_container_width=True
                        )
                    else:
                         st.download_button(
                            f"CSV — {len(st.session_state['selected_dois'])} ausgewählte",
                            data=_df_to_csv_bytes(df_sel),
                            file_name="paperscout_selected.csv",
                            mime="text/csv",
                            use_container_width=True
                        )
                else:
                    st.button("Excel — nur ausgewählte", disabled=True, use_container_width=True)


            with mail_col:
                st.markdown("#### 📧 DOI-Liste senden")
                with st.container(border=True):
                    sender_display = st.text_input(
                        "Absendername (z.B. Naomi oder Ralf)",
                        value="",
                    )
                    to_email = st.text_input("Empfänger-E-Mail-Adresse", key="doi_email_to")
                    
                    if st.button("DOI-Liste senden", use_container_width=True, type="primary"):
                        if not st.session_state["selected_dois"]:
                            st.warning("Bitte wähle mindestens eine DOI aus.")
                        elif not to_email or "@" not in to_email:
                            st.warning("Bitte gib eine gültige E-Mail-Adresse ein.")
                        else:
                            # Ausgewählte DOIs (lowercase)
                            sel_dois = st.session_state["selected_dois"]
                            df_sel = df[df["doi"].astype(str).str.lower().isin(sel_dois)].copy()
                            records = df_sel.to_dict(orient="records")

                            ok, msg = send_doi_email(
                                to_email,
                                records,
                                sender_display=sender_display.strip() or None
                            )
                            st.success(msg) if ok else st.error(msg)

else:
    st.info("Noch keine Ergebnisse geladen. Wähle Journals im Command Center und starte den Run.")
