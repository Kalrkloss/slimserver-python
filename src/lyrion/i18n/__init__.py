"""Localization of the Jive/JSON-RPC texts this port emits.

Perl reference (read-only, ``/tmp/lms-ref``), all rules verbatim:

* ``Slim/Utils/Strings.pm`` is the string registry. ``string($token)``
  upper-cases the token and looks it up in the *default language* table
  (Strings.pm:525-536); ``getString()`` additionally returns the token
  unchanged when it contains lowercase or whitespace (Strings.pm:564-580).
* The failsafe language is ``EN`` (Strings.pm:62 ``$failsafeLang = 'EN'``).
  While parsing, a key that has no line for the active language is filled
  from the ``EN`` value (Strings.pm:414-416 ``storeString``), and when a
  string is missing entirely the display layer returns the token itself
  (Display.pm:884-886).
* The language is the server pref ``language`` (Strings.pm:622-624
  ``getLanguage`` → ``$prefs->get('language') || $failsafeLang``), settable
  in Settings → Server → Basic (Slim/Web/Settings/Server/Basic.pm:28).
* For Jive/JSON clients Perl adds a per-request override: the HTTP
  ``Accept-Language`` header (``Cometd.pm:215-219`` upper-cases it;
  ``JSONRPC.pm:233-257`` strips a regional suffix to the base code) is put on
  the client as ``languageOverride`` (Cometd.pm:860-869, JSONRPC.pm:441-460)
  and ``Display::string`` loads that language instead of the server default
  (Display.pm:877-881 → Strings.pm:237-273 ``loadAdditional``).
  ``LANGUAGE``/``LC_*`` environment variables are NOT consulted for string
  selection; ``setLocale`` only uses them for ``LC_TIME``/``LC_COLLATE``
  (Strings.pm:714-728).

String table format (``strings.txt``): the header states "This tab-delimited
file contains all the text strings used in Lyrion Music Server" and the
parser (Strings.pm:334-397) matches a key line with ``^(\\S+)$`` and a
language line with ``^\\t(\\S*)\\t(.+)$`` — the language code is upper-cased
(``uc($one)``), an empty code continues the previous language, and comments
start with ``#``. Plugin tables live next to the plugin, e.g.
``Slim/Plugin/Favorites/strings.txt``.

This module carries only the keys this port actually emits (see
``strings_en.py``/``strings_de.py`` for the per-key source line), not the
whole Perl table.
"""
from __future__ import annotations

from .strings_de import STRINGS_DE
from .strings_en import STRINGS_EN

#: Strings.pm:62 — the language everything falls back to.
FAILSAFE_LANG = "EN"

#: Language tables we ship. Anything else resolves through the failsafe.
_TABLES: dict[str, dict[str, str]] = {
    "EN": STRINGS_EN,
    "DE": STRINGS_DE,
}


def normalize_language(lang: object) -> str:
    """Normalize a language code the way Perl does for clients.

    Upper-cases and turns ``-`` into ``_`` (``Cometd.pm:219`` upper-cases the
    raw ``Accept-Language``; ``JSONRPC.pm:252-257`` converts ``-`` → ``_`` and
    strips an unknown regional suffix to the base code). ``None``/empty gives
    the failsafe ``EN``.
    """
    code = str(lang).strip().replace("-", "_").upper() if lang else ""
    if not code:
        return FAILSAFE_LANG
    if code in _TABLES:
        return code
    base = code.split("_", 1)[0]
    return base if base in _TABLES else FAILSAFE_LANG


def available_languages() -> tuple[str, ...]:
    """Language codes this port carries a table for."""
    return tuple(_TABLES)


def get_string(key: str, lang: str = "de", default: str | None = None) -> str:
    """Return the localized text for ``key`` in ``lang``.

    Lookup chain (mirrors Perl ``storeString``, Strings.pm:414-416):

    1. the table for ``lang`` (normalized, region-stripped),
    2. the failsafe ``EN`` table,
    3. ``default`` if given (our current English literal),
    4. the upper-cased token itself (Display.pm:884-886).

    ``key`` is upper-cased first, exactly like ``string()`` (Strings.pm:526
    ``uc(shift)``).
    """
    token = str(key).upper()
    code = normalize_language(lang)
    table = _TABLES.get(code)
    if table is not None and token in table:
        return table[token]
    if code != FAILSAFE_LANG and token in _TABLES[FAILSAFE_LANG]:
        return _TABLES[FAILSAFE_LANG][token]
    if default is not None:
        return default
    return token


def resolve_language(request_lang: str | None = None) -> str:
    """Active UI language for a Jive/JSON-RPC response.

    Order (Perl ``Display::string``/``getLanguage``): an explicit request
    language first (the Jive ``Accept-Language`` override), then the server
    ``language`` pref, then the failsafe ``EN``.

    The request language is currently never supplied: our HTTP entry point
    (``WebAPIHandler.handle``) receives only the JSON-RPC body, no headers, so
    the Jive ``Accept-Language`` override cannot be read here without touching
    ``web/app.py``. The parameter exists so the caller can pass it once the
    header is plumbed through.
    """
    if request_lang:
        return normalize_language(request_lang)
    pref = None
    try:
        from lyrion.config import get_prefs
        pref = get_prefs().get("language")
    except Exception:  # noqa: BLE001 — fall back to failsafe on any pref error
        pref = None
    return normalize_language(pref)
