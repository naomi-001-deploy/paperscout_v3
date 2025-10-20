import httpx, os, re
from typing import List, Optional, Tuple
from tenacity import retry, wait_exponential, stop_after_attempt
from ..schemas import Article

CR_BASE = "https://api.crossref.org"

def _headers():
    mailto = os.getenv("CROSSREF_MAILTO") or "unknown@example.com"
    ua = f"paperscout/0.3 (+mailto:{mailto})"
    return {"User-Agent": ua}

@retry(wait=wait_exponential(multiplier=1, min=1, max=20), stop=stop_after_attempt(5))
def resolve_issn_by_title(journal_title: str) -> Tuple[Optional[str], str]:
    params = {"query": journal_title, "rows": "5"}
    with httpx.Client(timeout=30.0, headers=_headers()) as c:
        r = c.get(f"{CR_BASE}/journals", params=params)
        r.raise_for_status()
        items = r.json().get("message", {}).get("items", [])
        jt_norm = _norm(journal_title)
        best = None
        for it in items:
            title = it.get("title") or ""
            if _norm(title) == jt_norm:
                best = it; break
        if not best and items:
            best = items[0]
        if best:
            issn_list = best.get("ISSN") or []
            return (issn_list[0] if issn_list else None, best.get("title") or journal_title)
    return (None, journal_title)

def _norm(s: str) -> str:
    import re
    return re.sub(r"\s+", " ", (s or "").strip().lower())

def _extract_issued(item) -> Optional[str]:
    for key in ("issued", "published-online", "published-print"):
        v = item.get(key, {}).get("date-parts")
        if v and isinstance(v, list) and v and isinstance(v[0], list):
            parts = v[0] + [1,1,]
            y, m, d = parts[0], parts[1], parts[2]
            return f"{y:04d}-{m:02d}-{d:02d}"
    return None

@retry(wait=wait_exponential(multiplier=1, min=1, max=20), stop=stop_after_attempt(5))
def fetch_from_crossref_strict(journal: str, rows: int = 50, since: Optional[str] = None, until: Optional[str] = None) -> List[Article]:
    issn, resolved_title = resolve_issn_by_title(journal)
    params = {
        "rows": str(rows),
        "select": "DOI,title,author,container-title,abstract,issued,type,ISSN",
        "sort": "published",
        "order": "desc",
        "filter": "type:journal-article",
    }
    filt = ["type:journal-article"]
    if since: filt.append(f"from-pub-date:{since}")
    if until: filt.append(f"until-pub-date:{until}")
    if issn:  filt.append(f"issn:{issn}")
    else:     params["query.container-title"] = journal
    params["filter"] = ",".join(filt)

    arts: List[Article] = []
    with httpx.Client(timeout=30.0, headers=_headers()) as c:
        r = c.get(f"{CR_BASE}/works", params=params)
        r.raise_for_status()
        items = r.json().get("message", {}).get("items", [])

    jt = _norm(journal)
    for it in items:
        cont = " ".join(it.get("container-title") or []).strip()
        cont_norm = _norm(cont)
        its_issn = [s.lower() for s in (it.get("ISSN") or [])]
        ok = False
        if issn and any(s.lower() == issn.lower() for s in its_issn): ok = True
        elif cont_norm == jt: ok = True
        if not ok: continue

        issued = _extract_issued(it)
        if since and issued and issued < since: continue
        if until and issued and issued > until: continue

        title = " ".join(it.get("title") or []).strip()
        doi = it.get("DOI")
        auths = []
        for a in it.get("author") or []:
            name = " ".join([p for p in [a.get("given"), a.get("family")] if p])
            if name: auths.append(name)
        abstract = it.get("abstract")
        if abstract:
            import re
            abstract = re.sub(r"<[^>]+>", " ", abstract)
            abstract = " ".join(abstract.split())
        arts.append(Article(journal=cont or resolved_title or journal, title=title, authors=auths, doi=doi, abstract=abstract, issued=issued))
    return arts
