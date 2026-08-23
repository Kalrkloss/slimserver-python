#!/usr/bin/env python3
"""Jive/SlimProto discovery responder for the Python-LMS test setup.

The real SqueezeCenter discovery port is UDP 3483. This tool binds 3483
(SO_REUSEADDR so the OS keeps delivering copies alongside other listeners)
and answers Jive 'e' TLV requests AND classic 'd' probes advertising the
local Python-LMS (JSON/Cometd port 9080, slimproto 3484).

Run alongside the test server:  python3 tools/jive-discovery-responder.py
Stop with Ctrl-C. Only responds to broadcast/multicast sources on the LAN.
"""
import socket
import struct
import sys


def tlv(tag: bytes, value: bytes) -> bytes:
    return tag + bytes([len(value)]) + value


def main() -> None:
    json_port = int(sys.argv[1]) if len(sys.argv) > 1 else 9080
    name = sys.argv[2] if len(sys.argv) > 2 else "Lyrion-Py"
    version = b"9.2.0"

    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
    except OSError:
        pass
    s.bind(("", 3483))
    print(f"listening on UDP 3483, advertising {name!r} JSON={json_port}")

    while True:
        data, addr = s.recvfrom(2048)
        if not data:
            continue
        kind = data[:1]
        if kind == b"e":
            # Jive TLV request -> answer with 'E' + requested TLVs
            values = {
                b"IPAD": socket.gethostbyname(socket.gethostname()).encode(),
                b"NAME": name.encode("iso-8859-1", errors="replace"),
                b"JSON": str(json_port).encode(),
                b"VERS": version,
                b"UUID": b"lyrion-python-test",
            }
            resp = bytearray(b"E")
            pos = 1
            while pos + 5 <= len(data):
                t = data[pos:pos + 4]
                l = data[pos + 4]
                pos += 5 + l
                if t in values:
                    resp += tlv(t, values[t])
            # core fields always present (Jive needs address+port)
            if b"IPAD" not in resp:
                resp += tlv(b"IPAD", values[b"IPAD"])
            if b"JSON" not in resp:
                resp += tlv(b"JSON", values[b"JSON"])
            if b"NAME" not in resp:
                resp += tlv(b"NAME", values[b"NAME"])
            s.sendto(bytes(resp), addr)
            print(f"E-reply -> {addr}")
        elif kind == b"d":
            # classic slimproto probe -> 'D' + 17-byte padded hostname
            host = name.encode("iso-8859-1", errors="replace")[:17]
            host = host.ljust(17, b"\x00")
            s.sendto(b"D" + host, addr)
            print(f"D-reply -> {addr}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("bye")
