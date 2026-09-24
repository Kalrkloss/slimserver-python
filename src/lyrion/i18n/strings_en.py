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
    # ── Server-Einstellungen: Software-Updates / Netzwerk / Sicherheit / ────
    # ── Dateitypen / Logging / Leistung (web/settings.py) ───────────────────
    # Keys der Perl-Seiten ``settings/server/{software,networking,security,``
    # ``filetypes,debugging,performance}.html``; EN-Spalte der Perl-Tabelle
    # (``strings.txt``, Zeile je Eintrag), Sprache wie ``Strings.pm:525-536``.
    'MUSIC': 'Music',  # strings.txt:393
    'EVERYTHING': 'Everything',  # strings.txt:723
    'SETUP_GROUP_FORMATS_CONVERSION': 'File Format Conversion Setup',  # strings.txt:4024
    'SETUP_GROUP_FORMATS_CONVERSION_DESC': 'Lyrion Music Server can convert audio file formats on-the-fly for playback on your player.  You can disable specific formats below by unchecking them.  Only the name of the binary is shown here. To see/edit the entire command line, you will need to open convert.conf.  Click Change to save your changes.',  # strings.txt:4044
    'SETUP_FORMATS_PREFER_NATIVE': 'Prefer native format (decoding on the player) whenever possible',  # strings.txt:4064
    'SETUP_FORMATSLIST_MISSING_BINARY': 'Required binary was not found:',  # strings.txt:4076
    'FILE_FORMAT': 'File Format',  # strings.txt:4096
    'SETUP_INPUTTYPE': 'From',  # strings.txt:4116
    'STREAM_FORMAT': 'Stream Format',  # strings.txt:4128
    'SETUP_DISABLEDEXTENSIONSAUDIO': 'Disabled Audio File Extensions',  # strings.txt:4148
    'SETUP_DISABLEDEXTENSIONSAUDIO_DESC': 'Lyrion Music Server will look in the Media Folders for all supported file types (audio files and cue sheets). To disable specific file types from being processed when scanning the Music Folder, enter a comma separated list of file extensions below. E.g.: cue, mp4, aac',  # strings.txt:4167
    'SETUP_DISABLEDEXTENSIONSPLAYLIST': 'Disabled Playlist File Extensions',  # strings.txt:4186
    'SETUP_DISABLEDEXTENSIONSPLAYLIST_DESC': 'Lyrion Music Server will look in the Playlist Folder for all supported file types (playlist files and cue sheets). To disable specific file types from being processed when scanning the Playlist Folder, enter a comma separated list of file extensions below. E.g.: m3u, pls, cue',  # strings.txt:4205
    'DECODER': 'Decoder',  # strings.txt:4264
    'SETUP_BUFFERSECS': 'Radio Station Buffer Seconds',  # strings.txt:5026
    'SETUP_BUFFERSECS_DESC': 'When playing an Internet stream, the player buffers a small amount of data before beginning playback.  Specify the amount of audio data to buffer, in seconds from 3 to 30.  The default value is 3 seconds.  If you experience stuttering audio, increasing this value may help.',  # strings.txt:5045
    'SETUP_MAXWMARATE': 'Maximum WMA Stream Bitrate',  # strings.txt:5064
    'SETUP_MAXWMARATE_DESC': 'Some WMA streams have multiple bitrates available for streaming.  By default, Lyrion Music Server will select the highest bitrate stream available, but if you have a slower Internet connection, you may set a lower value here to limit the maximum rate that can be chosen from these multiple rate streams.',  # strings.txt:5083
    'SETUP_SYNCSTARTDELAY': 'Synchronized Players Startup Delay (ms)',  # strings.txt:5102
    'SETUP_SYNCSTARTDELAY_DESC': 'When multiple players are synchronized, Lyrion Music Server tells each player to start the track at the same time. It needs to make sure that each player has time to receive the start command before they all start together and so it sends the start command ahead of time and the players each wait until the right moment to start. If there is excessive network congestion, which can happen particularly when there are many players on the network and with wireless networks, then the default delay of 200ms may not be sufficient. You can change the default here if necessary.',  # strings.txt:5121
    'FOLDER': 'Folder',  # strings.txt:6260
    'SETUP_LANGUAGE': 'Language',  # strings.txt:6564
    'SETUP_LIBRARY_NAME': 'Media Library Name',  # strings.txt:6601
    'SETUP_MEDIADIRS': 'Media Folders',  # strings.txt:6655
    'SETUP_MEDIADIRS_DESC': 'You can specify one or more folders containing media files that Lyrion Music Server will scan and add to your media library.  Enter the paths to the folder below.',  # strings.txt:6673
    'SETUP_PLAYLISTDIR': 'Playlists Folder',  # strings.txt:6713
    'SETUP_PLAYLISTDIR_DESC': 'You can enter the path to a directory where your saved playlist files are stored on your hard disk. (You can leave this blank if you don\'t want to save playlists.)',  # strings.txt:6835
    'SETUP_RESCAN': 'Rescan Media Library',  # strings.txt:6855
    'SETUP_RESCAN_BUTTON': 'Rescan',  # strings.txt:6874
    'SETUP_RESCAN_DESC': 'Click Rescan to have Lyrion Music Server scan through your media library and add new media or update files that have changed.',  # strings.txt:6894
    'SETUP_WIPEDB': 'Clear library and rescan everything',  # strings.txt:6912
    'SETUP_STANDARDRESCAN': 'Look for new and changed media files',  # strings.txt:6930
    'SETUP_PLAYLISTRESCAN': 'Only rescan playlists',  # strings.txt:6948
    'SETUP_AUTO_RESCAN': 'Automatically detect changes',  # strings.txt:6974
    'SETUP_AUTO_RESCAN_DESC': 'Lyrion Music Server can watch your media folders and automatically detect changes.  New and changed files should appear in your library within several minutes.',  # strings.txt:6991
    'SETUP_AUTO_RESCAN_ENABLE': 'Enabled, watch for changes automatically',  # strings.txt:7008
    'SETUP_AUTO_RESCAN_DISABLE': 'Disabled, manually scan for new media files',  # strings.txt:7025
    'SETUP_AUTO_RESCAN_STAT_INTERVAL': 'Network share detection interval',  # strings.txt:7042
    'SETUP_AUTO_RESCAN_STAT_INTERVAL_DESC': 'To detect changes on remote network shares or on some operating systems with no native change detection support, the files must be polled for changes at regular intervals. Select the interval to use for this check, in minutes. The default value is 10 minutes.',  # strings.txt:7059
    'SETUP_HTTPPORT': 'Web Server Port Number',  # strings.txt:7790
    'SETUP_HTTPPORT_DESC': 'You can change the port number that is used to access the server from a web browser. (The default is 9000.)',  # strings.txt:7810
    'SETUP_HTTPPORT_OK': 'Now using port:',  # strings.txt:7830
    'SETUP_CHECKVERSION': 'Software Updates',  # strings.txt:7980
    'SETUP_CHECKVERSION_DESC': 'Lyrion Music Server can automatically check to see if an updated version of the software is available.  If it is, then a message will appear in Lyrion Music Server\'s web interface. Please note that Lyrion Music Server will always check for plugin updates according to the selected intervals, even if software checks are disabled.',  # strings.txt:8000
    'SETUP_CHECKVERSION_1': 'Automatically check for software updates',  # strings.txt:8012
    'SETUP_CHECKVERSION_0': 'Don\'t check for server software updates',  # strings.txt:8032
    'SETUP_CHECKVERSION_HOURLY': 'Hourly',  # strings.txt:8052
    'SETUP_CHECKVERSION_DAILY': 'Daily',  # strings.txt:8063
    'SETUP_CHECKVERSION_WEEKLY': 'Weekly',  # strings.txt:8074
    'SETUP_CHECKVERSION_MONTHLY': 'Monthly',  # strings.txt:8085
    'SETUP_AUTO_DOWNLOAD': 'Automatic Download',  # strings.txt:8096
    'SETUP_AUTO_DOWNLOAD_0': 'Do not automatically download updates',  # strings.txt:8130
    'SETUP_AUTO_DOWNLOAD_1': 'Automatically download updates when they\'re available.',  # strings.txt:8147
    'SETUP_CHECK_NOW': 'Check for updates now',  # strings.txt:8164
    'SETUP_SCAN_ON_PREF_CHANGE': 'Trigger Scan on Preference Changes',  # strings.txt:9160
    'SETUP_SCAN_ON_PREF_CHANGE_DESC': 'Some preferences need a full wipe & rescan of your media library to take effect. This scan can be triggered automatically if you wish. But it could cause multiple scans to be queued up if you change several preferences at once.',  # strings.txt:9172
    'SETUP_SCAN_ON_PREF_CHANGE_ON': 'Trigger scan automatically',  # strings.txt:9185
    'SETUP_SCAN_ON_PREF_CHANGE_OFF': 'Prompt to scan, but don\'t trigger it automatically',  # strings.txt:9198
    'SETUP_DBHIGHMEM': 'Database Memory Config',  # strings.txt:9224
    'SETUP_DBHIGHMEM_DESC': 'If your server has enough memory, you can obtain better performance by increasing the amount of memory available to the database. Changing this option requires a server restart.',  # strings.txt:9241
    'SETUP_DBHIGHMEM_NORMAL': 'Normal',  # strings.txt:9258
    'SETUP_DBHIGHMEM_HIGH': 'High (recommended for machines with 1+ GB RAM)',  # strings.txt:9275
    'SETUP_DBHIGHMEM_MAX': 'Maximum (recommended for libraries with more than 50,000 tracks and machines with 2+ GB RAM)',  # strings.txt:9292
    'SETUP_DISABLESTATISTICS': 'Library Statistics',  # strings.txt:9305
    'SETUP_DISABLESTATISTICS_DESC': 'When the Lyrion Music Server front page comes up, it scans your media library to figure out your library statistics and displays them on the front page.  You can choose to disable this behavior, which will shorten the time it takes to initially open up the web interface, especially with a large library.',  # strings.txt:9323
    'SETUP_DISABLE_STATISTICS': 'Disable library statistics',  # strings.txt:9341
    'SETUP_ENABLE_STATISTICS': 'Enable library statistics',  # strings.txt:9359
    'SETUP_ENHANCEDHTTP': 'Streaming mode for HTTP(S)',  # strings.txt:9377
    'SETUP_ENHANCEDHTTP_DESC': 'When a HTTP(S) connection to a server fails, Lyrion Music Server terminates playback. It can also try to re-open it (Persistent mode) or it can buffer streams (Cache mode) of defined length (podcasts, single tracks) on disk files. These will be removed once the stream has been played. This can improve the reliability with some servers that expect tracks to be downloaded, not streamed and thus close long connections. But it also does cause more writing to the file system, which could potentially wear out e.g. SD cards. If you have enough space for buffering files, then "Cache" mode is the way to go.',  # strings.txt:9388
    'SETUP_ENABLE_PERSISTENTHTTP': 'Persistent mode',  # strings.txt:9399
    'SETUP_ENABLE_BUFFEREDHTTP': 'Cache HTTP(S) streams on disk',  # strings.txt:9410
    'SETUP_DISABLE_ENHANCEDHTTP': 'Normal streaming',  # strings.txt:9421
    'SETUP_MAX_REDIRECTS': 'Maximum number of redirects',  # strings.txt:9724
    'SETUP_MAX_REDIRECTS_DESC': 'The maximum number of HTTP redirects to follow. Should more redirects be required, then the request is considered failure.',  # strings.txt:9734
    'SETUP_WEBPROXY': 'Web Proxy',  # strings.txt:9744
    'SETUP_WEBPROXY_DESC': 'You can specify the IP address and port for an HTTP/Web proxy to use when Lyrion Music Server connects to servers outside your local network.  Use the format xx.xx.xx.xx:yyyy where xx.xx.xx.xx is the IP address of the proxy, and yyyy is the port number.  Leave this field blank if you don\'t use a HTTP web proxy.',  # strings.txt:9763
    'SETUP_CORS_ALLOWED_HOSTS': 'CORS Allowed Hosts',  # strings.txt:9801
    'SETUP_CORS_ALLOWED_HOSTS_DESC': 'Cross-Origin Resource Sharing (CORS) is a mechanism that uses additional HTTP headers to tell a browser to let a web application running at one origin (domain) have permission to access selected resources from a server at a different origin.<br><br>Enter a comma separated list of hostnames you want to give access to your Lyrion Music Server. The entries need to have the protocol (eg. "http"), full host name ("www.example.com"), plus the port number, should the latter be non-standard. In most cases something like "https://www.example.com" should be good enough.',  # strings.txt:9812
    'SETUP_CSRFPROTECTIONLEVEL': 'CSRF Protection Level',  # strings.txt:9823
    'SETUP_CSRFPROTECTIONLEVEL_DESC': 'To protect against &quot;Cross Site Request Forgery&quot; (CSRF) security threats, Lyrion Music Server applies special scrutiny to HTTP requests for functions that can make changes to your system or manipulate playlists or players.  You may choose the level of scrutiny for Lyrion Music Server to use.  The default is None. <a href="/html/docs/http.html#csrf">See Help Section</a> for more details.',  # strings.txt:9841
    'SETUP_INSECURE_HTTPS': 'Insecure HTTPS',  # strings.txt:9859
    'SETUP_INSECURE_HTTPS2': 'Switch off certificate verification on HTTPS connections.',  # strings.txt:9870
    'SETUP_IPFILTER_HEAD': 'Block Incoming Connections',  # strings.txt:9892
    'SETUP_IPFILTER_DESC': 'This option allows you to enable blocking of incoming CLI and HTTP requests by source IP address.',  # strings.txt:9912
    'SETUP_IPFILTER': 'Block',  # strings.txt:9932
    'SETUP_NO_IPFILTER': 'Do not block',  # strings.txt:9952
    'SETUP_FILTERRULE_HEAD': 'Allowed IP Addresses',  # strings.txt:9972
    'SETUP_FILTERRULE_DESC': 'If you have turned on blocking you can enter the addresses that you\'d like to allow to connect to Lyrion Music Server.<br><br>This field accepts a list of specific IP addresses, * style wildcards, and IP address ranges in a comma separated list. For example:<BR><BR><B>10.1.2.2</B> will allow only 10.1.2.2 to connect<BR><BR><B>10.1.2.*</B> will allow anything with an IP address that be the 10.1.2.x addresses.<BR><BR><B>10.1.2.2-50</B> will allow IP addresses in the range 10.1.2.2 - 10.1.2.50 to connect.<BR><BR>Finally, multiple entries can be combined with commas, like this <B>10.1.2.2,172.16.1.*,192.168.1-254</B><BR><BR>The special IP address 127.0.0.1 allows a web browser to connect to Lyrion Music Server running on that same computer.',  # strings.txt:9992
    'SETUP_NO_AUTHORIZE': 'No password protection',  # strings.txt:10048
    'SETUP_AUTHORIZE': 'Password Protection',  # strings.txt:10068
    'SETUP_AUTHORIZE_DESC': 'You can choose to password protect Lyrion Music Server Remote Control. Choose "Password protection" below and enter a username and password, then click Change. From that point on when you use Lyrion Music Server Remote Control, you\'ll need to enter this username and password in your web browser.',  # strings.txt:10088
    'SETUP_USERNAME': 'Username',  # strings.txt:10108
    'SETUP_MISSING_USERNAME': 'You can\'t enable authorization without a password.',  # strings.txt:10128
    'SETUP_PASSWORD': 'Password',  # strings.txt:10145
    'SETUP_PASSWORD_REPEAT': 'Confirm Password',  # strings.txt:10165
    'SETUP_PASSWORD_MISMATCH': 'The passwords you entered do not match.',  # strings.txt:10182
    'SETUP_REMOTESTREAMTIMEOUT': 'Radio Station Timeout',  # strings.txt:10199
    'SETUP_REMOTESTREAMTIMEOUT_DESC': 'You can set the length of time that Lyrion Music Server will try to connect to an internet radio station or other source before giving up. Enter the amount of time, in seconds, below.',  # strings.txt:10218
    'NATIVE': 'Native',  # strings.txt:11444
    'NONE': 'None',  # strings.txt:11832
    'KBPS': 'kbps',  # strings.txt:13487
    'NO_LIMIT': 'No Limit',  # strings.txt:13498
    'FORMATS_SETTINGS': 'File Types',  # strings.txt:15158
    'SECURITY_SETTINGS': 'Security',  # strings.txt:15178
    'PERFORMANCE_SETTINGS': 'Performance',  # strings.txt:15198
    'NETWORK_SETTINGS': 'Network',  # strings.txt:15238
    'DEBUG_RADIO': 'Internet Radio',  # strings.txt:15322
    'DEBUG_TRANSCODING': 'Transcoder',  # strings.txt:15339
    'DEBUG_SERVER_CHOOSE': 'Server',  # strings.txt:15354
    'DEBUG_SCANNER_CHOOSE': 'Media Scanner',  # strings.txt:15368
    'DEBUG_SELECT_SET': 'Please select log set...',  # strings.txt:15385
    'DEBUG_DEFAULT': 'Reset logging preferences',  # strings.txt:15402
    'SETUP_GROUP_DEBUG_DESC': 'Lyrion Music Server has a number of logging settings that can be used to record detailed information about server and scanner operation. Each logging category has a severity for logging. With "Debug" being the most verbose, and "Off" being the least.',  # strings.txt:15419
    'DEBUGGING_SETTINGS': 'Logging',  # strings.txt:15437
    'DEBUGGING_ADVANCED': 'Advanced Log Settings',  # strings.txt:15454
    'CHECKVERSION_PROBLEM': 'There was a problem while checking for updates to Lyrion Music Server. (Error code %s)',  # strings.txt:16244
    'SETUP_PRECACHEARTWORK': 'Artwork Pre-caching',  # strings.txt:19444
    'SETUP_PRECACHEARTWORK_DESC': 'By default, all cover art found during the scanning phase is automatically resized and cached to improve performance when using Squeezebox Controller or the web interface.  This does slow down the scan process, so it can be disabled here.',  # strings.txt:19461
    'SETUP_PRECACHEARTWORK_ENABLED': 'Pre-cache album artwork',  # strings.txt:19478
    'SETUP_PRECACHEARTWORK_DISABLED': 'Do not pre-cache artwork',  # strings.txt:19495
    'SETUP_PRECACHEARTWORK_CUSTOM_SPECS': 'Custom pre-caching specifications',  # strings.txt:19512
    'SETUP_IMAGEPROXY': 'Artwork resizing',  # strings.txt:19525
    'SETUP_IMAGEPROXY_DESC': 'In order to get best quality artwork and performance on the devices, artwork files are resized before being sent to the client. By default this is handled by Lyrion Music Server itself. The helper can offload some of the heavy lifting to a separate process managed by the server application.',  # strings.txt:19538
    'SETUP_IMAGEPROXY_LOCAL': 'Use Lyrion Music Server to resize artwork',  # strings.txt:19549
    'SETUP_IMAGEPROXY_HELPER': 'Use Lyrion Music Server resizing helper to resize artwork',  # strings.txt:19562
    'SETUP_SERVERPRIORITY': 'Server Priority',  # strings.txt:19572
    'SETUP_SHUFFLE_METHOD': 'Shuffle method',  # strings.txt:19590
    'SETUP_USE_BALANCED_SHUFFLE': 'Use more balanced, but slower shuffle',  # strings.txt:19601
    'SETUP_USE_FASTER_SHUFFLE': 'Use faster, but less balanced shuffle',  # strings.txt:19612
    'SETUP_SERVERPRIORITY_DESC': 'You can specify the priority for Lyrion Music Server.',  # strings.txt:19623
    'SETUP_SCANNERPRIORITY': 'Scanner Priority',  # strings.txt:19641
    'SETUP_SCANNERPRIORITY_DESC': 'You can specify the priority for the scanning process.',  # strings.txt:19659
    'SETUP_PRIORITY_CURRENT': 'Current Server Priority',  # strings.txt:19677
    'SETUP_PRIORITY_HIGH': 'High',  # strings.txt:19695
    'SETUP_PRIORITY_ABOVE_NORMAL': 'Above Normal',  # strings.txt:19713
    'SETUP_PRIORITY_NORMAL': 'Normal',  # strings.txt:19729
    'SETUP_PRIORITY_BELOW_NORMAL': 'Below Normal',  # strings.txt:19743
    'SETUP_PRIORITY_LOW': 'Low',  # strings.txt:19761
    'SETUP_DEBUG_SERVER_LOG': 'Lyrion Music Server Log File',  # strings.txt:21313
    'SETUP_DEBUG_SERVER_LOG_DESC': 'Lyrion Music Server keeps a log file for all application related activities (Audio Streaming, Infrared, etc) here:',  # strings.txt:21331
    'SETUP_DEBUG_SCANNER_LOG': 'Scanner Log File',  # strings.txt:21349
    'SETUP_DEBUG_SCANNER_LOG_DESC': 'Lyrion Music Server keeps a log file for all scanning related activities, including iTunes & MusicIP here:',  # strings.txt:21367
    'LINES': 'lines',  # strings.txt:21384
    'SETUP_LOG_ZIPPED': 'ZIP archive',  # strings.txt:21435
    'SETUP_DEBUG_LEVEL_OFF': 'Off',  # strings.txt:21449
    'SETUP_DEBUG_LEVEL_FATAL': 'Fatal',  # strings.txt:21469
    'SETUP_DEBUG_LEVEL_ERROR': 'Error',  # strings.txt:21486
    'SETUP_DEBUG_LEVEL_WARN': 'Warn',  # strings.txt:21503
    'SETUP_DEBUG_LEVEL_INFO': 'Info',  # strings.txt:21520
    'SETUP_DEBUG_LEVEL_DEBUG': 'Debug',  # strings.txt:21536
    'SAVE_SETTINGS': 'Save Settings',  # strings.txt:21553
    'SETUP_VIEW_SCANNING': 'Scanning - View Progress',  # strings.txt:22325
    'SETUP_VIEW_NOT_SCANNING': 'View Previous Scan Details',  # strings.txt:22342
    'PERSIST_DEBUG_SETTINGS': 'Save logging settings for use at next application restart',  # strings.txt:22378
    'SETUP_MAXPLAYLISTLENGTH': 'Maximum Playlist Length',  # strings.txt:23170
    'SETUP_MAXPLAYLISTLENGTH_DESC': 'In order to protect the server against excessive memory use the maximum length of a playlist, including the Now-Playing playlist, can be restricted. A value of 0 means no restriction. If set then the minimum value is 10.',  # strings.txt:23187
    'CONTROLPANEL_NO_UPDATE_AVAILABLE': 'There\'s no updated Lyrion Music Server version available.',  # strings.txt:23289
    'SERVER_UPDATE_AVAILABLE': 'A new version of Lyrion Music Server is available (%s). <a href="%s" target="update">Click here to download</a>.',  # strings.txt:24044
    'SERVER_UPDATE_AVAILABLE_SHORT': 'A new version of Lyrion Music Server is available.',  # strings.txt:24062
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
