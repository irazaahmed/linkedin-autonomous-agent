import asyncio
import json
import os
import random
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

import linkedin_watcher
from linkedin_watcher import run

load_dotenv()

# Personal watcher (linkedin_watcher.py) ke same logic/fixes reuse karta hai —
# sirf engagement identity, logs aur dedup-history alag rakhte hain taake
# dono "agents" ek dusre ke history se mix na hon, aur koi bhi future DOM fix
# linkedin_watcher.py mein karne se dono ko mil jaye, dobara likhna na pade.
ENGAGE_AS_PAGE = os.getenv("ENGAGE_AS_PAGE", "Cybrum Solutions")
LOG_DIR        = Path("logs/cybrum")
ENGAGED_FILE   = LOG_DIR / "engaged.json"
PERSONA_FILE   = "persona_cybrum.md"

# --- Engage-as switcher diagnostic (sirf cybrum run ke liye) ---------------
# Live DOM dump (26-Jun-2026) ne confirm kiya: feed per "Comment, react, and
# repost as" jaisa koi session-wide switcher button hai hi nahi — shared
# switch_engage_as ka feed-level selector kabhi match nahi karega. Asli engage-as
# control comment composer ke andar (avatar+naam dropdown) hota hai, jo Comment
# box khulne ke BAAD render hota hai. Personal watcher chhede bina yahan shared
# function ko monkeypatch karte hain: original try hota hai, fail ho to feed +
# composer dono ka read-only DOM snapshot file mein dump hota hai taake asli
# switcher DOM mil jaye. Composer khulta hai sirf focus ke liye (Escape se band) —
# koi text/submit nahi hota.
_original_switch_engage_as = linkedin_watcher.switch_engage_as


async def _dump_switcher_dom(page, identity_name: str) -> dict:
    """Feed ka targeted, read-only DOM snapshot return karta hai. Sirf padhta
    hai — kuch click/type/submit nahi karta."""
    snapshot = await page.evaluate(
        """
        (pageName) => {
            const norm = s => (s || '').replace(/\\s+/g, ' ').trim();
            const describe = el => ({
                tag: el.tagName.toLowerCase(),
                role: el.getAttribute('role') || null,
                ariaLabel: el.getAttribute('aria-label') || null,
                ariaHaspopup: el.getAttribute('aria-haspopup') || null,
                title: el.getAttribute('title') || null,
                text: norm(el.innerText).slice(0, 80) || null,
                cls: norm((el.className || '').toString()).slice(0, 140) || null,
            });

            // 1) Sab clickable / menu-trigger elements (switcher inhi mein hoga)
            const clickables = Array.from(document.querySelectorAll(
                'button, a[role="button"], [role="button"], [aria-haspopup]'
            )).slice(0, 150).map(describe);

            // 2) Koi bhi element jo page name mention karta ho (e.g. "Posting as ...")
            const mentionsPage = Array.from(document.querySelectorAll('*'))
                .filter(el => {
                    const t = norm(el.innerText);
                    const a = el.getAttribute && (el.getAttribute('aria-label') || '');
                    return (t && t.length < 140 && t.includes(pageName))
                        || (a && a.includes(pageName));
                })
                .slice(0, 25)
                .map(describe);

            // 3) "as" / comment / react / repost wale aria-labels (broad, sorted)
            const actorLike = Array.from(document.querySelectorAll('[aria-label]'))
                .filter(el => /\\bas\\b|comment|react|repost|posting/i
                    .test(el.getAttribute('aria-label') || ''))
                .slice(0, 40)
                .map(describe);

            return { clickables, mentionsPage, actorLike };
        }
        """,
        identity_name,
    )

    return snapshot


async def _dump_comment_composer_dom(page, identity_name: str) -> dict:
    """Pehle post ka comment-box kholta hai (sirf focus — koi text/submit nahi),
    phir us composer ke aas-paas ka actor-switcher DOM capture karta hai, aur
    Escape se box band kar deta hai.

    v1 (ancestor-climb, <img> tag dhoondta tha) 3 alag runs mein kuch nahi mila —
    root cause: (1) LinkedIn avatars aksar <img> nahi, background-image wale
    <div>/<span> hote hain, is liye img-check kabhi match hi nahi hui; (2) fixed
    8-level climb composer ke deeply-nested wrapper divs mein editor ke bohot
    kareeb hi ruk gaya, us se upar wale row (jahan avatar+switcher hota) tak
    pahunchi hi nahi. v2 isliye DOM-ancestry chhod kar viewport-proximity use
    karta hai — editor ke bounding rect ke upar/aas-paas jo bhi clickable
    element hai (chahe wo kahin bhi DOM mein ho, ancestor ho ya na ho) wahi
    switcher ka candidate hai, plus poore page mein 'Comment/Post as' jaisa
    text/aria-label alag se dhoondte hain."""
    opened = await page.evaluate(
        """
        () => {
            const btn = Array.from(document.querySelectorAll('button'))
                .find(b => (b.getAttribute('aria-label') || '').trim() === 'Comment');
            if (!btn) return false;
            btn.click();
            return true;
        }
        """
    )
    if not opened:
        return {"opened": False}

    await asyncio.sleep(random.uniform(1.8, 2.6))

    snapshot = await page.evaluate(
        """
        () => {
            const norm = s => (s || '').replace(/\\s+/g, ' ').trim();
            const describe = el => {
                const r = el.getBoundingClientRect();
                return {
                    tag: el.tagName.toLowerCase(),
                    role: el.getAttribute('role') || null,
                    ariaLabel: el.getAttribute('aria-label') || null,
                    ariaHaspopup: el.getAttribute('aria-haspopup') || null,
                    title: el.getAttribute('title') || null,
                    alt: el.getAttribute('alt') || null,
                    text: norm(el.innerText).slice(0, 80) || null,
                    cls: norm((el.className || '').toString()).slice(0, 160) || null,
                    rect: { top: Math.round(r.top), left: Math.round(r.left),
                             w: Math.round(r.width), h: Math.round(r.height) },
                };
            };

            const editor = document.querySelector(
                'div.ql-editor[contenteditable="true"], [role="textbox"][contenteditable="true"]'
            );
            if (!editor) return { editorFound: false };
            const eRect = editor.getBoundingClientRect();

            // Candidate A: koi bhi clickable jo editor ke top se ~220px upar tak,
            // usi horizontal band mein ho — DOM position se qatai farq nahi
            // padta, sirf screen per kahan hai wo matter karta hai.
            const nearby = Array.from(document.querySelectorAll(
                'button, a[role="button"], [role="button"], [aria-haspopup]'
            ))
                .filter(el => {
                    const r = el.getBoundingClientRect();
                    return r.width > 0 && r.height > 0 &&
                        r.top <= eRect.top + 8 &&
                        (eRect.top - r.top) < 220;
                })
                .slice(0, 25)
                .map(describe);

            // Candidate B: chhote (avatar-size) elements jinka background-image
            // set hai — LinkedIn profile photos aksar <img> ke bajaye is tarah
            // render hote hain.
            const bgImages = Array.from(document.querySelectorAll('div, span'))
                .filter(el => {
                    const r = el.getBoundingClientRect();
                    if (r.width < 14 || r.width > 60 || r.height < 14 || r.height > 60) return false;
                    const bg = getComputedStyle(el).backgroundImage;
                    return bg && bg.includes('url(');
                })
                .slice(0, 15)
                .map(describe);

            // Candidate C: poore page (composer se bahar bhi — portal-rendered ho
            // sakta hai) mein 'comment as' / 'post as' jaisa text ya aria-label.
            const textMatches = Array.from(document.querySelectorAll('[aria-label], button, a'))
                .filter(el => /comment(ing)?\\s+as|post(ing)?\\s+as/i
                    .test((el.getAttribute('aria-label') || '') + ' ' + norm(el.innerText)))
                .slice(0, 15)
                .map(describe);

            return {
                editorFound: true,
                editorRect: { top: Math.round(eRect.top), left: Math.round(eRect.left) },
                nearby,
                bgImages,
                textMatches,
            };
        }
        """
    )

    # box band kar do taake page saaf rahe (kuch post nahi hua)
    try:
        await page.keyboard.press("Escape")
    except Exception:
        pass

    return {"opened": True, **snapshot}


async def _write_switch_debug(page, identity_name: str) -> Path:
    """Feed-level + comment-composer dono read-only snapshots ek JSON file mein."""
    feed = await _dump_switcher_dom(page, identity_name)
    try:
        composer = await _dump_comment_composer_dom(page, identity_name)
    except Exception as e:
        composer = {"error": str(e)}

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out = LOG_DIR / f"switch_debug_{stamp}.json"
    out.write_text(
        json.dumps(
            {
                "identity_name": identity_name,
                "url": page.url,
                "feed_snapshot": feed,
                "composer_snapshot": composer,
            },
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return out


async def switch_engage_as_with_dump(page, identity_name: str) -> bool:
    """Pehle asli switch try karta hai; fail ho to deep read-only DOM dump le kar
    file ka path print karta hai (taake ek live run mein asli switcher DOM mil
    jaye), phir False return karta hai — galat identity se engage karne se behtar
    hai run ruk jaye."""
    ok = await _original_switch_engage_as(page, identity_name)
    if ok:
        return True

    try:
        out = await _write_switch_debug(page, identity_name)
        print(f"\n  [DEBUG+] Switcher DOM snapshot likh diya: {out}")
        print("  [DEBUG+] (feed + comment-composer dono) — ye file mujhe paste kar do.\n")
    except Exception as e:
        print(f"  [!] DOM snapshot lete waqt error: {e}")

    return False


# Shared run() switch_engage_as ko apne module-namespace se naam se call karta hai,
# is liye yahan module attribute replace karne se sirf cybrum run affect hota hai;
# linkedin_watcher.py file disk per bilkul waisi ki waisi rehti hai.
linkedin_watcher.switch_engage_as = switch_engage_as_with_dump

if __name__ == "__main__":
    asyncio.run(run(
        engage_as=ENGAGE_AS_PAGE,
        engaged_path=ENGAGED_FILE,
        log_dir=LOG_DIR,
        persona_file=PERSONA_FILE,
    ))
