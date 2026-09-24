"""German (`DE`) strings for the keys this port emits.

Source: Perl reference /tmp/lms-ref, `DE` column (`\tDE\t…` lines). Keys that the
tables carry no `DE` line for are intentionally absent: Perl then uses the failsafe
`EN` value (Strings.pm:414-416) and `get_string()` does the same.
"""

from __future__ import annotations

STRINGS_DE: dict[str, str] = {
    'CHOICE_OFF': 'Aus',  # strings.txt:1153
    'LOW': 'Niedrig',  # strings.txt:11846
    'MEDIUM': 'Mittel',  # strings.txt:11790
    'HIGH': 'Hoch',  # strings.txt:11863
    'FIXED_VOLUME_100': 'Lautstärke auf 100 % festgelegt',  # strings.txt:11662
    'ANALOGOUTMODE_HEADPHONE': 'Kopfhörer',  # strings.txt:20213
    'ANALOGOUTMODE_ALWAYS_ON': 'Immer aktiv',  # strings.txt:20230
    'ANALOGOUTMODE_ALWAYS_OFF': 'Immer aus',  # strings.txt:20247
    'TRANSITION_NONE': 'Keine',  # strings.txt:6056
    'TRANSITION_CROSSFADE': 'Überblendung',  # strings.txt:6074
    'TRANSITION_FADE_IN': 'Einblenden',  # strings.txt:6092
    'TRANSITION_FADE_OUT': 'Ausblenden',  # strings.txt:6110
    'TRANSITION_FADE_IN_OUT': 'Ein- und Ausblenden',  # strings.txt:6128
    'REPLAYGAIN_DISABLED': 'Normalisierung deaktivieren',  # strings.txt:5672
    'REPLAYGAIN_TRACK_GAIN': 'Titel-Normalisierung verwenden (falls möglich)',  # strings.txt:5707
    'REPLAYGAIN_ALBUM_GAIN': 'Album-Normalisierung verwenden (falls möglich)',  # strings.txt:5725
    'REPLAYGAIN_SMART_GAIN': "'Intelligente' Normalisierung",  # strings.txt:5743
    'SORT_ARTISTALBUM': 'Interpret, Album',  # strings.txt:19031
    'SORT_ARTISTYEARALBUM': 'Interpret, Jahr, Album',  # strings.txt:19049
    'BRIGHTNESS_DARK': 'Dunkel',  # strings.txt:15573
    'BRIGHTNESS_DIMMEST': 'Am dunkelsten',  # strings.txt:15487
    'BRIGHTNESS_BRIGHTEST': 'Am hellsten',  # strings.txt:15593
    'BRIGHTNESS_AMBIENT': 'Automatisch',  # strings.txt:15504
    'SETUP_POWERONBRIGHTNESS_ABBR': 'Wenn aktiv',  # strings.txt:4468
    'SETUP_POWEROFFBRIGHTNESS_ABBR': 'Wenn aus',  # strings.txt:4486
    'SETUP_IDLEBRIGHTNESS_ABBR': 'Inaktivität',  # strings.txt:4504
    'SETUP_MINAUTOBRIGHTNESS': 'Minimale Helligkeit (automatisch)',  # strings.txt:4522
    'SETUP_SENSAUTOBRIGHTNESS': 'Empfindlichkeit der Helligkeit (automatisch)',  # strings.txt:4576
    'LIGHT': 'Light',  # strings.txt:11899
    'FULL': 'Voll',  # strings.txt:11881
    'LIGHT_N': 'Schwach eng',  # strings.txt:15521
    'STANDARD_N': 'Standard eng',  # strings.txt:15539
    'FULL_N': 'Voll eng',  # strings.txt:15556
    'SMALL': 'Klein',  # strings.txt:11770
    'LARGE': 'Groß',  # strings.txt:11750
    'HUGE': 'Sehr Groß',  # strings.txt:11809
    'OFF': 'Aus',  # strings.txt:11183
    'ON': 'Ein',  # strings.txt:11203
    'ALARM_ALL_ALARMS': 'Alle Wecker',  # strings.txt:1855
    'ALARM_ADD': 'Wecker hinzufügen',  # strings.txt:1675
    'ALARM_VOLUME': 'Weckerlautstärke',  # strings.txt:1621
    'ALARM_FADE': 'Weckerwiedergabe einblenden',  # strings.txt:1891
    'ALARM_ALARM_ENABLED': 'Aktiviert',  # strings.txt:1747
    'ALARM_SET_TIME': 'Zeit einrichten',  # strings.txt:1910
    'ALARM_SET_DAYS': 'Tage wählen',  # strings.txt:1928
    'ALARM_SELECT_PLAYLIST': 'Weckton',  # strings.txt:1585
    'ALARM_DELETE': 'Wecker entfernen',  # strings.txt:1729
    'ALARM_ALARM': 'Wecker',  # strings.txt:1509
    'ALARM_ALARM_REPEAT': 'Wecker wiederholen',  # strings.txt:1801
    'ALARM_ALARM_ONETIME': 'Einmaliger Alarm',  # strings.txt:1819
    'ALARM_OFF': 'Aus',  # strings.txt:1565
    'ALARM_DAY0': 'Sonntag',  # strings.txt:2147
    'ALARM_DAY1': 'Montag',  # strings.txt:2166
    'ALARM_DAY2': 'Dienstag',  # strings.txt:2185
    'ALARM_DAY3': 'Mittwoch',  # strings.txt:2204
    'ALARM_DAY4': 'Donnerstag',  # strings.txt:2223
    'ALARM_DAY5': 'Freitag',  # strings.txt:2242
    'ALARM_DAY6': 'Samstag',  # strings.txt:2261
    'ALARM_SHORT_DAY_0': 'So',  # strings.txt:2280
    'ALARM_SHORT_DAY_1': 'Mo',  # strings.txt:2298
    'ALARM_SHORT_DAY_2': 'Di',  # strings.txt:2316
    'ALARM_SHORT_DAY_3': 'Mi',  # strings.txt:2334
    'ALARM_SHORT_DAY_4': 'Do',  # strings.txt:2352
    'ALARM_SHORT_DAY_5': 'Fr',  # strings.txt:2370
    'ALARM_SHORT_DAY_6': 'Sa',  # strings.txt:2388
    'JIVE_ALARMSET_HELP': 'Wählen Sie die gewünschte Ziffer mit dem Rad und drücken Sie zum Bestätigen die mittlere Taste. Drücken Sie die mittlere Taste, nachdem Sie die Weckzeit eingestellt haben.',  # strings.txt:22609
    'SHUFFLE': 'Zufall',  # strings.txt:11300
    'SHUFFLE_OFF': 'Wiedergabeliste nicht mischen',  # strings.txt:11380
    'SHUFFLE_ON_SONGS': 'Zufällige Titelreihenfolge',  # strings.txt:11320
    'SHUFFLE_ON_ALBUMS': 'Zufällige Albumreihenfolge',  # strings.txt:11340
    'CANCEL': 'Abbrechen',  # strings.txt:22746
    'EMPTY': 'Leer',  # strings.txt:445
    'SLEEP_CANCEL': 'Schlafmodus abbrechen',  # strings.txt:1249
    'SLEEPING_IN_X_MINUTES': 'Schlafmodus in %s Minuten',  # strings.txt:1267
    'X_MINUTES': '%s Minuten',  # strings.txt:1285
    'SLEEP_AT_END_OF_SONG': 'Schlafmodus bei Titelende',  # strings.txt:1322
    'NOTHING_CURRENTLY_PLAYING': 'Keine Wiedergabe',  # strings.txt:1358
    'SYNC_ABOUT': 'Fügen Sie eine oder mehrere Squeezeboxen hinzu, um die Synchronisierungsfunktion zu verwenden und das volle Potenzial eines Audiosystems für mehrere Räume zu nutzen. Weitere Informationen erhalten Sie unter Lyrion.org.',  # strings.txt:23921
    'SYNC_X_TO': 'Synchronisieren %s mit:',  # strings.txt:16814
    'DO_NOT_SYNC': 'Keine Synchronisierung',  # strings.txt:16831
    'SYNCING_WITH': 'Synchronisieren mit: %s',  # strings.txt:16868
    'UNSYNCING_FROM': 'Synchronisierung aufheben von: %s',  # strings.txt:16885
    'RECENT_SEARCHES': 'Letzte Suchvorgänge',  # strings.txt:22967
    'PRESET_ADDING': 'Voreinstellung %s wird gespeichert...',  # strings.txt:23803
    'PRESET': 'Voreinstellung #%s',  # strings.txt:23841
    'PRESETS_NOT_DEFINED': 'Voreinstellung %s nicht definiert.',  # strings.txt:23784
    'JIVE_SET_PRESET_X': 'Voreinstellung %s festlegen',  # strings.txt:22462
    'JIVE_OVERWRITE_PRESET_X': '%s ersetzen?',  # strings.txt:22445
    'ADD': 'Hinzufügen',  # strings.txt:11263
    'DELETE': 'Löschen',  # strings.txt:16045
    'FAVORITES': 'Favoriten',  # Slim/Plugin/Favorites/strings.txt:3
    # Der jive-Popup-Text nach `favorites add` (Favorites/Plugin.pm:904-911).
    'FAVORITES_ADDING': 'Favoriten werden gespeichert ...',  # Favorites/strings.txt:38/:41
    'HOME': 'Hauptmenü',  # strings.txt:3140
    # Land des "Lokales Radio"-Feeds (web/settings.py, Pref
    # `radiobrowser_country`) — Perls einzige "country"-Beschriftung.
    'PLUGIN_PODCAST_COUNTRY': 'Land',  # Slim/Plugin/Podcast/strings.txt:270 (DE :273)
    # Option "alle Länder" derselben Auswahlliste.
    'ALL': 'Alle',  # strings.txt:11281 (DE :11284)
    # Settings.pm:168 — Warnung für einen abgelehnten Pref-Wert.
    'SETTINGS_INVALIDVALUE': '"%s" ist kein gültiger Wert für %s',  # strings.txt:10145 (DE :10148)
    # ── Player-Firmware (Slim/Player/Squeezebox.pm) ─────────────────────────
    # Der `showBriefly`-Block, wenn das Image der Zielrevision fehlt.
    'FIRMWARE_MISSING': 'Fehler: Firmware fehlt',  # strings.txt:2765 (DE :2768)
    'FIRMWARE_MISSING_DESC': 'Der Server kann zum Laden eines Firmware-Update keine Internetverbindung herstellen.',  # strings.txt:2784 (DE :2787)
    'UPDATING_FIRMWARE_SQUEEZEBOX': 'Squeezebox-Firmware wird aktualisiert ...',  # strings.txt:2669 (DE :2672)
    'UPDATING_FIRMWARE_SQUEEZEBOX2': 'Squeezebox-Firmware wird aktualisiert ...',  # strings.txt:2689 (DE :2692)
    'UPDATING_FIRMWARE_TRANSPORTER': 'Transporter-Firmware wird aktualisiert...',  # strings.txt:2709 (DE :2712)
    'UPDATING_FIRMWARE_RECEIVER': 'Squeezebox-Firmware wird aktualisiert...',  # strings.txt:2729 (DE :2732)
    'UPDATING_FIRMWARE_BOOM': 'Firmware wird aktualisiert.',  # strings.txt:2747 (DE :2750)
    # Boom-Zweistufen-Zähler (`Squeezebox.pm:425-434`).
    'OUT_OF': 'von',  # strings.txt:777 (DE :780)
    # ── Plugin-Verwaltung (Slim/Web/Settings/Server/Plugins.pm) ─────────────
    'SETUP_PLUGINS': 'Plugins verwalten',  # strings.txt:7183 (DE :7186)
    'PLUGINS_RESTART_MSG': 'Plugins wurden aktualisiert - Neustart erforderlich',  # strings.txt:23730 (DE :23733)
    'SETUP_EXTENSIONS_RESTART_MSG': 'Lyrion Music Server muss neu gestartet werden, damit die Änderungen in Kraft treten.',  # strings.txt:7409 (DE :7412)
    'ENABLED': 'Aktiviert',  # strings.txt:11400 (DE :11403)
    'DISABLED': 'Deaktiviert',  # strings.txt:11420 (DE :11423)
    'PLUGINS_CHANGED_NEED_RESTART': 'Beim nächsten Neustart der Anwendung werden die Änderungen wirksam. <a href="%s">Klicken Sie hier, um den Server jetzt neu zu starten.</a>',  # strings.txt:22253 (DE :22256)
    'PLUGIN_DSTM': 'Musikwiedergabe nie anhalten',  # DontStopTheMusic/strings.txt:1 (DE :4)
    'PLUGIN_DSTM_DESC': "Don't Stop The Music bewahrt Sie davor, von der Stille überrascht zu werden. Wenn sich die Wiedergabeliste dem Ende neigt, wird automatisch passende Musik angehängt.",  # DontStopTheMusic/strings.txt:12 (DE :15)
    'PLUGIN_NAME': 'Plugin',  # strings.txt:11293 (DE :11296)
    # ── Server-Einstellungen: Software-Updates / Netzwerk / Sicherheit / ────
    # ── Dateitypen / Logging / Leistung (web/settings.py) ───────────────────
    'MUSIC': 'Musik',  # strings.txt:392
    'EVERYTHING': 'Alles',  # strings.txt:722
    'SETUP_GROUP_FORMATS_CONVERSION': 'Dateikonvertierungseinstellungen',  # strings.txt:4023
    'SETUP_GROUP_FORMATS_CONVERSION_DESC': 'Lyrion Music Server kann während der Wiedergabe Audiodateiformate konvertieren. Sie können bestimmte Formate ausschließen, indem Sie das entsprechende Kontrollkästchen deaktivieren. Hier wird nur der Name der ausführbaren Datei angezeigt. Wenn Sie die gesamte Befehlszeile sehen möchten, müssen Sie \'convert.conf\' öffnen. Klicken Sie auf \'Ändern\', um die Änderungen zu speichern.',  # strings.txt:4043
    'SETUP_FORMATS_PREFER_NATIVE': 'Native Wiedergabe auf dem Player wenn immer möglich bevorzugen',  # strings.txt:4063
    'SETUP_FORMATSLIST_MISSING_BINARY': 'Ausführbare Datei wurde nicht gefunden:',  # strings.txt:4075
    'FILE_FORMAT': 'Dateiformat',  # strings.txt:4095
    'SETUP_INPUTTYPE': 'Von',  # strings.txt:4115
    'STREAM_FORMAT': 'Datenstromformat',  # strings.txt:4127
    'SETUP_DISABLEDEXTENSIONSAUDIO': 'Deaktivierte Audio-Dateinamenerweiterungen',  # strings.txt:4147
    'SETUP_DISABLEDEXTENSIONSAUDIO_DESC': 'Lyrion Music Server durchsucht die Medienordner nach allen unterstützten Dateitypen (Audiodateien und Cue Sheets). Zum Deaktivieren bestimmter Dateitypen geben Sie unten eine durch Kommas getrennte Liste ein. Beispiel: cue, mp4, aac',  # strings.txt:4166
    'SETUP_DISABLEDEXTENSIONSPLAYLIST': 'Deaktivierte Wiedergabelisten-Dateinamenerweiterungen',  # strings.txt:4185
    'SETUP_DISABLEDEXTENSIONSPLAYLIST_DESC': 'Lyrion Music Server durchsucht den Wiedergabelistenordner nach allen unterstützten Dateitypen (Wiedergabelistendateien und Cue Sheets). Zum Deaktivieren bestimmter Dateitypen geben Sie unten eine durch Kommas getrennte Liste ein. Beispiel: cue, m3u, pls',  # strings.txt:4204
    'DECODER': 'Dekoder',  # strings.txt:4263
    'SETUP_BUFFERSECS': 'Puffergröße für Internetradio',  # strings.txt:5025
    'SETUP_BUFFERSECS_DESC': 'Bei der Wiedergabe von Internetradiosendern puffert der Player eine geringe Datenmenge, bevor er die Wiedergabe startet. Geben Sie an, wie viel Audiodaten gepuffert werden soll (in Sekunden von 3 bis 30). Der Standardwert ist 3 Sekunden. Sollten Unterbrechungen bei der Wiedergabe auftreten, kann ein Erhöhung dieses Werts helfen.',  # strings.txt:5044
    'SETUP_MAXWMARATE': 'Maximale WMA-Datenstrom-Bitrate',  # strings.txt:5063
    'SETUP_MAXWMARATE_DESC': 'Bei bestimmten WMA-Datenströmen stehen mehrere Bitraten zur Übertragung zur Verfügung. Standardmäßig wählt Lyrion Music Server die höchste verfügbare Bitrate. Dies kann jedoch bei langsameren Internetverbindungen zu Problemen führen. Daher können Sie hier die maximal zulässige Bitrate festlegen.',  # strings.txt:5082
    'SETUP_SYNCSTARTDELAY': 'Startverzögerung für synchronisierte Player (ms)',  # strings.txt:5101
    'SETUP_SYNCSTARTDELAY_DESC': 'Wenn mehrere Player synchronisiert sind, gibt Lyrion Music Server jedem Player die Anweisung, den Titel zur gleichen Zeit zu starten. Es muss sichergestellt sein, dass jeder Player genügend Zeit hat, den Startbefehl zu empfangen, bevor die gleichzeitige Wiedergabe beginnt. Daher wird der Startbefehl vorzeitig gesendet und die Player können den richtigen Zeitpunkt abwarten. Sollte das Netzwerk überlastet sein, was vor allem auftreten kann, wenn sich viele Player im Netzwerk befinden und drahtlose Netzwerke genutzt werden, reicht die Standardverzögerung von 200 ms evtl. nicht aus. Sie können bei Bedarf den Standardwert hier ändern.',  # strings.txt:5120
    'FOLDER': 'Ordner',  # strings.txt:6259
    'SETUP_LANGUAGE': 'Sprache',  # strings.txt:6563
    'SETUP_LIBRARY_NAME': 'Name der Medienbibliothek',  # strings.txt:6600
    'SETUP_MEDIADIRS': 'Medienordner',  # strings.txt:6654
    'SETUP_MEDIADIRS_DESC': 'Sie können einen oder mehrere Ordner mit Mediendateien angeben, die Lyrion Music Server durchsuchen und Ihrer Medienbibliothek hinzufügen wird. Geben Sie unten die Ordnerpfade ein.',  # strings.txt:6672
    'SETUP_PLAYLISTDIR': 'Wiedergabelisten-Ordner',  # strings.txt:6712
    'SETUP_PLAYLISTDIR_DESC': 'Geben Sie hier den Ordner ein, in dem Wiedergabelistendateien gespeichert werden. Wenn Sie Wiedergabelisten nicht speichern möchten, können Sie dieses Feld leer lassen.',  # strings.txt:6834
    'SETUP_RESCAN': 'Medienbibliothek erneut durchsuchen',  # strings.txt:6854
    'SETUP_RESCAN_BUTTON': 'Durchsuchen starten',  # strings.txt:6873
    'SETUP_RESCAN_DESC': 'Klicken Sie auf \'Durchsuchen starten\', damit Lyrion Music Server die Medienbibliothek erneut nach neuen oder veränderten Musikstücken durchsucht.',  # strings.txt:6893
    'SETUP_WIPEDB': 'Datenbank löschen und alles neu durchsuchen',  # strings.txt:6911
    'SETUP_STANDARDRESCAN': 'Nach neuen und veränderten Mediendateien suchen',  # strings.txt:6929
    'SETUP_PLAYLISTRESCAN': 'Nur Wiedergabelisten durchsuchen',  # strings.txt:6947
    'SETUP_AUTO_RESCAN': 'Änderungen automatisch erkennen',  # strings.txt:6973
    'SETUP_AUTO_RESCAN_DESC': 'Lyrion Music Server kann die Medienordner durchsuchen und Änderungen automatisch erkennen. Nach einigen Minuten sollten die neuen und geänderten Dateien in der Bibliothek angezeigt werden.',  # strings.txt:6990
    'SETUP_AUTO_RESCAN_ENABLE': 'Aktiviert, automatisch auf Änderungen durchsuchen',  # strings.txt:7007
    'SETUP_AUTO_RESCAN_DISABLE': 'Deaktiviert, manuell nach neuen Mediendateien suchen',  # strings.txt:7024
    'SETUP_AUTO_RESCAN_STAT_INTERVAL': 'Netzwerkfreigabe – Erkennungsintervall',  # strings.txt:7041
    'SETUP_AUTO_RESCAN_STAT_INTERVAL_DESC': 'Um Änderungen auf Remote-Netzwerkfreigaben oder Betriebssystemen ohne Unterstützung von systemeigener Änderungserkennung zu erkennen, müssen die Dateienin regelmäßigen Abständen auf Änderungen abgefragt werden. Wählen Sie das für diese Überprüfung zu verwendende Intervall in Minuten. Der Standardwert ist 10 Minuten.',  # strings.txt:7058
    'SETUP_HTTPPORT': 'Anschlussnummer des Webservers',  # strings.txt:7789
    'SETUP_HTTPPORT_DESC': 'Sie können die Anschlussnummer ändern, die vom Browser für den Server-Zugriff verwendet wird (Standard ist 9000).',  # strings.txt:7809
    'SETUP_HTTPPORT_OK': 'Verwendeter Anschluss:',  # strings.txt:7829
    'SETUP_CHECKVERSION': 'Softwareaktualisierungen',  # strings.txt:7979
    'SETUP_CHECKVERSION_DESC': 'Lyrion Music Server kann automatisch prüfen, ob eine neue Softwareversion verfügbar ist. Ist dies der Fall, wird dies in der Web-Benutzeroberfläche von Lyrion Music Server gemeldet. Beachten Sie bitte, dass Plugins immer gemäss dem gewählten Intervall auf Aktualisierungen überprüft werden.',  # strings.txt:7999
    'SETUP_CHECKVERSION_1': 'Automatisch nach Software-Updates suchen',  # strings.txt:8011
    'SETUP_CHECKVERSION_0': 'Nicht automatisch nach aktualisierter Serversoftware suchen',  # strings.txt:8031
    'SETUP_CHECKVERSION_HOURLY': 'Stündlich',  # strings.txt:8051
    'SETUP_CHECKVERSION_DAILY': 'Täglich',  # strings.txt:8062
    'SETUP_CHECKVERSION_WEEKLY': 'Wöchentlich',  # strings.txt:8073
    'SETUP_CHECKVERSION_MONTHLY': 'Monatlich',  # strings.txt:8084
    'SETUP_AUTO_DOWNLOAD': 'Automatischer Download',  # strings.txt:8095
    'SETUP_AUTO_DOWNLOAD_0': 'Updates nicht automatisch herunterladen',  # strings.txt:8129
    'SETUP_AUTO_DOWNLOAD_1': 'Verfügbare Updates automatisch herunterladen.',  # strings.txt:8146
    'SETUP_CHECK_NOW': 'Jetzt nach Update suchen',  # strings.txt:8163
    'SETUP_SCAN_ON_PREF_CHANGE': 'Bei Einstellungesänderungen Sammlung durchsuchen',  # strings.txt:9159
    'SETUP_SCAN_ON_PREF_CHANGE_DESC': 'Einige Einstellungen erfordern ein erneutes Einlesen der Mediensammlung. Dieser Scan kann automatisch ausgelöst werden. Allerdings kann dies zur Folge haben, dass mehrere Durchsuchen-Läufe hintereinander gestartet werden, falls mehrere Einstellungen geändert werden.',  # strings.txt:9171
    'SETUP_SCAN_ON_PREF_CHANGE_ON': 'Durchsuchen automatisch starten',  # strings.txt:9184
    'SETUP_SCAN_ON_PREF_CHANGE_OFF': 'Zum Durchsuchen auffordern, aber nicht automatisch starten',  # strings.txt:9197
    'SETUP_DBHIGHMEM': 'Konfiguration des Datenbankspeichers',  # strings.txt:9223
    'SETUP_DBHIGHMEM_DESC': 'Wenn auf Ihrem Server genug Speicherplatz frei ist, können Sie die Leistung steigern, indem Sie den Speicherplatz für die Datenbank erhöhen. Um diese Option zu ändern, muss der Server neu gestartet werden.',  # strings.txt:9240
    'SETUP_DBHIGHMEM_NORMAL': 'Normal',  # strings.txt:9257
    'SETUP_DBHIGHMEM_HIGH': 'Hoch (empfohlen für Computer mit 1 GB RAM und mehr)',  # strings.txt:9274
    'SETUP_DBHIGHMEM_MAX': 'Maximum (empfohlen für Sammlungen mit mehr als 50\'000 Titeln und Computer mit 2 GB RAM und mehr)',  # strings.txt:9291
    'SETUP_DISABLESTATISTICS': 'Musiksammlungsstatistik',  # strings.txt:9304
    'SETUP_DISABLESTATISTICS_DESC': 'Beim Anzeigen der Lyrion Music Server-Startseite wird die Medienbibliothek durchsucht, um die Statistik anzeigen zu können. Sie können diese Funktion deaktivieren, wodurch die Zeit zum Öffnen der Web-Benutzeroberfläche verkürzt wird, besonders wenn die Bibliothek sehr groß ist.',  # strings.txt:9322
    'SETUP_DISABLE_STATISTICS': 'Statistik nicht anzeigen',  # strings.txt:9340
    'SETUP_ENABLE_STATISTICS': 'Statistik aktivieren',  # strings.txt:9358
    'SETUP_ENHANCEDHTTP': 'Streamingmodus für HTTP(S)',  # strings.txt:9376
    'SETUP_ENHANCEDHTTP_DESC': 'Wenn eine HTTP(S)-Verbindung zu einem Server fehlschlägt, beendet Lyrion Music Server die Wiedergabe (Normaler Modus). Er kann auch versuchen, die Verbindung wieder zu öffnen (Persistenter Modus), oder er kann Streams von bestimmter Länge (Podcasts, einzelne Tracks) auf der Festplatte zwischenspeichern (Cache Modus). Diese Dateien werden entfernt, sobald der Stream abgespielt wurde. Dies erhöht die Zuverlässigkeit mit einigen Anbietern, kann aber das Dateisystem zusätzlich belasten (z.B. wenn eine SD Karte verwendet wird).',  # strings.txt:9387
    'SETUP_ENABLE_PERSISTENTHTTP': 'Persistenter Modus',  # strings.txt:9398
    'SETUP_ENABLE_BUFFEREDHTTP': 'HTTP(S) Datenströme zwischenspeichern (Cache Modus)',  # strings.txt:9409
    'SETUP_DISABLE_ENHANCEDHTTP': 'Normaler Modus',  # strings.txt:9420
    'SETUP_MAX_REDIRECTS': 'Maximale Anzahl Weiterleitungen',  # strings.txt:9722
    'SETUP_MAX_REDIRECTS_DESC': 'Die maximale Anzahl von HTTP Weiterleitungen, die gefolgt werden soll. Werden weitere Weiterleitungen angefordert, so gilt die Anfrage als fehlgeschlagen.',  # strings.txt:9732
    'SETUP_WEBPROXY': 'Web-Proxy',  # strings.txt:9743
    'SETUP_WEBPROXY_DESC': 'Sie können die IP-Adresse und den Anschluss eines HTTP/Web-Proxys definieren, den Lyrion Music Server verwenden soll, um auf Server außerhalb des lokalen Netzwerks zugreifen zu können. Verwenden Sie das Format xx.xx.xx.xx:yyyy, wobei xx.xx.xx.xx die IP-Adresse des Proxys und yyyy die Anschlussnummer ist. Lassen Sie dieses Feld leer, wenn kein HTTP-Web-Proxy genutzt wird.',  # strings.txt:9762
    'SETUP_CORS_ALLOWED_HOSTS': 'CORS akzeptierte Hosts',  # strings.txt:9800
    'SETUP_CORS_ALLOWED_HOSTS_DESC': 'Cross-Origin Resource Sharing (CORS) ist ein Mechanismus, der Webbrowsern oder auch anderen Webclients Cross-Origin-Requests ermöglicht.<br><br>Definieren Sie eine Komma separierte Liste von Hostnamen, die auf ihren Lyrion Music Server zugreifen dürfen. Es müssen Protokoll (z.B. "http"), voller Hostname ("www.example.com"), und allenfalls Port angegeben werden, wenn letzterer nicht dem Standard entspricht. In den meisten Fällen reicht ein Eintrag wie "https://www.example.com".',  # strings.txt:9811
    'SETUP_CSRFPROTECTIONLEVEL': 'CSRF-Schutzstufe',  # strings.txt:9822
    'SETUP_CSRFPROTECTIONLEVEL_DESC': 'Zum Schutz gegen \'Cross Site Request Forgery\' (CSRF)-Sicherheitsrisiken prüft Lyrion Music Server HTTP-Anforderungen auf Funktionen, die das System ändern oder Wiedergabelisten oder Player beeinträchtigen können. Sie können die Schutzstufe für Lyrion Music Server festlegen. Standardmäßig ist kein Wert festgelegt. Weitere Informationen finden Sie in der <a href="/html/docs/http.html#csrf">Hilfe</a>.',  # strings.txt:9840
    'SETUP_INSECURE_HTTPS': 'Unsicheres HTTPS',  # strings.txt:9858
    'SETUP_INSECURE_HTTPS2': 'Server Zertifikate bei HTTPS Verbindungen nicht überprüfen.',  # strings.txt:9869
    'SETUP_IPFILTER_HEAD': 'Eingehende Verbindungen blockieren',  # strings.txt:9891
    'SETUP_IPFILTER_DESC': 'Mit dieser Option können Sie eingehende CLI- und HTTP-Anforderungen durch Angeben der Quell-IP-Adresse blockieren.',  # strings.txt:9911
    'SETUP_IPFILTER': 'Blockieren',  # strings.txt:9931
    'SETUP_NO_IPFILTER': 'Nicht blockieren',  # strings.txt:9951
    'SETUP_FILTERRULE_HEAD': 'Akzeptierte IP-Adressen',  # strings.txt:9971
    'SETUP_FILTERRULE_DESC': 'Ist die Blockierung aktiviert, können Sie IP-Adressen angeben, die eine Verbindung zu Lyrion Music Server herstellen dürfen.<br><br>Dieses Feld akzeptiert eine Liste spezifischer IP-Adressen. Verwenden Sie * als Jokerzeichen und trennen Sie unterschiedliche IP-Adressen durch Komma.<BR><BR><B>Beispiel: 10.1.2.2</B> akzeptiert nur 10.1.2.2<BR><BR><B>10.1.2.*</B> akzeptiert alle IP-Adressen, die mit \'10.1.2.\' beginnen.<BR><BR><B>10.1.2.2-50</B> akzeptiert die IP-Adressen 10.1.2.2 – 10.1.2.50.<BR><BR>Sie können mehrere Einträge durch Kommas kombinieren. Beispiel:<B>10.1.2.2,172.16.1.*,192.168.1-255.*</B><BR><BR>Die spezielle IP-Adresse 127.0.0.1 gestattet einem Web-Browser die Verbindung mit Lyrion Music Server auf demselben Computer.',  # strings.txt:9991
    'SETUP_NO_AUTHORIZE': 'Kein Kennwortschutz',  # strings.txt:10047
    'SETUP_AUTHORIZE': 'Kennwortschutz',  # strings.txt:10067
    'SETUP_AUTHORIZE_DESC': 'Sie können Lyrion Music Server Remote Control mit einem Kennwort schützen. Klicken Sie unten auf \'Kennwortschutz\', geben Sie einen Benutzernamen und ein Kennwort ein, und klicken Sie auf \'Ändern\'. Wenn Sie dann Lyrion Music Server Remote Control aufrufen, müssen Sie diesen Benutzernamen und das Kennwort eingeben.',  # strings.txt:10087
    'SETUP_USERNAME': 'Benutzername',  # strings.txt:10107
    'SETUP_MISSING_USERNAME': 'Sie können die Autorisierung nicht ohne ein Kennwort aktivieren.',  # strings.txt:10127
    'SETUP_PASSWORD': 'Kennwort',  # strings.txt:10144
    'SETUP_PASSWORD_REPEAT': 'Kennwort bestätigen',  # strings.txt:10164
    'SETUP_PASSWORD_MISMATCH': 'Die eingegebenen Kennwörter stimmen nicht überein.',  # strings.txt:10181
    'SETUP_REMOTESTREAMTIMEOUT': 'Radiosender-Zeitlimit',  # strings.txt:10198
    'SETUP_REMOTESTREAMTIMEOUT_DESC': 'Sie können festlegen, wie lange Lyrion Music Server versucht, eine Verbindung zu einem Internetradiosender oder einer anderen Quelle herzustellen. Geben Sie die Wartezeit in Sekunden ein.',  # strings.txt:10217
    'NATIVE': 'Nativ',  # strings.txt:11443
    'NONE': 'Keine',  # strings.txt:11831
    'KBPS': 'kb/s',  # strings.txt:13486
    'NO_LIMIT': 'Keine Beschränkung',  # strings.txt:13497
    'FORMATS_SETTINGS': 'Dateiarten',  # strings.txt:15157
    'SECURITY_SETTINGS': 'Sicherheit',  # strings.txt:15177
    'PERFORMANCE_SETTINGS': 'Leistung',  # strings.txt:15197
    'NETWORK_SETTINGS': 'Netzwerk',  # strings.txt:15237
    'DEBUG_RADIO': 'Internetradio',  # strings.txt:15321
    'DEBUG_TRANSCODING': 'Transkodierer',  # strings.txt:15338
    'DEBUG_SERVER_CHOOSE': 'Server',  # strings.txt:?
    'DEBUG_SCANNER_CHOOSE': 'Medienscanner',  # strings.txt:15367
    'DEBUG_SELECT_SET': 'Protokollsatz wählen...',  # strings.txt:15384
    'DEBUG_DEFAULT': 'Protokolleinstellungen zurücksetzen',  # strings.txt:15401
    'SETUP_GROUP_DEBUG_DESC': 'Lyrion Music Server enthält eine Reihe von Protokolleinstellungen, mit denen Sie umfangreiche Angaben über Lyrion Music Server- und Scanner-Vorgänge aufzeichnen können. Jede Protokollkategorie unterliegt einer Begrenzung in der Protokollierung ("Debug" mit der höchsten Wortanzahl, "Off" mit der niedrigsten).',  # strings.txt:15418
    'DEBUGGING_SETTINGS': 'Protokoll',  # strings.txt:15436
    'DEBUGGING_ADVANCED': 'Erweiterte Protokolleinstellungen',  # strings.txt:15453
    'CHECKVERSION_PROBLEM': 'Beim Suchen nach einem Lyrion Music Server-Update ist ein Problem aufgetreten: (Fehlercode %s)',  # strings.txt:16243
    'SETUP_PRECACHEARTWORK': 'Plattenhüllen zwischenspeichern',  # strings.txt:19443
    'SETUP_PRECACHEARTWORK_DESC': 'Standardmäßig werden alle während der Suche gefundenen Plattenhüllen automatisch in der Größe angepasst und zwischengespeichert, um die Leistung zu verbessern, wenn der Squeezebox Controller oder die Web-Benutzeroberfläche genutzt wird.  Da dies die Suche verlangsamt, können Sie den Vorgang hier deaktivieren.',  # strings.txt:19460
    'SETUP_PRECACHEARTWORK_ENABLED': 'Plattenhüllen zwischenspeichern',  # strings.txt:19477
    'SETUP_PRECACHEARTWORK_DISABLED': 'Plattenhüllen nicht zwischenspeichern',  # strings.txt:19494
    'SETUP_PRECACHEARTWORK_CUSTOM_SPECS': 'Spezifikationen für Zwischenspeicherung',  # strings.txt:19511
    'SETUP_IMAGEPROXY': 'Plattenhüllen Grössenanpassung',  # strings.txt:19524
    'SETUP_IMAGEPROXY_DESC': 'Bevor Plattenhüllen zu den Endgeräten geschickt werden, werden sie in der Grösse angepasst, um eine gute Qualität und Performance zu erreichen. Dies erfolgt standardmässig in Lyrion Music Server selber. Die Helferanwendung kann den Server entlasten, indem die Aufgabe an einen anderen Prozess übergeben wird.',  # strings.txt:19537
    'SETUP_IMAGEPROXY_LOCAL': 'Bilder lokal in Lyrion Music Server anpassen',  # strings.txt:19548
    'SETUP_IMAGEPROXY_HELPER': 'Bilder mit Lyrion Music Server Helferanwendung anpassen',  # strings.txt:19561
    'SETUP_SERVERPRIORITY': 'Serverpriorität',  # strings.txt:19571
    'SETUP_SHUFFLE_METHOD': 'Mischmethode',  # strings.txt:19589
    'SETUP_USE_BALANCED_SHUFFLE': 'Ausbalancierter, aber langsamer mischen',  # strings.txt:19600
    'SETUP_USE_FASTER_SHUFFLE': 'Schneller, aber weniger ausbalanciert mischen',  # strings.txt:19611
    'SETUP_SERVERPRIORITY_DESC': 'Sie können die Priorität festlegen, mit der Lyrion Music Server ausgeführt wird.',  # strings.txt:19622
    'SETUP_SCANNERPRIORITY': 'Durchsuchpriorität',  # strings.txt:19640
    'SETUP_SCANNERPRIORITY_DESC': 'Sie können die Priorität festlegen, mit welcher das Durchsuchen durchgeführt wird.',  # strings.txt:19658
    'SETUP_PRIORITY_CURRENT': 'Aktuelle Serverpriorität',  # strings.txt:19676
    'SETUP_PRIORITY_HIGH': 'Hoch',  # strings.txt:19694
    'SETUP_PRIORITY_ABOVE_NORMAL': 'Höher als normal',  # strings.txt:19712
    'SETUP_PRIORITY_NORMAL': 'Normal',  # strings.txt:?
    'SETUP_PRIORITY_BELOW_NORMAL': 'Niedriger als normal',  # strings.txt:19742
    'SETUP_PRIORITY_LOW': 'Niedrig',  # strings.txt:19760
    'SETUP_DEBUG_SERVER_LOG': 'Lyrion Music Server-Logdatei',  # strings.txt:21312
    'SETUP_DEBUG_SERVER_LOG_DESC': 'Lyrion Music Server protokolliert alle für die Anwendung relevanten Aktivitäten (Audio-Streaming, Infrarot usw.) in folgender Datei:',  # strings.txt:21330
    'SETUP_DEBUG_SCANNER_LOG': 'Scanner Logdatei',  # strings.txt:21348
    'SETUP_DEBUG_SCANNER_LOG_DESC': 'Lyrion Music Server protokolliert die Aktivitäten, die Suchvorgänge betreffen (inklusive iTunes, MusicIP), in dieser Datei:',  # strings.txt:21366
    'LINES': 'Zeilen',  # strings.txt:21383
    'SETUP_LOG_ZIPPED': 'ZIP Datei',  # strings.txt:21434
    'SETUP_DEBUG_LEVEL_OFF': 'Aus',  # strings.txt:21448
    'SETUP_DEBUG_LEVEL_FATAL': 'Fatale Fehler',  # strings.txt:21468
    'SETUP_DEBUG_LEVEL_ERROR': 'Fehler',  # strings.txt:21485
    'SETUP_DEBUG_LEVEL_WARN': 'Warnungen',  # strings.txt:21502
    'SETUP_DEBUG_LEVEL_INFO': 'Information',  # strings.txt:21519
    'SETUP_DEBUG_LEVEL_DEBUG': 'Debuggen',  # strings.txt:21535
    'SAVE_SETTINGS': 'Einstellungen speichern',  # strings.txt:21552
    'SETUP_VIEW_SCANNING': 'Durchsuchen gestartet - Fortschritt anzeigen',  # strings.txt:22324
    'SETUP_VIEW_NOT_SCANNING': 'Zeige Details zur vorherigen Suche',  # strings.txt:22341
    'PERSIST_DEBUG_SETTINGS': 'Log-Einstellungen beim nächsten Neustart beibehalten',  # strings.txt:22377
    'SETUP_MAXPLAYLISTLENGTH': 'Maximale Länge der Wiedergabeliste',  # strings.txt:23169
    'SETUP_MAXPLAYLISTLENGTH_DESC': 'Um eine übermäßige Speicherverwendung auf dem Server zu verhindern, kann die Länge der Wiedergabeliste, einschließlich der aktuell abgespielten Liste, eingeschränkt werden. Bei Eingabe des Werts \'0\' wird keine Einschränkung vorgenommen. Wird eine Einschränkung festgelegt, muss mindestens der Wert \'10\' eingegeben werden.',  # strings.txt:23186
    'CONTROLPANEL_NO_UPDATE_AVAILABLE': 'Es ist keine aktualisierte Version von Lyrion Music Server verfügbar.',  # strings.txt:23288
    'SERVER_UPDATE_AVAILABLE': 'Eine neue Version von Lyrion Music Server ist verfügbar (%s). <a href="%s" target="update">Klicken Sie hier, um sie herunterzuladen</a>.',  # strings.txt:24043
    'SERVER_UPDATE_AVAILABLE_SHORT': 'Eine neue Version von Lyrion Music Server ist verfügbar.',  # strings.txt:24061
    # ── Player-Seiten (web/settings_player.py; Perl Player/{Menu,Remote, ────
    # ── Synchronization}.pm) ────────────────────────────────────────────────
    'MENU_SETTINGS': 'Menüs',  # strings.txt:14914
    'REMOTE_SETTINGS': 'Fernbedienung',  # strings.txt:14988
    'SETUP_GROUP_MENUITEMS': 'Hauptmenü',  # strings.txt:3614
    'SETUP_GROUP_MENUITEMS_DESC': "Sie können die im Hauptmenü des Players verfügbaren Einträge bestimmen. Klicken Sie auf die entsprechenden Schaltflächen, um Einträge nach oben bzw. unten zu verschieben oder sie zu entfernen. Klicken Sie auf 'Hinzufügen', um einen entfernten Eintrag wieder aufzunehmen.",  # strings.txt:3633
    'SETUP_GROUP_NONMENUITEMS_INTRO': 'Inaktive Menüeinträge:',  # strings.txt:3654
    'MOVEUP': 'Nach oben',  # strings.txt:11226
    'MOVEDOWN': 'Nach unten',  # strings.txt:11246
    'ADD': 'Hinzufügen',  # strings.txt:11266
    'DELETE': 'Löschen',  # strings.txt:16048
    'SETUP_GROUP_IRSETS': 'Fernbedienungen',  # strings.txt:3911
    'SETUP_GROUP_IRSETS_DESC': 'Sie können bestimmen, ob der Player bestimmte Infrarotsignale ignorieren soll oder nicht. Aktivieren bzw. deaktivieren Sie das Kontrollkästchen neben dem Namen, um eine Fernbedienung zu aktivieren bzw. zu deaktivieren.',  # strings.txt:3931
    'SETUP_IRMAP': 'Funktionen der Fernbedienungstasten',  # strings.txt:6486
    'SETUP_IRMAP_DESC': "Sie können zwischen Tastenfunktionsgruppen wählen, die Tasten der Fernbedienung mit bestimmten Funktionen belegen. Die Gruppe 'Standard' wird in der Dokumentation beschrieben.",  # strings.txt:6506
    'SETUP_SYNCHRONIZE': 'Synchronisieren',  # strings.txt:4690
    'SETUP_SYNCHRONIZE_DESC': "Der Player kann mit anderen Playern synchronisiert werden, um simultan dieselbe Musik wiederzugeben. Wählen Sie die gewünschten Player aus der Liste der verfügbaren Synchronisierungsgruppen. Wählen Sie 'Keine Synchronisierung', um die Funktion zu deaktivieren.",  # strings.txt:4711
    'SETUP_NO_SYNCHRONIZATION': 'Keine Synchronisation',  # strings.txt:4732
    'SETUP_SYNCVOLUME': 'Lautstärke synchronisieren',  # strings.txt:4793
    'SETUP_SYNCVOLUME_DESC': "Sie können die Lautstärke synchronisierter Player aufeinander abstimmen oder unabhängig belassen. Wählen Sie die gewünschte Option und klicken Sie auf 'Ändern'.",  # strings.txt:4814
    'SETUP_SYNCVOLUME_ON': 'Player-Lautstärke synchronisieren',  # strings.txt:4753
    'SETUP_SYNCVOLUME_OFF': 'Player-Lautstärke nicht synchronisieren',  # strings.txt:4773
    'SETUP_SYNCPOWER': 'Ein-/Ausschalten synchronisieren',  # strings.txt:4874
    'SETUP_SYNCPOWER_DESC': "Sie können diesen Player einzeln oder zusammen mit den anderen synchronisierten Playern ein- bzw. ausschalten. Wählen Sie die gewünschte Option und klicken Sie auf 'Ändern'.",  # strings.txt:4895
    'SETUP_SYNCPOWER_ON': 'In Gruppe ein-/ausschalten',  # strings.txt:4834
    'SETUP_SYNCPOWER_OFF': 'Getrennt ein-/ausschalten',  # strings.txt:4854
    'SETUP_MAINTAINSYNC': 'Synchronisation beibehalten',  # strings.txt:5175
    'SETUP_MAINTAINSYNC_DESC': 'Auch wenn mehrere Player die Wiedergabe eines Titels gleichzeitig starten, können sie nach einer gewissen Zeit (i.d.R. nach ein paar Minuten) voneinander abweichen. Eine Differenz von mehr als 30\u00a0ms ist wahrnehmbar, wenn die Ausgabe mehrerer Player gleichzeitig gehört werden kann. Lyrion Music Server prüft ständig, wie viel ein Player wiedergegeben hat und nimmt bei Bedarf Änderungen vor. Sie können dieses Verhalten aktivieren bzw. deaktivieren.',  # strings.txt:5194
    'SETUP_MAINTAINSYNC_ON': 'Synchronisation bei der Wiedergabe beibehalten',  # strings.txt:5213
    'SETUP_MAINTAINSYNC_OFF': 'Synchronisation nicht beibehalten',  # strings.txt:5232
    'SETUP_STARTDELAY': 'Startverzögerung des Players (ms)',  # strings.txt:5139
    'SETUP_STARTDELAY_DESC': "Es kann eine gewisse Zeit dauern, bis ein Player die Wiedergabe beginnt (bis Sie die Musik hören), nachdem der von Lyrion Music Server empfangene Startbefehl verarbeitet wurde. Dies kann bei der Nutzung digitaler Ausgänge (abhängig vom angeschlossenen Gerät) oder bei bestimmten Software-Playern unter bestimmten Plattformen vorkommen. Sie können dann diese Verzögerung hier festlegen, damit Lyrion Music Server weiß, wie bald dieser Player gestartet werden soll, damit die Wiedergabe aus allen synchronisierten Playern gleichzeitig gestartet wird. Hinweis: Diese Verzögerung gilt zusätzlich zu 'Audioverzögerung des Players' (siehe unten).",  # strings.txt:5157
    'SETUP_PLAYDELAY': 'Audioverzögerung des Players (ms)',  # strings.txt:5251
    'SETUP_PLAYDELAY_DESC': 'Es kann ein wahrnehmbarer Unterschied zwischen der abgelaufenen Zeit, die der Player an Lyrion Music Server meldet, und der tatsächlichen Wiedergabezeit auftreten. Dies kann bei der Nutzung digitaler Ausgänge (abhängig vom angeschlossenen Gerät) oder bei bestimmten Software-Playern unter bestimmten Plattformen vorkommen. In diesen Fällen können Sie hier die Verzögerung einstellen, damit diese von Lyrion Music Server beim Anpassen der Synchronisation einbezogen wird.',  # strings.txt:5269
    'SETUP_MINSYNCADJUST': 'Minimale Synchronisationsanpassung (ms)',  # strings.txt:5287
    'SETUP_MINSYNCADJUST_DESC': 'Wenn Lyrion Music Server erkennt, dass die Synchronisation zweier Player leicht verschoben ist, wird i.d.R. am langsameren Player eine geringfügige Anpassung vorgenommen. Sie können die minimale Anpassung für diesen Player konfigurieren. Je größer dieses Intervall ist, desto seltener werden Anpassungen vorgenommen aber desto häufiger können Abweichungen vorkommen.',  # strings.txt:5306
    'SETUP_PACKETLATENCY': 'Netzwerkpaket-Wartezeit (ms)',  # strings.txt:5324
    'SETUP_PACKETLATENCY_DESC': 'Lyrion Music Server misst automatisch die Netzwerk-Wartezeit zwischen der Anwendung und dem Player. Dies ist zum Beibehalten der Synchronisation wichtig. Wenn diese Messungen zu inkonsistent sind, empfehlen wir, hier einen typischen Wert einzugeben.',  # strings.txt:5342

}
