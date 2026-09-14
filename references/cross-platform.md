# Plattformunabhängigkeit (Linux / macOS / Windows) und gvfs-Mounts

Stand: 2026-09-14. Perl-Referenz: Lyrion Music Server `public/9.0`
(read-only unter `/tmp/lms-ref` bzw. upstream `Slim/Utils/OS/*.pm`) — jede Regel
hier trägt ihre Fundstelle `Datei:Zeile`.

Ziel: der Port soll den Perl-LMS ersetzen. Die Musik-Mounts bleiben dieselben
wie bei Perl, **zusätzlich** müssen gvfs-Mounts (GNOME, `/run/user/<uid>/gvfs`)
sauber funktionieren, und der Server muss wie Perl auf Linux, macOS und Windows
startbar sein.

## 1. Soll: Verzeichnisse je Betriebssystem

Perl löst jedes Verzeichnis über `Slim::Utils::OSDetect::dirsFor($kind)`
(`Slim/Utils/OSDetect.pm:47-135` dispatcht auf `OSX`/`Win32`/`Win64`/`Linux`/
`Debian`/`Unix`) und `Slim::Utils/Prefs.pm:85-95` (`$::prefsdir` von der
Kommandozeile, sonst `dirsFor('prefs')`).

| kind | Windows | macOS | Linux (Paket) | Linux/Unix (Quelle) |
|---|---|---|---|---|
| prefs | `%ProgramData%\Lyrion\prefs` `Win32.pm:185-187` + `:485-560` | `~/Library/Application Support/Squeezebox` `OSX.pm:173-175` | `/var/lib/squeezeboxserver/prefs` `Debian.pm:77-79` | `$Bin/prefs` `Unix.pm:75-77,107-109` |
| cache | `%ProgramData%\Lyrion\Cache` `Win32.pm:157-159` | `~/Library/Caches/Squeezebox` `OSX.pm:171-174` | `/var/lib/squeezeboxserver/cache` `Debian.pm:85-87` | `$Bin/Cache` `Unix.pm:64-66` |
| log | `%ProgramData%\Lyrion\Logs` `Win32.pm:153-155` | `~/Library/Logs/Squeezebox` `OSX.pm:169-171` | `/var/log/squeezeboxserver` `Debian.pm:81-83` | `$Bin/Logs` `Unix.pm:60-62` |
| music | `%USERPROFILE%\Music` (`CSIDL_MYMUSIC`) `Win32.pm:189-197` | `~/Music` `OSX.pm:183-199` | *(keiner)* `Debian.pm:89-91` | *(keiner)* `Unix.pm:68-71` |
| playlists | `<music>\Playlists` `Win32.pm:219-221` | `~/Music/Playlists` `OSX.pm:203-205` | *(keiner)* | *(keiner)* |

Weiteres Soll aus den OS-Modulen:

* Windows schaltet **keine** Benutzer/Gruppen um (`Win32.pm:331`
  `dontSetUserAndGroup`); Dienste installiert der Installer, nicht der Server
  (`Win64.pm:55-89` `runService`, `:122-125` Neustart über den Service Manager).
* Log-Rotation macht nur Windows/macOS selbst (`OS.pm:215-242`, 100 MB
  `OS.pm:17`); Unix überlässt sie dem System (`Unix.pm:130` `sub logRotate {}`).
* Dateinamen-Vergleich ist auf Windows/macOS case-insensitiv:
  `OS.pm:388-390` `noCaseFilename = lc(Info::fileName)`, auf Windows zusätzlich
  locale-kodiert (`Win32.pm:318-321`); Reihenfolge über `sortFilename`
  (`OS.pm:368-383`).
* Ignorierte Einträge: `lost+found` (`OS.pm:302-307`) bzw.
  `System Volume Information`, `RECYCLER`, `Recycled`, `$Recycle.Bin`
  (`Win32.pm:364-379`).
* Windows-Laufwerke prüft Perl über `getDrives`/`isDriveReady`
  (`Win32.pm:381-441`); kurze 8.3-Namen und locale-kodierte Namen expandiert
  `getFileName` (`Win32.pm:247-289`), `decodeExternalHelperPath` ist
  `Win32::GetShortPathName` (`Win32.pm:236`).

## 2. Soll: Dateipfad ↔ `file://`-URL

* `fileURLFromPath` (`Slim/Utils/Misc.pm:307-351`) nutzt `URI::file`, leert den
  Host und hängt bei Leerzeichen am Ende ein `/` an (Bug 15511). `:`/`,`/`=`
  werden prozentkodiert — daher
  `file:///run/user/1000/gvfs/smb-share%3Aserver%3D…%2Cshare%3D…`.
  Bereits-URLs werden unverändert zurückgegeben (`Misc.pm:314`).
* `pathFromFileURL` (`Misc.pm:223-301`): Backslashes in URLs erlaubt
  (Bug 3589, `:255`), `..`-Segmente verboten (`:271-274`), Query/Fragment fallen
  weg (`$uri->path`), nicht-`file:`-URLs kommen unverändert zurück (`:231-236`).

## 3. Ist-Zustand des Ports

Zentrale Stelle: `src/lyrion/platform/paths.py` (neu).

* `dirs_for()` bildet die Perl-Tabelle oben ab; `default_music_dir()` gibt auf
  Unix `None` zurück, genau wie Perl `''` (`Unix.pm:68-71`).
* `port_dir()` / `resolve_serverdata_dir()` bestimmen die tatsächlich benutzten
  Verzeichnisse. Reihenfolge: `--serverdata` (Perls `--prefsdir`,
  `Prefs.pm:89-92`) → `$LYRION_SERVERDATA` → **Migration** (existiert ein
  Datenverzeichnis einer älteren Version, wird es weiter benutzt) → OS-Default.
  Der Quelltext dokumentiert das und der Start loggt die Entscheidung:
  `Server data directory: … (from migration|cli|env|os-default)`.
* **Linux bleibt bewusst beim bisherigen Pfad** `~/.lyrion/Lyrion/{Prefs,Cache,Logs}`,
  weil eine Python-Installation nicht in `$Bin` schreiben darf und Perl für
  Unix `$Bin/prefs` benutzt (`Unix.pm:107-109`); das Paket-Äquivalent wäre
  `/var/lib/squeezeboxserver/…` (`Debian.pm:77-87`). Ein Umzug wird nur
  vorgeschlagen, nicht vollzogen (siehe §7).
* Windows/macOS folgen jetzt **Perl exakt** (§1): `%ProgramData%\Lyrion\…` bzw.
  `~/Library/{Application Support,Caches,Logs}/Squeezebox`.
* `file_url_from_path`/`path_from_file_url` (§2) sind die einzigen
  Konvertierer; `media/folders.py` delegiert dorthin. Neu/gefixt: Windows-Pfade
  (`C:\…` → `file:///C:/…`), UNC (`\\server\share` → `file://server/share`),
  lokale Host-Angabe (`file://localhost/…`), macOS `/Volumes/…`,
  `..`-Ablehnung, unveränderte Nicht-`file:`-URLs.
* Pfadlisten (`mediadirs`, `ignoreInAudioScan`) werden mit
  `platform_paths.split_path_list()` getrennt: ein Komma, auf das `key=` folgt,
  gehört zu einer gvfs-Mount-ID und wird **nicht** als Trenner behandelt
  (vorher wurde `smb-share:server=h,share=s` zerrissen).
* Case-Insensitivität: `fs_is_case_insensitive()`, `casefold_key()` (Perls
  `lc()`), `resolve_existing_case()` (ersetzt Windows-`GetLongPathName`/8.3);
  `tracks`-Lookups in `media/folders.py` fallen bei Windows/macOS auf
  `COLLATE NOCASE` zurück (Perl: `noCaseFilename`, `OS.pm:388-390`).
* Keine POSIX-Zwänge mehr im Startpfad: `--daemon` ist auf Nicht-POSIX ein
  geloggter No-op (Perl nutzt dort den Service Manager, `Win64.pm:55-89`),
  `os.devnull` statt `/dev/null`, `signal.SIGHUP` nur wenn vorhanden
  (`getattr`), harte Linux-Defaults (`/mnt/media/Musik`, `/mnt/media2/Musik`)
  aus `ScanConfig`/`ImportConfig` entfernt — sie werden aus der
  Musikordner-Kette aufgelöst (`Prefs.pm:687-712`).

## 4. gvfs

Formen, die vorkommen (alle werden unterstützt):

```
/run/user/1000/gvfs/smb-share:server=192.168.1.90,share=musik/Musik
file:///run/user/1000/gvfs/smb-share%3Aserver%3D192.168.1.90%2Cshare%3Dmusik/Musik
file:///run/user/1000/gvfs/smb-share:server=192.168.1.90,share=musik/Musik   (roh)
```

* Dekodiert wird mit `unquote`, **nicht** `unquote_plus`: `+` ist im Pfad ein
  Literal und wird beim Kodieren zu `%2B` (`URI::file`).
* `gvfs_mount_root()` liefert den Mountpunkt (`…/gvfs/smb-share:server=h,share=s`).
* `gvfs_mount_state()` klassifiziert **ohne Systemeingriff** (kein `gio mount`,
  kein `mount`, kein Auto-Mount): `not-mounted`, wenn der Mountordner fehlt oder
  kein FUSE-Mount ist (`os.path.ismount` + `/proc/self/mounts`), `empty` wenn
  eingebunden und leer, sonst `mounted`.
* `explain_missing_path()` trennt damit die beiden Fälle, die von außen gleich
  aussehen — „Datei fehlt“ gegen „Mount nicht eingebunden“; dazu
  `not-readable` und `empty`. **Kein** Auto-Mount, keine Systemänderung: der
  Server sagt nur, was los ist.
* gvfs ist ein Linux/GNOME-FUSE-Mechanismus; auf Windows/macOS liefert
  `gvfs_runtime_dir()` `None` (dort gibt es solche Pfade nicht).

## 5. Start-Warnpfad

Beim Start prüft `__main__._warn_about_media_dirs()` die konfigurierten Ordner
(`mediadirs` → `audiodir` → `musicdir`) und loggt je Problem eine Warnung:

```
[WARNING] lyrion: Music folder not mounted: gvfs mount is not mounted: /run/user/1000/gvfs/smb-share:server=…,share=… — … is unreachable (the server does not mount shares by itself)
[WARNING] lyrion: Music folder is empty: exists but is empty: /tmp/xplat-empty
[WARNING] lyrion: Music folder does not exist: missing: /tmp/xplat-missing (the parent folder exists and is mounted) — check the share and the 'mediadirs' preference
[WARNING] lyrion: Music folder check: 3 of 3 configured folder(s) are not usable (see the warnings above) — the library will be incomplete until they are back
```

Perl überspringt einen fehlenden Ordner still (`Prefs.pm:700-710`); da mit
Mounts „Metadaten ja, Ton nein“ sonst nicht von „nie gescannt“ zu unterscheiden
ist, meldet der Port es aktiv. Ohne jede Konfiguration kommt
`No music folder is configured …`; bei intaktem Ordner eine INFO-Zeile.

## 6. Starten

Gemeinsam (alle OS): `python -m lyrion [--loglevel info]`. Datenverzeichnis
explizit: `--serverdata DIR` oder `$LYRION_SERVERDATA`.

* **Linux**: `.venv/bin/python3 -m lyrion --loglevel debug` (so startet auch
  `tools/restart_server.sh`, das die Ports 9000/9001/9090/3483 prüft).
* **macOS**: gleiche Kommandozeile; Daten landen in
  `~/Library/Application Support/Squeezebox`, Logs in
  `~/Library/Logs/Squeezebox`, Cache in `~/Library/Caches/Squeezebox`
  (Perl-Layout). Es gibt keine macOS-Sonderpfade und keine Alias-Auflösung
  (Perls `pathFromMacAlias`, `OSX.pm:190-195`, ist Finder-spezifisch).
* **Windows**: `python -m lyrion --loglevel info` in einer PowerShell;
  Daten in `%ProgramData%\Lyrion` (Schreibrechte für den Dienst-Account nötig).
  `--daemon` ist dort ein No-op (Warnung im Log) — als Dienst wird der Server
  wie bei Perl vom Installer eingerichtet, nicht von sich selbst. Kein
  `fork`, kein `setsid`, keine Signalgruppen: gestoppt wird mit Ctrl-C bzw.
  `Stop-Service`.

## 7. Bewusst NICHT umgebaut (Vorschläge)

| Punkt | Warum nicht jetzt | Perl-Beleg |
|---|---|---|
| Umzug des Linux-Datenverzeichnisses nach `/var/lib/lyrion` | verschiebt die Daten eines laufenden Systems (Bibliothek!) | `Debian.pm:77-87` |
| Bibliotheks-DB `Prefs/lyrion.db` → `Cache/library.db` | Pfad der laufenden Installation | `Debian.pm:85-87` + `tools/incr-import.py:13` |
| Dienst-/Starter-Installation (systemd, launchd, Windows-Service) | braucht Root/Administrator, ist eine Systemänderung | `Win64.pm:55-89`, `OS.pm:211-242` |
| Eigene Log-Rotation für Windows/macOS | Verhalten ändern ohne Testgerät; Logs liegen bereits je OS korrekt | `OS.pm:215-242` |
| `utils/osdetect.py` neben `platform/paths.py` | Datei lag außerhalb des erlaubten Umfangs; Werte weichen von Perl ab (`~/Library/Logs/Lyrion` statt `…/Squeezebox`, XDG-Pfade) — Konsolidierung empfohlen | `OSX.pm:169-175` |
| `~/.lyrion`-Defaults in `utils/log.py:146`, `utils/prefs.py:107`, `utils/osdetect.py:118-159`, `alarms.py:35`, `plugins/manager.py:39` | ebenfalls außerhalb des erlaubten Umfangs; der Startpfad benutzt sie nicht mehr (`config.py` → `platform/paths.py`), sie greifen nur, wenn diese Module direkt ohne Config benutzt werden — auf `port_dir()` umstellen | `Prefs.pm:85-95` |
| `/var/lib/squeezeboxserver/prefs/favorites.opml` als Favoriten-Quelle | Datei war gesperrt (`music/favorites.py:29-30`); dient nur als Lesekandidat für Debian-Perl-Installationen | — |
| Windows-Laufwerkserkennung (`getDrives`/`isDriveReady`), 8.3-Namen, Shortcut-Auflösung | braucht Windows-Hardware; `resolve_existing_case()` deckt den Case-Teil ab | `Win32.pm:247-289,381-441,570-643` |

## 8. Verifikation

```
python -m compileall -q src/
ruff check --select F821,F811,E9 src/ tests/test_cross_platform_paths.py
python -m pytest -q tests/ --ignore=tests/test_contract.py \
    -k 'path or url or config or folder or media or scan'
```

`tests/test_cross_platform_paths.py` deckt ab: gvfs-URL-Formen (kodiert und
roh), `+` als Literal, Umlaute/Sonderzeichen, Windows-Laufwerke und UNC, macOS
`/Volumes/…`, `..`-Ablehnung, Verzeichnisregeln je OS, Auflösungsreihenfolge
des Datenverzeichnisses, gvfs-Mount-Zustände, „fehlt“ vs. „nicht eingebunden“,
den Start-Warnpfad, Case-Auflösung inkl. `COLLATE NOCASE`-Fallback sowie einen
**statischen** Test, dass kein ungeschützter POSIX-Aufruf
(`os.fork`/`os.setsid`/`os.getuid`/`signal.SIGHUP`/`pkexec`/`systemctl` …) im
Baum steht und keine harten `/mnt`, `/var`-Defaults mehr existieren.

Ehrliche Grenzen: es gibt hier **kein** Windows- und **kein** macOS-Testgerät.
Die Windows-/macOS-Zweige sind über die expliziten Parameter
`os_name=`/`home=`/`env=` der Plattform-Funktionen getestet, nicht auf echter
Hardware. Nicht geprüft sind daher: echte UNC-Laufwerke und
`%ProgramData%`-Berechtigungen, macOS-Berechtigungen (TCC) für
`~/Library`, Windows-Diensteverhalten und 8.3-Namen. Der gvfs-Mountzustand
wurde auf dieser Maschine nur im Zustand „nicht eingebunden“ real gemessen
(siehe Logzeilen in §5); „mounted/empty“ ist unit-getestet.
