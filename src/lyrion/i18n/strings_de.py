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
    'HOME': 'Hauptmenü',  # strings.txt:3140
}
