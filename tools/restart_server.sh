#!/usr/bin/env bash
# Server sauber neu starten: wartet, bis der alte Prozess die Ports wirklich
# freigegeben hat, startet dann EINEN neuen und prüft die vier Ports.
#
# Lehre aus zwei Fehlversuchen: `kill -TERM` braucht bei großer Bibliothek
# deutlich länger als ein paar Sekunden (Bibliothek wird entladen), und ein zu
# früher Neustart führt zu zwei Instanzen, von denen eine 9090/3483 nicht binden
# kann (OSError 98) — dann ist der CLI-Listener tot.
set -uo pipefail

cd /home/keiner/Downloads/git/slimserver-python || exit 1
# Single-port layout (Perl parity): 9000 is the PUBLIC port served by the
# native frontend, 9001 the internal ASGI app behind it (loopback), 9090 the
# CLI listener and 3483 SlimProto/discovery. 9080 no longer exists.
PORTS=(9000 9001 9090 3483)
LOG=/tmp/lyrion-live.log

port_count() {
    ss -tln 2>/dev/null | grep -cE ":($(IFS='|'; echo "${PORTS[*]}"))" || true
}

server_pids() {
    pgrep -f "[l]yrion --loglevel" || true
}

echo "vorher: Ports=$(port_count) PIDs=$(server_pids | tr '\n' ' ')"

for pid in $(server_pids); do
    kill -TERM "$pid" 2>/dev/null && echo "SIGTERM an $pid"
done

# auf echte Freigabe warten (max 90 s)
for i in $(seq 1 90); do
    if [ "$(port_count)" = "0" ] && [ -z "$(server_pids)" ]; then
        echo "alte Instanz beendet nach ${i}s"
        break
    fi
    sleep 1
done

if [ "$(port_count)" != "0" ] || [ -n "$(server_pids)" ]; then
    # Beobachtet: der Server ignoriert SIGTERM und hält die Ports (Shutdown-Pfad
    # blockiert). Ohne hartes Nachfassen ist danach kein sauberer Neustart
    # möglich — und ein zweiter Start führt zum Doppelbetrieb mit toten
    # Listenern. Deshalb nach der Gnadenfrist SIGKILL auf die Reste.
    left="$(server_pids)"
    echo "WARNUNG: gibt nach ${i}s nicht frei (Ports=$(port_count) PIDs=$left) — SIGKILL"
    for pid in $left; do kill -KILL "$pid" 2>/dev/null; done
    sleep 3
    if [ "$(port_count)" != "0" ] || [ -n "$(server_pids)" ]; then
        echo "FEHLER: Reste halten weiter (Ports=$(port_count) PIDs=$(server_pids)) — kein Neustart"
        exit 2
    fi
    echo "Reste hart beendet"
fi

setsid nohup .venv/bin/python3 -m lyrion --loglevel debug > "$LOG" 2>&1 &
echo "gestartet, warte auf die vier Ports …"

for i in $(seq 1 90); do
    if [ "$(port_count)" = "4" ]; then
        echo "bereit nach ${i}s: Ports=$(port_count) PIDs=$(server_pids | tr '\n' ' ')"
        count=$(server_pids | wc -l)
        [ "$count" = "1" ] || { echo "FEHLER: $count Serverprozesse!"; exit 3; }
        grep -ac 'address already in use' "$LOG" | sed 's/^/Bindefehler im Log: /'
        exit 0
    fi
    sleep 1
done

echo "FEHLER: nur $(port_count) Ports offen"
exit 4
