"""Exercise real SQLAlchemy/PyMySQL connection creation without network or wall-clock sleeps."""
import io
import socket
import ssl
import time
from unittest.mock import Mock, call

import pymysql
import pytest

from ci_dashboard.common import db
from ci_dashboard.common.config import load_settings


def packet(seq, data):
    return len(data).to_bytes(3, "little") + bytes([seq]) + data


def greeting(*, tls):
    capabilities = 0x8000 | (0x0800 if tls else 0)  # SECURE_CONNECTION | SSL
    return (b"\x0a8.0.0\0" + bytes(4) + b"12345678\0" + capabilities.to_bytes(2, "little")
            + b"\x21" + bytes(16) + b"abcdefghijkl\0")


OK = b"\0" * 7


def access_denied_packet():
    return io.BytesIO(packet(0, b"\xff\x15\x04#28000Access denied"))


@pytest.mark.parametrize("reachable_on_retry", [True, False])
def test_tcp_timeouts_retry_within_budget(monkeypatch, reachable_on_retry):
    # One attempt previously spent the full 5s: socket.create_connection applied
    # the requested 2.5s timeout independently to *each* DNS answer.
    settings = load_settings({"CI_DASHBOARD_DB_URL": "mysql+pymysql://ci@db.example/dashboard"})
    engine = db.build_engine(settings, retry_connect_timeout=True)
    clock = [0.0]
    calls = []
    ips = ("192.0.2.1", "192.0.2.2")
    dns = Mock(return_value=[(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (ip, 3306))
                             for ip in ips])
    monkeypatch.setattr(socket, "getaddrinfo", dns)
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(time, "sleep", lambda sec: clock.__setitem__(0, clock[0] + sec))
    monkeypatch.setattr(db.random, "uniform", lambda *_: 0.1)

    class Socket:
        def __init__(self, *args):
            self.closed = False

        def settimeout(self, seconds):
            self.timeout = seconds

        def setsockopt(self, *args):
            pass

        def connect(self, address):
            calls.append((address[0], self))
            if len(calls) <= 2 or not reachable_on_retry:
                clock[0] += self.timeout
                raise TimeoutError("timed out")

        def makefile(self, mode):
            # On recovery the real driver must surface 1045 without retrying it.
            return access_denied_packet()

        def close(self):
            self.closed = True

    monkeypatch.setattr(socket, "socket", Socket)
    try:
        with pytest.raises(pymysql.OperationalError) as error:
            engine.pool._creator(None)
        assert error.value.args[0] == (1045 if reachable_on_retry else 2003)
        assert [ip for ip, _ in calls] == [*ips, ips[0]] + ([] if reachable_on_retry else [ips[1]])
        assert [sock.timeout for _, sock in calls[:2]] == [1.25, 1.25]
        assert clock[0] == pytest.approx(2.6 if reachable_on_retry else 5)
        dns.assert_called_once_with("db.example", 3306, type=socket.SOCK_STREAM)
        assert all(sock.closed for _, sock in calls)
    finally:
        engine.dispose()


@pytest.mark.parametrize("first_dial_times_out", [False, True])
def test_greeting_eof_retries_without_changing_tls_hostname(monkeypatch, tmp_path, first_dial_times_out):
    # Real driver authentication up to the TLS wrap; stub only the peer's reply
    # and the TLS transport itself. No network, DB credentials or CA required.
    engine = db.build_engine(load_settings({
        "CI_DASHBOARD_DB_URL": f"mysql+pymysql://ci@db.example/dashboard?ssl_capath={tmp_path}",
    }), retry_connect_timeout=True)
    ips = ("192.0.2.1", "192.0.2.2")
    resolves = Mock(return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 3306)) for ip in ips])
    monkeypatch.setattr(socket, "getaddrinfo", resolves)
    clock = [0.0]
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(time, "sleep", lambda sec: clock.__setitem__(0, clock[0] + sec))
    sockets = []
    sni = []

    class Peer:
        def __init__(self, *_):
            self.closed = False
            sockets.append(self)

        def connect(self, peer):
            self.ip = peer[0]
            if first_dial_times_out and self.ip == ips[0]:
                clock[0] += self.timeout
                raise TimeoutError("timed out")

        def settimeout(self, timeout):
            self.timeout = timeout

        def setsockopt(self, *args):
            pass

        def makefile(self, mode):
            if len(sockets) == (2 if first_dial_times_out else 1):
                return io.BytesIO()  # TCP succeeded; peer closed before greeting.
            if not sni:
                return io.BytesIO(packet(0, greeting(tls=True)))
            return io.BytesIO(packet(3, OK) + packet(1, OK) + packet(1, OK))

        def sendall(self, data):
            pass

        def close(self):
            self.closed = True

    def wrap(context, sock, *, server_hostname):
        sni.append(server_hostname)
        assert context.check_hostname
        return sock

    monkeypatch.setattr(socket, "socket", Peer)
    monkeypatch.setattr(ssl.SSLContext, "wrap_socket", wrap)
    try:
        conn = engine.pool._creator(None)
        assert [sock.ip for sock in sockets] == list(ips) + ([ips[1]] if first_dial_times_out else [])
        assert resolves.call_count == 1
        assert all(sock.closed for sock in sockets[:-1]) and not sockets[-1].closed
        assert conn.host == "db.example" and sni == [conn.host]
        assert (conn._read_timeout, conn._write_timeout) == (35, 30)
        conn.close()
        assert sockets[-1].closed
    finally:
        engine.dispose()


def test_ipv6_fallback_binds_source_and_can_use_time_left_after_fast_refusal(monkeypatch):
    engine = db.build_engine(load_settings({
        "CI_DASHBOARD_DB_URL": "mysql+pymysql://ci@db.example/dashboard?bind_address=::",
    }), retry_connect_timeout=True)
    ipv4 = ("192.0.2.1", 3306)
    ipv6 = ("2001:db8::1", 3306, 0, 0)
    tcp = socket.SOCK_STREAM, socket.IPPROTO_TCP
    monkeypatch.setattr(socket, "getaddrinfo", Mock(return_value=[
        (socket.AF_INET, *tcp, "", ipv4),
        (socket.AF_INET6, *tcp, "", ipv6),
    ]))
    clock = [0.0]
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    failed, slow = Mock(), Mock()
    failed.connect.side_effect = ConnectionRefusedError
    slow.makefile.return_value = access_denied_packet()
    make_socket = Mock(side_effect=[failed, slow])
    monkeypatch.setattr(socket, "socket", make_socket)

    def slow_connect(_):
        assert slow.settimeout.call_args.args[0] >= 1.5
        clock[0] += 1.5

    slow.connect.side_effect = slow_connect
    try:
        with pytest.raises(pymysql.OperationalError) as error:
            engine.pool._creator(None)
        assert error.value.args[0] == 1045
        assert make_socket.call_args_list == [call(socket.AF_INET, *tcp), call(socket.AF_INET6, *tcp)]
        failed.connect.assert_called_once_with(ipv4)
        slow.assert_has_calls([call.bind(("::", 0)), call.connect(ipv6)])
        failed.close.assert_called_once()
        slow.close.assert_called_once()
    finally:
        engine.dispose()


def test_unix_socket_is_opened_by_the_driver_without_dns(monkeypatch):
    path = "/path/to/mysql.sock"
    engine = db.build_engine(load_settings({
        "CI_DASHBOARD_DB_URL": f"mysql+pymysql://ci@db.example/dashboard?unix_socket={path}",
    }), retry_connect_timeout=True)
    dns = Mock(side_effect=AssertionError("Unix sockets do not use TCP DNS"))
    sock = Mock()
    sock.makefile.return_value = io.BytesIO(
        packet(0, greeting(tls=False)) + packet(2, OK) + packet(1, OK) + packet(1, OK)
    )
    make_socket = Mock(return_value=sock)
    monkeypatch.setattr(socket, "getaddrinfo", dns)
    monkeypatch.setattr(socket, "socket", make_socket)
    try:
        conn = engine.pool._creator(None)
        make_socket.assert_called_once_with(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.connect.assert_called_once_with(path)
        dns.assert_not_called()
        assert (conn._read_timeout, conn._write_timeout) == (35, 30)
        conn.close()
        sock.close.assert_called_once()
    finally:
        engine.dispose()


@pytest.mark.parametrize("exhausted_during", ["resolution", "dial"])
def test_tcp_budget_exhaustion_does_not_start_next_step(monkeypatch, exhausted_during):
    engine = db.build_engine(load_settings({
        "CI_DASHBOARD_DB_URL": "mysql+pymysql://ci@db.example/dashboard",
        "CI_DASHBOARD_DB_CONNECT_TIMEOUT_SECONDS": "1",
    }), retry_connect_timeout=True)
    clock = [0.0]
    sock = Mock()
    make_socket = Mock(return_value=sock)

    def resolve(*args, **kwargs):
        if exhausted_during == "resolution":
            clock[0] = 1.0
        return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("192.0.2.1", 3306))]

    sock.connect.side_effect = lambda _: clock.__setitem__(0, 1.0)
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    monkeypatch.setattr(socket, "socket", make_socket)
    try:
        with pytest.raises(pymysql.OperationalError) as error:
            engine.pool._creator(None)
        assert error.value.args[0] == 2003
        if exhausted_during == "resolution":
            assert "budget exhausted" in str(error.value)
            make_socket.assert_not_called()
        else:
            assert "timed out before greeting" in str(error.value)
            sock.close.assert_called_once()
        sock.makefile.assert_not_called()
    finally:
        engine.dispose()
