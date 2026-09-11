"""``contextmenu`` — SqueezePlay press-and-hold menus (Perl parity).

SqueezePlay opens the modal context window for a tapped item with
``['contextmenu', <index>, <quantity>, 'playlist_index:N', 'menu:track', …]``.
Until now this server answered ``{}`` (the command was unimplemented), so the
dialog opened **empty** (black window, only the X button).

Perl treats ``contextmenu`` as a *wrapper* (``Slim::Control::Queries.pm:6171``
``contextMenuQuery``): it copies every non ``_index``/``_quantity`` param,
forwards them to ``['<menu>info', 'items', <index>, <quantity>, …params]``
(:6196-6200) and returns that feed's result verbatim.  Consequences used here:

* without a ``menu`` param → ``setStatusBadParams`` (:6220) → JSON-RPC ``{}``;
* an unknown ``menu`` → the ``<menu>info`` command does not exist →
  bad dispatch → ``{}``;
* ``menu:track`` → ``trackinfo items``, ``menu:album`` → ``albuminfo items``,
  … (the valid names are the info feeds minus their ``info`` suffix).

``trackinfo``'s ``cliQuery`` (``Slim/Menu/TrackInfo.pm:1378``) then resolves
the context: a ``track_id`` wins, otherwise ``playlist_index`` addresses the
player's playlist (Perl array semantics: the token is numified with ``0 +``
and a negative index counts from the end), otherwise a raw ``url`` — with no
context at all the feed stays undefined → ``{}``.

Every shape emitted below is copied from a **live** Perl response captured
read-only from ``192.168.1.90:9000`` (player ``24:0a:c4:29:77:90``); the
verbatim answers live in ``tests/fixtures/perl_contextmenu_*.json`` and
``tests/test_contextmenu.py`` documents the exact probe per fixture.

Known divergences from Perl (deliberate, documented in the tests):

* the item ``item_id`` crumb is Perl's per-session SID; we reuse the request's
  crumb prefix or a stable ``ffffffff`` (key parity, value differs);
* a remote ``RemoteTrack`` id (``"-94115167819792"``) is opaque; we use the
  stream URL as the id when no library track matches it;
* ``albuminfo``/``artistinfo``/``genreinfo`` expose many more providers than
  the tap menu needs — we emit the same core entries (add/insert/play,
  favourites, library links) with Perl's exact shapes.
"""
from __future__ import annotations

import re
import urllib.parse
from typing import Any

# Screenshot-verified shape of the Jive window (all live context menus).
WINDOW_TEXT_LIST = {"windowStyle": "text_list"}
# Perl's pidgin URL on the contributor entry's presetParams.
_PERL_IP3K_URL = "not a valid URL, but needed to make the ip3k UI work..."
_NOT_PLAYABLE_WINDOW = {"isContextMenu": 1}

# Localised texts (LMS 'DE' strings) — taken verbatim from the Perl answers.
ADD_TO_END = "Am Ende hinzufügen"
PLAY_NEXT = "Als nächstes wiedergeben"
PLAY = "Wiedergabe"
FAV_ADD = "In Favoriten speichern"
FAV_DELETE = "Favorit löschen"
SHOW_ARTWORK = "Plattenhülle anzeigen"
MORE_INFO = "Weitere Infos"

#: ``menu:<name>`` → the info feed it forwards to.
_MENU_INFO = {
    "track": "trackinfo",
    "album": "albuminfo",
    "artist": "artistinfo",
    "genre": "genreinfo",
}
#: entity menu → (param key, browselibrary drill mode, label).
_ENTITY = {
    "album": ("album_id", "tracks", "Album"),
    "artist": ("artist_id", "albums", "Interpret"),
    "genre": ("genre_id", "artists", "Stilrichtung"),
}


def _perl_number(token: Any) -> int:
    """Perl ``0 + $token`` numification (see ``api._playctl_index``)."""
    from lyrion.web.api import _playctl_index

    return _playctl_index(token)


def split_request(args: list[Any]) -> tuple[int, int, dict[str, str]]:
    """Split ``contextmenu <index> <quantity> <params…>`` the Perl way.

    Replicates the ``cliQuery`` tagged-token hack (``TrackInfo.pm:1382``):
    a token containing ``:`` sitting in the index/quantity slot is a real
    tagged param that was mis-placed and is re-appended as such.
    """
    args = list(args or [])
    index: Any = args[0] if args else 0
    quantity: Any = args[1] if len(args) > 1 else 200
    rest = list(args[2:])
    if isinstance(index, str) and ":" in index:
        rest.insert(0, index)
        index = 0
    if isinstance(quantity, str) and ":" in quantity:
        rest.insert(0, quantity)
        quantity = 200
    params: dict[str, str] = {}
    for token in rest:
        text = str(token)
        if ":" in text:
            key, value = text.split(":", 1)
            params[key] = value
    return _perl_number(index), _perl_number(quantity), params


# ---------------------------------------------------------------------------
# Item builders — each mirrors one Perl provider's jive shape
# ---------------------------------------------------------------------------


def _core_items(target: dict[str, Any], id_type: type = int,
                extra_params: dict[str, Any] | None = None) -> list[dict]:
    """Add / Play-next / Play — Perl ``addTrackEnd``/``addTrackNext``/
    ``playTrack`` for a ``menuContext`` of ``normal`` (no ``context:``
    param), i.e. the three play-control entries the tap menu always carries.

    ``id_type`` controls the JSON type of the ``params`` id: Perl ships a
    library/entity id as a JSON *number* (``"album_id": 45``) but an opaque
    remote id as a string.  Only digit-like strings are converted, so a URL
    stays a string.  ``extra_params`` mirrors provider-specific extras — the
    album menu's entries carry ``menu: 1`` (live fixture), the track/artist/
    genre ones do not.
    """
    def convert(value: Any) -> Any:
        if id_type is int and isinstance(value, str) \
                and re.fullmatch(r"[+-]?[0-9]+", value):
            return int(value)
        return value

    target = {k: convert(v) for k, v in target.items()}
    extra = dict(extra_params or {})

    def action(cmd: str, next_window: str) -> dict:
        params: dict[str, Any] = {"cmd": cmd}
        params.update(target)
        params.update(extra)
        return {"player": 0, "cmd": ["playlistcontrol"],
                "params": params, "nextWindow": next_window}

    add = [action("add", "parent") for _ in range(3)]
    insert = [action("insert", "parent") for _ in range(3)]
    load = [action("load", "nowPlaying") for _ in range(2)]
    return [
        {"type": "text", "text": ADD_TO_END, "style": "item_add",
         "addAction": "go",
         "actions": {"go": add[0], "add": add[1], "play": add[2]}},
        {"type": "text", "text": PLAY_NEXT, "style": "item_insert",
         "addAction": "go",
         "actions": {"go": insert[0], "add": insert[1],
                     "play": insert[2]}},
        {"type": "text", "text": PLAY, "style": "itemplay",
         "addAction": "go",
         "actions": {"go": load[0], "play": load[1]}},
    ]


def _favorites_item(url: str, title: str, is_favorite: bool,
                    item_id: str = "") -> dict:
    """``trackinfo`` showFavorites entry: ``jivefavorites add`` when the
    stream is not a favourite, ``jivefavorites delete`` when it is."""
    if is_favorite:
        return {
            "type": "text", "text": FAV_DELETE, "style": "item_fav",
            "addAction": "go",
            "actions": {"go": {
                "player": 0, "cmd": ["jivefavorites", "delete"],
                "params": {"item_id": item_id, "isContextMenu": 1,
                           "url": url, "title": title}}},
        }
    return {
        "type": "text", "text": FAV_ADD, "style": "item_fav",
        "addAction": "go",
        "actions": {"go": {
            "player": 0, "cmd": ["jivefavorites", "add"],
            "params": {"isContextMenu": 1, "url": url, "title": title}}},
    }


def _entity_item(label: str, name: str, *, key: str, value: Any, mode: str,
                 info_cmd: str, preset_url: str) -> dict:
    """A library link row ("Interpret: X", "Album: X", "Stilrichtung: X") —
    Perl ``infoContributors``/``infoAlbum``/``infoGenres``: type ``playlist``
    with ``go``/``play``/``add``/``add-hold``/``more`` itemActions."""
    fixed = {key: value, "library_id": ""}

    def pc(cmd: str, next_window: str | None = None) -> dict:
        params: dict[str, Any] = {"cmd": cmd}
        params.update(fixed)
        params["menu"] = 1
        action: dict = {"player": 0, "cmd": ["playlistcontrol"],
                        "params": params}
        if next_window:
            action["nextWindow"] = next_window
        return action

    go_params: dict[str, Any] = {"mode": mode}
    go_params.update(fixed)
    go_params["menu"] = 1
    more_params: dict[str, Any] = dict(fixed)
    more_params["menu"] = 1
    return {
        "text": f"{label}: {name}",
        "type": "playlist",
        "presetParams": {"favorites_type": "playlist",
                         "favorites_title": name,
                         "favorites_url": preset_url},
        "actions": {
            "go": {"player": 0, "cmd": ["browselibrary", "items"],
                   "params": go_params},
            "play": pc("load", "nowPlaying"),
            "add": pc("add"),
            "add-hold": pc("insert"),
            "more": {"player": 0, "cmd": [info_cmd, "items"],
                     "window": dict(_NOT_PLAYABLE_WINDOW),
                     "params": more_params},
        },
    }


def _artwork_item(track_id: Any) -> dict:
    """Perl ``showArtwork`` — only emitted when the track has a cover."""
    return {
        "type": "text", "text": SHOW_ARTWORK, "showBigArtwork": 1,
        "addAction": "go",
        "actions": {
            "go": {"cmd": ["trackinfo", "items"],
                   "params": {"track_id": str(track_id),
                              "menu": "trackinfo"}},
            "do": {"cmd": ["artwork", track_id]},
        },
    }


def _more_item(track_id: Any, crumb: str) -> dict:
    """Perl ``infoMoreInfo`` — an unfold entry into the full ``trackinfo``
    window.  ``crumb`` is Perl's session item id (``<sid>.<index>``)."""
    return {
        "text": MORE_INFO, "addAction": "go",
        "actions": {"go": {
            "cmd": ["trackinfo", "items"],
            "params": {"menu": "trackinfo", "track_id": str(track_id),
                       "item_id": crumb, "isContextMenu": 1}}},
    }


def _track_base_actions(track_id: Any, index: int, quantity: int,
                        pc_extra: dict[str, Any] | None = None,
                        feed_extra: dict[str, Any] | None = None,
                        with_presets: bool = True) -> dict:
    """``trackinfo`` feed actions (live: ``trackinfo`` library menu).

    ``set-preset-0..9`` are the Jive favourite-slot shortcuts; XMLBrowser only
    emits them when at least one item carries ``presetParams`` (live: the
    library menu has them, the remote-stream menu does not).  ``playControl``
    is the window SqueezePlay uses to re-open the menu for another row.

    ``pc_extra`` mirrors Perl's ``playControl`` params, which are the request's
    tagged params *copied verbatim* (``playlist_index:abc`` stays ``"abc"``,
    an extra ``useContextMenu``/``url`` is echoed — live ``-1``/``abc``/
    ``useContextMenu:1`` probes); ``feed_extra`` is what ``cliQuery`` adds to
    the feed params — ``url`` for playlist/URL contexts only, never for a plain
    ``track_id`` lookup (live library fixture has no ``url``).
    """
    params = {"menu": "trackinfo", "track_id": str(track_id)}
    params.update(feed_extra or {})
    pc_params: dict[str, Any] = {"_index": str(index),
                                 "_quantity": str(quantity)}
    pc_params.update(pc_extra or {})
    pc_params["menu"] = "track"
    pc_params["track_id"] = str(track_id)
    actions: dict[str, dict] = {
        "go": {"params": dict(params), "itemsParams": "params",
               "cmd": ["trackinfo", "items"]},
        "add": {"player": 0, "cmd": ["trackinfo", "playlist", "add"],
                "itemsParams": "params", "params": dict(params)},
        "add-hold": {"player": 0, "cmd": ["trackinfo", "playlist", "insert"],
                     "itemsParams": "params", "params": dict(params)},
        "play": {"itemsParams": "params", "nextWindow": "nowPlaying",
                 "params": dict(params), "player": 0,
                 "cmd": ["trackinfo", "playlist", "play"]},
        "more": {"window": dict(_NOT_PLAYABLE_WINDOW), "player": 0,
                 "cmd": ["trackinfo", "items"], "itemsParams": "params",
                 "params": dict(params)},
        "playControl": {
            "player": 0, "cmd": ["trackinfo", "items"],
            "window": dict(_NOT_PLAYABLE_WINDOW),
            "itemsParams": "playControlParams",
            "params": pc_params,
        },
    }
    if with_presets:
        for slot in range(10):
            actions[f"set-preset-{slot}"] = {
                "player": 0,
                "cmd": ["jivefavorites", "set_preset", f"key:{slot}"],
                "itemsParams": "presetParams"}
    return actions


def _entity_base_actions(menu: str, key: str, value: Any, index: int,
                         quantity: int) -> dict:
    """``albuminfo``/``artistinfo``/``genreinfo`` feed actions (live)."""
    info_cmd = _MENU_INFO[menu]
    params = {"menu": info_cmd}
    return {
        "go": {"cmd": [info_cmd, "items"], "params": dict(params),
               "itemsParams": "params"},
        "add": {"player": 0, "cmd": [info_cmd, "playlist", "add"],
                "itemsParams": "params", "params": dict(params)},
        "add-hold": {"player": 0, "cmd": [info_cmd, "playlist", "insert"],
                     "itemsParams": "params", "params": dict(params)},
        "play": {"itemsParams": "params", "nextWindow": "nowPlaying",
                 "params": dict(params), "player": 0,
                 "cmd": [info_cmd, "playlist", "play"]},
        "more": {"player": 0, "cmd": [info_cmd, "items"],
                 "window": dict(_NOT_PLAYABLE_WINDOW),
                 "params": dict(params), "itemsParams": "params"},
        "playControl": {
            "player": 0, "cmd": [info_cmd, "items"],
            "window": dict(_NOT_PLAYABLE_WINDOW),
            "itemsParams": "playControlParams",
            "params": {"_index": str(index), "_quantity": str(quantity),
                       "menu": menu, key: str(value)},
        },
    }


def _crumb(params: dict[str, str]) -> str:
    """Perl's ``item_id`` is ``<session-sid>.<itemIndex>``; reuse the request
    crumb prefix when the client supplied one (stable, same key)."""
    previous = str(params.get("item_id") or "")
    if "." in previous:
        return previous.rsplit(".", 1)[0]
    return "ffffffff"


# ---------------------------------------------------------------------------
# Response assembly
# ---------------------------------------------------------------------------


def render_track_menu(*, track: dict | None, url: str, title: str,
                      track_id: Any, genre_id: Any, is_favorite: bool,
                      index: int, quantity: int, crumb: str,
                      pc_extra: dict[str, Any] | None = None,
                      feed_extra: dict[str, Any] | None = None) -> dict:
    """Build the ``menu:track`` window (Perl ``trackinfo`` feed).

    ``track`` is the library row (``api._load_tracks`` shape) or ``None`` for
    a stream with no matching library track.  Only providers that actually
    resolve are emitted, in Perl's provider order.
    """
    is_library = track is not None
    id_value = track_id if is_library else str(track_id)
    items: list[dict] = _core_items({"track_id": id_value},
                                    id_type=int if is_library else str)

    items.append(_favorites_item(url, title, is_favorite,
                                 item_id=crumb.rsplit(".", 1)[0] + ".0"
                                 if is_favorite else ""))
    actual_id = track.get("id", track_id) if track else track_id
    if track:
        if track.get("artist_id") is not None and track.get("artist"):
            items.append(_entity_item(
                "Interpret", track["artist"], key="artist_id",
                value=track["artist_id"], mode="albums",
                info_cmd="artistinfo", preset_url=_PERL_IP3K_URL))
        if track.get("album_id") is not None and track.get("album"):
            items.append(_entity_item(
                "Album", track["album"], key="album_id",
                value=track["album_id"], mode="tracks",
                info_cmd="albuminfo", preset_url="blabla"))
        if track.get("genre"):
            items.append(_entity_item(
                "Stilrichtung", track["genre"], key="genre_id",
                value=genre_id if genre_id is not None else track["genre"],
                mode="artists", info_cmd="genreinfo", preset_url="blabla"))
        if track.get("artwork_url") or track.get("cover"):
            items.append(_artwork_item(actual_id))
    items.append(_more_item(actual_id, crumb))

    base = _track_base_actions(
        actual_id, index, quantity, pc_extra=pc_extra, feed_extra=feed_extra,
        with_presets=any("presetParams" in item for item in items))
    return {
        "window": dict(WINDOW_TEXT_LIST),
        "offset": 0,
        "count": len(items),
        "item_loop": items,
        "title": title,
        "base": {"actions": base},
    }


def render_entity_menu(menu: str, *, key: str, value: Any, title: str,
                       favorite_url: str, index: int, quantity: int) -> dict:
    """Build a ``menu:album``/``menu:artist``/``menu:genre`` window."""
    # Live fixtures: the album menu's play-control params carry ``menu: 1``,
    # the artist/genre ones do not.
    extra = {"menu": 1} if menu == "album" else None
    items = _core_items({key: value}, extra_params=extra)
    if menu in ("album", "artist"):
        items.append(_favorites_item(favorite_url, title, False))
    return {
        "window": dict(WINDOW_TEXT_LIST),
        "offset": 0,
        "count": len(items),
        "item_loop": items,
        "title": title,
        "base": {"actions": _entity_base_actions(menu, key, value,
                                                 index, quantity)},
    }


# ---------------------------------------------------------------------------
# Context resolution (async, DB/player backed)
# ---------------------------------------------------------------------------


def _db_row(sql: str, params: tuple = ()) -> dict | None:
    from lyrion.web import api as api_mod

    try:
        rows = api_mod._db_query(sql, params)
    except Exception:  # noqa: BLE001
        return None
    return rows[0] if rows else None


def _genre_index(genre: str) -> int | None:
    """Our numeric genre id = the index into the DISTINCT genre list (the
    ``genres`` table is empty — same convention as ``browselibrary``)."""
    row = _db_row(
        "SELECT COUNT(*) AS n FROM (SELECT DISTINCT genre FROM tracks "
        "WHERE genre != '' AND genre < ? COLLATE NOCASE)", (genre,))
    return int(row["n"]) if row else None


async def _is_favorite(url: str) -> bool:
    if not url:
        return False
    try:
        from sqlalchemy import select

        from lyrion.database.schema import Favorite
        from lyrion.database.sqlite_helper import db_session

        async with db_session() as session:
            result = await session.execute(
                select(Favorite.id).where(Favorite.url == url).limit(1))
            return result.first() is not None
    except Exception:  # noqa: BLE001
        return False


def _playlist_item(player: Any, token: str) -> tuple[bool, Any]:
    """Resolve ``playlist_index:<token>`` like Perl's
    ``Slim::Player::Playlist::track``: number, negative from the end."""
    playlist = list(getattr(player, "playlist", None) or [])
    if not playlist:
        return False, None
    n = _perl_number(token)
    if n < 0:
        n += len(playlist)
    if 0 <= n < len(playlist):
        return True, playlist[n]
    return False, None


async def handle_contextmenu(api: Any, pm: Any, pid: str | None,
                             args: list[Any]) -> dict:
    """Entry point for ``JSONRPCAPI._slim_request`` (``cmd == contextmenu``).

    Returns ``{}`` exactly where Perl does: no ``menu`` param, an unknown
    ``menu``, or a context (playlist index / id) that resolves to nothing.
    """
    index, quantity, params = split_request(args)
    menu = params.get("menu")
    if not menu:
        return {}
    if menu not in _MENU_INFO:
        return {}
    # XMLBrowser's play-control branch wins over the menu providers whenever a
    # `xmlbrowserPlayControl` token is present; a trackinfo feed has no audio
    # rows there, so Perl returns the empty window (live-probed 0/1/5/-1).
    if "xmlbrowserPlayControl" in params:
        return {"window": dict(WINDOW_TEXT_LIST), "offset": 0, "count": 0}

    if menu == "track":
        return await _track_menu(api, pm, pid, index, quantity, params)
    return await _entity_menu(menu, index, quantity, params)


async def _track_menu(api: Any, pm: Any, pid: str | None, index: int,
                      quantity: int, params: dict[str, str]) -> dict:
    player = pm.get_player(pid) if (pm is not None and pid) else None
    track_id: Any = None
    url = ""
    playlist_hit = False

    raw_track_id = params.get("track_id")
    if raw_track_id not in (None, ""):
        track_id = _perl_number(raw_track_id)
        if track_id == 0 and not str(raw_track_id).lstrip("+-").isdigit():
            track_id = str(raw_track_id)
    elif "playlist_index" in params:
        ok, item = _playlist_item(player, params["playlist_index"])
        if not ok:
            return {}
        playlist_hit = True
        text = str(item)
        if text.lstrip("-").isdigit():
            track_id = _perl_number(text)
        else:
            url = text
    elif params.get("url"):
        url = str(params["url"])
    else:
        return {}

    track: dict | None = None
    if track_id is not None:
        info = await api._load_tracks([track_id])
        track = info.get(track_id)
    if track is None and url:
        row = _db_row("SELECT id FROM tracks WHERE url = ? LIMIT 1", (url,))
        if row:
            track_id = row["id"]
            info = await api._load_tracks([track_id])
            track = info.get(track_id)
    if track is None and not url:
        return {}

    if track:
        url = track.get("url") or url
        title = track.get("title") or params.get("title") or ""
        track_id = track.get("id", track_id)
    else:
        title = str(params.get("title") or "") or (
            getattr(player, "current_title", "") or "" if playlist_hit else "")

    # Perl's cliQuery adds `url` to the feed params for playlist/URL contexts
    # (never for a plain track_id lookup) and XMLBrowser copies the request's
    # tagged params verbatim into the feed + playControl params.
    from_playlist = "playlist_index" in params
    from_url = bool(params.get("url"))
    feed_extra = {"url": url} if (url and (from_playlist or from_url)) else {}
    pc_extra: dict[str, Any] = {k: v for k, v in params.items()
                                if k not in ("_index", "_quantity", "menu")}
    if url and (from_playlist or from_url):
        pc_extra["url"] = url

    genre_id = _genre_index(track["genre"]) if (
        track and track.get("genre")) else None
    favorite = await _is_favorite(url)
    return render_track_menu(
        track=track, url=url, title=title, track_id=track_id,
        genre_id=genre_id, is_favorite=favorite, index=index,
        quantity=quantity, crumb=_crumb(params), pc_extra=pc_extra,
        feed_extra=feed_extra)


async def _entity_menu(menu: str, index: int, quantity: int,
                       params: dict[str, str]) -> dict:
    key, _mode, _label = _ENTITY[menu]
    value = params.get(key)
    if value in (None, ""):
        return {}
    # SQLite compares a text parameter against an INTEGER pk without a match,
    # so numify numeric ids for the lookup (Perl's DBI does this implicitly).
    lookup: Any = int(value) if re.fullmatch(r"[+-]?[0-9]+", str(value)) \
        else value
    title = ""
    favorite_url = ""
    if menu == "album":
        row = _db_row("SELECT title FROM albums WHERE id = ?", (lookup,))
        title = (row or {}).get("title") or ""
        favorite_url = "db:album.title=" + urllib.parse.quote_plus(title)
    elif menu == "artist":
        row = _db_row("SELECT name FROM contributors WHERE id = ?", (lookup,))
        title = (row or {}).get("name") or ""
        favorite_url = "db:contributor.name=" + urllib.parse.quote_plus(title)
    else:  # genre — our numeric id indexes the DISTINCT genre list
        from lyrion.web.api import _genre_id_to_text

        title = _genre_id_to_text(value)
        value = title or value
    return render_entity_menu(menu, key=key, value=value, title=title,
                              favorite_url=favorite_url, index=index,
                              quantity=quantity)
