# Controller-Parität Runde 2 — Nachweis der Änderungen OHNE Serverneustart

## Harness-Lauf (Auftragsschritt 5)

```
source .venv/bin/activate && python tools/controller_parity.py \
  --out .hermes/gap-analysis/controller-parity-after-fixes.md
-> Bewertung: gleich=21, abweichend=48, fehlt=12, Fehler (unser)=2,
   Fehler (Perl)=20   (103 Kommandos)
```

Die Zahlen sind **identisch mit dem Ausgangsstand der Aufgabe**, weil der
laufende Dev-Server (127.0.0.1:9000) weiterhin den ALTEN Code ausliefert —
das Neustarten ist dem Parent vorbehalten. Der folgende In-Process-Nachweis
misst genau denselben, neuen Code ohne Serverneustart.

* Datum: 2026-09-13
* Werkzeug: `/tmp/parity_newcode_check.py` (nur lesend) — benutzt die
  Bewertungslogik aus `tools/controller_parity.py` (`cp.evaluate`/`cp.shape`).
* Referenz: Perl-LMS 9.1.1 `192.168.1.90:9000` (HTTP, read-only),
  Player `ca:c8:c7:26:6d:38`.
* „wir": **unsere NEUEN Handler in-process** (`JSONRPCAPI._slim_request`,
  derselbe Prozess, dieselbe Bibliotheks-DB read-only). Es wurde **kein**
  Server gestartet/gestoppt — der laufende Dev-Server (127.0.0.1:9000)
  liefert noch den alten Code, deshalb kann der Harness vor dem Neustart des
  Parent keinen Unterschied messen.
* Player-Stub: `PlayerState(mac=1C:87:2C:47:FC:36, ip=192.168.1.130,
  port=48248, mode=play, elapsed=354.191, playlist=[])` — nur für die
  Controller-Struktur, keine erfundenen Player-Daten am Live-Client.

## Ergebnisse je geändertem Kommando

| Kommando | vorher (Harness) | jetzt |
|---|---|---|
| `rescanprogress` | fehlt | **gleich** |
| `roles` | fehlt | **gleich** |
| `player count ?` | fehlt | **gleich** |
| `player id ?` | fehlt | **gleich** |
| `player name ?` | fehlt | **gleich** |
| `player model ?` | fehlt | **gleich** |
| `player ip ?` | fehlt | **gleich** |
| `years 0 10` | fehlt | **gleich** |
| `years 0 10 sort:year` | fehlt | **gleich** (ein Lauf: nur Schlüsselreihenfolge — Perl-Hash-Reihenfolge) |
| `tracks 0 10` | fehlt | abweichend (Perl `titles_loop` vorhanden; wir zusätzlich `item_loop`/`loop_loop`/`offset` + reichere Items) |
| `tracks 0 10 album_id:2` | fehlt | abweichend (Perl nur `count`, weil Album 2 keine Tracks hat) |
| `apps 0 50` | fehlt | abweichend (`appss_loop` vorhanden, leer — Perl hat 1 Plugin-Item) |
| `radios 0 10` | **Fehler (unser)** | abweichend (`radioss_loop` vorhanden, leer) |
| `browse radios 0 10` | **Fehler (unser)** | abweichend (`radioss_loop` vorhanden) |
| `musicfolder 0 100` | `musicfolder_loop` (falscher Schlüssel) | `folder_loop` + Aliase |
| `status*` | abweichend | Perl-Box brach in diesem Lauf ab (`conn_closed`) → nicht messbar |

Summe dieses Teil-Laufs (16 Kommandos + Perl-Abbrüche):
`{'gleich': 9, 'abweichend': 12, 'Fehler (Perl)': 9}`.

Kein geändertes Kommando ist mehr „fehlt" oder „Fehler (unser)";
`gleich` sind `rescanprogress`, `roles`, `player count ?`, `player id ?`,
`player name ?`, `player model ?`, `player ip ?`, `years 0 10`,
`years 0 10 sort:year`.

## Live-Proben (wörtlich, nur lesend, 2026-09-13)

Perl `192.168.1.90:9000/jsonrpc.js`, Player `ca:c8:c7:26:6d:38`::

    {"id":1,"method":"slim.request","params":["ca:c8:c7:26:6d:38",["player","count","?"]]}
      -> {"result":{"_count":3}}
    ["player","ip","?"]     -> {"result":{"_ip":"192.168.1.130:45614"}}
    ["player","model","?"]  -> {"result":{"_model":"squeezelite"}}
    ["rescanprogress"]      -> {"result":{"rescan":0}}
    ["years","0","2"]       -> {"result":{"count":65,"years_loop":[{"year":0,
                               "favorites_url":"db:year.id=0"},
                               {"favorites_url":"db:year.id=2026","year":2026}]}}
    ["tracks","0","2"]      -> {"result":{"count":80218,"titles_loop":[{"id":129497,
                               "title":"!!!!!!!", …}]}}
    ["roles"]               -> {"result":{"count":6}}
    ["apps","0","5"]        -> {"result":{"count":1,"appss_loop":[{"type":"xmlbrowser",
                               "cmd":"sounds","weight":90,"name":"Sounds & Effekte",
                               "icon":"plugins/Sounds/html/images/icon.png"}]}}
    ["radios","0","10"]     -> {"result":{"count":10,"radioss_loop":[{"cmd":"presets",
                               "type":"xmlbrowser","weight":5,
                               "name":"Eigene Voreinstellungen"}, …]}}
    ["musicfolder","0","100"] -> {"result":{"count":303,"folder_loop":[…]}}

unser Dev-Server (127.0.0.1:9000, noch alter Code)::

    ["player","count","?"] -> {"jsonrpc":"2.0","result":["player count 1"],"id":1}
    ["years","0","2"]      -> {"jsonrpc":"2.0","result":["years 0 2 year%3A2026 …"],"id":1}
    ["tracks","0","2"]     -> {"jsonrpc":"2.0","result":["tracks 0 2"],"id":1}
    ["roles"]              -> {"jsonrpc":"2.0","result":["roles"],"id":1}

## In-Process-Beleg für `musicfolder` (Dateigrenze-Auftrag)

```
['musicfolder','0','2']   -> {'count': 303, 'folder_loop': [2], 'item_loop': [2],
                              'loop_loop': [2], 'offset': 0}
['musicfolder','0','100'] -> {'count': 303, 'folder_loop': [100], …}
```

`count = 303` — dieselbe Zahl wie das Perl-LMS (Queries.pm:2507;
`lyrion.media.folders.musicfolder_result`, media dirs Misc.pm:727-756).
