import streamlit as st
import pandas as pd
from datetime import date
from paperscout.sources.crossref_strict import fetch_from_crossref_strict
from paperscout.exporters.excel import export_excel
from paperscout.pipeline import dedup_by_doi

st.set_page_config(page_title='paperscout UI', layout='wide')
st.title('paperscout – Journal Pull')

uploaded = st.file_uploader('Journal-Liste (Excel mit Spalte "Journal")', type=['xlsx','xls'])
journals = []
if uploaded is not None:
    df = pd.read_excel(uploaded)
    col = None
    for c in ['Journal','journal','JOURNAL','container-title']:
        if c in df.columns:
            col = c; break
    if col:
        journals = sorted(df[col].dropna().unique().tolist())
    else:
        st.error('Keine Spalte "Journal" gefunden.')

left, right = st.columns(2)
since = left.date_input('Seit (inkl.)', value=date(date.today().year,1,1))
until = right.date_input('Bis (inkl.)', value=date.today())

latest_only = st.checkbox('Nur neueste veröffentlichte Ausgabe', value=False)
include_online_first = st.checkbox('Online-First einbeziehen', value=True)

if st.button('Metadaten ziehen'):
    if not journals:
        st.warning('Bitte erst eine Journal-Liste hochladen.')
    else:
        all_articles = []
        with st.spinner('Hole Daten von Crossref...'):
            for j in journals:
                arts = fetch_from_crossref_strict(journal=j, rows=100, since=str(since), until=str(until))
                if latest_only and arts and any(a.issued for a in arts):
                    latest_date = max([a.issued for a in arts if a.issued])
                    arts = [a for a in arts if a.issued == latest_date]
                all_articles.extend(arts)
        rows = [a.to_excel_row() for a in dedup_by_doi(all_articles)]
        out = pd.DataFrame(rows, columns=['Was will ich?','Journal','Titel','Autoren','DOI','Abstract'])
        st.success(f'Fertig. {len(out)} Zeilen.')
        st.dataframe(out, use_container_width=True)
        out.to_excel('results_ui.xlsx', index=False)
        st.download_button('Download Excel', data=out.to_excel(index=False), file_name='results.xlsx', mime='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
