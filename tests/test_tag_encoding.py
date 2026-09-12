"""Tag-Encoding: CP1251-Tags dürfen nicht als Latin-1-Mojibake landen.

Perl-Vorbild: ``Slim::Utils::Unicode::utf8decode_guess``
(``Slim/Utils/Unicode.pm:154-186``) probiert bevorzugte Encodings strikt und
dann einen Charset-Detektor (``encodingFromString`` →
``Encode::Detect::Detector::detect``, ``Unicode.pm:482-503``). LMS schickt
Tag-Strings da durch; wir vertrauten mutagens ID3-Encoding-Byte.

Die Erwartungswerte stammen aus der echten Bibliothek (2026-09-12): 34 Titel,
2 Alben und 1 Interpret waren betroffen. Die Kontrollfälle sind genauso
wichtig — sie teilen sich den Zeichenbereich U+00C0–U+00FF und dürfen NICHT
"repariert" werden.
"""

import pytest

from lyrion.media.scanner import _fix_tag_encoding as fix


@pytest.mark.parametrize("broken,fixed", [
    # echte Werte aus lyrion.db (Titel)
    ("ßðèëî", "Ярило"),
    ("Âàëüñ", "Вальс"),
    ("Âñå Îäíî", "Все Одно"),
    ("Âñòóïëåíèå I", "Вступление I"),
    ("Âòîðîé àä Âåðòåðà", "Второй ад Вертера"),
    ("Ãðåøíèê", "Грешник"),
    ("Äåíåá", "Денеб"),
    # Album + Interpret
    ("Âäàëè Îò Ñêàë", "Вдали От Скал"),
    ("Êàòîðãà", "Каторга"),
    ("Ðàçíîòðàâèå", "Разнотравие"),
])
def test_cp1251_mojibake_is_recovered(broken, fixed):
    assert fix(broken) == fixed


def test_utf8_read_as_latin1_is_recovered():
    # 'GaladrielÂ´s Song' in the live DB -> the acute accent was double encoded
    assert fix("GaladrielÂ´s Song") == "Galadriel´s Song"


@pytest.mark.parametrize("unchanged", [
    # French / Finnish / Turkish / German share the U+00C0-U+00FF range
    "01 - Leçon de ténèbres",
    "01. Jäästä Syntynyt \\ Varjojen Virta",
    "Bir (Akustik Konser 2017) (feat. Gökhan Özoguz & Hakan Özoguz)",
    "Das Süße Mädl, Das Fredi Liebt",
    "Motörhead – Ace of Spades",
    # ASCII art with high symbols: decodes to almost no Cyrillic letters
    "Sailing Red Moon °º¤ø,¸¸,ø¤º°`",
    # already correct Unicode (not latin-1 encodable / no mis-decode)
    "Ярило",
    "Привет мир",
    "Björk: Homogenic",
    "Plain ASCII Title",
    "Café del Mar",
    "",
])
def test_correct_tags_are_left_alone(unchanged):
    assert fix(unchanged) == unchanged


def test_real_unicode_is_untouched_even_with_mixed_script():
    # Encodable only in UTF-8 -> must be returned as is
    value = "Кино — Группа крови"
    assert fix(value) == value


def test_tag_value_applies_the_repair(monkeypatch):
    """The repair runs inside _tag_value, so every tag field goes through it."""
    from lyrion.media import scanner as scanner_mod

    class _Tags(dict):
        pass

    tags = _Tags({"TIT2": ["ßðèëî"]})
    assert scanner_mod._tag_value(tags, "TIT2") == "Ярило"
