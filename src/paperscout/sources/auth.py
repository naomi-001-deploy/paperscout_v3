import json, pathlib, asyncio
from typing import Optional
from playwright.async_api import async_playwright

AUTH_DIR = pathlib.Path(".auth")
AUTH_DIR.mkdir(exist_ok=True)

def _storage_path_for(base: str) -> pathlib.Path:
    host = base.replace("https://","").replace("http://","").split("/")[0]
    return AUTH_DIR / f"{host}.json"

async def login_and_save(base: str) -> str:
    storage = _storage_path_for(base)
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        context = await browser.new_context(storage_state=None)
        page = await context.new_page()
        await page.goto(base, wait_until="load")
        print("Bitte über Hochschul-Login/SSO anmelden und Zugriff prüfen.")
        print("Lass das Fenster offen; es wird nach 10 Minuten automatisch gespeichert oder beende manuell.")
        await page.wait_for_timeout(60000*10)
        await context.storage_state(path=str(storage))
        await browser.close()
    return str(storage)

def ensure_storage_file(base: str) -> Optional[str]:
    p = _storage_path_for(base)
    return str(p) if p.exists() else None
