import typer, asyncio
from typing import Optional, List
from loguru import logger
from .sources.crossref_strict import fetch_from_crossref_strict
from .exporters.excel import export_excel
from .pipeline import dedup_by_doi
import pandas as pd
from pathlib import Path
from .sources.auth import login_and_save
from .sources.apa_fallback import fetch_apa_abstract

app = typer.Typer(no_args_is_help=True, help="paperscout – präzise Abfragen + Login-Fallbacks")

def parse_journals(journals: Optional[str], journals_file: Optional[str]) -> List[str]:
    items: List[str] = []
    if journals:
        items.extend([s.strip() for s in journals.split(';') if s.strip()])
    if journals_file:
        p = Path(journals_file)
        if p.suffix.lower() in {'.xlsx', '.xls'}:
            df = pd.read_excel(p)
            for col in ['Journal','journal','JOURNAL','container-title','container_title']:
                if col in df.columns:
                    items.extend([str(x).strip() for x in df[col].dropna().unique().tolist() if str(x).strip()])
                    break
        else:
            with open(p,'r',encoding='utf-8') as f:
                for line in f:
                    line=line.strip()
                    if line: items.append(line)
    seen=set(); uniq=[]
    for j in items:
        if j not in seen: uniq.append(j); seen.add(j)
    return uniq

@app.command('login')
def login(base: str = typer.Option(..., help='Basis-URL des Publishers, z.B. https://psycnet.apa.org')):
    path = asyncio.run(login_and_save(base))
    typer.echo(f'Gespeicherter Login-Status: {path}')

@app.command('pull')
def pull(
    journals: Optional[str] = typer.Option(None),
    journals_file: Optional[str] = typer.Option(None),
    since: Optional[str] = typer.Option(None, help='YYYY-MM-DD'),
    until: Optional[str] = typer.Option(None, help='YYYY-MM-DD'),
    out: str = typer.Option('results.xlsx'),
    rows: int = typer.Option(50),
    with_publisher_fallback: bool = typer.Option(False, help='Fehlende Abstracts via Publisher (z.B. APA) nachziehen (Login nötig).'),
    latest_only: bool = typer.Option(False, help='Nur Artikel aus der neuesten veröffentlichten Ausgabe'),
    include_online_first: bool = typer.Option(True, help='Auch Online-First inkludieren'),
):
    js = parse_journals(journals, journals_file)
    if not js:
        typer.echo('Keine Journals angegeben.'); raise typer.Exit(1)

    logger.info(f'{len(js)} Journals: {js}')
    all_articles = []
    for j in js:
        try:
            arts = fetch_from_crossref_strict(journal=j, rows=rows, since=since, until=until)
            if latest_only and arts and any(a.issued for a in arts):
                latest_date = max([a.issued for a in arts if a.issued])
                arts = [a for a in arts if a.issued == latest_date]
            # include_online_first heuristic omitted
            all_articles.extend(arts)
            logger.info(f'{j}: {len(arts)} Artikel')
        except Exception as e:
            logger.error(f'Fehler bei {j}: {e}')

    if with_publisher_fallback:
        async def fill_missing(arts):
            for a in arts:
                if not a.abstract and a.doi:
                    try:
                        txt = await fetch_apa_abstract(a.doi)
                        if txt: a.abstract = txt
                    except Exception:
                        pass
            return arts
        all_articles = asyncio.run(fill_missing(all_articles))

    export_excel(dedup_by_doi(all_articles), out)
    typer.echo(f'Fertig: {out}')
