"""English (failsafe) strings for the keys this port emits.

Source: Perl reference /tmp/lms-ref, `EN` column of the string tables, parsed like
Slim/Utils/Strings.pm `parseStrings`/`storeString` (Strings.pm:334-421). `EN` is the
failsafe language (Strings.pm:62); a key without a translation for the active
language falls back here (Strings.pm:414-416). Source file:keyline per entry.
"""

from __future__ import annotations

STRINGS_EN: dict[str, str] = {
    'CHOICE_OFF': 'Off',  # strings.txt:1153
    'LOW': 'Low',  # strings.txt:11846
    'MEDIUM': 'Medium',  # strings.txt:11790
    'HIGH': 'High',  # strings.txt:11863
    'FIXED_VOLUME_100': 'Fixed Volume 100%',  # strings.txt:11662
    'ANALOGOUTMODE_HEADPHONE': 'Headphones',  # strings.txt:20213
    'ANALOGOUTMODE_SUBOUT': 'Subwoofer',  # strings.txt:20205
    'ANALOGOUTMODE_ALWAYS_ON': 'Always On',  # strings.txt:20230
    'ANALOGOUTMODE_ALWAYS_OFF': 'Always Off',  # strings.txt:20247
    'TRANSITION_NONE': 'None',  # strings.txt:6056
    'TRANSITION_CROSSFADE': 'Crossfade',  # strings.txt:6074
    'TRANSITION_FADE_IN': 'Fade in',  # strings.txt:6092
    'TRANSITION_FADE_OUT': 'Fade out',  # strings.txt:6110
    'TRANSITION_FADE_IN_OUT': 'Fade in and out',  # strings.txt:6128
    'REPLAYGAIN_DISABLED': 'No Volume Adjustment',  # strings.txt:5672
    'REPLAYGAIN_TRACK_GAIN': 'Track Gain',  # strings.txt:5707
    'REPLAYGAIN_ALBUM_GAIN': 'Album Gain',  # strings.txt:5725
    'REPLAYGAIN_SMART_GAIN': 'Smart Gain',  # strings.txt:5743
    'SORT_ARTISTALBUM': 'Artist, Album',  # strings.txt:19031
    'SORT_ARTISTYEARALBUM': 'Artist, Year, Album',  # strings.txt:19049
    'ALBUM': 'Album',  # strings.txt:12438
    'BRIGHTNESS_DARK': 'Dark',  # strings.txt:15573
    'BRIGHTNESS_DIMMEST': 'Dimmest',  # strings.txt:15487
    'BRIGHTNESS_BRIGHTEST': 'Brightest',  # strings.txt:15593
    'BRIGHTNESS_AMBIENT': 'Automatic',  # strings.txt:15504
    'SETUP_POWERONBRIGHTNESS_ABBR': 'While Active',  # strings.txt:4468
    'SETUP_POWEROFFBRIGHTNESS_ABBR': 'While Off',  # strings.txt:4486
    'SETUP_IDLEBRIGHTNESS_ABBR': 'Idle',  # strings.txt:4504
    'SETUP_MINAUTOBRIGHTNESS': 'Minimal Brightness (Automatic)',  # strings.txt:4522
    'SETUP_SENSAUTOBRIGHTNESS': 'Brightness Sensitivity (Automatic)',  # strings.txt:4576
    'LIGHT': 'Light',  # strings.txt:11899
    'STANDARD': 'Standard',  # strings.txt:11936
    'FULL': 'Full',  # strings.txt:11881
    'LIGHT_N': 'Light Narrow',  # strings.txt:15521
    'STANDARD_N': 'Standard Narrow',  # strings.txt:15539
    'FULL_N': 'Full Narrow',  # strings.txt:15556
    'SMALL': 'Small',  # strings.txt:11770
    'LARGE': 'Large',  # strings.txt:11750
    'HUGE': 'Huge',  # strings.txt:11809
    'OFF': 'off',  # strings.txt:11183
    'ON': 'on',  # strings.txt:11203
    'ALARM_ALL_ALARMS': 'All Alarms',  # strings.txt:1855
    'ALARM_ADD': 'Add Alarm',  # strings.txt:1675
    'ALARM_VOLUME': 'Alarm Volume',  # strings.txt:1621
    'ALARM_FADE': 'Fade Alarms In',  # strings.txt:1891
    'ALARM_ALARM_ENABLED': 'Enabled',  # strings.txt:1747
    'ALARM_SET_TIME': 'Set Time',  # strings.txt:1910
    'ALARM_SET_DAYS': 'Choose Days',  # strings.txt:1928
    'ALARM_SELECT_PLAYLIST': 'Alarm Sound',  # strings.txt:1585
    'ALARM_DELETE': 'Remove Alarm',  # strings.txt:1729
    'ALARM_ALARM': 'Alarm',  # strings.txt:1509
    'ALARM_ALARM_REPEAT': 'Repeat Alarm',  # strings.txt:1801
    'ALARM_ALARM_ONETIME': 'One Time Alarm',  # strings.txt:1819
    'ALARM_OFF': 'Off',  # strings.txt:1565
    'ALARM_DAY0': 'Sunday',  # strings.txt:2147
    'ALARM_DAY1': 'Monday',  # strings.txt:2166
    'ALARM_DAY2': 'Tuesday',  # strings.txt:2185
    'ALARM_DAY3': 'Wednesday',  # strings.txt:2204
    'ALARM_DAY4': 'Thursday',  # strings.txt:2223
    'ALARM_DAY5': 'Friday',  # strings.txt:2242
    'ALARM_DAY6': 'Saturday',  # strings.txt:2261
    'ALARM_SHORT_DAY_0': 'Su',  # strings.txt:2280
    'ALARM_SHORT_DAY_1': 'Mo',  # strings.txt:2298
    'ALARM_SHORT_DAY_2': 'Tu',  # strings.txt:2316
    'ALARM_SHORT_DAY_3': 'We',  # strings.txt:2334
    'ALARM_SHORT_DAY_4': 'Th',  # strings.txt:2352
    'ALARM_SHORT_DAY_5': 'Fr',  # strings.txt:2370
    'ALARM_SHORT_DAY_6': 'Sa',  # strings.txt:2388
    'JIVE_ALARMSET_HELP': 'Use the scroll wheel to change clock digits, then press the center button to select that digit. Press the center button after time is entered to set the alarm time.',  # strings.txt:22609
    'SHUFFLE': 'Shuffle',  # strings.txt:11300
    'SHUFFLE_OFF': "Don't Shuffle Playlist",  # strings.txt:11380
    'SHUFFLE_ON_SONGS': 'Shuffle by Song',  # strings.txt:11320
    'SHUFFLE_ON_ALBUMS': 'Shuffle by Album',  # strings.txt:11340
    'CANCEL': 'Cancel',  # strings.txt:22746
    'EMPTY': 'Empty',  # strings.txt:445
    'SLEEP_CANCEL': 'Cancel sleep',  # strings.txt:1249
    'SLEEPING_IN_X_MINUTES': 'Sleeping in %s minutes',  # strings.txt:1267
    'X_MINUTES': '%s minutes',  # strings.txt:1285
    'SLEEP_AT_END_OF_SONG': 'Sleep at end of song',  # strings.txt:1322
    'NOTHING_CURRENTLY_PLAYING': 'Nothing currently playing',  # strings.txt:1358
    'SYNC_ABOUT': 'Add one or more additional Squeezeboxes to use the Synchronize feature and realize the full potential of multi-room audio. Visit Lyrion.org for more information.',  # strings.txt:23921
    'SYNC_X_TO': 'Sync %s to:',  # strings.txt:16814
    'DO_NOT_SYNC': 'No Sync',  # strings.txt:16831
    'SYNCING_WITH': 'Syncing with: %s',  # strings.txt:16868
    'UNSYNCING_FROM': 'Unsyncing from: %s',  # strings.txt:16885
    'RECENT_SEARCHES': 'Recent Searches',  # strings.txt:22967
    'PRESET_ADDING': 'Saving preset #%s...',  # strings.txt:23803
    'PRESET': 'Preset #%s',  # strings.txt:23841
    'PRESETS_NOT_DEFINED': 'Preset #%s not defined.',  # strings.txt:23784
    'JIVE_SET_PRESET_X': 'Set Preset %s',  # strings.txt:22462
    'JIVE_OVERWRITE_PRESET_X': 'Replace %s?',  # strings.txt:22445
    'ADD': 'Add',  # strings.txt:11263
    'DELETE': 'Delete',  # strings.txt:16045
    'FAVORITES': 'Favorites',  # Slim/Plugin/Favorites/strings.txt:3
    # The jive popup after `favorites add` (Favorites/Plugin.pm:904-911).
    'FAVORITES_ADDING': 'Saving favorites...',  # Slim/Plugin/Favorites/strings.txt:38/:42
    'HOME': 'Home',  # strings.txt:3140
    # Country of the "Lokales Radio" feed (web/settings.py, pref
    # `radiobrowser_country`).  Perl's global table has exactly one "country"
    # label — the Podcast plugin's field (Strings.pm loads plugin tables too);
    # measured in the reference tree /tmp/lms-full/Slim, where the Favorites
    # refs above come from: PLUGIN_PODCAST_COUNTRY key :270, EN :274.
    'PLUGIN_PODCAST_COUNTRY': 'Country',  # Slim/Plugin/Podcast/strings.txt:270 (EN :274)
    # "all" option of that select (an empty pref means all countries).
    'ALL': 'all',  # strings.txt:11281 (EN :11285)
    # Settings.pm:168 `sprintf(SETTINGS_INVALIDVALUE, $value, $pref)` — the
    # warning for a pref value the validator rejected.
    'SETTINGS_INVALIDVALUE': 'Invalid value "%s" for %s',  # strings.txt:10145 (EN :10149)
    # ── Player firmware (Slim/Player/Squeezebox.pm) ─────────────────────────
    # The `showBriefly` block when the image for the target revision is not on
    # disk (`Squeezebox2.pm:344-347`, `Squeezebox.pm:330-334`).
    'FIRMWARE_MISSING': 'Error: Missing Firmware',  # strings.txt:2765 (EN :2774)
    'FIRMWARE_MISSING_DESC': "Server can't connect to Internet to obtain firmware update.",  # strings.txt:2784 (EN :2793)
    # `UPDATING_FIRMWARE_<UC(model)>` (`Squeezebox.pm:420`) — one key per player
    # class, exactly as the Perl table carries them.
    'UPDATING_FIRMWARE_SQUEEZEBOX': 'Updating Squeezebox firmware.',  # strings.txt:2669 (EN :2678)
    'UPDATING_FIRMWARE_SQUEEZEBOX2': 'Updating Squeezebox firmware.',  # strings.txt:2689 (EN :2698)
    'UPDATING_FIRMWARE_TRANSPORTER': 'Updating Transporter firmware.',  # strings.txt:2709 (EN :2718)
    'UPDATING_FIRMWARE_RECEIVER': 'Updating Squeezebox Firmware.',  # strings.txt:2729 (EN :2738)
    'UPDATING_FIRMWARE_BOOM': 'Updating Firmware.',  # strings.txt:2747 (EN :2756)
    # `(` . $line . ' (1 ' . string('OUT_OF') . ' 2)'` — the Boom two-stage
    # upgrade counter (`Squeezebox.pm:425-434`).
    'OUT_OF': 'of',  # strings.txt:777 (EN :786)
    # ── Plugin management (Slim/Web/Settings/Server/Plugins.pm) ─────────────
    # Page name (`Plugins.pm:40-42` protectName('SETUP_PLUGINS')) and the
    # restart message of the plugin manager (`PluginManager.pm:569-585`).
    'SETUP_PLUGINS': 'Manage Plugins',  # strings.txt:7183 (EN :7187)
    'PLUGINS_RESTART_MSG': 'Plugins have been updated - Restart Required',  # strings.txt:23730 (EN :23734)
    # Restart hint of the settings page (`Plugins.pm:341`, used with :157).
    'SETUP_EXTENSIONS_RESTART_MSG': 'Please restart Lyrion Music Server for the changes to take effect.',  # strings.txt:7409 (EN :7413)
    # The plugin list states (`Plugins.pm:310` highlights enabled/disabled).
    'ENABLED': 'Enabled',  # strings.txt:11400 (EN :11404)
    'DISABLED': 'Disabled',  # strings.txt:11420 (EN :11424)
    'PLUGINS_CHANGED_NEED_RESTART': 'Changes will take place at the next application restart. <a href="%s">Please click here to restart the server now.</a>',  # strings.txt:22253 (EN :22257)
    # Plugin name and description of the shipped DSTM plugin
    # (Slim/Plugin/DontStopTheMusic/strings.txt:1/:5, :12/:16).
    'PLUGIN_DSTM': "Don't Stop The Music",  # DontStopTheMusic/strings.txt:1 (EN :5)
    'PLUGIN_DSTM_DESC': "Don't Stop The Music will make sure you don't let the silence kill your flow. Once you reach the end of your playlist it will automatically add music similar to what you've been listening to.",  # DontStopTheMusic/strings.txt:12 (EN :16)
    'PLUGIN_NAME': 'Plugin',  # strings.txt:11293 (EN :11297)
}
