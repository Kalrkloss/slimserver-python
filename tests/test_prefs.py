"""Tests for preference type coercion.

Regression: PreferenceStore.get() always returned strings because
_get_meta_sync created an async task it never awaited and returned None —
so a registered int pref (e.g. 9000) came back as "9000" and a bool pref
with default 0 came back as "0" (truthy!).
"""

import asyncio

import aiosqlite

from lyrion.config import PreferenceStore, _PREF_DB_SCHEMA


def _store():
    store = PreferenceStore(":memory:")

    async def init():
        store._db = await aiosqlite.connect(":memory:")
        store._db.row_factory = aiosqlite.Row
        await store._db.executescript(_PREF_DB_SCHEMA)
        await store.init_preference("audit_int", default=9000, type_name="int")
        await store.init_preference("audit_bool", default=0, type_name="bool")

    asyncio.run(init())
    return store


def test_int_pref_is_int():
    store = _store()
    value = store.get("audit_int")
    assert isinstance(value, int), f"int pref must be int, got {type(value).__name__} {value!r}"
    assert value == 9000


def test_bool_pref_default_zero_is_false():
    store = _store()
    value = store.get("audit_bool")
    assert value is False, f"bool pref default 0 must be False, got {value!r}"


def test_set_roundtrip_keeps_type():
    store = _store()

    async def run():
        await store.set("audit_int", 1234)

    asyncio.run(run())
    assert store.get("audit_int") == 1234
    assert isinstance(store.get("audit_int"), int)
