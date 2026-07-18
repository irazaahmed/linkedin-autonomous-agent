"""Ek-baari ka X login import — jab automated window me X ka login
verification-loop me phans jaye (phone number ke baad aagay na barhe).

Idea: apne ROZANA wale normal Chrome me x.com par login karo — wahan X ka
bot-check nahi atakta. Phir wahan se `auth_token` cookie copy kar ke yahan
paste karo. Ye script us se Playwright-format cookies bana kar
session/x_cookies.json me save karti hai, aur aakhir me ek browser khol ke
VERIFY karti hai ke session sach me chal raha hai. Uske baad x_watcher ko
login ka marhala kabhi nahi aayega.

auth_token kahan se milega (normal Chrome me):
  1. x.com kholo aur login karo
  2. F12 dabao (DevTools) -> upar "Application" tab
  3. Left side: Storage -> Cookies -> https://x.com
  4. List me `auth_token` wali row dhundo -> uski "Value" copy karo
     (lambi hex string hoti hai, ~40 characters)
"""

import asyncio
import json
import sys
import time

from playwright.async_api import async_playwright

import linkedin_watcher as lw
from x_watcher import COOKIES_FILE, X_HOME, is_logged_in


def build_cookies(auth_token: str, ct0: str | None = None) -> list[dict]:
    """auth_token hi asal session hai; ct0 (CSRF) na ho to X pehli visit par
    khud naya set kar deta hai. Dono .x.com aur .twitter.com par lagate hain
    — redirects kisi bhi domain se aa sakte hain."""
    expires = time.time() + 180 * 24 * 3600
    cookies = []
    for domain in (".x.com", ".twitter.com"):
        cookies.append({
            "name": "auth_token", "value": auth_token, "domain": domain,
            "path": "/", "expires": expires,
            "httpOnly": True, "secure": True, "sameSite": "None",
        })
        if ct0:
            cookies.append({
                "name": "ct0", "value": ct0, "domain": domain,
                "path": "/", "expires": expires,
                "httpOnly": False, "secure": True, "sameSite": "Lax",
            })
    return cookies


def _clean(value: str) -> str:
    value = value.strip().strip('"').strip("'")
    if "=" in value:  # "auth_token=abc..." paste kar diya to prefix hata do
        value = value.split("=", 1)[1]
    return value.strip()


async def verify() -> bool:
    """Save hui cookies se browser khol ke check karo ke /home khulta hai —
    yehi proof hai ke session zinda hai."""
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=False,
            channel="chrome",
            args=["--disable-blink-features=AutomationControlled",
                  "--no-first-run", "--no-default-browser-check"],
        )
        context = await browser.new_context(viewport={"width": 1280, "height": 900})
        await lw.load_cookies(context, COOKIES_FILE)
        page = await context.new_page()
        await page.goto(X_HOME, wait_until="domcontentloaded", timeout=60000)
        await asyncio.sleep(6)
        ok = is_logged_in(page.url)
        if ok:
            # X ne is visit par jo taza cookies (ct0 waghera) set kin, wo bhi
            # save kar lo — agli runs aur smooth hongi.
            await lw.save_cookies(context, COOKIES_FILE)
        await browser.close()
        return ok


def main():
    print("\n" + "=" * 55)
    print("   X Login Import — normal Chrome se session uthao")
    print("=" * 55)
    print("""
  auth_token kahan se milega (apne NORMAL Chrome me):
    1. x.com kholo aur login karo
    2. F12 dabao (DevTools) -> upar "Application" tab
    3. Left side: Storage -> Cookies -> https://x.com
    4. `auth_token` wali row -> uski "Value" copy karo
       (lambi hex string hoti hai, ~40 characters)
""")

    auth_token = _clean(input(">> auth_token paste karo: "))
    if len(auth_token) < 20:
        print("[!] Ye auth_token nahi lagta (bahut chhota hai). Dobara chalao.")
        sys.exit(1)

    ct0 = _clean(input(">> ct0 paste karo (optional — Enter daba ke skip): "))

    COOKIES_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(COOKIES_FILE, "w", encoding="utf-8") as f:
        json.dump(build_cookies(auth_token, ct0 or None), f, indent=2)
    print(f"\n[*] Cookies save ho gain: {COOKIES_FILE}")

    print("[*] Verify kar rahe hain — browser khulega, kuch second lagenge...")
    if asyncio.run(verify()):
        print("\n[OK] Session VERIFIED — X home timeline khul gayi!")
        print("     Ab dashboard se 'Run X Agent' chalao, login nahi poochhega.")
    else:
        print("\n[!] Session verify NAHI hua — home timeline nahi khuli.")
        print("    auth_token dobara copy karo (poori value, koi space na ho)")
        print("    aur ye script phir chalao.")
        sys.exit(1)


if __name__ == "__main__":
    main()
