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