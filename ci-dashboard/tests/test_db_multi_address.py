"""Exercise real SQLAlchemy/PyMySQL connection creation without network or wall-clock sleeps."""
import io
import socket
import ssl
import time
from unittest.mock import Mock

import pymysql
import pytest

from ci_dashboard.common import db
from ci_dashboard.common.config import load_settings


def test_two_tcp_timeouts_leave_time_to_reach_mysql_on_retry(monkeypatch):
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

    class Socket:
        def __init__(self, *args):
            self.closed = False

        def settimeout(self, seconds):
            self.timeout = seconds

        def setsockopt(self, *args):
            pass

        def connect(self, address):
            calls.append((address[0], self))
            if len(calls) <= 2:
                clock[0] += self.timeout
                raise TimeoutError("timed out")

        def makefile(self, mode):
            # A genuine MySQL error packet before authentication: the peer is now
            # reachable. PyMySQL must surface 1045 without another network attempt.
            error = b"\xff\x15\x04#28000Access denied"
            return io.BytesIO(len(error).to_bytes(3, "little") + b"\0" + error)

        def close(self):
            self.closed = True

    monkeypatch.setattr(socket, "socket", Socket)
    try:
        with pytest.raises(pymysql.OperationalError) as error:
            engine.pool._creator(None)
        assert error.value.args[0] == 1045
        assert [ip for ip, _ in calls] == [*ips, ips[0]]
        assert 2.5 <= clock[0] < 5
        assert all(sock.closed for _, sock in calls)
    finally:
        engine.dispose()


def test_greeting_eof_retries_next_address_without_changing_tls_hostname(monkeypatch, tmp_path):
    # Real driver authentication up to the TLS wrap; stub only the peer's reply
    # and the TLS transport itself. No network, DB credentials or CA required.
    engine = db.build_engine(load_settings({
        "CI_DASHBOARD_DB_URL": f"mysql+pymysql://ci@db.example/dashboard?ssl_capath={tmp_path}",
    }), retry_connect_timeout=True)
    ips = ("192.0.2.1", "192.0.2.2")
    resolves = Mock(return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 3306)) for ip in ips])
    monkeypatch.setattr(socket, "getaddrinfo", resolves)
    monkeypatch.setattr(time, "sleep", lambda _: None)
    sockets = []
    sni = []

    def packet(seq, data):
        return len(data).to_bytes(3, "little") + bytes([seq]) + data

    class Peer:
        def __init__(self, *_):
            self.closed = False
            sockets.append(self)

        def connect(self, peer):
            self.ip = peer[0]

        def settimeout(self, timeout):
            pass

        def setsockopt(self, *args):
            pass

        def makefile(self, mode):
            if self.ip == ips[0]:
                return io.BytesIO()  # TCP succeeded; peer closed before greeting.
            if not sni:
                greeting = (b"\x0a8.0.0\0" + bytes(4) + b"12345678\0"
                            + b"\x00\x88\x21" + bytes(16) + b"abcdefghijkl\0")
                return io.BytesIO(packet(0, greeting))
            ok = b"\0" * 7
            return io.BytesIO(packet(3, ok) + packet(1, ok) + packet(1, ok))

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
        assert [sock.ip for sock in sockets] == list(ips)
        assert resolves.call_count == 1
        assert sockets[0].closed and not sockets[1].closed
        assert conn.host == "db.example" and sni == [conn.host]
        assert (conn._read_timeout, conn._write_timeout) == (35, 30)
        conn.close()
        assert sockets[1].closed
    finally:
        engine.dispose()
