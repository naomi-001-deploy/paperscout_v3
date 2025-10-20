# paperscout v3

- Exakte Journalabfragen (ISSN/Title strict)
- Login-Fallback via Playwright (z. B. APA PsycNet) zum Nachziehen fehlender Abstracts (ToS beachten!)
- Kleine Streamlit-App zur Auswahl von Journals, Zeitraum, und Filter "aktuellste Ausgabe" vs. "Online first"

## Login-Flow (einmalig pro Publisher/SSO)
1) `poetry run paperscout login --base https://psycnet.apa.org` (öffnet Browser)
2) Über Hochschulzugang/SSO einloggen, bis Zugriff besteht
3) Fenster schließen → Cookies werden in `.auth/psycnet.apa.org.json` gespeichert

## Danach
`poetry run paperscout pull --journals-file ... --since 2025-09-01 --with-publisher-fallback --out results.xlsx`

## UI starten
`poetry run paperscout-ui`
