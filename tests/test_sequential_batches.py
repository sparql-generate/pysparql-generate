"""Sequential batches (on request: Execution(sequential=True), the command line,
generate_to): when the output of a query is the union of its outputs on the batches of its
first iterator (union_of_batches, the condition of the Java engine 2.6.2), the batches are
compiled and written one step after another, and the tables of one step only are in memory;
the output is the same as when all the batches are evaluated at once."""
import gzip
import io
import os
import subprocess
import sys
import threading

import pytest

import rdflib
from rdflib.compare import isomorphic

import sparql_generate as sg
from sparql_generate.compiler import union_of_batches
from sparql_generate.parser import parse

PREFIXES = """PREFIX iter: <http://w3id.org/sparql-generate/iter/>
PREFIX ex: <http://ex.org/>
"""
K, N = 4, 50
DOCS = {"http://ex.org/FEEDS.csv": "feed\n" + "".join(f"f{i}\n" for i in range(K))}
for i in range(K):
    DOCS[f"http://ex.org/T-f{i}.csv"] = "id,a,parent\n" + "".join(f"x{j},a{j},x{j // 2}\n" for j in range(N))

# the shape of gtfs-feeds: a FROM GENERATE of keys looked up by the sub-queries, blank nodes
# in the template of the query and of a sub-query, one feed per batch
QUERY = PREFIXES + """
GENERATE {
  [] ex:feed ?feed .
  GENERATE {
    <http://ex.org/{?feed}/{?id}> ex:a ?a ; ex:parent ?p ; ex:note [ ex:of ?id ] .
  }
  ITERATOR iter:CSV(<http://ex.org/T-{?feed}.csv>, "id", "a", "parent") AS ?id ?a ?parent
  WHERE { BIND(CONCAT(?feed, "|", ?parent) AS ?k) OPTIONAL { ?p ex:key ?k } } .
}
FROM GENERATE {
  GENERATE { <http://ex.org/{?feed}/{?id}> ex:key ?k . }
  ITERATOR iter:CSV(<http://ex.org/T-{?feed}.csv>, "id") AS ?id WHERE { BIND(CONCAT(?feed, "|", ?id) AS ?k) } .
}
ITERATOR iter:CSV(<http://ex.org/FEEDS.csv>, "feed") AS ?feed .
ITERATOR iter:CSV(<http://ex.org/FEEDS.csv>, 1, "feed") AS ?feed
WHERE {}
"""


def execution(query=QUERY, docs=DOCS, step_rows=1, **kw):
    ex = sg.Execution(query, documents=docs, allow_network=False, sequential=True, **kw)
    ex.compiler.step_rows = step_rows  # one batch per step
    return ex


def parse_nt(text):
    g = rdflib.Graph()
    g.parse(data=text, format="nt")
    return g


def test_same_output_as_all_batches_at_once(tmp_path):
    with execution() as ex:
        assert ex._sequential and ex.compiler.n_batches == K
        out = tmp_path / "out.nt"
        n = ex.write(str(out))
    text = out.read_text()
    assert n == len(text.splitlines()) == K * (1 + 4 * N)
    with execution() as ex:
        whole = ex.to_graph()  # all the batches at once
        assert not ex._sequential
    got = parse_nt(text)
    assert isomorphic(got, whole)
    # a blank node per solution: one per feed in the template of the query, one per row in
    # the template of the sub-query, none shared across batches
    assert len({s for s, p, o in got if p == rdflib.URIRef("http://ex.org/feed")}) == K
    assert len({o for s, p, o in got if p == rdflib.URIRef("http://ex.org/note")}) == K * N
    # the lookups in the FROM GENERATE of the first batch hold for the next ones
    assert len(list(got.triples((None, rdflib.URIRef("http://ex.org/parent"), None)))) == K * N


def test_stream_output():
    buf = io.StringIO()
    with execution() as ex:
        n = ex.write(buf)
    assert n == K * (1 + 4 * N)
    with execution() as ex:
        assert isomorphic(parse_nt(buf.getvalue()), ex.to_graph())


def test_memory_of_one_batch(tmp_path):
    """The tables of one batch only: the largest table that a batch creates holds the rows of
    one feed, and the tables of a batch are dropped before the next one."""
    sizes = []
    with execution() as ex:
        engine, keep = ex.engine, None
        orig = ex._next_batch

        def next_batch(lines=0):
            rows = engine.con.execute(
                "SELECT table_name, estimated_size FROM duckdb_tables() WHERE temporary").fetchall()
            sizes.append(max(sz for name, sz in rows if name not in ex.compiler.protected))
            return orig(lines)

        ex._next_batch = next_batch
        ex.write(str(tmp_path / "out.nt"))
        left = [r[0] for r in engine.con.execute("SELECT table_name FROM duckdb_tables() WHERE temporary").fetchall()]
        keep = ex.compiler.protected
    assert len(sizes) == K
    assert max(sizes) == N  # the rows of the sub-query, for one feed
    assert len([t for t in left if t not in keep]) < len(left)


def test_outputs_that_depend_on_the_batches_are_not_split():
    for clause in ["LIMIT 3", "ORDER BY ?id", "GROUP BY ?id"]:
        q = QUERY.replace("OPTIONAL { ?p ex:key ?k } } .", "OPTIONAL { ?p ex:key ?k } } " + clause + " .")
        assert not union_of_batches(parse(q)), clause
    assert not union_of_batches(parse(QUERY.replace("ex:a ?a ;", "ex:a ?a ; ex:list LIST(?a) ;")))
    assert not union_of_batches(parse(QUERY.replace("WHERE {}", "WHERE {} LIMIT 2")))
    assert union_of_batches(parse(QUERY))
    with execution(QUERY.replace("WHERE {}", "WHERE {} LIMIT 2")) as ex:
        assert not ex._sequential
        # LIMIT applies to each batch (rule BATCH, as the Java engine): here every feed
        assert len(ex.to_graph()) == K * (1 + 4 * N)
    # a FROM clause in a sub-query would be computed again for each batch
    sub_from = QUERY.replace("  ITERATOR iter:CSV(<http://ex.org/T-{?feed}.csv>, \"id\", \"a\", \"parent\")",
                             "  FROM <http://ex.org/g.ttl>\n  ITERATOR iter:CSV(<http://ex.org/T-{?feed}.csv>, \"id\", \"a\", \"parent\")")
    assert not union_of_batches(parse(sub_from))


def test_no_split_without_request():
    with sg.Execution(QUERY, documents=DOCS, allow_network=False) as ex:
        assert not ex._sequential


def test_no_batch_size_no_split():
    q = QUERY.replace("iter:CSV(<http://ex.org/FEEDS.csv>, 1, \"feed\")", "iter:CSV(<http://ex.org/FEEDS.csv>, \"feed\")")
    with execution(q) as ex:
        assert not ex._sequential


def test_other_outputs_after_a_sequential_write(tmp_path):
    with execution() as ex:
        ex.write(str(tmp_path / "a.nt"))
        g = ex.to_graph()  # all the batches again, at once
        ex.write(str(tmp_path / "b.nt"))
    assert isomorphic(parse_nt((tmp_path / "a.nt").read_text()), g)
    assert len((tmp_path / "b.nt").read_text().splitlines()) == len((tmp_path / "a.nt").read_text().splitlines())


def test_steps_group_batches(tmp_path):
    """After the first batch, a step groups as many batches as fit in step_rows rows."""
    steps = []
    with execution(step_rows=3 * (4 * N + 1)) as ex:
        orig = ex._next_batch

        def next_batch(lines=0):
            more = orig(lines)
            if more:
                steps.append((ex.compiler.batch_lo, ex.compiler.batch_hi))
            return more

        ex._next_batch = next_batch
        n = ex.write(str(tmp_path / "out.nt"))
    assert n == K * (1 + 4 * N)
    assert steps[0][0] == 1 and steps[-1][1] == K
    assert all(hi - lo >= 1 for lo, hi in steps) and len(steps) < K - 1


def lines_of(path):
    if path.endswith(".gz"):
        return gzip.decompress(open(path, "rb").read()).decode("utf-8")
    if path.endswith(".zst"):
        return subprocess.run(["zstd", "-dc", path], capture_output=True, check=True).stdout.decode("utf-8")
    return open(path, encoding="utf-8").read()


@pytest.mark.parametrize("ext", [".nt.gz", ".nt.zst"])
def test_compressed_output(tmp_path, ext):
    if ext == ".nt.zst" and subprocess.run(["which", "zstd"], capture_output=True).returncode:
        pytest.skip("no zstd command")
    out = str(tmp_path / ("out" + ext))
    with execution() as ex:
        n = ex.write(out)
    text = lines_of(out)
    assert n == len(text.splitlines()) == K * (1 + 4 * N)
    with execution() as ex:
        assert isomorphic(parse_nt(text), ex.to_graph())


def test_destination_left_as_it_was_on_error(tmp_path):
    """An error in a later step (a missing document with fail_on_missing): the regular file
    keeps its content, and no temporary file is left next to it."""
    # the sub-query reads U-<feed>.csv, missing for the last feed (the FROM GENERATE reads T)
    q = QUERY.replace('iter:CSV(<http://ex.org/T-{?feed}.csv>, "id", "a", "parent")',
                      'iter:CSV(<http://ex.org/U-{?feed}.csv>, "id", "a", "parent")')
    docs = dict(DOCS)
    docs.update({f"http://ex.org/U-f{i}.csv": DOCS[f"http://ex.org/T-f{i}.csv"] for i in range(K - 1)})
    out = tmp_path / "out.nt"
    out.write_text("before\n")
    with execution(q, docs=docs, fail_on_missing=True) as ex:
        assert ex._sequential
        with pytest.raises(Exception):
            ex.write(str(out))
    assert out.read_text() == "before\n"
    assert os.listdir(tmp_path) == ["out.nt"]


def test_fifo_output(tmp_path):
    fifo = str(tmp_path / "fifo")
    os.mkfifo(fifo)
    got = []
    reader = threading.Thread(target=lambda: got.append(open(fifo, encoding="utf-8").read()))
    reader.start()
    with execution() as ex:
        n = ex.write(fifo)
    reader.join(30)
    assert not reader.is_alive()
    assert n == len(got[0].splitlines()) == K * (1 + 4 * N)


def test_cli_to_dev_stdout(tmp_path):
    """-o /dev/stdout redirected to a file, and the step after step write of the CLI."""
    for name, text in DOCS.items():
        (tmp_path / name.rsplit("/", 1)[1]).write_text(text)
    q = QUERY.replace("http://ex.org/FEEDS.csv", "FEEDS.csv").replace("http://ex.org/T-{?feed}.csv", "T-{?feed}.csv")
    (tmp_path / "query.rqg").write_text("BASE <http://ex.org/>\n" + q)
    with open(tmp_path / "redirected.nt", "w") as f:
        r = subprocess.run([sys.executable, "-m", "sparql_generate.cli", "-d", str(tmp_path), "-q", "query.rqg",
                            "-o", "/dev/stdout"], stdout=f, stderr=subprocess.PIPE, text=True)
    assert r.returncode == 0, r.stderr
    assert len((tmp_path / "redirected.nt").read_text().splitlines()) == K * (1 + 4 * N)


def test_not_split():
    """Queries whose batches are not compiled one after another."""
    # a stream, a web API: read once, never again for each step
    for it in ['iter:WebSocket(<ws://localhost:1>) AS ?m', 'iter:HTTPGet(<http://localhost:1/>, 1, 2) AS ?m']:
        q = QUERY.replace('ITERATOR iter:CSV(<http://ex.org/FEEDS.csv>, 1, "feed") AS ?feed', "ITERATOR " + it)
        assert not union_of_batches(parse(q)), it
    # volatile functions in a WHERE clause, evaluated once per step; NOW() anywhere
    for where in ["{ { SELECT (BNODE() AS ?b) WHERE {} } }", "{ BIND(UUID() AS ?u) }", "{ BIND(RAND() AS ?r) }",
                  "{ BIND(STRUUID() AS ?u) }"]:
        assert not union_of_batches(parse(QUERY.replace("WHERE {}", "WHERE " + where))), where
    assert not union_of_batches(parse(QUERY.replace("WHERE {}", "BIND(NOW() AS ?now)\nWHERE {}")))
    # a single level: DuckDB evaluates all the batches with the memory of one
    single = PREFIXES + """GENERATE { <http://ex.org/{?feed}/{?id}> ex:f ?feed . }
ITERATOR iter:CSV(<http://ex.org/FEEDS.csv>, 1, "feed") AS ?feed
ITERATOR iter:CSV(<http://ex.org/T-{?feed}.csv>, "id") AS ?id WHERE {}"""
    assert not union_of_batches(parse(single))


def test_step_after_a_small_first_batch():
    """The step is computed again after each step from the largest batch seen so far: a
    small first batch does not make the next steps exceed the budget more than twice."""
    small = {"http://ex.org/FEEDS.csv": DOCS["http://ex.org/FEEDS.csv"].replace("feed\n", "feed\n") + "".join(
        f"f{i}\n" for i in range(K, 12))}
    for i in range(12):
        n = 1 if i == 0 else N
        small[f"http://ex.org/T-f{i}.csv"] = "id,a,parent\n" + "".join(f"x{j},a{j},x{j // 2}\n" for j in range(n))
    budget = 3 * (4 * N + 1)
    steps = []
    with execution(docs=small, step_rows=budget) as ex:
        orig = ex._next_batch

        def next_batch(lines=0):
            more = orig(lines)
            if more:
                steps.append(ex.compiler.batch_hi - ex.compiler.batch_lo)
            return more

        ex._next_batch = next_batch
        ex.write(io.StringIO())
    assert sum(steps) == 11
    assert max(steps) * (4 * N + 1) <= 2 * budget
