"""Stream iterators, offline: iter:WebSocket on a local WebSocket server, iter:MQTTSubscribe on a
simulated MQTT broker; the stream is read up to a number of messages or a duration, each
message a batch of its own; without a bound the query is refused."""
import base64
import hashlib
import socket
import struct
import threading
import time

import pytest

import sparql_generate as sg

P = "PREFIX iter: <http://w3id.org/sparql-generate/iter/>\nPREFIX fun: <http://w3id.org/sparql-generate/fn/>\n"


def serve(handler):
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen()

    def loop():
        while True:
            try:
                conn, _ = srv.accept()
            except OSError:
                return
            threading.Thread(target=handler, args=(conn,), daemon=True).start()

    threading.Thread(target=loop, daemon=True).start()
    return srv, srv.getsockname()[1]


def ws_handler(conn):
    """Answers the handshake, reads the first message, then sends three text frames (the third
    in two fragments), a ping, and stays open."""
    data = b""
    while b"\r\n\r\n" not in data:
        data += conn.recv(4096)
    key = [l.split(b":", 1)[1].strip() for l in data.split(b"\r\n") if l.lower().startswith(b"sec-websocket-key")][0]
    acc = base64.b64encode(hashlib.sha1(key + b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11").digest())
    conn.sendall(b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                 b"Sec-WebSocket-Accept: " + acc + b"\r\n\r\n")
    conn.settimeout(0.5)
    first = b"none"
    try:
        h = conn.recv(2)
        n = h[1] & 0x7F
        mask = conn.recv(4)
        first = bytes(b ^ mask[i % 4] for i, b in enumerate(conn.recv(n)))
    except OSError:
        pass
    for i in range(2):
        payload = ('{"n": %d, "query": "%s"}' % (i, first.decode())).encode()
        conn.sendall(bytes([0x81, len(payload)]) + payload)
    conn.sendall(bytes([0x01, 3]) + b"par" + bytes([0x89, 0]) + bytes([0x80, 2]) + b"ts")
    time.sleep(5)
    conn.close()


def mqtt_handler(conn):
    """CONNACK, SUBACK, then one PUBLISH per subscribed topic (QoS 0) and one with QoS 1."""
    def read_packet():
        head = conn.recv(1)[0]
        n, mult = 0, 1
        while True:
            d = conn.recv(1)[0]
            n += (d & 0x7F) * mult
            if not d & 0x80:
                break
            mult *= 128
        body = b""
        while len(body) < n:
            body += conn.recv(n - len(body))
        return head, body

    read_packet()  # CONNECT
    conn.sendall(bytes([0x20, 2, 0, 0]))
    head, body = read_packet()  # SUBSCRIBE
    pid, i, topics = body[:2], 2, []
    while i < len(body):
        tl = struct.unpack("!H", body[i:i + 2])[0]
        topics.append(body[i + 2:i + 2 + tl].decode())
        i += 3 + tl
    conn.sendall(bytes([0x90, 2 + len(topics)]) + pid + bytes([0] * len(topics)))
    for t in topics:
        tb = t.encode()
        payload = b"21.5"
        conn.sendall(bytes([0x30, 2 + len(tb) + len(payload)]) + struct.pack("!H", len(tb)) + tb + payload)
    tb, payload = b"room/qos1", b'{"v": 1}'
    conn.sendall(bytes([0x32, 4 + len(tb) + len(payload)]) + struct.pack("!H", len(tb)) + tb + b"\x00\x07" + payload)
    time.sleep(5)
    conn.close()


@pytest.fixture(scope="module")
def ws():
    srv, port = serve(ws_handler)
    yield f"ws://127.0.0.1:{port}/feed"
    srv.close()


@pytest.fixture(scope="module")
def mqtt():
    srv, port = serve(mqtt_handler)
    yield f"tcp://127.0.0.1:{port}"
    srv.close()


def test_websocket_bounded_by_messages_and_by_time(ws):
    q = P + f'SELECT ?m (fun:JSONPath(?m, "$.n") AS ?n) ITERATOR iter:WebSocket(<{ws}>, "subscribe") AS ?m WHERE {{}}'
    rows = sg.select(q, stream_messages=2)
    assert [r["n"].split("^^")[0] for r in rows] == ['"0"', '"1"'] and '\\"query\\": \\"subscribe\\"' in rows[0]["m"]
    # by time: the server sends three messages (the last in two fragments, with a ping in
    # between), then stays silent; the stream is read for one second
    t = time.monotonic()
    rows = sg.select(P + f'SELECT ?m ITERATOR iter:WebSocket(<{ws}>) AS ?m WHERE {{}}', stream_seconds=1)
    assert len(rows) == 3 and rows[2]["m"] == '"parts"' and time.monotonic() - t < 3
    # each message is a batch: an aggregate per message
    rows = sg.select(P + f'SELECT (COUNT(*) AS ?c) ITERATOR iter:WebSocket(<{ws}>) AS ?m WHERE {{}}', stream_messages=3)
    assert len(rows) == 3
    with pytest.raises(sg.CompileError):  # an endless stream
        sg.Execution(P + f'SELECT ?m ITERATOR iter:WebSocket(<{ws}>) AS ?m WHERE {{}}')


def test_mqtt_subscribe(mqtt):
    q = P + f'SELECT ?t ?m ITERATOR iter:MQTTSubscribe(<{mqtt}>, "room/1", "room/2") AS ?t ?m WHERE {{}}'
    rows = sg.select(q, stream_messages=3)
    assert [(r["t"], r["m"]) for r in rows] == [('"room/1"', '"21.5"'), ('"room/2"', '"21.5"'), ('"room/qos1"', '"{\\"v\\": 1}"')]
    rows = sg.select(q, stream_seconds=1)
    assert len(rows) == 3
    with pytest.raises(sg.CompileError):
        sg.Execution(q)
