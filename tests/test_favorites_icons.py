"""Favourite rows keep the logo their OPML entry carries.

Perl never invents an icon for a favourite: the ``icon`` attribute of the
``favorites.opml`` outline *is* the row's image, and only a missing one is
derived when the file is loaded::

    # Slim/Plugin/Favorites/OpmlFavorites.pm:133-136 (_urlindex)
    if ( !$entry->{'icon'} || … ) {
        $entry->{'icon'} = $class->icon($entry->{'URL'});
    }

    # :83-88
    sub icon {
        return Slim::Player::ProtocolHandlers->iconForURL($url)
            || 'html/images/favorites.png';
    }

That value travels to the client through

* ``XMLBrowser.pm:1160-1166`` — menu mode:
  ``$hash{'icon' . ($item->{icon} =~ /^https?:/ ? '' : '-id')} =
  proxiedImage($item->{icon})``;
* ``XMLBrowser.pm:1386-1387`` — flat/``loop_loop`` mode:
  ``$hash{image} = proxiedImage($item->{image}); $hash{image} ||=
  proxiedImage($item->{icon})``;
* ``XMLBrowser.pm:1943`` (``_favoritesParams``) — ``presetParams.icon`` takes
  the **raw** item value (``favorites_icon || image || icon || cover``), so the
  ``set-preset-*`` actions send what the OPML says;
* ``XMLBrowser.pm:1043-1049`` — a playable http row caches
  ``remote_image_$url`` from that image, which is the logo the receiver shows
  for the playing stream (``Slim/Music/Info.pm:485-489`` caches it again).

Live Perl 9.1.1 (read-only 2026-09-19, ``favorites items … item_id:<sid>.0``)
answers exactly that: the row of ``Hirschmilch Chillout`` carries
``image``/``icon-id`` = ``/imageproxy/http%3A%2F%2Fcdn-radiotime-logos.tunein
.com%2Fs111987q.png/image.png``, the folder ``favorites.png``, and the 2nd
``1Mix`` row shows the raw/proxied split (row field proxied,
``presetParams.icon`` raw http).
"""

from __future__ import annotations

import asyncio
import io
import urllib.error
import urllib.request
from contextlib import asynccontextmanager

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from lyrion.database.schema import Base, Favorite
from lyrion.music.favorites import (FAVORITES_ICON, STREAM_ICON,
                                    FavoritesManager, favorite_icon)
from lyrion.web import favorites_menu

LOGO = ("/imageproxy/http%3A%2F%2Fcdn-radiotime-logos.tunein.com%2F"
        "s111987q.png/image.png")
RAW_LOGO = "http://cdn-radiotime-logos.tunein.com/s111987q.png"
TUNEIN = ("/imageproxy/http%3A%2F%2Fcdn-profiles.tunein.com%2Fs355203%2F"
          "images%2Flogoq.jpg%3Ft%3D1/image.jpg")
RAW_TUNEIN = "http://cdn-profiles.tunein.com/s355203/images/logoq.jpg?t=1"

OPML = f"""<?xml version="1.0" encoding="UTF-8"?>
<opml version="1.0">
  <head><title>Favorites</title></head>
  <body>
    <outline text="Chill" title="Chill" icon="html/images/favorites.png">
      <outline text="Hirschmilch Chillout" title="Hirschmilch Chillout" \
URL="http://hirschmilch.de:7000/chillout.mp3" type="audio" icon="{LOGO}"/>
      <outline text="Hirschmilch Electronic" title="Hirschmilch Electronic" \
URL="http://hirschmilch.de:7000/electronic.mp3" type="audio" \
icon="html/images/favorites.png"/>
    </outline>
    <outline text="Leer" title="Leer"/>
    <outline text="1Mix" title="1Mix" URL="http://a/1" type="audio" \
icon="{RAW_TUNEIN}"/>
  </body>
</opml>
"""


def _temp_db(tmp_path, monkeypatch, opml_text: str | None = OPML):
    """Temp DB + temp OPML wired into the real code paths.

    Returns ``(session_ctx, engine)``; the caller runs the manager against it.
    """
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path/'lyrion.db'}")
    factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)

    async def _create():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    asyncio.run(_create())

    @asynccontextmanager
    async def session_ctx():
        session = factory()
        try:
            yield session
            await session.commit()
        finally:
            await session.close()

    opml = tmp_path / "favorites.opml"
    if opml_text is not None:
        opml.write_text(opml_text, encoding="utf-8")
    monkeypatch.setattr("lyrion.database.sqlite_helper.db_session", session_ctx)
    monkeypatch.setattr("lyrion.music.favorites._opml_path",
                        lambda: opml if opml_text is not None else None)
    monkeypatch.setattr("lyrion.music.favorites._opml_import_done", False)
    return session_ctx, engine


def _import_and_list():
    from lyrion.music.favorites import ensure_opml_imported

    async def run():
        await ensure_opml_imported()
        return await FavoritesManager().list_tree()

    return asyncio.run(run())


def _stored_titles(session_ctx):
    async def run():
        async with session_ctx() as session:
            rows = (await session.execute(select(Favorite))).scalars().all()
            return {r.title: r.icon for r in rows}

    return asyncio.run(run())


# ── 1. the rule itself ────────────────────────────────────────────────────


def test_icon_rule_matches_perl():
    """``OpmlFavorites.pm:83-88`` + ``HTTP.pm:1138-1148``."""
    assert favorite_icon(None) == FAVORITES_ICON
    assert favorite_icon("") == FAVORITES_ICON
    assert favorite_icon("file:///music/x.mp3") == FAVORITES_ICON
    assert favorite_icon("http://hirschmilch.de:7000/chillout.mp3") == STREAM_ICON
    assert favorite_icon("https://a/b") == STREAM_ICON


def test_row_icon_splits_external_and_relative_like_perl():
    """``XMLBrowser.pm:1160-1166`` — ``icon`` for ``http(s):``, else ``icon-id``."""
    assert favorites_menu.row_icon(FAVORITES_ICON) == {
        "icon-id": FAVORITES_ICON}
    assert favorites_menu.row_icon(LOGO) == {"icon-id": LOGO}
    # an external logo is proxied (Perl: proxiedImage) into the ``icon`` field
    assert favorites_menu.row_icon(RAW_LOGO) == {
        "icon": "/imageproxy/http%3A%2F%2Fcdn-radiotime-logos.tunein.com%2F"
                "s111987q.png/image.png"}


def test_audio_item_row_is_proxied_preset_icon_is_raw():
    """Perl's 2nd ``1Mix`` row: proxied row field, raw ``presetParams.icon``."""
    item = favorites_menu.audio_item("1Mix", "abcdef01.0", url="http://a/1",
                                     icon_id=RAW_TUNEIN)
    assert item["icon"] == TUNEIN
    assert item["presetParams"]["icon"] == RAW_TUNEIN


# ── 2. import keeps the icon ──────────────────────────────────────────────


def test_opml_import_keeps_the_icon_attribute(tmp_path, monkeypatch):
    _temp_db(tmp_path, monkeypatch)
    tree = _import_and_list()
    by_title = {f["title"]: f for f in tree}

    assert by_title["Chill"]["icon"] == FAVORITES_ICON
    children = {c["title"]: c for c in by_title["Chill"]["children"]}
    assert children["Hirschmilch Chillout"]["icon"] == LOGO
    assert children["Hirschmilch Electronic"]["icon"] == FAVORITES_ICON
    # the entry without an ``icon`` attribute keeps Perl's derived default
    assert by_title["Leer"]["icon"] == FAVORITES_ICON
    assert by_title["1Mix"]["icon"] == RAW_TUNEIN


def test_merge_backfills_a_missing_icon_and_never_overwrites(tmp_path,
                                                             monkeypatch):
    """Rows of the pre-column import carry ``icon`` NULL → filled from the OPML."""
    session_ctx, engine = _temp_db(tmp_path, monkeypatch)

    async def seed():
        async with session_ctx() as session:
            session.add_all([
                # legacy rows of the same OPML: same URL / same title+parent
                Favorite(title="Chill", url=None, position=0),
                Favorite(title="Hirschmilch Electronic",
                         url="http://hirschmilch.de:7000/electronic.mp3",
                         icon="html/images/radio.png", position=0),
            ])

    asyncio.run(seed())
    _import_and_list()
    stored = _stored_titles(session_ctx)

    assert stored["Chill"] == FAVORITES_ICON             # backfilled
    assert stored["Hirschmilch Electronic"] == "html/images/radio.png", \
        "an existing icon must never be overwritten (Perl: only !$entry->{icon})"
    assert stored["Hirschmilch Chillout"] == LOGO        # new row, icon kept
    asyncio.run(engine.dispose())


def test_rows_without_a_stored_icon_get_perls_default(tmp_path, monkeypatch):
    """A row the DB knows but the OPML does not: ``$favs->icon($url)``."""
    monkeypatch.setattr("lyrion.music.favorites._opml_path", lambda: None)
    monkeypatch.setattr("lyrion.music.favorites._opml_import_done", True)
    session_ctx, engine = _temp_db(tmp_path, monkeypatch, opml_text=None)

    async def seed():
        async with session_ctx() as session:
            session.add_all([
                Favorite(title="Ordner", url=None, position=0),
                Favorite(title="Radio", url="http://hirschmilch.de:7000/x.mp3",
                         position=1),
            ])

    asyncio.run(seed())
    items = asyncio.run(FavoritesManager().list_items())
    assert [i["icon"] for i in items] == [FAVORITES_ICON, STREAM_ICON]
    asyncio.run(engine.dispose())


# ── 3. the API ships it ───────────────────────────────────────────────────


class _Favs:
    """Minimal manager stand-in with stored icons (Perl's live OPML)."""

    TREE = {
        None: [
            {"id": 1, "title": "Chill", "url": None, "type": "folder",
             "parent_id": None, "position": 0, "icon": FAVORITES_ICON},
            {"id": 2, "title": "1Mix Radio EDM Stream",
             "url": "http://opml.radiotime.com/Tune.ashx?id=s355203",
             "type": "stream", "parent_id": None, "position": 1,
             "icon": TUNEIN},
        ],
        1: [
            {"id": 3, "title": "Hirschmilch Chillout",
             "url": "http://hirschmilch.de:7000/chillout.mp3", "type": "stream",
             "parent_id": 1, "position": 0, "icon": LOGO},
        ],
    }

    async def list_items(self, parent_id=None):
        return [dict(x) for x in self.TREE.get(parent_id, [])]

    async def resolve_path(self, path):
        return None


def _items(monkeypatch, args):
    from lyrion.web.api import JSONRPCAPI

    monkeypatch.setattr("lyrion.music.favorites.get_favorites_manager",
                        lambda: _Favs())
    return asyncio.run(JSONRPCAPI()._json_favorites_items(None, list(args)))


def test_flat_rows_carry_the_stored_logo(monkeypatch):
    """``XMLBrowser.pm:1386-1387`` — ``$hash{image} ||= proxiedImage(icon)``."""
    res = _items(monkeypatch, ["items", 0, 200])
    rows = res["loop_loop"]
    assert rows[0]["image"] == FAVORITES_ICON
    assert rows[1]["image"] == TUNEIN


def test_menu_rows_carry_the_stored_logo(monkeypatch):
    """``XMLBrowser.pm:1160-1166`` + ``_favoritesParams`` (``:1943``)."""
    res = _items(monkeypatch, ["items", 0, 200, "menu:favorites",
                               "useContextMenu:1"])
    folder, stream = res["item_loop"][0], res["item_loop"][1]
    assert folder["icon-id"] == FAVORITES_ICON
    assert stream["icon-id"] == TUNEIN
    assert stream["presetParams"]["icon"] == TUNEIN

    # the nested row (folder id 1) keeps its logo too — the ``sid.<n>`` drill
    # resolves through the real manager's list_items (see
    # tests/test_favorites_menu.py); the stub answers the bare id.
    sub = _items(monkeypatch, ["items", 0, 200, "menu:favorites",
                               "useContextMenu:1", "item_id:1"])
    assert sub["item_loop"][0]["icon-id"] == LOGO


# ── 4. playback registers the logo (Perl's ``remote_image_$url``) ─────────


def test_play_registers_title_and_logo_on_the_player(monkeypatch):
    """``XMLBrowser.pm:1043-1049`` → the status logo of the playing stream."""
    from lyrion.player.state import PlayerState

    player = PlayerState(mac="02:11:22:33:44:55", name="Test", ip="127.0.0.1",
                         port=1)
    played: list[tuple] = []

    class _PM:
        def get_player(self, mac):
            return player

        async def play_url(self, mac, url, title=None):
            played.append((mac, url, title))
            return True

    import lyrion.player as player_pkg

    monkeypatch.setattr(player_pkg, "PlayerManager", _PM)

    fav = Favorite(id=7, title="Hirschmilch Chillout",
                   url="http://hirschmilch.de:7000/chillout.mp3", icon=LOGO)

    class _Session:
        async def get(self, model, pk):
            return fav

    @asynccontextmanager
    async def session_ctx():
        yield _Session()

    manager = FavoritesManager()
    monkeypatch.setattr(manager, "_db_session", session_ctx)

    ok = asyncio.run(manager.play("02:11:22:33:44:55", 7))
    assert ok is True
    assert played == [("02:11:22:33:44:55",
                       "http://hirschmilch.de:7000/chillout.mp3",
                       "Hirschmilch Chillout")]
    assert player.stream_titles["http://hirschmilch.de:7000/chillout.mp3"] == \
        "Hirschmilch Chillout"
    # ``_set_stream_image`` drops the leading ``html/`` (static_dir has it)
    assert player.stream_images[
        "http://hirschmilch.de:7000/chillout.mp3"] == LOGO


# ── 5. the logo really answers (raw check, skipped without network) ───────


BASE = "http://127.0.0.1:9002"


def _fetch(path: str):
    try:
        with urllib.request.urlopen(BASE + path, timeout=15) as r:
            return r.status, r.headers.get("Content-Type", ""), r.read()
    except urllib.error.HTTPError as e:
        return e.code, "", b""
    except OSError as e:
        pytest.skip(f"dev server not reachable: {e}")


def test_the_small_and_large_logo_url_really_deliver():
    """The row's path serves a real image in both sizes (Pillow measures it)."""
    pillow = pytest.importorskip("PIL.Image")

    head, _, tail = LOGO.rpartition("/")
    variants = {"full": LOGO,
                "small": f"{head}/{tail.replace('.', '_40x40_m.', 1)}"}
    for label, path in variants.items():
        status, ctype, data = _fetch(path)
        if status != 200:
            pytest.skip(f"image proxy/CDN answered {status} for {path}")
        assert ctype.startswith("image/"), (label, ctype)
        with pillow.open(io.BytesIO(data)) as img:
            assert img.width > 0 and img.height > 0, label
            if label == "small":
                assert (img.width, img.height) == (40, 40), (img.width,
                                                             img.height)
