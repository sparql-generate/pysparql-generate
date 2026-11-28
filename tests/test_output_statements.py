"""The output written statement by statement: the union of the SELECTs of the triple
patterns is cut into statements of at most compiler.OUTPUT_STATEMENT_CHARS characters of SQL,
run one after another (DuckDB plans a union of many levels that are not written to a table
in a time that grows with the square of their number: the queries that rml2sparql-generate
writes for gtfs-feeds have one such level per join). Every output is the same, whatever the
number of statements."""
import gzip
import io
import os
import subprocess
import threading
import time

import pytest
import rdflib
from rdflib.compare import isomorphic

import sparql_generate as sg
from sparql_generate import compiler

PREFIXES = "PREFIX iter: <http://w3id.org/sparql-generate/iter/> PREFIX ex: <http://ex.org/> "
N_PARENT, N_CHILD = 8, 30


def documents(feeds: int):
    docs = {}
    for f in range(feeds):
        docs[f"http://ex.org/P{f}.csv"] = "id,name\n" + "".join(f"p{j},n{j}\n" for j in range(N_PARENT))
        docs[f"http://ex.org/C{f}.csv"] = "id,parent\n" + "".join(f"c{j},p{j % N_PARENT}\n" for j in range(N_CHILD))
    return docs


DOCS = documents(12)


def feed(f: int) -> str:
    """The shape of a feed of gtfs-feeds translated from RML: a triples map of the parents, one
    of the children, and the join (R8: a sub-query whose FROM GENERATE gives the keys of the
    parents, a level with one output pattern, not written to a table)."""
    return f"""
  GENERATE {{ ?s ex:name ?n ; a ex:P . }}
  ITERATOR iter:CSV(<http://ex.org/P{f}.csv>, "id", "name") AS ?id ?n
  WHERE {{ BIND(IRI(CONCAT("http://ex.org/f{f}/p/", ?id)) AS ?s) }} .
  GENERATE {{ ?s a ex:C ; ex:id ?id . }}
  ITERATOR iter:CSV(<http://ex.org/C{f}.csv>, "id", "parent") AS ?id ?parent
  WHERE {{ BIND(IRI(CONCAT("http://ex.org/f{f}/c/", ?id)) AS ?s) }} .
  GENERATE {{ ?s ex:parent ?o . }}
  FROM GENERATE {{
    GENERATE {{ ?o ex:key ?k . }}
    ITERATOR iter:CSV(<http://ex.org/P{f}.csv>, "id") AS ?id
    WHERE {{ BIND(IRI(CONCAT("http://ex.org/f{f}/p/", ?id)) AS ?o) BIND(?id AS ?k) }} .
  }} .
  ITERATOR iter:CSV(<http://ex.org/C{f}.csv>, "id", "parent") AS ?id ?parent
  WHERE {{ BIND(IRI(CONCAT("http://ex.org/f{f}/c/", ?id)) AS ?s) ?o ex:key ?parent }} ."""


def query(feeds: int = 12) -> str:
    return PREFIXES + "GENERATE {" + "".join(feed(f) for f in range(feeds)) + "\n} WHERE { }"


TRIPLES = 12 * (2 * N_PARENT + 3 * N_CHILD)


def execution(q=None, **kw):
    return sg.Execution(q or query(), documents=DOCS, allow_network=False, **kw)


@pytest.fixture
def small(monkeypatch):
    """Statements of one SELECT each (the budget is smaller than any SELECT)."""
    monkeypatch.setattr(compiler, "OUTPUT_STATEMENT_CHARS", 1)


def reference() -> rdflib.Graph:
    """The output in one statement."""
    budget, compiler.OUTPUT_STATEMENT_CHARS = compiler.OUTPUT_STATEMENT_CHARS, 10 ** 9
    try:
        with execution() as ex:
            assert len(ex.plan.triples_statements()) == 1
            return ex.to_graph()
    finally:
        compiler.OUTPUT_STATEMENT_CHARS = budget


def parse_nt(text: str) -> rdflib.Graph:
    g = rdflib.Graph()
    g.parse(data=text, format="nt")
    return g


def test_statements_of_the_budget():
    parts = ["SELECT 1 AS x", "SELECT 2 AS x", "SELECT 3 AS x"]
    assert compiler.union_statements(parts, budget=1000) == ["\nUNION ALL\n".join(parts)]
    assert compiler.union_statements(parts, budget=1) == parts
    two = "\nUNION ALL\n".join(parts[:2])
    assert compiler.union_statements(parts, budget=len(two)) == [two, parts[2]]
    assert compiler.union_statements(parts, budget=len(two) - 1) == parts


def test_regular_file(small, tmp_path):
    ref = reference()
    out = tmp_path / "out.nt"
    out.write_text("an older and longer content\n" * 10000)
    with execution() as ex:
        assert len(ex.plan.triples_statements()) == len(ex.plan.triple_selects) > 30
        n = ex.write(str(out))
    assert n == TRIPLES == len(ref)
    assert isomorphic(parse_nt(out.read_text()), ref)
    assert os.listdir(tmp_path) == ["out.nt"]  # no part left behind


def test_compressed_file(small, tmp_path):
    ref = reference()
    out = tmp_path / "out.nt.gz"
    with execution() as ex:
        assert ex.write(str(out)) == TRIPLES
    assert isomorphic(parse_nt(gzip.open(out, "rt", encoding="utf-8").read()), ref)
    assert os.listdir(tmp_path) == ["out.nt.gz"]


def test_stream_and_api(small):
    ref = reference()
    buf = io.StringIO()
    with execution() as ex:
        assert ex.write(buf) == TRIPLES
        assert isomorphic(parse_nt(buf.getvalue()), ref)
        assert len(list(ex.triples())) == TRIPLES
        assert ex.arrow().num_rows == TRIPLES
        assert sum(b.num_rows for b in ex.batches()) == TRIPLES
        assert len(set(ex.triples(distinct=True))) == TRIPLES
        assert isomorphic(ex.to_graph(), ref)


def test_dev_null(small):
    with execution() as ex:
        assert ex.write(os.devnull) == TRIPLES


def test_fifo(small, tmp_path):
    """A FIFO is opened once: one end of file, after the last statement."""
    fifo = str(tmp_path / "fifo")
    os.mkfifo(fifo)
    got, done = [], []
    reader = threading.Thread(target=lambda: got.append(open(fifo, encoding="utf-8").read()), daemon=True)
    reader.start()

    def write():
        with execution() as ex:
            done.append(ex.write(fifo))

    writer = threading.Thread(target=write, daemon=True)
    writer.start()
    writer.join(60)
    reader.join(60)
    assert not writer.is_alive() and not reader.is_alive(), "the write to the FIFO hangs"
    assert done[0] == len(got[0].splitlines()) == TRIPLES


def test_quads(small, tmp_path):
    q = query(3).replace("GENERATE { ?s ex:parent ?o . }", "GENERATE { GRAPH ex:g { ?s ex:parent ?o . } }")
    with execution(q) as ex:
        ref = ex.to_dataset()
    out = tmp_path / "out.nq"
    with execution(q) as ex:
        assert len(ex.plan.quads_statements()) > 1
        n = ex.write(str(out), format="nq")
        assert len(list(ex.quads())) == n
    ds = rdflib.Dataset()
    ds.parse(data=out.read_text(), format="nquads")
    assert n == 3 * (2 * N_PARENT + 3 * N_CHILD) == len(ref)
    assert len(ds.graph(rdflib.URIRef("http://ex.org/g"))) == 3 * N_CHILD


def test_cli_output(small, tmp_path):
    """The command line, -o a regular file: the same triples as one statement gives."""
    for name, text in DOCS.items():
        (tmp_path / name.rsplit("/", 1)[1]).write_text(text)
    q = query(4).replace("http://ex.org/P", "P").replace("http://ex.org/C", "C")
    (tmp_path / "query.rqg").write_text("BASE <http://ex.org/>\n" + q)
    out = tmp_path / "out.nt"
    env = dict(os.environ)
    r = subprocess.run([os.sys.executable, "-c",
                        "import sys; from sparql_generate import compiler, cli; "
                        "compiler.OUTPUT_STATEMENT_CHARS = 1; sys.argv[1:] = " +
                        repr(["-d", str(tmp_path), "-q", "query.rqg", "-o", str(out)]) + "; cli.main()"],
                       capture_output=True, text=True, env=env)
    assert r.returncode == 0, r.stderr
    assert len(out.read_text().splitlines()) == 4 * (2 * N_PARENT + 3 * N_CHILD)


def test_many_joins_planned_in_linear_time():
    """120 feeds, 120 join levels not written to a table: in one statement, DuckDB 2.0.1 plans
    the output in a time that grows with the square of their number (90 such levels of
    gtfs-feeds K = 10: 10 s); statement by statement, in linear time."""
    with sg.Execution(query(120), documents=documents(120), allow_network=False) as ex:
        statements = ex.plan.triples_statements()
        assert len(statements) > 1
        assert all(len(s) <= compiler.OUTPUT_STATEMENT_CHARS or "UNION ALL" not in s for s in statements)
        t0 = time.perf_counter()
        assert ex.write(os.devnull) == 120 * (2 * N_PARENT + 3 * N_CHILD)
        assert time.perf_counter() - t0 < 15
