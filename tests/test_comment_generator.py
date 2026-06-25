import pytest

from comment_generator import extract_comment, _is_valid_comment, _pick_comment_length


def test_is_valid_comment_rejects_short_text():
    assert not _is_valid_comment("Too short.")


def test_is_valid_comment_rejects_lowercase_start():
    text = "this comment starts lowercase and is definitely long enough to pass the length check."
    assert not _is_valid_comment(text)


def test_is_valid_comment_rejects_bad_phrases():
    text = "Could you share the full post content so I can write something specific?"
    assert not _is_valid_comment(text)


def test_is_valid_comment_accepts_clean_comment():
    text = "The bottleneck is never the model, it's integrating it into existing workflows."
    assert _is_valid_comment(text)


def test_extract_comment_from_blockquote():
    raw = (
        'Here is a comment:\n\n'
        '> "The real challenge with automation is not the tooling, '
        'it is organizational buy-in across teams."\n\n'
        "Let me know if you want variations."
    )
    result = extract_comment(raw)
    assert result.startswith("The real challenge")


def test_extract_comment_from_quoted_block():
    raw = (
        "Sure, here's a comment you could post: "
        '"We have seen this exact pattern with enterprise clients rolling out '
        'AI tools without addressing the underlying workflow gaps first." '
        "Hope that helps!"
    )
    result = extract_comment(raw)
    assert "enterprise clients" in result


def test_extract_comment_fallback_paragraph():
    raw = (
        "Following the post's theme, here's a comment:\n\n"
        "Most companies treat AI adoption as a tooling problem when it is "
        "actually a change management problem, and that gap is exactly "
        "where most rollouts stall.\n\n"
        "Let me know if you'd like another version."
    )
    result = extract_comment(raw)
    assert result.startswith("Most companies")


def test_extract_comment_raises_on_garbage():
    raw = "could you share more details? what specific outcome are you looking to achieve here?"
    with pytest.raises(RuntimeError):
        extract_comment(raw)


def test_pick_comment_length_short_post_below_threshold_roll(monkeypatch):
    monkeypatch.setattr("comment_generator.random.random", lambda: 0.1)
    assert _pick_comment_length("short post") == "short"


def test_pick_comment_length_short_post_above_threshold_roll(monkeypatch):
    monkeypatch.setattr("comment_generator.random.random", lambda: 0.9)
    assert _pick_comment_length("short post") == "medium"


def test_pick_comment_length_long_post_uses_lower_short_chance(monkeypatch):
    long_post = "x" * 300
    monkeypatch.setattr("comment_generator.random.random", lambda: 0.5)
    assert _pick_comment_length(long_post) == "medium"
    monkeypatch.setattr("comment_generator.random.random", lambda: 0.3)
    assert _pick_comment_length(long_post) == "short"
