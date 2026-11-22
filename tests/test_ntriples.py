"""The native reader of N-Triples and N-Quads files (no rdflib): the dataset table of rule
(BGP) read by DuckDB, compared with the same file parsed by rdflib."""
import subprocess
import sys

import rdflib
from rdflib.compare import isomorphic

import sparql_generate as sg
from sparql_generate.ntriples import native_quads

PFX = "PREFIX ex: <http://ex.org/>\n"
NT = r"""# a comment, then a blank line

<http://ex.org/a> <http://ex.org/name> "Ann \"A\" \\ éé\n\ttab" .
<http://ex.org/a> <http://ex.org/label> "hello"@en-GB .
<http://ex.org/a> <http://ex.org/age> "42"^^<http://www.w3.org/2001/XMLSchema#integer> .
<http://ex.org/a> <http://ex.org/plain> "x"^^<http://www.w3.org/2001/XMLSchema#string> .
<http://ex.org/a> <http://ex.org/knows> _:b1 .
_:b1 <http://ex.org/name> "Bob" .
_:b1 <http://ex.org/knows> <http://ex.org/a>.
<http://ex.org/c>	<http://ex.org/name>	"tabs and # not a comment" . # a comment
"""


def write(tmp_path, name, text):
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return str(p)


def graph_of(rows):
    g = rdflib.Graph()
    g.parse(data="\n".join(f"{s} {p} {o} ." for s, p, o in rows), format="nt")
    return g


def test_native_reader_reads_what_rdflib_reads(tmp_path):
    path = write(tmp_path, "d.nt", NT)
    with sg.Execution(PFX + "GENERATE { ex:s ex:p ex:o } WHERE {}", allow_network=False) as ex:
        assert native_quads(ex.engine, path) is not None  # the fast path applies to this file
    q = PFX + "GENERATE { ?s ?p ?o } WHERE { ?s ?p ?o }"
    native = graph_of(sg.generate(q, dataset=path))
    parsed = rdflib.Graph()
    parsed.parse(path, format="nt")
    ref = rdflib.Graph()  # RDF 1.1: "x"^^xsd:string is "x" (rdflib keeps them apart)
    for s_, p_, o_ in parsed:
        if isinstance(o_, rdflib.Literal) and o_.datatype == rdflib.XSD.string:
            o_ = rdflib.Literal(str(o_))
        ref.add((s_, p_, o_))
    assert len(native) == 8 and isomorphic(native, ref)
    # joins on the terms decoded from the file, constants of the query included
    q = PFX + 'GENERATE { ?x ex:friendName ?n } WHERE { ?x ex:knows ?y . ?y ex:name ?n . ?x ex:age 42 }'
    assert [o for s, p, o in sg.generate(q, dataset=path)] == ['"Bob"']
    q = PFX + 'GENERATE { ?s ex:is ex:it } WHERE { ?s ex:label "hello"@en-GB ; ex:plain "x" ; ex:name "Ann \\"A\\" \\\\ éé\\n\\ttab" }'
    assert [s for s, p, o in sg.generate(q, dataset=path)] == ["<http://ex.org/a>"]


def test_nquads_named_graphs_and_the_rdflib_fallback(tmp_path):
    path = write(tmp_path, "d.nq", """<http://ex.org/a> <http://ex.org/p> "1" <http://ex.org/g1> .
<http://ex.org/a> <http://ex.org/p> "2" <http://ex.org/g2> .
<http://ex.org/a> <http://ex.org/p> "0" .
<http://ex.org/a> <http://ex.org/q> <http://ex.org/b> .
<http://ex.org/b> <http://ex.org/q> <http://ex.org/c> .
""")
    q = PFX + "GENERATE { ?g ex:has ?v } WHERE { GRAPH ?g { ex:a ex:p ?v } }"
    assert sorted(sg.generate(q, dataset=path)) == [
        ("<http://ex.org/g1>", "<http://ex.org/has>", '"1"'), ("<http://ex.org/g2>", "<http://ex.org/has>", '"2"')]
    q = PFX + "GENERATE { ex:a ex:p ?v } WHERE { ex:a ex:p ?v }"
    assert [o for s, p, o in sg.generate(q, dataset=path)] == ['"0"']
    # an arbitrary-length path (native) and a negated property set (rdflib, on the dataset
    # converted from the native table)
    q = PFX + "GENERATE { ex:a ex:reaches ?x } WHERE { ex:a ex:q+ ?x }"
    assert sorted(o for s, p, o in sg.generate(q, dataset=path)) == ["<http://ex.org/b>", "<http://ex.org/c>"]
    q = PFX + "GENERATE { ex:a ex:other ?x } WHERE { ex:a !ex:p ?x }"
    assert [o for s, p, o in sg.generate(q, dataset=path)] == ["<http://ex.org/b>"]


def test_files_that_need_rdflib(tmp_path):
    # a \U escape, an escaped IRI, a blank node graph name, Turtle: parsed by rdflib
    q = PFX + "GENERATE { ?s ?p ?o } WHERE { ?s ?p ?o }"
    cases = {"u.nt": '<http://ex.org/a> <http://ex.org/p> "\\U0001F600" .\n',
             "i.nt": '<http://ex.org/\\u0041> <http://ex.org/p> "x" .\n',
             "b.nq": '<http://ex.org/a> <http://ex.org/p> "x" _:g .\n',
             "t.ttl": '@prefix ex: <http://ex.org/> . ex:a ex:p "x" .\n'}
    with sg.Execution(PFX + "GENERATE { ex:s ex:p ex:o } WHERE {}", allow_network=False) as ex:
        for name, text in cases.items():
            path = write(tmp_path, name, text)
            if name.endswith((".nt", ".nq")):
                assert native_quads(ex.engine, path) is None
    assert [o for s, p, o in sg.generate(q, dataset=str(tmp_path / "u.nt"))] == ['"\U0001F600"']
    assert [s for s, p, o in sg.generate(q, dataset=str(tmp_path / "i.nt"))] == ["<http://ex.org/A>"]
    assert [o for s, p, o in sg.generate(q, dataset=str(tmp_path / "t.ttl"))] == ['"x"']


def test_cli_dataset_option(tmp_path):
    path = write(tmp_path, "d.nt", NT)
    qf = write(tmp_path, "q.rqg", PFX + "GENERATE { ?s ex:n ?n } WHERE { ?s ex:name ?n }")
    out = subprocess.run([sys.executable, "-m", "sparql_generate.cli", "-q", qf, "--dataset", path, "--no-network"],
                         capture_output=True, text=True, check=True).stdout
    assert out.count(" .\n") == 3


def test_native_reader_on_many_triples(tmp_path):
    p = tmp_path / "big.nt"
    with open(p, "w") as f:
        for i in range(100_000):
            f.write(f'<http://ex.org/s{i}> <http://ex.org/next> <http://ex.org/s{i + 1}> .\n'
                    f'<http://ex.org/s{i}> <http://ex.org/p> "{i}"^^<http://www.w3.org/2001/XMLSchema#integer> .\n')
    q = PFX + "GENERATE { ?s ex:q ?w } WHERE { ?s ex:next ?n . ?n ex:p ?v BIND(?v + 1 AS ?w) FILTER(?v > 10) }"
    with sg.Execution(q, dataset=str(p)) as ex:
        assert sum(1 for _ in ex.triples()) == 100_000 - 1 - 10


TTL = """@prefix ex: <http://ex.org/> .
@base <http://ex.org/base/> .
ex:a ex:name "Ann"@en ; ex:age 42 ; ex:ok true ; ex:knows [ ex:name "Bob" ] , <rel> ;
     ex:list ( 1 2.5 "x" ) ; ex:long \"\"\"two
lines\"\"\" ; ex:u "\\U0001F600" .
"""


def test_turtle_without_rdflib(tmp_path, monkeypatch):
    pytest = __import__("pytest")
    pytest.importorskip("pyoxigraph")
    path = write(tmp_path, "d.ttl", TTL)
    import rdflib.plugins.parsers.notation3 as n3

    def refuse(*a, **k):  # rdflib's Turtle parser must not be used
        raise AssertionError("rdflib parsed the Turtle file")

    q = PFX + "GENERATE { ?s ?p ?o } WHERE { ?s ?p ?o }"
    monkeypatch.setattr(n3.TurtleParser, "parse", refuse)
    native = graph_of(sg.generate(q, dataset=path))
    monkeypatch.undo()
    ref = rdflib.Graph()
    ref.parse(path, format="turtle")
    assert len(native) == len(ref) == 15 and isomorphic(native, ref)
    # FROM <doc.ttl> too
    docs = {"http://ex.org/d.ttl": path}
    q = PFX + "GENERATE { ?s ex:n ?n } FROM <http://ex.org/d.ttl> WHERE { ?s ex:name ?n }"
    assert sorted(o for s, p, o in sg.generate(q, documents=docs)) == ['"Ann"@en', '"Bob"']


def test_a_file_that_repeats_a_triple_is_a_set(tmp_path):
    path = write(tmp_path, "d.nt", '<http://ex.org/a> <http://ex.org/p> "1" .\n' * 3)
    q = PFX + "GENERATE { ?s ex:q ?o } WHERE { ?s ex:p ?o }"
    assert len(list(sg.generate(q, dataset=path))) == 1
