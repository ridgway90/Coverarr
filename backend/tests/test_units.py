from coverarr.clients.jellyfin import provider_ids
from coverarr.clients.plex import parse_plex_guids
from coverarr.security import hash_password, verify_password
from coverarr.services.files import file_hints, file_key, show_folder
from coverarr.services.matching import normalize_collection_name, normalize_title
from coverarr.services.mismatch import pick_suggestion, suggest
from coverarr.services.posters import hamming, poster_hash
from tests.fakeserver import poster_png


def test_plex_guids_new_agent():
    meta = {"guid": "plex://movie/5d77", "Guid": [{"id": "imdb://tt1"}, {"id": "tmdb://2"}]}
    assert parse_plex_guids(meta) == {"imdb": "tt1", "tmdb": "2"}


def test_plex_guids_legacy_agents():
    assert parse_plex_guids({"guid": "com.plexapp.agents.imdb://tt0111161?lang=en"}) == {
        "imdb": "tt0111161"
    }
    assert parse_plex_guids({"guid": "com.plexapp.agents.thetvdb://75592/1/2?lang=en"}) == {
        "tvdb": "75592"
    }
    assert parse_plex_guids({"guid": "local://123"}) == {}


def test_jellyfin_provider_ids_case_insensitive():
    assert provider_ids({"ProviderIds": {"Tmdb": "1", "IMDB": "tt2", "Tvdb": ""}}) == {
        "tmdb": "1",
        "imdb": "tt2",
    }


def test_normalisation():
    assert normalize_title("The World's End") == normalize_title("World’s End")
    assert normalize_title("Fast & Furious") == "fast and furious"
    assert normalize_collection_name("The Cornetto Collection") == "cornetto"
    assert normalize_collection_name("Cornetto") == "cornetto"


def test_password_hashing():
    encoded = hash_password("s3cret-pass")
    assert verify_password("s3cret-pass", encoded)
    assert not verify_password("wrong", encoded)
    assert not verify_password("x", "garbage")


def test_poster_hash_ignores_overlay_bands_but_not_different_art():
    clean = poster_hash(poster_png("movie:Hot Fuzz"))
    overlaid = poster_hash(poster_png("movie:Hot Fuzz", overlay=True))
    resized = poster_hash(poster_png("movie:Hot Fuzz", width=200))
    other = poster_hash(poster_png("movie:Hot Fuzz", alt=True))
    assert hamming(clean, overlaid) <= 10
    assert hamming(clean, resized) <= 6
    assert hamming(clean, other) > 10


REAL_PATH = (
    "/mnt/media/Movies/2004/9 Songs (2004) {tmdb-27}/"
    "9 Songs (2004) {tmdb-27} [WEBDL-2160p][EAC3 5.1][h265]-PiRaTeS.mkv"
)


def test_file_keys_ignore_mount_points():
    other_mount = REAL_PATH.replace("/mnt/media/Movies/2004", "/data/movies")
    assert file_key(REAL_PATH, "movie") == file_key(other_mount, "movie")
    assert file_key("D:\\TV\\Spaced", "show") == file_key("/tv/Spaced", "show") == "spaced"


def test_file_hints():
    hints = file_hints(REAL_PATH, "movie")
    assert (hints.tmdb_id, hints.title, hints.year) == ("27", "9 Songs", 2004)
    assert file_hints("/m/Arrival (2016).mkv", "movie").title == "Arrival"
    assert file_hints("/tv/Black Books (2000) [tvdbid-72216]", "show").tvdb_id == "72216"
    assert show_folder("/tv/Spaced/Season 01/Spaced - S01E01.mkv") == "/tv/Spaced"
    assert show_folder("/tv/Spaced/Specials/x.mkv") == "/tv/Spaced"
    assert show_folder("/tv/Spaced/x.mkv") == "/tv/Spaced"


def _candidates():
    return [
        {"media_item_id": 1, "title": "9 Songs", "year": 2004, "tmdb_id": "27", "servers": [1]},
        {"media_item_id": 2, "title": "9", "year": 2009, "tmdb_id": "12244", "servers": [2]},
    ]


def test_suggestion_follows_file_name_but_explains_why():
    # Two servers disagree and the file name names one of them: suggest it, with reasons.
    # (It can still be wrong if the file itself is misnamed, which is why fixes need confirming.)
    cands = _candidates()
    hints = {"tmdb_id": "27", "title": "9 Songs", "year": 2004}
    suggest(cands, hints, total_servers=2)
    assert pick_suggestion(cands) == (1, "high")
    assert cands[0]["reasons"][0] == "File name says {tmdb-27}"
    assert cands[1]["reasons"] == ["File name says {tmdb-27}, not this"]


def test_no_suggestion_when_nothing_decides():
    cands = _candidates()
    suggest(cands, {}, total_servers=2)
    assert pick_suggestion(cands) == (None, "none")
