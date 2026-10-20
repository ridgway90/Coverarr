from coverarr.clients.jellyfin import provider_ids
from coverarr.clients.plex import parse_plex_guids
from coverarr.security import hash_password, verify_password
from coverarr.services.matching import normalize_collection_name, normalize_title
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
