"""X me login ka aasaan rasta — agent wala profile AAM Chrome me kholo.

Masla: Playwright jab Chrome chalata hai to automation ke flags ke sath
chalata hai (--enable-automation waghera). X unhe dekh kar login par sakhti
kar deta hai — password ki jagah phone maangta hai, phir aagay nahi barhta.

Hal: ye script wahi profile (session/x_profile) BILKUL normal Chrome me
kholti hai — koi automation flag nahi, isliye X ke liye ye ek aam browser
hai aur login normal tarah chalta hai (email/username -> password).
Login profile me save ho jata hai; uske baad x_watcher usi profile se
seedha logged-in khulta hai. Sirf EK baar chalana hota hai.

Istemal:
  1. python x_manual_login.py
  2. Jo Chrome window khule usme X login karo (home timeline aa jaye)
  3. Chrome ki WOH window poori band karo
  4. python x_watcher.py  (ya dashboard se Run X Agent) — login nahi poochhega
"""

import subprocess
import sys
from pathlib import Path

from x_watcher import PROFILE_DIR

CHROME_PATHS = [
    Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
    Path(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"),
]


def find_chrome() -> Path:
    for p in CHROME_PATHS:
        if p.exists():
            return p
    print("[!] Chrome nahi mila — in jagahon par dekha:")
    for p in CHROME_PATHS:
        print(f"    {p}")
    sys.exit(1)


def main():
    chrome = find_chrome()
    profile = str(PROFILE_DIR.resolve())
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)

    print("\n" + "=" * 55)
    print("   X Manual Login — agent ke profile me normal login")
    print("=" * 55)
    print(f"""
  [*] Chrome khul raha hai (profile: {profile})

  1. Is window me x.com par LOGIN karo
     (tip: email ki jagah @username dalo to phone wala
      chakkar aksar aata hi nahi)
  2. Home timeline nazar aa jaye = login mukammal
  3. Phir ye Chrome window POORI band kar do
  4. Uske baad chalao:  python x_watcher.py
     — login screen nahi ayegi, seedha kaam shuru
""")

    # subprocess.run yahan RUKTA hai jab tak user Chrome band na kare —
    # isi liye window band hote hi neeche wala "ab agent chalao" message
    # theek waqt par chhapta hai.
    subprocess.run([
        str(chrome),
        f"--user-data-dir={profile}",
        "--no-first-run",
        "--no-default-browser-check",
        "https://x.com/login",
    ])

    print("\n[OK] Chrome band ho gaya. Agar login mukammal tha to ab")
    print("     'python x_watcher.py' chalao — seedha timeline khulegi.\n")


if __name__ == "__main__":
    main()
