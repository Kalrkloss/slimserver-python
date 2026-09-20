"""Senderlogos fuer ALLE Playlist-Eintraege (Perl ``remote_image_$url``).

Perl files a stream's station logo under the URL the play STARTS from and reads
it back with the URL of the **playlist entry** it is handed
(``Slim/Control/Queries.pm:4386`` ``Playlist::track`` → ``_songData`` →
``Slim/Player/Protocols/HTTP.pm:1092`` ``$cache->get("remote_image_$url")``).
When the entry's URL has been resolved (redirect / M3U), Perl carries the icon
onto the canonical final URL (``Slim/Utils/Scanner/Remote.pm:307-308``), so the
logo stays reachable for every entry — not only for the running one.

These tests pin the port's two halves of that rule:

* ``api._carry_stream_image_across_redirect`` (the redirect alias) and
* ``api._stream_artwork_url`` (the lookup the status/playlist render uses),

plus the two places a logo is filed: a rendered feed row
(``Slim/Control/XMLBrowser.pm:1043-1049``) and ``$favs->icon($url)``
(``Slim/Plugin/Favorites/OpmlFavorites.pm:83-88``).
"""

from __future__ import annotations

from types import SimpleNamespace

from lyrion.music.favorites import FAVORITES_ICON, STREAM_ICON, favorite_icon
from lyrion.web.api import (
    _carry_stream_image_across_redirect,
    _register_feed_row_images,
    _registered_stream_image,
    _stream_artwork_url,
)

ENTRY_A = "http://hirschmilch.de:7000/chillout.mp3"
ENTRY_B_ORIGINAL = ("http://opml.radiotime.com/Tune.ashx?id=s355203"
                    "&partnerId=16&serial=deadbeef")
ENTRY_B_RESOLVED = "https://fr2.1mix.co.uk:8000/320h"
LOGO_A = "/imageproxy/http%3A%2F%2Fcdn-radiotime-logos.tunein.com%2Fs111987q.png/image.png"
LOGO_B = "/imageproxy/http%3A%2F%2Fcdn-profiles.tunein.com%2Fs355203%2Fimages%2Flogoq.jpg/image.jpg"


def _player() -> SimpleNamespace:
    return SimpleNamespace(stream_images={ENTRY_A: LOGO_A,
                                          ENTRY_B_ORIGINAL: LOGO_B},
                           stream_titles={}, current_url="")


def test_carry_keeps_original_and_files_alias() -> None:
    """``Remote.pm:307-308`` — the icon is COPIED, the original key survives."""
    player = _player()
    _carry_stream_image_across_redirect(player, ENTRY_B_ORIGINAL,
                                        ENTRY_B_RESOLVED)
    assert player.stream_images[ENTRY_B_ORIGINAL] == LOGO_B
    assert player.stream_images[ENTRY_B_RESOLVED] == LOGO_B


def test_carry_does_not_overwrite_a_known_logo() -> None:
    """Perl only sets the alias when the canonical URL has no entry yet."""
    player = _player()
    player.stream_images[ENTRY_B_RESOLVED] = "html/images/radio.png"
    _carry_stream_image_across_redirect(player, ENTRY_B_ORIGINAL,
                                        ENTRY_B_RESOLVED)
    assert player.stream_images[ENTRY_B_RESOLVED] == "html/images/radio.png"


def test_every_entry_keeps_its_logo_after_the_url_was_resolved() -> None:
    """Entry A (untouched) and entry B (rewritten) both answer their logo."""
    player = _player()
    # The strm send rewrote entry B to the resolved URL and carried the icon.
    playlist = [ENTRY_A, ENTRY_B_RESOLVED]
    _carry_stream_image_across_redirect(player, ENTRY_B_ORIGINAL,
                                        ENTRY_B_RESOLVED)
    assert _stream_artwork_url(player, playlist[0]) == LOGO_A
    assert _stream_artwork_url(player, playlist[1]) == LOGO_B


def test_lookup_without_registration_stays_empty() -> None:
    """No ``remote_image_$url`` ⇒ no artwork (the caller keeps its default)."""
    player = _player()
    assert _stream_artwork_url(player, "http://unknown.invalid/live.mp3") == ""


def test_registered_stream_image_is_server_wide(monkeypatch) -> None:
    """Perl's cache is one key per URL — not per player (``Info.pm:487``)."""
    owner = _player()
    other = SimpleNamespace(stream_images={}, stream_titles={})

    class _PM:
        def get_all_players(self):
            return [owner]

    monkeypatch.setattr("lyrion.player.manager.PlayerManager", _PM)
    assert _registered_stream_image(other, ENTRY_A) == LOGO_A
    assert _registered_stream_image(None, ENTRY_A) == LOGO_A
    assert _registered_stream_image(other, "http://nope.invalid/x") == ""


def test_favorite_icon_derives_from_registered_stream_image() -> None:
    """``$favs->icon($url)`` = handler logo first, placeholder only without one."""
    player = _player()
    assert favorite_icon(ENTRY_A, player) == LOGO_A
    # A stream nobody filed a logo for keeps the HTTP handler's placeholder
    # (``HTTP.pm:1138-1148``), a folder/no URL the favourites one (:87).
    assert favorite_icon("http://unknown.invalid/live.mp3", player) == STREAM_ICON
    assert favorite_icon(None, player) == FAVORITES_ICON
    assert favorite_icon("file:///music/x.flac", player) == FAVORITES_ICON


def test_feed_rows_file_their_logo_under_the_row_url() -> None:
    """``XMLBrowser.pm:1043-1049`` — the rendered row's image is cached."""
    player = SimpleNamespace(stream_images={})
    rows = [
        {"type": "audio", "text": "1Mix Radio",
         "icon": "/imageproxy/http%3A%2F%2Fcdn-profiles.tunein.com%2Fs355203%2Fimage.jpg",
         "presetParams": {"favorites_url": ENTRY_B_ORIGINAL,
                          "icon": "http://cdn-profiles.tunein.com/s355203/image.jpg"}},
        {"type": "audio", "text": "Ohne Logo",
         "presetParams": {"favorites_url": "http://streams.invalid/nologo",
                          "icon": ""}},
        {"type": "link", "text": "Ordner",
         "actions": {"go": {"params": {"item_id": "abc.0"}}}},
    ]
    _register_feed_row_images(player, rows)
    assert _registered_stream_image(player, ENTRY_B_ORIGINAL).endswith(
        "image.jpg")
    # A row without a logo files nothing — the placeholder stays the fallback.
    assert _registered_stream_image(player, "http://streams.invalid/nologo") == ""
    assert player.stream_images == {ENTRY_B_ORIGINAL:
                                    player.stream_images[ENTRY_B_ORIGINAL]}
