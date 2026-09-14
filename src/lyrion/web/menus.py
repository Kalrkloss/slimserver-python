"""Menu builders for the Jive/JSON-RPC menus (Perl parity).

Perl reference (read-only, ``/tmp/lms-ref``); every claim with file:line:

* ``base.actions`` of a browselibrary window:
  ``Slim/Control/XMLBrowser.pm:966-1000`` builds ``go``/``play``/``add``/
  ``add-hold``/``more``, ``:1005-1012`` appends ``playControl``,
  ``:1427`` calls ``_jivePresetBase`` when an item carried ``presetParams``
  (``:1131-1135`` sets ``$presetFavSet = 1``), ``:1798-1808``
  ``_jivePresetBase`` emits ``set-preset-0`` … ``set-preset-9``.
* ``_makeAction`` (``XMLBrowser.pm:1663-1689``): ``params = fixedParams +
  {menu => 1}``, ``player => 0``, ``window {isContextMenu => 1}`` for
  ``more``, and ``itemsParams`` from the feed's ``commonVariables``
  (``'commonParams'`` here).
* ``add-hold`` is the feed's ``insert`` action (``BrowseLibrary.pm:1183-1186``
  tracks, ``:1244-1260`` artists, ``:1325-1340`` genres, ``:1943-1946``
  bmf, ``:2084-2093`` album tracks) → ``playlistcontrol cmd:insert``.
* ``more`` is the feed's ``info`` action, per mode: ``trackinfo items``
  (``BrowseLibrary.pm:1922-1926``), ``albuminfo items`` (:1743-1746),
  ``artistinfo items`` (:1253-1256), ``genreinfo items`` (:1325-1328),
  ``yearinfo items`` (:1383-1386), ``folderinfo items`` (:2068).
* My-Music child nodes: ``Slim/Menu/BrowseLibrary.pm:495-653``
  ``_registerBaseNodes`` (ids/weights/params/conditions) and ``:660-707``
  ``getJiveMenu`` (``text`` = the ``name`` token, ``homeMenuText``,
  ``icon`` from ``jiveIcon``, ``actions.go`` = ``browselibrary items``
  with ``menu => 1`` + the node params).
* Home items: ``Slim/Control/Jive.pm:261-320`` ``mainMenu``,
  ``:1395-1600`` ``playerSettingsMenu``, ``:1879-1959``
  ``repeatSettings``/``shuffleSettings``, ``:1681-1704`` ``syncMenuItem``,
  ``:2239-2275`` ``playerPower``, ``:371-397`` ``albumSortSettingsItem``.
* Live Perl 9.1.1 answers captured read-only from ``192.168.1.90:9000``
  (player ``24:0a:c4:29:77:90``) on 2026-09-12; the verbatim commands and
  the literal Perl JSON live in ``tests/test_menus.py``.

Titles come from ``strings.txt`` (read-only) through ``lyrion.i18n`` —
never a bare English literal.  The ``(DE, EN)`` pairs below are the DE/EN
columns of ``/tmp/lms-ref/strings.txt`` with their line numbers; a key that
already exists in ``lyrion/i18n/strings_*.py`` wins over the pair
(``get_string`` lookup chain, ``Strings.pm:414-416``).
"""
from __future__ import annotations

from typing import Any

#: token → (DE value, strings.txt DE line, EN value, strings.txt EN line)
_TITLES: dict[str, tuple[str, int, str, int]] = {
    "MY_MUSIC": ("Eigene Musik", 530, "My Music", 531),
    "RADIO": ("Radio", 505, "Radio", 506),
    "BROWSE_BY_ARTIST": ("Interpreten", 13991, "Artists", 13992),
    "BROWSE_BY_ALBUMARTIST": ("Album-Interpreten", 14047, "Album Artists", 14048),
    "BROWSE_BY_ALL_ARTISTS": ("Alle Interpreten", 14028, "All Artists", 14029),
    "BROWSE_BY_ALBUM": ("Alben", 14224, "Albums", 14225),
    "BROWSE_BY_GENRE": ("Stilrichtung", 13967, "Genres", 13968),
    "BROWSE_BY_WORK": ("Werke", 14318, "Works", 14319),
    "BROWSE_BY_YEAR": ("Jahrgang", 14329, "Years", 14330),
    "BROWSE_NEW_MUSIC": ("Neue Musik", 14274, "New Music", 14275),
    "BROWSE_MUSIC_FOLDER": ("Musikordner", 14347, "Music Folder", 14348),
    "SEARCH": ("Suchen", 16412, "Search", 16413),
    "SAVED_PLAYLISTS": ("Wiedergabelisten", 12098, "Playlists", 12099),
    "BROWSE_ARTISTS": ("Interpreten durchsuchen", 14011, "Browse Artists", 14012),
    "BROWSE_ALBUMARTISTS": ("Album-Interpreten durchsuchen", 14066,
                            "Browse Album Artists", 14067),
    "ALL_ARTISTS": ("Alle Interpreten", 13628, "All Artists", 13629),
    "BROWSE_ALBUMS": ("Alben durchsuchen", 14084, "Browse Albums", 14085),
    "BROWSE_GENRES": ("Stilrichtungen durchsuchen", 14102,
                      "Browse Genres", 14103),
    "BROWSE_WORKS": ("Werke durchsuchen", 14120, "Browse Works", 14121),
    "BROWSE_YEARS": ("Jahre durchsuchen", 14199, "Browse Years", 14200),
    "AUDIO_SETTINGS": ("Audio", 14966, "Audio", 14967),
    "REPEAT": ("Wiederholen", 11123, "Repeat", 11124),
    "SHUFFLE": ("Zufall", 11300, "Shuffle", 11301),
    "ALARM": ("Wecker", 1489, "Alarm Clock", 1490),
    "SLEEP": ("Schlafmodus", 1376, "Sleep", 1377),
    "SYNCHRONIZE": ("Synchronisieren", 14378, "Synchronize", 14379),
    "SETUP_TRANSITIONTYPE": ("Überblendung", 5962, "Crossfade", 5963),
    "REPLAYGAIN": ("Lautstärken-Normalisierung", 5761,
                   "Volume Adjustment", 5762),
    "FIXED_VOLUME": ("Festgelegte Lautstärke", 11645, "Fixed Volume", 11646),
    "ALBUMS_SORT_METHOD": ("Sortierverfahren für Alben", 22996,
                           "Albums Sort Method", 22997),
    "OFF": ("Aus", 11183, "off", 11184),
    "SONG": ("Titel", 13822, "Song", 13823),
    "ALBUM": ("Album", 12438, "Album", 12439),
    "PLAYLIST_SHORT": ("Liste", 608, "Playlist", 609),
    "JIVE_TURN_PLAYER_OFF": ("%s ausschalten", 22729, "Turn Off %s", 22730),
    "JIVE_TURN_PLAYER_ON": ("%s einschalten", 22712, "Turn On %s", 22713),
}

#: browselibrary mode → the feed's ``info`` command (``more`` action target).
INFO_FEEDS: dict[str, str] = {
    "tracks": "trackinfo",
    "albums": "albuminfo",
    "artists": "artistinfo",
    "genres": "genreinfo",
    "years": "yearinfo",
    "folder": "folderinfo",
}

#: ``more`` is only emitted for the modes whose ``*info items`` feed this
#: port actually serves (``web.api`` dispatches them).  ``folder`` is NOT
#: live-probed for the ``menu:1`` bmf window (the only sample,
#: ``/tmp/perl_bmf_top.json``, is the older ``menu:browselibrary`` request
#: form) — the node is left out instead of pointing at a dead command.
MORE_KINDS: frozenset[str] = frozenset(
    {"tracks", "albums", "artists", "genres", "years"})

#: browselibrary mode → the mode ``go`` drills into (Perl fixedParams).
GO_MODE: dict[str, str] = {
    "albums": "tracks", "artists": "albums", "genres": "albums",
    "years": "albums", "folder": "bmf", "tracks": "tracks",
}


def menu_title(token: str) -> str:
    """``$client->string($token)`` for a menu title (``Strings.pm:525-536``).

    Lookup goes through ``lyrion.i18n.get_string``; the DE/EN column of
    ``strings.txt`` is the fallback when our shipped i18n table has no line
    for the key yet (same chain as ``Strings.pm:414-416``).
    """
    from lyrion.i18n import get_string, resolve_language

    lang = resolve_language()
    de, _de_line, en, _en_line = _TITLES.get(token, (token, 0, token, 0))
    return get_string(token, lang=lang, default=de if lang == "DE" else en)


def _ucfirst(text: str) -> str:
    """Perl ``ucfirst`` — as used on the choice strings (``Jive.pm:1884``)."""
    return text[:1].upper() + text[1:] if text else text


# ── browselibrary base.actions (MENU-03/04/05) ───────────────────────────

def add_hold_action(kind: str) -> dict:
    """Perl's feed ``insert`` action (``XMLBrowser.pm:926-931,964-966``).

    Live shapes: albums ``{cmd:insert, menu:1}``, artists ``{cmd:insert,
    menu_mode:artists, menu:1}`` (``BrowseLibrary.pm:1244-1260``),
    genres ``{cmd:insert, role_id:ALBUMARTIST, menu:1}`` (:1325-1340).
    """
    params: dict = {"cmd": "insert"}
    if kind == "artists":
        params["menu_mode"] = "artists"
    elif kind == "genres":
        params["role_id"] = "ALBUMARTIST"
    params["menu"] = 1
    return {"player": 0, "cmd": ["playlistcontrol"],
            "itemsParams": "commonParams", "params": params}


def more_action(kind: str, filters: dict | None = None) -> dict | None:
    """Perl's feed ``info`` action — the long-press context menu (MENU-03).

    ``XMLBrowser.pm:932-938`` + ``:1663-1689``: ``cmd [<feed>, 'items']``,
    ``itemsParams 'commonParams'``, ``window {isContextMenu:1}``, and
    ``params`` = the feed's ``fixedParams`` + ``menu:1``.  Only the tracks
    feed has ``fixedParams`` (``_tagsToParams``, ``BrowseLibrary.pm:1916``),
    which is why the live tracks entry echoes the album drill while
    albums/artists/genres/years carry ``{menu:1}`` alone.
    """
    feed = INFO_FEEDS.get(kind)
    if feed is None or kind not in MORE_KINDS:
        return None
    params: dict = {"menu": 1}
    if kind == "tracks":
        params.update(filters or {})
    return {"player": 0, "cmd": [feed, "items"], "itemsParams": "commonParams",
            "window": {"isContextMenu": 1}, "params": params}


def set_preset_actions() -> dict:
    """``_jivePresetBase`` (``XMLBrowser.pm:1798-1808``) — ``set-preset-0`` …
    ``set-preset-9`` → ``jivefavorites set_preset key:N`` reading
    ``presetParams``.
    """
    return {
        f"set-preset-{n}": {
            "player": 0,
            "cmd": ["jivefavorites", "set_preset", f"key:{n}"],
            "itemsParams": "presetParams",
        }
        for n in range(10)
    }


def window_style_for_items(items: list[dict]) -> dict:
    """Perl's ``window.windowStyle`` for an item list (XMLBrowser.pm).

    Perl decides the style from the *items* of the answered window, not from
    the browse mode — ``Slim/Control/XMLBrowser.pm``::

        :1104-1106  # Bug 13175, support custom windowStyle - this is really naff
                    $windowStyle = $item->{style} if $item->{style};
        :1119-1123  if ($item->{'name2'}) { $itemText .= "\\n" . $item->{'name2'};
                        $windowStyle = 'icon_list' if !$windowStyle; }
        :1124-1126  elsif (my $line2 = $item->{line2} || $item->{subtext}) {
                        $windowStyle = 'icon_list';
                        $itemText = ( $item->{line1} || $nameOrTitle ) . "\\n" . $line2; }
        :1160-1169  $item->{icon} / $item->{image} / $item->{artwork_track_id}
                        → $hash{'icon-id'} / {'icon'} and $hasImage = 1
        :1434-1441  if ($windowStyle) → it
                    elsif ($hasImage) → 'home_menu'
                    else              → 'text_list'

    So a window is ``icon_list`` as soon as one of its feed items carries
    ``name2``/``line2``/``subtext`` (Perl renders those into ``text`` as
    ``name "\\n" name2``), ``home_menu`` when no item has a second text line
    but one carries an image, and ``text_list`` otherwise.  Live Perl 9.1.1
    (read-only, 2026-09-14, player ``ca:c8:c7:26:6d:38``, request
    ``browselibrary items 0 5 menu:1 mode:<m>``) follows exactly that:
    ``albums`` (=``text`` has the artist line) ``icon_list``, ``artists``
    (every row carries ``icon-id: html/images/artists.png``) ``home_menu``,
    ``genres``/``years``/``tracks``/``bmf`` ``text_list``.

    Our items fold Perl's ``name2``/``line2`` into ``text`` with the same
    ``"\\n"`` separator, so the rendered newline is the item-level signal;
    the image keys are Perl's own (``icon-id`` for a relative path — Perl
    only strips the ``-id`` suffix for ``^https?:`` icons — and ``icon`` for
    the rest).  ``item["style"]`` is deliberately NOT read: Perl's
    ``:1104`` looks at the *feed* item's ``style``, while the ``style`` in an
    answered item is the computed one (``$hash{'style'} = 'item_play'`` /
    ``'itemNoAction'``, :1111-1113/:1172-1175), and no browselibrary feed of
    this port sets a feed style.
    """
    has_image = False
    icon_list = False
    for it in items:
        if not isinstance(it, dict):
            continue
        if "\n" in str(it.get("text") or ""):
            icon_list = True                       # :1119-1126
        if it.get("icon") or it.get("icon-id") or it.get("artwork_track_id"):
            has_image = True                       # :1160-1169
    if icon_list:
        return {"windowStyle": "icon_list"}
    if has_image:
        return {"windowStyle": "home_menu"}
    return {"windowStyle": "text_list"}


def base_actions(kind: str, filters: dict | None = None, start: int = 0,
                 count: int = 1, preset_fav_set: bool = False) -> dict:
    """Perl ``base.actions`` for a browselibrary menu window.

    ``preset_fav_set`` mirrors Perl's ``$presetFavSet``: the ``set-preset-*``
    block is only added when an item of the window carried ``presetParams``
    (``XMLBrowser.pm:1131-1135,1427``) — live: albums/artists/years/tracks
    have it, genres do not.
    """
    go_mode = GO_MODE.get(kind, "albums")
    go_params: dict = {"mode": go_mode, "menu": 1}
    if kind == "artists":
        go_params["menu_mode"] = "artists"
    actions: dict = {
        "go": {"player": 0, "cmd": ["browselibrary", "items"],
               "itemsParams": "commonParams", "params": go_params},
        "play": {"player": 0, "cmd": ["playlistcontrol"],
                 "itemsParams": "commonParams",
                 "params": {"cmd": "load", "menu": 1},
                 "nextWindow": "nowPlaying"},
        "add": {"player": 0, "cmd": ["playlistcontrol"],
                "itemsParams": "commonParams",
                "params": {"cmd": "add", "menu": 1}},
        "add-hold": add_hold_action(kind),
    }
    if kind == "folder":
        # ── Perl's "Musikordner" feed (mode:bmf) ─────────────────────────
        # Live Perl 9.1.1 (read-only, 2026-09-14):
        #   browselibrary items 0 3 menu:1 mode:bmf
        #   base.actions.go = {"cmd": ["browselibrary", "items"],
        #                      "itemsParams": "params",
        #                      "params": {"mode": "bmf",
        #                                 "menu": "browselibrary"}}
        # i.e. the bmf feed's own commonVariables are ``params`` (not
        # ``commonParams`` as in the albums/artists/years feeds) and its
        # ``menu`` value is the FEED NAME, not ``1``.  The tap the client
        # builds from that is
        # ``browselibrary items … menu:1 mode:bmf menu:browselibrary
        #   item_id:<index> isContextMenu:1`` — the item's own ``params``
        # (see ``_browselibrary_menu_items``) supply the drill target.  Our
        # previous shape pointed ``go`` at ``playControlParams`` with
        # ``window.isContextMenu`` (the press-and-hold variant), so a plain
        # tap produced no drill request at all: "Musikordner sieht nur die
        # folder, kann sie aber nicht öffnen".
        go_params: dict = {"mode": "bmf", "menu": "browselibrary"}
        cm_action: dict = {
            "player": 0, "cmd": ["browselibrary", "items"],
            "itemsParams": "playControlParams",
            "window": {"isContextMenu": 1},
            "params": {"mode": "bmf", "_quantity": str(count),
                       "_index": str(start), "menu": "1"},
        }
        folder_actions: dict = {
            "go": {"player": 0, "cmd": ["browselibrary", "items"],
                   "itemsParams": "params", "params": dict(go_params)},
            "play": {"player": 0, "cmd": ["browselibrary", "playlist", "play"],
                     "itemsParams": "params", "params": dict(go_params),
                     "nextWindow": "nowPlaying"},
            "add": {"player": 0, "cmd": ["browselibrary", "playlist", "add"],
                    "itemsParams": "params", "params": dict(go_params)},
            "add-hold": {"player": 0,
                         "cmd": ["browselibrary", "playlist", "insert"],
                         "itemsParams": "params", "params": dict(go_params)},
            "more": {"player": 0, "cmd": ["browselibrary", "items"],
                     "itemsParams": "params", "params": dict(go_params),
                     "window": {"isContextMenu": 1}},
            "playControl": cm_action,
        }
        if preset_fav_set:
            folder_actions.update(set_preset_actions())
        return folder_actions

    more = more_action(kind, filters)
    if more is not None:
        actions["more"] = more
    if kind == "tracks":
        # Context-menu only (press-and-hold) — a plain tap on an audio row
        # must not re-open the same list.  Perl shares the shape with ``go``
        # (XMLBrowser.pm:973-983).
        cm_params: dict = {"mode": go_mode, "menu": 1,
                           "useContextMenu": 1,
                           "_index": start, "_quantity": count}
        cm_params.update(filters or {})
        cm_action: dict = {"player": 0, "cmd": ["browselibrary", "items"],
                           "itemsParams": "playControlParams",
                           "window": {"isContextMenu": 1},
                           "params": cm_params}
        actions["go"] = cm_action
        actions["playControl"] = cm_action
    if preset_fav_set:
        actions.update(set_preset_actions())
    return actions


# ── My Music nodes (MENU-13) ─────────────────────────────────────────────

def my_music_nodes(*, unified_artists: bool = False, has_works: bool = False,
                   has_playlists: bool = False) -> list[dict]:
    """The My-Music child nodes (``BrowseLibrary.pm:495-653``).

    ``text``/``homeMenuText`` are the Perl string tokens rendered for the
    active language; ``actions.go`` is ``browselibrary items`` with
    ``menu:1`` + the node's ``params`` (``getJiveMenu``, :679-690).

    Perl gates some nodes with per-node ``condition`` callbacks — the port
    mirrors them instead of shipping entries with no data behind them
    (``_conditionWrapper``, :667-675):

    * ``myMusicArtists`` (unified) only with the ``useUnifiedArtistsList``
      server pref; ``myMusicArtistsAlbumArtists`` + ``myMusicArtistsAllArtists``
      only *without* it (:507-527).  Live Perl has it off, so the live menu
      lists Album-Interpreten + Alle Interpreten.
    * ``myMusicWorks`` only when the library has works (``totals->{work}``,
      :547-556) — this port has no works model, so it stays hidden.
    * ``myMusicPlaylists`` only with a playlist dir or ``totals->{playlist}``
      (:618-625) — this port has no playlist store, so it stays hidden.
    """
    # id, text token (node ``name``), homeMenuText token (None = none
    # registered), weight, params (BrowseLibrary.pm:495-653)
    specs: list[tuple[str, str, str | None, int, dict]] = []
    if unified_artists:
        specs.append(("myMusicArtists", "BROWSE_BY_ARTIST", "BROWSE_ARTISTS",
                      10, {"mode": "artists"}))
    else:
        specs.append(("myMusicArtistsAlbumArtists", "BROWSE_BY_ALBUMARTIST",
                      "BROWSE_ALBUMARTISTS", 9,
                      {"mode": "artists", "role_id": "ALBUMARTIST"}))
        specs.append(("myMusicArtistsAllArtists", "BROWSE_BY_ALL_ARTISTS",
                      "ALL_ARTISTS", 11, {"mode": "artists"}))
    specs.append(("myMusicAlbums", "BROWSE_BY_ALBUM", "BROWSE_ALBUMS", 20,
                  {"mode": "albums"}))
    specs.append(("myMusicGenres", "BROWSE_BY_GENRE", "BROWSE_GENRES", 30,
                  {"mode": "genres"}))
    if has_works:
        specs.append(("myMusicWorks", "BROWSE_BY_WORK", "BROWSE_WORKS", 35,
                      {"mode": "works"}))
    specs.append(("myMusicYears", "BROWSE_BY_YEAR", "BROWSE_YEARS", 40,
                  {"mode": "years"}))
    specs.append(("myMusicNewMusic", "BROWSE_NEW_MUSIC", "BROWSE_NEW_MUSIC", 50,
                  {"mode": "albums", "sort": "new", "wantMetadata": 1}))
    specs.append(("myMusicMusicFolder", "BROWSE_MUSIC_FOLDER",
                  "BROWSE_MUSIC_FOLDER", 70, {"mode": "bmf"}))
    if has_playlists:
        specs.append(("myMusicPlaylists", "SAVED_PLAYLISTS", "SAVED_PLAYLISTS",
                      80, {"mode": "playlists"}))
    specs.append(("myMusicSearch", "SEARCH", None, 90, {"mode": "search"}))

    nodes: list[dict] = []
    for iid, name_tok, home_tok, weight, params in specs:
        go_params = {"menu": 1, **params}
        node: dict = {
            "text": menu_title(name_tok),
            "id": iid,
            "node": "myMusic",
            "weight": weight,
            "actions": {"go": {"cmd": ["browselibrary", "items"],
                               "params": go_params}},
        }
        if home_tok is not None:
            node["homeMenuText"] = menu_title(home_tok)
        # getJiveMenu only sets ``icon`` from a node's ``jiveIcon``
        # (:697-703); only the artist and works nodes carry one
        # (BrowseLibrary.pm:522,546,618).
        if iid in ("myMusicArtists", "myMusicArtistsAlbumArtists",
                   "myMusicArtistsAllArtists"):
            node["icon"] = "html/images/artists.png"
        elif iid == "myMusicWorks":
            node["icon"] = "html/images/works.png"
        nodes.append(node)
    return nodes


# ── Home-menu items backed by this port (MENU-12) ───────────────────────

def power_node(player_name: str, power_on: bool) -> dict:
    """``playerPower`` (``Jive.pm:2239-2275``) — text switches with the
    player's power state, action ``power 0|1``."""
    token = "JIVE_TURN_PLAYER_OFF" if power_on else "JIVE_TURN_PLAYER_ON"
    return {
        "text": menu_title(token) % (player_name or ""),
        "id": "playerpower",
        "node": "home",
        "weight": 100,
        "actions": {"do": {"player": 0, "cmd": ["power", 0 if power_on else 1]}},
    }


def settings_nodes(*, player_name: str = "", power_on: bool = False,
                   repeat: int = 0, shuffle: int = 0) -> list[dict]:
    """Home ``settings``/``advancedSettings`` items this port can back.

    Emitted are the entries whose command this port implements: the Audio
    node, Repeat/Shuffle (``playlist repeat|shuffle``), Alarm
    (``alarmsettings``), Sleep (``sleepsettings``), Sync (``syncsettings``),
    Crossfade (``crossfadesettings``), ReplayGain (``replaygainsettings``),
    Fixed Volume (``jivefixedvolumesettings``) and Albums Sort Method
    (``jivealbumsortsettings``) — all ``Slim/Control/Jive.pm`` handlers that
    ``_JIVE_QUERY_COMMANDS``/``_JIVE_MENU_COMMANDS`` already serve.

    Deliberately absent (see the module docstring + the task report):
    ``settingsInformation`` (``systeminfo items``), ``settingsPlayerNameChange``
    (``name``), ``settingsRescan``/``settingsDontStopTheMusic`` (plugin menus,
    ``Slim/Plugin/{Rescan,DontStopTheMusic}/Plugin.pm``) and the
    display/device-capability entries (``settingsBass``/``Treble``/``StereoXL``/
    ``LineOut``/brightness/textsize) — this port has no per-model capability
    model, so those would be guesses instead of parity.
    """
    def _go(cmd: str, params: dict | None = None) -> dict:
        go: dict = {"player": 0, "cmd": [cmd]}
        if params:
            go["params"] = params
        return {"go": go}

    def _choices(setting: str, selected: int) -> dict:
        return {"player": 0, "cmd": ["playlist", setting, str(selected)]}

    repeat_strings = [_ucfirst(menu_title(t))
                      for t in ("OFF", "SONG", "PLAYLIST_SHORT")]
    shuffle_strings = [_ucfirst(menu_title(t))
                       for t in ("OFF", "SONG", "ALBUM")]
    return [
        {"text": menu_title("REPEAT"), "id": "settingsRepeat",
         "node": "settings", "weight": 20,
         "choiceStrings": repeat_strings,
         "selectedIndex": int(repeat or 0) + 1,
         "actions": {"do": {"choices": [_choices("repeat", i)
                                         for i in range(3)]}}},
        {"text": menu_title("SHUFFLE"), "id": "settingsShuffle",
         "node": "settings", "weight": 10,
         "choiceStrings": shuffle_strings,
         "selectedIndex": int(shuffle or 0) + 1,
         "actions": {"do": {"choices": [_choices("shuffle", i)
                                         for i in range(3)]}}},
        {"text": menu_title("AUDIO_SETTINGS"), "id": "settingsAudio",
         "node": "settings", "isANode": 1, "weight": 35},
        {"text": menu_title("ALARM"), "id": "settingsAlarm",
         "node": "settings", "weight": 29, "actions": _go("alarmsettings")},
        {"text": menu_title("SLEEP"), "id": "settingsSleep",
         "node": "settings", "weight": 65, "actions": _go("sleepsettings")},
        {"text": menu_title("SYNCHRONIZE"), "id": "settingsSync",
         "node": "settings", "weight": 70, "actions": _go("syncsettings")},
        {"text": menu_title("SETUP_TRANSITIONTYPE"), "id": "settingsXfade",
         "node": "settingsAudio", "weight": 30,
         "iconStyle": "hm_settingsAudio", "actions": _go("crossfadesettings")},
        {"text": menu_title("REPLAYGAIN"), "id": "settingsReplayGain",
         "node": "settingsAudio", "weight": 40,
         "iconStyle": "hm_settingsAudio", "actions": _go("replaygainsettings")},
        {"text": menu_title("FIXED_VOLUME"), "id": "settingsFixedVolume",
         "node": "settingsAudio", "weight": 100,
         "iconStyle": "hm_settingsAudio",
         "actions": _go("jivefixedvolumesettings")},
        {"text": menu_title("ALBUMS_SORT_METHOD"),
         "id": "settingsAlbumSettings", "node": "advancedSettings",
         "weight": 105, "iconStyle": "hm_advancedSettings",
         "actions": _go("jivealbumsortsettings", {"menu": "radio"})},
    ]


# ── yearinfo items (target of the ``more`` action for mode:years) ────────

def year_info_menu(year: Any, index: int = 0, quantity: int = 20) -> dict:
    """``yearinfo items`` (``Slim/Menu/YearInfo.pm``; live Perl 9.1.1).

    Live answer (``yearinfo items 0 20 menu:1 year:2026``): ``count:3``,
    ``title:2026``, ``window.text_list`` and the three playlist-control
    entries "Am Ende hinzufügen" (``item_add``), "Als nächstes wiedergeben"
    (``item_insert``) and "Wiedergabe" (``itemplay``), each carrying
    ``playlistcontrol`` actions with ``{cmd:add|insert|load, year:<year>}``.
    """
    y = str(year)

    def entry(op: str, style: str, text: str, next_window: str,
              extra_actions: tuple[str, ...]) -> dict:
        action = {"player": 0, "cmd": ["playlistcontrol"],
                  "params": {"cmd": op, "year": y}, "nextWindow": next_window}
        item: dict = {"type": "text", "text": text, "style": style,
                      "actions": {k: dict(action) for k in extra_actions},
                      "addAction": "go"}
        return item

    item_loop = [
        entry("add", "item_add", "Am Ende hinzufügen", "parent",
              ("go", "add", "play")),
        entry("insert", "item_insert", "Als nächstes wiedergeben", "parent",
              ("go", "add", "play")),
        entry("load", "itemplay", "Wiedergabe", "nowPlaying", ("go", "play")),
    ]
    base = {
        "actions": {
            "go": {"cmd": ["yearinfo", "items"], "itemsParams": "params",
                   "params": {"menu": "yearinfo"}},
            "play": {"itemsParams": "params", "params": {"menu": "yearinfo"},
                     "nextWindow": "nowPlaying", "player": 0,
                     "cmd": ["yearinfo", "playlist", "play"]},
            "add": {"cmd": ["yearinfo", "playlist", "add"], "player": 0,
                    "params": {"menu": "yearinfo"}, "itemsParams": "params"},
            "add-hold": {"itemsParams": "params",
                         "params": {"menu": "yearinfo"}, "player": 0,
                         "cmd": ["yearinfo", "playlist", "insert"]},
            "more": {"cmd": ["yearinfo", "items"], "player": 0,
                     "window": {"isContextMenu": 1},
                     "params": {"menu": "yearinfo"}, "itemsParams": "params"},
            "playControl": {"itemsParams": "playControlParams",
                            "params": {"_quantity": str(quantity),
                                       "_index": str(index), "menu": "1",
                                       "year": y},
                            "player": 0, "cmd": ["yearinfo", "items"],
                            "window": {"isContextMenu": 1}},
        },
    }
    return {"title": y, "offset": 0, "count": len(item_loop),
            "window": {"windowStyle": "text_list"},
            "item_loop": item_loop, "base": base}
