from concert_scout import (
    classify_event, deduplicate, exact_artist_match, is_excluded_event,
    known_artists_in_title, normalize_artist, parse_args, price_text,
    report_sort_key, ReportEvent,
)

ARTISTS = ["Ms. Lauryn Hill", "D'Angelo", "India.Arie"]


def event(name="A Concert", event_id="1", genre="R&B", attractions=None):
    return {
        "id": event_id,
        "name": name,
        "classifications": [{"segment": {"name": "Music"}, "genre": {"name": genre}}],
        "_embedded": {"attractions": attractions if attractions is not None else [{"name": name}]},
    }


def test_artist_name_normalization():
    assert normalize_artist("  D’Angelo ") == "d angelo"
    assert normalize_artist("India.Arie") == "india arie"
    assert normalize_artist("R&B") == "r and b"


def test_exact_artist_matching():
    assert exact_artist_match(["Guest", "MS. LAURYN HILL"], ARTISTS) == "Ms. Lauryn Hill"
    assert exact_artist_match(["Lauryn Hill Experience"], ARTISTS) is None


def test_tribute_act_exclusion():
    assert is_excluded_event(event("A Tribute to Sade"))
    assert is_excluded_event(event("The Prince Experience", attractions=[]))
    assert not is_excluded_event(event("Sade with Unknown Opener"))


def test_deduplication():
    assert [e["id"] for e in deduplicate([event(event_id="a"), event(event_id="a"), event(event_id="b")])] == ["a", "b"]


def test_match_classification():
    exact = event("Ms Lauryn Hill Live", attractions=[{"name": "Ms. Lauryn Hill"}])
    assert classify_event(exact, ARTISTS)[0] == "MUST SEE"
    assert classify_event(event("Soul Night", genre="Soul"), ARTISTS)[0] == "STRONG MATCH"
    liked = event("Favorite Live", genre="Alternative", attractions=[{"name": "Favorite Artist"}])
    assert classify_event(liked, ARTISTS, ["Favorite Artist"])[0] == "STRONG MATCH"
    assert classify_event(event("Reggae Artist", genre="Reggae"), ARTISTS)[0] == "DISCOVERY"
    assert classify_event(event("Unknown Indie Artist", genre="Alternative"), ARTISTS) is None


def test_missing_price_handling():
    assert price_text(event()) == "Price not published"


def test_local_calendar_artist_detection_is_exact():
    assert known_artists_in_title("Jazmine Sullivan Live", ["Jazmine Sullivan"]) == ["Jazmine Sullivan"]
    assert known_artists_in_title("Jill Scott with Guests", ["Jill Scott"]) == ["Jill Scott"]
    assert known_artists_in_title("Scott Bradlee's Postmodern Jukebox", ["Jill Scott"]) == []


def test_profile_command_line_arguments():
    args = parse_args(["--config", "profiles/husband.json", "--state", "data/husband.json",
                       "--recipient-env", "HUSBAND_ALERT_EMAIL_TO"])
    assert args.config == "profiles/husband.json"
    assert args.state == "data/husband.json"
    assert args.recipient_env == "HUSBAND_ALERT_EMAIL_TO"


def test_preferred_cheap_venue_sorting():
    cheap = event("Favorite", event_id="cheap")
    cheap["priceRanges"] = [{"min": 25, "max": 30}]
    cheap["_embedded"]["venues"] = [{"name": "The Other Side"}]
    expensive = event("Favorite", event_id="expensive")
    expensive["priceRanges"] = [{"min": 100, "max": 150}]
    expensive["_embedded"]["venues"] = [{"name": "Arena"}]
    config = {"preferred_venues": ["The Other Side"], "prefer_low_prices": True}
    cheap_report = ReportEvent(cheap, "MUST SEE", "", "", 10, "America/Chicago")
    expensive_report = ReportEvent(expensive, "MUST SEE", "", "", 5, "America/Chicago")
    assert report_sort_key(cheap_report, config) < report_sort_key(expensive_report, config)
