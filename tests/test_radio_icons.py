"""Die erste Ebene unter Radio trägt Symbole — Perls Plugin-Icons, real geliefert.

Symptom (User): „Die erste Ebene unter Radio zeigt keine Menuesymbole in
SqueezePlay, obwohl dafuer Platzhalter vorgesehen sind."  Die Kategoriezeilen
(Eigene Voreinstellungen, Lokales Radio, Musik, Sport, Nachrichten,
Talksendungen, Nach Standort, Nach Sprache, Podcasts, TuneIn durchsuchen)
trugen in unserem ``item_loop`` das Feld ``icon-id`` bereits **Perl-konform**
(``/plugins/TuneIn/html/images/<name>.png``), die URL selbst aber antwortete
**404** — der Zeilenverweis lief ins Leere, und der Client zeichnete das
Platzhalter-/Leerbild.

Perl-Kette (read-only ``/tmp/lms-ref``, Live-Perl 9.1.1 192.168.1.90):

* ``Slim/Plugin/InternetRadio/TuneIn.pm:33-81`` — ``use constant MENUS``
  liefert die ``tag → {icon, weight}``-Tabelle; jeder ``icon``-Wert ist ein
  **skins-relativer** Pfad ``/plugins/TuneIn/html/images/<name>.png``.
* ``Slim/Plugin/InternetRadio/Plugin.pm:150`` — jedes dynamische OPML-Untermenü
  wird mit ``menu => 'radios'`` registriert, ``TuneIn.pm`` reicht ``icon``
  durch (Handler ``registerIconHandler``, ``Plugin.pm:139-143``).
* ``Slim/Plugin/OPMLBased.pm:200-247`` — der jive-Item-Aufbau der Zeile;
  ``Slim/Control/XMLBrowser.pm:1159-1161`` setzt daraus das Feld::

    if ( $item->{icon} ) {
        $hash{'icon' . ($item->{icon} =~ /^https?:/ ? '' : '-id')}
            = proxiedImage($item->{icon});
        $hasImage = 1;
    }

  ``proxiedImage`` (``Slim/Web/ImageProxy.pm:443-458``) lässt einen relativen
  Pfad **unverändert** (nur ``https?:`` wird proxied) — die Zeile trägt also
  wirklich ``/plugins/TuneIn/html/images/<name>.png``.
* **Ausgeliefert** wird dieser Pfad vom statischen Resolver: der Web-Root ist
  Perls ``HTML/``, und jede Anfrage wird per ``Slim::Web::HTTP::fixHttpPath``
  gegen die INCLUDE_PATH-Verzeichnisse des Skins aufgelöst
  (``Slim/Web/HTTP.pm:2687-2689`` → ``Slim/Web/Template/NoWeb.pm:154-186``).
  Diese Liste enthält pro Template-Root das Skin-Unterverzeichnis
  (``Slim/Web/Template/SkinManager.pm:169-179``: ``catdir($rootDir, $dir)``),
  und ``Slim/Utils/PluginManager.pm:366-377`` fügt für jedes Plugin dessen
  ``HTML/``-Verzeichnis als Template-Root hinzu
  (``Slim::Web::HTTP::addTemplateDirectory($htmlDir)``).  Die Datei liegt
  physisch unter ``Slim/Plugin/InternetRadio/HTML/EN/plugins/TuneIn/html/
  images/`` — relativ zu einem Template-Root also ``EN/<URL-Pfad>``.  Genau
  diesen Kandidaten erzeugt ``app._static_path_variants`` (``EN/`` + Pfad),
  unser statischer Root ist Perls ``HTML/`` (``html/``).
* **Client-Sprache** (SqueezePlay-Log, ``/tmp/squeezeplay-aug27-client.log``)::

    SlimServer.lua:1227 fetchArtwork(/plugins/TuneIn/html/images/radiopresets.png
                                    => /plugins/TuneIn/html/images/radiopresets_40x40_m.png)
    SocketHttp.lua:186 RequestHttp {GET /plugins/TuneIn/html/images/radiopresets_40x40_m.png}

  Der Client hängt also seinen ``artworkspec`` ``40x40_m`` an — beide Größen
  müssen echte PNGs sein (Perl: ``radiopresets.png`` 14815 B/512x512,
  ``radiopresets_40x40_m.png`` 1463 B/40x40 RGBA, curl 2026-09-19).

Behoben wurde das mit den Dateien selbst (``html/EN/plugins/TuneIn/html/
images/*.png``, byte-identisch zu Perls Plugin-Assets) — der statische
Resolver war bereits Perl-konform und liest pro Anfrage von der Platte.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from lyrion.web.api import JSONRPCAPI, WebAPIHandler
from lyrion.web.app import _static_path_variants

REPO_HTML = Path(__file__).resolve().parent.parent / "html"
PLUGIN_IMAGES = REPO_HTML / "EN" / "plugins" / "TuneIn" / "html" / "images"

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"

#: Perl's ``MENUS`` (``TuneIn.pm:33-81``) plus the plugin's own ``icon.png``;
#: every entry is a file in the plugin's ``HTML/EN/plugins/TuneIn/html/images``.
MENUS = {
    "presets": "radiopresets.png",
    "local": "radiolocal.png",
    "music": "radiomusic.png",
    "sports": "radiosports.png",
    "news": "radionews.png",
    "talk": "radiotalk.png",
    "location": "radioworld.png",
    "language": "radioworld.png",
    "world": "radioworld.png",
    "podcast": "podcasts.png",
    "search": "radiosearch.png",
    "default": "radio.png",
}

#: The exact requests SqueezePlay sent for the Radio first level
#: (``SocketHttp.lua:186`` in the client log); ``icon.png`` is requested for
#: the plug-in's own row.
CLIENT_REQUESTS = [
    "radiopresets_40x40_m.png", "radiolocal_40x40_m.png",
    "radiomusic_40x40_m.png", "radiosports_40x40_m.png",
    "radionews_40x40_m.png", "radiotalk_40x40_m.png",
    "radioworld_40x40_m.png", "podcasts_40x40_m.png",
    "radiosearch_40x40_m.png",
]


def _static(path: str) -> tuple[int, dict, bytes]:
    api = WebAPIHandler()
    api.set_static_dir(str(REPO_HTML))
    return api._serve_static(path)


def _rows(args: list[str]) -> list[dict]:
    result = asyncio.run(
        JSONRPCAPI()._json_radios("radios", [str(a) for a in args]))
    return result["item_loop"]


def _icon_urls() -> list[str]:
    """Every icon URL the Radio first level hands out (both answer forms)."""
    jive = [row["icon-id"] for row in _rows(["0", "20", "menu:radio"])]
    plain = asyncio.run(JSONRPCAPI()._json_radios("radios", ["0", "20"]))
    urls = jive + [row["icon"] for row in plain["radioss_loop"]]
    return list(dict.fromkeys(urls))


def _small(url: str) -> str:
    head, _, tail = url.rpartition("/")
    return f"{head}/{tail.replace('.', '_40x40_m.', 1)}"


# ── 1. the rows carry Perl's plugin paths, and the files exist ─────────────


def test_first_level_rows_carry_perls_plugin_icon_paths():
    """``TuneIn.pm:33-81`` icon → ``XMLBrowser.pm:1159-1161`` ``icon-id``."""
    rows = _rows(["0", "20", "menu:radio"])
    assert len(rows) == 10
    for row in rows:
        url = row["icon-id"]
        assert url.startswith("/plugins/TuneIn/html/images/"), url
        name = url.rsplit("/", 1)[1]
        assert name in MENUS.values(), name
        # the file really exists at Perl's physical location
        assert (PLUGIN_IMAGES / name).is_file(), url


def test_every_menu_icon_of_perls_table_is_shipped():
    """All of ``MENUS`` (incl. ``radio.png`` default + the plugin ``icon.png``)."""
    for name in sorted(set(MENUS.values()) | {"icon.png"}):
        path = PLUGIN_IMAGES / name
        assert path.is_file(), name
        assert path.read_bytes().startswith(PNG_MAGIC), name


def test_plugin_icon_files_are_byte_identical_to_perls_assets():
    """Only ever the same image under the same path — never a made-up one.

    Perl's sizes from the read-only checkout (md5 of the live server's answer,
    2026-09-19) are pinned here so a corrupted/placeholder file fails loudly.
    """
    expected = {
        "icon.png": "d8d03e0c857553b5a7db183282429e38",
        "podcasts.png": "d4ae8d411722e9b9bb214c840d1b5711",
        "radiolocal.png": "2276f3fa1f1e10b8e5f9029bb882ecb0",
        "radiomusic.png": "0d6558722f834619864254fc5744878a",
        "radionews.png": "be9dbc7d1fbeb60f16ca2d3e6ec18c88",
        "radio.png": "942758d916019aeb76788189b9964eae",
        "radiopresets.png": "d745b1a4406d51f301b12c92813e93a3",
        "radiosearch.png": "c0d0aa661afcea3aa9b249eb357e823e",
        "radiosports.png": "2cfa34bba6b090cd4ad28084a1077b5a",
        "radiotalk.png": "41281f6826c98d6e7db59a525a99309d",
        "radioworld.png": "4d6efdbd61647f3545aeffcd590d5957",
    }
    for name, md5 in expected.items():
        data = (PLUGIN_IMAGES / name).read_bytes()
        assert hashlib.md5(data).hexdigest() == md5, name


# ── 2. the static resolver answers Perl's plugin path ─────────────────────


def test_plugin_path_resolves_like_perls_skin_lookup():
    """Template-Root + Skin (``EN/``) — ``SkinManager.pm:169-179``."""
    variants = _static_path_variants(
        "/plugins/TuneIn/html/images/radiopresets.png")
    assert variants[0] == "plugins/TuneIn/html/images/radiopresets.png"
    assert "EN/plugins/TuneIn/html/images/radiopresets.png" in variants


# ── 3. real images, small and large (offline, in-process) ────────────────


def test_static_serves_every_row_icon_in_both_sizes():
    """No 404 placeholder: the row URL is a real PNG, 512x512 and 40x40."""
    pillow = pytest.importorskip("PIL.Image")
    urls = _icon_urls()
    # 9 distinct files: "Nach Standort" and "Nach Sprache" share radioworld.png
    assert sorted(u.rsplit("/", 1)[1] for u in urls) == sorted([
        "podcasts.png", "radiolocal.png", "radiomusic.png", "radionews.png",
        "radiopresets.png", "radiosearch.png", "radiosports.png",
        "radiotalk.png", "radioworld.png"])
    for url in urls:
        for label, want in (("full", (512, 512)), ("small", (40, 40))):
            path = url if label == "full" else _small(url)
            status, headers, body = _static(path)
            assert status == 200, (path, status, body[:60])
            assert headers["Content-Type"] == "image/png", (path, headers)
            assert body.startswith(PNG_MAGIC), path
            with pillow.open(io.BytesIO(body)) as img:
                assert img.size == want, (path, img.size)


def test_the_clients_own_requests_answer():
    """The exact ``GET`` lines of the client log (``_40x40_m`` suffix)."""
    pillow = pytest.importorskip("PIL.Image")
    for name in CLIENT_REQUESTS:
        status, headers, body = _static(
            f"/plugins/TuneIn/html/images/{name}")
        assert status == 200, (name, status)
        assert headers["Content-Type"] == "image/png", (name, headers)
        with pillow.open(io.BytesIO(body)) as img:
            assert img.size == (40, 40), (name, img.size)


def test_a_missing_plugin_image_still_answers_perls_404_page():
    """A 404 stays a 404 (Perl: ``HTTP.pm:708-711``) — no silent placeholder."""
    status, headers, body = _static(
        "/plugins/TuneIn/html/images/doesnotexist.png")
    assert status == 404
    assert headers["Content-Type"].startswith("text/html")
    assert not body.startswith(PNG_MAGIC)


# ── 4. raw over HTTP: the running server really delivers the bytes ────────


BASE = "http://127.0.0.1:9002"


def _fetch(path: str):
    try:
        with urllib.request.urlopen(BASE + path, timeout=15) as r:
            return (r.status, r.headers.get("Content-Type", ""),
                    r.headers.get("Content-Length"), r.read())
    except urllib.error.HTTPError as e:
        return e.code, e.headers.get("Content-Type", ""), None, e.read()
    except OSError as e:
        pytest.skip(f"Testserver nicht erreichbar: {e}")


def test_live_row_icons_answer_raw_in_both_sizes():
    """HTTP 200, ``Content-Length`` (SqueezePlay verträgt kein chunked) + Maße."""
    pillow = pytest.importorskip("PIL.Image")
    for url in _icon_urls():
        for label, want in (("full", (512, 512)), ("small", (40, 40))):
            path = url if label == "full" else _small(url)
            status, ctype, length, body = _fetch(path)
            assert status == 200, (path, status, body[:80])
            assert ctype == "image/png", (path, ctype)
            assert length is not None, (path, "kein Content-Length")
            assert int(length) == len(body), (path, length, len(body))
            with pillow.open(io.BytesIO(body)) as img:
                assert img.size == want, (path, img.size)


def test_live_serves_perls_bytes_unchanged():
    """Gross = Perls Datei unverändert (klein wird serverseitig skaliert)."""
    for url in _icon_urls():
        status, _ctype, _length, body = _fetch(url)
        assert status == 200, url
        on_disk = (PLUGIN_IMAGES / url.rsplit("/", 1)[1]).read_bytes()
        assert body == on_disk, url


def test_live_radio_root_node_still_matches_perls_fields():
    """Sanity: the row fields of the running server (raw JSON-RPC)."""
    req = urllib.request.Request(
        BASE + "/jsonrpc.js",
        data=json.dumps({"id": 1, "method": "slim.request",
                         "params": ["", ["radios", "0", "200",
                                         "menu:radio"]]}).encode(),
        headers={"Content-Type": "application/json"})
    try:
        result = json.load(urllib.request.urlopen(req, timeout=15))["result"]
    except OSError as e:
        pytest.skip(f"Testserver nicht erreichbar: {e}")
    assert result["count"] == 10
    for row in result["item_loop"]:
        assert row["window"] == {"titleStyle": "album"}
        # the search row additionally carries Perl's ``input`` block
        # (``OPMLBased.pm:221-246``), every other row exactly these five keys
        expected = {"text", "weight", "icon-id", "actions", "window"}
        assert set(row) == (expected | {"input"} if "input" in row else expected)
        assert ("input" in row) == row["text"].endswith("durchsuchen")
        assert row["icon-id"].startswith("/plugins/TuneIn/html/images/")
