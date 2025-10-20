from pydantic import BaseModel, Field
from typing import List, Optional

class Article(BaseModel):
    journal: str = Field(...)
    title: str
    authors: List[str] = []
    doi: Optional[str] = None
    abstract: Optional[str] = None
    issued: Optional[str] = None  # YYYY-MM-DD

    def to_excel_row(self):
        return {
            "Was will ich?": False,
            "Journal": self.journal,
            "Titel": self.title,
            "Autoren": ", ".join(self.authors) if self.authors else "",
            "DOI": f"https://doi.org/{self.doi}" if self.doi and not self.doi.startswith("http") else (self.doi or ""),
            "Abstract": self.abstract or "",
        }
