"""Drives the REAL agent functions (linkedin_watcher.py / comment_generator.py)
against a local mock feed (demo/mock_feed.html) instead of real LinkedIn, and
records the session as a video. No real LinkedIn account, no real third-party
data, no ToS exposure — proves the agent's actual decision logic on camera."""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from playwright.async_api import async_playwright

from comment_generator import generate_comment
from linkedin_watcher import (
    STEALTH_SCRIPT,
    clean_post_text,
    click_and_comment,
    get_posts_from_page,
    is_relevant_post,
    pick_reaction,
    react_to_post,
)

MOCK_PAGE_URL = (Path(__file__).parent / "mock_feed.html").resolve().as_uri()
RECORDING_DIR = Path(__file__).parent / "recording"


async def main():
    RECORDING_DIR.mkdir(exist_ok=True)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False, channel="chrome")
        context = await browser.new_context(
            viewport={"width": 1280, "height": 900},
            record_video_dir=str(RECORDING_DIR),
            record_video_size={"width": 1280, "height": 900},
        )
        await context.add_init_script(STEALTH_SCRIPT)
        page = await context.new_page()

        await page.goto(MOCK_PAGE_URL)
        await asyncio.sleep(3)

        posts = await get_posts_from_page(page)
        print(f"[*] Found {len(posts)} demo posts\n")

        for post in posts:
            post_id = post["id"]
            text = clean_post_text(post["text"])
            if len(text) < 60:
                continue

            print(f"[POST] {text[:90]}...")

            reaction = pick_reaction(text)
            print(f"  Reaction: {reaction}")
            reacted = await react_to_post(page, post_id, reaction)
            print(f"  Reacted: {reacted}")
            await asyncio.sleep(2)

            if not is_relevant_post(text):
                print("  Not relevant to persona/celebration scope — skipping comment.\n")
                await asyncio.sleep(2.5)
                continue

            print("  Generating real comment via Groq...")
            loop = asyncio.get_event_loop()
            try:
                comment = await loop.run_in_executor(None, generate_comment, text)
            except Exception as e:
                print(f"  [SKIP] Comment generation failed: {e}\n")
                await asyncio.sleep(2)
                continue
            print(f"  Comment: {comment}")

            success = await click_and_comment(page, post_id, comment)
            print(f"  Posted: {success}\n")
            await asyncio.sleep(3)

        await asyncio.sleep(3)
        await context.close()
        await browser.close()

    files = list(RECORDING_DIR.glob("*.webm"))
    if files:
        print(f"[*] Video saved: {files[-1]}")


asyncio.run(main())
