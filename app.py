# app.py
import streamlit as st
import pandas as pd
import httpx
import os
import re
from io import BytesIO
from datetime import date
from typing import List, Optional, Tuple

st.set_page_config(page_title="paperscout UI", layout="wide")

# -----------------------
# Utilities / Crossref
# -----------------------
CR_BASE = "https://api.crossref.org"
def _headers():
    mailto = os.getenv("CROSSREF_MAILTO") or "you@example.com"
    return {"User-Agent": f"paperscout-ui (+mailto:{mailto})"}

def norm(s: Optional[str]) -> str:
    if not s:
        return ""
    return re.sub(r"\s+", " ", s.strip().lower())

def resolve_issn_by_title(journal_title: str) -> Tuple[Optional[str], str]:
    """Versucht, ISSN für einen Journal-Titel über /journals Endpunkt zu lösen."""
    try:
        with httpx.Client(timeout=20.0, headers=_headers()) as c:
            r = c.get(f"{CR_BASE}/journals", params={"query": journal_title, "rows": 5})
            r.raise_for_status()
            items = r.json().get("message", {}).get("items", [])
            jt_norm = norm(journal_title)
            best = None
            for it in items:
                title = it.get("title") or ""
                if norm(title) == jt_norm:
                    best = it; break
            if not best and items:
                best = items[0]
            if best:
                issn_list = best.get("ISSN") or []
                return (issn_list[0] if issn_list else None, best.get("title") or journal_title)
    except Exception:
        pass
    return (None, journal_title)

def extract_issued(item) -> Optional[str]:
    for key in ("issued", "published-online", "published-print"):
        v = item.get(key, {}).get("date-parts")
        if v and isinstance(v, list) and v and isinstance(v[0], list):
            parts = v[0] + [1,1]
            y, m, d = parts[0], parts[1], parts[2]
            return f"{y:04d}-{m:02d}-{d:02d}"
    return None

def fetch_from_crossref_strict(journal: str, rows: int = 50, since: Optional[str] = None, until: Optional[str] = None) -> List[dict]:
    """Holt Artikel-Metadaten (Crossref). Liefert Liste von dicts mit keys: journal,title,authors,doi,abstract,issued"""
    issn, resolved_title = resolve_issn_by_title(journal)
    params = {
        "rows": str(rows),
        "select": "DOI,title,author,container-title,abstract,issued,type,ISSN",
        "sort": "published",
        "order": "desc",
    }
    filters = []
    filters.append("type:journal-article")
    if since:
        filters.append(f"from-pub-date:{since}")
    if until:
        filters.append(f"until-pub-date:{until}")
    if issn:
        filters.append(f"issn:{issn}")
    else:
        # wenn keine ISSN gefunden, versuchen Container-Title query
        params["query.container-title"] = journal
    if filters:
        params["filter"] = ",".join(filters)

    try:
        with httpx.Client(timeout=30.0, headers=_headers()) as c:
            resp = c.get(f"{CR_BASE}/works", params=params)
            resp.raise_for_status()
            items = resp.json().get("message", {}).get("items", [])
    except Exception as e:
        st.error(f"Fehler bei Crossref-Anfrage für '{journal}': {e}")
        return []

    jt = norm(journal)
    out = []
    for it in items:
        cont = " ".join(it.get("container-title") or []).strip()
        cont_norm = norm(cont)
        its_issn = [s.lower() for s in (it.get("ISSN") or [])]
        ok = False
        if issn and any(s.lower() == (issn or "").lower() for s in its_issn):
            ok = True
        elif cont_norm == jt:
            ok = True
        if not ok and issn:  # allow issn match only
            continue
        issued = extract_issued(it)
        if since and issued and issued < since: 
            continue
        if until and issued and issued > until:
            continue
        title = " ".join(it.get("title") or []).strip()
        doi = it.get("DOI")
        auths = []
        for a in it.get("author") or []:
            name = " ".join([p for p in [a.get("given"), a.get("family")] if p])
            if name: auths.append(name)
        abstract = it.get("abstract")
        if abstract:
            abstract = re.sub(r"<[^>]+>", " ", abstract)
            abstract = " ".join(abstract.split())
        out.append({
            "journal": cont or resolved_title or journal,
            "title": title,
            "authors": ", ".join(auths),
            "doi": doi,
            "abstract": abstract or "",
            "issued": issued or ""
        })
    return out

def dedup_by_doi_dicts(items: List[dict]) -> List[dict]:
    seen = set(); out = []
    for a in items:
        key = (a.get("doi") or "").lower().strip()
        if key and key not in seen:
            seen.add(key); out.append(a)
        elif not key:
            out.append(a)
    return out

# -----------------------
# Streamlit UI
# -----------------------
st.title("paperscout – Journal Picker & Zeitraum")

col1, col2 = st.columns([2,1])

with col1:
    st.markdown("### 1) Journalliste hochladen (Excel) oder lokale Datei verwenden")
    uploaded = st.file_uploader("Excel mit Spalte 'Journal' (xlsx/xls)", type=["xlsx","xls"])
    journals = []
    if uploaded is not None:
        try:
            df = pd.read_excel(uploaded)
            possible_cols = ["Journal","journal","JOURNAL","container-title","container_title"]
            found = None
            for c in possible_cols:
                if c in df.columns:
                    found = c; break
            if not found:
                st.error("Die hochgeladene Datei enthält keine Spalte 'Journal'. Bitte Spalte umbenennen.")
            else:
                journals = sorted(df[found].dropna().astype(str).unique().tolist())
        except Exception as e:
            st.error(f"Fehler beim Lesen der Datei: {e}")
    else:
        # Fallback: lade journals_input.xlsx im Projektordner, falls vorhanden
        if os.path.exists("journals_input.xlsx"):
            try:
                df = pd.read_excel("journals_input.xlsx")
                for c in ["Journal","journal","JOURNAL","container-title","container_title"]:
                    if c in df.columns:
                        journals = sorted(df[c].dropna().astype(str).unique().tolist())
                        break
            except Exception as e:
                st.warning(f"Lokale journals_input.xlsx konnte nicht gelesen werden: {e}")

    if not journals:
        st.info("Keine Journals geladen. Lade eine Excel hoch oder lege 'journals_input.xlsx' in das Projektverzeichnis.")
    else:
        st.markdown("### 2) Wähle Journals (Haken setzen)")
        chosen = st.multiselect("Journals", options=journals, default=journals[:10])
        st.markdown(f"{len(chosen)} ausgewählt")

with col2:
    st.markdown("### 3) Zeitraum & Optionen")
    today = date.today()
    since = st.date_input("Seit (inkl.)", value=date(today.year,1,1))
    until = st.date_input("Bis (inkl.)", value=today)
    latest_only = st.checkbox("Nur neueste veröffentlichte Ausgabe (heuristisch)", value=False)
    include_online_first = st.checkbox("Online-First einbeziehen", value=True)
    rows = st.number_input("Max. Treffer pro Journal", min_value=10, max_value=1000, step=10, value=100)

st.markdown("---")
run = st.button("Metadaten ziehen")

if run:
    if not ('chosen' in locals() and chosen):
        st.warning("Bitte mindestens ein Journal auswählen.")
    else:
        st.info("Starte Abruf — bitte VPN aktivieren, falls du Zugriff auf Campus-Publisher brauchst.")
        all_articles = []
        progress = st.progress(0)
        n = len(chosen)
        for i, j in enumerate(chosen, start=1):
            st.write(f"Ziehe: {j}")
            arts = fetch_from_crossref_strict(journal=j, rows=int(rows), since=str(since), until=str(until))
            if latest_only and arts:
                dates = [a['issued'] for a in arts if a.get('issued')]
                if dates:
                    latest_date = max(dates)
                    arts = [a for a in arts if a.get('issued') == latest_date]
            # include_online_first: Crossref-Angaben sind uneinheitlich; hier vorerst unverändert
            all_articles.extend(arts)
            progress.progress(int(i/n*100))
        progress.empty()
        all_articles = dedup_by_doi_dicts(all_articles)
        if not all_articles:
            st.warning("Keine Treffer gefunden.")
        else:
            df_out = pd.DataFrame([{
                "Was will ich?": False,
                "Journal": a["journal"],
                "Titel": a["title"],
                "Autoren": a["authors"],
                "DOI": (f"https://doi.org/{a['doi']}" if a.get("doi") and not a["doi"].startswith("http") else (a.get("doi") or "")),
                "Abstract": a.get("abstract",""),
                "Issued": a.get("issued","")
            } for a in all_articles])
            st.success(f"Fertig — {len(df_out)} Zeilen")
            st.dataframe(df_out, use_container_width=True)

            # Excel export
            towrite = BytesIO()
            df_out.to_excel(towrite, index=False, engine="openpyxl")
            towrite.seek(0)
            st.download_button(
                label="Download Excel (results.xlsx)",
                data=towrite,
                file_name="results.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            )

