from typing import List, Set
from .schemas import Article

def dedup_by_doi(articles: List[Article]) -> List[Article]:
    seen: Set[str] = set(); out: List[Article] = []
    for a in articles:
        key = (a.doi or "").lower().strip()
        if key and key not in seen: seen.add(key); out.append(a)
        elif not key: out.append(a)
    return out
