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
    "short": "Length: ONE short sentence (under ~18 words) with one specific point.",
    "medium": "Length: at most TWO short sentences, under ~25 words total. Never more.",
}


def _pick_comment_length(post_content: str) -> str:
    """Comment chhota rakhte hain — zyada tar ONE sentence, kabhi kabhi do.
    Chhota/light post ko zyada chance short, detailed post ko bhi mostly short
    par thodi gunjaish do-sentence ki. Thoda randomness taake same-length post
    har baar same na lage."""
    short_chance = 0.85 if len(post_content) < 220 else 0.7
    return "short" if random.random() < short_chance else "medium"


# Roman Urdu (Urdu likha in English letters) ke high-frequency function words —
# ye English professional posts mein practically kabhi nahi aate, is liye inki
# ginti se language reliably detect ho jati hai bina kisi library ke.
ROMAN_URDU_MARKERS = {
    "hai", "hain", "ka", "ki", "ke", "ko", "ne", "se", "mein", "par", "aur",
    "nahi", "nahin", "kya", "kyun", "kaise", "kaisay", "aap", "hum", "tum",
    "mera", "meri", "mere", "apna", "apni", "liye", "kaam", "bohot", "bhot",
    "acha", "accha", "theek", "yeh", "ye", "woh", "wo", "kar", "karo", "karta",
    "karti", "karna", "raha", "rahi", "rahe", "kiya", "gaya", "gayi", "hua",
    "hui", "jo", "jis", "bhi", "sab", "kuch", "thoda", "zyada", "bina", "taake",
    "warna", "phir", "abhi", "wapas", "dekho", "batao", "chahiye", "matlab",
    "log", "logon", "baat", "waqt", "sahi", "banaya", "diya", "milta", "hota",
}


def _detect_language(post_content: str) -> str:
    """Post Roman Urdu mein hai ya English — marker words ginta hai. 3+ markers
    mile to Roman Urdu, warna English."""
    tokens = re.findall(r"[a-z]+", post_content.lower())
    hits = sum(1 for t in tokens if t in ROMAN_URDU_MARKERS)
    return "roman_urdu" if hits >= 3 else "english"


LANGUAGE_INSTRUCTIONS = {
    "roman_urdu": (
        "The post is written in Roman Urdu (Urdu typed in English letters). Write "
        "your comment in Roman Urdu too — natural and conversational, the way "
        "people actually type on LinkedIn. Do NOT use Urdu script and do NOT "
        "switch to formal English."
    ),
    "english": (
        "The post is in English. Write your comment in plain, simple, easy "
        "English — short everyday words, no jargon, no fancy vocabulary."
    ),
}


# System role instruction user-prompt se zyada strongly obey hoti hai — pehle
# format rule sirf user prompt mein tha aur llama phir bhi meta-text/fragment
# de deta tha (ek run mein 8 mein se 3 comment isi wajah se skip hue).
SYSTEM_PROMPT = (
    "You write SHORT {platform} comments in the voice of the given persona. "
    "Keep every comment brief and use plain, simple, easy words — never long, "
    "never fancy or jargon-heavy. Always write in the SAME language and script "
    "as the post: if the post is Roman Urdu (Urdu in English letters), reply in "
    "Roman Urdu; if it is English, reply in plain English. "
    "Do NOT open with formulaic phrases like 'We've seen', 'I've seen', "
    "'Great post', 'Love this', or 'Congratulations'. Do NOT invent statistics, "
    "numbers, or percentages. Write the comment as a statement, not a question — "
    "only rarely end with a question, and never a generic one. "
    "Output ONLY the raw comment text that will be posted — no preamble, "
    "no explanation, no surrounding quotes, no markdown, no bullet points, "
    "no multiple options. Never ask a clarifying question and never say you "
    "need more context; if unsure, still write one confident, specific comment "
    "grounded in the post."
)


def generate_comment(
    post_content: str,
    persona_file: str = "persona.md",
    platform: str = "LinkedIn",
) -> str:
    persona = load_persona(persona_file)
    length_instruction = LENGTH_INSTRUCTIONS[_pick_comment_length(post_content)]
    language_instruction = LANGUAGE_INSTRUCTIONS[_detect_language(post_content)]

    user_prompt = (
        f"{persona}\n\n"
        f'A {platform} post says: "{post_content[:500]}"\n\n'
        f"Write ONLY the comment text this persona would post in reply. "
        f"{language_instruction} {length_instruction}"
    )

    # Groq kabhi kabhi meta-text ya adhoora fragment de deta hai jise
    # extract_comment reject kar deta hai — ek hi call pe wo post seedha skip ho
    # jata tha. Groq fast/sasta hai, is liye 2 retry lagate hain (temperature>0
    # se har retry alag sample deta hai); teeno fail hon to hi skip.
    last_err: Exception = RuntimeError("Groq se clean comment nahi mila.")
    for _ in range(3):
        try:
            response = _get_client().chat.completions.create(
                model=GROQ_MODEL,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT.format(platform=platform)},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=0.8,
            )
        except APIError as e:
            raise RuntimeError(f"Groq error (code {e.status_code}): {str(e)[:300]}")

        output = (response.choices[0].message.content or "").strip()
        if not output:
            last_err = RuntimeError("Groq error: khaali response mila.")
            continue

        try:
            return extract_comment(output)
        except RuntimeError as e:
            last_err = e
            continue

    raise last_err


BAD_PHRASES = (
    "could you share", "what specific", "are you looking to",
    "full post content", "let me know", "happy to help",
    "i need more", "can you provide", "i just need", "clarify",
)


def _is_valid_comment(p: str) -> bool:
    """Reject sentence fragments (mid-thought, lowercase start) aur clarifying questions."""
    p = p.strip()
    if len(p) < 30:  # comments ab chhote hain — 40 valid short comment reject kar deta
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
    paragraphs = [p.strip() for p in raw.split("\n") if len(p.strip()) >= 30]
    content = [
        p for p in paragraphs
        if _is_valid_comment(p) and not any(p.lower().startswith(s) for s in meta_starts)
    ]
    if content:
        return max(content, key=len)

    # Koi clean comment nahi mila — garbage post karne se behtar hai fail ho jaye
    raise RuntimeError("Groq se clean comment nahi mila (sirf meta-text ya fragment tha).")
