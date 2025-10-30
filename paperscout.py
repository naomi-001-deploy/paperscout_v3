# app_v6_openai.py – Paperscout (Nur API-Version)
# UI-Update: Modernes Design mit CSS-Karten und Tabs.
# BUGFIX: StreamlitDuplicateElementId durch eindeutige Button-Labels behoben.
# BUGFIX: HTML-Escaping-Problem im Abstract (f-string-Konflikt) endgültig behoben.
# BUGFIX: Synchronisierung zwischen "Alle auswählen"-Buttons und individuellen Checkboxen.
# FEATURE (NEU): Dynamische Farbgebung (Dark Mode / Light Mode) durch Streamlit CSS-Variablen.

import os, re, html, json, smtplib, ssl, hashlib
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

def _stable_sel_key(r: dict, i: int) -> str:
    # robuste Basis: DOI -> URL -> Titel -> Index
    basis = (str(r.get("doi") or "") + "|" +
             str(r.get("url") or "") + "|" +
             str(r.get("title") or "")).lower()
    # kurze, saubere ID
    h = hashlib.sha1(basis.encode("utf-8")).hexdigest()[:12]
    return f"sel_card_{h}_{i}"

# --- SMTP aus Secrets/Env laden (robust, auch wenn keine secrets.toml vorhanden ist) ---
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
    """
    Liest einen optionalen Proxy aus:
    - ENV: PAPERSCOUT_PROXY (z. B. 'http://user:pass@host:port' oder 'socks5://host:1080')
    - Session: st.session_state['proxy_url'] (wird im UI gesetzt)
    Gibt ein httpx-kompatibles proxies-Dict zurück oder None.
    """
    p = (st.session_state.get("proxy_url") or
         os.getenv("PAPERSCOUT_PROXY") or "").strip()
    if not p:
        return None
    return {"http": p, "https": p}

def _http_client(timeout: float, headers: dict | None = None) -> httpx.Client:
    """
    Einheitlicher httpx-Client:
    - http2=False (Publisher liefern unter H2 anderes Markup)
    - follow_redirects=True
    - optionaler Proxy (HTTP/HTTPS/SOCKS)
    - Cookie-Handling (NEU/VERBESSERT)
    """
    return httpx.Client(
        timeout=timeout,
        headers=headers or _headers(),
        follow_redirects=True,
        http2=False,
        proxies=_proxy_dict(),
        cookies=httpx.Cookies(),  # <-- VERBESSERUNG
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
    - Probiert verschiedene Datumsfilter.
    - Fällt zurück auf Container-Title-Query und ALT_ISSN.
    - Letzter Notanker: ohne Datumsfilter (wir filtern client-seitig).
    - Filtert auf type:journal-article.
    - harter Nachfilter: exakter Container-Title ODER ISSN-Match.
    """
    mailto = os.getenv("CROSSREF_MAILTO") or "you@example.com"
    base_filters = [
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
            for f_from, f_until in base_filters:
                filt = f"{f_from}:{since},{f_until}:{until},type:journal-article"
                url_list.append(
                    f"{CR_BASE}/works?query.container-title={quote_plus(journal)}&filter={filt}&sort=published&order=desc&rows={rows}&mailto={mailto}"
                )
            return url_list
        else:
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

# =========================
# KEIN TOC-SCRAPING MEHR
# =========================


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

# -------------------------
# ScienceDirect / Elsevier – direkter JSON-Endpoint
# -------------------------
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

# -------------------------
# KEINE TOC-FILTER-TOOLS MEHR
# -------------------------

# =========================
# Hauptpipeline
# =========================
def collect_all(journal: str, since: str, until: str, rows: int, ai_model: str) -> List[Dict[str, Any]]:
    issn = JOURNAL_ISSN.get(journal)
    if not issn:
        return []

    base = fetch_crossref_any(journal, issn, since, until, rows)
    out: List[Dict[str, Any]] = []

    if not base:
        return []

    for rec in base:
        if rec.get("abstract"):
            out.append(rec)
            continue

        doi = rec.get("doi", "")

        for fn in (fetch_semantic, fetch_openalex):
            if not doi:
                break
            data = fn(doi)
            if data and data.get("abstract"):
                for k in ["title", "authors", "journal", "issued", "abstract", "url"]:
                    if not rec.get(k):
                        rec[k] = data.get(k)
                break

        if not rec.get("abstract"):
            # Prüfen, ob "sciencedirect" in der URL ist ODER ob das Journal
            # (gemäß ISSN) ein Sciencedirect-Journal ist.
            is_sd_url = "sciencedirect.com" in (rec.get("url","") or "")
            # (Wir haben JOURNAL_REGISTRY nicht mehr, also machen wir einen
            # Workaround und checken, ob die ISSN zu TLQ gehört)
            is_sd_journal = (issn == "1048-9843") # The Leadership Quarterly
            
            if is_sd_url or is_sd_journal:
                abs_text = fetch_sciencedirect_abstract(rec.get("url") or rec.get("doi",""))
                if abs_text:
                    rec["abstract"] = abs_text

        if not rec.get("abstract") and rec.get("url"):
            html_text = fetch_html(rec["url"])
            if html_text:
                abs_simple = extract_abstract_from_html_simple(html_text)
                if abs_simple:
                    rec["abstract"] = abs_simple

        if not rec.get("abstract") and rec.get("url"):
            html_text = fetch_html(rec["url"])
            if html_text:
                ai = ai_extract_metadata_from_html(html_text, ai_model)
                if ai:
                    for k in ["title", "authors", "journal", "issued", "abstract", "doi", "url"]:
                        if not rec.get(k) and ai.get(k):
                            rec[k] = ai.get(k)

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
# E-Mail Versand (SMTP)
# =========================
def send_doi_email(to_email: str, dois: List[str], sender_display: Optional[str] = None) -> tuple[bool, str]:
    host = os.getenv("EMAIL_HOST")
    port = int(os.getenv("EMAIL_PORT", "587"))
    user = os.getenv("EMAIL_USER")
    password = os.getenv("EMAIL_PASSWORD")
    sender_addr = os.getenv("EMAIL_FROM") or user
    default_name = os.getenv("EMAIL_SENDER_NAME", "paperscout")
    use_tls = os.getenv("EMAIL_USE_TLS", "true").lower() in ("1","true","yes","y")
    use_ssl = os.getenv("EMAIL_USE_SSL", "false").lower() in ("1,""true","yes","y")

    if not (host and port and sender_addr and user and password):
        return False, "SMTP nicht konfiguriert (EMAIL_HOST/PORT/USER/PASSWORD/EMAIL_FROM)."

    display_name = (sender_display or "").strip() or default_name

    body_lines = [
        "Hallo,",
        "",
        f"ausgewählt von: {display_name}",
        "",
        "Hier ist die Liste der ausgewählten DOIs:",
        *[f"- {d if d.startswith('10.') else d}" for d in dois],
        "",
        "Viele Grüße",
        display_name,
    ]
    msg = MIMEText("\n".join(body_lines), _charset="utf-8")
    msg["Subject"] = f"[paperscout] {len(dois)} DOI(s) — {display_name}"
    msg["From"] = formataddr((display_name, sender_addr))
    msg["To"] = to_email

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

        return True, "E-Mail gesendet."
    except Exception as e:
        return False, f"E-Mail Versand fehlgeschlagen: {e}"

# =========================
# =========================
# NEUE UI (v3) - JETZT MIT DARK MODE
# =========================
# =========================
st.title("🕵🏻 paperscout – Journal Service")

# Init Session State für Auswahl
if "selected_dois" not in st.session_state:
    st.session_state["selected_dois"] = set()

# --- KORREKTUR: CSS-Block (v3) für Dark Mode ---
# Verwendet jetzt Streamlit CSS-Variablen für dynamische Farben
CARD_STYLE_V3 = """
<style>
    /*
    NEUE THEME-AWARE KARTEN (v3)
    Verwendet Streamlit CSS-Variablen, um sich an Light/Dark-Mode anzupassen.
    */
    .result-card {
        /* Nimmt die "Hintergrundfarbe für Container" (hellgrau/dunkelgrau) */
        background-color: var(--secondary-background-color); 
        border: 1px solid var(--secondary-background-color); /* Rand in gleicher Farbe */
        border-left: 6px solid var(--primary-color); /* Akzentfarbe (z.B. blau) */
        border-radius: 8px;
        padding: 1.1rem;
        margin-bottom: 1rem;
        /* Subtiler Schatten, der auf beiden Modi funktioniert */
        box-shadow: 0 2px 8px rgba(0,0,0,0.05); 
        transition: all 0.2s ease-in-out;
    }
    .result-card:hover {
        /* Heller/dunkler als der Hintergrund, je nach Modus */
        background-color: var(--background-color); 
        box-shadow: 0 5px 15px rgba(0,0,0,0.1);
        transform: translateY(-2px);
    }
    .result-card h3 {
        color: var(--text-color); /* Passt sich an (schwarz/weiß) */
        margin-top: 0;
        margin-bottom: 0.25rem;
    }
    .result-card .meta {
        color: var(--secondary-text-color); /* Passt sich an (grau) */
        font-size: 0.9rem;
        margin-bottom: 0.75rem;
    }
    .result-card .authors {
        color: var(--text-color); /* Passt sich an (schwarz/weiß) */
        font-size: 0.95rem;
        font-weight: 500;
    }
    .result-card details {
        margin-top: 1rem;
    }
    .result-card details summary {
        cursor: pointer;
        font-weight: bold;
        color: var(--primary-color); /* Nutzt die Akzentfarbe des Themes */
        font-size: 0.95rem;
        list-style-type: '➕ ';
    }
    .result-card details[open] summary {
        list-style-type: '➖ ';
    }
    .result-card details > div {
        /* Nimmt die Haupt-Hintergrundfarbe (weiß/sehr dunkelgrau) */
        background-color: var(--background-color); 
        border-radius: 5px;
        padding: 0.75rem 1rem;
        margin-top: 0.5rem;
        /* Rand ist jetzt die "normale" Randfarbe */
        border: 1px solid var(--border-color, var(--gray-300)); 
    }
    
    /* Expliziter Fallback für Rand im Dark Mode (falls --border-color nicht gesetzt ist) */
    html.dark .result-card details > div {
        border: 1px solid var(--border-color, var(--gray-800));
    }
    
    .result-card details .abstract {
        color: var(--text-color); /* Passt sich an */
        white-space: pre-wrap;
        font-size: 0.9rem;
        line-height: 1.6;
    }
    .result-card details a {
        color: var(--primary-color); /* Links nutzen auch Akzentfarbe */
        text-decoration: none;
    }
    .result-card details a:hover {
        text-decoration: underline;
    }
</style>
"""
st.markdown(CARD_STYLE_V3, unsafe_allow_html=True)


# --- Setup-Tabs ---
tab1, tab2 = st.tabs(["🔍 Schritt 1: Auswahl", "⚙️ Schritt 2: Einstellungen"])
journals = sorted(JOURNAL_ISSN.keys())
today = date.today()

with tab1:
    st.markdown("#### Journals auswählen")
    
    def _chk_key(name: str) -> str:
        return "chk_" + re.sub(r"\W+", "_", name.lower()).strip("_")

    sel_all_col, desel_all_col, _ = st.columns([1, 1, 4])
    with sel_all_col:
        select_all_clicked = st.button("Alle **Journals** auswählen", use_container_width=True)
    with desel_all_col:
        deselect_all_clicked = st.button("Alle **Journals** abwählen", use_container_width=True)

    if select_all_clicked:
        for j in journals:
            st.session_state[_chk_key(j)] = True
    if deselect_all_clicked:
        for j in journals:
            st.session_state[_chk_key(j)] = False

    chosen: List[str] = []
    cols = st.columns(3)
    for idx, j in enumerate(journals):
        k = _chk_key(j)
        current_val = st.session_state.get(k, False)
        with cols[idx % 3]:
            if st.checkbox(j, value=current_val, key=k):
                chosen.append(j)

    st.markdown(f"**{len(chosen)}** Journal(s) ausgewählt.")
    st.divider()
    
    st.markdown("#### Zeitraum definieren")
    date_col1, date_col2, date_col3 = st.columns(3)
    with date_col1:
        since = st.date_input("Seit (inkl.)", value=date(today.year, 1, 1))
    with date_col2:
        until = st.date_input("Bis (inkl.)", value=today)
    with date_col3:
        st.markdown("<br>", unsafe_allow_html=True) # Kleiner Layout-Hack für die Höhe
        last30 = st.checkbox("Nur letzte 30 Tage", value=False)
        if last30:
            st.caption(f"Aktiv: {(today - timedelta(days=30)).isoformat()} bis {today.isoformat()}")
        last7 = st.checkbox("Nur letzte 7 Tage", value=False)
        if last7:
            st.caption(f"Aktiv: {(today - timedelta(days=7)).isoformat()} bis {today.isoformat()}")
        last1 = st.checkbox("Nur letzter Tag", value=False)
        if last1:
            st.caption(f"Aktiv: {(today - timedelta(days=1)).isoformat()} bis {today.isoformat()}")
            


with tab2:
    st.markdown("#### Technische Einstellungen")
    rows = st.number_input("Max. Treffer pro Journal", min_value=5, max_value=200, step=5, value=100)
    ai_model = st.text_input("OpenAI Modell (für Abstract-Fallback)", value="gpt-4o-mini")
    
    st.markdown("#### API-Keys & E-Mails")
    api_key_input = st.text_input("🔑 OpenAI API-Key", type="password", value="", help="Optional. Wird für Artikel ohne Abstract benötigt.")
    if api_key_input:
        os.environ["PAPERSCOUT_OPENAI_API_KEY"] = api_key_input
        st.caption("API-Key für diese Sitzung gesetzt.")
        
    crossref_mail = st.text_input("📧 Crossref Mailto (empfohlen)", value=os.getenv("CROSSREF_MAILTO", ""), help="Eine E-Mail-Adresse verbessert die Zuverlässigkeit der Crossref-API.")
    if crossref_mail:
        os.environ["CROSSREF_MAILTO"] = crossref_mail
        st.caption("Crossref-Mailto für diese Sitzung gesetzt.")

    st.markdown("#### Netzwerk & Versand")
    proxy_url = st.text_input("🌐 Proxy (optional)", value=os.getenv("PAPERSCOUT_PROXY", ""), help="Format: http://user:pass@host:port")
    if proxy_url:
        st.session_state["proxy_url"] = proxy_url.strip()
        st.success("Proxy für diese Sitzung aktiv.")
    else:
        st.session_state["proxy_url"] = ""

    with st.expander("✉️ E-Mail Versand (Status)", expanded=False):
        ok = all(os.getenv(k) for k in ["EMAIL_HOST","EMAIL_PORT","EMAIL_USER","EMAIL_PASSWORD","EMAIL_FROM"])
        if ok:
            st.success(f"SMTP konfiguriert für: {os.getenv('EMAIL_FROM')}")
        else:
            st.error("SMTP nicht vollständig konfiguriert. Bitte Secrets/Env setzen.")

st.divider()

# --- Start-Button ---
run_col1, run_col2, run_col3 = st.columns([2, 1, 2])
with run_col2:
    run = st.button("🚀 Let´s go! Metadaten ziehen", use_container_width=True, type="primary")

if run:
    if not chosen:
        st.warning("Bitte mindestens ein Journal in Schritt 1 auswählen.")
    else:
        st.info("Starte Abruf — Crossref, Semantic Scholar, OpenAlex, KI-Fallback...")

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

        for i, j in enumerate(chosen, 1):
            progress.progress(min(i / max(n, 1), 1.0), f"({i}/{n}) Verarbeite: {j}")
            rows_j = collect_all(j, s_since, s_until, int(rows), ai_model)
            rows_j = dedup(rows_j)
            all_rows.extend(rows_j)

        progress.empty()
        if not all_rows:
            st.warning("Keine Treffer im gewählten Zeitraum/Journals gefunden.")
        else:
            df = pd.DataFrame(all_rows)
            cols = [c for c in ["title", "doi", "issued", "journal", "authors", "abstract", "url"] if c in df.columns]
            if cols:
                df = df[cols]

            st.session_state["results_df"] = df
            st.session_state["selected_dois"] = set() # Auswahl zurücksetzen
            
            # Alle Checkbox-States löschen/zurücksetzen, falls alte Keys von einem früheren Lauf existieren
            for key in list(st.session_state.keys()):
                if key.startswith("sel_card_"):
                    del st.session_state[key]
            
            st.success(f"🎉 {len(df)} Treffer geladen!")

# ================================
# --- NEUE ERGEBNISANZEIGE (v2) ---
# ================================
st.divider()

# --- NEU: Anker für "Hoch" ---
st.markdown("<a id='results_top'></a>", unsafe_allow_html=True) 

st.subheader("📚 Ergebnisse")

# --- NEU: Link für "Runter" ---
# Kleiner CSS-Hack, um den Link rechtsbündig über den Buttons zu platzieren
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
    df = st.session_state["results_df"].copy()

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

    st.caption("Klicke links auf die Checkbox, um Einträge für den E-Mail-Versand auszuwählen.")

    # --- KORREKTUR 2 (Sync-Fix): Logik für "Alle auswählen/abwählen" ---
    # Wir müssen *vor* den Buttons eine Map aller DOIs und Keys erstellen.
    doi_key_map = {}
    for i, (_, r) in enumerate(df.iterrows(), start=1):
        doi_norm = (r.get("doi", "") or "").lower()
        if doi_norm:
            sel_key = _stable_sel_key(r, i)
            doi_key_map[doi_norm] = sel_key
    # --- ENDE KORREKTUR 2 ---


    # --- Aktionen: Auswahl & Download ---
    action_col1, action_col2, action_col3 = st.columns([1, 1, 1])
    with action_col1:
        st.metric(label="Aktuell ausgewählt", value=f"{len(st.session_state['selected_dois'])} / {len(df)}")
    
    with action_col2:
        if st.button("Alle **Ergebnisse** auswählen", use_container_width=True):
            # --- KORREKTUR 3 (Sync-Fix): Button-Logik aktualisiert ---
            for doi, key in doi_key_map.items():
                st.session_state[key] = True  # Setzt den Status der individuellen Checkbox
            st.session_state["selected_dois"] = set(doi_key_map.keys()) # Setzt die Master-Liste
            st.rerun()
            # --- ENDE KORREKTUR 3 ---

    with action_col3:
        if st.button("Alle **Ergebnisse** abwählen", use_container_width=True):
            # --- KORREKTUR 4 (Sync-Fix): Button-Logik aktualisiert ---
            for key in doi_key_map.values():
                if key in st.session_state:
                    st.session_state[key] = False # Setzt den Status der individuellen Checkbox
            st.session_state["selected_dois"].clear() # Leert die Master-Liste
            st.rerun()
            # --- ENDE KORREKTUR 4 ---
    
    st.markdown("---") # Visueller Trenner
    # --- NEU: Fixierte Pfeil-Navigation (Start/Ende) ---
    FIXED_NAV_HTML = """
    <style>
    .fixed-nav {
        position: fixed;
        bottom: 1.5rem; /* Abstand von unten */
        left: 50%;
        transform: translateX(-50%); /* Zentrierung */
        background-color: var(--secondary-background-color);
        border: 1px solid var(--border-color, var(--gray-300));
        border-radius: 25px; /* Pillenform */
        padding: 0.5rem 1rem;
        box-shadow: 0 4px 12px rgba(0,0,0,0.1);
        z-index: 9999; /* Über allem anderen */
        opacity: 0.9; /* Leichte Transparenz */
    }
    /* Fallback für Darkmode-Rand */
    html.dark .fixed-nav {
         border: 1px solid var(--border-color, var(--gray-800));
    }
    .fixed-nav a {
        display: inline-block;
        text-decoration: none;
        color: var(--text-color);
        font-size: 1.25rem; /* Größere Pfeile */
        margin: 0 0.75rem;
        transition: transform 0.1s ease-in-out;
    }
    .fixed-nav a:hover {
        transform: scale(1.2);
        color: var(--primary-color); /* Akzentfarbe beim Hover */
    }
    </style>
    
    <div class="fixed-nav">
        <a href="#results_top" title="Zum Anfang der Liste">⬆️</a>
        <a href="#actions_bottom" title="Zum E-Mail Versand">⬇️</a>
    </div>
    """
    st.markdown(FIXED_NAV_HTML, unsafe_allow_html=True)
    # --- ENDE NEU ---
    
    # --- Ergebnis-Loop (Neue Karten v2) ---
    for i, (_, r) in enumerate(df.iterrows(), start=1):
        doi_val = str(r.get("doi", "") or "")
        doi_norm = doi_val.lower()
        link_val = _to_http(r.get("link", "") or doi_val)
        title = r.get("title", "") or "(ohne Titel)"
        journal = r.get("journal", "") or ""
        issued = r.get("issued", "") or ""
        authors = r.get("authors", "") or ""
        abstract = r.get("abstract", "") or ""

        left, right = st.columns([0.07, 0.93])
        
        # Checkbox in der linken Spalte
        with left:
            sel_key = _stable_sel_key(r, i) # 'i' startet bei 1, passt zu KORREKTUR 2
            
            if doi_norm: # Nur Checkbox anzeigen, wenn eine DOI vorhanden ist
                # --- KORREKTUR 5 (Sync-Fix): Checkbox an on_change binden ---
                st.checkbox(
                    " ", # Leeres Label
                    # 'value' wird jetzt ignoriert, da der Status
                    # über den 'key' und die 'on_change' callbacks gesteuert wird.
                    # Wir setzen es trotzdem für die initiale Erstellung.
                    value=st.session_state.get(sel_key, False), # Holt den aktuellen Status
                    key=sel_key,
                    label_visibility="hidden", # Versteckt das leere Label
                    on_change=toggle_doi,      # <--- WICHTIG
                    args=(doi_norm, sel_key)   # <--- WICHTIG (übergibt DOI und KEY)
                )
                # --- ENDE KORREKTUR 5 ---

        # Gestaltete Karte in der rechten Spalte
        with right:
            
            # HTML-sichere Inhalte erstellen
            title_safe = html.escape(title)
            meta_safe = html.escape(" · ".join([x for x in [journal, issued] if x]))
            authors_safe = html.escape(authors)
            
            # URLs/Links (sollten nicht escaped werden)
            doi_safe = _to_http(doi_val)
            link_safe = link_val
            
            # Link-Text (sollte escaped werden)
            doi_val_safe = html.escape(doi_val)
            link_val_safe = html.escape(link_val)

            # HTML für DOI und Link (nur wenn vorhanden)
            doi_html = ""
            if doi_val:
                doi_html = '<b>DOI:</b> <a href="' + doi_safe + '" target="_blank">' + doi_val_safe + '</a><br>'
                
            link_html = ""
            if link_val and link_val != doi_safe:
                link_html = '<b>URL:</b> <a href="' + link_safe + '" target="_blank">' + link_val_safe + '</a><br>'
            
            # HTML für Abstract
            if abstract:
                abstract_safe = html.escape(abstract)
                abstract_html = '<b>Abstract</b><br><p class="abstract">' + abstract_safe + '</p>'
            else:
                abstract_html = "<i>Kein Abstract vorhanden.</i>"

            # Die komplette HTML-Karte (sicher mit '+' statt f-string)
            card_html = (
                '<div class="result-card">'
                f'<h3>{title_safe}</h3>'
                f'<div class="meta">{meta_safe}</div>'
                f'<div class="authors">{authors_safe}</div>'
                '<details>'
                '<summary>Details anzeigen</summary>'
                '<div>' +
                doi_html +       # Variable sicher mit + einfügen
                link_html +      # Variable sicher mit + einfügen
                '<br>' +
                abstract_html +  # Variable sicher mit + einfügen
                '</div>'
                '</details>'
                '</div>'
            )
            st.markdown(card_html, unsafe_allow_html=True)
            
    st.divider()
    # --- NEU: Link "Hoch" und Anker "Unten" ---
    # Wir nutzen den gleichen CSS-Hack, nur mit angepassten Rändern
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
    st.subheader("🏁 Aktionen: Download & Versand")

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
                    ok, msg = send_doi_email(
                        to_email,
                        sorted(st.session_state["selected_dois"]),
                        sender_display=sender_display.strip() or None
                    )
                    st.success(msg) if ok else st.error(msg)

else:
    st.info("Noch keine Ergebnisse geladen. Wähle Journals und klicke auf „Let’s go!“")
