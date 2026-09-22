"""Löschen eines Favoriten wirkt wie Perl — Liste **und** ``favorites.opml``.

Perl:

* ``Slim/Plugin/Favorites/Plugin.pm:924-964`` ``cliDelete`` liest die
  **getaggten** Parameter ``item_id``/``url`` und ruft ``deleteIndex`` bzw.
  ``deleteUrl``;
* ``Slim/Plugin/Favorites/OpmlFavorites.pm:412-426`` ``deleteIndex``
  splict den Eintrag aus dem geladenen Outline-Baum und
  ``OpmlFavorites.pm:91-99`` ``save`` schreibt das ganze Dokument zurück
  (``Slim/Plugin/Favorites/Opml.pm:91-138``) — die Datei *ist* Perls Ablage,
  der Favorit ist danach aus Liste und Ablage weg.

Unsere Ablage ist die DB, ``favorites.opml`` die Migrationsquelle, die
``lyrion.music.favorites.ensure_opml_imported`` beim Start in die DB merged.
Ein Löschen, das nur die DB-Zeile entfernt, wird von diesem Merge beim
nächsten Start rückgängig gemacht — der Favorit taucht wieder auf.  Genau
das prüft ``test_backfill_after_delete_does_not_resurrect`` (der
Symptom-Regressionstest).
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from lyrion.control.cli import CLIContext, CLIHandler
from lyrion.database.schema import Base, Favorite
from lyrion.music.favorites import FavoritesManager, ensure_opml_imported

MAC = "1C:87:2C:47:FC:36"

OPML = """<?xml version="1.0" encoding="UTF-8"?>
<opml version="1.0">
  <head>
    <title>Favorites</title>
  </head>
  <body>
    <outline text="Chill" title="Chill" icon="html/images/favorites.png">
      <outline text="Hirschmilch Chillout" title="Hirschmilch Chillout" URL="http://hirschmilch.de:7000/chillout.mp3" type="audio" icon="html/images/radio.png"/>
      <outline text="Hirschmilch Electronic" title="Hirschmilch Electronic" URL="http://hirschmilch.de:7000/electronic.mp3" type="audio" icon="html/images/radio.png"/>
    </outline>
    <outline text="Lokal" title="Lokal" icon="html/images/favorites.png">
      <outline text="SWR 3" title="SWR 3" URL="https://liveradio.swr.de/sw282p3/swr3/" type="audio" icon="html/images/radio.png"/>
    </outline>
    <outline text="1Mix" title="1Mix" URL="http://opml.radiotime.com/Tune.ashx?id=s355203" type="audio" icon="html/images/radio.png"/>
  </body>
</opml>
"""


def _temp_db(tmp_path, monkeypatch, opml_text: str | None = OPML):
    """Temp-DB + Temp-OPML in die echten Codepfade verdrahtet."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path/'lyrion.db'}")
    factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)

    async def _create():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    asyncio.run(_create())

    @asynccontextmanager
    async def session_ctx():
        session = factory()
        try:
            yield session
            await session.commit()
        finally:
            await session.close()

    opml = tmp_path / "favorites.opml"
    if opml_text is not None:
        opml.write_text(opml_text, encoding="utf-8")
    monkeypatch.setattr("lyrion.database.sqlite_helper.db_session", session_ctx)
    monkeypatch.setattr("lyrion.music.favorites._opml_path",
                        lambda: opml if opml_text is not None else None)
    monkeypatch.setattr("lyrion.music.favorites._opml_import_done", False)
    # Der Modul-Singleton merkt sich seine Session-Factory beim ersten Aufruf;
    # ohne Reset arbeitet er im nächsten Test noch auf der alten Temp-DB.
    monkeypatch.setattr("lyrion.music.favorites._manager", None)
    return session_ctx, opml


def _import():
    asyncio.run(ensure_opml_imported())


def _id_of(title: str) -> Any:
    """DB-Id des Favoriten mit *title* (der Import hat ihn angelegt)."""

    async def run():
        async with FavoritesManager()._db_session() as session:
            rows = (await session.execute(
                select(Favorite.id, Favorite.title))).all()
            for db_id, name in rows:
                if name == title:
                    return db_id
        return None

    return asyncio.run(run())


def _titles() -> set[str]:
    async def run():
        async with FavoritesManager()._db_session() as session:
            rows = (await session.execute(select(Favorite.title))).scalars().all()
        return set(rows)

    return asyncio.run(run())


def _delete(fav_id: int) -> bool:
    return asyncio.run(FavoritesManager().delete(fav_id))


def _cli(line: str, player_id: str | None = MAC) -> str:
    handler = CLIHandler()
    ctx = CLIContext(player_id=player_id)
    lines = asyncio.run(handler.dispatch(ctx, line))
    assert len(lines) == 1, lines
    return lines[0]


# ── 1. Löschen eines Streams: Liste + Datei ───────────────────────────────


def test_delete_removes_stream_from_list_and_opml(tmp_path, monkeypatch):
    """``OpmlFavorites.pm:412-426`` + ``Opml.pm:91-138``."""
    _, opml = _temp_db(tmp_path, monkeypatch)
    _import()
    assert "1Mix" in _titles()

    assert _delete(_id_of("1Mix")) is True

    # Liste (DB)
    assert "1Mix" not in _titles()
    # Ablage (OPML) — der Eintrag ist aus der Datei verschwunden
    text = opml.read_text(encoding="utf-8")
    assert "Tune.ashx" not in text
    # Gegenprobe: alles andere steht unverändert da
    assert "Hirschmilch Chillout" in text
    assert "SWR 3" in text
    assert 'text="Chill"' in text
    assert 'text="Lokal"' in text


# ── 2. Löschen innerhalb eines Ordners ────────────────────────────────────


def test_delete_inside_a_folder_keeps_the_folder_and_siblings(tmp_path,
                                                              monkeypatch):
    """Perls Crumb-Pfad (``XMLBrowser.pm:341-353``) — nur die eine Zeile."""
    _, opml = _temp_db(tmp_path, monkeypatch)
    _import()

    assert _delete(_id_of("Hirschmilch Electronic")) is True

    text = opml.read_text(encoding="utf-8")
    assert "electronic.mp3" not in text
    assert "chillout.mp3" in text          # Geschwister bleibt
    assert 'text="Chill"' in text          # Ordner bleibt
    assert "Hirschmilch Chillout" in _titles()
    assert "Hirschmilch Electronic" not in _titles()


# ── 3. Ordner löschen nimmt den ganzen Teilbaum ───────────────────────────


def test_delete_of_a_folder_drops_its_whole_subtree(tmp_path, monkeypatch):
    """``session.delete`` kaskadiert; die Outlines gehen mit dem Ordner."""
    _, opml = _temp_db(tmp_path, monkeypatch)
    _import()

    assert _delete(_id_of("Chill")) is True

    assert "Chill" not in _titles()
    assert "Hirschmilch Chillout" not in _titles()
    assert "Hirschmilch Electronic" not in _titles()
    text = opml.read_text(encoding="utf-8")
    assert "chillout.mp3" not in text
    assert "electronic.mp3" not in text
    assert 'text="Chill"' not in text
    # der zweite Ordner und der Wurzel-Eintrag bleiben unberührt
    assert 'text="Lokal"' in text
    assert "Tune.ashx" in text
    assert "SWR 3" in _titles()


# ── 4. Die Backfill-Falle: kein Wiederauftauchen nach dem Neustart ────────


def test_backfill_after_delete_does_not_resurrect(tmp_path, monkeypatch):
    """Der Neustart läuft ``ensure_opml_imported`` erneut — der Gelöschte
    darf dabei nicht wieder in die DB gemerged werden."""
    monkeypatch.setattr("lyrion.music.favorites._opml_import_done", False)
    _temp_db(tmp_path, monkeypatch)
    _import()
    assert _delete(_id_of("1Mix")) is True
    assert _delete(_id_of("Hirschmilch Chillout")) is True
    assert _delete(_id_of("Lokal")) is True

    # Neustart: das Flag ist weg, der Merge läuft wieder über die Datei
    monkeypatch.setattr("lyrion.music.favorites._opml_import_done", False)
    _import()

    assert "1Mix" not in _titles()
    assert "Hirschmilch Chillout" not in _titles()
    assert "Lokal" not in _titles()
    # der überlebende Ordner samt Kind ist weiterhin genau einmal da
    assert "Chill" in _titles()
    assert "Hirschmilch Electronic" in _titles()
    assert len([t for t in _titles() if t == "Chill"]) == 1


# ── 5. CLI: Perls getaggte Form ───────────────────────────────────────────


def test_cli_delete_accepts_perls_tagged_item_id(tmp_path, monkeypatch):
    """``Plugin.pm:933`` ``$request->getParam('item_id')`` — die Form, die ein
    Controller nach ``favorites items`` zurückschickt."""
    _temp_db(tmp_path, monkeypatch)
    _import()
    fav_id = _id_of("1Mix")

    assert _cli(f"favorites delete item_id:{fav_id}") == \
        f"favorites delete item_id%3A{fav_id}"
    assert "1Mix" not in _titles()


def test_cli_delete_accepts_a_session_crumb_and_a_url(tmp_path, monkeypatch):
    """Session-Crumb (``<sid>.<index>``) und ``url:`` wie in Perl."""
    _temp_db(tmp_path, monkeypatch)
    _import()

    # der Wurzel-Stream steht an Position 2 (Chill, Lokal, 1Mix)
    assert _cli("favorites delete item_id:deadbeef.2") == \
        "favorites delete item_id%3Adeadbeef.2"
    assert "1Mix" not in _titles()

    _cli("favorites delete url:https%3A%2F%2Fliveradio.swr.de%2Fsw282p3%2Fswr3%2F")
    assert "SWR 3" not in _titles()
    assert "Lokal" in _titles()          # leerer Ordner bleibt


def test_cli_delete_without_a_hit_deletes_nothing(tmp_path, monkeypatch):
    """``Plugin.pm:943-946`` — weder Eintrag noch URL ⇒ bad params, kein Treffer."""
    _temp_db(tmp_path, monkeypatch)
    _import()
    before = _titles()

    _cli("favorites delete item_id:999999")
    _cli("favorites delete url:http%3A%2F%2Fgibt.es.nicht%2Fx")

    assert _titles() == before
