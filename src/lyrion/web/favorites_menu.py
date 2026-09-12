"""Perl-parity JSON shape for the Favorites browse menu (jive/SqueezePlay).

Why this module exists
----------------------
Perl's ``XMLBrowser`` answers a *menu-mode* browse request (any request
carrying a ``menu:`` token) with three things our old favourites handler
never sent: ``base``, ``window`` and touch-to-play item fields.  Without
them SqueezePlay plays the station but never leaves the list for the
"Aktueller Titel" screen.

The screen switch is decided **client-side** from the response the client
already holds — it is *not* a server-side reaction to the play command:

* ``share/jive/applets/SlimBrowser/SlimBrowserApplet.lua:1774-1779`` —
  the tap action ``go`` is rewritten through ``_actionAliasMap`` onto
  ``item.goAction``.
* ``:1891-1922`` — for ``actionName == 'play'`` the base action table is
  consulted (our touch-to-play item carries no ``actions`` of its own).
* ``:1924-1928`` — ``nextWindow`` precedence: item action > base action >
  item > base.
* ``:2044-2048`` — ``nextWindow == 'nowPlaying'`` → ``_goNowPlaying()``,
  i.e. the Now Playing screen.
* ``:704-767`` (``_performJSONAction``) — the merged command is sent:
  ``base.actions[action].cmd`` + ``from`` + ``qty`` + the item's
  ``params`` (via ``itemsParams``), plus a trailing ``useContextMenu:1``.

Perl reference (READ-ONLY checkout in ``/tmp/lms-ref``, public/9.2):

* ``Slim/Plugin/Favorites/Plugin.pm:747`` ``cliBrowse`` — builds the OPML
  feed; ``:756`` shows that only *definedness* of ``menu`` matters
  (``menu:favorites`` / ``menu:radio`` / ``menu:1`` behave identically).
* ``Slim/Control/XMLBrowser.pm:973-981`` — ``base.actions`` incl.
  ``playControl`` and ``play`` with ``nextWindow => nowPlaying``.
* ``:1033-1147`` — ``isPlayable`` / ``item_id`` / ``isContextMenu``.
* ``:1259-1267`` — ``goAction => 'play'``, ``style => 'itemplay'``,
  ``params.touchToPlay`` / ``touchToPlaySingle``.
* ``:1422-1442`` — ``base.go = base.play`` when *every* item is
  touch-to-play (``$allTouchToPlay``), and ``windowStyle``
  ``home_menu`` (has images) vs. ``text_list``.
* ``:1798-1809`` — ``_jivePresetBase`` (``set-preset-0..9``).

Word-for-word live answers live in ``tests/fixtures/perl_favorites_*
.json``; see ``tests/test_favorites_menu.py`` for the probes.

Documented deviations from Perl
-------------------------------
* ``icon-id`` / ``presetParams.icon`` fall back to
  ``html/images/favorites.png``: Perl proxies the OPML ``image``/``icon``
  attribute, our ``favorites`` table has no icon column.
* ``base.actions.playControl.cmd`` is the literal ``["favorites","items"]``
  from the fixtures instead of being recomputed from the request tokens.
* ``defeatDestructiveTouchToPlay`` (``base.go = base.playControl`` for
  pre-7.6 clients, ``XMLBrowser.pm:1430``) is not implemented.
"""

from __future__ import annotations

from typing import Any

#: Perl ``proxiedImage('html/images/favorites.png')`` — the folder icon and
#: our fallback for stations (no logo column in the ``favorites`` table).
FAVORITES_ICON = "html/images/favorites.png"

MENU = "favorites"

#: ``XMLBrowser.pm:1434-1442`` — image-less feeds render as a plain list.
WINDOW_HOME_MENU: dict[str, Any] = {"windowStyle": "home_menu"}
WINDOW_TEXT_LIST: dict[str, Any] = {"windowStyle": "text_list"}

#: ``window => {isContextMenu => 1}`` on Perl's ``more``/``playControl``.
_IS_CONTEXT_MENU: dict[str, Any] = {"isContextMenu": 1}

#: Default ``menu:`` params every base action carries.
_BASE_PARAMS: dict[str, Any] = {"menu": MENU}


def _play_action() -> dict[str, Any]:
    """``base.actions.play`` — the touch-to-play target.

    ``nextWindow: "nowPlaying"`` is the field SlimBrowserApplet.lua:2044
    turns into the screen switch (Perl: ``_makeAction(..., 'nowPlaying')``).
    """
    return {
        "player": 0,
        "cmd": [MENU, "playlist", "play"],
        "params": dict(_BASE_PARAMS),
        "itemsParams": "params",
        "nextWindow": "nowPlaying",
    }


def base_actions(*, playcontrol_params: dict[str, Any],
                 with_presets: bool = True,
                 all_touch_to_play: bool = False) -> dict[str, dict]:
    """The ``base.actions`` table of a favourites menu response.

    ``playcontrol_params`` is the request's tagged-param copy
    (``XMLBrowser.pm:978`` ``$request->getParamsCopy()``);
    ``with_presets`` mirrors ``_jivePresetBase`` being called only when at
    least one item ships ``presetParams``; ``all_touch_to_play`` mirrors
    ``$allTouchToPlay`` (``:1429-1431``).
    """
    params = dict(_BASE_PARAMS)
    actions: dict[str, dict[str, Any]] = {
        # NOTE: Perl's ``go`` action carries no ``player`` key.
        "go": {"cmd": [MENU, "items"], "itemsParams": "params",
               "params": dict(params)},
        "play": _play_action(),
        "add": {"player": 0, "cmd": [MENU, "playlist", "add"],
                "params": dict(params), "itemsParams": "params"},
        "add-hold": {"player": 0, "cmd": [MENU, "playlist", "insert"],
                     "params": dict(params), "itemsParams": "params"},
        "more": {"player": 0, "cmd": [MENU, "items"],
                 "window": dict(_IS_CONTEXT_MENU),
                 "itemsParams": "params", "params": dict(params)},
        "playControl": {"player": 0, "cmd": [MENU, "items"],
                        "window": dict(_IS_CONTEXT_MENU),
                        "itemsParams": "playControlParams",
                        "params": dict(playcontrol_params)},
    }
    if with_presets:
        for slot in range(10):
            actions[f"set-preset-{slot}"] = {
                "player": 0,
                "cmd": ["jivefavorites", "set_preset", f"key:{slot}"],
                "itemsParams": "presetParams",
            }
    if all_touch_to_play:
        # XMLBrowser.pm:1430 — every row is touch-to-play, so the whole list
        # becomes one big "play" surface.
        actions["go"] = actions["play"]
    return actions


def folder_item(text: str, item_id: str,
                icon_id: str = FAVORITES_ICON) -> dict[str, Any]:
    """A favourites folder row (XMLBrowser.pm:1240-1255).

    Only ``actions.go`` + ``addAction: "go"`` — the tap drills into
    ``favorites items item_id:<this id>``.
    """
    return {
        "text": text,
        "addAction": "go",
        "icon-id": icon_id,
        "actions": {
            "go": {
                "cmd": [MENU, "items"],
                "params": {"menu": MENU, "item_id": str(item_id)},
            },
        },
    }


def audio_item(text: str, item_id: str, *, url: str,
               icon_id: str = FAVORITES_ICON,
               favorites_type: str = "audio") -> dict[str, Any]:
    """A touch-to-play station row (XMLBrowser.pm:1131-1147, 1259-1267).

    No ``actions`` — the play command comes from ``base.actions.play``,
    completed with this item's ``params`` (``itemsParams: "params"``).
    """
    item_id = str(item_id)
    icon = icon_id or FAVORITES_ICON
    return {
        "text": text,
        "type": "audio",
        "style": "itemplay",
        "goAction": "play",
        "icon-id": icon,
        "params": {
            "item_id": item_id,
            "touchToPlay": item_id,
            "touchToPlaySingle": 1,
            "isContextMenu": 1,
        },
        # _favoritesParams(): what the ``set-preset-*`` base actions send.
        "presetParams": {
            "favorites_url": url,
            "favorites_title": text,
            "favorites_type": favorites_type,
            "icon": icon,
        },
    }


def _window_for(items: list[dict]) -> dict[str, Any]:
    """``XMLBrowser.pm:1434-1442``: images ⇒ home_menu, else text_list."""
    if any("icon-id" in it or "icon" in it for it in items):
        return dict(WINDOW_HOME_MENU)
    return dict(WINDOW_TEXT_LIST)


def render_favorites_menu(items: list[dict], *,
                          title: str = "Favorites",
                          playcontrol_params: dict[str, Any] | None = None
                          ) -> dict[str, Any]:
    """Assemble a menu-mode favourites response from Perl-shaped ``items``.

    ``items`` must already come from :func:`folder_item` /
    :func:`audio_item` (the caller owns the tree walk).
    """
    all_touch_to_play = all(it.get("goAction") == "play" for it in items)
    with_presets = any("presetParams" in it for it in items)
    return {
        "window": _window_for(items),
        "offset": 0,
        "count": len(items),
        "item_loop": items,
        "title": title,
        "base": {
            "actions": base_actions(
                playcontrol_params=dict(playcontrol_params or {}),
                with_presets=with_presets,
                all_touch_to_play=all_touch_to_play,
            ),
        },
    }


__all__ = [
    "FAVORITES_ICON",
    "MENU",
    "WINDOW_HOME_MENU",
    "WINDOW_TEXT_LIST",
    "audio_item",
    "base_actions",
    "folder_item",
    "render_favorites_menu",
]
