"""Stream iterators: ``iter:WebSocket(uri [, message])`` and ``iter:MQTTSubscribe(server,
topic...)``, as the Java engine defines them, bounded.

The Java iterators never end: each message is a batch (rule BATCH) of an endless stream.
pysparql-generate does not execute queries as streams, so it reads a bounded prefix of the
stream at compile time: at most ``stream_messages`` messages, for at most ``stream_seconds``
seconds (``Execution(stream_messages=..., stream_seconds=...)``, ``--stream-messages``,
``--stream-seconds``); without a bound the query is refused, as ``iter:HTTPGet`` without a
number of calls.  Each message is a batch of its own.

The clients are minimal and have no dependency: RFC 6455 (text and binary frames,
fragmentation, ping, close; ``wss://`` through TLS) and MQTT 3.1.1 (CONNECT, SUBSCRIBE with
QoS 0 and 1, PUBLISH, PUBACK; ``tcp://`` and ``ssl://``).
"""
from __future__ import annotations

import base64
import os
import socket
import ssl
import struct
import time
from typing import Iterator, List, Optional, Tuple
from urllib.parse import urlparse


class StreamError(Exception):
    pass


class _Reader:
    """Reads exact byte counts from a socket until a deadline."""

    def __init__(self, sock, deadline: Optional[float]):
        self.sock, self.deadline, self.buf = sock, deadline, b""

    def _fill(self) -> None:
        if self.deadline is not None:
            left = self.deadline - time.monotonic()
            if left <= 0:
                raise TimeoutError
            self.sock.settimeout(left)
        chunk = self.sock.recv(65536)
        if not chunk:
            raise EOFError
        self.buf += chunk

    def read(self, n: int) -> bytes:
        while len(self.buf) < n:
            self._fill()
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def read_until(self, marker: bytes) -> bytes:
        while marker not in self.buf:
            if len(self.buf) > 1 << 20:
                raise StreamError("handshake too long")
            self._fill()
        i = self.buf.index(marker) + len(marker)
        out, self.buf = self.buf[:i], self.buf[i:]
        return out


def _connect(host: str, port: int, secure: bool, deadline: Optional[float]):
    timeout = None if deadline is None else max(0.01, deadline - time.monotonic())
    sock = socket.create_connection((host, port), timeout=timeout or 30)
    if secure:
        sock = ssl.create_default_context().wrap_socket(sock, server_hostname=host)
    return sock


def _deadline(seconds: Optional[float]) -> Optional[float]:
    return None if seconds is None else time.monotonic() + seconds


# -- WebSocket (RFC 6455) ----------------------------------------------------------------------


def _ws_frame(opcode: int, payload: bytes) -> bytes:
    """A masked client frame."""
    head = bytes([0x80 | opcode])
    n = len(payload)
    if n < 126:
        head += bytes([0x80 | n])
    elif n < 1 << 16:
        head += bytes([0x80 | 126]) + struct.pack("!H", n)
    else:
        head += bytes([0x80 | 127]) + struct.pack("!Q", n)
    mask = os.urandom(4)
    return head + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(payload))


def websocket_messages(uri: str, first: Optional[str], max_messages: Optional[int],
                       max_seconds: Optional[float]) -> List[str]:
    u = urlparse(uri)
    if u.scheme not in ("ws", "wss"):
        raise StreamError(f"not a WebSocket URI: {uri}")
    secure = u.scheme == "wss"
    port = u.port or (443 if secure else 80)
    deadline = _deadline(max_seconds)
    out: List[str] = []
    sock = _connect(u.hostname, port, secure, deadline)
    try:
        key = base64.b64encode(os.urandom(16)).decode()
        path = (u.path or "/") + (f"?{u.query}" if u.query else "")
        sock.sendall((f"GET {path} HTTP/1.1\r\nHost: {u.hostname}:{port}\r\nUpgrade: websocket\r\n"
                      f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
        r = _Reader(sock, deadline)
        status = r.read_until(b"\r\n\r\n").split(b"\r\n", 1)[0]
        if b" 101" not in status:
            raise StreamError(f"WebSocket handshake refused: {status.decode(errors='replace')}")
        if first:
            sock.sendall(_ws_frame(1, first.encode("utf-8")))
        parts: List[bytes] = []
        while max_messages is None or len(out) < max_messages:
            try:
                b0, b1 = r.read(2)
                n = b1 & 0x7F
                if n == 126:
                    n = struct.unpack("!H", r.read(2))[0]
                elif n == 127:
                    n = struct.unpack("!Q", r.read(8))[0]
                mask = r.read(4) if b1 & 0x80 else None
                data = r.read(n)
            except (TimeoutError, socket.timeout, EOFError, OSError):
                break
            if mask:
                data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
            opcode = b0 & 0x0F
            if opcode == 8:  # close
                break
            if opcode == 9:  # ping
                sock.sendall(_ws_frame(10, data))
                continue
            if opcode in (0, 1, 2):
                parts.append(data)
                if b0 & 0x80:
                    out.append(b"".join(parts).decode("utf-8", errors="replace"))
                    parts = []
    finally:
        sock.close()
    return out


# -- MQTT 3.1.1 --------------------------------------------------------------------------------


def _mqtt_str(s: str) -> bytes:
    b = s.encode("utf-8")
    return struct.pack("!H", len(b)) + b


def _mqtt_packet(kind: int, body: bytes) -> bytes:
    n, rem = len(body), b""
    while True:
        d, n = n % 128, n // 128
        rem += bytes([d | (0x80 if n else 0)])
        if not n:
            break
    return bytes([kind]) + rem + body


def _mqtt_read(r: _Reader) -> Tuple[int, bytes]:
    head = r.read(1)[0]
    mult, n = 1, 0
    while True:
        d = r.read(1)[0]
        n += (d & 0x7F) * mult
        if not d & 0x80:
            break
        mult *= 128
    return head, r.read(n)


def mqtt_messages(server: str, topics: List[str], max_messages: Optional[int],
                  max_seconds: Optional[float]) -> List[Tuple[str, str]]:
    u = urlparse(server)
    if u.scheme not in ("tcp", "ssl", "mqtt", "mqtts"):
        raise StreamError(f"not an MQTT server: {server} (tcp:// or ssl://)")
    secure = u.scheme in ("ssl", "mqtts")
    deadline = _deadline(max_seconds)
    out: List[Tuple[str, str]] = []
    sock = _connect(u.hostname, u.port or (8883 if secure else 1883), secure, deadline)
    try:
        cid = "sg-" + base64.b32encode(os.urandom(5)).decode().lower()
        connect = _mqtt_str("MQTT") + bytes([4, 0x02]) + struct.pack("!H", 60) + _mqtt_str(cid)
        sock.sendall(_mqtt_packet(0x10, connect))
        r = _Reader(sock, deadline)
        head, body = _mqtt_read(r)
        if head >> 4 != 2 or len(body) < 2 or body[1] != 0:
            raise StreamError(f"MQTT connection refused by {server}")
        if not topics:
            return out
        sub = struct.pack("!H", 1) + b"".join(_mqtt_str(t) + bytes([1]) for t in topics)
        sock.sendall(_mqtt_packet(0x82, sub))
        while max_messages is None or len(out) < max_messages:
            try:
                head, body = _mqtt_read(r)
            except (TimeoutError, socket.timeout, EOFError, OSError):
                break
            kind = head >> 4
            if kind == 3:  # PUBLISH
                qos = (head >> 1) & 3
                tl = struct.unpack("!H", body[:2])[0]
                topic = body[2:2 + tl].decode("utf-8", errors="replace")
                i = 2 + tl
                if qos:
                    pid = body[i:i + 2]
                    i += 2
                    if qos == 1:
                        sock.sendall(_mqtt_packet(0x40, pid))
                out.append((topic, body[i:].decode("utf-8", errors="replace")))
        try:
            sock.sendall(bytes([0xE0, 0]))  # DISCONNECT
        except OSError:
            pass
    finally:
        sock.close()
    return out


# -- iterators ----------------------------------------------------------------------------------


def _bounds(ctx, name: str):
    from .iterators import IteratorError

    m, s = getattr(ctx.engine, "stream_messages", None), getattr(ctx.engine, "stream_seconds", None)
    if m is None and s is None:
        raise IteratorError(f"{name} is an endless stream: bound it with stream_messages or stream_seconds "
                            "(--stream-messages, --stream-seconds)")
    return m, s


def _const_str(e, what: str) -> str:
    from .iterators import IteratorError

    if not e.is_const or e.const is None:
        raise IteratorError(f"{what} must be a constant")
    return str(e.const)


def iter_websocket(ctx, args, nvars):
    from .iterators import EMPTY, IterPlan, IteratorError
    from .termtypes import STR_T

    if not 1 <= len(args) <= 2:
        raise IteratorError("iter:WebSocket needs a WebSocket URI and an optional first message")
    m, s = _bounds(ctx, "iter:WebSocket")
    if not ctx.engine.documents.allow_network:
        ctx.warnings.append("iter:WebSocket: the network is not allowed (--no-network)")
        return EMPTY
    uri = _const_str(args[0], "the WebSocket URI")
    first = _const_str(args[1], "the first message") if len(args) == 2 else None
    try:
        msgs = websocket_messages(uri, first, m, s)
    except (StreamError, OSError) as ex:
        ctx.warnings.append(f"iter:WebSocket <{uri}>: {ex}")
        msgs = []
    import pyarrow as pa

    tbl = pa.table({"c0": pa.array(msgs, type=pa.string())})
    return IterPlan("table", f"(SELECT * FROM {ctx.engine.register_arrow(tbl)})", [("c0", STR_T)], batch=1)


def iter_mqtt_subscribe(ctx, args, nvars):
    from .iterators import EMPTY, IterPlan, IteratorError
    from .termtypes import STR_T

    if not args:
        raise IteratorError("iter:MQTTSubscribe needs the MQTT server")
    m, s = _bounds(ctx, "iter:MQTTSubscribe")
    if not ctx.engine.documents.allow_network:
        ctx.warnings.append("iter:MQTTSubscribe: the network is not allowed (--no-network)")
        return EMPTY
    server = _const_str(args[0], "the MQTT server")
    topics = [_const_str(a, "a topic") for a in args[1:]]
    try:
        msgs = mqtt_messages(server, topics, m, s)
    except (StreamError, OSError) as ex:
        ctx.warnings.append(f"iter:MQTTSubscribe <{server}>: {ex}")
        msgs = []
    import pyarrow as pa

    tbl = pa.table({"c0": pa.array([t for t, _ in msgs], type=pa.string()),
                    "c1": pa.array([p for _, p in msgs], type=pa.string())})
    return IterPlan("table", f"(SELECT * FROM {ctx.engine.register_arrow(tbl)})", [("c0", STR_T), ("c1", STR_T)], batch=1)


def register_all():
    from . import ast as A
    from .iterators import ITERATORS

    ITERATORS[A.SG_ITER + "WebSocket"] = iter_websocket
    ITERATORS[A.SG_ITER + "MQTTSubscribe"] = iter_mqtt_subscribe
