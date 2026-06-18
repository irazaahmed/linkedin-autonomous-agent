from linkedin_watcher import clean_post_text, pick_reaction, post_fingerprint


def test_clean_post_text_strips_ui_noise():
    raw = (
        "Feed post\n"
        "Jane Doe\n"
        "Jane Doe commented\n"
        "Suggested\n"
        "Founder @ Acme | Helping teams ship faster\n"
        "We just crossed 10,000 customers and the team made it happen.\n"
        "1,204\n"
        "Like\nComment\nShare\nSend\n"
    )
    cleaned = clean_post_text(raw)
    assert "We just crossed 10,000 customers" in cleaned
    assert "Like" not in cleaned.split("\n")
    assert "Suggested" not in cleaned
    assert "Jane Doe commented" not in cleaned
    assert "1,204" not in cleaned


def test_pick_reaction_detects_celebration():
    text = "Thrilled to announce we just hit a huge milestone for the team!"
    assert pick_reaction(text) == "celebrate"


def test_pick_reaction_detects_support():
    text = "I was laid off last week and I'm now looking for opportunities."
    assert pick_reaction(text) == "support"


def test_pick_reaction_defaults_to_like():
    text = "Just a regular Tuesday update with nothing special going on here today."
    assert pick_reaction(text) == "like"


def test_post_fingerprint_stable_for_whitespace_and_case_differences():
    a = "Some post content   with   extra spaces"
    b = "some post content with extra spaces"
    assert post_fingerprint(a) == post_fingerprint(b)


def test_post_fingerprint_differs_for_different_text():
    assert post_fingerprint("Post A content here") != post_fingerprint("Post B content here")
