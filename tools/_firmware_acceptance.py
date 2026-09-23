"""Live-Abnahme Auftrag 2: Firmware-Route + Download/SHA1/Negativ/Tod.

Läuft gegen die LAUFENDE Instanz (Port 9000) und gegen lokale simulierte
Update-Server (je ein eigener freier Port).  Kein Zugriff auf Perl-LMS.

Aufruf:  .venv/bin/python3 tools/_firmware_acceptance.py
"""
from __future__ import annotations

import hashlib
import http.client
import http.server
import logging
import os
import shutil
import socket
import struct
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

CACHE = Path(os.environ.get("LYRION_SERVERDATA",
                            str(Path.home() / ".lyrion" / "Lyrion"))) / "Cache"
UPDATES = CACHE / "updates"
WEB_PORT = 9000
SLIM_PORT = 3483


# --- simulierter Update-Server (eigener freier Port je Aufruf) --------------

class _Handler(http.server.BaseHTTPRequestHandler):
    files: dict[str, bytes] = {}

    def do_GET(self):  # noqa: N802
        data = self.files.get(self.path)
        if data is None:
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b"nope")
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt, *args):  # keep the output clean
        pass


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _start_fake_server(files: dict[str, bytes]) -> int:
    _Handler.files = files
    srv = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv.server_address[1]


# --- (a) Route liefert echte Bytes + Content-Type: binary -------------------

def _helo_register_model(model: str) -> socket.socket | None:
    """HELO an den laufenden Server schicken; das registriert das Modell.

    Wire-Format wie ``tests/conftest.py:_register_player`` (deviceid + revision
    + mac + uuid + wlan + brH + brL + lang + caps).  Der Socket bleibt offen —
    der Server entfernt einen Player, sobald seine letzte Verbindung schließt.
    """
    caps = f"Model={model},ModelName=AcceptancePlayer".encode()
    mac_bytes = bytes.fromhex("00000000009a")
    body = (
        struct.pack(">I", 36 + len(caps))
        + bytes([4, 0])
        + mac_bytes
        + b"\x00" * 16
        + struct.pack(">H", 0)
        + struct.pack(">I", 0)
        + struct.pack(">I", 0)
        + b"en"
        + caps
    )
    frame = b"HELO" + body
    for _ in range(20):
        try:
            s = socket.create_connection(("127.0.0.1", SLIM_PORT), timeout=2)
            s.sendall(frame)
            return s
        except OSError:
            time.sleep(0.2)
    return None


def check_route_serves_bytes() -> str:
    payload = b"\x00\x01LIVE-FIRMWARE-BYTES\xff" * 4

    UPDATES.mkdir(parents=True, exist_ok=True)
    # Ein <model>.version + <model>_<ver>_r<rev>.bin im updates-Verzeichnis
    # (Perl get_fw_locally) — genau diese Datei muss die Route liefern.
    (UPDATES / "jive.version").write_text("8.5.0 r16560\nlive\n")
    image = UPDATES / "jive_8.5.0_r16560.bin"
    image.write_bytes(payload)

    # Der laufende Dienst lernt das Modell beim HELO (Perl: ``Firmware.pm:193``
    # prüft danach periodisch) und findet die Datei über ``get_fw_locally``.
    keep = _helo_register_model("jive")

    resp = body = None
    ctype = clen = None
    for _ in range(40):
        conn = http.client.HTTPConnection("127.0.0.1", WEB_PORT, timeout=10)
        conn.request("GET", "/firmware/jive_8.5.0_r16560.bin")
        resp = conn.getresponse()
        body = resp.read()
        ctype = resp.getheader("Content-Type")
        clen = resp.getheader("Content-Length")
        conn.close()
        if resp.status == 200:
            break
        time.sleep(0.5)
    if keep is not None:
        keep.close()
    return (f"(a) status={resp.status} Content-Type={ctype!r} "
            f"Content-Length={clen} bytes={len(body)} "
            f"sha1={hashlib.sha1(body).hexdigest()[:12]} "
            f"match={body == payload}")


# --- (b) <model>.version laden + SHA1 prüfen --------------------------------

def check_download_and_sha() -> str:
    from lyrion.utils import firmware as fw

    version_body = b"8.5.0 r16560\nlive acceptance\n"
    payload = b"LIVE-DOWNLOADED-FIRMWARE"
    port = _start_fake_server({
        "/9.2.0/jive.version": version_body,
        "/9.2.0/jive.version.sha": hashlib.sha1(version_body).hexdigest().encode(),
        "/9.2.0/jive_8.5.0_r16560.bin": payload,
        "/9.2.0/jive_8.5.0_r16560.bin.sha": hashlib.sha1(payload).hexdigest().encode(),
    })

    tmp = UPDATES / "acceptance"
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True, exist_ok=True)
    dl = fw.FirmwareDownloader(tmp, base_url=f"http://127.0.0.1:{port}/",
                               base_folder="9.2.0")
    body = dl.download_version_file("jive")
    parsed = fw.parse_version_file(body.decode()) if body else None
    target = tmp / "jive_8.5.0_r16560.bin"
    res = dl.download(target)
    log = tmp / "acceptance.log"
    log.write_text(f"version_body={body!r}\nparsed={parsed}\n"
                   f"bin_status={res.status} sha1={res.sha1}\n"
                   f"stored={target.exists()}\n")
    return (f"(b) version_geladen={body is not None} parsed={parsed} "
            f"bin_status={res.status} "
            f"sha1_match={res.sha1 == hashlib.sha1(payload).hexdigest()} "
            f"datei={target.exists()} log={log}")


# --- (c) Negativ: falscher SHA -> Datei wird NICHT übernommen ----------------

def check_bad_sha() -> str:
    from lyrion.utils import firmware as fw

    payload = b"TAMPERED-PAYLOAD"
    port = _start_fake_server({
        "/9.2.0/jive_8.5.0_r16560.bin": payload,
        "/9.2.0/jive_8.5.0_r16560.bin.sha": (b"0" * 40),
    })
    tmp = UPDATES / "acceptance-neg"
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True, exist_ok=True)
    # Eine GUTE Datei vorlegen — sie darf nicht überschrieben werden.
    target = tmp / "jive_8.5.0_r16560.bin"
    target.write_bytes(b"GOOD-OLD")
    dl = fw.FirmwareDownloader(tmp, base_url=f"http://127.0.0.1:{port}/",
                               base_folder="9.2.0")
    res = dl.download(target)
    return (f"(c) status={res.status} detail={res.detail!r} "
            f"inhalt_unveraendert={target.read_bytes() == b'GOOD-OLD'} "
            f"tmp_reste={len(list(tmp.glob('*.tmp')))} backoff={dl.check_time}")


# --- (d) tote Update-Quelle bricht nichts -----------------------------------

def check_dead_source() -> str:
    from lyrion.utils import firmware as fw

    tmp = UPDATES / "acceptance-dead"
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True, exist_ok=True)
    dead_port = _free_port()  # nicht gebunden -> Connection refused
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    dl = fw.FirmwareDownloader(tmp, base_url=f"http://127.0.0.1:{dead_port}/",
                               base_folder="9.2.0")
    res = dl.download(tmp / "jive.version")
    return (f"(d) status={res.status} detail={res.detail!r} "
            f"datei_existiert={(tmp / 'jive.version').exists()} "
            f"backoff={dl.check_time} (kein Absturz)")


if __name__ == "__main__":
    for fn in (check_route_serves_bytes, check_download_and_sha,
               check_bad_sha, check_dead_source):
        try:
            print(fn())
        except Exception as exc:  # noqa: BLE001
            print(f"{fn.__name__}: FEHLER {type(exc).__name__}: {exc}")
