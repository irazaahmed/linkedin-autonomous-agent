"""X (Twitter) autonomous engagement agent — home timeline ke relevant tweets
ko Like + short reply karta hai, logged-in X account se.

LinkedIn watcher ke proven patterns (cookie session, dedup, verification-first
clicks, human pacing) yahan reuse hote hain, lekin X ka DOM LinkedIn se kahin
aasaan hai: har element pe stable `data-testid` hota hai aur har tweet ka
permanent `/status/<id>` link — is liye LinkedIn wali content-fingerprint
guessing ki zaroorat nahi, tweet-id hi dedup key hai.

Ek X-specific trap: timeline VIRTUALIZED hai — scroll karne par purane tweet
DOM se nikal jate hain aur wapas aane par naye nodes bante hain. Is liye kabhi
bhi ElementHandle ko scrolls ke paar hold nahi karte; har action se pehle
tweet ko uske status-id se (CSS `:has()`) dobara dhundhte hain."""

import asyncio
import random
import sys
from datetime import datetime, timezone
from pathlib import Path

from playwright.async_api import async_playwright, Page
from playwright.async_api import TimeoutError as PWTimeout

import linkedin_watcher as lw
from comment_generator import generate_comment

X_HOME       = "https://x.com/home"
COOKIES_FILE = lw.SESSION_DIR / "x_cookies.json"
LOG_DIR      = Path("logs/x")
ENGAGED_FILE = LOG_DIR / "engaged.json"
PERSONA_FILE = "persona_x.md"

MAX_POSTS          = lw.MAX_POSTS
MAX_POST_AGE_HOURS = lw.MAX_POST_AGE_HOURS
MIN_TWEET_CHARS    = 40   # itne se chhote tweets pe reply generate karne layak context nahi hota
MAX_REPLY_CHARS    = 275  # X ki 280 limit se thoda neeche, safe margin


def is_logged_in(url: str) -> bool:
    return "/home" in url and "/i/flow" not in url


# Har visible tweet ka snapshot — sirf DATA nikalta hai (id, text, age,
# promoted, liked), koi element reference nahi, kyunke virtualization unhe
# kabhi bhi invalidate kar sakti hai.
_SCAN_TWEETS = """
    () => {
        const out = [];
        for (const art of document.querySelectorAll('article[data-testid="tweet"]')) {
            let id = null;
            const timeEl = art.querySelector('a[href*="/status/"] time');
            const link = timeEl ? timeEl.closest('a') : null;
            if (link) {
                const m = (link.getAttribute('href') || '').match(/\\/status\\/(\\d+)/);
                if (m) id = m[1];
            }
            const textEl = art.querySelector('[data-testid="tweetText"]');
            const promoted =
                !!art.closest('[data-testid="placementTracking"]') ||
                Array.from(art.querySelectorAll('span')).some(s => {
                    const t = (s.textContent || '').trim();
                    return t === 'Ad' || t === 'Promoted';
                });
            out.push({
                id,
                text: textEl ? textEl.innerText : '',
                datetime: timeEl ? timeEl.getAttribute('datetime') : null,
                promoted,
                liked: !!art.querySelector('[data-testid="unlike"]'),
            });
        }
        return out;
    }
"""


def tweet_age_hours(iso: str | None) -> float | None:
    """time[datetime] ka ISO string ("2026-07-18T09:00:00.000Z") -> hours.
    LinkedIn ki "6h"-text-parsing se kahin behtar — exact timestamp milta hai."""
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return (datetime.now(timezone.utc) - dt).total_seconds() / 3600
    except ValueError:
        return None


def truncate_reply(text: str) -> str:
    """280-char limit guard. Persona pehle hi bahut chhota likhwata hai, ye
    sirf safety net hai — sentence boundary pe kaato, warna word boundary."""
    if len(text) <= MAX_REPLY_CHARS:
        return text
    cut = text[:MAX_REPLY_CHARS]
    for mark in (". ", "! ", "? "):
        idx = cut.rfind(mark)
        if idx > 60:
            return cut[: idx + 1].strip()
    return cut[: cut.rfind(" ")].rstrip(" ,;:-")


def find_tweet(page: Page, tweet_id: str):
    """Tweet ko status-id se locate karo — virtualization-safe, har action se
    pehle fresh lookup. Quoted tweets nested article banate hain jo same link
    match kar sakta hai; .first document-order me OUTER article deta hai (jis
    ke paas action bar hai)."""
    return page.locator(
        f'article[data-testid="tweet"]:has(a[href*="/status/{tweet_id}"])'
    ).first


async def close_composer(page: Page):
    """Reply modal ko discard karo (fail hone par) — Escape, aur agar X
    'Discard?' confirmation dikhaye to usay bhi confirm karo."""
    try:
        await page.keyboard.press("Escape")
        await asyncio.sleep(0.8)
        confirm = page.locator('[data-testid="confirmationSheetConfirm"]')
        if await confirm.count():
            await confirm.first.click()
            await asyncio.sleep(0.5)
    except Exception:
        pass


async def like_tweet(page: Page, tweet_id: str) -> bool:
    """Like + VERIFY: click ke baad button ka data-testid 'like' se 'unlike'
    ho jata hai — wahi proof hai ke like sach me laga (LinkedIn lesson: click
    succeeded is not proof it worked)."""
    art = find_tweet(page, tweet_id)
    try:
        await art.scroll_into_view_if_needed(timeout=8000)
        await asyncio.sleep(random.uniform(0.8, 1.6))
        if await art.locator('[data-testid="unlike"]').count():
            print("  [*] Pehle se liked hai.")
            return True
        await art.locator('[data-testid="like"]').first.click(timeout=8000)
        await art.locator('[data-testid="unlike"]').first.wait_for(
            state="visible", timeout=6000
        )
        print("  [OK] Like ho gaya (verified).")
        return True
    except PWTimeout:
        print("  [!] Like verify nahi hua — like button 'unlike' me nahi badla.")
        return False
    except Exception as e:
        print(f"  [!] Like error: {str(e)[:120]}")
        return False


async def reply_to_tweet(page: Page, tweet_id: str, reply_text: str) -> bool:
    """Reply button -> modal composer -> type -> Post -> VERIFY. Success ka
    signal: composer textarea DOM se detach ho jata hai (modal band). Fail ho
    to composer discard kar ke False — adhoora draft khula nahi chhodte."""
    art = find_tweet(page, tweet_id)
    try:
        await art.scroll_into_view_if_needed(timeout=8000)
        await art.locator('[data-testid="reply"]').first.click(timeout=8000)

        box = page.locator('[data-testid="tweetTextarea_0"]')
        await box.wait_for(state="visible", timeout=10000)
        await box.click()
        await asyncio.sleep(random.uniform(0.5, 1.0))

        # Draft.js contenteditable — keyboard events hi reliably register hote
        # hain, isi liye fill() ke bajaye human-paced typing.
        for ch in reply_text:
            await page.keyboard.type(ch)
            await asyncio.sleep(random.uniform(0.03, 0.10))
        await asyncio.sleep(random.uniform(0.8, 1.5))

        post_btn = page.locator('[data-testid="tweetButton"]').first
        if await post_btn.is_disabled():
            print("  [!] Post button disabled (char limit ya restriction) — discard.")
            await close_composer(page)
            return False
        await post_btn.click()

        try:
            await box.wait_for(state="detached", timeout=12000)
        except PWTimeout:
            print("  [!] Composer band nahi hua — reply post hona verify nahi hua.")
            await close_composer(page)
            return False

        print("  [OK] Reply post ho gaya (composer band — verified).")
        return True
    except Exception as e:
        print(f"  [!] Reply error: {str(e)[:150]}")
        await close_composer(page)
        return False


async def run():
    lw.SESSION_DIR.mkdir(exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    print("\n" + "=" * 55)
    print("   X (Twitter) Autonomous Engagement Agent")
    print("   AI Solutions Expert — Builder Persona")
    print("=" * 55 + "\n")

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=lw.HEADLESS,
            channel="chrome",
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-first-run",
                "--no-default-browser-check",
                "--disable-infobars",
                "--start-maximized",
            ],
        )
        # LinkedIn wala hardcoded user-agent aur STEALTH_SCRIPT yahan jaan ke
        # NAHI lagate: hum asli Chrome (channel="chrome") chala rahe hain, jis
        # ka apna genuine UA / window.chrome / plugins already hain. Purana
        # fake UA (Chrome 131) asli engine se mismatch karta tha aur fake
        # window.chrome skeleton genuine wale se KAM asli lagta hai — X ka
        # anti-bot yehi pakar ke login ko verification loop me phansa deta
        # tha (phone number ke baad aagay na barhna).
        context = await browser.new_context(
            viewport={"width": 1280, "height": 900},
        )
        await lw.load_cookies(context, COOKIES_FILE)

        page = await context.new_page()
        print("[*] Opening X...")
        await page.goto(X_HOME, wait_until="domcontentloaded", timeout=60000)
        await asyncio.sleep(5)
        print(f"[*] URL: {page.url}\n")

        if not is_logged_in(page.url):
            if COOKIES_FILE.exists():
                COOKIES_FILE.unlink()
                print("[!] Purani X cookies delete — fresh login hoga.")
            print("─" * 55)
            print("  X khul gaya hai. Apne account se login karo.")
            print("  Home timeline pe aane ke baad yahan ENTER dabao.")
            print("─" * 55)
            # NOTE: prompt me "jab feed pe ho" dashboard ka LOGIN_PROMPT_MARKER
            # hai — change karna ho to app.py bhi update karo.
            input("\n  >> Enter (jab feed pe ho): ")
            await lw.save_cookies(context, COOKIES_FILE)
            if not is_logged_in(page.url):
                await page.goto(X_HOME, wait_until="domcontentloaded", timeout=60000)
                await asyncio.sleep(5)
        else:
            print("[*] Session valid — home timeline pe aa gaye!\n")
            await lw.save_cookies(context, COOKIES_FILE)

        print("[*] Tweets load hone ka wait kar raha hun...")
        await asyncio.sleep(4)

        processed = 0
        attempts = 0
        seen: set[str] = set()
        skip_stats = {"promoted": 0, "liked": 0, "too_short": 0, "old": 0,
                      "irrelevant": 0, "dedup": 0}
        engaged = lw.load_engaged(ENGAGED_FILE)
        scroll_budget = max(30, MAX_POSTS * 10)
        loop = asyncio.get_event_loop()
        print(f"[*] {len(engaged)} tweets already engaged in previous runs (skip list loaded).\n")

        while processed < MAX_POSTS and attempts < scroll_budget:
            tweets = await page.evaluate(_SCAN_TWEETS)

            candidate = None
            for t in tweets:
                tid = t["id"]
                if not tid or tid in seen:
                    continue
                if tid in engaged:
                    seen.add(tid)
                    skip_stats["dedup"] += 1
                    continue
                seen.add(tid)
                if t["promoted"]:
                    skip_stats["promoted"] += 1
                    continue
                if t["liked"]:
                    skip_stats["liked"] += 1
                    continue
                if len(t["text"].strip()) < MIN_TWEET_CHARS:
                    skip_stats["too_short"] += 1
                    continue
                age = tweet_age_hours(t["datetime"])
                if age is not None and age > MAX_POST_AGE_HOURS:
                    skip_stats["old"] += 1
                    continue
                if not lw.is_relevant_post(t["text"]):
                    skip_stats["irrelevant"] += 1
                    continue
                candidate = t
                break

            if not candidate:
                attempts += 1
                await page.evaluate("window.scrollBy(0, 900)")
                await asyncio.sleep(random.uniform(2.5, 4.5))
                continue

            tid = candidate["id"]
            text = candidate["text"].strip()
            preview = text[:110].replace("\n", " ")
            print("─" * 55)
            print(f"[{processed + 1}/{MAX_POSTS}] Tweet {tid}")
            print(f'  "{preview}..."')

            liked = await like_tweet(page, tid)

            comment = ""
            posted = False
            try:
                comment = await loop.run_in_executor(
                    lw._executor,
                    lambda: generate_comment(text, PERSONA_FILE, platform="X (Twitter)"),
                )
                comment = truncate_reply(comment)
                print(f'  Reply: "{comment}"')
                posted = await reply_to_tweet(page, tid, comment)
            except RuntimeError as e:
                print(f"  [!] Comment generate nahi hua: {e}")

            # Like lag chuka hai, is liye fail hone par bhi engaged mark karte
            # hain — same tweet ko dobara try kar ke double-engage nahi karna.
            engaged[tid] = {
                "timestamp": datetime.now().isoformat(),
                "preview": text[:120],
            }
            lw.save_engaged(engaged, ENGAGED_FILE)
            lw.log_result(tid, text, comment, posted,
                          reaction="like", reacted=liked, log_dir=LOG_DIR)

            if posted:
                processed += 1
            if processed < MAX_POSTS:
                await lw.human_gap()

        print("=" * 55)
        print(f"  Complete! Replies: {processed}/{MAX_POSTS}")
        print(f"  Skips: promoted {skip_stats['promoted']}, already-liked {skip_stats['liked']}, "
              f"too-short {skip_stats['too_short']}, old {skip_stats['old']}, "
              f"irrelevant {skip_stats['irrelevant']}, dedup {skip_stats['dedup']}.")
        print(f"  Log: {LOG_DIR}/{datetime.now().strftime('%Y-%m-%d')}.json")
        print("=" * 55)

        if sys.stdin.isatty():
            input("\nEnter dabao browser band karne ke liye...")
        else:
            print("\nBrowser band ho raha hai...")
        await browser.close()


if __name__ == "__main__":
    asyncio.run(run())
