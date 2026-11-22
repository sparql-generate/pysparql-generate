"""Quad patterns (rule QUAD): GRAPH blocks in GENERATE templates, and the outputs that
hold quads.  The cases checked against the Java engine are tests/cases/quads-*."""
import os
import subprocess
import sys

import pytest

import sparql_generate as sg
from quadsutil import datasets_isomorphic

P = "PREFIX ex: <http://ex/>\n"
INT = "^^<http://www.w3.org/2001/XMLSchema#integer>"


def quads(q, **kw):
    with sg.Execution(P + q, allow_network=False, **kw) as ex:
        return sorted(ex.quads(distinct=True), key=str)


def nq(q, **kw):
    return "".join(f"{s} {p} {o}{' ' + g if g else ''} .\n" for s, p, o, g in quads(q, **kw))


def same(q, expected_nq, **kw):
    ok, detail = datasets_isomorphic(nq(q, **kw), P.replace("PREFIX ex: <http://ex/>\n", "") + expected_nq)
    assert ok, detail


def test_dataset_isomorphism_sees_the_graph_names():
    a = '<http://ex/s> <http://ex/p> "1" <http://ex/g> .\n'
    assert datasets_isomorphic(a, a)[0]
    assert not datasets_isomorphic(a, a.replace("ex/g>", "ex/h>"))[0]
    assert not datasets_isomorphic(a, '<http://ex/s> <http://ex/p> "1" .\n')[0]
    assert datasets_isomorphic('_:a <http://ex/p> _:b _:b .\n', '_:x <http://ex/p> _:y _:y .\n')[0]
    assert not datasets_isomorphic('_:a <http://ex/p> _:b _:b .\n', '_:x <http://ex/p> _:y _:x .\n')[0]


def test_graph_blocks():
    same("GENERATE { ex:s ex:p ?o . GRAPH ex:g { ex:s ex:q ?o } GRAPH ?h { ex:s ex:r ?o } } "
         "WHERE { VALUES (?h ?o) { (ex:h 1) } }",
         f'<http://ex/s> <http://ex/p> "1"{INT} .\n'
         f'<http://ex/s> <http://ex/q> "1"{INT} <http://ex/g> .\n'
         f'<http://ex/s> <http://ex/r> "1"{INT} <http://ex/h> .\n')


def test_unbound_or_literal_graph_gives_no_quad():
    got = quads('GENERATE { GRAPH ?g { ex:s ex:p ?o } } WHERE { VALUES (?g ?o) { (ex:g 1) (UNDEF 2) ("g" 3) } }')
    assert got == [("<http://ex/s>", "<http://ex/p>", f'"1"{INT}', "<http://ex/g>")]


def test_blank_node_graph_name():
    # as the Java engine (and RDF 1.1 datasets): a blank node names a graph
    (s, p, o, g), = quads("GENERATE { GRAPH ?g { ex:s ex:p 1 } } WHERE { BIND(BNODE() AS ?g) }")
    assert g.startswith("_:")


def test_nested_graph_blocks_are_refused():
    with pytest.raises(sg.ParseError, match="cannot be nested"):
        sg.parse(P + "GENERATE { GRAPH ex:g { GRAPH ex:h { ex:s ex:p 1 } } }")
    with pytest.raises(sg.ParseError, match="cannot be nested"):
        sg.parse(P + "GENERATE { GRAPH ex:g { GENERATE { ex:s ex:p 1 } . GRAPH ex:h { ex:s ex:p 1 } } }")
    # a sub-query of a GRAPH block has GRAPH blocks of its own
    sg.parse(P + "GENERATE { GRAPH ex:g { GENERATE { GRAPH ex:h { ex:s ex:p 1 } } . } }")


def test_subquery_of_variable_graph_per_graph():
    # one evaluation per graph (as the Java engine): COUNT and LIMIT range over a graph
    got = quads("GENERATE { GRAPH ?g { GENERATE { ex:s ex:n ?{ COUNT(?o) } } . "
                "GENERATE { ex:s ex:min ?o } ORDER BY ?o LIMIT 1 . } } "
                "WHERE { VALUES (?g ?o) { (ex:g1 1) (ex:g2 2) (ex:g1 3) (ex:g2 4) (ex:g2 5) (UNDEF 6) } }")
    assert [(p, o, g) for _, p, o, g in got] == sorted([
        ("<http://ex/n>", f'"2"{INT}', "<http://ex/g1>"), ("<http://ex/n>", f'"3"{INT}', "<http://ex/g2>"),
        ("<http://ex/min>", f'"1"{INT}', "<http://ex/g1>"), ("<http://ex/min>", f'"2"{INT}', "<http://ex/g2>")],
        key=lambda t: str(("<http://ex/s>",) + t))


def test_named_query_in_graph_block():
    called = P + "GENERATE ex:q(?x) { ex:s ex:q ?x . GRAPH ex:k { ex:s ex:r ?x } } WHERE {}"
    got = quads("GENERATE { GENERATE ex:q(?o) . GRAPH ex:g { GENERATE ex:q(?o) . } GRAPH ?h { GENERATE ex:q(?o) . } } "
                "WHERE { VALUES (?o ?h) { (1 ex:h) } }", named_queries={"http://ex/q": called})
    assert sorted(((p, g) for _, p, _, g in got), key=str) == sorted([
        ("<http://ex/q>", None), ("<http://ex/q>", "<http://ex/g>"), ("<http://ex/q>", "<http://ex/h>"),
        ("<http://ex/r>", "<http://ex/k>")], key=str)


def test_rml_graph_map_form():
    # the form of a translated RML graph map: GRAPH blocks in nested sub-GENERATEs, the
    # graph computed from the variables of a batched iterator
    csv = "id,name\n1,Alice\n2,Bob\n3,Carol\n"
    q = '''PREFIX iter: <http://w3id.org/sparql-generate/iter/>
    GENERATE {
      GENERATE { GRAPH ?g { ?s ex:name ?name } GRAPH <http://ex/all/{?id}> { ?s a ex:P } ?s ex:id ?id . }
      ITERATOR iter:CSV(<http://ex/p.csv>, 2, "id", "name") AS ?id ?name
      WHERE { BIND(IRI(CONCAT("http://ex/p/", ?id)) AS ?s) BIND(IRI(CONCAT("http://ex/g/", ?name)) AS ?g) } .
    } WHERE {}'''
    got = quads(q, documents={"http://ex/p.csv": {"data": csv, "mediatype": "text/csv"}})
    assert len(got) == 9
    assert ("<http://ex/p/2>", "<http://ex/name>", '"Bob"', "<http://ex/g/Bob>") in got
    assert ("<http://ex/p/3>", "<http://www.w3.org/1999/02/22-rdf-syntax-ns#type>", "<http://ex/P>",
            "<http://ex/all/3>") in got


def test_from_generate_ignores_quads():
    with sg.Execution(P + "GENERATE { ?s ex:found ?p } FROM GENERATE { ex:s ex:p 1 . GRAPH ex:g { ex:s ex:q 1 } } . "
                      "WHERE { ?s ?p ?o }") as ex:
        assert list(ex.quads()) == [("<http://ex/s>", "<http://ex/found>", "<http://ex/p>", None)]
        assert any("FROM GENERATE" in w for w in ex.warnings)


# -- outputs ------------------------------------------------------------------------------

Q = "GENERATE { ex:s ex:p 1 . GRAPH ex:g { ex:s ex:q 1 } GENERATE { GRAPH ex:h { ex:s ex:r 1 } } . }"


def test_query_without_graph_blocks_is_unchanged():
    q = P + "GENERATE { _:b ex:p ?o . ex:s ex:l LIST(?o) } WHERE { VALUES ?o { 1 2 } } ORDER BY ?o"
    with sg.Execution(q) as ex:
        assert not ex.has_quads
        triples = sorted(ex.triples())
        assert sorted(ex.quads()) == sorted(t + (None,) for t in triples)
        assert not ex.warnings
        assert len(ex.to_dataset()) == len(set(triples))


def test_triple_outputs_give_the_default_graph_with_a_warning(tmp_path):
    with sg.Execution(P + Q) as ex:
        assert ex.has_quads
        with pytest.warns(UserWarning, match="default graph only"):
            assert list(ex.triples()) == [("<http://ex/s>", "<http://ex/p>", f'"1"{INT}')]
        assert len(ex.to_graph()) == 1
        assert ex.write(str(tmp_path / "out.nt")) == 1
        assert any("default graph only" in w for w in ex.warnings)
    with pytest.warns(UserWarning):
        assert len(list(sg.generate(P + Q))) == 1


def test_quad_outputs(tmp_path):
    expected = (f'<http://ex/s> <http://ex/p> "1"{INT} .\n<http://ex/s> <http://ex/q> "1"{INT} <http://ex/g> .\n'
                f'<http://ex/s> <http://ex/r> "1"{INT} <http://ex/h> .\n')
    assert len(list(sg.generate_quads(P + Q))) == 3
    ds = sg.to_dataset(P + Q)
    assert len(ds) == 3 and {str(g.identifier) for g in ds.graphs()} >= {"http://ex/g", "http://ex/h"}
    assert sg.generate_to(P + Q, str(tmp_path / "out.nq"), format="nq") == 3
    assert datasets_isomorphic((tmp_path / "out.nq").read_text(), expected)[0]
    sg.generate_to(P + Q, str(tmp_path / "out.trig"), format="trig")
    import rdflib

    back = rdflib.Dataset()
    back.parse(str(tmp_path / "out.trig"), format="trig")
    assert datasets_isomorphic(back.serialize(format="nquads"), expected)[0]


def run_cli(tmp_path, *opts):
    (tmp_path / "query.rqg").write_text(P + Q)
    return subprocess.run([sys.executable, "-m", "sparql_generate.cli", "-d", str(tmp_path), "-q", "query.rqg",
                           "--no-network", *opts], capture_output=True, text=True,
                          env={**os.environ, "PYTHONPATH": os.path.join(os.path.dirname(__file__), "..", "src")})


def test_cli_formats(tmp_path):
    # without a format, N-Quads for a query with GRAPH blocks (N-Triples otherwise)
    r = run_cli(tmp_path, "-o", "out.nq")
    assert r.returncode == 0, r.stderr
    assert len((tmp_path / "out.nq").read_text().splitlines()) == 3
    r = run_cli(tmp_path, "-fo", "TRIG", "-o", "out.trig")
    assert r.returncode == 0, r.stderr
    # a format that holds triples only: an error, as the Java engine
    r = run_cli(tmp_path, "-fo", "NT", "-o", "out.nt")
    assert r.returncode == 1
    assert "N-Triples cannot hold" in r.stderr
