"""Plugin-Assets, die SqueezePlay abfragt — Perls Dateien, nicht nachgezeichnet.

Symptom (nach dem TuneIn-Auftrag `030dd3315`): der Client fragte zwei weitere
Plugin-Bilder an, die bei uns **404** lieferten —
``/plugins/Sounds/html/images/icon_40x40_m.png`` und
``/plugins/RemoteLibrary/html/icon_40x40_m.png``.

Systematischer Abgleich (read-only ``/tmp/lms-ref``, Live-Perl 9.1.1
192.168.1.90): Perl liefert einen Pfad genau dann statisch aus, wenn
``fixHttpPath`` ihn in einem INCLUDE_PATH-Verzeichnis findet —
``Slim/Web/HTTP.pm:2687-2689`` → ``Slim/Web/Template/NoWeb.pm:154-186`` über
die Liste aus ``Slim/Web/Template/SkinManager.pm:169-179``
(``catdir($rootDir, $dir)`` je Template-Root und Skin).  Template-Roots sind
Perls ``HTML/`` **und** jedes ``Slim/Plugin/*/HTML/``, das ein *geladenes*
Plugin über ``Slim/Utils/PluginManager.pm:366-380`` registriert
(``Slim::Web::HTTP::addTemplateDirectory($htmlDir)`` → ``HTTP.pm:2683``).

Ein Vergleich aller Template-Roots × Skin-Unterverzeichnisse gegen unser
``html/`` ergab 664 web-sichtbare Perl-Pfade; 27 davon waren Plugin-Bilder,
die Perl mit **200** und wir mit **404** beantworteten.  Sie liegen jetzt
md5-identisch unter ``html/EN/<URL-Pfad>`` (unser statischer Root *ist* Perls
``HTML/``, der ``EN/``-Kandidat ist Perls Skin-Verzeichnis).

Der Test pinnt (a) die Bytes der 27 übernommenen Dateien, (b) dass unser
Resolver/Pillow je URL 200 + ``image/png`` + echte Maße liefert — auch für die
``_40x40_m``-Namen, die Perl *dynamisch* skaliert (``HTTP.pm:78-81`` schickt
jeden ``_<N>x<N>_<m>``-Pfad in ``Slim::Web::Graphics::artworkRequest``,
``HTTP.pm:1199-1245``), und (c) dass bewusst *nicht* übernommene Pfade weiter
404 liefern (Perl-Parität, nicht Toleranz).
"""

from __future__ import annotations

import hashlib
import io
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from lyrion.web.api import WebAPIHandler
from lyrion.web.app import _static_path_variants

REPO_HTML = Path(__file__).resolve().parent.parent / "html"
PERL_REF = Path("/tmp/lms-ref")

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"

#: Die 27 übernommenen Assets: URL → (md5, Bytes) der Perl-Datei.
#: Quelle = ``html/EN/<URL>``; Perl-Fundstelle je Pfad in ``api.py``
#: (``_serve_static``) bzw. ``Slim/Plugin/<Plugin>/HTML/EN/<URL>``.
ASSETS = {
    "/html/images/favorites_remove.png": ("71ec8c9038ffccfe575ad65bdfd5b057", 15242),
    "/plugins/DigitalInput/html/images/icon.png": ("213c52e46566f86c021593f4e3054f0d", 24729),
    "/plugins/DigitalInput/html/images/icon_40x40_m.png": ("85a40908cf306300076f65d166c4d4ce", 1843),
    "/plugins/DontStopTheMusic/html/images/icon.png": ("f8f0dba3ccb008246ddd6cc7415c6c3c", 18970),
    "/plugins/ExtendedBrowseModes/html/composers.png": ("717c4b7f651eaf9a6d7d5040a0a36763", 21082),
    "/plugins/ExtendedBrowseModes/html/conductors.png": ("986335bbb2c10fea7616d933faa66bb8", 19155),
    "/plugins/ExtendedBrowseModes/html/icon.png": ("5f30b866fede0cdf0c8a4f6de2b14870", 15180),
    "/plugins/ExtendedBrowseModes/html/icon_charts.png": ("dcf5486962958b09a396de26585418af", 13517),
    "/plugins/ExtendedBrowseModes/html/icon_folder.png": ("bcf15577ef4fc86c69372e8d404c7fc1", 11581),
    "/plugins/ExtendedBrowseModes/html/jazzcomposers.png": ("68bfcdd31c29a69317718148304b923a", 22902),
    "/plugins/ExtendedBrowseModes/html/newartists_MTL_svg_artistnew.png": ("f79db468e72e8b388872c2b66dfebc34", 21953),
    "/plugins/ExtendedBrowseModes/html/popularalbums_MTL_svg_popularalbum.png": ("796239d5c4cc9c10baf25d1ffce10251", 18620),
    "/plugins/ExtendedBrowseModes/html/randomalbums.png": ("64c1c4fcd2696cb615b62d81f6c74c01", 22607),
    "/plugins/ExtendedBrowseModes/html/recentartists_MTL_svg_artistrecent.png": ("f79db468e72e8b388872c2b66dfebc34", 21953),
    "/plugins/ExtendedBrowseModes/html/topartists_MTL_svg_artistpopular.png": ("dcf5486962958b09a396de26585418af", 13517),
    "/plugins/InfoBrowser/html/images/icon.png": ("be9dbc7d1fbeb60f16ca2d3e6ec18c88", 20261),
    "/plugins/InfoBrowser/html/images/icon_40x40_m.png": ("677d3466cd4ba3132e2e099be28ee3f9", 1626),
    "/plugins/LineIn/html/images/icon.png": ("aa645656ff20ba5ebf3be513095b74ad", 10924),
    "/plugins/LineIn/html/images/icon_40x40_m.png": ("e0f439b440ed9c400e37af5914e67a47", 1083),
    "/plugins/MyApps/html/images/icon.png": ("e6d24268cbe77165e7f681512c597706", 21172),
    "/plugins/MyApps/html/images/icon_40x40_m.png": ("8539673cf47d97bb71e8ddf5a959c057", 1645),
    "/plugins/RandomPlay/html/images/icon.png": ("7df3620e0ecff4d83b019015c257e643", 19411),
    "/plugins/RandomPlay/html/images/icon_40x40_m.png": ("ad2e503647297a25d3f02918bf7f3397", 1455),
    "/plugins/RemoteLibrary/html/icon.png": ("a1b19a412e20e9c2828424422960546c", 14212),
    "/plugins/RemoteLibrary/html/lms.png": ("4e10437efc8b931e472fc2542698bf0e", 17903),
    "/plugins/Sounds/html/images/icon.png": ("4e6718ee054f9aec7fb7189dd1e93db1", 20978),
    "/plugins/Sounds/html/images/icon_40x40_m.png": ("91c6f9dbcd16d09875c4c336eaeaf40c", 1450),
}

#: Die zwei URLs aus dem Client-Log (SqueezePlay hängt seinen ``artworkspec``
#: ``_40x40_m`` an die Basis an, ``SocketHttp.lua:186``).
CLIENT_URLS = [
    "/plugins/Sounds/html/images/icon_40x40_m.png",
    "/plugins/RemoteLibrary/html/icon_40x40_m.png",
]

#: Pfade, die im Perl-Baum liegen, aber **nicht** übernommen wurden, weil
#: Perl sie selbst nicht ausliefert (Plugin-Root nicht registriert) — roh
#: gemessen am Live-Perl: dieselbe URL antwortet dort 404.
NOT_SERVED_BY_PERL = [
    "/plugins/Podcast/html/images/icon.png",
    "/plugins/Podcast/html/images/icon_40x40_m.png",
    "/plugins/UPnP/logo.png",
    "/plugins/LineOut/html/images/icon.png",
    "/plugins/Analytics/html/icon.png",
    "/plugins/MusicMagic/html/images/icon.png",
    "/plugins/ZenRadio/html/icon.png",
]


def _static(path: str) -> tuple[int, dict, bytes]:
    api = WebAPIHandler()
    api.set_static_dir(str(REPO_HTML))
    return api._serve_static(path)


def _sized(url: str, spec: str = "40x40_m") -> str:
    head, _, tail = url.rpartition("/")
    base, _, ext = tail.rpartition(".")
    return f"{head}/{base}_{spec}.{ext}"


# ── 1. Die übernommenen Dateien sind Perls Bytes, unter Perls Pfad ────────


def test_every_adopted_asset_is_perls_bytes_under_perls_path():
    for url, (md5, size) in ASSETS.items():
        path = REPO_HTML / "EN" / url.lstrip("/")
        assert path.is_file(), url
        data = path.read_bytes()
        assert len(data) == size, url
        assert hashlib.md5(data).hexdigest() == md5, url
        assert data.startswith(PNG_MAGIC), url


def test_adopted_assets_live_in_perls_plugin_tree():
    """Nur eigene Pfade kopieren, die es bei Perl wirklich gibt (skip ohne Ref)."""
    if not PERL_REF.is_dir():
        pytest.skip("Perl-Referenzbaum /tmp/lms-ref nicht vorhanden")
    for url in ASSETS:
        rel = url.lstrip("/")
        hits = list(PERL_REF.glob("Slim/Plugin/*/HTML/EN/" + rel))
        assert len(hits) == 1, (url, hits)
        assert (REPO_HTML / "EN" / rel).read_bytes() == hits[0].read_bytes(), url


def test_the_two_client_urls_of_the_report_answer_200():
    """Genau die zwei Pfade aus der Meldung — Status + Maße (Pillow)."""
    pillow = pytest.importorskip("PIL.Image")
    for url in CLIENT_URLS:
        status, headers, body = _static(url)
        assert status == 200, (url, status)
        assert headers["Content-Type"] == "image/png", (url, headers)
        with pillow.open(io.BytesIO(body)) as img:
            assert img.size == (40, 40), (url, img.size)


# ── 2. Der Resolver findet sie wie Perl (Skin-Kandidat ``EN/``) ───────────


def test_plugin_asset_resolves_via_perls_skin_candidate():
    variants = _static_path_variants("/plugins/Sounds/html/images/icon.png")
    assert variants[0] == "plugins/Sounds/html/images/icon.png"
    assert "EN/plugins/Sounds/html/images/icon.png" in variants


def test_every_adopted_asset_is_served_in_its_own_size():
    """Basis 512x512, ``_40x40_m`` 40x40 — auch wo Perl dynamisch skaliert."""
    pillow = pytest.importorskip("PIL.Image")
    for url in ASSETS:
        status, headers, body = _static(url)
        assert status == 200, (url, status)
        assert headers["Content-Type"] == "image/png", (url, headers)
        with pillow.open(io.BytesIO(body)) as img:
            size = img.size
        assert size in ((512, 512), (40, 40)), (url, size)
        # und die jeweils andere Größe, wie sie der Client anfragt
        other = _sized(url) if size == (512, 512) else url.rsplit("_40x40_m", 1)[0] + ".png"
        status, headers, body = _static(other)
        assert status == 200, (other, status)
        with pillow.open(io.BytesIO(body)) as img:
            assert img.size in ((512, 512), (40, 40)), (other, img.size)


def test_a_sized_name_without_perl_file_is_scaled_from_the_base():
    """Perl skaliert ``_40x40_m`` on the fly (``HTTP.pm:1199-1245``)."""
    pillow = pytest.importorskip("PIL.Image")
    for url in ("/plugins/Sounds/html/images/icon_50x50_o.png",
                "/plugins/RemoteLibrary/html/icon_40x40_m.png",
                "/plugins/MyApps/html/images/icon_96x96_o.png"):
        status, headers, body = _static(url)
        assert status == 200, (url, status)
        assert headers["Content-Type"] == "image/png", (url, headers)
        assert body.startswith(PNG_MAGIC), url
        with pillow.open(io.BytesIO(body)) as img:
            assert img.size in ((40, 40), (50, 50), (96, 96)), (url, img.size)


# ── 3. Bewusst nicht übernommen: Perl liefert dieselben Pfade auch 404 ───


def test_unregistered_plugin_html_roots_stay_404_like_perl():
    """Keine Toleranz gegenüber Perl: was Perl 404t, bleibt bei uns 404.

    Live-Perl 9.1.1 antwortet auf diese URLs 404 (Plugin-Root nicht in der
    INCLUDE_PATH, ``PluginManager.pm:366-380``) — ein Anlegen hier würde eine
    Abweichung erzeugen statt sie zu schließen.
    """
    for url in NOT_SERVED_BY_PERL:
        status, _headers, body = _static(url)
        assert status == 404, (url, status)
        assert not body.startswith(PNG_MAGIC), url


def test_skin_chrome_of_perls_default_skin_is_not_invented():
    """``HTML/Default``-Assets haben keinen ``EN/``-Pfad — nicht nachbauen."""
    for url in ("/html/images/b_play.gif", "/html/images/b_next.gif",
                "/html/ext/ext-main.js"):
        status, _headers, _body = _static(url)
        assert status == 404, (url, status)


def test_we_never_ship_a_plugin_image_perl_does_not_have():
    """Jedes Plugin-Bild in unserem Baum existiert 1:1 in Perls Baum."""
    if not PERL_REF.is_dir():
        pytest.skip("Perl-Referenzbaum /tmp/lms-ref nicht vorhanden")
    root = REPO_HTML / "EN/plugins"
    kept = 0
    for f in sorted(root.rglob("*")):
        if not f.is_file() or f.suffix.lower() not in {".png", ".gif", ".jpg", ".jpeg", ".ico"}:
            continue
        rel = f.relative_to(REPO_HTML / "EN").as_posix()
        hits = list(PERL_REF.glob("Slim/Plugin/*/HTML/EN/" + rel))
        assert hits, f"nicht in Perls Baum: {rel}"
        assert any(h.read_bytes() == f.read_bytes() for h in hits), rel
        kept += 1
    assert kept >= len(ASSETS) - 1, kept


# ── 4. Roh über HTTP: der laufende Server liefert dieselben Bytes ────────

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


def test_live_serves_every_adopted_asset_with_length_and_size():
    pillow = pytest.importorskip("PIL.Image")
    for url in ASSETS:
        status, ctype, length, body = _fetch(url)
        assert status == 200, (url, status, body[:80])
        assert ctype == "image/png", (url, ctype)
        assert length is not None, (url, "kein Content-Length")
        assert int(length) == len(body), (url, length, len(body))
        with pillow.open(io.BytesIO(body)) as img:
            assert img.size in ((512, 512), (40, 40)), (url, img.size)


def test_live_answers_the_two_client_urls_raw():
    pillow = pytest.importorskip("PIL.Image")
    for url in CLIENT_URLS:
        status, ctype, length, body = _fetch(url)
        assert status == 200, (url, status)
        assert ctype == "image/png", url
        assert int(length) == len(body), url
        with pillow.open(io.BytesIO(body)) as img:
            assert img.size == (40, 40), (url, img.size)
