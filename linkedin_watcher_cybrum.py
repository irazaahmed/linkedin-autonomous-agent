import asyncio
import os
from pathlib import Path

from dotenv import load_dotenv

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

if __name__ == "__main__":
    asyncio.run(run(
        engage_as=ENGAGE_AS_PAGE,
        engaged_path=ENGAGED_FILE,
        log_dir=LOG_DIR,
        persona_file=PERSONA_FILE,
    ))
