#!/usr/bin/env python3
"""Jive discovery test against the Python LMS (port from argv, default 3484)."""
import socket
import sys

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 3484

pkt = b"e" + b"IPAD\x00NAME\x00JSON\x00VERS\x00UUID\x00" + \
      b"JVID" + bytes([6, 0x12, 0x34, 0x56, 0x78, 0x12, 0x34])

s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
s.settimeout(3)
s.sendto(pkt, ("255.255.255.255", PORT))
try:
    data, addr = s.recvfrom(2048)
except socket.timeout:
    print("NO RESPONSE")
    sys.exit(1)

print(f"from {addr}: {data!r}")
ptr = 1
fields = {}
while ptr + 5 <= len(data) - 0:
    t = data[ptr:ptr+4]
    l = data[ptr+4]
    v = data[ptr+5:ptr+5+l]
    fields[t.decode(errors="replace")] = v.decode(errors="replace")
    ptr += 5 + l
print("parsed:", fields)
name, ip, port = fields.get("NAME"), fields.get("IPAD"), fields.get("JSON")
if name and ip and port:
    print(f"SqueezePlay would register server: {name} at {ip}:{port}")
