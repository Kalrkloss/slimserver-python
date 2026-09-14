"""The jive popup Perl sends after ``favorites add``.

``Slim/Plugin/Favorites/Plugin.pm:895-912`` (after ``$favs->save``)::

    # show feedback to jive
    if ($request->source && $request->source =~ /\\/slim\\/request/) {
        $client->showBriefly({
            jive => {
                type => 'mixed',
                style => 'favorite',
                'icon' => $icon || $favs->icon($url),
                text => [ $client->string('FAVORITES_ADDING'), $title ],
            },
        });
    }

``showBriefly`` (``Slim/Display/Display.pm``) caches it for ``status``
(``renderCache->{showBriefly}``, :280-283, 15 s) and pushes
``['displaynotify', 'showbriefly', $parts, $duration]`` to the
``displaystatus`` subscribers (:285-288 → :905-913) — that push is the event
the controller renders as the confirmation.

``$client->string('FAVORITES_ADDING')`` comes from
``Slim/Plugin/Favorites/strings.txt:38-42`` (EN ``Saving favorites...``,
DE ``Favoriten werden gespeichert ...``); ``$favs->icon($url)`` is
``OpmlFavorites.pm:83-88`` → ``iconForURL($url) || 'html/images/favorites.png'``,
and an http(s) stream answers ``html/images/radio.png`` (``HTTP.pm:1147``).
"""

import asyncio
from contextlib import asynccontextmanager

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from lyrion.database.schema import Base
from lyrion.music.favorites import FavoritesManager
from lyrion.web.api import JSONRPCAPI

MAC = "1C:87:2C:47:FC:36"
URL = "http://example.com/radio.mp3"
FOLDER_TITLE = "Meine Sender"

#: The two catalog values of ``FAVORITES_ADDING`` (Favorites/strings.txt:38-42).
ADDING_TEXTS = {"Saving favorites...", "Favoriten werden gespeichert ..."}


@pytest.fixture
def fav_db(tmp_path, monkeypatch):
    """Throwaway favourites DB and a quiet ``favorites changed`` notifier."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path/'lyrion.db'}")
    factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)

    async def _create():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    asyncio.run(_create())

    @asynccontextmanager
    async def _session_ctx():
        session = factory()
        try:
            yield session
            await session.commit()
        finally:
            await session.close()

    monkeypatch.setattr("lyrion.database.sqlite_helper.db_session", _session_ctx)
    monkeypatch.setattr("lyrion.music.favorites._opml_path", lambda: None)
    monkeypatch.setattr("lyrion.music.favorites._opml_import_done", False)
    # The manager captures ``db_session`` in its constructor, and the module
    # keeps ONE singleton: drop it so it binds to this tmp DB, not to the one
    # of a previous test (which would make the add land in another file).
    monkeypatch.setattr("lyrion.music.favorites._manager", None)
    # The 'favorites changed' push needs a running loop; not under test here.
    monkeypatch.setattr("lyrion.music.favorites._notify_favorites_changed",
                        lambda: None)
    yield
    asyncio.run(engine.dispose())


class FakeCometd:
    """Records Perl's ``displaynotify`` → ``displaystatus`` push."""

    def __init__(self):
        self.displays: list[tuple] = []

    async def notify_display(self, player_id: str, kind: str = "showbriefly",
                             jive: dict | None = None,
                             duration: int | None = None) -> None:
        self.displays.append((player_id, kind, jive, duration))

    async def notify_favorites_changed(self) -> None:
        return None


@pytest.fixture
def fake_cometd(monkeypatch):
    mgr = FakeCometd()
    monkeypatch.setattr("lyrion.web.cometd.get_manager", lambda: mgr)
    return mgr


def _request(args: list, pid: str | None = MAC, api=None) -> dict:
    return asyncio.run((api or JSONRPCAPI())._slim_request(pid, args))


def _push(fake_cometd) -> dict:
    """The ``$parts->{'jive'}`` of the single pushed displaynotify."""
    assert len(fake_cometd.displays) == 1, "exactly one displaynotify"
    return fake_cometd.displays[0][2]


def test_favorites_add_pushes_the_perl_showbriefly_event(fav_db, fake_cometd):
    """The raw push: ``showbriefly`` carrying Perl's ``favorite`` hash."""
    result = _request(["favorites", "add", "title:Mein Sender", f"url:{URL}"])

    assert result == {"count": 1}                       # Plugin.pm:859
    pid, kind, _jive, _duration = fake_cometd.displays[0]
    assert pid == MAC                                   # $request->client
    assert kind == "showbriefly"                        # Display.pm:287
    jive = _push(fake_cometd)
    assert set(jive) == {"type", "style", "icon", "text"}   # Plugin.pm:904-911
    assert jive["type"] == "mixed"                      # :906
    assert jive["style"] == "favorite"                  # :907
    assert jive["icon"] == "html/images/radio.png"      # :908 + HTTP.pm:1147
    assert jive["text"] == [jive["text"][0], "Mein Sender"]  # :909
    assert jive["text"][0] in ADDING_TEXTS, jive["text"][0]


def test_favorites_add_answers_displaystatus_with_the_popup(fav_db, fake_cometd):
    """``displaystatus`` answers the cached popup (Display.pm:280-283).

    That is what a controller reads when it (re)subscribes or polls — Perl
    keeps the notification and reports ``type`` + ``display``
    (``Queries.pm:1651-1701``).
    """
    # One API object = one server: the popup cache lives on the handler
    # instance that served the request (as it does live).
    api = JSONRPCAPI()
    _request(["favorites", "add", "title:Mein Sender", f"url:{URL}"], api=api)
    answer = _request(["displaystatus"], api=api)
    assert answer.get("type") == "showbriefly"
    display = answer.get("display") or {}
    assert display.get("style") == "favorite"
    assert display.get("icon") == "html/images/radio.png"
    assert display["text"][1] == "Mein Sender"


def test_favorites_add_keeps_storing_the_entry(fav_db, fake_cometd):
    """Vorher/Nachher — the favourite itself still lands, list intact."""
    assert asyncio.run(FavoritesManager().list_items()) == []

    _request(["favorites", "add", "title:Mein Sender", f"url:{URL}"])

    items = asyncio.run(FavoritesManager().list_items())
    assert [i["title"] for i in items] == ["Mein Sender"]
    assert items[0]["url"] == URL
    assert items[0]["type"] == "stream"                 # Plugin.pm:854 'audio'


def test_folder_seeded_before_the_add_stays_in_the_list(fav_db, fake_cometd):
    """A favourite added into a folder keeps the OPML document order."""
    _request(["favorites", "addlevel", f"title:{FOLDER_TITLE}"])
    folder = asyncio.run(FavoritesManager().list_items())[0]
    _request(["favorites", "add", "title:Mein Sender", f"url:{URL}",
              f"item_id:{folder['id']}"])

    assert [i["title"] for i in asyncio.run(FavoritesManager().list_items())] == [
        FOLDER_TITLE]
    children = asyncio.run(FavoritesManager().list_items(folder["id"]))
    assert [c["title"] for c in children] == ["Mein Sender"]
    assert len(fake_cometd.displays) == 2


def test_addlevel_gets_the_popup_with_the_favorites_icon(fav_db, fake_cometd):
    """Perl fires the popup after ``save`` for BOTH commands (:895-912).

    An ``addlevel`` has no URL, so ``icon($url)`` falls back to
    ``html/images/favorites.png`` (OpmlFavorites.pm:87-88).
    """
    assert _request(["favorites", "addlevel", f"title:{FOLDER_TITLE}"]) == {
        "count": 1}

    jive = _push(fake_cometd)
    assert jive["icon"] == "html/images/favorites.png"
    assert jive["text"][1] == FOLDER_TITLE
    assert jive["text"][0] in ADDING_TEXTS


def test_a_client_icon_wins(fav_db, fake_cometd):
    """``$icon || $favs->icon($url)`` — the client's icon has priority (:908)."""
    _request(["favorites", "add", "title:Mein Sender", f"url:{URL}",
              "icon:/imageproxy?icon=1"])

    assert _push(fake_cometd)["icon"] == "/imageproxy?icon=1"


def test_request_without_a_player_pushes_nothing(fav_db, fake_cometd):
    """No ``$request->client`` → no showBriefly."""
    assert _request(["favorites", "add", "title:Ohne", f"url:{URL}"],
                    pid=None) == {"count": 1}
    assert fake_cometd.displays == []


def test_missing_cometd_manager_never_breaks_the_add(fav_db, monkeypatch):
    """A popup is feedback; a broken push must not lose the favourite."""
    monkeypatch.setattr("lyrion.web.cometd.get_manager", lambda: None)

    assert _request(["favorites", "add", "title:Mein Sender", f"url:{URL}"]) == {
        "count": 1}
    assert [i["title"] for i in asyncio.run(FavoritesManager().list_items())] == [
        "Mein Sender"]


def test_bad_params_are_rejected_without_a_popup(fav_db, fake_cometd):
    """Without title AND url Perl returns before ``save`` (:872-877)."""
    assert _request(["favorites", "add", "title:Nur Titel"]) == {}
    assert fake_cometd.displays == []
    assert asyncio.run(FavoritesManager().list_items()) == []
