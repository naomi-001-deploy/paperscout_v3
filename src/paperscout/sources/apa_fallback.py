import re, httpx
from typing import Optional
from urllib.parse import quote
from bs4 import BeautifulSoup

def fetch_apa_abstract(doi: str) -> Optional[str]:
    """
    Holt Abstracts über DOI (APA PsycNet) – nutzt IP/VPN-basierten Zugriff.
    Kein Login, keine Cookies nötig.
    """
    url = f"https://doi.org/{quote(doi)}"
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/123 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml",
    }
    try:
        with httpx.Client(timeout=30.0, headers=headers, follow_redirects=True) as c:
            r = c.get(url)
            r.raise_for_status()
            html = r.text
        soup = BeautifulSoup(html, "lxml")
        for sel in [
            "section.abstract", "div.abstract", "div#abstract",
            "div.article-abstract", "[data-test='abstract']",
            "div.col-abstract", "article#abstract"
        ]:
            el = soup.select_one(sel)
            if el:
                txt = re.sub(r"\s+", " ", el.get_text(" ", strip=True))
                if txt and len(txt) > 20:
                    return txt
    except Exception:
        return None
    return None
