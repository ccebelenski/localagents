"""Story 2.3 item 3: the node state from a host's endpoints' LAST probes (monitor spec §4
"Per node"), the node's collapse to one line, and the port-22 fallback connector. The pure
truth table never touches the network; probe_port is tested over real 127.0.0.1 sockets and
a non-routable address — no network beyond the kernel's own refusal. The port-22 connector
is the real ``open_connection`` in production and a faked one (monkeypatched) in the
deterministic timeout test."""
import asyncio
import socket

import httpx

from localagents.monitor import state
from localagents.monitor.collect import probe_port
from localagents.registry import classify_connect_failure

_DOWN = {"up": False, "host": "down"}
_UNKNOWN = {"up": False, "host": "unknown"}
_REFUSED = {"up": False, "host": "up"}  # a refusal proves the box is up; the endpoint is down


def test_a_live_endpoint_makes_the_node_up():
    """[up] -> up."""
    assert state.node_state([{"up": True, "host": "up"}]) == "up"


def test_a_refusal_proves_the_box_is_up():
    """[refused] -> up: an unreachable endpoint on a box that answers is 'endpoint down',
    shown on that line only — the node itself is up."""
    assert state.node_state([_REFUSED]) == "up"


def test_a_read_timeout_node_is_up_without_the_fallback():
    """Ruling C (review 2.3 probe 2): classify_connect_failure calls a ReadTimeout host
    ``up`` (the connect answered), so the any-host-up rule fires on the single endpoint —
    the stalled box no longer pays a port-22 connect every tick to re-prove it."""
    out = classify_connect_failure(httpx.ReadTimeout("timed out"), "http://ai2:8080")
    assert state.node_state([{"up": False, "host": out["host"]}]) == "up"


def test_every_endpoint_unreachable_is_down():
    """[down, down] -> down: every endpoint reports the box unreachable."""
    assert state.node_state([_DOWN, _DOWN]) == "down"


def test_all_timeouts_with_an_answered_fallback_is_up():
    """[unknown, unknown], fallback 'answered' (port 22 connects) -> up."""
    assert state.node_state([_UNKNOWN, _UNKNOWN], "answered") == "up"


def test_all_timeouts_with_a_refused_fallback_is_up():
    """[unknown, unknown], fallback 'refused' (port 22 refuses) -> up."""
    assert state.node_state([_UNKNOWN, _UNKNOWN], "refused") == "up"


def test_all_timeouts_with_a_nothing_fallback_is_down():
    """[unknown, unknown], fallback 'nothing' (port 22 silent) -> down."""
    assert state.node_state([_UNKNOWN, _UNKNOWN], "nothing") == "down"


def test_all_timeouts_without_a_fallback_is_unknown():
    """[unknown, unknown], fallback None -> unknown: a timeout is ambiguous (box off, or a
    firewall dropping packets)."""
    assert state.node_state([_UNKNOWN, _UNKNOWN]) == "unknown"


def test_a_mixed_down_and_unknown_node_is_unknown():
    """[down, unknown] -> unknown: one endpoint timed out and another is unreachable —
    nothing proves the box is down, and an unproven down is an alert."""
    assert state.node_state([_DOWN, _UNKNOWN]) == "unknown"


def test_refused_and_down_is_up():
    """[refused, down] -> up: the refusal already proved the box is up."""
    assert state.node_state([_REFUSED, _DOWN]) == "up"


def test_a_host_with_no_endpoints_is_unknown():
    """[] -> unknown."""
    assert state.node_state([]) == "unknown"


def test_every_endpoint_dormant_collapses_the_node():
    """node_dormant: every endpoint dormant — the node collapses to one line."""
    assert state.node_dormant(["dormant", "dormant"]) is True


def test_one_live_endpoint_keeps_the_node_expanded():
    assert state.node_dormant(["live", "dormant"]) is False


def test_one_down_recent_endpoint_keeps_the_node_expanded():
    assert state.node_dormant(["down", "dormant"]) is False


def test_no_endpoint_states_is_vacuously_dormant():
    """Every (zero) endpoint is dormant: a host with no endpoints has nothing to expand."""
    assert state.node_dormant([]) is True


async def _handler(reader, writer):
    writer.close()


def test_a_listening_port_answers():
    """A real server on 127.0.0.1 (ephemeral port, asyncio.start_server) -> answered."""
    async def go():
        server = await asyncio.start_server(_handler, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        try:
            return await probe_port("127.0.0.1", port)
        finally:
            server.close()
            await server.wait_closed()
    assert asyncio.run(go()) == "answered"


def test_a_closed_port_on_localhost_refuses():
    """A port we bound and closed on 127.0.0.1: the kernel's own refusal -> refused."""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    assert asyncio.run(probe_port("127.0.0.1", port)) == "refused"


def test_a_non_routable_address_yields_nothing():
    """10.255.255.1 with a 0.1 s window: no route (the kernel's EHOSTUNREACH) or a dropped
    SYN (the timeout) — neither needs any network beyond the kernel's own refusal."""
    assert asyncio.run(probe_port("10.255.255.1", 22, timeout_s=0.1)) == "nothing"


def test_a_reset_from_the_connector_is_refused(monkeypatch):
    """Ruling D (review 2.3): a TCP reset is an ANSWER — something on the box rejected the
    connection — so it is refused (the box is up), not nothing (which would say down)."""
    async def reset(*args, **kwargs):
        raise ConnectionResetError("Connection reset by peer")
    monkeypatch.setattr("asyncio.open_connection", reset)
    assert asyncio.run(probe_port("anywhere", 22, timeout_s=0.5)) == "refused"


def test_a_timeout_from_the_connector_is_nothing(monkeypatch):
    """The timeout branch pinned deterministically: the connector never answers."""
    async def never(*args, **kwargs):
        await asyncio.Event().wait()

    monkeypatch.setattr("asyncio.open_connection", never)
    assert asyncio.run(probe_port("10.255.255.1", 22, timeout_s=0.1)) == "nothing"
