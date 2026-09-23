"""Don't Stop The Music — the first concrete plugin of this port.

Perl reference (``/tmp/lms-ref``, read-only)
============================================

Ported from ``Slim/Plugin/DontStopTheMusic/Plugin.pm`` with
``Slim/Plugin/RandomPlay/DontStopTheMusic.pm`` as the only provider that ships
with the server.  The functions carried over one-to-one:

* ``initPlugin`` (``Plugin.pm:52-92``) — register the handler set, subscribe to
  playlist changes, dispatch ``dontstopthemusicsetting``.
* ``registerHandler``/``getHandler``/``getSortedHandlerTokens``
  (``Plugin.pm:94-127``).
* ``onPlaylistChange`` (``Plugin.pm:176-223``) — the playlist event filter:
  skip our own requests (``source eq __PACKAGE__``), skip when no provider is
  set, skip when ``repeat`` is on, and delay the start when only one track is
  playing (``:212-216``).
* ``dontStopTheMusic`` (``Plugin.pm:242-324``) — the core: count the tracks
  remaining (``Playlist::count - songIndex - 1``), only act below
  ``MIN_TRACKS_LEFT`` (``:36``), refuse when the last queue entry is a
  repeating stream (``:263-267``) or has no duration (``:272-277``), then add
  the provider's tracks while removing the duplicates.
* ``deDupePlaylist``/``deDupe`` (``Plugin.pm:326-358``).
* ``getMixableProperties`` (``:360-405``) — pick up to five random tracks from
  the current playlist.
* ``getMixablePropertiesFromTrack`` (``:407-441``).
* ``isConflictingPluginActive`` (``Plugin.pm:226-240``) — plugins flagged
  ``canConflictWithDSTM`` in ``install.xml`` can veto via ``disableDSTM()``.
* RandomPlay provider (``Slim/Plugin/RandomPlay/DontStopTheMusic.pm:26-99``):
  ``PLUGIN_RANDOM_TITLEMIX_KEEP_GENRES``, ``PLUGIN_RANDOM_TRACK``, the
  genre-mixing ``mixWithGenres`` and the album/contributor/year variants.

What the port does NOT carry: the ``mysqueezebox.com`` menu plumbing
(``Slim::Control::Jive::registerPluginMenu``, ``Plugin.pm:74-88``) — this port
has no ``myApps`` container (documented gap B3); the settings page of the plugin
(``Settings.pm``) depends on that menu.  The provider pref therefore stays
reachable through the plugin's own settings page
(``/plugins/DontStopTheMusic/settings.html``) and the CLI.
"""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable, Optional

from lyrion.plugins.base import Plugin, PluginMetadata

logger = logging.getLogger(__name__)

#: ``use constant MIN_TRACKS_LEFT => 2`` (``Plugin.pm:36``).
MIN_TRACKS_LEFT = 2

#: ``my $prefs = preferences('plugin.dontstopthemusic')`` (``Plugin.pm:38``) —
#: the pref store this port uses for plugin settings.
PREF_CATEGORY = "plugin.dontstopthemusic"
#: ``$prefs->client($client)->get('provider')`` (``Plugin.pm:110,183``).
PREF_PROVIDER = "provider"
#: ``$prefs->get('newtracks')`` (``Plugin.pm:259``) — server-wide, not per player.
PREF_NEW_TRACKS = "newtracks"

#: ``PLUGIN_DSTM`` (``Slim/Plugin/DontStopTheMusic/strings.txt:1``, EN :5).
STRING_NAME = "PLUGIN_DSTM"
#: ``PLUGIN_DSTM_DESC`` (``strings.txt:12``, EN :16).
STRING_DESC = "PLUGIN_DSTM_DESC"

#: Provider tokens, keys of ``%handlers`` (``Plugin.pm:50``) — the RandomPlay
#: provider registers exactly these (``RandomPlay/DontStopTheMusic.pm:26-66``).
PLUGIN_RANDOM_TITLEMIX_WITH_GENRES = "PLUGIN_RANDOM_TITLEMIX_WITH_GENRES"
PLUGIN_RANDOM_TITLEMIX_KEEP_GENRES = "PLUGIN_RANDOM_TITLEMIX_KEEP_GENRES"
PLUGIN_RANDOM_TRACK = "PLUGIN_RANDOM_TRACK"
PLUGIN_RANDOM_ALBUM_MIX_WITH_GENRES = "PLUGIN_RANDOM_ALBUM_MIX_WITH_GENRES"
PLUGIN_RANDOM_ALBUM_MIX_KEEP_GENRES = "PLUGIN_RANDOM_ALBUM_MIX_KEEP_GENRES"
PLUGIN_RANDOM_ALBUM_ITEM = "PLUGIN_RANDOM_ALBUM_ITEM"
PLUGIN_RANDOM_CONTRIBUTOR_ITEM = "PLUGIN_RANDOM_CONTRIBUTOR_ITEM"
PLUGIN_RANDOM_YEAR_ITEM = "PLUGIN_RANDOM_YEAR_ITEM"

#: ``randomplay://<type>`` (``RandomPlay/DontStopTheMusic.pm:32-65``).
RANDOMPLAY_SCHEMES = {
    PLUGIN_RANDOM_TITLEMIX_KEEP_GENRES: "track",
    PLUGIN_RANDOM_TRACK: "track",
    PLUGIN_RANDOM_ALBUM_MIX_KEEP_GENRES: "album",
    PLUGIN_RANDOM_ALBUM_ITEM: "album",
    PLUGIN_RANDOM_CONTRIBUTOR_ITEM: "contributor",
    PLUGIN_RANDOM_YEAR_ITEM: "year",
}
#: Providers that mix with the *genres of the current playlist* instead of the
#: whole library (``mixWithGenres``, ``RandomPlay/DontStopTheMusic.pm:69-100``).
GENRE_MIX_PROVIDERS = {
    PLUGIN_RANDOM_TITLEMIX_WITH_GENRES: "track",
    PLUGIN_RANDOM_ALBUM_MIX_WITH_GENRES: "album",
}

#: ``$maxPlaylistLength`` handling (``Plugin.pm:294-301``).
#: Perl uses ``preferences('server')->get('maxPlaylistLength')``.
SERVER_PREF_MAX_PLAYLIST_LENGTH = "maxPlaylistLength"

#: How many tracks :meth:`get_mixable_properties` returns at most
#: (``Plugin.pm:388-391`` "pick five random tracks from the playlist").
MAX_MIX_TRACKS = 5


class DontStopTheMusic(Plugin):
    """Continuous playback when the playlist runs out.

    Loaded and enabled by :class:`lyrion.plugins.manager.PluginManager`.
    """

    metadata = PluginMetadata(
        name=STRING_NAME,
        version="1.0.0",                     # install.xml:6
        author="Lyrion Community",           # install.xml:8
        description=STRING_DESC,             # install.xml:7
        id="DontStopTheMusic",               # install.xml:5 name part
        plugin_type="2",                     # install.xml:13 (extension)
    )

    def __init__(self) -> None:
        super().__init__()
        #: ``my %handlers`` (``Plugin.pm:50``) — provider token → callable.
        self.handlers: dict[str, Callable[..., Awaitable[list]]] = {}
        #: ``$conflictingPlugins`` memo (``Plugin.pm:225-233``).
        self._conflicting_plugin_ids: Optional[list[str]] = None

    # ------------------------------------------------------------------
    # Perl ``initPlugin`` (``Plugin.pm:52-92``)
    # ------------------------------------------------------------------

    async def on_startup(self) -> None:
        """Register the RandomPlay provider set — ``Plugin.pm:52-92``.

        Perl registers the providers in the RandomPlay plugin
        (``RandomPlay/DontStopTheMusic.pm:25-67``); the port ships them with the
        plugin itself because RandomPlay is not a plugin here yet (the function
        lives in the core).  The playlist-change subscription
        (``Plugin.pm:91``) and the ``dontstopthemusicsetting`` dispatch
        (``:72``) attach to the core's playlist hook — see
        :meth:`on_playlist_change`.
        """
        self.register_randomplay_providers()
        from lyrion.plugins.manager import PluginManager

        manager = PluginManager()
        manager.register_playlist_change_hook(self.on_playlist_change)
        logger.info("Don't Stop The Music: %d providers registered", len(self.handlers))

    async def on_shutdown(self) -> None:
        from lyrion.plugins.manager import PluginManager

        PluginManager().unregister_playlist_change_hook(self.on_playlist_change)

    # ------------------------------------------------------------------
    # Perl ``registerHandler``/``getHandler``/``getSortedHandlerTokens``
    # (``Plugin.pm:94-127``)
    # ------------------------------------------------------------------

    def register_handler(self, handler_id: str,
                         handler: Callable[..., Awaitable[list]]) -> None:
        """``registerHandler`` (``Plugin.pm:94-97``)."""
        self.handlers[handler_id] = handler

    def unregister_handler(self, handler_id: str) -> None:
        """``unregisterHandler`` (``Plugin.pm:99-102``)."""
        self.handlers.pop(handler_id, None)

    def register_randomplay_providers(self) -> None:
        """The eight providers of ``RandomPlay/DontStopTheMusic.pm:26-66``."""
        self.register_handler(PLUGIN_RANDOM_TITLEMIX_WITH_GENRES,
                              lambda ctx: self._mix_with_genres("track", ctx))
        self.register_handler(PLUGIN_RANDOM_TITLEMIX_KEEP_GENRES,
                              lambda ctx: _ready(["randomplay://track"]))
        self.register_handler(PLUGIN_RANDOM_TRACK,
                              lambda ctx: _ready(["randomplay://track"]))
        self.register_handler(PLUGIN_RANDOM_ALBUM_MIX_WITH_GENRES,
                              lambda ctx: self._mix_with_genres("album", ctx))
        self.register_handler(PLUGIN_RANDOM_ALBUM_MIX_KEEP_GENRES,
                              lambda ctx: _ready(["randomplay://album"]))
        self.register_handler(PLUGIN_RANDOM_ALBUM_ITEM,
                              lambda ctx: _ready(["randomplay://album"]))
        self.register_handler(PLUGIN_RANDOM_CONTRIBUTOR_ITEM,
                              lambda ctx: _ready(["randomplay://contributor"]))
        self.register_handler(PLUGIN_RANDOM_YEAR_ITEM,
                              lambda ctx: _ready(["randomplay://year"]))

    def get_handler(self, provider: str,
                    ctx: Any = None) -> Optional[Callable[..., Awaitable[list]]]:
        """``getHandler`` (``Plugin.pm:104-111``) — handler of the configured provider."""
        return self.handlers.get(provider)

    def get_sorted_handler_tokens(self) -> list[str]:
        """``getSortedHandlerTokens`` (``Plugin.pm:113-127``).

        Perl sorts by the *translated* string (``utf8toLatin1Transliterate``);
        the port sorts by the localized text of the token, which is what the
        menu shows.
        """
        from lyrion.utils.strings import get_string

        return sorted(self.handlers, key=lambda token: get_string(token, default=token))

    # ------------------------------------------------------------------
    # Perl ``onPlaylistChange`` (``Plugin.pm:176-223``)
    # ------------------------------------------------------------------

    async def on_playlist_change(self, event: dict) -> None:
        """React to a playlist event — ``Plugin.pm:176-223``.

        ``event`` carries ``source``, ``command``, ``client`` and ``repeat``.
        """
        client = event.get("client")
        if client is None:
            return
        if event.get("source") == self.__class__.__name__:      # Plugin.pm:182
            return
        provider = await self._provider_for(client)             # :183
        logger.debug("DSTM event=%s provider=%r", event.get("command"), provider)
        if not provider:
            return
        if event.get("command") not in ("newsong", "delete", "cant_open", "resume"):
            return

        # Spotify sometimes fails to load tracks without a newsong (:188-191).
        if event.get("command") == "cant_open":
            url = str(event.get("url") or "")
            error = str(event.get("error") or "")
            if not url.startswith("spotify") or not error.startswith("103"):
                return

        # Don't interfere with automatically adding plugin
        # (RandomPlay, SugarCube, …) — Plugin.pm:194-197.
        conflicting = await self.is_conflicting_plugin_active(client)
        if conflicting:
            logger.warning(
                "Found %s active - I'm not going to interfere with it.", conflicting)
            return

        # create mix if we near the end, repeat is off — Plugin.pm:205-222.
        if int(event.get("repeat") or 0):
            return
        song_index = int(event.get("song_index") or 0)
        if song_index == 0:
            return                       # :212-216 delay rule: wait for the next song
        await self.dont_stop_the_music(client)

    # ------------------------------------------------------------------
    # Perl ``isConflictingPluginActive`` (``Plugin.pm:226-240``)
    # ------------------------------------------------------------------

    async def is_conflicting_plugin_active(self, client: Any) -> Optional[str]:
        """A conflicting plugin is active when it answers ``disableDSTM()`` true."""
        from lyrion.plugins.manager import PluginManager

        manager = PluginManager()
        if self._conflicting_plugin_ids is None:
            self._conflicting_plugin_ids = [
                plugin_id for plugin_id in manager.manifest_names()
                if manager.data_for_plugin(plugin_id).get("canConflictWithDSTM")
            ]
        for plugin_id in self._conflicting_plugin_ids:
            plugin = manager.get_plugin(plugin_id)
            if plugin is None or not plugin.enabled:
                continue
            veto = getattr(plugin, "disable_dstm", None)
            if veto is None:
                continue
            try:
                if await veto(client):
                    return plugin_id
            except Exception as exc:  # noqa: BLE001 — Perl evals each call (:236)
                logger.warning("DSTM: disableDSTM of %s raised: %s", plugin_id, exc)
        return None

    # ------------------------------------------------------------------
    # Perl ``dontStopTheMusic`` (``Plugin.pm:242-324``)
    # ------------------------------------------------------------------

    async def dont_stop_the_music(self, client: Any) -> list:
        """Add similar tracks when the playlist is about to end.

        Returns the URL list that was appended (empty when nothing happened).
        """
        from lyrion.plugins.manager import PluginManager

        # don't process multiple requests at the same time (Plugin.pm:249-250).
        if self._client_active(client):
            return []

        player = client
        song_index = self._streaming_song_index(player)             # :254
        total = len(getattr(player, "playlist", []) or [])
        songs_remaining = total - song_index - 1                    # :255

        logger.info("%d songs remaining, songIndex = %d", songs_remaining, song_index)

        num_tracks = await self._new_tracks_pref()                  # :259
        if songs_remaining >= num_tracks:
            return []

        # don't continue when the last item is a radio station (:262-267)
        if self._last_item_is_repeating_stream(player):
            return []

        if await self._last_track_duration(player) is None:         # :272-277
            logger.info("Found radio station last in the queue - don't start a mix.")
            return []

        provider = await self._provider_for(getattr(player, "master", player))
        handler = self.get_handler(provider, player)
        if handler is None:
            return []

        self._set_client_active(player, True)
        try:
            context = _ProviderContext(
                client=player,
                plugin=self,
                song_index=song_index,
                streaming_song_index=song_index,
            )
            tracks = await handler(context)
            tracks = await self.de_dupe_playlist(player, tracks)    # :290

            if tracks:
                max_length = self._max_playlist_length()            # :294
                if max_length and total + len(tracks) > max_length:
                    await self._trim_playlist(player, len(tracks))  # :296-301
                added = await self._add_tracks(player, tracks)       # :304-307
                return added
            elif not provider.startswith("PLUGIN_RANDOM") \
                    and PluginManager().is_enabled("RandomPlay"):    # :309-314
                logger.warning(
                    "I'm sorry, we couldn't create any reasonable result with "
                    "your current playlist. We'll just play something instead.")
                return await self._add_tracks(player, ["randomplay://track"])
            logger.info("No matching tracks found for current playlist!")   # :315-317
            return []
        finally:
            self._set_client_active(player, False)

    # ------------------------------------------------------------------
    # Perl ``deDupePlaylist``/``deDupe`` (``Plugin.pm:326-358``)
    # ------------------------------------------------------------------

    async def de_dupe_playlist(self, client: Any, tracks: list) -> list:
        """Remove candidates already in the playlist — ``Plugin.pm:326-345``."""
        if not tracks:
            return []
        seen = {
            _item_url(item) for item in (getattr(client, "playlist", []) or [])
        }
        return self.de_dupe(tracks, seen)

    @staticmethod
    def de_dupe(tracks: list, seen: set) -> list:
        """``deDupe`` (``Plugin.pm:347-358``) — keep first occurrences only."""
        out: list = []
        for track in tracks or []:
            key = _item_url(track)
            if key in seen:
                continue
            seen.add(key)
            out.append(track)
        return out

    # ------------------------------------------------------------------
    # Perl ``getMixableProperties`` (``Plugin.pm:360-405``)
    # ------------------------------------------------------------------

    async def get_mixable_properties(self, client: Any,
                                     count: int = MAX_MIX_TRACKS) -> Optional[list[dict]]:
        """Metadata of up to ``count`` random tracks of the current playlist."""
        if client is None:
            return None
        player = getattr(client, "master", client)
        properties: list[dict] = []
        duration = 0
        for track in (getattr(player, "playlist", []) or []):
            info = await self.get_mixable_properties_from_track(player, track)
            if info is None:
                continue
            artist, title, duration, track_id = info
            if artist is None or title is None:                 # :372
                continue
            properties.append({
                "id": track_id,
                "artist": artist,
                "title": title,
                "url": _item_url(track),
            })

        if properties and duration:                             # :384
            logger.info("Auto-mixing from random tracks in current playlist")
            if count and len(properties) > count:
                # Perl ``Slim::Player::Playlist::fischer_yates_shuffle`` (:389).
                from lyrion.player.manager import fischer_yates_shuffle

                fischer_yates_shuffle(properties)
                del properties[count:]
            return properties

        if not duration:
            logger.info("Found radio station last in the queue - don't start a mix.")
        else:
            logger.info("No mixable items found in current playlist!")
        return None

    # ------------------------------------------------------------------
    # Perl ``getMixablePropertiesFromTrack`` (``Plugin.pm:407-441``)
    # ------------------------------------------------------------------

    async def get_mixable_properties_from_track(self, client: Any, track: Any):
        """``(artist, title, duration, id)`` of a playlist entry, or ``None``."""
        if client is None or track is None:
            return None
        player = getattr(client, "master", client)
        if isinstance(track, int):
            track = await self._track_by_id(track)
            if track is None:
                return None
        artist = getattr(track, "artist", None)
        title = getattr(track, "title", None)
        duration = getattr(track, "duration", None)
        track_id = getattr(track, "id", None)
        return (artist, title, duration, track_id)

    @staticmethod
    async def _track_by_id(track_id: int):
        """Track entity by id (Perl ``Slim::Schema->objectForUrl``/``track``)."""
        from sqlalchemy import select

        from lyrion.database.schema import Track
        from lyrion.database.sqlite_helper import db_session

        try:
            async with db_session() as session:
                return (await session.execute(
                    select(Track).where(Track.id == track_id)
                )).scalar_one_or_none()
        except Exception as exc:  # noqa: BLE001 — a mix must not kill playback
            logger.debug("DSTM: track lookup for %s failed: %s", track_id, exc)
            return None

    # ------------------------------------------------------------------
    # ``mixWithGenres`` (``RandomPlay/DontStopTheMusic.pm:69-100``)
    # ------------------------------------------------------------------

    async def _mix_with_genres(self, mix_type: str, ctx: "_ProviderContext") -> list:
        """``randomplay://<type>?genres=<genres of the playlist>``.

        Perl collects the genres of every playlist track (``:74-90``), escapes
        them and appends ``?genres=…`` (``:92-99``).  The port keeps the same
        query shape; the RandomPlay protocol handler of this port is what
        resolves it (``music/radio``-style scheme resolution is a later task —
        see the class docstring "UNKLAR").
        """
        from urllib.parse import quote

        player = ctx.client
        genres: dict[str, int] = {}
        for track in (getattr(player, "playlist", []) or []):
            entity = track
            if isinstance(track, int):
                entity = await self._track_by_id(track)
            if entity is None:
                continue
            genre = getattr(entity, "genre", None)
            if genre:
                genres[str(genre)] = genres.get(str(genre), 0) + 1
        query = ""
        if genres:
            query = "?genres=" + ",".join(quote(g, safe="") for g in genres)
        return [f"randomplay://{mix_type}{query}"]

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    async def _provider_for(self, client: Any) -> str:
        """The provider pref of ``client``'s master (``Plugin.pm:110,183``)."""
        pref = await self._client_pref(client, PREF_PROVIDER)
        return str(pref or "")

    async def _client_pref(self, client: Any, name: str):
        """Client-scoped plugin pref (Perl ``$prefs->client($client)->get``).

        Perls ``playerpref``-Kommando legt den Wert in ``$client->prefs`` ab
        (``Commands.pm:2631-2677``); unser ``PlayerState.playerprefs`` ist
        genau dieser Satz (``player/manager.py:616-618``).
        """
        player = getattr(client, "master", client)
        prefs = getattr(player, "playerprefs", None) or {}
        if f"{PREF_CATEGORY}:{name}" in prefs:
            return prefs[f"{PREF_CATEGORY}:{name}"]
        if name in prefs:
            return prefs[name]
        from lyrion.config import get_prefs

        return get_prefs().get(f"{PREF_CATEGORY}:{name}", "")

    async def _new_tracks_pref(self) -> int:
        """``$prefs->get('newtracks') || MIN_TRACKS_LEFT`` (``Plugin.pm:259``)."""
        from lyrion.config import get_prefs

        value = get_prefs().get(PREF_NEW_TRACKS, 0)
        try:
            number = int(value)
        except (TypeError, ValueError):
            number = 0
        return number or MIN_TRACKS_LEFT

    def _max_playlist_length(self) -> int:
        """``preferences('server')->get('maxPlaylistLength')`` (``Plugin.pm:294``)."""
        from lyrion.config import get_prefs

        try:
            return int(get_prefs().get(SERVER_PREF_MAX_PLAYLIST_LENGTH, 0) or 0)
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _streaming_song_index(player: Any) -> int:
        """``Playlist::streamingSongIndex`` — queue index of the playing song."""
        index = getattr(player, "playlist_position", None)
        if index is None:
            index = getattr(player, "current_song_index", 0)
        try:
            return int(index or 0)
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _last_item_is_repeating_stream(player: Any) -> bool:
        """``isRepeatingStream`` check of the last queue entry (``Plugin.pm:263-267``).

        A radio/stream entry in the queue is a ``str`` URL in this port
        (``PlayerState.playlist`` holds ids *and* stream URLs, ``state.py:183``);
        ``randomplay://`` entries are the non-repeating RandomPlay pseudo-URLs
        (``RandomPlay/Plugin.pm``), everything with ``://`` is treated as a
        stream — the port's own RemoteStream check.
        """
        playlist = getattr(player, "playlist", []) or []
        if not playlist:
            return False
        last = playlist[-1]
        if isinstance(last, str):
            return "://" in last and not last.startswith("randomplay://")
        return False

    async def _last_track_duration(self, player: Any) -> Optional[float]:
        """Duration of the last queue entry (``Plugin.pm:272``).

        ``undef``/``0`` → Perl treats the queue as radio (``:274-277``).  A
        stream entry in this port's playlist is a ``str`` URL and has no
        duration; a track id is looked up in the library
        (``getMixablePropertiesFromTrack``, ``:407-441``).
        """
        playlist = getattr(player, "playlist", []) or []
        if not playlist:
            return None
        last = playlist[-1]
        if isinstance(last, str):
            return None
        if isinstance(last, int):
            track = await self._track_by_id(last)
            if track is None:
                return None
            duration = getattr(track, "duration", None)
            try:
                return float(duration) if duration else None
            except (TypeError, ValueError):
                return None
        return None

    def _client_active(self, player: Any) -> bool:
        """``$client->pluginData('active')`` (``Plugin.pm:250``)."""
        return bool(getattr(player, "_dstm_active", False))

    def _set_client_active(self, player: Any, active: bool) -> None:
        try:
            player._dstm_active = bool(active)
        except AttributeError:                # frozen/slotted player — no-op
            pass

    async def _trim_playlist(self, player: Any, n_tracks: int) -> None:
        """Delete ``n_tracks`` entries from the front (``Plugin.pm:296-301``)."""
        from lyrion.player.manager import PlayerManager

        player_id = _player_id(player)
        for _ in range(n_tracks):
            if not PlayerManager().playlist_remove(player_id, 0):
                break

    async def _add_tracks(self, player: Any, tracks: list) -> list:
        """``playlist add`` / ``playlist addtracks`` (``Plugin.pm:304-307``)."""
        from lyrion.player.manager import PlayerManager

        manager = PlayerManager()
        player_id = _player_id(player)
        added: list = []
        for track in tracks:
            if isinstance(track, int):
                if manager.playlist_add(player_id, track):
                    added.append(track)
                continue
            _add_stream(player, track)
            added.append(track)
        return added


class _ProviderContext:
    """The ``($client, $cb)`` pair Perl hands to a provider handler."""

    __slots__ = ("client", "plugin", "song_index", "streaming_song_index")

    def __init__(self, *, client: Any, plugin: DontStopTheMusic, song_index: int,
                 streaming_song_index: int) -> None:
        self.client = client
        self.plugin = plugin
        self.song_index = song_index
        self.streaming_song_index = streaming_song_index


async def _ready(tracks: list) -> list:
    """Provider that has its result immediately (``$cb->($client, [...])``)."""
    return list(tracks)


def _item_url(item: Any) -> Any:
    """Perl ``blessed($_) ? $_->url : $_`` (``Plugin.pm:335``)."""
    if isinstance(item, (int, str)):
        return item
    url = getattr(item, "url", None)
    return url if url is not None else item


def _player_id(player: Any) -> str:
    for attr in ("mac", "id", "player_id"):
        value = getattr(player, attr, None)
        if value:
            return str(value)
    return ""


def _add_stream(player: Any, url: str) -> None:
    """Append a stream URL to the player's playlist (``state.py:183``)."""
    playlist = getattr(player, "playlist", None)
    if playlist is None:
        return
    if url not in playlist:
        playlist.append(url)
    try:
        player.playlist_total = len(playlist)
    except AttributeError:
        pass


def get_plugin() -> DontStopTheMusic:
    """Factory the manager uses to build the plugin instance.

    Perl's counterpart is the module itself being required
    (``PluginManager.pm:323`` ``tryModuleLoad($module)``).
    """
    return DontStopTheMusic()


def disable_dstm(client: Any = None) -> bool:
    """``canConflictWithDSTM`` veto hook (``Plugin.pm:226-240``).

    Only plugins flagged ``<canConflictWithDSTM>1</canConflictWithDSTM>`` in
    their ``install.xml`` need this; DSTM itself never vetoes itself.
    """
    return False

