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
    # ── Player-Seiten (web/settings_player.py; Perl Player/{Menu,Remote, ────
    # ── Synchronization}.pm) ────────────────────────────────────────────────
    'MENU_SETTINGS': 'Menus',  # strings.txt:14911 (EN :14915)
    'REMOTE_SETTINGS': 'Remote',  # strings.txt:14985 (EN :14989)
    'SETUP_GROUP_MENUITEMS': 'Home Menu Customization',  # strings.txt:3611 (EN :3615)
    'SETUP_GROUP_MENUITEMS_DESC': 'You can customize the choices that are available on the top-level Home menu on the player\'s display. Click to move items up and down or to remove menu items. Click on "Add" to add a removed item back to the menu.',  # strings.txt:3630 (EN :3634)
    'SETUP_GROUP_NONMENUITEMS_INTRO': 'Inactive Menu Items:',  # strings.txt:3651 (EN :3655)
    'MOVEUP': 'Move Up',  # strings.txt:11223 (EN :11227)
    'MOVEDOWN': 'Move Down',  # strings.txt:11243 (EN :11247)
    'ADD': 'Add',  # strings.txt:11263 (EN :11267)
    'DELETE': 'Delete',  # strings.txt:16045 (EN :16049)
    'SETUP_GROUP_IRSETS': 'Remote Controls',  # strings.txt:3908 (EN :3912)
    'SETUP_GROUP_IRSETS_DESC': 'You can choose to have this player respond to or ignore certain infrared remote signals. To enable a remote control code set, put a check next to the name below. To disable a remote control code set, clear the check mark.',  # strings.txt:3928 (EN :3932)
    'SETUP_IRMAP': 'Remote Button Functions',  # strings.txt:6483 (EN :6487)
    'SETUP_IRMAP_DESC': 'You can choose between button function sets. These sets map particular buttons on the infrared remote to particular functions. The "Standard" set is the one that is described in the documentation.',  # strings.txt:6503 (EN :6507)
    'SETUP_SYNCHRONIZE': 'Synchronize',  # strings.txt:4687 (EN :4691)
    'SETUP_SYNCHRONIZE_DESC': 'The player can be synchronized with other players, enabling them to play the same music simultaneously. Choose the players you would like to synchronize with from the list of available synchronization groups. Choose No Synchronization to stop synchronization.',  # strings.txt:4708 (EN :4712)
    'SETUP_NO_SYNCHRONIZATION': 'No Synchronization',  # strings.txt:4729 (EN :4733)
    'SETUP_SYNCVOLUME': 'Synchronize Volume',  # strings.txt:4790 (EN :4794)
    'SETUP_SYNCVOLUME_DESC': 'Synchronize the volume of this player with the other players in the synchronization group.',  # strings.txt:4811 (EN :4815)
    'SETUP_SYNCVOLUME_ON': "Sync player's volume",  # strings.txt:4750 (EN :4754)
    'SETUP_SYNCVOLUME_OFF': "Don't sync player's volume",  # strings.txt:4770 (EN :4774)
    'SETUP_SYNCPOWER': 'Synchronize Power',  # strings.txt:4871 (EN :4875)
    'SETUP_SYNCPOWER_DESC': 'Synchronize the power state of this player with the other players in the synchronization group.',  # strings.txt:4892 (EN :4896)
    'SETUP_SYNCPOWER_ON': 'Power off/on with group',  # strings.txt:4831 (EN :4835)
    'SETUP_SYNCPOWER_OFF': 'Power off/on separately',  # strings.txt:4851 (EN :4855)
    'SETUP_MAINTAINSYNC': 'Maintain Synchronization',  # strings.txt:5172 (EN :5176)
    'SETUP_MAINTAINSYNC_DESC': 'Lyrion Music Server will attempt to keep the players in the synchronization group aligned while playing.',  # strings.txt:5191 (EN :5195)
    'SETUP_MAINTAINSYNC_ON': 'Maintain synchronization while playing',  # strings.txt:5210 (EN :5214)
    'SETUP_MAINTAINSYNC_OFF': "Don't maintain synchronization",  # strings.txt:5229 (EN :5233)
    'SETUP_STARTDELAY': 'Player Start Delay (ms)',  # strings.txt:5136 (EN :5140)
    'SETUP_STARTDELAY_DESC': 'A player can take a noticeable time to start playing (before you begin to hear the audio) after it processes the start command from Lyrion Music Server. This can be the case when using digital outputs (depending upon the connected equipment) or with software players on some platforms. In these cases, this delay can be set here so that Lyrion Music Server knows how much in advance to start this player so that the audio from all the synchronized players starts at the same time. Note: this is in addition to any ongoing "Player Audio Delay" (below).',  # strings.txt:5154 (EN :5158)
    'SETUP_PLAYDELAY': 'Player Audio Delay (ms)',  # strings.txt:5248 (EN :5252)
    'SETUP_PLAYDELAY_DESC': 'There can be a noticeable difference between how much of a track that the player reports it has played to Lyrion Music Server and how much audio has been heard. This can be the case when using digital outputs (depending upon the connected equipment) or with software players on some platforms. In these cases, this delay can be set here so that Lyrion Music Server can take it into account when adjusting synchronization.',  # strings.txt:5266 (EN :5270)
    'SETUP_MINSYNCADJUST': 'Minimum Synchronization Adjustment (ms)',  # strings.txt:5284 (EN :5288)
    'SETUP_MINSYNCADJUST_DESC': 'When Lyrion Music Server detects that two synchronized players have gotten a little bit out of synchronization with each other, it makes a small adjustment, usually to the player that has gotten behind. You can configure the minimum size of such adjustments for this player. The larger this interval, the less frequent will be the adjustments but the more the players will be allowed to drift apart.',  # strings.txt:5303 (EN :5307)
    'SETUP_PACKETLATENCY': 'Network Packet Latency (ms)',  # strings.txt:5321 (EN :5325)
    'SETUP_PACKETLATENCY_DESC': 'Lyrion Music Server automatically measures the network latency between it and the player. This is important when maintaining synchronization. Occasionally these measurements can be too inconsistent to be useful, in which case you can specify a typical value here.',  # strings.txt:5339 (EN :5343)
}