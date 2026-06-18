import asyncio
import hashlib
import os
import random
import json
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from dotenv import load_dotenv
from playwright.async_api import async_playwright, Page, BrowserContext
from comment_generator import generate_comment

load_dotenv()

_executor = ThreadPoolExecutor(max_workers=1)

# ── Config (tunable via .env, sensible defaults if unset) ─────────────────────
MAX_POSTS        = int(os.getenv("MAX_POSTS", "5"))
MIN_GAP_SECONDS  = float(os.getenv("MIN_GAP_SECONDS", "50"))
MAX_GAP_SECONDS  = float(os.getenv("MAX_GAP_SECONDS", "90"))
HEADLESS         = os.getenv("HEADLESS", "false").strip().lower() == "true"
SESSION_DIR      = Path("session")
LOGS_DIR         = Path("logs")
COOKIES_FILE     = SESSION_DIR / "cookies.json"
ENGAGED_FILE     = LOGS_DIR / "engaged.json"
LINKEDIN_FEED    = "https://www.linkedin.com/feed/"

# UI text jo post content mein nahi chahiye
UI_WORDS = {
    "like", "comment", "share", "repost", "send", "follow", "connect",
    "reactions", "reaction", "promoted", "sponsored", "followers",
    "connections", "see more", "see less", "load more", "1st", "2nd", "3rd",
    "view profile", "message", "following", "ago", "edited", "add a comment",
    "comments", "reposts", "· 1st", "· 2nd", "· 3rd",
}

# Dynamic social-proof lines ("X commented", "Suggested", "Feed post") jo run se
# run badal sakti hain — dedup fingerprint ke liye ye text mein nahi chahiye,
# warna same post alag fingerprint bana dega.
SOCIAL_PROOF_RE = re.compile(
    r"^(feed post|suggested|promoted)$|"
    r"\b(commented|likes? this|loves? this|supports? this|"
    r"celebrates? this|finds? this insightful|reacted to this)\b",
    re.I,
)

STEALTH_SCRIPT = """
    Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
    Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
    Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']});
    window.chrome = { runtime: {}, loadTimes: function(){}, csi: function(){}, app: {} };
    const originalQuery = window.navigator.permissions.query;
    window.navigator.permissions.query = (parameters) => (
        parameters.name === 'notifications' ?
        Promise.resolve({ state: Notification.permission }) :
        originalQuery(parameters)
    );
"""
# ──────────────────────────────────────────────────────────────────────────────


async def save_cookies(context: BrowserContext):
    SESSION_DIR.mkdir(exist_ok=True)
    cookies = await context.cookies()
    with open(COOKIES_FILE, "w", encoding="utf-8") as f:
        json.dump(cookies, f, indent=2)
    print(f"  [*] Session saved ({len(cookies)} cookies)")


async def load_cookies(context: BrowserContext) -> bool:
    if not COOKIES_FILE.exists():
        return False
    with open(COOKIES_FILE, encoding="utf-8") as f:
        cookies = json.load(f)
    if not cookies:
        return False
    await context.add_cookies(cookies)
    print(f"  [*] Loaded saved session ({len(cookies)} cookies)")
    return True


def is_on_feed(url: str) -> bool:
    return "linkedin.com/feed" in url and "login" not in url and "authwall" not in url


def clean_post_text(raw: str) -> str:
    """Post ke UI text hata ke sirf actual content rakhta hai."""
    lines = raw.split("\n")
    good = []
    for line in lines:
        line = line.strip()
        if not line or len(line) < 4:
            continue
        low = line.lower()
        if low in UI_WORDS:
            continue
        if any(low.startswith(w) for w in UI_WORDS):
            continue
        if SOCIAL_PROOF_RE.search(line):
            continue
        # Numbers only (like counts)
        if re.match(r"^\d[\d,\.KkMm ]*$", line):
            continue
        # Author headline lines (short lines with • separator)
        if "•" in line and len(line) < 120:
            continue
        # Degree indicators
        if re.match(r"^(1st|2nd|3rd)\s*$", line, re.I):
            continue
        good.append(line)
    return "\n".join(good[:40])


def post_fingerprint(text: str) -> str:
    """Cleaned post text se stable hash banata hai — data-bot-id har run mein
    reset hoti hai (scroll position se assign hoti hai), is liye wo cross-run
    dedup ke liye use nahi ho sakti."""
    normalized = re.sub(r"\s+", " ", text.strip().lower())[:300]
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def load_engaged() -> dict:
    if not ENGAGED_FILE.exists():
        return {}
    try:
        with open(ENGAGED_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_engaged(engaged: dict):
    LOGS_DIR.mkdir(exist_ok=True)
    with open(ENGAGED_FILE, "w", encoding="utf-8") as f:
        json.dump(engaged, f, indent=2, ensure_ascii=False)


# Post ke content ke hisab se reaction choose karne ke liye keywords
REACTION_KEYWORDS = {
    "celebrate": [
        "celebrat", "milestone", "anniversary", "launch", "thrilled to announce",
        "excited to announce", "proud to", "achievement", "congrat", "promoted",
        "new role", "new job", "graduat", "award", "winning", "followers!",
        "years at", "humbled", "honored", "🎉",
    ],
    "support": [
        "laid off", "layoff", "lost my job", "open to work", "looking for a job",
        "looking for opportunities", "difficult time", "passed away", "loss of",
        "struggling", "hard time", "job search", "unemployed", "rejection",
    ],
    "love": [
        "grateful", "thank you all", "thankful", "family", "heartfelt", "touched",
        "blessed", "appreciate you", "means the world",
    ],
    "funny": [
        "lol", "haha", "joke", "funny story", "couldn't stop laughing",
    ],
    "insightful": [
        "data shows", "research", "study found", "framework", "lessons learned",
        "strategy", "analysis", "here's what i learned", "key takeaway", "insight",
    ],
}


def pick_reaction(text: str) -> str:
    """Post content dekh kar best reaction decide karta hai (default: like)."""
    low = text.lower()
    scores = {name: 0 for name in REACTION_KEYWORDS}
    for name, keywords in REACTION_KEYWORDS.items():
        for kw in keywords:
            if kw in low:
                scores[name] += 1
    best = max(scores, key=scores.get)
    if scores[best] == 0:
        return "like"
    return best


# Comment ke liye "relevant" categories — persona ke expertise + generic
# celebration/achievement posts (LinkedIn pe inko engage karna normal hai,
# topic se related ho ya na ho). List intentionally broad rakhi hai taake
# acche posts galti se skip na hon — kam matches ka risk zyada hai.
RELEVANCE_KEYWORDS = {
    "ai_automation": [
        "ai", "artificial intelligence", "automation", "agent", "agentic",
        "genai", "generative ai", "llm", "machine learning", "chatgpt",
        "claude", "copilot", "rpa", "workflow", "no-code", "low-code",
        "n8n", "zapier", "make.com", "digital transformation", "ai-native",
        "ai tool", "ai adoption", "ai agent", "autonomous",
    ],
    "business_growth": [
        "startup", "founder", "ceo", "fundrais", "scaling", "scale up",
        "roi", "revenue", "growth", "business strategy", "leadership",
        "entrepreneur", "venture", "investor", "pitch deck", "go-to-market",
        "smb", "sme", "enterprise",
    ],
    "future_of_work": [
        "future of work", "remote work", "hybrid work", "hiring", "talent",
        "workplace", "human-ai", "reskilling", "upskilling", "workforce",
    ],
    "celebration_achievement": REACTION_KEYWORDS["celebrate"],
}


def is_relevant_post(text: str) -> bool:
    """Persona ke expertise (AI/automation/business growth/future of work) ya
    celebration/achievement post — inhi par comment generate karte hain.
    Baaki posts par sirf reaction milta hai, comment skip ho jata hai."""
    low = text.lower()
    return any(
        kw in low
        for keywords in RELEVANCE_KEYWORDS.values()
        for kw in keywords
    )


def log_result(post_id: str, preview: str, comment: str, success: bool,
                reaction: str | None = None, reacted: bool | None = None):
    LOGS_DIR.mkdir(exist_ok=True)
    log_file = LOGS_DIR / f"{datetime.now().strftime('%Y-%m-%d')}.json"
    entry = {
        "timestamp": datetime.now().isoformat(),
        "post_id": post_id,
        "post_preview": preview[:200],
        "comment": comment,
        "success": success,
        "reaction": reaction,
        "reacted": reacted,
    }
    logs = []
    if log_file.exists():
        with open(log_file, encoding="utf-8") as f:
            try:
                logs = json.load(f)
            except Exception:
                logs = []
    logs.append(entry)
    with open(log_file, "w", encoding="utf-8") as f:
        json.dump(logs, f, indent=2, ensure_ascii=False)


async def type_humanly(element, text: str):
    for char in text:
        await element.type(char)
        await asyncio.sleep(random.uniform(0.04, 0.13))


async def get_posts_from_page(page: Page) -> list[dict]:
    """Comment buttons ke zariye posts dhundho — class names change hone se farq nahi padta.
    Har post container per data-bot-id likh dete hain taake baad mein exact post wapas mil sake
    (preview-text matching unreliable thi, isi se purane comments galat post per chale gaye the)."""
    return await page.evaluate("""
        () => {
            const posts = [];
            const seen = new Set();
            window.__botPostCounter = window.__botPostCounter || 0;

            // Sab Comment buttons dhundho
            const allButtons = Array.from(document.querySelectorAll('button'));
            const commentBtns = allButtons.filter(btn => {
                const txt = btn.innerText.trim().toLowerCase();
                return txt === 'comment' || txt === 'add a comment';
            });

            for (const btn of commentBtns) {
                // Button se upar jao post container tak
                let node = btn.parentElement;
                let postContainer = null;

                for (let i = 0; i < 25; i++) {
                    if (!node) break;
                    const rect = node.getBoundingClientRect();
                    if (rect.height > 250 && rect.width > 400) {
                        postContainer = node;
                        if (rect.height > 500) break;  // Pura post mil gaya
                    }
                    node = node.parentElement;
                }

                if (!postContainer) continue;

                // Stable ID is post container per likh do (CSS attribute selector ke liye)
                let botId = postContainer.getAttribute('data-bot-id');
                if (!botId) {
                    botId = 'bot-post-' + (window.__botPostCounter++);
                    postContainer.setAttribute('data-bot-id', botId);
                }

                if (seen.has(botId)) continue;
                seen.add(botId);

                const rawText = postContainer.innerText || '';

                // Sponsored check
                const isSponsored = rawText.toLowerCase().includes('promoted') ||
                                    rawText.toLowerCase().includes('sponsored');

                posts.push({
                    id: botId,
                    text: rawText.slice(0, 2000),
                    sponsored: isSponsored,
                });
            }

            return posts;
        }
    """)


async def react_to_post(page: Page, post_id: str, reaction: str) -> bool:
    """Post per Like/Celebrate/Support/Love/Insightful/Funny reaction deta hai.

    LinkedIn's react button has aria-label="Reaction button state: no reaction"
    (not "Like") — ARIA labels override visible text for accessible-name matching,
    so get_by_role(name="Like") never matches it. Locate it via the aria-label
    prefix instead, scoped to this post's container.
    """
    try:
        post = page.locator(f'[data-bot-id="{post_id}"]').first
        like_btn = post.locator('button[aria-label^="Reaction button state"]').first

        if await like_btn.count() == 0:
            print("  React button nahi mila.")
            return False

        if reaction != "like":
            # Reaction picker (flyout) kholne ke liye hover karo
            await like_btn.hover()
            await asyncio.sleep(random.uniform(0.8, 1.3))

            option = page.get_by_role("button", name=re.compile(rf"^{reaction}$", re.I)).first
            if await option.count() > 0:
                await option.click()
            else:
                # Picker nahi khula to simple Like kar do
                await like_btn.click()
        else:
            await like_btn.click()

        await asyncio.sleep(random.uniform(1, 2))

        # Verify: button state ne actually "no reaction" se hat ke kuch aur dikhana chahiye
        new_state = await like_btn.get_attribute("aria-label")
        return bool(new_state) and "no reaction" not in new_state.lower()

    except Exception as e:
        print(f"  Reaction error: {e}")
        return False


async def click_and_comment(page: Page, post_id: str, comment_text: str) -> bool:
    """post_id wale exact post per comment likhta hai aur verify karta hai ke wo wahan actually gaya."""
    try:
        # Sirf isi post_id ke container ke andar Comment button dhundho — kisi dusre post per nahi jana
        clicked = await page.evaluate("""
            (postId) => {
                const container = document.querySelector(`[data-bot-id="${postId}"]`);
                if (!container) return false;
                const buttons = Array.from(container.querySelectorAll('button'));
                const commentBtn = buttons.find(b => {
                    const txt = b.innerText.trim().toLowerCase();
                    return txt === 'comment' || txt === 'add a comment';
                });
                if (!commentBtn) return false;
                commentBtn.click();
                return true;
            }
        """, post_id)

        if not clicked:
            print("  Comment button nahi mila.")
            return False

        await asyncio.sleep(random.uniform(2, 3))

        # LinkedIn ka comment editor (tiptap/ProseMirror) data-bot-id container ke BAHAR mount
        # hota hai — isliye container se upar walk karke wo ancestor dhundo jis mein editor mile,
        # aur usi ancestor ko ek scope-attribute de do (submit button + verification ke liye bhi
        # yehi wider scope chahiye, sirf data-bot-id container kaafi nahi).
        found = await page.evaluate("""
            (postId) => {
                const container = document.querySelector(`[data-bot-id="${postId}"]`);
                if (!container) return false;
                const selector = "div.ql-editor[contenteditable='true'], div[role='textbox'][contenteditable='true'], div[contenteditable='true']";
                let node = container;
                for (let i = 0; i < 8; i++) {
                    if (!node) break;
                    const editor = node.querySelector(selector);
                    if (editor) {
                        node.setAttribute('data-bot-scope', postId);
                        editor.setAttribute('data-bot-input', postId);
                        return true;
                    }
                    node = node.parentElement;
                }
                return false;
            }
        """, post_id)

        if not found:
            print("  Comment input nahi mila.")
            return False

        comment_input = await page.query_selector(f'[data-bot-input="{post_id}"]')
        if not comment_input or not await comment_input.is_visible():
            print("  Comment input nahi mila.")
            return False

        await comment_input.click()
        await asyncio.sleep(0.5)
        await type_humanly(comment_input, comment_text)
        await asyncio.sleep(random.uniform(1.5, 2.5))

        # Submit button class names hashed/change hote hain — isliye text se dhundo, wider scope mein
        submitted = await page.evaluate("""
            (postId) => {
                const container = document.querySelector(`[data-bot-scope="${postId}"]`);
                if (!container) return false;
                const buttons = Array.from(container.querySelectorAll('button'));
                const candidates = buttons.filter(b => {
                    const txt = b.innerText.trim().toLowerCase();
                    return (txt === 'comment' || txt === 'post') && !b.disabled;
                });
                if (candidates.length === 0) return false;
                candidates[candidates.length - 1].click();
                return true;
            }
        """, post_id)

        if not submitted:
            await comment_input.press("Control+Enter")

        await asyncio.sleep(random.uniform(2.5, 4))

        # Verify: comment actually post per dikh raha hai ya sirf box mein type ho ke reh gaya
        snippet = comment_text[:30].strip()
        posted_ok = await page.evaluate("""
            (args) => {
                const container = document.querySelector(`[data-bot-scope="${args.postId}"]`) ||
                                   document.querySelector(`[data-bot-id="${args.postId}"]`);
                if (!container) return false;
                return container.innerText.includes(args.snippet);
            }
        """, {"postId": post_id, "snippet": snippet})

        if not posted_ok:
            print("  [!] Comment submit hua lekin post per show nahi ho raha — fail mark kar rahe hain.")
            return False

        return True

    except Exception as e:
        print(f"  Error: {e}")
        return False


async def human_gap():
    """Posts ke beech gap — taake reactions/comments burst mein na jayein aur spam na lage."""
    delay = random.uniform(MIN_GAP_SECONDS, MAX_GAP_SECONDS)
    print(f"  {delay:.0f}s wait (spam na lage)...\n")
    await asyncio.sleep(delay)


def build_target_urls() -> list[str]:
    """TARGET_HASHTAGS (.env, comma-separated, # optional) se hashtag feed
    URLs banata hai — taake bot algorithmic home feed ke bajaye specific
    topics target kar sake. Unset/empty ho to purana behavior: sirf home feed."""
    raw = os.getenv("TARGET_HASHTAGS", "").strip()
    tags = [t.strip().lstrip("#") for t in raw.split(",") if t.strip()]
    if not tags:
        return [LINKEDIN_FEED]
    return [f"https://www.linkedin.com/feed/hashtag/{t}/" for t in tags]


async def run():
    SESSION_DIR.mkdir(exist_ok=True)
    LOGS_DIR.mkdir(exist_ok=True)

    print("\n" + "=" * 55)
    print("   LinkedIn Autonomous Commenter")
    print("   AI Solutions Expert — CEO/Founder Persona")
    print("=" * 55 + "\n")

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=HEADLESS,
            channel="chrome",
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-first-run",
                "--no-default-browser-check",
                "--disable-infobars",
                "--start-maximized",
            ],
        )

        context = await browser.new_context(
            viewport={"width": 1280, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
        )

        await context.add_init_script(STEALTH_SCRIPT)
        has_session = await load_cookies(context)

        page = await context.new_page()

        print("[*] Opening LinkedIn...")
        await page.goto(LINKEDIN_FEED, wait_until="domcontentloaded", timeout=60000)
        await asyncio.sleep(5)

        current_url = page.url
        print(f"[*] URL: {current_url}\n")

        if not is_on_feed(current_url):
            if COOKIES_FILE.exists():
                COOKIES_FILE.unlink()
                print("[!] Purani cookies delete — fresh login hoga.")
            print("─" * 55)
            print("  LinkedIn khul gaya hai. Google se login karo.")
            print("  Feed pe aane ke baad yahan ENTER dabao.")
            print("─" * 55)
            input("\n  >> Enter (jab feed pe ho): ")
            await save_cookies(context)
            if not is_on_feed(page.url):
                await page.goto(LINKEDIN_FEED, wait_until="domcontentloaded", timeout=60000)
                await asyncio.sleep(5)
        else:
            print("[*] Session valid — feed pe aa gaye!\n")
            await save_cookies(context)

        # Posts load hone do
        print("[*] Posts load hone ka wait kar raha hun...")
        await asyncio.sleep(4)
        await page.evaluate("window.scrollBy(0, 400)")
        await asyncio.sleep(3)
        await page.evaluate("window.scrollBy(0, -400)")
        await asyncio.sleep(2)

        processed   = 0
        seen_ids    = set()
        engaged     = load_engaged()
        print(f"[*] {len(engaged)} posts already engaged in previous runs (skip list loaded).\n")

        targets = build_target_urls()
        random.shuffle(targets)  # order varies run to run so a long hashtag list doesn't always favor the first few
        if targets == [LINKEDIN_FEED]:
            print("[*] Target: home feed (no TARGET_HASHTAGS set).\n")
        else:
            print(f"[*] Targeted hashtags: {', '.join(t.rstrip('/').rsplit('/', 1)[-1] for t in targets)}\n")

        for target_url in targets:
            if processed >= MAX_POSTS:
                break

            if not page.url.rstrip("/").startswith(target_url.rstrip("/")):
                print(f"[*] Navigating to {target_url}")
                await page.goto(target_url, wait_until="domcontentloaded", timeout=60000)
                await asyncio.sleep(4)
                await page.evaluate("window.scrollBy(0, 400)")
                await asyncio.sleep(2)
                await page.evaluate("window.scrollBy(0, -400)")
                await asyncio.sleep(2)

            print(f"[*] Scanning shuru: {target_url}\n")
            scroll_num = 0
            # Relevance filter + dedup ke baad har post comment nahi banta — is
            # liye scroll budget MAX_POSTS ke hisab se scale karte hain, fixed
            # 25 kaafi nahi raha jab MAX_POSTS barha do. Har target ko apna
            # fresh budget milta hai taake pehla hashtag baaki sabka budget na khaye.
            max_scrolls = max(15, MAX_POSTS * 8)

            while processed < MAX_POSTS and scroll_num < max_scrolls:

                if not is_on_feed(page.url):
                    await page.goto(target_url, wait_until="domcontentloaded", timeout=60000)
                    await asyncio.sleep(4)

                try:
                    posts = await get_posts_from_page(page)
                except Exception as e:
                    print(f"  [!] Scan error: {e}")
                    await asyncio.sleep(3)
                    scroll_num += 1
                    continue

                if scroll_num == 0:
                    print(f"[*] {len(posts)} posts mile is scroll mein.\n")

                for post in posts:
                    if processed >= MAX_POSTS:
                        break

                    post_id = post.get("id", "")
                    if not post_id or post_id in seen_ids:
                        continue
                    seen_ids.add(post_id)

                    if post.get("sponsored"):
                        print("  [SKIP] Sponsored post.")
                        continue

                    text = clean_post_text(post.get("text", ""))
                    if len(text) < 60:
                        continue

                    fp = post_fingerprint(text)
                    if fp in engaged:
                        print("  [SKIP] Pehle hi kabhi engage ho chuke is post pe.")
                        continue

                    print(f"[POST {processed + 1}/{MAX_POSTS}]")
                    print(f"  Preview  : {text[:120].replace(chr(10), ' ')}...")

                    reaction = pick_reaction(text)
                    print(f"  Reaction : {reaction}")
                    reacted = await react_to_post(page, post_id, reaction)
                    print(f"  {'[OK]' if reacted else '[WARN]'} Reaction {'de diya' if reacted else 'nahi de paya'}.")

                    if reacted:
                        engaged[fp] = {
                            "timestamp": datetime.now().isoformat(),
                            "preview": text[:80],
                            "commented": False,
                        }
                        save_engaged(engaged)

                    if not is_relevant_post(text):
                        print("  [SKIP] Comment skip — post persona ke expertise/celebration scope se bahar.\n")
                        if processed < MAX_POSTS:
                            await human_gap()
                        continue

                    print(f"  Generating comment...")
                    loop = asyncio.get_event_loop()
                    try:
                        comment = await loop.run_in_executor(_executor, generate_comment, text)
                    except Exception as e:
                        print(f"  [SKIP] Comment generate nahi hua: {e}\n")
                        # Reaction to pehle hi de di — agla post bhi gap se hi karo
                        if processed < MAX_POSTS:
                            await human_gap()
                        continue
                    print(f"  Comment  : {comment}")
                    print(f"  Posting...")

                    success = await click_and_comment(page, post_id, comment)
                    log_result(post_id, text, comment, success, reaction, reacted)

                    if success:
                        processed += 1
                        if fp in engaged:
                            engaged[fp]["commented"] = True
                            save_engaged(engaged)
                        print(f"  [OK] Done! ({processed}/{MAX_POSTS})\n")
                    else:
                        print("  [FAIL] Next post pe ja raha hun.\n")

                    if processed < MAX_POSTS:
                        await human_gap()

                await page.evaluate("window.scrollBy(0, 900)")
                await asyncio.sleep(random.uniform(3, 5))
                scroll_num += 1

        print("=" * 55)
        print(f"  Complete! Comments: {processed}/{MAX_POSTS}")
        print(f"  Log: logs/{datetime.now().strftime('%Y-%m-%d')}.json")
        print("=" * 55)

        input("\nEnter dabao browser band karne ke liye...")
        await browser.close()


if __name__ == "__main__":
    asyncio.run(run())
