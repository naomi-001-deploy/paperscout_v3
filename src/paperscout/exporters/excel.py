import pandas as pd
from typing import List
from ..schemas import Article

def export_excel(articles: List[Article], out_path: str):
    rows = [a.to_excel_row() for a in articles]
    df = pd.DataFrame(rows, columns=["Was will ich?", "Journal", "Titel", "Autoren", "DOI", "Abstract"])
    df.to_excel(out_path, index=False)
    return out_path
