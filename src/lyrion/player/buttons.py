"""IR / button opcodes -> actions (PROT-19).

Perl is the sole reference. Every table entry below is transcribed verbatim
from the pinned Perl tree (/tmp/lms-ref, read-only) and carries its source
file:line in the surrounding comment.

Wire formats (``Slim/Networking/Slimproto.pm``):

* ``IR  `` (:521-546) ``_ir_handler`` — payload is exactly 10 bytes
  (:530 ``length != 10`` -> warn "bad length ... for IR. Ignoring"):
  ``time(4, BE) codeformat(1) nbits(1) ircode(4, BE)``. ``unpack('NxxH8')``
  (:539) yields the 1 kHz tick time and the IR code as 8 UPPERCASE hex
  digits.
* ``BUTN`` (:1270-1280) ``_button_handler`` — ``unpack('NH8')`` (:1275):
  ``time(4, BE) code(4)`` -> the very same hex string; the code is handed to
  the SAME ``Slim::Hardware::IR::enqueue`` (:1277) as ``IR  ``, so a hard
  button and an IR button share one lookup path.
* ``KNOB`` (:1282-1318) ``_knob_handler`` — ``unpack('NNC')`` (:1287):
  ``time(4) position(4, signed via bit 31, :1290-1292) sync(1)``. It calls
  ``executeButton($client, 'knob', ...)`` (:1315) and answers ``knoa``
  (:1317).

Dispatch chain after the frame (Perl):
``_ir_handler``/``_button_handler`` -> ``IR::enqueue`` (:541, :1277) ->
``IR::idle`` -> ``$client->execute(['ir', bytes, time])`` (IR.pm:126) ->
``Commands::irCommand`` -> ``IR::processIR`` (IR.pm:634) ->
``lookupCodeBytes`` (IR.pm:435) -> ``lookup``/``lookupFunction``
(IR.pm:459/491) -> ``processCode`` (IR.pm:1116) -> ``executeButton``
(IR.pm:1052) -> ``Slim::Buttons::Common::getFunction`` (Common.pm:1338) ->
the mode's function sub.

Code tables:

* ``IR_CODE_SETS`` is built from ``IR/Slim_Devices_Remote.ir``,
  ``IR/Front_Panel.ir`` and ``IR/jvc_dvd.ir`` — Perl loads every ``*.ir``
  file in the IR dir and inverts ``button = code`` into ``code -> button``
  (IR.pm:165-199, :359-394, :392). All sets are enabled for EVERY player
  unless the per-player pref ``disabledirsets`` excludes them (IR.pm:400-410);
  ``hasFrontPanel`` (Client.pm:668 -> 0, Boom.pm:212, Transporter.pm:362)
  only filters which files the *setup page* offers (IR.pm:188), not the
  runtime lookup. => UNKLAR: there is NO per-player-class code table in Perl.
* ``BUTTON_FUNCTIONS`` is ``IR/Default.map`` — the one map that is the
  default for every player class (``irmap`` pref default,
  Player.pm:41 ``Slim::Hardware::IR::defaultMapFile()``; IR.pm:212-220).
  ``button.*`` expands to the six press styles (IR.pm:43, :343-355).
* ``lookupFunction`` searches the player's maps in the order
  ``(mode, <class or previous mode>, 'common')`` (IR.pm:491-527), so
  ``[common]`` is the fallback every mode shares.

Functions: a function name is turned into ``(sub, arg)`` by
``Slim::Buttons::Common::getFunction`` (Common.pm:1338-1360): the exact name
wins, else ``X_Y`` splits into sub ``X`` with arg ``Y``. ``executeButton``
passes the FUNCTION name (not the button name) to the sub as its second
argument (IR.pm:1084, :1104); an unknown function is only logged
(IR.pm:1106-1112 "Button [..] with irCode: [..] not implemented in mode:
[..]"), an unknown IR code only notifies ``unknownir`` (IR.pm:644-651).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

logger = logging.getLogger("lyrion.player.buttons")


# ---------------------------------------------------------------------------
# Wire formats — Slim/Networking/Slimproto.pm
# ---------------------------------------------------------------------------

IR_FRAME_LEN = 10                 # Slimproto.pm:530
KNOB_FRAME_LEN = 9                # Slimproto.pm:1287 'NNC' = 4+4+1

# Slim/Hardware/IR.pm:43 — the styles a 'button.*' map entry expands to.
BUTTON_PRESS_STYLES = ('', '.single', '.double', '.repeat', '.hold', '.hold_release')

# Slim/Networking/Slimproto.pm:62 'IR  ' (space padded) / :58 'BUTN'.
IR_OPCODE = "IR  "
BUTN_OPCODE = "BUTN"
KNOB_OPCODE = "KNOB"


def parse_ir_frame(payload: bytes) -> tuple[int, str] | None:
    """``IR  `` payload -> (tick time, 8-hex code); None on bad length.

    Slimproto.pm:530-541: ``length != 10`` is warned and ignored, then
    ``unpack('NxxH8', ...)`` (:539) reads the 4-byte big-endian time and the
    4-byte code as uppercase hex (2 skipped bytes = code format + bit count).
    """
    if len(payload) != IR_FRAME_LEN:              # :530
        return None
    tick = int.from_bytes(payload[0:4], "big")    # 'N'
    code = payload[6:10].hex().upper()            # 'H8'
    return tick, code


def parse_butn_frame(payload: bytes) -> tuple[int, str]:
    """``BUTN`` payload -> (tick time, 8-hex code). Slimproto.pm:1275
    ``unpack('NH8', ...)`` — same code form as ``IR  `` (:1277)."""
    tick = int.from_bytes(payload[0:4], "big")
    code = payload[4:8].hex().upper()
    return tick, code


def parse_knob_frame(payload: bytes) -> tuple[int, int, int]:
    """``KNOB`` payload -> (tick time, signed position, sync).

    Slimproto.pm:1287-1292 ``unpack('NNC')``; a set bit 31 makes the position
    negative (Perl has no unsigned network long at :1289-1292).
    """
    tick = int.from_bytes(payload[0:4], "big")
    position = int.from_bytes(payload[4:8], "big")
    if position & (1 << 31):                      # :1290
        position = -(position & 0x7FFFFFFF)       # :1291
    sync = payload[8] if len(payload) > 8 else 0
    return tick, position, sync


# ---------------------------------------------------------------------------
# Tables — transcribed from the Perl IR/ files (see module docstring)
# ---------------------------------------------------------------------------

# IR/Slim_Devices_Remote.ir + IR/Front_Panel.ir + IR/jvc_dvd.ir
# Inverted button=code -> code->button exactly like IR.pm:392 loadIRFile.
IR_CODE_SETS: dict[str, dict[str, str]] = {
    'Slim_Devices_Remote.ir': {
        '76899867': '0',
        '7689F00F': '1',
        '768908F7': '2',
        '76898877': '3',
        '768948B7': '4',
        '7689C837': '5',
        '768928D7': '6',
        '7689A857': '7',
        '76896897': '8',
        '7689E817': '9',
        '7689B04F': 'arrow_down',
        '7689906F': 'arrow_left',
        '7689D02F': 'arrow_right',
        '7689E01F': 'arrow_up',
        '768900FF': 'voldown',
        '7689807F': 'volup',
        '768940BF': 'power',
        '7689C03F': 'rew',
        '768920DF': 'pause',
        '7689A05F': 'fwd',
        '7689609F': 'add',
        '768910EF': 'play',
        '768958A7': 'search',
        '7689D827': 'shuffle',
        '768938C7': 'repeat',
        '7689B847': 'sleep',
        '76897887': 'now_playing',
        '7689F807': 'size',
        '768904FB': 'brightness',
        '768918E7': 'favorites',
        '7689708F': 'browse',
        '76898F70': 'power_on',
        '76898778': 'power_off',
        '768922DD': 'home',
        '7689A25D': 'now_playing_2',
        '7689629D': 'search_2',
        '7689E21D': 'favorites_2',
        '76897C83': 'menu_browse_album',
        '7689748B': 'menu_browse_artist',
        '76897A85': 'menu_browse_playlists',
        '7689728D': 'menu_browse_music',
        '768954AB': 'menu_search_artist',
        '76895CA3': 'menu_search_album',
        '768952AD': 'menu_search_song',
        '768906F9': 'digital_input_aes-ebu',
        '76898679': 'digital_input_bnc-spdif',
        '768946B9': 'digital_input_rca-spdif',
        '7689C639': 'digital_input_toslink',
        '76890EF1': 'analog_input_line_in',
        '7689C43B': 'muting',
        '76898A75': 'preset_1',
        '76894AB5': 'preset_2',
        '7689CA35': 'preset_3',
        '76892AD5': 'preset_4',
        '7689AA55': 'preset_5',
        '76896A95': 'preset_6',
    },
    'Front_Panel.ir': {
        '00010000': '0.down',
        '00010001': '1.down',
        '00010002': '2.down',
        '00010003': '3.down',
        '00010004': '4.down',
        '00010005': '5.down',
        '00010006': '6.down',
        '00010007': '7.down',
        '00010008': '8.down',
        '00010009': '9.down',
        '0001000A': 'power_front.down',
        '0001000B': 'arrow_up.down',
        '0001000C': 'arrow_down.down',
        '0001000D': 'arrow_left.down',
        '0001000E': 'knob_push.down',
        '0001000F': 'search.down',
        '00010010': 'rew.down',
        '00010011': 'fwd.down',
        '00010012': 'play.down',
        '00010013': 'add.down',
        '00010014': 'brightness.down',
        '00010015': 'now_playing.down',
        '00010017': 'pause.down',
        '00010018': 'browse.down',
        '00010019': 'volup_front.down',
        '0001001A': 'voldown_front.down',
        '0001001B': 'size.down',
        '0001001C': 'visual.down',
        '0001001D': 'volumemode.down',
        '00010023': 'preset_1.down',
        '00010024': 'preset_2.down',
        '00010025': 'preset_3.down',
        '00010026': 'preset_4.down',
        '00010027': 'preset_5.down',
        '00010028': 'preset_6.down',
        '00010029': 'snooze.down',
        '0001005A': 'knob_left',
        '0001005B': 'knob_right',
        '00020000': '0.up',
        '00020001': '1.up',
        '00020002': '2.up',
        '00020003': '3.up',
        '00020004': '4.up',
        '00020005': '5.up',
        '00020006': '6.up',
        '00020007': '7.up',
        '00020008': '8.up',
        '00020009': '9.up',
        '0002000A': 'power_front.up',
        '0002000B': 'arrow_up.up',
        '0002000C': 'arrow_down.up',
        '0002000D': 'arrow_left.up',
        '0002000E': 'knob_push.up',
        '0002000F': 'search.up',
        '00020010': 'rew.up',
        '00020011': 'fwd.up',
        '00020012': 'play.up',
        '00020013': 'add.up',
        '00020014': 'brightness.up',
        '00020015': 'now_playing.up',
        '00020017': 'pause.up',
        '00020018': 'browse.up',
        '00020019': 'volup_front.up',
        '0002001A': 'voldown_font.up',
        '0002001B': 'size.up',
        '0002001C': 'visual.up',
        '0002001D': 'volumemode.up',
        '00020023': 'preset_1.up',
        '00020024': 'preset_2.up',
        '00020025': 'preset_3.up',
        '00020026': 'preset_4.up',
        '00020027': 'preset_5.up',
        '00020028': 'preset_6.up',
        '00020029': 'snooze.up',
    },
    'jvc_dvd.ir': {
        '0000F776': '0',
        '0000F786': '1',
        '0000F746': '2',
        '0000F7C6': '3',
        '0000F726': '4',
        '0000F7A6': '5',
        '0000F766': '6',
        '0000F7E6': '7',
        '0000F716': '8',
        '0000F796': '9',
        '0000F78B': 'arrow_down',
        '0000F74B': 'arrow_left',
        '0000F7CB': 'arrow_right',
        '0000F70B': 'arrow_up',
        '0000F70E': 'rew',
        '0000F76E': 'fwd',
        '0000F78D': 'brightness_down',
        '0000F70D': 'brightness_up',
        '0000F703': 'size',
        '0000F7B6': 'format',
        '0000F783': 'menu_home',
        '0000C038': 'muting',
        '0000C538': 'muting',
        '0000F72B': 'shuffle',
        '0000F7B2': 'pause',
        '0000F7F6': 'now_playing',
        '0000F732': 'play',
        '0000F7D6': 'play',
        '0000F702': 'power',
        '0000F743': 'add',
        '0000F7AB': 'repeat',
        '0000F7B3': 'sleep',
        '0000F7C2': 'stop',
        '0000C0F8': 'voldown',
        '0000C5F8': 'voldown',
        '0000F7F8': 'voldown',
        '0000C078': 'volup',
        '0000C578': 'volup',
        '0000F778': 'volup',
        '0000F779': 'menu_playlist',
        '0000F77A': 'menu_browse_genre',
        '0000F77B': 'menu_browse_artist',
        '0000F77C': 'menu_browse_album',
        '0000F77D': 'menu_browse_music',
        '0000F77E': 'menu_search_artist',
        '0000F77F': 'menu_search_album',
        '0000F780': 'menu_search_song',
        '0000F781': 'menu_browse_playlists',
        '0000F782': 'menu_plugins',
        '0000F784': 'menu_settings',
        '0000F785': 'menu_pop',
        '0000F704': 'textsize_small',
        '0000F705': 'textsize_large',
        '0000F706': 'textsize_medium',
        '0000F72C': 'shuffle_off',
        '0000F72D': 'shuffle_on',
        '0000F7AC': 'repeat_off',
        '0000F7AD': 'repeat_one',
        '0000F7AE': 'repeat_all',
        '0000F700': 'power_off',
        '0000F701': 'power_on',
        '0000F7F9': 'playdisp_none',
        '0000F7FA': 'playdisp_et',
        '0000F7FB': 'playdisp_rt',
        '0000F7FC': 'playdisp_pb',
        '0000F7FD': 'playdisp_pbet',
        '0000F7FE': 'playdisp_pbrt',
    },
}

# IR/Default.map, parsed exactly like IR.pm:300-357 loadMapFile.
BUTTON_FUNCTIONS: dict[str, dict[str, dict[str, str]]] = {
    'Default.map': {
        'modename': {},
        'common': {
            '0': 'numberScroll_0',
            '1': 'numberScroll_1',
            '2': 'numberScroll_2',
            '3': 'numberScroll_3',
            '4': 'numberScroll_4',
            '5': 'numberScroll_5',
            '6': 'numberScroll_6',
            '7': 'numberScroll_7',
            '8': 'numberScroll_8',
            '9': 'numberScroll_9',
            '0.hold': 'preset_0',
            '1.hold': 'preset_1',
            '2.hold': 'preset_2',
            '3.hold': 'preset_3',
            '4.hold': 'preset_4',
            '5.hold': 'preset_5',
            '6.hold': 'preset_6',
            '7.hold': 'preset_7',
            '8.hold': 'preset_8',
            '9.hold': 'preset_9',
            '0.hold_release': 'preset_release0',
            '1.hold_release': 'preset_release1',
            '2.hold_release': 'preset_release2',
            '3.hold_release': 'preset_release3',
            '4.hold_release': 'preset_release4',
            '5.hold_release': 'preset_release5',
            '6.hold_release': 'preset_release6',
            '7.hold_release': 'preset_release7',
            '8.hold_release': 'preset_release8',
            '9.hold_release': 'preset_release9',
            'arrow_down': 'down',
            'arrow_down.repeat': 'down_repeat',
            'knob_right': 'down',
            'knob_right.repeat': 'down_repeat',
            'arrow_left': 'left',
            'arrow_left.hold': 'home',
            'arrow_right': 'right',
            'knob_push': 'right',
            'arrow_up': 'up',
            'arrow_up.repeat': 'up_repeat',
            'knob_left': 'up',
            'knob_left.repeat': 'up_repeat',
            'rew.single': 'jump_rew',
            'rew.hold': 'song_scanner',
            'brightness_down': 'brightness_down',
            'brightness_up': 'brightness_up',
            'brightness.single': 'brightness_toggle',
            'brightness.hold': 'dead',
            'brightness': 'dead',
            'fwd.single': 'jump_fwd',
            'fwd.hold': 'song_scanner',
            'format': 'titleFormat',
            'muting': 'muting',
            'pause.single': 'pause',
            'pause.hold': 'stop',
            'play.single': 'play',
            'play.hold': 'play',
            'power': 'power_toggle',
            'power_front': 'power_toggle',
            'power_front.hold': 'dead',
            'power_front.repeat': 'dead',
            'power_off': 'power_off',
            'power_on': 'power_on',
            'add.single': 'add',
            'add.hold': 'dead',
            'sleep.single': 'sleep',
            'sleep.hold': 'sleep',
            'snooze.single': 'snooze',
            'snooze.hold': 'sleep',
            'stop': 'stop',
            'voldown': 'volume',
            'voldown.repeat': 'volume',
            'volup': 'volume',
            'volup.repeat': 'volume',
            'volup_front': 'volume_front',
            'volup_front.repeat': 'volume_front',
            'voldown_front': 'volume_front',
            'voldown_front.repeat': 'volume_front',
            'pitchdown': 'pitch_down',
            'pitchdown.repeat': 'pitch_down',
            'pitchup': 'pitch_up',
            'pitchup.repeat': 'pitch_up',
            'bassdown': 'bass_down',
            'bassdown.repeat': 'bass_down',
            'bassup': 'bass_up',
            'bassup.repeat': 'bass_up',
            'trebledown': 'treble_down',
            'trebledown.repeat': 'treble_down',
            'trebleup': 'treble_up',
            'trebleup.repeat': 'treble_up',
            'volume': 'volume',
            'volumemode': 'volumemode',
            'home': 'home',
            'preset_0.single': 'playPreset_0',
            'preset_1.single': 'playPreset_1',
            'preset_2.single': 'playPreset_2',
            'preset_3.single': 'playPreset_3',
            'preset_4.single': 'playPreset_4',
            'preset_5.single': 'playPreset_5',
            'preset_6.single': 'playPreset_6',
            'preset_7.single': 'playPreset_7',
            'preset_8.single': 'playPreset_8',
            'preset_9.single': 'playPreset_9',
            'preset_0.hold': 'favorites_add0',
            'preset_1.hold': 'favorites_add1',
            'preset_2.hold': 'favorites_add2',
            'preset_3.hold': 'favorites_add3',
            'preset_4.hold': 'favorites_add4',
            'preset_5.hold': 'favorites_add5',
            'preset_6.hold': 'favorites_add6',
            'preset_7.hold': 'favorites_add7',
            'preset_8.hold': 'favorites_add8',
            'preset_9.hold': 'favorites_add9',
            'now_playing': 'playdisp_toggle',
            'playdisp_none': 'playdisp_0',
            'playdisp_et': 'playdisp_1',
            'playdisp_rt': 'playdisp_2',
            'playdisp_pb': 'playdisp_3',
            'playdisp_pbet': 'playdisp_4',
            'playdisp_pbrt': 'playdisp_5',
            'size': 'textsize_toggle',
            'textsize_small': 'textsize_small',
            'textsize_medium': 'textsize_medium',
            'textsize_large': 'textsize_large',
            'shuffle.single': 'shuffle_toggle',
            'shuffle.hold': 'randomPlay',
            'shuffle_off': 'shuffle_off',
            'shuffle_on': 'shuffle_on',
            'repeat': 'repeat_toggle',
            'repeat_off': 'repeat_0',
            'repeat_one': 'repeat_1',
            'repeat_all': 'repeat_2',
            'browse': 'browse',
            'search': 'globalsearch',
            'favorites.single': 'favorites',
            'favorites.hold': 'favorites_add',
            'visual': 'visual_toggle',
            'favorites_2.single': 'favorites',
            'favorites_2.hold': 'favorites_add',
            'now_playing_2': 'playdisp_toggle',
            'search_2': 'globalsearch',
            'menu_playlist': 'menu_playlist',
            'menu_browse_genre': 'menu_browse_genre',
            'menu_browse_artist': 'menu_browse_artist',
            'menu_browse_album': 'menu_browse_album',
            'menu_browse_music': 'menu_browse_music',
            'menu_search_artist': 'menu_search_artist',
            'menu_search_album': 'menu_search_album',
            'menu_search_song': 'menu_search_song',
            'menu_browse_playlists': 'menu_browse_playlists',
            'menu_plugins': 'menu_plugins',
            'menu_home': 'menu_home',
            'menu_settings': 'menu_settings',
            'menu_pop': 'menu_pop',
            'menu_now_playing': 'menu_now_playing',
            'menu_synchronize': 'menu_synchronize',
            'digital_input_aes-ebu': 'modefunction_Slim::Plugin::DigitalInput::Plugin->aes-ebu',
            'digital_input_bnc-spdif': 'modefunction_Slim::Plugin::DigitalInput::Plugin->bnc-spdif',
            'digital_input_rca-spdif': 'modefunction_Slim::Plugin::DigitalInput::Plugin->rca-spdif',
            'digital_input_toslink': 'modefunction_Slim::Plugin::DigitalInput::Plugin->toslink',
            'analog_input_line_in': 'modefunction_Slim::Plugin::LineIn::Plugin->linein',
        },
        'home': {
            'arrow_left.hold': 'dead',
        },
        'playlist': {
            'add.hold': 'zap',
        },
        'off': {
            'play.single': 'modefunction_off->play',
            'add': 'dead',
            'add.single': 'dead',
            'add.double': 'dead',
            'add.repeat': 'dead',
            'add.hold': 'dead',
            'add.hold_release': 'dead',
            'arrow_right': 'dead',
            'arrow_right.single': 'dead',
            'arrow_right.double': 'dead',
            'arrow_right.repeat': 'dead',
            'arrow_right.hold': 'dead',
            'arrow_right.hold_release': 'dead',
            'knob_push': 'dead',
            'knob_push.single': 'dead',
            'knob_push.double': 'dead',
            'knob_push.repeat': 'dead',
            'knob_push.hold': 'dead',
            'knob_push.hold_release': 'dead',
            'arrow_left': 'dead',
            'arrow_left.single': 'dead',
            'arrow_left.double': 'dead',
            'arrow_left.repeat': 'dead',
            'arrow_left.hold': 'dead',
            'arrow_left.hold_release': 'dead',
            'arrow_down': 'dead',
            'arrow_down.single': 'dead',
            'arrow_down.double': 'dead',
            'arrow_down.repeat': 'dead',
            'arrow_down.hold': 'dead',
            'arrow_down.hold_release': 'dead',
            'arrow_up': 'dead',
            'arrow_up.single': 'dead',
            'arrow_up.double': 'dead',
            'arrow_up.repeat': 'dead',
            'arrow_up.hold': 'dead',
            'arrow_up.hold_release': 'dead',
            'knob_left': 'dead',
            'knob_left.single': 'dead',
            'knob_left.double': 'dead',
            'knob_left.repeat': 'dead',
            'knob_left.hold': 'dead',
            'knob_left.hold_release': 'dead',
            'knob_right': 'dead',
            'knob_right.single': 'dead',
            'knob_right.double': 'dead',
            'knob_right.repeat': 'dead',
            'knob_right.hold': 'dead',
            'knob_right.hold_release': 'dead',
            'format': 'dead',
            'fwd': 'dead',
            'fwd.single': 'dead',
            'fwd.double': 'dead',
            'fwd.repeat': 'dead',
            'fwd.hold': 'dead',
            'fwd.hold_release': 'dead',
            'menu_browse_album': 'dead',
            'menu_browse_artist': 'dead',
            'menu_browse_genre': 'dead',
            'menu_browse_music': 'dead',
            'menu_browse_playlists': 'dead',
            'menu_home': 'dead',
            'menu_playlist': 'dead',
            'menu_plugins': 'dead',
            'menu_pop': 'dead',
            'menu_search_album': 'dead',
            'menu_search_artist': 'dead',
            'menu_search_song': 'dead',
            'menu_settings': 'dead',
            'menu_now_playing': 'dead',
            'muting': 'dead',
            'now_playing': 'dead',
            'pause': 'dead',
            'pause.single': 'dead',
            'pause.double': 'dead',
            'pause.repeat': 'dead',
            'pause.hold': 'dead',
            'pause.hold_release': 'dead',
            'playdisp_et': 'dead',
            'playdisp_toggle': 'dead',
            'playdisp_none': 'dead',
            'playdisp_pb': 'dead',
            'playdisp_pbet': 'dead',
            'playdisp_pbrt': 'dead',
            'playdisp_rt': 'dead',
            'repeat': 'dead',
            'repeat_toggle': 'dead',
            'repeat_all': 'dead',
            'repeat_off': 'dead',
            'repeat_one': 'dead',
            'rew': 'dead',
            'rew.single': 'dead',
            'rew.double': 'dead',
            'rew.repeat': 'dead',
            'rew.hold': 'dead',
            'rew.hold_release': 'dead',
            'shuffle': 'dead',
            'shuffle_toggle': 'dead',
            'shuffle.single': 'dead',
            'shuffle.double': 'dead',
            'shuffle.repeat': 'dead',
            'shuffle.hold': 'dead',
            'shuffle.hold_release': 'dead',
            'shuffle_off': 'dead',
            'shuffle_on': 'dead',
            'search': 'dead',
            'browse': 'dead',
            'browse.single': 'dead',
            'browse.double': 'dead',
            'browse.repeat': 'dead',
            'browse.hold': 'dead',
            'browse.hold_release': 'dead',
            'favorites': 'dead',
            'favorites.single': 'dead',
            'favorites.double': 'dead',
            'favorites.repeat': 'dead',
            'favorites.hold': 'dead',
            'favorites.hold_release': 'dead',
            'search_2': 'dead',
            'browse_2': 'dead',
            'browse_2.single': 'dead',
            'browse_2.double': 'dead',
            'browse_2.repeat': 'dead',
            'browse_2.hold': 'dead',
            'browse_2.hold_release': 'dead',
            'favorites_2': 'dead',
            'favorites_2.single': 'dead',
            'favorites_2.double': 'dead',
            'favorites_2.repeat': 'dead',
            'favorites_2.hold': 'dead',
            'favorites_2.hold_release': 'dead',
            'sleep.single': 'snooze',
            'sleep.hold': 'dead',
            'snooze.single': 'snooze',
            'snooze.hold': 'dead',
            'stop': 'dead',
            'voldown': 'dead',
            'voldown.single': 'dead',
            'voldown.double': 'dead',
            'voldown.repeat': 'dead',
            'voldown.hold': 'dead',
            'voldown.hold_release': 'dead',
            'volup': 'dead',
            'volup.single': 'dead',
            'volup.double': 'dead',
            'volup.repeat': 'dead',
            'volup.hold': 'dead',
            'volup.hold_release': 'dead',
            'volup_front': 'dead',
            'volup_front.single': 'dead',
            'volup_front.double': 'dead',
            'volup_front.repeat': 'dead',
            'volup_front.hold': 'dead',
            'volup_front.hold_release': 'dead',
            'voldown_front': 'dead',
            'voldown_front.single': 'dead',
            'voldown_front.double': 'dead',
            'voldown_front.repeat': 'dead',
            'voldown_front.hold': 'dead',
            'voldown_front.hold_release': 'dead',
            '0': 'dead',
            '1': 'dead',
            '2': 'dead',
            '3': 'dead',
            '4': 'dead',
            '5': 'dead',
            '6': 'dead',
            '7': 'dead',
            '8': 'dead',
            '9': 'dead',
            'pitchdown': 'dead',
            'pitchdown.single': 'dead',
            'pitchdown.double': 'dead',
            'pitchdown.repeat': 'dead',
            'pitchdown.hold': 'dead',
            'pitchdown.hold_release': 'dead',
            'pitchup': 'dead',
            'pitchup.single': 'dead',
            'pitchup.double': 'dead',
            'pitchup.repeat': 'dead',
            'pitchup.hold': 'dead',
            'pitchup.hold_release': 'dead',
            'bassdown': 'dead',
            'bassdown.single': 'dead',
            'bassdown.double': 'dead',
            'bassdown.repeat': 'dead',
            'bassdown.hold': 'dead',
            'bassdown.hold_release': 'dead',
            'bassup': 'dead',
            'bassup.single': 'dead',
            'bassup.double': 'dead',
            'bassup.repeat': 'dead',
            'bassup.hold': 'dead',
            'bassup.hold_release': 'dead',
            'trebledown': 'dead',
            'trebledown.single': 'dead',
            'trebledown.double': 'dead',
            'trebledown.repeat': 'dead',
            'trebledown.hold': 'dead',
            'trebledown.hold_release': 'dead',
            'trebleup': 'dead',
            'trebleup.single': 'dead',
            'trebleup.double': 'dead',
            'trebleup.repeat': 'dead',
            'trebleup.hold': 'dead',
            'trebleup.hold_release': 'dead',
            'volumemode': 'dead',
            'volumemode.single': 'dead',
            'volumemode.double': 'dead',
            'volumemode.repeat': 'dead',
            'volumemode.hold': 'dead',
            'volumemode.hold_release': 'dead',
            'visual': 'dead',
            'visual.single': 'dead',
            'visual.double': 'dead',
            'visual.repeat': 'dead',
            'visual.hold': 'dead',
            'visual.hold_release': 'dead',
            'home': 'dead',
            'home.single': 'dead',
            'home.double': 'dead',
            'home.repeat': 'dead',
            'home.hold': 'dead',
            'home.hold_release': 'dead',
            'digital_input_aes-ebu': 'dead',
            'digital_input_bnc-spdif': 'dead',
            'digital_input_rca-spdif': 'dead',
            'digital_input_toslink': 'dead',
            'analog_input_line_in': 'dead',
        },
        'block': {
            'add': 'dead',
            'add.single': 'dead',
            'add.double': 'dead',
            'add.repeat': 'dead',
            'add.hold': 'dead',
            'add.hold_release': 'dead',
            'arrow_down': 'dead',
            'arrow_down.single': 'dead',
            'arrow_down.double': 'dead',
            'arrow_down.repeat': 'dead',
            'arrow_down.hold': 'dead',
            'arrow_down.hold_release': 'dead',
            'arrow_left': 'dead',
            'arrow_right': 'dead',
            'knob_push': 'dead',
            'arrow_up': 'dead',
            'arrow_up.single': 'dead',
            'arrow_up.double': 'dead',
            'arrow_up.repeat': 'dead',
            'arrow_up.hold': 'dead',
            'arrow_up.hold_release': 'dead',
            'knob_left': 'dead',
            'knob_left.single': 'dead',
            'knob_left.double': 'dead',
            'knob_left.repeat': 'dead',
            'knob_left.hold': 'dead',
            'knob_left.hold_release': 'dead',
            'knob_right': 'dead',
            'knob_right.single': 'dead',
            'knob_right.double': 'dead',
            'knob_right.repeat': 'dead',
            'knob_right.hold': 'dead',
            'knob_right.hold_release': 'dead',
            'format': 'dead',
            'fwd': 'dead',
            'fwd.single': 'dead',
            'fwd.double': 'dead',
            'fwd.repeat': 'dead',
            'fwd.hold': 'dead',
            'fwd.hold_release': 'dead',
            'menu_browse_album': 'dead',
            'menu_browse_artist': 'dead',
            'menu_browse_genre': 'dead',
            'menu_browse_music': 'dead',
            'menu_browse_playlists': 'dead',
            'menu_home': 'dead',
            'menu_playlist': 'dead',
            'menu_plugins': 'dead',
            'menu_pop': 'dead',
            'menu_search_album': 'dead',
            'menu_search_artist': 'dead',
            'menu_search_song': 'dead',
            'menu_settings': 'dead',
            'now_playing': 'dead',
            'play': 'dead',
            'play.single': 'dead',
            'play.double': 'dead',
            'play.repeat': 'dead',
            'play.hold': 'dead',
            'play.hold_release': 'dead',
            'power': 'dead',
            'power_front': 'dead',
            'power_front.single': 'dead',
            'power_front.double': 'dead',
            'power_front.repeat': 'dead',
            'power_front.hold': 'dead',
            'power_front.hold_release': 'dead',
            'power_off': 'dead',
            'power_on': 'dead',
            'rew': 'dead',
            'rew.single': 'dead',
            'rew.double': 'dead',
            'rew.repeat': 'dead',
            'rew.hold': 'dead',
            'rew.hold_release': 'dead',
            'shuffle': 'dead',
            'shuffle_off': 'dead',
            'shuffle_on': 'dead',
            'sleep': 'dead',
            'stop': 'dead',
            'home': 'dead',
            'home.single': 'dead',
            'home.double': 'dead',
            'home.repeat': 'dead',
            'home.hold': 'dead',
            'home.hold_release': 'dead',
        },
        'browsedb': {
            'play': 'dead',
            'play.single': 'play_0',
            'play.hold': 'create_mix',
            'add': 'dead',
            'add.single': 'play_1',
            'add.hold': 'play_2',
        },
        'browsetree': {
            'play': 'dead',
            'play.single': 'play_0',
            'play.hold': 'create_mix',
            'add': 'dead',
            'add.single': 'play_1',
            'add.hold': 'play_2',
        },
        'trackinfo': {
            'play': 'dead',
            'play.single': 'play_0',
            'play.hold': 'create_mix',
            'add': 'dead',
            'add.single': 'play_1',
            'add.hold': 'play_2',
        },
        'browsemenu': {
            'play': 'right',
            'add': 'right',
        },
        'plugins': {
            'play': 'right',
            'add': 'right',
        },
        'settings': {
            'play': 'right',
            'add': 'right',
        },
        'screensaver': {
            '0': 'done',
            '1': 'done',
            '2': 'done',
            '3': 'done',
            '4': 'done',
            '5': 'done',
            '6': 'done',
            '7': 'done',
            '8': 'done',
            '9': 'done',
            'arrow_down': 'done_passbackplaylist',
            'arrow_left': 'done_passbackplaylist',
            'arrow_right': 'done_passbackplaylist',
            'knob_push': 'done_passbackplaylist',
            'arrow_up': 'done_passbackplaylist',
            'play.single': 'done',
            'play.hold': 'done',
            'add.single': 'done',
            'add.hold': 'zap',
            'pause.single': 'done_passback',
            'pause.hold': 'done_passback',
            'stop': 'done_passback',
            'knob': 'done',
            'knob_left': 'volume',
            'knob_right': 'volume',
        },
        'idlesaver': {
            '0': 'done',
            '1': 'done',
            '2': 'done',
            '3': 'done',
            '4': 'done',
            '5': 'done',
            '6': 'done',
            '7': 'done',
            '8': 'done',
            '9': 'done',
            'arrow_down': 'done_passbackplaylist',
            'arrow_left': 'done_passbackplaylist',
            'arrow_right': 'done_passbackplaylist',
            'knob_push': 'done_passbackplaylist',
            'arrow_up': 'done_passbackplaylist',
            'play.single': 'done',
            'play.hold': 'done',
            'add.single': 'done',
            'add.hold': 'done',
            'pause.single': 'done_passback',
            'pause.stop': 'done_passback',
            'stop': 'done_passback',
            'fwd': 'done_passback',
            'rew': 'done_passback',
            'knob': 'done',
            'knob_left': 'done_passback',
            'knob_right': 'done_passback',
        },
        'INPUT.Time': {
            '0': 'numberLetter_0',
            '1': 'numberLetter_1',
            '2': 'numberLetter_2',
            '3': 'numberLetter_3',
            '4': 'numberLetter_4',
            '5': 'numberLetter_5',
            '6': 'numberLetter_6',
            '7': 'numberLetter_7',
            '8': 'numberLetter_8',
            '9': 'numberLetter_9',
            'arrow_left': 'left',
            'arrow_right': 'right',
            'knob_push': 'right',
            'search': 'exit_search',
            'play': 'exit_play',
            'play.single': 'dead',
            'play.double': 'dead',
            'play.repeat': 'dead',
            'play.hold': 'dead',
            'play.hold_release': 'dead',
            'add': 'exit_add',
            'add.single': 'dead',
            'add.double': 'dead',
            'add.repeat': 'dead',
            'add.hold': 'dead',
            'add.hold_release': 'dead',
            'knob_left': 'down',
            'knob_left.repeat': 'down',
            'knob_right': 'up',
            'knob_right.repeat': 'up',
        },
        'INPUT.Text': {
            '0': 'numberLetter_0',
            '0.hold': 'letter_0',
            '1': 'numberLetter_1',
            '1.hold': 'letter_1',
            '2': 'numberLetter_2',
            '2.hold': 'letter_2',
            '3': 'numberLetter_3',
            '3.hold': 'letter_3',
            '4': 'numberLetter_4',
            '4.hold': 'letter_4',
            '5': 'numberLetter_5',
            '5.hold': 'letter_5',
            '6': 'numberLetter_6',
            '6.hold': 'letter_6',
            '7': 'numberLetter_7',
            '7.hold': 'letter_7',
            '8': 'numberLetter_8',
            '8.hold': 'letter_8',
            '9': 'numberLetter_9',
            '9.hold': 'letter_9',
            'arrow_left': 'backspace',
            'arrow_right': 'nextChar',
            'knob_push': 'nextChar',
            'search': 'exit_search',
            'play': 'exit_play',
            'play.single': 'dead',
            'play.double': 'dead',
            'play.repeat': 'dead',
            'play.hold': 'dead',
            'play.hold_release': 'dead',
            'add': 'letter_space',
            'add.single': 'dead',
            'add.double': 'dead',
            'add.repeat': 'dead',
            'add.hold': 'dead',
            'add.hold_release': 'dead',
        },
        'INPUT.List': {
            'arrow_up': 'up',
            'arrow_up.repeat': 'up_repeat',
            'arrow_down': 'down',
            'arrow_down.repeat': 'down_repeat',
            'knob_left': 'up',
            'knob_left.repeat': 'up_repeat',
            'knob_right': 'down',
            'knob_right.repeat': 'down_repeat',
            'arrow_left': 'exit_left',
            'arrow_right': 'exit_right',
            'knob_push': 'exit_right',
            'play': 'passback',
            'play.single': 'passback',
            'play.double': 'passback',
            'play.repeat': 'passback',
            'play.hold': 'passback',
            'play.hold_release': 'passback',
            'add': 'passback',
            'add.single': 'passback',
            'add.double': 'passback',
            'add.repeat': 'passback',
            'add.hold': 'passback',
            'add.hold_release': 'passback',
            'search': 'passback',
            'search.single': 'passback',
            'search.double': 'passback',
            'search.repeat': 'passback',
            'search.hold': 'passback',
            'search.hold_release': 'passback',
            'stop': 'passback',
            'stop.single': 'passback',
            'stop.double': 'passback',
            'stop.repeat': 'passback',
            'stop.hold': 'passback',
            'stop.hold_release': 'passback',
            'pause': 'passback',
            'pause.single': 'passback',
            'pause.double': 'passback',
            'pause.repeat': 'passback',
            'pause.hold': 'passback',
            'pause.hold_release': 'passback',
        },
        'INPUT.Choice': {
            'arrow_left': 'exit_left',
            'arrow_right': 'exit_right',
            'knob_push': 'exit_right',
            'play': 'dead',
            'play.single': 'play_0',
            'play.hold': 'create_mix',
            'add': 'dead',
            'add.single': 'add_single',
            'add.double': 'dead',
            'add.repeat': 'dead',
            'add.hold': 'add_hold',
            'add.hold_release': 'dead',
            'search': 'passback',
            'search.single': 'passback',
            'search.double': 'passback',
            'search.repeat': 'passback',
            'search.hold': 'passback',
            'search.hold_release': 'passback',
            'stop': 'passback',
            'stop.single': 'passback',
            'stop.double': 'passback',
            'stop.repeat': 'passback',
            'stop.hold': 'passback',
            'stop.hold_release': 'passback',
            'pause': 'passback',
            'pause.single': 'passback',
            'pause.double': 'passback',
            'pause.repeat': 'passback',
            'pause.hold': 'passback',
            'pause.hold_release': 'passback',
        },
        'INPUT.Bar': {
            'knob_left': 'down',
            'knob_left.repeat': 'down',
            'knob_right': 'up',
            'knob_right.repeat': 'up',
            'arrow_left': 'exit_left',
            'arrow_right': 'exit_right',
            'knob_push': 'exit_right',
            'play': 'passback',
            'play.single': 'passback',
            'play.double': 'passback',
            'play.repeat': 'passback',
            'play.hold': 'passback',
            'play.hold_release': 'passback',
            'add': 'passback',
            'add.single': 'passback',
            'add.double': 'passback',
            'add.repeat': 'passback',
            'add.hold': 'passback',
            'add.hold_release': 'passback',
            'search': 'passback',
            'search.single': 'passback',
            'search.double': 'passback',
            'search.repeat': 'passback',
            'search.hold': 'passback',
            'search.hold_release': 'passback',
            'stop': 'passback',
            'stop.single': 'passback',
            'stop.double': 'passback',
            'stop.repeat': 'passback',
            'stop.hold': 'passback',
            'stop.hold_release': 'passback',
            'pause': 'passback',
            'pause.single': 'passback',
            'pause.double': 'passback',
            'pause.repeat': 'passback',
            'pause.hold': 'passback',
            'pause.hold_release': 'passback',
            'fwd': 'passback',
            'fwd.single': 'passback',
            'fwd.double': 'passback',
            'fwd.repeat': 'passback',
            'fwd.hold': 'passback',
            'fwd.hold_release': 'passback',
            'rew': 'passback',
            'rew.single': 'passback',
            'rew.double': 'passback',
            'rew.repeat': 'passback',
            'rew.hold': 'passback',
            'rew.hold_release': 'passback',
        },
        'INPUT.Volume': {
            'knob_left': 'down',
            'knob_left.repeat': 'down',
            'knob_right': 'up',
            'knob_right.repeat': 'up',
            'arrow_up': 'up',
            'arrow_down': 'down',
            'arrow_up.repeat': 'up',
            'arrow_down.repeat': 'down',
            'volup': 'up',
            'volup.repeat': 'up',
            'voldown': 'down',
            'voldown.repeat': 'down',
            'volup_front': 'up',
            'volup_front.repeat': 'up',
            'voldown_front': 'down',
            'voldown_front.repeat': 'down',
            'arrow_left': 'exit_left',
            'arrow_right': 'exit',
            'knob_push': 'exit',
            'play': 'exit_passback',
            'play.single': 'exit_passback',
            'play.double': 'exit_passback',
            'play.repeat': 'exit_passback',
            'play.hold': 'exit_passback',
            'play.hold_release': 'exit_passback',
            'add': 'exit_passback',
            'add.single': 'exit_passback',
            'add.double': 'exit_passback',
            'add.repeat': 'exit_passback',
            'add.hold': 'exit_passback',
            'add.hold_release': 'exit_passback',
            'search': 'exit_passback',
            'search.single': 'exit_passback',
            'search.double': 'exit_passback',
            'search.repeat': 'exit_passback',
            'search.hold': 'exit_passback',
            'search.hold_release': 'exit_passback',
            'stop': 'exit_passback',
            'stop.single': 'exit_passback',
            'stop.double': 'exit_passback',
            'stop.repeat': 'exit_passback',
            'stop.hold': 'exit_passback',
            'stop.hold_release': 'exit_passback',
            'pause': 'exit_passback',
            'pause.single': 'exit_passback',
            'pause.double': 'exit_passback',
            'pause.repeat': 'exit_passback',
            'pause.hold': 'exit_passback',
            'pause.hold_release': 'exit_passback',
            'fwd': 'exit_passback',
            'fwd.single': 'exit_passback',
            'fwd.double': 'exit_passback',
            'fwd.repeat': 'exit_passback',
            'fwd.hold': 'exit_passback',
            'fwd.hold_release': 'exit_passback',
            'rew': 'exit_passback',
            'rew.single': 'exit_passback',
            'rew.double': 'exit_passback',
            'rew.repeat': 'exit_passback',
            'rew.hold': 'exit_passback',
            'rew.hold_release': 'exit_passback',
        },
    },
}

# Perl's default map for every player class: Player.pm:41
# 'irmap' => Slim::Hardware::IR::defaultMapFile(), IR.pm:212-220.
DEFAULT_MAP_FILE = "Default.map"

# Whether the class has a front panel (only affects the setup page + which
# .ir set is *offered*, IR.pm:188): Client.pm:668 0, Boom.pm:212,
# Transporter.pm:362. Not used for the code lookup (see module docstring).
FRONT_PANEL_CLASSES = frozenset({"boom", "transporter"})


# ---------------------------------------------------------------------------
# Lookup — Slim/Hardware/IR.pm + Slim/Buttons/Common.pm
# ---------------------------------------------------------------------------

# Keys of Slim::Buttons::Common %functions (Common.pm:265-1336) plus the
# mode-specific subs that the IR maps actually reference. Used to replicate
# Common.pm:1338-1360 getFunction's "X_Y -> sub X + arg Y" split.
FUNCTION_SUBS = frozenset({
    "dead", "fwd", "rew", "jump", "jumpinsong", "pause", "stop", "menu_pop",
    "menu", "brightness", "playdisp", "visual", "search", "globalsearch",
    "browse", "favorites", "playPreset", "preset", "repeat", "volumemode",
    "volume", "pitch", "bass", "treble", "muting", "snooze", "sleep",
    "power", "shuffle", "titleFormat", "datetime", "textsize",
    "clearPlaylist", "modefunction", "changeMap", "home", "zap",
    # mode modules referenced by Default.map / Front_Panel.ir
    "numberScroll", "done", "create_mix",
})
# NOTE: 'play' is deliberately NOT in this set. Common.pm %functions has no
# 'play' sub (each mode defines its own: Power.pm:33, Playlist.pm:216,
# Home.pm:124); 'play_0'/'play_1' (browsedb/INPUT.Choice) are UI "play the
# browsed entry N" and must NOT be folded onto the plain play action here.


def split_function(name: str) -> tuple[str, str | None]:
    """``X_Y`` -> ``(X, Y)`` when ``X`` is a known sub. Common.pm:1338-1360.

    The regex is non-greedy (``(.+?)_(.+)``), so the FIRST underscore splits.
    """
    if name in FUNCTION_SUBS:
        return name, None
    head, sep, rest = name.partition("_")
    if sep and head in FUNCTION_SUBS:
        return head, rest
    return name, None


def lookup_button(code: str, disabled: tuple[str, ...] = (),
                  sets: dict[str, dict[str, str]] | None = None) -> tuple[str | None, str | None]:
    """Hex code -> (ir-set file, button name). IR.pm:435-455 lookupCodeBytes.

    ``disabled`` is Perl's per-player ``disabledirsets`` pref (IR.pm:400-410).
    """
    if code is None:
        return None, None
    code = code.upper()
    table = IR_CODE_SETS if sets is None else sets
    for fname, mapping in table.items():
        if fname in disabled:
            continue
        if code in mapping:
            return fname, mapping[code]
    return None, None


def lookup_function(button: str, mode: str | None = None,
                    hold: bool = False) -> str | None:
    """Button name -> function name. IR.pm:491-527 lookupFunction.

    Search order is ``(mode, 'common')`` (IR.pm:500); ``hold=True`` appends
    the ``.hold`` press style (IR.pm:43, the hold timer at IR.pm:879-905),
    otherwise the plain key and then ``.single`` (the single-press release
    timer, IR.pm:531-560, :764-778) are tried.
    """
    modes = [m for m in (mode, "common") if m]
    candidates = [button + ".hold"] if hold else [button, button + ".single"]
    for entry in modes:
        section = BUTTON_FUNCTIONS.get(DEFAULT_MAP_FILE, {}).get(entry)
        if not section:
            continue
        for cand in candidates:
            if cand in section:
                return section[cand]
    return None


# ---------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------

async def _act_dead(pm, player, button, arg) -> None:
    """Common.pm:266 ``'dead' => sub {}`` — the map's explicit no-op."""
    return None


async def _act_pause(pm, player, button, arg) -> None:
    """Common.pm:369-383: never a toggle command — pick the wanted mode."""
    if not player.playlist and player.current_track_id is None and not player.current_url:
        return                                    # :373-377 empty playlist
    want_pause = player.mode != "pause"           # :380
    await pm.pause_player(player.mac, want_pause)


async def _act_stop(pm, player, button, arg) -> None:
    """Common.pm:385-408: execute(['stop'])."""
    await pm.stop_player(player.mac)


async def _act_fwd(pm, player, button, arg) -> None:
    """Common.pm:268-275 'fwd' -> ``playlist jump +1`` (which is ``skip``)."""
    await pm.playlist_jump(player.mac, "+1")


async def _act_rew(pm, player, button, arg) -> None:
    """Common.pm:277-294 'rew': less than a second since the last IR press steps
    back one track, otherwise the current track restarts."""
    await pm.playlist_jump(player.mac, "-1")


async def _act_jump(pm, player, button, arg) -> None:
    """Common.pm:296-337 'jump' with arg 'rew'|'fwd'|else (restart +0).

    Perl does not move indices itself here — every branch executes a
    ``playlist jump`` command and lets ``playlistJumpCommand``
    (Commands.pm:921-1036) do the arithmetic:

    * ``rew`` (:310-325): a song time below five seconds OR a stopped player
      steps back one (``-1``), anything else restarts the current track (``+0``)
    * ``fwd`` (:327-331): ``+1``
    * anything else (:332-336): ``+0``
    """
    if arg == "rew":
        if float(getattr(player, "elapsed", 0.0) or 0.0) < 5 \
                or getattr(player, "mode", "stop") == "stop":
            await pm.playlist_jump(player.mac, "-1")
        else:
            await pm.playlist_jump(player.mac, "+0")
    elif arg == "fwd":
        await pm.playlist_jump(player.mac, "+1")
    else:
        await pm.playlist_jump(player.mac, "+0")


async def _act_play(pm, player, button, arg) -> None:
    """Power.pm:33-38 (off mode): power 1 + play; Playlist.pm:216 resume."""
    if not player.power:
        pm.set_power(player.mac, True)
    if player.mode == "pause":
        await pm.pause_player(player.mac, False)
    elif player.current_track_id is not None:
        await pm.play_track(player.mac, player.current_track_id)
    elif getattr(player, "current_url", None):
        await pm.play_url(player.mac, str(player.current_url),
                          getattr(player, "current_title", "") or "")


async def _act_power(pm, player, button, arg) -> None:
    """Common.pm:1115-1130: power_on -> 1, power_off -> 0, else toggle."""
    if arg == "on":
        pm.set_power(player.mac, True)
    elif arg == "off":
        pm.set_power(player.mac, False)
    else:
        pm.set_power(player.mac, not player.power)


async def _act_volume(pm, player, button, arg) -> None:
    """Common.pm:969-982 pushes the volume mode; the value itself is changed
    through ``mixer volume`` (Volume.pm:89-104: onChange ->
    Settings::executeCommand, command 'mixer', subcommand 'volume',
    increment 1 at :99). The button name carries the direction."""
    if not player.use_volume_control:             # :974 hasVolumeControl
        return
    direction = -1 if "down" in button else 1 if "up" in button else 0
    if not direction:
        return
    await pm.set_volume(player.mac, player.volume + direction)


async def _act_muting(pm, player, button, arg) -> None:
    """Common.pm:1008-1015: mixer muting <!current mute>."""
    player.mute = not player.mute
    await pm.set_volume(player.mac, 0 if player.mute else player.volume)


async def _act_sleep(pm, player, button, arg) -> None:
    """Common.pm:1035-1113: pick the next choice of (0,15,30,45,60,90)
    minutes (:1060) and execute(['sleep', minutes*60]) (:1095)."""
    choices = [0, 15, 30, 45, 60, 90]
    current = int(getattr(player, "sleep_remaining", 0) or 0) // 60
    nxt = 0
    for choice in choices:
        if choice > current:
            nxt = choice
            break
    player.sleep_remaining = nxt * 60


async def _act_snooze(pm, player, button, arg) -> None:
    """Common.pm:1017-1033: an active alarm snoozes, else the datetime UI.
    No alarm object is modelled here -> only the sleep-timer branch."""
    if getattr(player, "sleep_remaining", 0):
        await _act_sleep(pm, player, "sleep", None)


async def _act_repeat(pm, player, button, arg) -> None:
    """Common.pm:915-947: arg 0/1/2 sets it, else toggle -> playlist repeat."""
    if arg in ("0", "1", "2"):
        player.repeat = int(arg)
    else:
        player.repeat = (int(player.repeat) + 1) % 3


async def _act_shuffle(pm, player, button, arg) -> None:
    """Common.pm:1132-1166: shuffle_on -> 1, shuffle_off -> 0, else toggle."""
    if arg == "on":
        player.shuffle = 1
    elif arg == "off":
        player.shuffle = 0
    else:
        player.shuffle = 0 if player.shuffle else 1


# function sub-name -> coroutine. Only the server-side effects are wired; the
# display/UI-mode subs (menu, browse, playdisp, brightness, textsize, visual,
# pitch/bass/treble, titleFormat, datetime, clearPlaylist, changeMap,
# modefunction, numberScroll, done, create_mix, search, home, zap, ...) have
# no counterpart in this port yet -> UNKLAR, they are logged and skipped.
ACTIONS = {
    "dead": _act_dead,
    "pause": _act_pause,
    "stop": _act_stop,
    "fwd": _act_fwd,
    "rew": _act_rew,
    "jump": _act_jump,
    "play": _act_play,
    "power": _act_power,
    "volume": _act_volume,
    "muting": _act_muting,
    "sleep": _act_sleep,
    "snooze": _act_snooze,
    "repeat": _act_repeat,
    "shuffle": _act_shuffle,
}


@dataclass
class ButtonAction:
    """Outcome of ``handle_button`` (all fields Perl-grounded)."""
    mac: str
    code: str
    kind: str
    ir_set: str | None = None
    button: str | None = None
    function: str | None = None
    sub: str | None = None
    arg: str | None = None
    front_panel: bool = False  # code carried a .down/.up suffix (IR.pm:798-877)
    action: str = "none"        # "executed" | "no-op" | "unknown-code" | "not-implemented"


async def _execute(mac: str, code: str, kind: str, hold: bool,
                   disabled: tuple[str, ...], manager, mode: str | None) -> ButtonAction:
    res = ButtonAction(mac=mac, code=code, kind=kind)
    ir_set, button = lookup_button(code, disabled)
    res.ir_set, res.button = ir_set, button
    if button is None:
        # IR.pm:644-651 — unknown code: notify 'unknownir' and stop.
        logger.info("Unknown %s code %s for %s (Perl: 'unknownir')", kind, code, mac)
        res.action = "unknown-code"
        return res
    # Front-panel keys are stored with a `.down`/`.up` suffix (Front_Panel.ir);
    # processIR strips it (:691-704) and processFrontPanel looks the BASE name
    # up (:825 for 'down', :836-841 for the .hold timer, :869 for .single).
    base, sep, fp_dir = button.partition(".")
    if sep and fp_dir in ("up", "down", "repeat"):
        res.front_panel = True
        lookup_name = base
    else:
        lookup_name = button
    function = lookup_function(lookup_name, mode=mode, hold=hold)
    res.function = function
    if function is None:
        res.action = "no-op"
        return res
    await _run_function(res, function, button, manager)
    return res


async def _run_function(res: ButtonAction, function: str, button: str,
                        manager) -> ButtonAction:
    """``Common::getFunction`` + call (``Common.pm:1338-1360``, IR.pm:1084-1104).

    ``getFunction`` returns the exact sub for a function name, else splits
    ``X_Y`` into the sub ``X`` with argument ``Y`` (:1349-1354 for the mode's
    functions, :1356-1358 for the shared ``%functions``). The FUNCTION name —
    not the button name — is what the sub receives as its second argument
    (IR.pm:1084, :1104).
    """
    sub, arg = split_function(function)
    res.function, res.sub, res.arg = function, sub, arg
    handler = ACTIONS.get(sub)
    if handler is None:
        # IR.pm:1106-1112 — Perl only warns here, the socket stays open.
        logger.info("Button %r with irCode %r not implemented in mode (Perl "
                    "Slim/Buttons/Common.pm:1106-1112)", button, function)
        res.action = "not-implemented"
        return res
    if manager is None:
        from lyrion.player.manager import PlayerManager
        manager = PlayerManager()
    player = manager.get_player(res.mac) if res.mac else None
    if player is None:
        res.action = "no-op"
        return res
    await handler(manager, player, button, arg)
    res.action = "executed"
    return res


async def execute_named_button(button: str, mac: str | None = None, *,
                               manager=None, mode: str | None = None,
                               hold: bool = False) -> ButtonAction:
    """``button <code>`` -> ``IR::executeButton`` — Commands.pm:263-291.

    The wire paths (``IR  ``/``BUTN``/``KNOB``) carry a hex code that is first
    resolved to a button NAME (:func:`handle_button`); a ``button`` command
    (CLI, JSON-RPC, cometd) already carries the name and Perl hands it straight
    to ``executeButton`` (``Commands.pm:288``) with ``_orFunction`` defaulting
    to 1. That flag makes a name the function when the map has no entry for it
    (``IR.pm:1061-1064``) — which is why ``button jump_fwd`` from the
    controllers runs ``%functions{'jump'}`` with the argument ``fwd``
    (``Common.pm:296-337``) and not a map entry for a ``jump_fwd`` button.
    """
    res = ButtonAction(mac=mac or "", code=button, kind="command")
    function = lookup_function(button, mode=mode, hold=hold)
    if function is None:
        function = button                            # IR.pm:1061-1064
    await _run_function(res, function, button, manager)
    return res


async def handle_button(mac: str, code: str, kind: str = "ir", hold: bool = False,
                        disabled: tuple[str, ...] = (), manager=None,
                        mode: str | None = None) -> ButtonAction:
    """Translate one IR/button code into a player action.

    ``kind`` is ``"ir"`` (``IR  `` opcode) or ``"butn"`` (``BUTN`` opcode);
    both carry the same 8-hex code and share this path (Slimproto.pm:541 vs
    :1277 -> the same ``IR::enqueue``). ``code`` is the hex string from
    :func:`parse_ir_frame` / :func:`parse_butn_frame`; an ``int`` is accepted
    for convenience. ``hold=True`` selects the ``.hold`` press style.
    ``mode`` is the Perl mode stack entry for the lookup (IR.pm:491-527);
    ``None`` searches ``[common]`` only (no mode stack in this port).
    ``disabled`` is the per-player ``disabledirsets`` pref (Player.pm:40,
    default empty; IR.pm:400-410).
    """
    if isinstance(code, int):
        code = f"{code & 0xFFFFFFFF:08X}"
    return await _execute(mac, str(code).upper(), kind, hold, disabled, manager,
                          mode)


async def handle_knob(mac: str, position: int, sync: int = 0,
                      manager=None) -> ButtonAction:
    """``KNOB`` frame -> ``executeButton($client, 'knob', ...)``.

    Slimproto.pm:1315 runs the button named ``knob``; the knob's direction
    drives ``up``/``down`` only inside a screensaver/volume mode
    (Default.map ``[screensaver] knob_left = volume``). No such mode stack
    exists here => UNKLAR, the frame is logged and the ``knoa`` ack
    (Slimproto.pm:1317) is left to the protocol layer.
    """
    res = ButtonAction(mac=mac, code=str(position), kind="knob")
    function = lookup_function("knob")
    res.button, res.function = "knob", function
    if function is None:
        res.action = "not-implemented"
        logger.debug("KNOB %s pos=%s sync=%s: 'knob' unmapped in mode 'common'",
                     mac, position, sync)
    else:
        sub, arg = split_function(function)
        res.sub, res.arg, res.action = sub, arg, "no-op"
    return res


# Documented gaps — reported instead of guessed.
UNKLAR = (
    "No per-player-class button/IR table exists in the Perl tree: every "
    "class shares IR/Default.map (Player.pm:41) and all *.ir code sets "
    "(IR.pm:400-410). Per-class differences are limited to the front-panel "
    "offering in the setup page (IR.pm:188, Client.pm:668, Boom.pm:212, "
    "Transporter.pm:362).",
    "The mode stack is not modelled: lookups only use [common] (plus an "
    "explicit mode argument). Modes like [off], [playlist], [browsedb], "
    "[screensaver], [INPUT.List] are in the table but not entered by the "
    "playback path (Buttons/Common.pm:243-262 setMode/pushMode).",
    "Hold/repeat/acceleration timers (IR.pm:530-1044: checkRelease, fireHold, "
    "holdCode, repeatCode, accelCount, IRHOLDTIME 0.9 s, IRMINTIME 0.140 s) "
    "are reduced to the hold=True flag; no repeat/acceleration state machine.",
    "Display/UI-mode functions (menu*, browse, search, globalsearch, playdisp, "
    "brightness, textsize, visual, titleFormat, volumemode, pitch/bass/treble, "
    "datetime, clearPlaylist, changeMap, modefunction, numberScroll, done, "
    "create_mix, home, zap, INPUT.* Text/Choice/List/TIME handlers) have no "
    "counterpart and are logged as not implemented.",
    "Presets (Common.pm:880-914 preset, :825-879 playPreset) and favorites "
    "(:688-824) need a preset/favorite store that this port does not have yet.",
    "favorites_add via favorites_hold, and snooze's alarm branch "
    "(Common.pm:1027-1032, Slim::Utils::Alarm) are not modelled.",
    "KNOB: only the 'knob' button name is resolved; the position/velocity/"
    "acceleration kinematics (IR.pm:706-740) and the knoa ack are not "
    "implemented.",
)
