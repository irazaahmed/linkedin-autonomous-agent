from linkedin_watcher import (
    clean_post_text,
    is_relevant_post,
    pick_reaction,
    post_age_hours,
    post_connection_degree,
    post_fingerprint,
)


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


def test_is_relevant_post_accepts_ai_automation_topics():
    text = "We just rolled out an AI agent to automate our internal workflow approvals."
    assert is_relevant_post(text)


def test_is_relevant_post_accepts_celebration_achievement():
    text = "Thrilled to announce I just crossed 3,000 followers on this platform!"
    assert is_relevant_post(text)


def test_is_relevant_post_rejects_unrelated_course_post():
    text = "Popular course on LinkedIn Learning: Testing React Applications with Jest and React Testing Library."
    assert not is_relevant_post(text)


def test_post_age_hours_parses_hours():
    raw = "Feed post\nJane Doe\nFounder @ Acme\n6h • Edited\nGreat post body here.\n"
    assert post_age_hours(raw) == 6.0


def test_post_age_hours_parses_days_and_weeks():
    assert post_age_hours("Jane Doe\nFounder @ Acme\n2d\nBody text") == 48.0
    assert post_age_hours("Jane Doe\nFounder @ Acme\n3w\nBody text") == 3 * 24 * 7


def test_post_age_hours_parses_minutes_and_now():
    assert post_age_hours("Jane Doe\nFounder @ Acme\n45m\nBody text") == 0.75
    assert post_age_hours("Jane Doe\nFounder @ Acme\nNow\nBody text") == 0.0


def test_post_age_hours_ignores_unrelated_numbers_in_body():
    raw = (
        "Feed post\nJane Doe\nFounder @ Acme | Grow your LinkedIn in 60 Days\n"
        "6h\n"
        "We just crossed 10,000 customers, 3,000 followers and a 5 min read.\n"
    )
    assert post_age_hours(raw) == 6.0


def test_post_age_hours_returns_none_when_no_timestamp_found():
    raw = "Jane Doe\nFounder @ Acme\nWe just crossed 10,000 customers today."
    assert post_age_hours(raw) is None


def test_post_connection_degree_detects_first_degree():
    raw = "Jane Doe\n· 1st\nFounder @ Acme\n6h\nBody text here."
    assert post_connection_degree(raw) == 1


def test_post_connection_degree_detects_second_and_third():
    assert post_connection_degree("Jane Doe\n2nd\nHeadline\nBody") == 2
    assert post_connection_degree("Jane Doe\n3rd\nHeadline\nBody") == 3


def test_post_connection_degree_defaults_to_lowest_priority_when_missing():
    raw = "Feed post\nSuggested\nJane Doe\nFounder @ Acme\nBody text here."
    assert post_connection_degree(raw) == 4
