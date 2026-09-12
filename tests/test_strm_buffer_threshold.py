"""`strm` bufferThreshold parity with the Perl LMS.

The player starts decoding once its input buffer reaches this value (KB),
so guessing it (the old default was 50) directly changes the time between a
tap and audible playback. Perl locations:

* Slim/Player/Player.pm:65 — client pref default `bufferThreshold` = 255 KB
* Slim/Player/Squeezebox.pm:916-921 — a file smaller than the threshold is
  reduced to (filesize/1024)-1 KB
* Slim/Player/Squeezebox.pm:160-179 — remote streams: 20 KB, or
  int(bitrate/8) * bufferSecs / 1000 with bufferSecs default 3 (:162),
  everything capped at 255 by the byte-sized frame field
"""

from lyrion.networking.protocol import (
    BUFFER_THRESHOLD_KB,
    REMOTE_BUFFER_THRESHOLD_KB,
    stream_buffer_threshold,
)


def test_local_file_uses_the_perl_pref_default():
    assert BUFFER_THRESHOLD_KB == 255                  # Player.pm:65
    assert stream_buffer_threshold(5_363_570) == 255


def test_small_local_file_is_reduced_to_its_own_size():
    # Squeezebox.pm:918-921 → (filesize/1024)-1, never below 1
    assert stream_buffer_threshold(128 * 1024) == 127
    assert stream_buffer_threshold(1024) == 1
    assert stream_buffer_threshold(512) == 1           # (0 → 2) - 1 = 1


def test_remote_stream_default_and_bitrate_rule():
    assert REMOTE_BUFFER_THRESHOLD_KB == 20
    assert stream_buffer_threshold(remote=True) == 20
    assert stream_buffer_threshold(remote=True, bitrate_bps=128_000) == 48
    assert stream_buffer_threshold(remote=True, bitrate_bps=2_000_000) == 255
    # unknown bitrate keeps the 20 KB default
    assert stream_buffer_threshold(remote=True, bitrate_bps=None) == 20
    assert stream_buffer_threshold(remote=True, bitrate_bps=0) == 20


def test_value_always_fits_the_byte_sized_frame_field():
    assert stream_buffer_threshold() == 255
    assert 1 <= stream_buffer_threshold(1, remote=True, bitrate_bps=1) <= 255
    assert stream_buffer_threshold(10**9) == 255
