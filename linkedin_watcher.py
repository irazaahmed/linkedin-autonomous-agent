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
MAX_POST_AGE_HOURS = float(os.getenv("MAX_POST_AGE_HOURS", "12"))
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


# LinkedIn ki header timestamp line ("6h", "2d", "1mo • Edited") match karne
# ke liye — purani posts (jahan engagement ka faida nahi) skip karne ke liye
# clean_post_text se PEHLE raw text par chalana zaroori hai, warna ye line
# (length < 4 ya bullet-headline filter ki wajah se) cleaning mein hi udh jati hai.
POST_AGE_RE = re.compile(
    r"^(\d+)\s*(mo|mos|min|mins|hr|hrs|sec|secs|wk|wks|yr|yrs|[smhdwy])"
    r"(?:[\s•\-]*(?:edited|promoted))*[\s•\-]*$",
    re.I,
)

UNIT_TO_HOURS = {
    "s": 1 / 3600, "sec": 1 / 3600, "secs": 1 / 3600,
    "m": 1 / 60, "min": 1 / 60, "mins": 1 / 60,
    "h": 1, "hr": 1, "hrs": 1,
    "d": 24, "day": 24, "days": 24,
    "w": 24 * 7, "wk": 24 * 7, "wks": 24 * 7,
    "mo": 24 * 30, "mos": 24 * 30,
    "y": 24 * 365, "yr": 24 * 365, "yrs": 24 * 365,
}


def post_age_hours(raw_text: str) -> float | None:
    """Post container ke raw (uncleaned) text mein se header timestamp line
    dhundh ke age hours mein deta hai ("6h" -> 6.0, "2d" -> 48.0). Sirf header
    ke aas-paas wali pehli few lines check karte hain (body text mein kahin
    bhi "6h" jaisi line milne se false-positive na ho). Line na mile to None
    — age unknown hone par post drop nahi karte (selector format badal sakta
    hai, benefit of doubt deta hai)."""
    for line in raw_text.split("\n")[:8]:
        line = line.strip()
        if not line:
            continue
        if line.lower() in ("now", "just now"):
            return 0.0
        m = POST_AGE_RE.fullmatch(line)
        if m:
            unit = m.group(2).lower()
            return int(m.group(1)) * UNIT_TO_HOURS.get(unit, 1)
    return None


# Connection-degree badge ("1st"/"2nd"/"3rd") apni line par akela hota hai,
# kabhi middot prefix ke sath ("· 1st" — UI_WORDS mein bhi yehi form hai).
DEGREE_RE = re.compile(r"^[·•]?\s*(1st|2nd|3rd)\s*$", re.I)
DEGREE_RANK = {"1st": 1, "2nd": 2, "3rd": 3}


def post_connection_degree(raw_text: str) -> int:
    """Author connection-degree raw text se nikalta hai — 1 = 1st-degree
    (direct connection), 2 = 2nd-degree, 3 = 3rd-degree, 4 = badge nahi mila
    (Suggested/company-page posts, sab se kam priority). Lower number =
    pehle tackle karna hai (connected logo ki posts ko priority)."""
    for line in raw_text.split("\n")[:10]:
        m = DEGREE_RE.match(line.strip())
        if m:
            return DEGREE_RANK[m.group(1).lower()]
    return 4


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


def load_engaged(engaged_file: Path = ENGAGED_FILE) -> dict:
    if not engaged_file.exists():
        return {}
    try:
        with open(engaged_file, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_engaged(engaged: dict, engaged_file: Path = ENGAGED_FILE):
    engaged_file.parent.mkdir(parents=True, exist_ok=True)
    with open(engaged_file, "w", encoding="utf-8") as f:
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
                reaction: str | None = None, reacted: bool | None = None,
                log_dir: Path = LOGS_DIR):
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / f"{datetime.now().strftime('%Y-%m-%d')}.json"
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


async def get_posts_from_page(page: Page) -> tuple[list[dict], dict]:
    """Comment buttons ke zariye posts dhundho — class names change hone se farq nahi padta.
    Har post container per data-bot-id likh dete hain taake baad mein exact post wapas mil sake
    (preview-text matching unreliable thi, isi se purane comments galat post per chale gaye the).

    Visible text match ("Comment") ke sath-sath aria-label se bhi match karte hain — Like button
    ki tarah (README ka challenge #1) LinkedIn kabhi kabhi visible label hata ke sirf aria-label
    chhod deta hai, ya ek count badge text ke sath jod deta hai ("Comment\\n42") jo exact-match
    todh deta. Debug counters bhi return karte hain taake 0-posts wali run mein turant pata chale
    ke selector hi kuch nahi pakad raha, ya buttons mil rahe hain lekin container walk fail ho rahi hai."""
    return await page.evaluate("""
        () => {
            const posts = [];
            const seen = new Set();
            window.__botPostCounter = window.__botPostCounter || 0;

            // Sab Comment buttons dhundho — visible text ya aria-label se
            const allButtons = Array.from(document.querySelectorAll('button'));
            const commentBtns = allButtons.filter(btn => {
                const txt = (btn.innerText || '').trim().toLowerCase();
                if (txt === 'comment' || txt === 'add a comment') return true;
                const aria = (btn.getAttribute('aria-label') || '').trim().toLowerCase();
                return /^comment\\b/.test(aria);
            });

            let noContainer = 0;

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

                if (!postContainer) { noContainer++; continue; }

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

            return [posts, {
                totalButtons: allButtons.length,
                commentBtnsFound: commentBtns.length,
                noContainer: noContainer,
            }];
        }
    """)


async def react_to_post(page: Page, post_id: str, reaction: str) -> bool:
    """Post per Like/Celebrate/Support/Love/Insightful/Funny reaction deta hai.

    LinkedIn's react button has aria-label="Reaction button state: no reaction"
    (not "Like") — ARIA labels override visible text for accessible-name matching,
    so get_by_role(name="Like") never matches it. Locate it via the aria-label
    prefix instead, scoped to this post's container.

    Hover/click yahan Playwright ke native hover()/click() se NAHI karte — LinkedIn
    kabhi kabhi ek floating-ui tooltip/loading-spinner portal ya koi aur transient
    overlay chhod deta hai jo poori `<html>` ko "intercepts pointer events" bana deta
    hai, aur Playwright ka actionability check 30s tak retry kar ke bhi fail ho jata
    hai. JS se directly .click()/dispatchEvent karne se ye hit-test/intercept check
    bypass ho jata hai — same pattern jo comment button aur submit button ke liye
    pehle se use ho raha hai is file mein.
    """
    # Session ke shuru mein LinkedIn ke React handlers abhi "warm" nahi hote —
    # synthetic mouseover se flyout kabhi pehli baar nahi khulta aur aria-label
    # update late aata hai, is liye pehle 2-3 reactions aksar fail dikhte the.
    # Isliye ek retry lagate hain; aur agar button pe pehle se reaction lagi ho
    # (ya pichle attempt ne laga di ho) to dobara click nahi karte warna toggle
    # hoke un-react ho jayegi.
    for attempt in range(2):
        try:
            if await _try_react_once(page, post_id, reaction):
                return True
        except Exception as e:
            print(f"  Reaction error: {e}")
        if attempt == 0:
            await asyncio.sleep(random.uniform(1.2, 2.0))  # settle hone do, phir dobara
    return False


async def _try_react_once(page: Page, post_id: str, reaction: str) -> bool:
    post = page.locator(f'[data-bot-id="{post_id}"]').first
    like_btn = post.locator('button[aria-label^="Reaction button state"]').first

    if await like_btn.count() == 0:
        print("  React button nahi mila.")
        return False

    # Pehle se reaction lagi hai? To dobara click mat karo (toggle-off se bachao).
    existing = await like_btn.get_attribute("aria-label")
    if existing and "no reaction" not in existing.lower():
        return True

    if reaction != "like":
        # Reaction picker (flyout) kholne ke liye hover karo
        await like_btn.evaluate(
            "el => { el.dispatchEvent(new MouseEvent('mouseover', {bubbles: true})); "
            "el.dispatchEvent(new MouseEvent('mouseenter', {bubbles: true})); }"
        )
        await asyncio.sleep(random.uniform(0.8, 1.3))

        option = page.get_by_role("button", name=re.compile(rf"^{reaction}$", re.I)).first
        if await option.count() > 0:
            await option.evaluate("el => el.click()")
        else:
            # Picker nahi khula to simple Like kar do
            await like_btn.evaluate("el => el.click()")
    else:
        await like_btn.evaluate("el => el.click()")

    # Verify: aria-label update kabhi late aata hai, is liye ~4s tak poll karo —
    # ek single 1-2s check false-negative de raha tha (jis se dedup miss + agle
    # run mein dobara react/un-react ka risk banta tha).
    for _ in range(8):
        await asyncio.sleep(0.5)
        new_state = await like_btn.get_attribute("aria-label")
        if new_state and "no reaction" not in new_state.lower():
            return True
    return False


async def click_and_comment(page: Page, post_id: str, comment_text: str) -> bool:
    """post_id wale exact post per comment likhta hai aur verify karta hai ke wo wahan actually gaya."""
    try:
        # Sirf isi post_id ke container ke andar Comment button dhundho — kisi dusre post per nahi jana.
        # Yahan bhi aria-label fallback chahiye, get_posts_from_page wali wajah se (visible text
        # hamesha exact "comment" nahi hota — LinkedIn kabhi count/icon-only label deta hai).
        clicked = await page.evaluate("""
            (postId) => {
                const container = document.querySelector(`[data-bot-id="${postId}"]`);
                if (!container) return { ok: false, reason: "container nahi mila" };
                const buttons = Array.from(container.querySelectorAll('button'));
                const commentBtn = buttons.find(b => {
                    const txt = (b.innerText || '').trim().toLowerCase();
                    if (txt === 'comment' || txt === 'add a comment') return true;
                    const aria = (b.getAttribute('aria-label') || '').trim().toLowerCase();
                    return /^comment\\b/.test(aria);
                });
                if (!commentBtn) {
                    const sample = buttons.slice(0, 12).map(b => ({
                        text: (b.innerText || '').trim().slice(0, 25),
                        aria: (b.getAttribute('aria-label') || '').slice(0, 50),
                    }));
                    return { ok: false, reason: "match nahi mila", buttonCount: buttons.length, sample };
                }
                commentBtn.click();
                return { ok: true };
            }
        """, post_id)

        if not clicked.get("ok"):
            print(f"  Comment button nahi mila ({clicked.get('reason')}).")
            if "sample" in clicked:
                print(f"  [DEBUG] Container mein {clicked['buttonCount']} buttons, sample: {clicked['sample']}")
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

        # Native .click() (Playwright) is overlay/portal se intercept ho jata hai —
        # JS se click() call karo, react_to_post wali wajah se (see uske docstring).
        await comment_input.evaluate("el => el.click()")
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
                    if (b.disabled) return false;
                    const txt = (b.innerText || '').trim().toLowerCase();
                    if (txt === 'comment' || txt === 'post') return true;
                    const aria = (b.getAttribute('aria-label') || '').trim().toLowerCase();
                    return aria === 'comment' || aria === 'post' || aria.startsWith('post comment');
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


async def switch_engage_as(page: Page, identity_name: str) -> bool:
    """LinkedIn ka identity-picker use karta hai taake baad ke sab
    reactions/comments personal profile ke bajaye ek Page (e.g. company) ke
    naam se hon. Ye ek session-wide setting hai — is liye run() shuru hote hi
    sirf EK BAAR call hota hai.

    Trigger koi standalone button nahi — ek chhota avatar hai jo HAR post ke
    reaction button ke bilkul left mein, usi row per baitha hota hai (koi
    aria-label/alt nahi, is liye naam se dhoond nahi sakte). Isko click karne
    se ek radiogroup-dialog khulta hai jisme 'Select {identity_name}'
    aria-label wala radio option hota hai, phir Save. Live DOM diagnostic se
    confirm kiya gaya selector chain hai — pehla button[aria-label*="repost
    as"] wala guess kabhi match nahi hua tha kyunke wo control exist hi nahi
    karta."""
    # Sirf .click() kabhi kabhi custom LinkedIn components ke React handlers ko
    # nahi jagata — pointerdown/mousedown/mouseup/click ka poora sequence
    # dispatch karta hai, jo real user click ke zyada kareeb hai. Locator.evaluate
    # apne pehle argument mein matched element khud "el" naam se pass karta hai,
    # is liye ye ek seedhi arrow-function expression honi chahiye — function
    # wrapper ke bagair "return" illegal hai (isi wajah se pichli baar
    # SyntaxError aaya).
    _DISPATCH_CLICK = """
        el => {
            const rect = el.getBoundingClientRect();
            const cx = rect.left + rect.width / 2;
            const cy = rect.top + rect.height / 2;
            const opts = { bubbles: true, cancelable: true, clientX: cx, clientY: cy, view: window };
            el.dispatchEvent(new PointerEvent('pointerdown', opts));
            el.dispatchEvent(new MouseEvent('mousedown', opts));
            el.dispatchEvent(new PointerEvent('pointerup', opts));
            el.dispatchEvent(new MouseEvent('mouseup', opts));
            el.click();
        }
    """

    try:
        before_src = await page.evaluate(
            """
            () => {
                const reactionBtn = document.querySelector('button[aria-label^="Reaction button state"]');
                if (!reactionBtn) return null;
                const rRect = reactionBtn.getBoundingClientRect();
                const avatarImg = Array.from(document.querySelectorAll('img'))
                    .filter(el => {
                        const r = el.getBoundingClientRect();
                        if (r.width < 14 || r.width > 40) return false;
                        const sameRow = Math.abs((r.top + r.height / 2) - (rRect.top + rRect.height / 2)) < 20;
                        const isLeftOf = (r.left + r.width) <= rRect.left + 4;
                        return sameRow && isLeftOf;
                    })
                    .sort((a, b) => b.getBoundingClientRect().left - a.getBoundingClientRect().left)[0] || null;
                return avatarImg ? avatarImg.getAttribute('src') : null;
            }
            """
        )
        if not before_src:
            print("  [!] Identity-avatar (reaction button ke paas) nahi mila.")
            return False

        avatar_target = await page.evaluate(
            """
            () => {
                const reactionBtn = document.querySelector('button[aria-label^="Reaction button state"]');
                if (!reactionBtn) return false;
                const rRect = reactionBtn.getBoundingClientRect();
                const avatarImg = Array.from(document.querySelectorAll('img'))
                    .filter(el => {
                        const r = el.getBoundingClientRect();
                        if (r.width < 14 || r.width > 40) return false;
                        const sameRow = Math.abs((r.top + r.height / 2) - (rRect.top + rRect.height / 2)) < 20;
                        const isLeftOf = (r.left + r.width) <= rRect.left + 4;
                        return sameRow && isLeftOf;
                    })
                    .sort((a, b) => b.getBoundingClientRect().left - a.getBoundingClientRect().left)[0] || null;
                if (!avatarImg) return false;

                const ir = avatarImg.getBoundingClientRect();
                let node = document.elementFromPoint(ir.left + ir.width / 2, ir.top + ir.height / 2);
                for (let i = 0; i < 6 && node; i++) {
                    const looksClickable = node.tabIndex >= 0 ||
                        node.getAttribute('role') === 'button' ||
                        node.hasAttribute('aria-haspopup') ||
                        node.tagName.toLowerCase() === 'a' ||
                        node.tagName.toLowerCase() === 'button';
                    if (looksClickable) { node.click(); return true; }
                    node = node.parentElement;
                }
                return false;
            }
            """
        )
        if not avatar_target:
            print("  [!] Identity-avatar (reaction button ke paas) nahi mila.")
            return False

        await asyncio.sleep(random.uniform(1.5, 2.5))

        option = page.locator(f'[role="radio"][aria-label="Select {identity_name}"]').first
        if await option.count() == 0:
            radiogroup = page.locator('[role="radiogroup"]').first
            group_text = (await radiogroup.inner_text())[:300] if await radiogroup.count() > 0 else "(radiogroup bhi nahi khula)"
            print(f"  [!] '{identity_name}' ka radio option nahi mila.")
            print(f"  [DEBUG] Dialog ka text: {group_text}")
            return False

        await option.evaluate(_DISPATCH_CLICK)
        await asyncio.sleep(random.uniform(0.8, 1.3))

        checked = await option.get_attribute("aria-checked")
        if checked != "true":
            print(f"  [!] '{identity_name}' radio click ke baad bhi checked nahi hua (aria-checked={checked!r}).")
            return False

        # Save button ko poore page mein nahi, isi dialog ke andar dhoondo —
        # page-wide 'Save' match kisi aur (unrelated, hidden) button ko bhi
        # pakad sakta hai.
        save_btn = await page.evaluate_handle(
            """
            () => {
                let node = document.querySelector('[role="radiogroup"]');
                for (let i = 0; i < 6 && node; i++) {
                    const btn = Array.from(node.querySelectorAll('button, [role="button"]'))
                        .find(b => (b.innerText || '').trim().toLowerCase() === 'save');
                    if (btn) return btn;
                    node = node.parentElement;
                }
                return null;
            }
            """
        )
        save_el = save_btn.as_element()
        if save_el is None:
            print("  [!] Dialog ke andar Save button nahi mila.")
            return False

        await save_el.evaluate(_DISPATCH_CLICK)
        await asyncio.sleep(random.uniform(1.5, 2.5))

        after_src = await page.evaluate(
            """
            () => {
                const reactionBtn = document.querySelector('button[aria-label^="Reaction button state"]');
                if (!reactionBtn) return null;
                const rRect = reactionBtn.getBoundingClientRect();
                const avatarImg = Array.from(document.querySelectorAll('img'))
                    .filter(el => {
                        const r = el.getBoundingClientRect();
                        if (r.width < 14 || r.width > 40) return false;
                        const sameRow = Math.abs((r.top + r.height / 2) - (rRect.top + rRect.height / 2)) < 20;
                        const isLeftOf = (r.left + r.width) <= rRect.left + 4;
                        return sameRow && isLeftOf;
                    })
                    .sort((a, b) => b.getBoundingClientRect().left - a.getBoundingClientRect().left)[0] || null;
                return avatarImg ? avatarImg.getAttribute('src') : null;
            }
            """
        )

        if not after_src or after_src == before_src:
            print(f"  [!] Save ke baad bhi identity-avatar nahi badla (switch asar mein nahi aaya).")
            print(f"  [DEBUG] before_src={before_src!r} after_src={after_src!r}")
            return False

        print(f"  [OK] Engagement identity '{identity_name}' per switch ho gayi (avatar badal gaya).")
        return True

    except Exception as e:
        print(f"  [!] Engage-as switch error: {e}")
        return False


async def human_gap():
    """Posts ke beech gap — taake reactions/comments burst mein na jayein aur spam na lage."""
    delay = random.uniform(MIN_GAP_SECONDS, MAX_GAP_SECONDS)
    print(f"  {delay:.0f}s wait (spam na lage)...\n")
    await asyncio.sleep(delay)


async def run(
    engage_as: str | None = None,
    engaged_path: Path = ENGAGED_FILE,
    log_dir: Path = LOGS_DIR,
    persona_file: str = "persona.md",
):
    """engage_as set ho to run shuru hote hi 'Comment, react, and repost as'
    identity us Page per switch karte hain (e.g. company), aur switch fail ho
    to run abort kar dete hain — galat identity se comment/react hone se
    behtar hai run hi na ho. engaged_path/log_dir alag rakhne se ek dusra
    'agent' (dusri identity ke liye) apna khud ka dedup/log history rakh
    sakta hai, personal watcher ke history se mix hue bina."""
    SESSION_DIR.mkdir(exist_ok=True)
    log_dir.mkdir(parents=True, exist_ok=True)

    print("\n" + "=" * 55)
    print("   LinkedIn Autonomous Commenter" + (f" — {engage_as}" if engage_as else ""))
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

        if engage_as:
            print(f"[*] Engagement identity '{engage_as}' per switch kar rahe hain...")
            switched = await switch_engage_as(page, engage_as)
            if not switched:
                print(
                    f"\n[!] '{engage_as}' identity per switch nahi ho saka — run "
                    "yahan rok rahe hain (galat identity se comment/react hone se "
                    "behtar hai run hi na ho).\n"
                )
                input("\nEnter dabao browser band karne ke liye...")
                await browser.close()
                return

        processed   = 0
        attempt_num = 0
        seen_ids    = set()
        engaged     = load_engaged(engaged_path)
        print(f"[*] {len(engaged)} posts already engaged in previous runs (skip list loaded).\n")

        print("[*] Scanning home feed.\n")
        scroll_num = 0
        # Relevance filter + dedup ke baad har post comment nahi banta — is
        # liye scroll budget MAX_POSTS ke hisab se scale karte hain, fixed
        # 25 kaafi nahi raha jab MAX_POSTS barha do.
        max_scrolls = max(15, MAX_POSTS * 8)
        scan_stats = {"scanned": 0, "sponsored": 0, "too_old": 0, "too_short": 0, "dedup": 0}

        # Kabhi kabhi ek chhota scrollBy(900) LinkedIn ka lazy-load trigger nahi
        # chhuta paata (ek bara post hi 900px se zyada lamba ho sakta hai), is
        # liye agla scan same posts wapas deta hai — koi naya post nahi milta.
        # Consecutive stagnant scrolls track karte hain: jab ye ho, normal
        # chhote scroll ke bajaye bottom tak bara jump + zyada wait karte hain
        # taake LinkedIn ko naya content load karne ka pura chance mile. Agar
        # phir bhi kuch naya nahi milta (feed genuinely khatam ho gaya), to
        # poora max_scrolls budget jalaye bina jaldi nikal jate hain.
        consecutive_stagnant = 0
        MAX_STAGNANT_SCROLLS = 4

        while processed < MAX_POSTS and scroll_num < max_scrolls:

            if not is_on_feed(page.url):
                await page.goto(LINKEDIN_FEED, wait_until="domcontentloaded", timeout=60000)
                await asyncio.sleep(4)

            try:
                posts, scan_debug = await get_posts_from_page(page)
            except Exception as e:
                print(f"  [!] Scan error: {e}")
                await asyncio.sleep(3)
                scroll_num += 1
                continue

            if scroll_num == 0:
                print(f"[*] {len(posts)} posts mile is scroll mein.\n")
                if not posts:
                    print(
                        f"  [DEBUG] Page par {scan_debug['totalButtons']} buttons mile, "
                        f"{scan_debug['commentBtnsFound']} 'Comment' jaise lage, "
                        f"{scan_debug['noContainer']} ka post container nahi mila.\n"
                        "  [DEBUG] Agar commentBtnsFound 0 hai to LinkedIn ne Comment button ka "
                        "text/aria-label badal diya hai (selector update chahiye). Agar "
                        "commentBtnsFound > 0 lekin noContainer barabar hai to container-size "
                        "heuristic (height>250, width>400) fail ho rahi hai.\n"
                    )

            seen_before_scroll = len(seen_ids)

            # Connected (1st-degree) aur 2nd-degree logon ki posts ko pehle
            # tackle karo — har naye-revealed batch mein priority se sort.
            posts.sort(key=lambda p: post_connection_degree(p.get("text", "")))

            for post in posts:
                if processed >= MAX_POSTS:
                    break

                post_id = post.get("id", "")
                if not post_id or post_id in seen_ids:
                    continue
                seen_ids.add(post_id)
                scan_stats["scanned"] += 1

                if post.get("sponsored"):
                    print("  [SKIP] Sponsored post.")
                    scan_stats["sponsored"] += 1
                    continue

                age_hours = post_age_hours(post.get("text", ""))
                if age_hours is not None and age_hours > MAX_POST_AGE_HOURS:
                    print(f"  [SKIP] Post {age_hours:.0f}h purana hai (limit {MAX_POST_AGE_HOURS:.0f}h) — engagement ka faida nahi.")
                    scan_stats["too_old"] += 1
                    continue

                text = clean_post_text(post.get("text", ""))
                if len(text) < 60:
                    scan_stats["too_short"] += 1
                    continue

                fp = post_fingerprint(text)
                if fp in engaged:
                    print("  [SKIP] Pehle hi kabhi engage ho chuke is post pe.")
                    scan_stats["dedup"] += 1
                    continue

                attempt_num += 1
                print(f"[POST {processed + 1}/{MAX_POSTS}]  (attempt #{attempt_num})")
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
                    save_engaged(engaged, engaged_path)

                if not is_relevant_post(text):
                    print("  [SKIP] Comment skip — post persona ke expertise/celebration scope se bahar.\n")
                    if processed < MAX_POSTS:
                        await human_gap()
                    continue

                print(f"  Generating comment...")
                loop = asyncio.get_running_loop()
                try:
                    comment = await loop.run_in_executor(_executor, generate_comment, text, persona_file)
                except Exception as e:
                    print(f"  [SKIP] Comment generate nahi hua: {e}\n")
                    # Reaction to pehle hi de di — agla post bhi gap se hi karo
                    if processed < MAX_POSTS:
                        await human_gap()
                    continue
                print(f"  Comment  : {comment}")
                print(f"  Posting...")

                success = await click_and_comment(page, post_id, comment)
                log_result(post_id, text, comment, success, reaction, reacted, log_dir=log_dir)

                if success:
                    processed += 1
                    # Reaction fail hui thi to fp abhi engaged mein nahi hoga —
                    # phir bhi comment post ho gaya, is liye entry create/update
                    # karo warna ye commented post dedup se chhoot jayega aur
                    # agle run mein dobara comment ho sakta hai.
                    entry = engaged.get(fp) or {
                        "timestamp": datetime.now().isoformat(),
                        "preview": text[:80],
                    }
                    entry["commented"] = True
                    engaged[fp] = entry
                    save_engaged(engaged, engaged_path)
                    print(f"  [OK] Done! ({processed}/{MAX_POSTS})\n")
                else:
                    print("  [FAIL] Next post pe ja raha hun.\n")

                if processed < MAX_POSTS:
                    await human_gap()

            new_found = len(seen_ids) - seen_before_scroll
            if new_found == 0:
                consecutive_stagnant += 1
                print(f"  [*] Is scroll mein koi naya post nahi mila ({consecutive_stagnant}/{MAX_STAGNANT_SCROLLS}) — bara scroll + zyada wait try kar rahe hain.")
                if consecutive_stagnant >= MAX_STAGNANT_SCROLLS:
                    print("  [*] Feed se naye posts aana band ho gaya hai — scan yahin rok rahe hain (waqt zaya nahi karte).\n")
                    break
                await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                await asyncio.sleep(random.uniform(5, 8))
            else:
                consecutive_stagnant = 0
                await page.evaluate("window.scrollBy(0, 900)")
                await asyncio.sleep(random.uniform(3, 5))
            scroll_num += 1

        print(
            f"[*] Scan summary: scanned {scan_stats['scanned']}, "
            f"sponsored {scan_stats['sponsored']}, too-old {scan_stats['too_old']}, "
            f"too-short {scan_stats['too_short']}, dedup {scan_stats['dedup']}, "
            f"engaged {processed}.\n"
        )

        print("=" * 55)
        print(f"  Complete! Comments: {processed}/{MAX_POSTS}")
        print(f"  Log: {log_dir}/{datetime.now().strftime('%Y-%m-%d')}.json")
        print("=" * 55)

        input("\nEnter dabao browser band karne ke liye...")
        await browser.close()


if __name__ == "__main__":
    asyncio.run(run())
