"""Sequential batches: when the output of a query is the union of its outputs on the batches
of its first iterator (union_of_batches, the condition of the Java engine 2.6.2), the batches
are compiled and written one after another, and the tables of one batch only are in memory;
the output is the same as when all the batches are evaluated at once."""
import io

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


def execution(query=QUERY, docs=DOCS, step_rows=1):
    ex = sg.Execution(query, documents=docs, allow_network=False)
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

        def next_batch():
            rows = engine.con.execute(
                "SELECT table_name, estimated_size FROM duckdb_tables() WHERE temporary").fetchall()
            sizes.append(max(sz for name, sz in rows if name not in ex.compiler.protected))
            return orig()

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
    with execution(step_rows=2 * N + 10) as ex:
        orig = ex._next_batch

        def next_batch():
            more = orig()
            if more:
                steps.append((ex.compiler.batch_lo, ex.compiler.batch_hi))
            return more

        ex._next_batch = next_batch
        n = ex.write(str(tmp_path / "out.nt"))
    assert n == K * (1 + 4 * N)
    assert steps[0][0] == 1 and steps[-1][1] == K
    assert all(hi - lo >= 1 for lo, hi in steps) and len(steps) < K - 1
