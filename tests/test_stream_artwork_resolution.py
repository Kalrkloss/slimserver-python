"""The big (Now-Playing) logo of a stream resolved from a feed row.

Symptom (user, verbatim): „1mix aus favoriten hat ein kleines logo aber kein
großes." — the row in the Favourites list showed the station logo, the large
artwork of the Now-Playing window did not.

Measured cause (live 2026-09-20, our dev server + Perl 9.1.1 read-only):

* ``favorites playlist play item_id:28`` plays the TuneIn playlist URL
  ``http://opml.radiotime.com/Tune.ashx?id=s355203&…&partnerId=15…``; the scan
  resolves it to ``https://fr2.1mix.co.uk:8000/320h`` and
  ``networking/protocol.py:3044-3053`` rewrites the playlist entry to that
  resolved URL.
* Our status then answered ``"artwork_url": "html/images/radio.png"`` (the
  placeholder) although the row logo was registered — the lookup used the
  RESOLVED url while the registration sits under the row's URL.
* Perl reports the row logo for the very same state::

      status - 1 tags:galdKJcjou remoteMeta:1
      remoteMeta.artwork_url = "/imageproxy/http%3A%2F%2Fcdn-profiles.tunein\
.com%2Fs355203%2Fimages%2Flogoq.jpg%3Ft%3D1/image.jpg"
      playlist_loop[0].url    = "https://fr2.1mix.co.uk:8000/320h"

Perl's rule (why the entry URL decides):

* ``Slim/Music/Info.pm:485-489`` — ``setRemoteMetadata`` caches the logo under
  the URL the play starts from; the feed renderer caches it there too
  (``Slim/Control/XMLBrowser.pm:1043-1049``).
* ``Slim/Player/Protocols/HTTP.pm:1092`` — ``getMetadataFor`` opens with
  ``my $cover = $cache->get("remote_image_$url")`` and resolves a playlist URL
  only afterwards, for the icon branch (``:1095-1098``).
* ``Slim/Control/Queries.pm:4386`` — ``_songData`` gets the PLAYLIST ENTRY
  (``Playlist::track($client, $playlist_cur_index)``); the swap to the playing
  track happens later (``:5942-5949``) and changes the reported ``url`` only.
* ``Slim/Utils/Scanner/Remote.pm:307-308`` — the icon is carried across a
  redirect (``$cache->set("remote_image_" . $request->uri…, $icon)``).
* ``Slim/Web/ImageProxy.pm:407-425`` — ``proxiedImage`` wraps an external URL,
  a skin-relative path stays as it is.

SqueezePlay's request is built from the status item (``icon-id``/``icon``,
``NowPlayingApplet.lua:107-114`` → ``SlimServer.lua:1172-1203`` appends
``_180x180_m``): with the fix the client asks for
``/imageproxy/http%3A//cdn-profiles.tunein.com/s355203/images/logoq.jpg%3Ft%3D1/image_180x180_m.jpg``
and the server answers 200 with a real 180x180 image (measured live).
"""

from __future__ import annotations

import asyncio

import pytest

from lyrion.player.manager import PlayerManager
from lyrion.player.state import PlayerState
from lyrion.web.api import (JSONRPCAPI, RADIO_PLACEHOLDER_ICON,
                            jive_now_playing_display)

MAC = "1c:87:2c:47:fc:36"
# The favourite's URL (favorites.opml) and the URL the scan resolves it to.
ENTRY_URL = ("http://opml.radiotime.com/Tune.ashx?id=s355203&formats=aac,ogg,"
             "mp3,wmpro,wma,wmvoice&partnerId=15&serial="
             "89d36ef4510ad7d85b912b3d0fee8bad")
RESOLVED_URL = "https://fr2.1mix.co.uk:8000/320h"
STATION = "1Mix Radio EDM Stream"
RAW_TUNEIN = "http://cdn-profiles.tunein.com/s355203/images/logoq.jpg?t=1"
PROXIED_TUNEIN = ("/imageproxy/http%3A%2F%2Fcdn-profiles.tunein.com%2Fs355203"
                  "%2Fimages%2Flogoq.jpg%3Ft%3D1/image.jpg")

TAGS = ["-", 1, "tags:galdKJcjou", "remoteMeta:1"]


@pytest.fixture(autouse=True)
def _isolated_players():
    prev = getattr(PlayerManager, "_instance", None)
    pm = object.__new__(PlayerManager)
    pm._initialized = True
    pm.players = {}
    pm._protocol_handler = None
    PlayerManager._instance = pm
    yield
    PlayerManager._instance = prev


class _PM:
    def __init__(self, player: PlayerState) -> None:
        self._player = player

    def get_all_players(self):
        return [self._player]

    def get_player(self, mac):
        return self._player if mac in (self._player.mac, None) else None


def _status(player: PlayerState, args: list) -> dict:
    api = JSONRPCAPI()

    async def load(_ids):
        return {}

    async def run():
        api._load_tracks = load            # type: ignore[method-assign]
        return await api._json_player_status(_PM(player), MAC,
                                             [str(a) for a in args])

    return asyncio.run(run())


def _playing_stream(start_url: str, image: str, title: str = STATION,
                    resolved: str = RESOLVED_URL) -> PlayerState:
    """A player whose playlist entry was rewritten to ``resolved``.

    Mirrors the live order: the registration happens for the row's URL through
    the very helpers ``favorites.play`` uses (``_set_stream_title`` /
    ``_set_stream_image``, ``music/favorites.py:374-375``) and
    ``networking/protocol.py:3044-3053`` replaces the playlist entry with the
    resolved URL afterwards.  ``stream_baseline_title`` is the name the play
    started with (``PlayerState.begin_stream`` / ``manager.play_url:1494``).
    """
    player = PlayerState(mac=MAC, name="Taverne", ip="192.168.1.130",
                         port=43856)
    player.power = True
    player.mode = "play"
    player.playlist = [resolved]
    player.playlist_position = 0
    player.playlist_total = 1
    player.current_url = resolved
    player.remote = 1
    player.stream_baseline_title = title
    if title:
        JSONRPCAPI._set_stream_title(player, start_url, title)
    if image:
        JSONRPCAPI._set_stream_image(player, start_url, image)
    pm = object.__new__(PlayerManager)
    pm._initialized = True
    pm.players = {MAC.upper().replace(":", ""): player}
    pm._protocol_handler = None
    PlayerManager._instance = pm
    return player


def test_resolved_stream_reports_the_logo_registered_for_the_entry_url():
    """Perl live 2026-09-20 (same state): the row logo, proxied, not the
    placeholder — the playlist entry URL decided the lookup."""
    player = _playing_stream(ENTRY_URL, RAW_TUNEIN)
    res = _status(player, TAGS)
    meta = res["remoteMeta"]
    assert meta["artwork_url"] == PROXIED_TUNEIN, meta
    # Perl publishes the RESOLVED url, exactly like this port does.
    assert meta["url"] == RESOLVED_URL, meta
    item = res["playlist_loop"][0]
    assert item["artwork_url"] == PROXIED_TUNEIN, item
    # ``_addJiveSong`` (Queries.pm:5625-5627): a defined artwork_url becomes
    # the jive ``icon`` — the field SqueezePlay's Now-Playing reads.
    assert item["icon"] == PROXIED_TUNEIN, item
    assert "icon-id" not in item, item


def test_menu_item_and_display_block_carry_the_same_logo():
    """The jive item (``menu:menu``) and the displaystatus block are the two
    other paths the Now-Playing artwork reaches the client through
    (``Queries.pm:5625-5633`` resp. ``Player.pm:584-631``)."""
    player = _playing_stream(ENTRY_URL, RAW_TUNEIN)
    res = _status(player, ["-", 1, "menu:menu", "useContextMenu:1"])
    assert res["item_loop"][0]["icon"] == PROXIED_TUNEIN, res["item_loop"][0]
    block = asyncio.run(jive_now_playing_display(player))
    assert block["icon"] == PROXIED_TUNEIN, block


def test_skin_relative_logo_is_not_wrapped_in_the_imageproxy():
    """The 1.FM favourite's ``html/images/favorites.png``: ``proxiedImage``
    only wraps ``^https?:`` (``ImageProxy.pm:447``) — the skin-relative path is
    published unchanged (live, the 1.FM stream: ``"artwork_url":
    "images/favorites.png"``; the ``html/`` prefix is dropped because our static
    root already is ``html/``)."""
    player = _playing_stream("http://strm112.1.fm/ambientpsy_mobile_mp3",
                             "html/images/favorites.png",
                             title="1.FM - Ambient Psychill",
                             resolved="http://strm112.1.fm/ambientpsy_mobile_mp3")
    meta = _status(player, TAGS)["remoteMeta"]
    assert meta["artwork_url"] == "images/favorites.png", meta
    assert _status(player, TAGS)["playlist_loop"][0]["icon"] == \
        "images/favorites.png"


def test_stream_started_without_a_registration_keeps_the_placeholder():
    """A bare ``playlist play <url>`` registers neither name nor logo, so the
    PREVIOUS station's registration must not leak into it (Perl: no
    ``remote_image_$url`` ⇒ ``html/images/radio.png``, ``HTTP.pm:1136-1147``)."""
    player = _playing_stream(ENTRY_URL, RAW_TUNEIN)
    # The next stream: a plain URL, no registration, baseline title = the URL.
    player.stream_baseline_title = RESOLVED_URL
    player.playlist = ["http://other.example/stream.mp3"]
    player.current_url = "http://other.example/stream.mp3"
    meta = _status(player, TAGS)["remoteMeta"]
    assert meta["artwork_url"] == "html/images/radio.png", meta
    item = _status(player, TAGS)["playlist_loop"][0]
    assert item["icon-id"] == RADIO_PLACEHOLDER_ICON, item


def test_most_recent_registration_wins_for_the_running_stream():
    """Two rows can carry the same station name (the live port has two
    ``1Mix Radio EDM Stream`` favourites): the registration written
    immediately before the play is the one that belongs to the running
    stream."""
    player = _playing_stream(ENTRY_URL, "html/images/favorites.png")
    other = ENTRY_URL.replace("partnerId=15", "partnerId=16")
    JSONRPCAPI._set_stream_title(player, other, STATION)
    JSONRPCAPI._set_stream_image(player, other, RAW_TUNEIN)
    meta = _status(player, TAGS)["remoteMeta"]
    assert meta["artwork_url"] == PROXIED_TUNEIN, meta
