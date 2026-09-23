"""Plugin-Verwaltung: Register, Zustandsautomat, Web-Seite und das DSTM-Plugin.

Perl-Belege (``/tmp/lms-ref``, read-only)
=========================================

**Manifest-Register** (``Slim/Utils/PluginManager.pm``):

* ``_findInstallManifests`` :631-654 — ``install.xml`` unter ``dirsFor('Plugins')``,
  ``$mtimesum += (stat($file))[9]``.
* ``_parseInstallManifest`` :675-796 — Name aus ``<module>``
  (``Slim::Plugin::X::Plugin`` → ``X``, :697-699) bzw. dem Verzeichnis (:703),
  ``defaultState`` → Pref (:711-728), ``basedir`` (:730), Grading
  ``INSTALLERROR_NO_MODULE``/:734, ``INSTALLERROR_INVALID_VERSION``/:738,
  ``INSTALLERROR_INCOMPATIBLE_PLATFORM``/:783.
* Cache ``plugin-data.yaml`` + ``__cacheinfo`` :586-629; Invalidierung über
  ``version``/``count``/``mtimesum``/``server`` :131-165; ``CACHE_VERSION => 4`` :41.

**Zustandsautomat**: gültige Werte ``disabled``/``enabled``/``needs-enable``/
``needs-disable``/``needs-install``/``needs-uninstall`` (:32); Pending-Ops in
``init`` (:88-114); ``enablePlugin`` → ``needs-enable`` (:535-545);
``disablePlugin`` → ``needs-disable``, von ``enforce`` verhindert (:547-563);
``_needsEnable``/``_needsDisable`` lösen auf (:822-838); ``needsRestart``
(:565-567); ``message`` → ``PLUGINS_RESTART_MSG`` (:569-585).

**Web-Seite** ``settings/server/plugins.html``: ``Plugins.pm:40-46``
(``SETUP_PLUGINS``, ``page``), ``:91-94`` (``manual:<plugin>`` → enable/disable),
``:339-350`` (Restart-Hinweis ``PLUGINS_CHANGED_NEED_RESTART``/
``SETUP_EXTENSIONS_RESTART_MSG``); unbekannte Settings-Pfade bleiben 404
(``Slim/Web/HTTP.pm:1128-1131``).

**DSTM-Plugin**: ``Slim/Plugin/DontStopTheMusic/Plugin.pm`` — ``MIN_TRACKS_LEFT``
:36, Provider-Registry :94-127, ``dontStopTheMusic`` :242-324, ``deDupe``
:347-358, ``getMixableProperties`` :360-405; Provider
``Slim/Plugin/RandomPlay/DontStopTheMusic.pm:26-66``; ``install.xml:4-18``.
"""

from __future__ import annotations

import asyncio

import aiosqlite
import httpx
import pytest

from lyrion.config import _PREF_DB_SCHEMA, get_prefs
from lyrion.plugins.manager import (
    STATE_DISABLED,
    STATE_ENABLED,
    STATE_NEEDS_DISABLE,
    STATE_NEEDS_ENABLE,
    PREF_NAMESPACE,
    PluginManager,
)
from lyrion.plugins.registry import (
    CACHE_VERSION,
    INSTALLERROR_SUCCESS,
    ManifestRegistry,
    check_plugin_version,
    compare_versions,
)
from lyrion.plugins.dontstopthemusic import DontStopTheMusic, MIN_TRACKS_LEFT
from lyrion.web.app import create_app

PLUGINS_DIR = __import__("pathlib").Path(__file__).resolve().parents[1] / "src" / "lyrion" / "plugins"


@pytest.fixture(scope="module", autouse=True)
def _prefs_store():
    """Globalen Prefs-Store auf eine In-Memory-DB hängen und danach schließen.

    Ohne das Schließen hängt aiosqlites Worker-Thread am Prozessende und pytest
    beendet sich nicht mehr (siehe ``tests/test_web_settings.py:55-80``).
    """
    store = get_prefs()
    opened = False
    if getattr(store, "_db", None) is None:
        async def _open() -> None:
            store._db = await aiosqlite.connect(":memory:")
            store._db.row_factory = aiosqlite.Row
            await store._db.executescript(_PREF_DB_SCHEMA)

        asyncio.run(_open())
        opened = True
    yield store
    db = getattr(store, "_db", None)
    if opened and db is not None:
        try:
            asyncio.run(db.close())
        except Exception:  # noqa: BLE001 — Aufräumen darf nicht scheitern
            pass
        store._db = None


@pytest.fixture
def manager():
    """Frischer Manager mit dem mitgelieferten Plugin-Verzeichnis.

    Der Singleton wird zwischen den Tests auf den Ausgangszustand gesetzt, damit
    Zustände/Prefs nicht überlaufen (gleiches Muster wie ``PlayerManager`` in
    ``tests/test_web_settings.py:83-94``).
    """
    mgr = PluginManager()
    old_dirs = list(mgr.plugin_dirs)
    old_states = {k: get_prefs().get(k) for k in get_prefs().keys()
                  if k.startswith(PREF_NAMESPACE + ":")}
    mgr.plugin_dirs = [PLUGINS_DIR]
    mgr.registry = ManifestRegistry(mgr.plugin_dirs)
    yield mgr
    mgr.plugin_dirs = old_dirs
    mgr.registry = ManifestRegistry(old_dirs)
    mgr.plugins = {}
    mgr.loaded = {}
    mgr.enabled_plugin_names = set()
    mgr.disabled = set()

    async def _restore():
        for key in list(get_prefs().keys()):
            if key.startswith(PREF_NAMESPACE + ":"):
                await get_prefs().set(key, "")
        for key, value in old_states.items():
            await get_prefs().set(key, value)

    asyncio.run(_restore())


def _request(method: str, path: str, *, data=None) -> httpx.Response:
    """Eine Anfrage gegen die ASGI-App (in-process, kein Serverstart)."""
    async def run() -> httpx.Response:
        transport = httpx.ASGITransport(app=create_app())
        async with httpx.AsyncClient(transport=transport,
                                     base_url="http://testserver") as client:
            return await client.request(method, path, data=data)

    return asyncio.run(run())


def _set_state(name: str, state: str) -> None:
    async def run():
        prefs = get_prefs()
        key = f"{PREF_NAMESPACE}:{name}"
        await prefs.init_preference(key, default="", category=PREF_NAMESPACE)
        await prefs.set(key, state)

    asyncio.run(run())


# ── Register (install.xml) ──────────────────────────────────────────────────

def test_registry_finds_the_shipped_install_xml():
    """``_findInstallManifests`` :631-654 — genau die mitgelieferte Datei."""
    registry = ManifestRegistry([PLUGINS_DIR])
    files, mtimesum = registry.find_install_manifests()

    assert [f.name for f in files] == ["install.xml"]
    assert files[0].parent.name == "DontStopTheMusic"
    assert mtimesum > 0                     # ``$mtimesum += (stat($file))[9]`` :649


def test_parse_manifest_reads_all_perl_fields():
    """``_parseInstallManifest`` :675-796 + ``install.xml:4-18``."""
    registry = ManifestRegistry([PLUGINS_DIR])
    files, _ = registry.find_install_manifests()
    manifest = registry.parse_manifest(files[0])

    assert manifest is not None
    assert manifest.name == "DontStopTheMusic"                    # :697-699
    assert manifest.module == "Slim::Plugin::DontStopTheMusic::Plugin"
    assert manifest.version == "1.0.0"
    assert manifest.description == "PLUGIN_DSTM_DESC"
    assert manifest.creator == "Lyrion Community"
    assert manifest.plugin_type == "2"
    assert manifest.min_version == "7.9"                          # :806-807
    assert manifest.max_version == "*"
    assert manifest.enforce is False
    assert manifest.default_state == "enabled"
    assert manifest.error == INSTALLERROR_SUCCESS                 # :791


def test_manifest_without_module_gets_installerror_no_module(tmp_path):
    """``PluginManager.pm:732-734`` — fehlendes ``<module>``."""
    plugin_dir = tmp_path / "Broken"
    plugin_dir.mkdir()
    (plugin_dir / "install.xml").write_text(
        "<?xml version='1.0'?><extension><name>BROKEN</name>"
        "<targetApplication><minVersion>7.0</minVersion>"
        "<maxVersion>*</maxVersion></targetApplication></extension>",
        encoding="utf-8")

    registry = ManifestRegistry([tmp_path])
    manifest = registry.parse_manifest(plugin_dir / "install.xml")

    assert manifest is not None
    assert manifest.error == "INSTALLERROR_NO_MODULE"


def test_manifest_with_wrong_target_version_is_invalid(tmp_path):
    """``_checkPluginVersion`` :798-820 — min/max gegen ``$::VERSION``."""
    plugin_dir = tmp_path / "Future"
    plugin_dir.mkdir()
    (plugin_dir / "install.xml").write_text(
        "<?xml version='1.0'?><extension><name>FUTURE</name>"
        "<module>Slim::Plugin::Future::Plugin</module>"
        "<targetApplication><minVersion>10.0</minVersion>"
        "<maxVersion>*</maxVersion></targetApplication></extension>",
        encoding="utf-8")

    registry = ManifestRegistry([tmp_path])
    manifest = registry.parse_manifest(plugin_dir / "install.xml")

    assert manifest is not None
    assert manifest.error == "INSTALLERROR_INVALID_VERSION"


def test_version_compare_and_check_plugin_version():
    """``compareVersions``/``checkVersion`` — ``*`` heißt jede Version (:807)."""
    assert compare_versions("9.0.0", "7.9") > 0
    assert compare_versions("7.0a", "7.0") == 0
    assert compare_versions("6.5", "7.0") < 0

    registry = ManifestRegistry([PLUGINS_DIR])
    files, _ = registry.find_install_manifests()
    manifest = registry.parse_manifest(files[0])
    assert check_plugin_version(manifest, server_version="9.0.0")
    assert check_plugin_version(manifest, server_version="8.5.0")
    assert not check_plugin_version(manifest, server_version="7.0.0")


def test_cache_roundtrip_and_invalidation(tmp_path):
    """Cache ``_writePluginCache``/``_loadPluginCache`` :592-629, :116-187."""
    cache_file = tmp_path / "plugin-data.json"
    registry = ManifestRegistry([PLUGINS_DIR])
    registry.read(use_cache=True, cache_file=cache_file)

    assert cache_file.is_file()
    payload = __import__("json").loads(cache_file.read_text(encoding="utf-8"))
    assert payload["__cacheinfo"]["version"] == CACHE_VERSION    # :41
    assert payload["__cacheinfo"]["count"] == 1

    # Ein zweiter Lauf liest den Cache (kein Reparse-Grund).
    again = ManifestRegistry([PLUGINS_DIR])
    again.read(use_cache=True, cache_file=cache_file)
    assert again.cache_invalid_reason == ""
    assert again.manifests["DontStopTheMusic"].version == "1.0.0"

    # Geänderte mtimesum → Cache ungültig (:145-146).
    payload["__cacheinfo"]["mtimesum"] = -1
    cache_file.write_text(__import__("json").dumps(payload), encoding="utf-8")
    third = ManifestRegistry([PLUGINS_DIR])
    third.read(use_cache=True, cache_file=cache_file)
    assert third.cache_invalid_reason == "manifest checksum differs"


# ── Zustandsautomat (Prefs) ─────────────────────────────────────────────────

def test_first_sight_seeds_default_state(manager):
    """``PluginManager.pm:711-728`` — ``defaultState`` → Pref."""
    async def run():
        return await manager.ensure_plugin_state("DontStopTheMusic", "enabled")

    assert asyncio.run(run()) == STATE_ENABLED
    assert manager.plugin_state("DontStopTheMusic") == STATE_ENABLED


def test_default_state_disabled_seeds_disabled(manager):
    """``PluginManager.pm:720-723`` — ``<defaultState>disabled``."""
    async def run():
        return await manager.ensure_plugin_state("SomePlugin", "disabled")

    assert asyncio.run(run()) == STATE_DISABLED


def test_disable_writes_needs_disable_and_reports_restart(manager):
    """``PluginManager.pm:547-563,565-585``."""
    manager.discover_plugins(use_cache=False)
    asyncio.run(manager.ensure_plugin_state("DontStopTheMusic", "enabled"))

    async def run():
        changed = await manager.disable_plugin("DontStopTheMusic")
        return changed

    assert asyncio.run(run()) is True
    assert manager.plugin_state("DontStopTheMusic") == STATE_NEEDS_DISABLE
    assert manager.needs_restart() is True                       # :565-567
    message = manager.message()
    assert message is not None
    assert message.startswith("Plugins have been updated - Restart Required")
    assert "DontStopTheMusic" in message                         # :578-581


def test_enforce_plugin_cannot_be_disabled(manager, tmp_path):
    """``PluginManager.pm:551-556`` — ``<enforce>`` verhindert Deaktivieren."""
    plugin_dir = tmp_path / "Enforced"
    plugin_dir.mkdir()
    (plugin_dir / "install.xml").write_text(
        "<?xml version='1.0'?><extension><name>Enforced</name>"
        "<module>Slim::Plugin::Enforced::Plugin</module>"
        "<enforce>1</enforce>"
        "<targetApplication><minVersion>7.0</minVersion>"
        "<maxVersion>*</maxVersion></targetApplication></extension>",
        encoding="utf-8")
    manager.plugin_dirs = [tmp_path]
    manager.registry = ManifestRegistry([tmp_path])
    manager.discover_plugins(use_cache=False)
    asyncio.run(manager.ensure_plugin_state("Enforced", "enabled"))

    async def run():
        return await manager.disable_plugin("Enforced")

    assert asyncio.run(run()) is False
    assert manager.plugin_state("Enforced") == STATE_ENABLED


def test_resolve_pending_states_enables_and_disables(manager):
    """``PluginManager.pm:88-114`` + ``_needsEnable``/``_needsDisable`` :822-838."""
    # ``ensure_plugin_state`` ist für das erste Sehen eines Manifests
    # (``PluginManager.pm:711-728``); die Pending-Werte selbst schreibt
    # ``$prefs->set`` (enablePlugin/disablePlugin, :535-563).
    asyncio.run(manager.seed_plugin_state("PluginA", STATE_NEEDS_ENABLE))
    asyncio.run(manager.seed_plugin_state("PluginB", STATE_NEEDS_DISABLE))

    async def run():
        return await manager.resolve_pending_states()

    resolved = asyncio.run(run())
    assert resolved["PluginA"] == STATE_ENABLED
    assert resolved["PluginB"] == STATE_DISABLED
    assert manager.plugin_state("PluginA") == STATE_ENABLED
    assert manager.plugin_state("PluginB") == STATE_DISABLED
    assert manager.needs_restart() is False


# ── Laden und Start (PluginManager.pm:190-410) ──────────────────────────────

def test_startup_loads_the_shipped_plugin(manager):
    """``startup`` = ``init``/``load`` + drei Init-Pässe (:383-404)."""
    async def run():
        await manager.startup(use_cache=False)
        return manager

    asyncio.run(run())

    assert "DontStopTheMusic" in manager.plugins
    assert manager.is_enabled("DontStopTheMusic")
    plugin = manager.get_plugin("DontStopTheMusic")
    assert isinstance(plugin, DontStopTheMusic)
    assert len(plugin.handlers) == 8                     # RandomPlay :26-66

    async def stop():
        await manager.shutdown()

    asyncio.run(stop())


def test_disabled_plugin_is_not_loaded(manager):
    """``PluginManager.pm:232-235`` — ``disabled`` wird übersprungen."""
    manager.discover_plugins(use_cache=False)
    asyncio.run(manager.ensure_plugin_state("DontStopTheMusic", STATE_DISABLED))

    async def run():
        await manager.startup(use_cache=False)

    asyncio.run(run())
    assert "DontStopTheMusic" not in manager.plugins
    assert "DontStopTheMusic" in manager.disabled


def test_pending_disable_wins_over_load(manager):
    """Deaktivieren → ``needs-disable``; nach Neustart (``startup``) nicht geladen."""
    manager.discover_plugins(use_cache=False)
    asyncio.run(manager.ensure_plugin_state("DontStopTheMusic", STATE_ENABLED))
    asyncio.run(manager.disable_plugin("DontStopTheMusic"))

    async def run():
        await manager.startup(use_cache=False)

    asyncio.run(run())
    assert manager.plugin_state("DontStopTheMusic") == STATE_DISABLED
    assert "DontStopTheMusic" not in manager.plugins


def test_enable_after_disable_loads_again(manager):
    """Aktivieren umgekehrt: ``needs-enable`` → ``enabled`` → geladen."""
    manager.discover_plugins(use_cache=False)
    asyncio.run(manager.ensure_plugin_state("DontStopTheMusic", STATE_DISABLED))

    async def cycle():
        await manager.startup(use_cache=False)
        assert "DontStopTheMusic" not in manager.plugins
        await manager.enable_plugin("DontStopTheMusic")
        assert manager.plugin_state("DontStopTheMusic") == STATE_NEEDS_ENABLE
        await manager.startup(use_cache=False)

    asyncio.run(cycle())
    assert "DontStopTheMusic" in manager.plugins


# ── Das portierte Plugin wirkt ──────────────────────────────────────────────

class _FakeTrack:
    """Minimaler Track wie ``schema.Track`` für die DSTM-Logik."""

    def __init__(self, track_id, duration=0.0, genre=None, artist="A", title="T"):
        self.id = track_id
        self.duration = duration
        self.genre = genre
        self.artist = artist
        self.title = title


class _FakePlayer:
    """Minimaler Player wie ``PlayerState`` für die DSTM-Logik."""

    def __init__(self, playlist, position):
        self.playlist = list(playlist)
        self.playlist_position = position
        self.mac = "1C:87:2C:47:FC:36"
        self.master = self
        self.playerprefs = {}
        self.playlist_total = len(self.playlist)
        self.added = []


def test_dstm_appends_a_mix_when_the_playlist_runs_out(monkeypatch):
    """``Plugin.pm:242-324`` — bei ``songsRemaining < numTracks`` wird gefüllt."""
    plugin = DontStopTheMusic()
    # Provider werden in ``initPlugin`` registriert (``Plugin.pm:52-92`` bzw.
    # ``RandomPlay/DontStopTheMusic.pm:25-67``) — hier der Aufruf ohne Manager.
    plugin.register_randomplay_providers()

    async def fake_add(player, tracks):
        player.added.extend(tracks)
        return list(tracks)

    monkeypatch.setattr(plugin, "_add_tracks", fake_add)

    async def fake_track(track_id):
        # Perl ``getMixablePropertiesFromTrack`` (:407-441): der letzte Eintrag
        # braucht eine Dauer, sonst gilt die Warteschlange als Radio (:274-277).
        return _FakeTrack(track_id, duration=180.0)

    monkeypatch.setattr(plugin, "_track_by_id", fake_track)

    async def run():
        # 2 Einträge, Position 1 → songsRemaining = 0 < MIN_TRACKS_LEFT (:255,261)
        player = _FakePlayer([11, 12], 1)
        player.playerprefs["plugin.dontstopthemusic:provider"] = "PLUGIN_RANDOM_TRACK"
        return await plugin.dont_stop_the_music(player), player

    added, player = asyncio.run(run())
    assert added == ["randomplay://track"]          # RandomPlay/DSTM.pm:35-39
    assert player.added == ["randomplay://track"]


def test_dstm_does_nothing_when_enough_tracks_remain(monkeypatch):
    """``Plugin.pm:261-262`` — erst unter ``MIN_TRACKS_LEFT`` eingreifen."""
    plugin = DontStopTheMusic()

    async def fake_add(player, tracks):
        player.added.extend(tracks)
        return list(tracks)

    monkeypatch.setattr(plugin, "_add_tracks", fake_add)

    async def run():
        player = _FakePlayer(list(range(10)), 1)     # 8 übrig ≥ 2
        player.playerprefs["plugin.dontstopthemusic:provider"] = "PLUGIN_RANDOM_TRACK"
        return await plugin.dont_stop_the_music(player), player

    added, player = asyncio.run(run())
    assert added == []
    assert player.added == []
    assert MIN_TRACKS_LEFT == 2                      # Plugin.pm:36


def test_dstm_gets_no_mix_without_a_provider(monkeypatch):
    """``Plugin.pm:281`` — ohne Provider-Pref kein Handler."""
    plugin = DontStopTheMusic()

    async def fake_add(player, tracks):
        player.added.extend(tracks)
        return list(tracks)

    monkeypatch.setattr(plugin, "_add_tracks", fake_add)

    async def run():
        player = _FakePlayer([11, 12], 1)            # kein provider-Pref
        return await plugin.dont_stop_the_music(player)

    assert asyncio.run(run()) == []


def test_dstm_skips_when_the_last_entry_is_a_radio_stream(monkeypatch):
    """``Plugin.pm:263-267`` — letzter Eintrag ist ein wiederholender Stream."""
    plugin = DontStopTheMusic()

    async def run():
        player = _FakePlayer([11, "http://radio.example/stream"], 1)
        player.playerprefs["plugin.dontstopthemusic:provider"] = "PLUGIN_RANDOM_TRACK"
        return await plugin.dont_stop_the_music(player)

    assert asyncio.run(run()) == []


def test_dstm_de_dupe_removes_candidates_already_queued():
    """``deDupe`` (``Plugin.pm:347-358``) — keine Dubletten in die Playlist."""
    plugin = DontStopTheMusic()
    tracks = [7, 7, 8, 9, 8]
    seen: set = {7}
    assert plugin.de_dupe(tracks, seen) == [8, 9]


def test_dstm_genre_provider_builds_the_randomplay_url_with_genres():
    """``mixWithGenres`` (``RandomPlay/DontStopTheMusic.pm:69-100``)."""
    plugin = DontStopTheMusic()

    class _Track:
        def __init__(self, genre):
            self.genre = genre

    class _Ctx:
        def __init__(self, player):
            self.client = player

    async def run():
        player = _FakePlayer([], 0)
        player.playlist = [_Track("Rock"), _Track("Jazz")]
        return await plugin._mix_with_genres("track", _Ctx(player))

    result = asyncio.run(run())
    assert result == ["randomplay://track?genres=Rock,Jazz"]     # :92-99


# ── Web-Seite ───────────────────────────────────────────────────────────────

def test_plugins_page_returns_200_with_metadata(manager):
    """``Plugins.pm:44-46`` + Metadaten aus ``install.xml`` (Perl :201-357)."""
    _set_state("DontStopTheMusic", STATE_ENABLED)
    res = _request("GET", "/settings/server/plugins.html")

    assert res.status_code == 200
    assert res.headers["content-type"] == "text/html"
    body = res.text
    assert 'data-plugin="DontStopTheMusic"' in body
    assert "1.0.0" in body                       # install.xml:6
    assert "Lyrion Community" in body            # install.xml:8
    assert "Don&#x27;t Stop The Music" in body or "Don't Stop The Music" in body


def test_plugins_page_shows_the_current_state(manager):
    """Die Seite zeigt ``enabled``/``disabled`` bzw. den Pending-Zustand."""
    _set_state("DontStopTheMusic", STATE_DISABLED)
    res = _request("GET", "/settings/server/plugins.html")
    assert 'data-state="disabled"' in res.text

    _set_state("DontStopTheMusic", STATE_NEEDS_ENABLE)
    res2 = _request("GET", "/settings/server/plugins.html")
    assert 'data-state="needs-enable"' in res2.text


def test_post_disable_writes_needs_disable_and_shows_restart_hint(manager):
    """``Plugins.pm:91-94`` (manual:) + ``:339-350`` (Restart-Hinweis)."""
    _set_state("DontStopTheMusic", STATE_ENABLED)
    res = _request("POST", "/settings/server/plugins.html",
                   data={"saveSettings": "1", "manual:DontStopTheMusic": "0"})

    assert res.status_code == 200
    assert manager.plugin_state("DontStopTheMusic") == STATE_NEEDS_DISABLE
    assert "restartWarning" in res.text
    assert "restart=1" in res.text


def test_post_enable_writes_needs_enable(manager):
    """Aktivieren umgekehrt — ``Plugins.pm:93``."""
    _set_state("DontStopTheMusic", STATE_DISABLED)
    res = _request("POST", "/settings/server/plugins.html",
                   data={"saveSettings": "1", "manual:DontStopTheMusic": "1"})

    assert res.status_code == 200
    assert manager.plugin_state("DontStopTheMusic") == STATE_NEEDS_ENABLE


def test_unknown_settings_path_still_404(manager):
    """``Slim/Web/HTTP.pm:1128-1131`` — unbekannte Pfade bleiben 404."""
    assert _request("GET", "/settings/server/does-not-exist.html").status_code == 404


def test_other_settings_pages_unchanged(manager):
    """Gegenprobe: die übrigen Seiten antworten weiter 200."""
    for path in ("/settings/server/basic.html", "/settings/server/status.html",
                 "/settings/server/artworkonline.html"):
        assert _request("GET", path).status_code == 200, path


# ── Wirkung im Betrieb: playlist-Notifikation → DSTM füllt nach ─────────────

def test_playlist_newsong_notification_reaches_dstm(monkeypatch):
    """Der Pfad ``notify`` → Plugin-Hook ist verdrahtet.

    Perl ``Slim/Control/Request::notify`` ruft die Abonnenten
    (``Plugin.pm:91``); unser ``control/notifications._notify_plugins`` leitet
    genau die ``playlist``-Ereignisse an ``PluginManager.notify_playlist_change``
    weiter.  Ohne diese Verdrahtung liefe DSTM nie.
    """
    from lyrion.control.notifications import Notification, notify
    from lyrion.plugins.manager import PluginManager

    mgr = PluginManager()
    seen: list[dict] = []

    async def spy(event):
        seen.append(event)

    mgr.register_playlist_change_hook(spy)
    mgr._started = True
    try:
        async def run():
            note = Notification.from_array(
                "1C:87:2C:47:FC:36", ["playlist", "newsong", "T", 1])
            notify(note)
            await asyncio.sleep(0)

        asyncio.run(run())
    finally:
        mgr.unregister_playlist_change_hook(spy)
        mgr._started = False

    assert len(seen) == 1
    assert seen[0]["command"] == "newsong"


def test_irrelevant_notifications_do_not_reach_dstm():
    """Nur ``playlist newsong|delete|cant_open|resume`` (``Plugin.pm:91``)."""
    from lyrion.control.notifications import Notification, notify
    from lyrion.plugins.manager import PluginManager

    mgr = PluginManager()
    seen: list[dict] = []

    async def spy(event):
        seen.append(event)

    mgr.register_playlist_change_hook(spy)
    mgr._started = True
    try:
        async def run():
            notify(Notification.from_array("", ["mixer", "volume", "50"]))
            notify(Notification.from_array("", ["playlist", "shuffle", "1"]))
            await asyncio.sleep(0)

        asyncio.run(run())
    finally:
        mgr.unregister_playlist_change_hook(spy)
        mgr._started = False

    assert seen == []
