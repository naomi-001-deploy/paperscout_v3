# app_v6_openai.py – Paperscout mit Crossref + Semantic Scholar + OpenAlex + OpenAI-Fallback + optionalem TOC-Filter
import os, re, html, json
import streamlit as st
import pandas as pd
import httpx
from io import BytesIO
from datetime import date, datetime
from typing import List, Optional, Dict

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
def _headers(extra: dict | None = None):
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
            if r.status_code == 403 and "sciencedirect.com" in url:
                r = c.get(url, headers=_headers({"Referer": "https://www.sciencedirect.com/"}))
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

JOURNAL_ISSN = {
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
}

# =========================
# Aktuelles Heft (TOC) – Registry & Tools
# =========================
JOURNAL_REGISTRY = {
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
}

def _dedupe_keep_order(urls: list[str]) -> list[str]:
    seen=set(); out=[]
    for u in urls:
        if u not in seen:
            seen.add(u); out.append(u)
    return out

def _links_sciencedirect_issue(html_text: str) -> list[str]:
    hrefs = re.findall(r'href=["\'](?:https?:\/\/www\.sciencedirect\.com)?(\/science\/article\/pii\/[A-Z0-9]+)["\']', html_text, flags=re.I)
    return ["https://www.sciencedirect.com"+h for h in _dedupe_keep_order(hrefs)]

def _pick_latest_sciencedirect_issue(issues_html: str, journal_slug: str) -> Optional[str]:
    m = re.findall(rf'href=["\'](\/journal\/{journal_slug}\/vol\/(\d+)\/issue\/(\d+))["\']', issues_html, flags=re.I)
    if not m:  # Fallback auf /latest
        return f"https://www.sciencedirect.com/journal/{journal_slug}/latest"
    tuples = [(int(v), int(i), p) for (p, v, i) in m]
    tuples.sort(reverse=True)
    return "https://www.sciencedirect.com" + tuples[0][2]

def _links_sage_toc(html_text: str) -> list[str]:
    hrefs = re.findall(r'href=["\'](\/doi\/(?:full|abs|epub)\/[^"\']+)["\']', html_text, flags=re.I)
    hrefs = [h.replace("/abs/","/full/") for h in hrefs]
    return ["https://journals.sagepub.com"+h for h in _dedupe_keep_order(hrefs)]

def _links_wiley_toc(html_text: str) -> list[str]:
    hrefs = re.findall(r'href=["\'](\/doi\/(?:full|abs)\/[^"\']+)["\']', html_text, flags=re.I)
    hrefs = [h.replace("/abs/","/full/") for h in hrefs]
    return ["https://onlinelibrary.wiley.com"+h for h in _dedupe_keep_order(hrefs)]

def _links_informs_toc(html_text: str) -> list[str]:
    hrefs = re.findall(r'href=["\'](\/doi\/(?:full|abs)\/[^"\']+)["\']', html_text, flags=re.I)
    hrefs = [h.replace("/abs/","/full/") for h in hrefs]
    return ["https://pubsonline.informs.org"+h for h in _dedupe_keep_order(hrefs)]

def _links_aom_toc(html_text: str) -> list[str]:
    hrefs = re.findall(r'href=["\'](\/doi\/(?:full|abs)\/[^"\']+)["\']', html_text, flags=re.I)
    hrefs = [h.replace("/abs/","/full/") for h in hrefs]
    return ["https://journals.aom.org"+h for h in _dedupe_keep_order(hrefs)]

def _links_hogrefe_toc(html_text: str) -> list[str]:
    hrefs = re.findall(r'href=["\'](\/doi\/(?:full|abs)\/[^"\']+)["\']', html_text, flags=re.I)
    hrefs = [h.replace("/abs/","/full/") for h in hrefs]
    return ["https://econtent.hogrefe.com"+h for h in _dedupe_keep_order(hrefs)]

def _links_apa_toc(html_text: str) -> list[str]:
    hrefs = re.findall(r'href=["\'](https:\/\/psycnet\.apa\.org\/(?:record|fulltext)\/[^"\']+)["\']', html_text, flags=re.I)
    return _dedupe_keep_order(hrefs)

def _fetch_current_issue_links(journal_name: str) -> list[str]:
    """Gibt alle Artikel-Links der aktuellen Ausgabe zurück."""
    cfg = JOURNAL_REGISTRY.get(journal_name)
    if not cfg:
        return []
    pub = cfg["publisher"]

    # 1) TOC-HTML bestimmen
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
def fetch_crossref(issn:str,since:str,until:str,rows:int)->List[dict]:
    url=f"{CR_BASE}/journals/{issn}/works?filter=from-pub-date:{since},until-pub-date:{until}&sort=published&order=desc&rows={rows}"
    try:
        with httpx.Client(timeout=30,headers=_headers()) as c:
            r=c.get(url);r.raise_for_status()
            items=r.json().get("message",{}).get("items",[])
    except Exception as e:
        if st.session_state.get("debug_mode"): st.error(f"Crossref-Fehler ({issn}): {e}")
        return []
    out=[]
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

def fetch_semantic(doi:str)->Optional[dict]:
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

def fetch_openalex(doi:str)->Optional[dict]:
    api=f"https://api.openalex.org/works/DOI:{doi}"
    try:
        r=httpx.get(api,timeout=15)
        if r.status_code!=200:return None
        js=r.json()
        abs_text=""
        if "abstract_inverted_index" in js:
            # einfache Rekonstruktion; ausreichend für Kurztexte
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

def ai_extract_metadata_from_html(html_text:str,model:str)->Optional[dict]:
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

# =========================
# ScienceDirect / Elsevier – direkter JSON-Endpoint
# =========================
def fetch_sciencedirect_abstract(doi_or_url: str) -> Optional[str]:
    """
    Holt Abstracts direkt von ScienceDirect (Elsevier),
    indem der PII-Endpunkt abgefragt wird.
    """
    # Beispiel: https://doi.org/10.1016/j.leaqua.2024.101792
    m = re.search(r"(S\d{16,})", doi_or_url)
    pii = m.group(1) if m else None
    if not pii:
        # Falls DOI, dann HTML holen und PII extrahieren
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
def collect_all(journal:str,since:str,until:str,rows:int,ai_model:str)->List[dict]:
    issn = JOURNAL_ISSN.get(journal)
    if not issn:
        return []

    base = fetch_crossref(issn, since, until, rows)
    out: List[dict] = []

    for rec in base:
        # 1) Crossref liefert Abstract?
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
        # -> wenn URL sciencedirect ist ODER Journal in Registry als sciencedirect markiert
        if not rec.get("abstract"):
            is_sd_url = "sciencedirect.com" in (rec.get("url","") or "")
            is_sd_journal = JOURNAL_REGISTRY.get(journal, {}).get("publisher") == "sciencedirect"
            if is_sd_url or is_sd_journal:
                abs_text = fetch_sciencedirect_abstract(rec.get("url") or rec.get("doi",""))
                if abs_text:
                    rec["abstract"] = abs_text

        # 4) KI-Fallback (OpenAI, nur aus HTML, kein Halluzinieren)
        if not rec.get("abstract") and rec.get("url"):
            html_text = fetch_html(rec["url"])
            if html_text:
                ai = ai_extract_metadata_from_html(html_text, ai_model)
                if ai and ai.get("abstract"):
                    for k in ["title", "authors", "journal", "issued", "abstract", "doi"]:
                        if not rec.get(k):
                            rec[k] = ai.get(k)

        out.append(rec)

    return out

def dedup(items:List[dict])->List[dict]:
    seen=set();out=[]
    for a in items:
        d=(a.get("doi") or "").lower()
        if d in seen: continue
        seen.add(d); out.append(a)
    return out

def _canon(u: str) -> str:
    return (u or "").strip().rstrip("/").lower()

def filter_to_current_issue(records: list[dict], journal_name: str) -> list[dict]:
    """
    Schneidet die Records auf genau die Artikel der aktuellen TOC zu.
    Matching primär über URL; für Wiley/SAGE/AOM/INFORMS sind DOIs oft im Link.
    """
    toc_links = set(_canon(u) for u in _fetch_current_issue_links(journal_name))
    if not toc_links:
        return []  # keine TOC? Dann lieber leer zurückgeben.

    out = []
    for r in records:
        url = _canon(r.get("url",""))
        doi = (r.get("doi") or "").strip().lower()
        if url and url in toc_links:
            out.append(r); continue
        url_norm = url.replace("/abs/","/full/")
        if url_norm in toc_links:
            out.append(r); continue
        if doi and any(doi in tl for tl in toc_links):
            out.append(r); continue
        if "sciencedirect.com" in url and any("/pii/" in tl for tl in toc_links):
            out.append(r); continue

    return out

# =========================
# UI (identisch zu bisher)
# =========================
st.title("paperscout – Journal Picker & Zeitraum")

col1,col2=st.columns([2,1])
with col1:
    st.markdown("### 1) Journals auswählen")

    journals = sorted(JOURNAL_ISSN.keys())

    # stabiler Key je Journal
    def _chk_key(name: str) -> str:
        return "chk_" + re.sub(r"\W+", "_", name.lower()).strip("_")

    if not journals:
        st.info("Keine Journals gefunden.")
        chosen = []
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

        chosen = []
        cols = st.columns(3)
        for idx, j in enumerate(journals):
            k = _chk_key(j)
            current_val = st.session_state.get(k, False)
            with cols[idx % 3]:
                if st.checkbox(j, value=current_val, key=k):
                    chosen.append(j)

        st.markdown(f"**{len(chosen)}** ausgewählt")

with col2:
    st.markdown("### 2) Zeitraum & Optionen")
    today=date.today()
    since=st.date_input("Seit (inkl.)",value=date(today.year,1,1))
    only_current_issue = st.checkbox("Nur aktuelles Heft (TOC-Filter)", value=False)
    until=st.date_input("Bis (inkl.)",value=today)
    rows=st.number_input("Max. Treffer pro Journal",min_value=5,max_value=200,step=5,value=50)
    debug=st.checkbox("Debug anzeigen",value=False)
    st.session_state["debug_mode"]=debug
    ai_model=st.text_input("OpenAI Modell",value="gpt-4o-mini")
    api_key_input=st.text_input("🔑 OpenAI API-Key",type="password",value="")
    if api_key_input:
        os.environ["PAPERSCOUT_OPENAI_API_KEY"]=api_key_input
        st.caption("API-Key gesetzt.")

st.markdown("---")
run=st.button("🚀 Let´s go! Metadaten ziehen")

if run:
    if not chosen:
        st.warning("Bitte mindestens ein Journal auswählen.")
    else:
        st.info("Starte Abruf — Crossref, Semantic Scholar, OpenAlex, KI-Fallback.")
        all_rows=[]; progress=st.progress(0); n=len(chosen)
        s_since,s_until=str(since),str(until)
        for i,j in enumerate(chosen,1):
            st.write(f"Quelle: {j}")
            rows_j=collect_all(j,s_since,s_until,int(rows),ai_model)

            # ✂️ Nur aktuelles Heft (optional)
            if only_current_issue:
                rows_j = filter_to_current_issue(rows_j, j)

            all_rows.extend(rows_j)
            progress.progress(int(i/n*100))
        progress.empty()
        all_rows=dedup(all_rows)
        if not all_rows:
            st.warning("Keine Treffer gefunden.")
        else:
            df=pd.DataFrame([{
                "Journal":r.get("journal",""),
                "Titel":r.get("title",""),
                "Autoren":r.get("authors",""),
                "DOI":(f"https://doi.org/{r['doi']}" if r.get("doi") and not str(r['doi']).startswith("http") else r.get("doi","")),
                "URL":r.get("url",""),
                "Abstract":r.get("abstract",""),
                "Issued":r.get("issued","")
            } for r in all_rows])
            st.success(f"Fertig — {len(df)} Zeilen")
            st.dataframe(df,use_container_width=True)
            towrite=BytesIO()
            with pd.ExcelWriter(towrite,engine="openpyxl") as writer:
                df.to_excel(writer,index=False,sheet_name="results")
            towrite.seek(0)
            st.download_button("Download Excel (results.xlsx)",towrite,file_name="results.xlsx",
                               mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
