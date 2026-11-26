"""HTTP and SQL, offline: fun:HTTPGet and the HTTP extraction functions, iter:HTTPGet on a local
HTTP server (a JSON API, the main use of the demonstration server), iter:SQL on SQLite and
DuckDB files."""
import json
import sqlite3
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

import sparql_generate as sg

P = ("PREFIX iter: <http://w3id.org/sparql-generate/iter/>\nPREFIX fun: <http://w3id.org/sparql-generate/fn/>\n"
     "PREFIX ex: <http://ex.org/>\n")


class Handler(BaseHTTPRequestHandler):
    calls = 0

    def do_GET(self):
        Handler.calls += 1
        if self.path.startswith("/papers"):
            body = json.dumps({"data": [{"title": "A", "year": 2017}, {"title": "B", "year": 2020}],
                               "accept": self.headers.get("Accept"), "call": Handler.calls}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
        else:
            body = b"not found"
            self.send_response(404)
            self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(n)[::-1]
        self.send_response(201)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


@pytest.fixture(scope="module")
def server():
    httpd = HTTPServer(("127.0.0.1", 0), Handler)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()


def objs(q, **kw):
    return [o for s, p, o in sorted(sg.generate(P + q, **kw))]


def test_fun_http_get_a_json_api(server):
    q = f'''GENERATE {{ ex:p ex:title ?t ; ex:code ?c ; ex:accept ?a }}
    WHERE {{ BIND(fun:HTTPGet(<{server}/papers>, "Accept: application/json") AS ?r)
            BIND(fun:HTTPExtractBody(?r) AS ?b) BIND(fun:HTTPExtractResponseCode(?r) AS ?c)
            BIND(fun:JSONPath(?b, "$.data[1].title") AS ?t) BIND(fun:JSONPath(?b, "$.accept") AS ?a) }}'''
    assert objs(q) == ['"application/json"', '"200"^^<http://www.w3.org/2001/XMLSchema#integer>', '"B"']
    # the whole response: status line, headers, empty line, body
    (r,) = [row["r"] for row in sg.select(P + f'SELECT ?r WHERE {{ BIND(fun:HTTPGet(<{server}/nothing>) AS ?r) }}')]
    text = json.loads(r)
    assert text.split("\n")[0].endswith(" 404 Not Found") and "\nContent-Type: text/plain\n" in text
    assert text.endswith("\n\nnot found")
    (h,) = [row["h"] for row in sg.select(P + f'SELECT ?h WHERE {{ BIND(fun:HTTPExtractHeader(fun:HTTPGet(<{server}/nothing>)) AS ?h) }}')]
    assert "Content-Type: text/plain" in json.loads(h) and "not found" not in json.loads(h)
    # POST with a body
    (r,) = [row["r"] for row in sg.select(P + f'SELECT ?r WHERE {{ BIND(fun:HTTPExtractBody(fun:HTTPPost(<{server}/x>, "", "abc")) AS ?r) }}')]
    assert r == '"cba"'


def test_iter_http_get_each_response_is_a_batch(server):
    # two calls, one second apart; then the JSON of each response is iterated
    q = f'''SELECT ?call (COUNT(?title) AS ?n)
    ITERATOR iter:HTTPGet(<{server}/papers>, 1, 2) AS ?doc
    ITERATOR iter:JSONPath(?doc, "$.data[*]", "$.title") AS ?e ?title
    BIND(fun:JSONPath(?doc, "$.call") AS ?call)
    WHERE {{ }} GROUP BY ?call'''
    rows = sg.select(P + q)
    assert [r["n"].split("^^")[0] for r in rows] == ['"2"', '"2"'] and len({r["call"] for r in rows}) == 2
    (d,) = [r["dt"] for r in sg.select(P + f'SELECT (DATATYPE(?doc) AS ?dt) ITERATOR iter:HTTPGet(<{server}/papers>, 1, 1) AS ?doc WHERE {{}}')]
    assert d == "<https://www.iana.org/assignments/media-types/application/json>"
    with pytest.raises(sg.CompileError):  # a stream that never ends
        sg.Execution(P + f'SELECT ?d ITERATOR iter:HTTPGet(<{server}/papers>, 1) AS ?d WHERE {{}}')


def test_no_network(server):
    with sg.Execution(P + f'GENERATE {{ ex:s ex:p ?r }} WHERE {{ BIND(fun:HTTPGet(<{server}/papers>) AS ?r) }}',
                      allow_network=False) as ex:
        assert list(ex.triples()) == [] and any("network" in w for w in ex.warnings)


@pytest.mark.parametrize("kind", ["sqlite", "duckdb"])
def test_iter_sql(tmp_path, kind):
    path = tmp_path / f"db.{kind}"
    rows = [(1, "Ada", 1.5, True, "1815-12-10"), (2, "Alan", None, False, "1912-06-23")]
    if kind == "sqlite":
        con = sqlite3.connect(path)
        con.execute("CREATE TABLE p (id INTEGER, name TEXT, score REAL, ok BOOLEAN, born DATE)")
    else:
        import duckdb

        con = duckdb.connect(str(path))
        con.execute("CREATE TABLE p (id INTEGER, name VARCHAR, score DOUBLE, ok BOOLEAN, born DATE)")
    con.executemany("INSERT INTO p VALUES (?, ?, ?, ?, ?)", rows)
    con.commit()
    con.close()
    q = (f'SELECT ?id ?name ?score ?ok ITERATOR iter:SQL(<jdbc:{kind}:{path}>, "SELECT id, name, score, ok FROM p ORDER BY id") '
         'AS ?id ?name ?score ?ok WHERE {} ORDER BY ?id')
    got = sg.select(P + q)
    X = "^^<http://www.w3.org/2001/XMLSchema#"
    assert got[0] == {"id": f'"1"{X}integer>', "name": '"Ada"', "score": f'"1.5E0"{X}double>', "ok": (
        f'"true"{X}boolean>' if kind == "duckdb" else f'"1"{X}integer>')}
    assert got[1]["score"] is None
    if kind == "duckdb":
        (b,) = [r["b"] for r in sg.select(P + f'SELECT ?b ITERATOR iter:SQL(<jdbc:duckdb:{path}>, "SELECT born FROM p WHERE id = 1") AS ?b WHERE {{}}')]
        assert b == f'"1815-12-10"{X}date>'
    # a query that the database rejects: an error that stops the execution (the Java engine 2.6.1)
    with pytest.raises(sg.RaiseError, match="^SQL error: "):
        sg.select(P + f'SELECT ?x ITERATOR iter:SQL(<jdbc:{kind}:{path}>, "SELECT nope FROM p") AS ?x WHERE {{}}')
