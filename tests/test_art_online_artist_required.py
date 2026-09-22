"""Der Interpret ist Pflicht — UAS-Vorbild ``albumuniversal.xml:6-10``.

Anlass (live belegt, Bibliothek des Anwenders): viele Alben mit „Greatest Hits“
im Namen bekamen das Cover von Bob Marley - Greatest Hits.  Die Ursache war
nicht die Suchphrase allein, sondern die **Auslieferung** über die Album-Spalten
(``art_online.py`` ``ArtOnlineCache.find_by_album_sync``): sie verglich nur den
Titel und nahm den ersten Treffer, sodass ``albums.id=6794`` (Björk, „Greatest
Hits“, 2002) die Datei ``f87a0f981d73159ae138.jpg`` des Bob-Marley-Eintrags
(MBID ``035d9663-6997-32d5-abcb-3ba834f6d823``) bekam.  Dieselbe Titelsuche
hätte auch in der Leiter jeden Interpreten zugelassen, wenn das Album keinen
trägt.

Regeln, die hier festgehalten werden:

* Jede Suchstufe trägt die Interpreten-Klausel (UAS' Phrase); entspannt wird
  nur der *Titel*, nie der Interpret.
* Der Treffer muss den Interpreten tragen (Vergleichsform ohne führenden
  Artikel/Leerzeichen — dieselbe Bildung wie der Sortierschlüssel des Imports,
  ``media/importer.py:91-98``).
* Mehrdeutige Fälle (mehrere Interpreten zum Titel, keiner davon exakt) sind ein
  ``miss`` **mit Grund**, kein Ratetreffer.
* Lieber kein Cover (Perl-Platzhalter, ``Slim/Web/Graphics.pm:275-291``) als ein
  fremdes.

Kein Live-Netz: die MusicBrainz-Antworten sind auf die entscheidenden Treffer
gekürzte, echte Antworten auf ``release:"Greatest Hits"`` bzw.
``release:"Point Blank" AND artist:"Bonfire"``.
"""

from __future__ import annotations

import asyncio
import io
import json

import pytest
from PIL import Image

from lyrion.media import art_online
from lyrion.media.art_online import (
    AlbumQuery,
    ArtOnlineService,
    ArtOnlineSettings,
    Candidate,
    FetchResponse,
    ProviderError,
    candidate_matches,
    mb_query_variants,
    search_musicbrainz_detailed,
    select_candidate,
)

ARTIST_CLAUSE_BOB = 'artistname:"Bob Marley" OR artist:"Bob Marley"'

#: Echte MusicBrainz-Antwort (gekürzt) auf ``release:"Greatest Hits"``: der
#: Bob-Marley-Eintrag steht vorne, dazu drei weitere Interpreten desselben Titels.
MB_GREATEST_HITS = {
    "count": 4,
    "releases": [
        {"id": "3b3b3b3b-0000-4000-8000-000000000001", "score": 100,
         "title": "Greatest Hits", "date": None,
         "artist-credit": [{"name": "Bob Marley",
                            "artist": {"id": "x1", "name": "Bob Marley"}}],
         "release-group": {"id": "035d9663-6997-32d5-abcb-3ba834f6d823",
                           "title": "Greatest Hits", "primary-type": "Album",
                           "first-release-date": None}},
        {"id": "3b3b3b3b-0000-4000-8000-000000000002", "score": 100,
         "title": "Greatest Hits", "date": "1992",
         "artist-credit": [{"name": "The Police",
                            "artist": {"id": "x2", "name": "The Police"}}],
         "release-group": {"id": "5a614d6e-274a-3cc8-8e03-65cdd86b964f",
                           "title": "Greatest Hits", "primary-type": "Album",
                           "first-release-date": None}},
        {"id": "3b3b3b3b-0000-4000-8000-000000000003", "score": 100,
         "title": "Greatest Hits", "date": "1971",
         "artist-credit": [{"name": "Fleetwood Mac",
                            "artist": {"id": "x3", "name": "Fleetwood Mac"}}],
         "release-group": {"id": "8808041f-3e82-36d1-a62c-212496daa321",
                           "title": "Greatest Hits", "primary-type": "Album",
                           "first-release-date": None}},
        {"id": "3b3b3b3b-0000-4000-8000-000000000004", "score": 100,
         "title": "Greatest Hits", "date": "2002",
         "artist-credit": [{"name": "Björk",
                            "artist": {"id": "x4", "name": "Björk"}}],
         "release-group": {"id": "07d2f772-531d-3d19-920a-72337891800d",
                           "title": "Greatest Hits", "primary-type": "Album",
                           "first-release-date": None}},
    ],
}


def jpeg_bytes(width: int = 500, height: int = 500) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (width, height), (30, 50, 70)).save(buf, "JPEG", quality=80)
    return buf.getvalue()


class FakeFetcher:
    """Ersetzt ``HttpFetcher``: MusicBrainz-Antwort je Suchterm, CAA nur für
    erlaubte Release-Group-MBIDs (sonst 404 wie das echte Archiv)."""

    def __init__(self, payload: dict | None = None,
                 caa_mbids: tuple[str, ...] = ()) -> None:
        self.payload = payload if payload is not None else MB_GREATEST_HITS
        self.caa_mbids = caa_mbids
        self.queries: list[str] = []
        self.caa_urls: list[str] = []

    async def get(self, url: str, params=None) -> FetchResponse:
        query = str((params or {}).get("query") or "")
        if "musicbrainz.org" in url:
            self.queries.append(query)
            # Wie MusicBrainz: die Phrase filtert; wir liefern die Trefferliste
            # trotzdem immer (der Test prüft die Prüfung am Kandidaten).
            return FetchResponse(status=200, content=json.dumps(self.payload).encode(),
                                 headers={"Content-Type": "application/json"})
        if "coverartarchive.org" in url:
            self.caa_urls.append(url)
            if any(mbid in url for mbid in self.caa_mbids):
                return FetchResponse(status=200, content=jpeg_bytes(),
                                     headers={"Content-Type": "image/jpeg"})
            return FetchResponse(status=404, content=b"{}",
                                 headers={"Content-Type": "application/json"})
        raise ProviderError(f"unerwartete URL im Test: {url}")

    async def close(self) -> None:
        pass


def build(tmp_path, payload: dict | None = None,
          caa_mbids: tuple[str, ...] = (), *,
          providers: tuple[str, ...] = ("musicbrainz",)):
    settings = ArtOnlineSettings(cache_dir=tmp_path / "cache", providers=providers)
    fetch = FakeFetcher(payload, caa_mbids)
    return ArtOnlineService(settings, fetcher=fetch,
                            db_path=tmp_path / "cache" / "art_online.db"), fetch


# ---------------------------------------------------------------------------
# 1. Die Leiter entspannt nur den Titel
# ---------------------------------------------------------------------------


def test_every_search_stage_carries_the_artist():
    """UAS' Phrase: ``release:"A" AND (artistname:"B" OR artist:"B")`` — immer."""
    stages = mb_query_variants(AlbumQuery(album="Greatest Hits", artist="Bob Marley"))
    assert stages == [f'release:"Greatest Hits" AND ({ARTIST_CLAUSE_BOB})']
    assert all("release:" in s and "artistname:" in s for s in stages)

    # Klammer-Zusatz: Stufe 2 entspannt den Titel, der Interpret bleibt.
    stages = mb_query_variants(
        AlbumQuery(album="Point Blank (MMXXIII Version)", artist="Bonfire"))
    assert stages[0] == ('release:"Point Blank (MMXXIII Version)" '
                         'AND (artistname:"Bonfire" OR artist:"Bonfire")')
    assert stages[1] == ('release:"Point Blank" '
                         'AND (artistname:"Bonfire" OR artist:"Bonfire")')
    assert all('artistname:"Bonfire"' in s for s in stages), stages

    # „feat.“-Kürzung (albumuniversal.xml:11-20) kürzt nur den Interpreten,
    # aber sie lässt ihn nie weg.
    stages = mb_query_variants(
        AlbumQuery(album="Knightclub", artist="Feuerschwanz feat. Dag Sdp"))
    assert any('artistname:"Feuerschwanz" OR artist:"Feuerschwanz"' in s
               for s in stages), stages
    assert all("artistname:" in s for s in stages), stages


def test_album_without_an_artist_gets_no_hit(tmp_path):
    """Ohne Interpreten ist der Titel allein kein Schlüssel — kein Ratetreffer."""
    assert mb_query_variants(AlbumQuery(album="Greatest Hits")) == [
        'release:"Greatest Hits"']
    bob = Candidate(title="Greatest Hits", group_title="Greatest Hits",
                    artist="Bob Marley", release_group_id="035d9663")
    assert candidate_matches(bob, AlbumQuery(album="Greatest Hits")) is False

    svc, _fetch = build(tmp_path)
    query = AlbumQuery(album="Greatest Hits")
    assert asyncio.run(svc.find_cover(query)) is None
    entry = svc.cache.read_sync(query.key())
    assert entry is not None and entry.status == "miss"
    assert art_online._failure_is_definitive(entry.reason), entry.reason


# ---------------------------------------------------------------------------
# 2. Der Treffer muss den Interpreten tragen (Bob-Marley-Fall)
# ---------------------------------------------------------------------------


def test_foreign_artist_never_wins_for_an_ambiguous_title(tmp_path):
    """„Greatest Hits“ von Björk bekommt Björks Eintrag — nie Bob Marleys."""
    svc, fetch = build(tmp_path, caa_mbids=("07d2f772-531d-3d19-920a-72337891800d",),
                       providers=("musicbrainz", "coverartarchive"))
    query = AlbumQuery(album="Greatest Hits", artist="Björk", year=2002)
    result = asyncio.run(svc.find_cover(query))

    # Die Abfrage verlangte den Interpreten (roh).
    assert fetch.queries == [
        'release:"Greatest Hits" AND (artistname:"Björk" OR artist:"Björk")']
    # Der akzeptierte Treffer ist Björks Release-Group, nicht Bob Marleys.
    assert result is not None
    assert result.mbid == "07d2f772-531d-3d19-920a-72337891800d"
    assert "035d9663-6997-32d5-abcb-3ba834f6d823" not in result.source_url
    assert all("035d9663-6997-32d5-abcb-3ba834f6d823" not in u
               for u in fetch.caa_urls), fetch.caa_urls
    entry = svc.cache.read_sync(query.key())
    assert entry is not None and entry.status == "hit"
    assert entry.mbid == "07d2f772-531d-3d19-920a-72337891800d"


def test_no_cover_when_the_catalogue_only_knows_foreign_artists(tmp_path):
    """Nur fremde Interpreten in der Antwort → ``miss`` mit Grund, kein Bild."""
    only_bob = {"count": 1, "releases": [MB_GREATEST_HITS["releases"][0]]}
    svc, fetch = build(tmp_path, only_bob,
                       caa_mbids=("035d9663-6997-32d5-abcb-3ba834f6d823",),
                       providers=("musicbrainz", "coverartarchive"))
    query = AlbumQuery(album="Greatest Hits", artist="Björk", year=2002)
    assert asyncio.run(svc.find_cover(query)) is None
    entry = svc.cache.read_sync(query.key())
    assert entry is not None and entry.status == "miss"
    assert "kein passender Treffer" in entry.reason, entry.reason
    assert fetch.caa_urls == [], "kein Bild-Anbieter ohne passenden Treffer"
    assert art_online._failure_is_definitive(entry.reason), entry.reason


def test_pick_candidate_prefers_the_matching_artist_of_an_ambiguous_title():
    query = AlbumQuery(album="Greatest Hits", artist="Bob Marley")
    candidates = [
        Candidate(title="Greatest Hits", group_title="Greatest Hits",
                  artist="The Police", year=1992, score=100),
        Candidate(title="Greatest Hits", group_title="Greatest Hits",
                  artist="Bob Marley", year=None, score=100),
        Candidate(title="Greatest Hits", group_title="Greatest Hits",
                  artist="Fleetwood Mac", year=1971, score=100),
    ]
    match, reason = select_candidate(candidates, query)
    assert reason == ""
    assert match is not None and match.artist == "Bob Marley"


def test_two_foreign_artists_are_ambiguous_and_become_a_miss_with_reason():
    """Mehrere plausible Kandidaten, abweichender Interpret → ``miss``, nicht raten."""
    query = AlbumQuery(album="Greatest Hits", artist="Bob Marley")
    candidates = [
        Candidate(title="Greatest Hits", group_title="Greatest Hits",
                  artist="Bob Marley Band", score=100),
        Candidate(title="Greatest Hits", group_title="Greatest Hits",
                  artist="Bob Marley Live", score=100),
    ]
    match, reason = select_candidate(candidates, query)
    assert match is None
    assert reason.startswith("mehrdeutig"), reason
    # Der Grund gehört in den Cache und gilt als definitive Absage (kein
    # Endlos-Retry, siehe ``art_online._failure_is_definitive``).
    assert art_online._failure_is_definitive(f"musicbrainz: {reason}")


def test_search_musicbrainz_reports_the_ambiguous_reason(tmp_path):
    payload = {"count": 2, "releases": [
        {"id": "a", "score": 100, "title": "Greatest Hits", "date": None,
         "artist-credit": [{"name": "Bob Marley Band"}],
         "release-group": {"id": "rg-1", "title": "Greatest Hits"}},
        {"id": "b", "score": 100, "title": "Greatest Hits", "date": None,
         "artist-credit": [{"name": "Bob Marley Live"}],
         "release-group": {"id": "rg-2", "title": "Greatest Hits"}},
    ]}
    svc, fetch = build(tmp_path, payload)
    match, hits, reason = asyncio.run(search_musicbrainz_detailed(
        fetch, svc.settings, AlbumQuery(album="Greatest Hits", artist="Bob Marley")))
    assert match is None and hits == 2
    assert reason.startswith("mehrdeutig"), reason


def test_the_police_is_not_the_cure(tmp_path):
    """Kurze Namen: ``difflib`` hält „The Police“/„The Cure“ für 75 % gleich."""
    police = Candidate(title="Greatest Hits", group_title="Greatest Hits",
                       artist="The Police", score=100)
    assert candidate_matches(police, AlbumQuery(album="Greatest Hits",
                                                artist="The Cure")) is False
    # Varianten desselben Namens bleiben erlaubt (Sortierschlüssel „police“).
    assert candidate_matches(police, AlbumQuery(album="Greatest Hits",
                                                artist="police")) is True
    assert art_online.artist_key("The Police") == art_online.artist_key("police")


# ---------------------------------------------------------------------------
# 3. Auslieferung über die Album-Spalten (der eigentliche Fehler)
# ---------------------------------------------------------------------------


def _write_cover(svc, album: str, artist: str, year=None) -> object:
    query = AlbumQuery(album=album, artist=artist, year=year)
    return svc.cache.write_cover_sync(
        query.key(), query, jpeg_bytes(500), provider="coverartarchive",
        mime="image/jpeg", source_url="https://coverartarchive.org/x",
        mbid="035d9663-6997-32d5-abcb-3ba834f6d823", width=500, height=500)


def test_cached_greatest_hits_cover_is_only_served_to_its_own_artist(tmp_path):
    """Regression: Björk, Roxette, Evergrey & Co. bekamen Bob Marleys Datei."""
    svc = ArtOnlineService(ArtOnlineSettings(cache_dir=tmp_path / "cache"))
    path = _write_cover(svc, "Greatest Hits", "Bob Marley")

    assert svc.read_cached_album("Greatest Hits", "Bob Marley") is not None
    for foreign in ("Björk", "Roxette", "Evergrey", "Levellers", "The Cure",
                    "Ace of Base", "various artists", ""):
        assert svc.read_cached_album("Greatest Hits", foreign, 2002) is None, foreign

    # Der eigene Treffer bleibt: Sortierschlüssel-Schreibweise inklusive.
    own = svc.read_cached_album("Greatest Hits", "bob marley")
    assert own is not None and own.path == path
    assert svc.read_cached_album("Greatest Hits", "The Police") is None


def test_own_entry_wins_over_an_earlier_foreign_entry(tmp_path):
    """Steht ein fremder Eintrag vorne, gewinnt trotzdem der passende."""
    svc = ArtOnlineService(ArtOnlineSettings(cache_dir=tmp_path / "cache"))
    foreign = _write_cover(svc, "Greatest Hits", "Bob Marley")
    own = _write_cover(svc, "Greatest Hits", "Fleetwood Mac", 1971)
    assert foreign != own
    result = svc.read_cached_album("Greatest Hits", "Fleetwood Mac", 1971)
    assert result is not None and result.path == own


def test_albums_with_an_unambiguous_name_keep_their_cover(tmp_path):
    """Gegenprobe: eindeutige Namen/Interpreten ändern sich nicht."""
    svc = ArtOnlineService(ArtOnlineSettings(cache_dir=tmp_path / "cache"))
    path = _write_cover(svc, "Point Blank", "Bonfire", 1989)
    for artist in ("Bonfire", "bonfire", "BONFIRE", "The Bonfire"):
        result = svc.read_cached_album("Point Blank", artist, 1989)
        assert result is not None and result.path == path
