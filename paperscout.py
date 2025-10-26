# app_v6_openai.py – Paperscout (Nur API-Version)
# Bereinigt von TOC-Scraping-Logik für Cloud-Hosting.
# UI-Update: Ergebnisse mit st.expander einklappbar.
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
# (Alle Funktionen bzgl. JOURNAL_REGISTRY, _links_*, _fetch_current_issue_links,
# filter_to_current_issue, fetch_aom_toc_fallback etc. wurden entfernt)
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
    until = st.date_input("Bis (inkl.)", value=today)

    # --- TOC-Filter Checkbox entfernt ---
    
    # „letzte 30 Tage“
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
    proxy_url = st.text_input("🌐 Proxy (optional, http/httpshttps/socks5)", value=os.getenv("PAPERSCOUT_PROXY", ""))
    if proxy_url:
        st.session_state["proxy_url"] = proxy_url.strip()
        st.caption("Proxy aktiv. Requests laufen über diese Adresse.")
    else:
        st.session_state["proxy_url"] = ""

    if crossref_mail:
        os.environ["CROSSREF_MAILTO"] = crossref_mail
        st.caption("Crossref-Mailto gesetzt (bessere Crossref-Ergebnisse).")

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
    if "chosen" not in locals():
        chosen = []
    if not chosen:
        st.warning("Bitte mindestens ein Journal auswählen.")
    else:
        st.info("Starte Abruf — Crossref, Semantic Scholar, OpenAlex, KI-Fallback.")

        all_rows: List[Dict[str, Any]] = []
        progress = st.progress(0)
        n = len(chosen)

        if last30:
            s_since = (today - timedelta(days=30)).isoformat()
            s_until = today.isoformat()
        else:
            s_since, s_until = str(since), str(until)

        for i, j in enumerate(chosen, 1):
            st.write(f"Quelle: {j}")
            rows_j = collect_all(j, s_since, s_until, int(rows), ai_model)

            # --- TOC-Filter-Logik entfernt ---

            rows_j = dedup(rows_j)
            all_rows.extend(rows_j)

            progress.progress(min(i / max(n, 1), 1.0))

        progress.empty()
        if not all_rows:
            st.warning("Keine Treffer im gewählten Zeitraum/Journals.")
        else:
            df = pd.DataFrame(all_rows)
            cols = [c for c in ["title", "doi", "issued", "journal", "authors", "abstract", "url"] if c in df.columns]
            if cols:
                df = df[cols]

            st.session_state["results_df"] = df
            st.session_state["selected_dois"] = set()
            st.success(f"{len(df)} Treffer geladen.")

# --- Persistente Ergebnisanzeige: Karten-Layout mit Expander ---
st.markdown("---")
st.subheader("Ergebnisse")

if "results_df" in st.session_state and not st.session_state["results_df"].empty:
    df = st.session_state["results_df"].copy()

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

    if "url" in df.columns:
        df["link"] = df["url"].apply(_to_http)
    elif "doi" in df.columns:
        df["link"] = df["doi"].apply(_to_http)
    else:
        df["link"] = ""

    if "selected_dois" not in st.session_state:
        st.session_state["selected_dois"] = set()

    st.caption("Klicke links auf die Checkbox, um Einträge für den E-Mail-Versand auszuwählen.")

    import re
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
        with left:
            sel_key = _stable_sel_key(r, i)
            chk = st.checkbox(
                "",
                value=(doi_norm in st.session_state["selected_dois"]),
                key=sel_key,
            )
            if chk and doi_norm:
                st.session_state["selected_dois"].add(doi_norm)
            elif not chk and doi_norm:
                st.session_state["selected_dois"].discard(doi_norm)

        # === UI-ÄNDERUNG HIER ===
        with right:
            # --- Standardansicht (immer sichtbar) ---
            st.markdown(f"### {title}")
            meta = " · ".join([x for x in [journal, issued] if x])
            if meta:
                st.caption(meta)

            if authors:
                st.markdown(f"**Autor:innen:** {authors}")

            # --- Ausklappbare Details ---
            with st.expander("Details anzeigen (Abstract, DOI, URL)"):
                if doi_val:
                    st.markdown(f"**DOI:** {_to_http(doi_val)}")
                if link_val and link_val != _to_http(doi_val):
                    st.markdown(f"**URL:** {link_val}")

                if abstract:
                    st.markdown("**Abstract**")
                    st.write(abstract)
                else:
                    st.info("Kein Abstract vorhanden.")
        # === ENDE UI-ÄNDERUNG ===

        st.divider()

    b1, b2, b3, _ = st.columns([1.2, 1.2, 2, 4])
    with b1:
        if st.button("Alles auswählen"):
            all_vis = set(df["doi"].dropna().astype(str).str.lower())
            st.session_state["selected_dois"].update(all_vis)
            st.rerun()
    with b2:
        if st.button("Alle abwählen"):
            st.session_state["selected_dois"].clear()
            st.rerun()
    with b3:
        st.caption(f"Aktuell ausgewählt: **{len(st.session_state['selected_dois'])}** DOI(s)")

    def df_to_excel_bytes(df_in: pd.DataFrame) -> BytesIO | None:
        engine = _pick_excel_engine()
        if engine is None:
            return None
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

    col_dl1, col_dl2 = st.columns([1.6, 2])
    with col_dl1:
        x_all = df_to_excel_bytes(df)
        if x_all is not None:
            st.download_button(
                "⬇️ Excel — alle Ergebnisse",
                data=x_all,
                file_name="paperscout_results.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
        else:
            st.warning("Keine Excel-Engine gefunden (xlsxwriter/openpyxl). Biete CSV-Fallback an.")
            st.download_button(
                "⬇️ CSV — alle Ergebnisse",
                data=_df_to_csv_bytes(df),
                file_name="paperscout_results.csv",
                mime="text/csv",
            )

    with col_dl2:
        if st.session_state["selected_dois"]:
            df_sel = df[df["doi"].astype(str).str.lower().isin(st.session_state["selected_dois"])].copy()
            x_sel = df_to_excel_bytes(df_sel)
            if x_sel is not None:
                st.download_button(
                    "⬇️ Excel — nur ausgewählte",
                    data=x_sel,
                    file_name="paperscout_selected.xlsx",
                    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                )
            else:
                st.download_button(
                    "⬇️ CSV — nur ausgewählte",
                    data=_df_to_csv_bytes(df_sel),
                    file_name="paperscout_selected.csv",
                    mime="text/csv",
                )
        else:
            st.button("⬇️ Excel — nur ausgewählte", disabled=True)

    # --- E-Mail Versand ---
    st.markdown("### 📧 DOI-Liste per E-Mail senden")
    sender_display = st.text_input(
        "Absendername (wird in der E-Mail angezeigt)",
        value="",
        placeholder="z. B. Naomi oder Ralf"
    )
    to_email = st.text_input("Empfänger-E-Mail-Adresse", key="doi_email_to")
    if st.button("DOI-Liste senden"):
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
