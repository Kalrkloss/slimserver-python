"""German localization of the Jive/JSON-RPC texts we emit.

Perl-Referenz (alles read-only ``/tmp/lms-ref``); jede Behauptung mit Fundstelle:

* ``Slim/Utils/Strings.pm:525-536`` ``string($token)``: ``uc($token)``, Lookup in
  der Tabelle der aktiven Sprache; ``:564-580`` ``getString()``; Failsafe ``EN``
  ``:62``; Fallback EN bei fehlender Übersetzung ``:414-416`` (``storeString``);
  Rückgabe des Tokens wenn gar nichts gefunden wird ``Display.pm:884-886``.
* Sprachwahl: Server-Pref ``language`` (``Strings.pm:622-624``), setzbar in
  ``Slim/Web/Settings/Server/Basic.pm:28``; Client-Override ``Cometd.pm:215-219``
  (``uc`` des ``Accept-Language``) → ``languageOverride`` ``Cometd.pm:860-869``
  → ``Display.pm:877-881``.
* Tabellenformat: ``strings.txt`` Kopf „This tab-delimited file …“; Parser
  ``Strings.pm:334-397`` Key-Zeile ``^(\\S+)$``, Sprachzeile ``^\\t(\\S*)\\t(.+)$``;
  Plugin-Tabelle ``Slim/Plugin/Favorites/strings.txt:3`` (``FAVORITES``).

Die DE-Werte stammen 1:1 aus der ``DE``-Spalte der Perl-Tabellen; die
EN-Werte sind der ``EN``-Failsafe (identisch mit unserer bisherigen Ausgabe).
Die Zeilennummern in den Parametern sind die ``DE``-Zeilen in der Quelle.
"""

from __future__ import annotations

import pytest

from lyrion.i18n import (
    FAILSAFE_LANG,
    STRINGS_DE,
    STRINGS_EN,
    available_languages,
    get_string,
    normalize_language,
    resolve_language,
)
from lyrion.web import api


# ── EN-Parität: unsere englischen Fallbacks == Perl-EN ────────────────────

def test_every_jive_key_has_en_and_matches_current_literal():
    """Strings.pm:414-416 — der ``EN``-Failsafe je Key; Quelle ``_JIVE_STRINGS``."""
    assert api._JIVE_STRINGS, "der Jive-String-Funnel darf nicht leer sein"
    for key, literal in api._JIVE_STRINGS.items():
        assert key in STRINGS_EN, f"{key} fehlt im EN-Failsafe"
        assert STRINGS_EN[key] == literal, (
            f"{key}: EN-Failsafe {STRINGS_EN[key]!r} != Literal {literal!r}")


def test_en_failsafe_is_english_failsafe_language():
    """Strings.pm:62 ``$failsafeLang = 'EN'``."""
    assert FAILSAFE_LANG == "EN"
    assert "EN" in available_languages()


# ── DE-Lookup mit Perl-Strings.txt-Werten ─────────────────────────────────

@pytest.mark.parametrize("key,de,src", [
    ("ALARM_ALL_ALARMS", "Alle Wecker", "strings.txt:1858"),
    ("ALARM_ADD", "Wecker hinzufügen", "strings.txt:1678"),
    ("ALARM_SET_TIME", "Zeit einrichten", "strings.txt:1913"),
    ("ALARM_DAY1", "Montag", "strings.txt:2169"),
    ("ALARM_OFF", "Aus", "strings.txt:1568"),
    ("SHUFFLE", "Zufall", "strings.txt:11303"),
    ("SHUFFLE_OFF", "Wiedergabeliste nicht mischen", "strings.txt:11383"),
    ("CANCEL", "Abbrechen", "strings.txt:22749"),
    ("EMPTY", "Leer", "strings.txt:448"),
    ("SLEEP_CANCEL", "Schlafmodus abbrechen", "strings.txt:1252"),
    ("X_MINUTES", "%s Minuten", "strings.txt:1288"),
    ("DO_NOT_SYNC", "Keine Synchronisierung", "strings.txt:16834"),
    ("SYNC_X_TO", "Synchronisieren %s mit:", "strings.txt:16817"),
    ("NOTHING_CURRENTLY_PLAYING", "Keine Wiedergabe", "strings.txt:1361"),
    ("PRESET", "Voreinstellung #%s", "strings.txt:23845"),
    ("BRIGHTNESS_DARK", "Dunkel", "strings.txt:15576"),
    ("HOME", "Hauptmenü", "strings.txt:3143"),
    ("FAVORITES", "Favoriten", "Slim/Plugin/Favorites/strings.txt:6"),
])
def test_de_lookup_returns_perl_value(key, de, src):
    assert get_string(key, lang="de") == de, src


def test_de_table_values_differ_from_en_where_translated():
    """Jeder DE-Eintrag stammt aus der ``DE``-Spalte (nicht kopiertes EN)."""
    translated = [k for k, v in STRINGS_DE.items()
                  if k in STRINGS_EN and STRINGS_EN[k] != v]
    assert len(translated) > 70
    assert get_string("ALARM_DAY0", "de") == "Sonntag"          # strings.txt:2150
    assert get_string("ALARM_SHORT_DAY_0", "de") == "So"        # strings.txt:2283


# ── Failsafe-Kette ────────────────────────────────────────────────────────

@pytest.mark.parametrize("key,en,src", [
    ("ANALOGOUTMODE_SUBOUT", "Subwoofer", "strings.txt:20205"),
    ("ALBUM", "Album", "strings.txt:12438"),
    ("STANDARD", "Standard", "strings.txt:11936"),
])
def test_missing_de_falls_back_to_en(key, en, src):
    """Strings.pm:414-416 — keine DE-Übersetzung → Failsafe ``EN``."""
    assert key not in STRINGS_DE
    assert get_string(key, lang="de") == en, src


def test_unknown_key_uses_default_then_token():
    """Strings.pm:564-580 / Display.pm:884-886 — Default, sonst der Token."""
    assert get_string("NO_SUCH_KEY_XYZ", lang="de") == "NO_SUCH_KEY_XYZ"
    assert get_string("NO_SUCH_KEY_XYZ", "de", default="Fallback") == "Fallback"


def test_token_is_upper_cased_before_lookup():
    """Strings.pm:526 ``my $token = uc(shift);`` ('light' → 'LIGHT')."""
    assert get_string("alarm_day1", "de") == "Montag"
    assert get_string("light", "de") == "Light"                 # strings.txt:11903


# ── Sprachauflösung ───────────────────────────────────────────────────────

@pytest.mark.parametrize("raw,code", [
    ("de", "DE"), ("DE", "DE"), ("de-DE", "DE"), ("de_AT", "DE"),
    ("en", "EN"), ("en-US", "EN"), ("EN_GB", "EN"),
    ("fr", "EN"), ("", "EN"), (None, "EN"), ("zh-CN", "EN"),
])
def test_normalize_language(raw, code):
    """Cometd.pm:219 ``uc`` + JSONRPC.pm:252-254 ``-``→``_`` / Regional-Abbau."""
    assert normalize_language(raw) == code


def test_resolve_language_uses_server_pref(monkeypatch):
    """Strings.pm:622-624 — ``$prefs->get('language') || $failsafeLang``."""
    class _Prefs:
        def __init__(self, lang):
            self._lang = lang

        def get(self, name, default=None):
            return self._lang if name == "language" else default

    monkeypatch.setattr("lyrion.config.get_prefs", lambda: _Prefs("de"))
    assert resolve_language() == "DE"
    monkeypatch.setattr("lyrion.config.get_prefs", lambda: _Prefs(None))
    assert resolve_language() == "EN"
    monkeypatch.setattr("lyrion.config.get_prefs", lambda: _Prefs("de_DE"))
    assert resolve_language() == "DE"


def test_resolve_language_request_override_wins():
    """Cometd.pm:860-869 — der ``Accept-Language``-Override schlägt den Pref."""
    assert resolve_language("de-DE") == "DE"
    assert resolve_language("en") == "EN"


# ── Verdrahtung in api.py ─────────────────────────────────────────────────

def test_jive_string_german_when_server_language_is_de(monkeypatch):
    monkeypatch.setattr("lyrion.config.get_prefs",
                        lambda: type("P", (), {"get": lambda s, n, d=None: "de"})())
    assert api._jive_string("ALARM_ALL_ALARMS") == "Alle Wecker"
    assert api._jive_string("SHUFFLE") == "Zufall"
    assert api._jive_string("ALARM_DAY1") == "Montag"
    # Format-Token bleibt erhalten (Strings.pm:525-536 ``sprintf``).
    assert api._jive_str("SYNC_X_TO", "Küche") == "Synchronisieren Küche mit:"


def test_jive_string_english_by_default(monkeypatch):
    monkeypatch.setattr("lyrion.config.get_prefs",
                        lambda: type("P", (), {"get": lambda s, n, d=None: "en"})())
    assert api._jive_string("ALARM_ALL_ALARMS") == "All Alarms"
    assert api._jive_string("SHUFFLE") == "Shuffle"
    assert api._jive_string("HOME") == "Home"


def test_jive_string_unknown_key_returns_token():
    """Display.pm:884-886 — unbekannter Token kommt unverändert zurück."""
    assert api._jive_string("no_such_jive_key") == "NO_SUCH_JIVE_KEY"
