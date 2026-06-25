import os
import random
import re
from pathlib import Path

from dotenv import load_dotenv
from groq import Groq, APIError

load_dotenv()

GROQ_MODEL = "llama-3.3-70b-versatile"
_client = None


def _get_client() -> Groq:
    global _client
    if _client is None:
        api_key = os.getenv("GROQ_API_KEY")
        if not api_key:
            raise RuntimeError("GROQ_API_KEY .env mein nahi mili.")
        _client = Groq(api_key=api_key)
    return _client


def load_persona(persona_file: str = "persona.md") -> str:
    path = Path(persona_file)
    if path.exists():
        return path.read_text(encoding="utf-8")
    return "AI Solutions Expert, CEO/Founder. Write thoughtful expert comments."


LENGTH_INSTRUCTIONS = {
    "short": "Length: exactly ONE sentence — short and punchy, still with one specific insight.",
    "medium": "Length: 2 to 3 sentences, per the persona's style rules above.",
}


def _pick_comment_length(post_content: str) -> str:
    """Comment ki length post ke hisab se choose karta hai — chhota/light post
    zyada context nahi deta is liye short comment zyada chance pe aata hai,
    lamba/detailed post medium (2-3 sentence) comment ke liye gunjaish deta
    hai. Hamesha 3 sentences tak mat jao — isi se comments "bohot lambay" lag
    rahe the. Thoda randomness bhi rakha hai taake same-length post bhi
    har baar same na lage."""
    short_chance = 0.65 if len(post_content) < 220 else 0.4
    return "short" if random.random() < short_chance else "medium"


def generate_comment(post_content: str, persona_file: str = "persona.md") -> str:
    persona = load_persona(persona_file)
    length_instruction = LENGTH_INSTRUCTIONS[_pick_comment_length(post_content)]

    prompt = (
        f"{persona}\n\n"
        f'A LinkedIn post says: "{post_content[:500]}"\n\n'
        f"Write ONLY the comment text this persona would post in reply — "
        f"no preamble, no explanation, no quotes around it. {length_instruction}"
    )

    try:
        response = _get_client().chat.completions.create(
            model=GROQ_MODEL,
            messages=[{"role": "user", "content": prompt}],
        )
    except APIError as e:
        raise RuntimeError(f"Groq error (code {e.status_code}): {str(e)[:300]}")

    output = (response.choices[0].message.content or "").strip()

    if not output:
        raise RuntimeError("Groq error: khaali response mila.")

    return extract_comment(output)


BAD_PHRASES = (
    "could you share", "what specific", "are you looking to",
    "full post content", "let me know", "happy to help",
    "i need more", "can you provide", "i just need", "clarify",
)


def _is_valid_comment(p: str) -> bool:
    """Reject sentence fragments (mid-thought, lowercase start) aur clarifying questions."""
    p = p.strip()
    if len(p) < 40:
        return False
    if not (p[0].isupper() or p[0] in '"\''):
        return False
    low = p.lower()
    if any(bp in low for bp in BAD_PHRASES):
        return False
    return True


def extract_comment(raw: str) -> str:
    """Groq ke extra explanation hata ke sirf comment text nikalta hai."""

    # Method 1: Blockquote lines (> "comment text")
    blockquote_lines = []
    for line in raw.split("\n"):
        line = line.strip()
        if line.startswith(">"):
            content = line.lstrip(">").strip().strip('"').strip("'")
            if content:
                blockquote_lines.append(content)
    if blockquote_lines:
        candidate = " ".join(blockquote_lines)
        if _is_valid_comment(candidate):
            return candidate

    # Method 2: Quoted block (double quotes spanning 60+ chars)
    quoted = re.findall(r'"([^"]{60,})"', raw)
    for q in quoted:
        if _is_valid_comment(q):
            return q.strip()

    # Method 3: Sabse lamba valid paragraph jo meta text ya fragment na ho
    meta_starts = (
        "following", "here's", "based on", "as an", "if you",
        "you can", "let me", "note:", "per the", "i would", "this comment",
        "per your", "want me", "i can run",
    )
    paragraphs = [p.strip() for p in raw.split("\n") if len(p.strip()) > 40]
    content = [
        p for p in paragraphs
        if _is_valid_comment(p) and not any(p.lower().startswith(s) for s in meta_starts)
    ]
    if content:
        return max(content, key=len)

    # Koi clean comment nahi mila — garbage post karne se behtar hai fail ho jaye
    raise RuntimeError("Groq se clean comment nahi mila (sirf meta-text ya fragment tha).")
