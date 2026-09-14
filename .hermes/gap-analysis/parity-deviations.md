# Paritäts-Audit: bewusste Abweichungen vom Perl-LMS (Python-Port)

Richtlinie: **keine Workarounds — 100 % LMS-Verhalten.** Jede hier gelistete
Zeile ist ein Bug-Kandidat, nicht eine Designentscheidung. Wo eine Abweichung
schon behoben wurde, steht das dabei.

Erhebung (2026-09-14): `grep -rn -i "deviat|superset|workaround|bewusst|
repurpos|nicht portiert|vereinfacht|documented|deliberately|on purpose|
intentional|Python-side|not Perl|Perl has no" src/ --include=*.py`
(69 Treffer) + Verifikation jeder Fundstelle gegen den Code. Perl-Referenz,
die hier lesbar vorliegt: `/tmp/lms-ref` (`Cometd.pm`, `HTTP.pm`,
`Slim/Web/Cometd/Manager.pm`, `Slim/Control/{Queries,Request,Commands}.pm`,
`Slim/Music/Info.pm`, `Slim/Player/*`, `Slim/Utils/Timers.pm`). Für Stellen,
deren Perl-Quelle hier nicht lesbar ist (`XMLBrowser.pm`, `Jive.pm`,
`Prefs.pm`, `ImageProxy.pm`, `Alarm.pm`, `Strings.pm`), steht
**UNVERIFIZIERT** — dann ist nur die Port-Seite belegt.

Spalte „Aufwand“: klein (≤ ~1 h, lokal), mittel (ein Modul/eine Route),
groß (Schema/Browse-Modell, mehrere Module).

---

## D1 — `advice` im /meta/connect-Ack ist ein Superset

* **Ort:** `src/lyrion/web/cometd.py:428-449` (`connect_advice`), genutzt von
  `connect_ack` :452-470 und `src/lyrion/web/app.py:154`.
* **Perl-Soll (verifiziert):** `Cometd.pm:266-276` (erstes Event der
  /meta/(re)connect-Antwort) setzt genau
  `advice => { interval => $streaming ? RETRY_DELAY : 0 }` — **nur** `interval`.
* **Ist:** `{"reconnect": "retry", "interval": 0|5000, "timeout": 60000}`.
  `timeout` ist die 60 s aus dem Handshake (`Cometd.pm:251`), `reconnect` ein
  Restposten der alten Python-Form.
* **Auswirkung:** libcometd/Jive ignorieren unbekannte Advice-Schlüssel
  (Bayeux erlaubt sie), also heute unauffällig; ein strikter Bayeux-Client
  könnte `reconnect:"retry"` anders auswerten als den Default `handshake`.
  Kein Client-Fehler beobachtet.
* **Aufwand:** klein.
* **Vorschlag:** `connect_advice` auf `{"interval": …}` reduzieren, Tests
  (`tests/test_cometd_asgi.py:353`, `test_cometd_controllers.py:467/626`,
  `test_cometd_push.py:1188`) und die Fixtures nachziehen; danach
  Byte-Vergleich gegen Perl-Antwort.

## D2 — Favoriten-Reihenfolge: Ordner zuerst, dann alphabetisch

* **Ort:** `src/lyrion/music/favorites.py:88-104` (`list_items`:
  `.order_by(Favorite.url.is_not(None), func.lower(Favorite.title))`); wirkt
  über `src/lyrion/web/api.py:2425+` (`favorites items`) und
  `:2296-2312` (`_fav_position_path`, Index = Position in dieser Sortierung).
  `list_tree` (:106-118) sortiert dagegen nach `position, title` —
  **inkonsistent innerhalb des Ports**.
* **Perl-Soll (UNVERIFIZIERT, Quelle im Port zitiert):** Perl liest die
  OPML-Outlines in Dokument-Reihenfolge (`Slim/Plugin/Favorites/Opml.pm` in
  den Port-Kommentaren), d. h. die gespeicherte `position` bestimmt die
  Reihenfolge — nicht der Titel.
* **Auswirkung:** In Squeezer/Squeeze Client/SqueezePlay steht die
  Favoritenliste in anderer Reihenfolge als am Perl-Server; die
  `item_id`s (`<sid>.<index>`) sind intern konsistent (Taps funktionieren),
  weichen aber von Perls Indizes ab — ein Vergleich zweier Serverlogs zeigt
  verschiedene Indizes.
* **Aufwand:** klein (eine ORDER-BY, plus `list_tree` angleichen).
* **Vorschlag:** `order_by(Favorite.position, Favorite.id)` in `list_items`
  und `list_tree`; `position` beim OPML-Import füllen (geschieht laut
  Import-Pfad :120-150 schon) und im Test gegen eine OPML-Fixture pinnen.

## D3 — `go.player: 0` auf Home-/Menüknoten

* **Ort:** `src/lyrion/web/menus.py:389-394` (`_go`) und alle Aufrufer
  (`:148,168,179,201,203,207,233,240,242,245,247,250` …).
* **Perl-Soll (UNVERIFIZIERT):** laut Port-Kommentar lässt Perl das Feld auf
  Menüknoten/Aktionen ohne Playerbindung weg (`Jive.pm`-`_jiveGo`-Pfade).
* **Ist:** immer `"player": 0`.
* **Auswirkung:** Jive/SqueezePlay prüft `player`, um zu entscheiden, ob eine
  Aktion einen Player-Kontext braucht; `0` heißt „kein Player" — Verhalten
  entspricht vermutlich dem Fehlen des Felds, ist aber nicht belegt.
  **UNKLAR**, ob ein Client das unterscheidet.
* **Aufwand:** klein, aber **erst messen**: identische Requests gegen
  `192.168.1.90:9000` und den Port diffen, dann Feld weglassen.
* **Vorschlag:** Live-Diff `favorites items` / `settings menu` (nur lesend),
  danach `_go` das Feld nur bei Playerbindung setzen.

## D4 — Ordner-IDs sind virtuell (`crc32`), Perl liefert `tracks.id`

* **Ort:** `src/lyrion/web/api.py:685-712` (Kommentar + Ableitung) und
  `_folder_dir_by_id`; die `dir`-Zeilen selbst fehlen.
* **Perl-Soll (UNVERIFIZIERT, Quelle im Port zitiert):** Verzeichnisse liegen
  als `content_type='dir'`-**Zeilen in `tracks`**; `folder_id` ist die echte
  `tracks.id`. Deshalb versteht Perl eine `folder_id` ohne Zusatzwissen.
* **Ist:** stabile, aus dem Pfad abgeleitete positive Ganzzahl (`crc32`),
  die der Port rückwärts auflöst.
* **Auswirkung:** Ein Client, der eine `folder_id` speichert (Squeezer,
  Squeeze Client) und später wiederverwendet, funktioniert nur gegen diesen
  Server; ein Wechsel auf Perl (oder ein Perl-Client, der eine Perl-id
  mitschickt) bricht. Paginierung/`item_loop`-Reihenfolge bleibt korrekt.
* **Aufwand:** groß (DB-Schema/Scanner: dir-Zeilen anlegen, IDs stabil
  halten).
* **Vorschlag:** beim Browsen/Library-Import je Verzeichnis eine
  `tracks`-Zeile mit `content_type='dir'` materialisieren, `folder_id` =
  `tracks.id`; `crc32` nur noch als Fallback für nicht materialisierte
  Pfade. Vorher: `tracks`-Schema gegen Perl prüfen.

## D5 — `/imageproxy/...` fehlt

* **Ort:** keine Route in `src/lyrion/web/app.py`, `networking/cometd_stream.py`
  oder `web/api.py` (grep „imageproxy" → nur ein Kommentar bei
  `app.py:502` über `/music/…/cover.jpg`).
* **Perl-Soll (UNVERIFIZIERT, `Slim/Web/ImageProxy.pm` hier nicht lesbar):**
  Perl liefert über `/imageproxy/…` u. a. die TuneIn-/Sender-Logos und
  skaliert Bilder über `?w=&h=`.
* **Auswirkung:** SqueezePlay/Squeezer zeigen bei Sender-Logos und manchen
  Menü-Icons den Platzhalter statt des Logos (404 → Fallback-Bild).
* **Aufwand:** mittel (Route + Proxy-Fetch + Größenparameter).
* **Vorschlag:** `/imageproxy/<url|id>` nach Perl nachbauen (Remote-GET,
  `w`/`h`-Resize wie `_resize_cover`), Tests mit gemocktem Upstream.

## D6 — `_defeatDestructiveTouchToPlay`: benannter, unbekannter Client

* **Ort:** `src/lyrion/web/api.py:367-412`
  (`_defeat_destructive_touch_to_play`), `:256-276` (`_defeat_pref_default`),
  ausgewertet in `favorites_menu.py:45+`.
* **Perl-Soll (UNVERIFIZIERT, Zitat aus `XMLBrowser.pm:1976` im Port):**
  Perl löst den Client aus dem Socket auf; eine Anfrage mit einer MAC, die
  der Server nicht kennt, wird **gar nicht beantwortet** (Verbindung wird
  ohne Ergebnis geschlossen). `!$client` bzw. ein unbekannter Client nimmt
  den *defeated* Branch (playControl).
* **Ist:** portiert bis auf einen Fall — ein **benannter**, dem Port aber
  unbekannter Client (`player is None` + `pid` gesetzt) behält den
  nicht-defeated `play`-Branch statt wie Perl abzubrechen.
* **Auswirkung:** Favoriten-Zeilen eines nicht installierten Players liefern
  `goAction: "play"` statt `playControl` → ein Tap schaltet/startet anders
  als am Perl-Server (im Test nur mit künstlichem `pid` erreichbar; echte
  Controller sind installiert).
* **Aufwand:** klein.
* **Vorschlag:** den `pid`-ohne-Player-Fall auf den defeated Branch legen
  (Perl-Verhalten) oder die Anfrage wie Perl ohne Antwort schließen;
  Fixture in `tests/test_favorites_menu.py` pinnen.

## D7 — Connect-Ack steht nicht an erster Stelle der Antwort

* **Ort:** `src/lyrion/web/app.py:172-178` (Kommentar „P3-4"), `first =
  list(replies) + [ack]`.
* **Perl-Soll (verifiziert):** `Cometd.pm:269-270`: „We want the
  /meta/(re)connect response to always be the first event sent in the
  response, so it's stored in the special first_event slot".
* **Ist:** die Batch-Acks (z. B. `/meta/subscribe`) kommen vor dem
  Connect-Ack.
* **Auswirkung:** libcometd verarbeitet Nachrichten in Reihenfolge; für
  SqueezeClient/Squeezer beobachtet kein Fehler, ungetestet für
  Material/andere Clients. Abweichung bleibt eine Abweichung.
* **Aufwand:** klein.
* **Vorschlag:** `[ack] + replies` und die Tests
  (`test_cometd_asgi.py` Connect-Tests) anpassen; identisch in
  `networking/cometd_stream.py` (dort nicht in meinem Änderungsbereich).

## D8 — Gehaltene Streaming-Antwort: Historie

* **Ort:** `src/lyrion/web/app.py:118-270`, `cometd.py:70-88`.
* **Status: BEHOBEN (2026-09-14, dieser Commit).** Der 5-s-Abbruch
  (`STREAMING_HOLD_WINDOW`) ist raus; die ASGI-Antwort bleibt offen, bis der
  Peer geht oder der Client verworfen wird (Perl `Cometd.pm:283/288-297`,
  `HTTP.pm:277`). Live gemessen: vorher Ende nach 5,00 s Stille, nachher
  > 20 s offen, RST-Disconnect → Grace statt Drop; Pipelining auf dem
  öffentlichen Port 9000 weiterhin beantwortet.

## D9 — Ereignis-Queue hat ein Python-seitiges Limit (`MAX_QUEUED_EVENTS`)

* **Ort:** `src/lyrion/web/cometd.py:106-117` (Cap 256), angewendet beim
  `push` (:968+).
* **Perl-Soll (verifiziert):** `Slim/Web/Cometd/Manager.pm:181-189`
  (`queue_events`) hat kein Limit; Perl verlässt sich auf
  `LONG_POLLING_AUTOKILL` (180 s, `Cometd.pm:49`) und `webCloseHandler`.
* **Ist:** älteste Events werden verworfen, sobald 256 warten.
* **Auswirkung:** Nur erreichbar, wenn ein Client mit Abos lange ohne
  offene Verbindung bleibt und sehr viele Events entstehen (Player-Events
  sind klein/gebündelt). Dann fehlen einem Client die ältesten Events —
  Perl hätte sie geliefert.
* **Aufwand:** klein.
* **Vorschlag:** Cap entfernen und stattdessen den Perl-Pfad nutzen
  (Autokill verwirft den Client nach 180 s) — oder Cap belegen und als
  begründete Abweichung in der Doku führen. Ohne Beleg: entfernen.

## D10 — Nackte `**` / `/**` Subscribe-Muster gelten als Match-all

* **Ort:** `src/lyrion/web/cometd.py:156-203` (`_is_glob`, `_channel_matches`).
* **Perl-Soll (verifiziert):** `add_channels` schreibt nur
  `/<etwas>/**` bzw. `/<etwas>/*` in Regexe um und gibt alles andere an
  `Tie::RegexpHash`; ein nacktes `**` kompiliert dort nicht
  („Quantifier follows nothing").
* **Ist:** `**`/`/**` matchen jeden Kanal.
* **Auswirkung:** Ein Client, der `**` abonniert, bekommt hier alle Events,
  am Perl-Server dagegen einen Subscribe-Fehler. Kein bekannter Client
  sendet das (jive sendet `/<cid>/**`).
* **Aufwand:** klein.
* **Vorschlag:** Toleranz entfernen oder als dokumentierten Superset
  belassen — Null-Fehlerkosten, aber Entscheidung dokumentieren.

## D11 — CLI: echte Aktionen dort, wo Perl nur zurückspiegelt

* **Ort:** `src/lyrion/control/cli_commands.py:1409-1428` (`volume`),
  `:1313-1326` (`prev`), `:1337+` (`next`), `:4030-4125`
  (`radio add|delete|top` — eigene Verben).
* **Perl-Soll (verifiziert, live + `Slim/Control/Request.pm:474-637`):**
  `volume`, `prev`, `next`, `radio` haben **keinen** Dispatch-Eintrag;
  Perl echot die Anfrage unverändert (Status 104 →
  `Plugin/CLI/Plugin.pm:657-663`). Live 2026-09-12: `volume 50` → `volume 50`.
* **Ist:** Antwort identisch (Echo), **aber** der Port ändert zusätzlich
  Lautstärke/Titel — am Perl-Server passiert nichts.
* **Auswirkung:** Ein Telnet/CLI-Client (oder ein Skript, das ein Echo als
  „ignoriert" liest) verändert hier den Player, wo Perl nichts tut.
* **Aufwand:** klein.
* **Vorschlag:** Nebenwirkung entfernen (nur Echo) oder die Verben hinter
  einen expliziten Nicht-Perl-Schalter legen; `radio`-Verben als eigenen
  Namensraum (z. B. `lyrion radio …`) führen.

## D12 — `mediadirs`/`musicfolder`-Reihenfolge weicht ab

* **Ort:** `src/lyrion/media/folders.py:250-292` (`defaultMediaDirs`-
  Äquivalent, `effective_media_dirs`).
* **Perl-Soll (UNVERIFIZIERT, Zitat `Prefs.pm:687-712` im Port):** Perls
  Default legt den OS-Musikordner zuerst fest; `mediadirs` ist nie leer,
  solange ein Musikordner existiert.
* **Ist:** gescannte Bibliotheks-Wurzeln zuerst, `~/Music` als letzter
  Ausweg; eine leere `mediadirs`-Pref wird toleriert.
* **Auswirkung:** `musicfolder`/`mediadirs`-Antworten können eine andere
  Reihenfolge (und bei leerer Pref einen anderen ersten Eintrag) liefern als
  Perl — sichtbar in der Web-UI und für „Zufälliges Album"-Pfade.
* **Aufwand:** klein (Reihenfolge) bis mittel (leerer Store).
* **Vorschlag:** Reihenfolge an Perl angleichen, sobald bekannt ist, warum
  die Pref leer ist; sonst als begründete Abweichung im Settings-Doc führen.

## D13 — Settings: nicht nachgebildete Felder und Side-Effects

* **Ort:** `src/lyrion/web/settings.py:48-60` (UNKLAR-Abschnitt).
* **Perl-Soll (UNVERIFIZIERT):** `Server/Basic.pm:39-62,118-127` (Rescan-
  Trigger, `pref_rescan`/`pref_rescantype`), `Player/Audio.pm:35-117` /
  `Player/Display.pm:44-61` (fähigkeitsabhängige Felder),
  `Player/Alarm.pm:60-108,139-177` (Alarm anlegen/löschen).
* **Ist:** Felder fehlen bzw. werden unbedingt geliefert; Alarm-Menü
  schreibt nur Prefs; `mediadirs` wird kommasepariert gespeichert.
* **Auswirkung:** Web-UI-Formulare sind unvollständig; ein Rescan wird über
  diese Seite nicht ausgelöst; Player-Fähigkeitsfilter fehlen.
* **Aufwand:** mittel bis groß.
* **Vorschlag:** schrittweise: (1) `mediadirs`-Format an Perl angleichen,
  (2) Rescan-Pref + Trigger, (3) Kapazitätsfilter. Jeder Schritt mit
  Live-Diff der HTML-Antwort.

## D14 — Alarm-Tagesmaske und `volume = -1`-Sentinel

* **Ort:** `src/lyrion/alarms.py:64-70` (`day_int`, eigenes Bitfeld),
  `:254-262` (`volume = -1` = „unverändert lassen").
* **Perl-Soll (UNVERIFIZIERT, Zitate `Slim/Utils/Alarm.pm:116-118,183-197`,
  `:593-628` im Port):** Tage werden über `$alarm->day(0..6)` mit
  `0 = Sonntag` adressiert; es gibt keinen Sentinel, `fireAlarm` liest die
  Lautstärke unmittelbar vor dem Umschalten.
* **Auswirkung:** Nur intern sichtbar, solange die Alarm-API des Ports
  benutzt wird; ein Import/Export über Perls Prefs-Format (Bitmaske vs.
  `day(n)`) kann Tage falsch abbilden. Alarm-Prefs in der Web-UI sind
  betroffen, wenn ein Perl-Client sie schreibt.
* **Aufwand:** klein (Konvertierung) bzw. mittel (Prefs-Kompatibilität).
* **Vorschlag:** an der Prefs-Grenze auf `day(0..6)` konvertieren und
  `volume = -1` durch „Feld nicht setzen" ersetzen.

## D15 — `_startChannel`-Frameguard verwirft, was Perl korrupt sendet

* **Ort:** `src/lyrion/networking/protocol.py:3310-3320`.
* **Perl-Soll (verifiziert):** Perl `pack`t ohne Längenprüfung; ein
  `transition`-String > 1 Byte erzeugt dort stillschweigend einen
  5-Byte-Header und einen korrupten Frame.
* **Ist:** `ValueError` — der Port verweigert den Frame.
* **Auswirkung:** Nur bei fehlerhaften Aufrufern; ein Client sieht dann
  keinen/ einen anderen Frame als bei Perl (Perl: korrupter Frame). Kein
  Client-Fall bekannt.
* **Aufwand:** klein.
* **Vorschlag:** belegen, dass kein Perl-Pfad je > 1 Byte liefert (dann
  Guard als Assertion belassen und dokumentieren) — oder Guard entfernen.

## D16 — Albem-Unique-Index wird bei Legacy-DBs nicht nachgezogen

* **Ort:** `src/lyrion/database/sqlite_helper.py:125-140`
  (`migrate_legacy_db`).
* **Perl-Soll (n/a — Port-eigene Migration):** Perl kennt dieses Schema
  nicht; die Abweichung ist der **Zustand** einer migrierten DB.
* **Ist:** Spalte `albums.albumartist_sort` wird additiv ergänzt, der
  Unique-Index absichtlich **nicht** neu gebaut; erst ein voller Rescan
  konvergiert.
* **Auswirkung:** Bis zum Rescan können doppelte Albem-Zeilen (gleicher
  Titel, anderer Interpret/„Sort"-Schlüssel) bestehen → Dubletten in
  Albenlisten, abweichende Zähler gegenüber Perl.
* **Aufwand:** mittel.
* **Vorschlag:** nach dem Rescan-Trigger (D13) automatisch einen
  Index-Rebuild einplanen; bis dahin im Status-Report melden.

## D17 — Player-Display: Custom-Zeichen / IR-Tabellen nicht portiert

* **Ort:** `src/lyrion/player/fonts.py:30,535,638,721` (Custom-Char-Verwaltung
  „nicht portiert"), `src/lyrion/player/buttons.py:1514-1530`
  (UNKLAR: Modus-Stack, per-Klasse-Button-Tabellen),
  `src/lyrion/player/streaming.py:28-40` (`STREAMOUTPUT_*` existiert in Perl
  nicht; echte Gapless-Transition UNKLAR).
* **Perl-Soll (teilweise verifiziert):** `TextVFD.pm:103-294,365-1141`
  (Custom-Zeichen, hier nicht lesbar), `Player.pm:41`, `IR.pm:188,400-410`
  (eine gemeinsame IR-Tabelle je Klasse).
* **Auswirkung:** Auf VFD-Player-Displays (Boom/Transporter) fehlen
  benutzerdefinierte Zeichen; Button-/IR-Zuordnung nutzt nur `[common]`;
  Gapless/Crossfade-Übergänge sind nicht umgesetzt (strm-Frame ohne echte
  Transition).
* **Aufwand:** groß (Firmware-/Display-Semantik, reale Hardware nötig).
* **Vorschlag:** als offene Punkte führen; nichts erfinden. Zuerst mit
  einem echten VFD-Player gegen Perl messen (Byte-Diff der Frame-Folge).

## D18 — Kosmetik: 9080-Erwähnungen in Werkzeugen/Doku

* **Ort:** `tools/_probe.py:2`, `tools/_probe_push2.py:13`,
  `tools/jive-discovery-responder.py:7,22`; `src/lyrion/web/app.py:207-209`
  (jetzt korrekt als Historie formuliert).
* **Perl-Soll:** Es gibt nur **einen** öffentlichen Port (9000); 9080 ist
  seit Commit `54a201363` abgeschafft.
* **Auswirkung:** nur Verwirrung bei der Fehlersuche.
* **Aufwand:** klein.
* **Vorschlag:** Probes auf 9000 umstellen (Default), Kommentare nachziehen.
  `tools/` liegt außerhalb meines Änderungsbereichs.

## D19 — Sprachdateien: Keys ohne `DE`-Zeile

* **Ort:** `src/lyrion/i18n/strings_de.py:1-8`.
* **Bewertung: keine Abweichung** — Keys ohne `DE`-Zeile fehlen absichtlich
  und fallen auf `EN` zurück, genau wie Perl (`Strings.pm:414-416`).
  Hier nur aufgeführt, weil der Docstring wie eine Abweichung klingt.

---

## Nachtrag: Prüfreihenfolge für die Abarbeitung

1. D8 (erledigt), D1, D7, D3 (alle klein, nur Frames) — Frame-Diff gegen
   `192.168.1.90:9000` nach jedem Schritt.
2. D2, D6, D11 (klein, Verhalten) — Tests pinnen, Live-Diff.
3. D5, D9, D10, D12, D14, D15 (klein/mittel) — je Route/Modul.
4. D4, D13, D16, D17 (mittel/groß) — Schema-/Modell-Arbeit, eigene Runde.

Diff-Werkzeug für alle Schritte: identische Requests (nur lesend) an
`127.0.0.1:9000` und `192.168.1.90:9000`, Antwort-JSON bytegenau
vergleichen (`tools/`-Probes liefern die Vorlagen).
