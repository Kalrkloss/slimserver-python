"""``/meta/handshake`` advice — the key set was audited twice; it matches.

Parity-Audit D1 claimed the handshake advice of this port was a **superset**
of Perl's.  Raw comparison of both live servers (read-only, no writes on the
Perl side, 2026-09-14) refutes that: the key sets are identical.

Perl 9.1.1 (``Slim/Web/Cometd.pm:246-263``) builds the handshake answer as
``{id, channel, version, supportedConnectionTypes, clientId, successful,
advice}`` with

    my $advice = {
        reconnect => 'retry',                # :249
        interval  => LONG_POLLING_INTERVAL,  # :250
        timeout   => LONG_POLLING_TIMEOUT,   # :251
    };

Live probe (``POST /cometd`` with one ``/meta/handshake`` message, identical
bytes to both servers):

    PERL: {"advice":{"timeout":60000,"interval":0,"reconnect":"retry"},
           "supportedConnectionTypes":["long-polling","streaming"],"id":"1",
           "clientId":"b705bf35","version":"1.0","channel":"/meta/handshake",
           "successful":true}
    OURS: {"channel":"/meta/handshake","id":"1","successful":true,
           "version":"1.0","clientId":"lyrion-6",
           "supportedConnectionTypes":["long-polling","streaming"],
           "advice":{"reconnect":"retry","interval":0,"timeout":60000}}

Same seven top-level keys, same three advice keys, same values
(``LONG_POLLING_TIMEOUT`` = 60000, ``LONG_POLLING_INTERVAL`` = 0, Cometd.pm:47).
The *connect* advice (``:271-279``) is the one that carries exactly
``{interval}`` — that is where the earlier audit's "superset" reading came
from; it is already correct (``connect_advice``).
"""

import asyncio

from lyrion.web.cometd import CometdManager

PERL_HANDSHAKE_KEYS = {"id", "channel", "version", "supportedConnectionTypes",
                       "clientId", "successful", "advice"}
PERL_ADVICE_KEYS = {"reconnect", "interval", "timeout"}


class _StubRPC:
    async def handle_request(self, body: bytes) -> bytes:  # pragma: no cover
        return b"{}"


def test_handshake_advice_key_set_matches_perl_live():
    async def run():
        mgr = CometdManager(_StubRPC())
        return await mgr.handle_messages([{
            "channel": "/meta/handshake", "id": "1", "version": "1.0",
            "minimumVersion": "0.9",
            "supportedConnectionTypes": ["long-polling", "streaming"],
        }])

    reply = asyncio.run(run())[0]
    assert set(reply) == PERL_HANDSHAKE_KEYS, sorted(reply)
    assert set(reply["advice"]) == PERL_ADVICE_KEYS, reply["advice"]
    assert reply["advice"] == {"reconnect": "retry", "interval": 0,
                               "timeout": 60000}
    # Perl has NO timestamp in a handshake answer (time2str is only used for
    # /meta/(re)connect :276 and /meta/disconnect :338)
    assert "timestamp" not in reply
