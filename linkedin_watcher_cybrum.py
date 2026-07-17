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


async def _dump_reaction_button_neighborhood(page, identity_name: str) -> dict:
    """User ne bataya: switcher comment composer mein nahi, seedha post ke
    Reaction (Like) button ke paas hota hai — wahan chhoti si tasveer (current
    engage-as identity ka avatar) dikhti hai, usi per click karke Cybrum
    Solutions select hota hai. Composer kholne ki zaroorat nahi — pehle post
    ka reaction button dhoondo, uski poori neighborhood (khud ka outerHTML,
    parent action-bar row ka outerHTML jisme sab siblings hain, aur ~120px ke
    radius mein koi bhi <img> ya background-image wala chhota element) dump
    karo. Kuch click nahi karta — sirf padhta hai."""
    return await page.evaluate(
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
                    alt: el.getAttribute('alt') || null,
                    text: norm(el.innerText).slice(0, 60) || null,
                    cls: norm((el.className || '').toString()).slice(0, 160) || null,
                    rect: { top: Math.round(r.top), left: Math.round(r.left),
                             w: Math.round(r.width), h: Math.round(r.height) },
                };
            };

            const reactionBtn = document.querySelector('button[aria-label^="Reaction button state"]');
            if (!reactionBtn) return { found: false };

            const rRect = reactionBtn.getBoundingClientRect();
            const parent = reactionBtn.parentElement;
            const grandparent = parent ? parent.parentElement : null;

            // Radius search: koi bhi <img> ya chhota background-image wala
            // element jo reaction button ke ~120px andar ho — DOM position se
            // farq nahi padta, jaisa comment-box search mein bhi kiya tha.
            const nearbyVisuals = Array.from(document.querySelectorAll('img, div, span'))
                .filter(el => {
                    const r = el.getBoundingClientRect();
                    if (r.width === 0 || r.height === 0) return false;
                    const dx = Math.abs((r.left + r.width / 2) - (rRect.left + rRect.width / 2));
                    const dy = Math.abs((r.top + r.height / 2) - (rRect.top + rRect.height / 2));
                    if (dx > 160 || dy > 100) return false;
                    if (el.tagName.toLowerCase() === 'img') return true;
                    const bg = getComputedStyle(el).backgroundImage;
                    return bg && bg.includes('url(');
                })
                .slice(0, 20)
                .map(describe);

            // Siblings within the action-bar row (Like/Comment/Repost live here —
            // an identity badge, if any, is most likely a sibling of this button).
            const siblings = parent
                ? Array.from(parent.children).slice(0, 15).map(describe)
                : [];

            // Pichli run ne ek chhota (24x24) unlabeled <img> reaction button ke
            // BILKUL left mein pakda (same row, thodi si upar/neeche tolerance) —
            // ye hi identity-avatar hone ka strongest candidate hai. Uske exact
            // center point per elementFromPoint chalao aur upar climb karke
            // sabse kareeb wala CLICKABLE ancestor dhoondo (role/tabindex/
            // aria-haspopup/cursor:pointer — chahe khud <img> clickable na ho,
            // uska wrapper hoga).
            const avatarImg = Array.from(document.querySelectorAll('img'))
                .filter(el => {
                    const r = el.getBoundingClientRect();
                    if (r.width < 14 || r.width > 40) return false;
                    const sameRow = Math.abs((r.top + r.height / 2) - (rRect.top + rRect.height / 2)) < 20;
                    const isLeftOf = (r.left + r.width) <= rRect.left + 4;
                    return sameRow && isLeftOf;
                })
                .sort((a, b) => b.getBoundingClientRect().left - a.getBoundingClientRect().left)[0] || null;

            let avatarClickTarget = null;
            if (avatarImg) {
                const ir = avatarImg.getBoundingClientRect();
                const cx = ir.left + ir.width / 2;
                const cy = ir.top + ir.height / 2;
                const atPoint = document.elementFromPoint(cx, cy);
                const chain = [];
                let node = atPoint;
                for (let i = 0; i < 6 && node; i++) {
                    const style = getComputedStyle(node);
                    chain.push({
                        ...describe(node),
                        tabIndex: node.tabIndex,
                        cursor: style.cursor,
                        hasOnclickAttr: node.hasAttribute('onclick'),
                    });
                    node = node.parentElement;
                }
                avatarClickTarget = {
                    imgSrc: avatarImg.getAttribute('src'),
                    imgRect: describe(avatarImg).rect,
                    pointChecked: { x: Math.round(cx), y: Math.round(cy) },
                    ancestorChain: chain,
                };
            }

            return {
                found: true,
                reactionBtnRect: describe(reactionBtn).rect,
                reactionBtnHTML: (reactionBtn.outerHTML || '').slice(0, 2000),
                parentHTML: (parent ? parent.outerHTML : '').slice(0, 6000),
                grandparentTag: grandparent ? grandparent.tagName.toLowerCase() : null,
                grandparentCls: grandparent ? norm((grandparent.className || '').toString()).slice(0, 200) : null,
                siblings,
                nearbyVisuals,
                avatarClickTarget,
            };
        }
        """
    )


async def _dump_identity_flyout(page, identity_name: str) -> dict:
    """v1 bug mila: `page.locator('[aria-label="Switch to different account"]').first`
    GLOBAL selector hai — poore page per LinkedIn shayad yehi aria-label kayi
    jagah reuse karta hai (sidebar profile-card switcher, etc.), aur `.first`
    ne DOM-order mein pehla wala pakad liya — jo sidebar ka apna account-
    switcher nikla (Ahmed Raza/Cybrum Solutions ka post-level picker nahi).
    User ne confirm kiya: reaction button ke paas wala avatar click karne se
    'Ahmed Raza' / 'Cybrum Solutions' list wala chhota dialog khulta hai.

    Fix: SAME position-based lookup dobara karte hain (avatar jo reaction
    button ke bilkul left mein, same row per hai) aur us EXACT node ko click
    karte hain — global selector se dobara query nahi karte. Poora
    find→click→wait→capture ek hi evaluate() call mein hai taake wahi node
    reference use ho jo humne dhoonda. Capture ab role se nahi, seedha
    identity_name/'Ahmed Raza' jaisे text se dhoondta hai — kyunke pichli
    baar role="menu" filter ne sirf unrelated pre-existing sidebar/video
    menus pakad liye the, jo humare click ka nateeja thi hi nahi."""
    result = await page.evaluate(
        """
        async (pageName) => {
            const norm = s => (s || '').replace(/\\s+/g, ' ').trim();
            const describe = el => {
                const r = el.getBoundingClientRect();
                return {
                    tag: el.tagName.toLowerCase(),
                    role: el.getAttribute('role') || null,
                    ariaLabel: el.getAttribute('aria-label') || null,
                    text: norm(el.innerText).slice(0, 100) || null,
                    cls: norm((el.className || '').toString()).slice(0, 160) || null,
                    rect: { top: Math.round(r.top), left: Math.round(r.left),
                             w: Math.round(r.width), h: Math.round(r.height) },
                };
            };

            const reactionBtn = document.querySelector('button[aria-label^="Reaction button state"]');
            if (!reactionBtn) return { triggerFound: false, reason: 'no reaction button' };
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
            if (!avatarImg) return { triggerFound: false, reason: 'no avatar image found near reaction button' };

            const ir = avatarImg.getBoundingClientRect();
            const atPoint = document.elementFromPoint(ir.left + ir.width / 2, ir.top + ir.height / 2);

            // Upar climb karke pehla "clickable-looking" ancestor dhoondo
            // (role/tabIndex/aria-haspopup/cursor:pointer) — yehi node click
            // karenge, koi doosri query nahi.
            let clickTarget = atPoint;
            for (let i = 0; i < 6 && clickTarget; i++) {
                const style = getComputedStyle(clickTarget);
                const looksClickable = clickTarget.tabIndex >= 0 ||
                    clickTarget.getAttribute('role') === 'button' ||
                    clickTarget.hasAttribute('aria-haspopup') ||
                    clickTarget.tagName.toLowerCase() === 'a' ||
                    clickTarget.tagName.toLowerCase() === 'button';
                if (looksClickable) break;
                clickTarget = clickTarget.parentElement;
            }
            if (!clickTarget) return { triggerFound: false, reason: 'no clickable ancestor found' };

            const beforeClickInfo = describe(clickTarget);
            clickTarget.click();
            await new Promise(r => setTimeout(r, 2000));

            // Poore page mein identity_name aur 'Ahmed Raza' jaisा text dhoondo —
            // role/tag pe bharosa nahi karte is baar, sirf text per.
            const findTextMatches = (needle) => Array.from(document.querySelectorAll('body *'))
                .filter(el => {
                    if (el.children.length > 3) return false; // leaf-ish elements only
                    const t = norm(el.innerText);
                    return t && t.length < 60 && t.includes(needle);
                })
                .slice(0, 10)
                .map(describe);

            const identityMatches = findTextMatches(pageName);
            const personalMatches = findTextMatches('Ahmed Raza');

            // Aur ek broad net: kuch bhi jo click se pehle DOM mein nahi tha
            // (naya element) — hum sirf ab ke visible clickable elements ka
            // ek sample bhi de dete hain jo click point ke qareeb (300px) hon.
            const cx = ir.left, cy = ir.top;
            const nearClickPoint = Array.from(document.querySelectorAll('button, a, li, [role]'))
                .filter(el => {
                    const r = el.getBoundingClientRect();
                    if (r.width === 0 || r.height === 0) return false;
                    return Math.abs(r.top - cy) < 300 && Math.abs(r.left - cx) < 300;
                })
                .slice(0, 25)
                .map(describe);

            return {
                triggerFound: true,
                clickedElement: beforeClickInfo,
                identityMatches,
                personalMatches,
                nearClickPoint,
            };
        }
        """,
        identity_name,
    )

    try:
        await page.keyboard.press("Escape")
    except Exception:
        pass
    await asyncio.sleep(0.5)

    return result


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
    """Feed-level + reaction-button-neighborhood + comment-composer — teeno
    read-only snapshots ek JSON file mein."""
    feed = await _dump_switcher_dom(page, identity_name)
    try:
        reaction = await _dump_reaction_button_neighborhood(page, identity_name)
    except Exception as e:
        reaction = {"error": str(e)}
    try:
        flyout = await _dump_identity_flyout(page, identity_name)
    except Exception as e:
        flyout = {"error": str(e)}
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
                "reaction_button_snapshot": reaction,
                "identity_flyout_snapshot": flyout,
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
