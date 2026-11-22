"""Dataset clauses (rule FROM): FROM, FROM NAMED, FROM GENERATE [NAMED], and one
dataset per group of input rows when the clauses depend on them (divergence D9)."""
import rdflib

import sparql_generate as sg

PFX = "BASE <http://ex.org/> PREFIX ex: <http://ex.org/>\n"
A_TTL = "@prefix ex: <http://ex.org/> . ex:a ex:p 1 . ex:a2 ex:p 2 ."
B_TTL = "@prefix ex: <http://ex.org/> . ex:b ex:p 10 ."
DOCS = {"http://ex.org/a.ttl": {"data": A_TTL, "mediatype": "text/turtle"},
        "http://ex.org/b.ttl": {"data": B_TTL, "mediatype": "text/turtle"}}


def docs():
    from sparql_generate.documents import Document, DocumentSet

    ds = DocumentSet()
    for iri, d in DOCS.items():
        ds.docs[iri] = Document(iri, data=d["data"], mediatype=d["mediatype"])
    return ds


def objs(q, **kw):
    return sorted(r[2] for r in sg.generate(PFX + q, documents=docs(), allow_network=False, **kw))


INT = "^^<http://www.w3.org/2001/XMLSchema#integer>"


def test_from_document():
    assert objs("GENERATE { ?s ex:q ?o } FROM <a.ttl> WHERE { ?s ex:p ?o }") == [f'"1"{INT}', f'"2"{INT}']
    # two FROM clauses: the RDF merge of the graphs is the default graph
    assert len(objs("GENERATE { ?s ex:q ?o } FROM <a.ttl> FROM <b.ttl> WHERE { ?s ex:p ?o }")) == 3


def test_from_named():
    rows = sorted(sg.generate(PFX + "GENERATE { ?s ex:in ?g } FROM NAMED <a.ttl> FROM NAMED <b.ttl> WHERE { GRAPH ?g { ?s ex:p ?o } }",
                              documents=docs(), allow_network=False))
    assert sorted((r[0], r[2]) for r in rows) == sorted([("<http://ex.org/a>", "<http://ex.org/a.ttl>"),
                                                         ("<http://ex.org/a2>", "<http://ex.org/a.ttl>"),
                                                         ("<http://ex.org/b>", "<http://ex.org/b.ttl>")])
    # the named graphs are not in the default graph
    assert objs("GENERATE { ?s ex:q ?o } FROM NAMED <a.ttl> WHERE { ?s ex:p ?o }") == []


def test_from_a_named_graph_of_the_dataset():
    ds = rdflib.Dataset(default_union=False)
    ds.graph(rdflib.URIRef("http://ex.org/g")).parse(data=B_TTL, format="turtle")
    ds.default_graph.parse(data=A_TTL, format="turtle")
    assert objs("GENERATE { ?s ex:q ?o } FROM <g> WHERE { ?s ex:p ?o }", dataset=ds) == [f'"10"{INT}']
    # without FROM, the default graph of the dataset
    assert len(objs("GENERATE { ?s ex:q ?o } WHERE { ?s ex:p ?o }", dataset=ds)) == 2


def test_from_generate():
    q = 'GENERATE { ?s ex:q ?o } FROM GENERATE { ex:x ex:p ?i } WHERE { VALUES ?i { 5 6 } } . WHERE { ?s ex:p ?o }'
    assert objs(q) == [f'"5"{INT}', f'"6"{INT}']
    # FROM GENERATE ... NAMED <g> fills the named graph (D15 in the Java engine)
    q = 'GENERATE { ?s ex:q ?g } FROM GENERATE { ex:x ex:p 1 } NAMED <urn:g> . WHERE { GRAPH ?g { ?s ex:p ?o } }'
    assert objs(q) == ["<urn:g>"]
    # FROM GENERATE of a named query
    q2 = PFX + 'GENERATE <http://ex.org/g.rqg>() { ex:s ex:p ex:o . } WHERE {}'
    q = 'GENERATE { ?s ex:q ?g } FROM GENERATE <http://ex.org/g.rqg>() NAMED <urn:g> . WHERE { GRAPH ?g { ?s ?p ?o } }'
    assert objs(q, named_queries={"http://ex.org/g.rqg": q2}) == ["<urn:g>"]


def test_dataset_per_group_of_input_rows():
    # D9: a sub-query evaluated on several parent solutions, whose FROM depends on them,
    # reads one dataset per solution (the Java engine uses the first row's for all)
    q = '''GENERATE { GENERATE { ?doc ex:has ?o } FROM ?doc WHERE { ?s ex:p ?o } . }
           WHERE { VALUES ?doc { <a.ttl> <b.ttl> } }'''
    rows = sorted(sg.generate(PFX + q, documents=docs(), allow_network=False))
    assert [(r[0], r[2]) for r in rows] == [("<http://ex.org/a.ttl>", f'"1"{INT}'), ("<http://ex.org/a.ttl>", f'"2"{INT}'),
                                           ("<http://ex.org/b.ttl>", f'"10"{INT}')]
    # the same through a named query called on two argument tuples
    q2 = PFX + 'GENERATE <http://ex.org/q2.rqg>(?doc) { ?doc ex:has ?o } FROM ?doc WHERE { ?s ex:p ?o }'
    q = 'GENERATE { GENERATE <http://ex.org/q2.rqg>(?doc) . } WHERE { VALUES ?doc { <a.ttl> <b.ttl> } }'
    rows = sorted(sg.generate(PFX + q, documents=docs(), allow_network=False, named_queries={"http://ex.org/q2.rqg": q2}))
    assert len(rows) == 3 and {r[0] for r in rows} == {"<http://ex.org/a.ttl>", "<http://ex.org/b.ttl>"}
    # and with a FROM GENERATE that reads a parent variable
    q = '''GENERATE { GENERATE { <s{?i}> ex:sees ?o } FROM GENERATE { ex:x ex:v ?i } . WHERE { ?x ex:v ?o } . }
           WHERE { VALUES ?i { 1 2 } }'''
    rows = sorted(sg.generate(PFX + q, allow_network=False))
    assert [(r[0], r[2]) for r in rows] == [("<http://ex.org/s1>", f'"1"{INT}'), ("<http://ex.org/s2>", f'"2"{INT}')]


def test_graphs_are_sets():
    # FROM GENERATE writes a bag; the graph it builds is a set: a join on it does not multiply
    # its solutions (the RML joins translated to SPARQL-Generate, GTFS: n^2 per key before)
    q = PFX + '''GENERATE { ?s ex:p ?o } FROM GENERATE { GENERATE { ex:a ex:k ?k . ex:b ex:k ?k } WHERE { VALUES ?k { 1 1 1 } } . } .
    WHERE { ?s ex:k ?x . ?o ex:k ?x }'''
    assert len(list(sg.generate(q, allow_network=False))) == 4


def test_identical_from_generate_in_sibling_subqueries():
    # each FROM GENERATE gives its own dataset; the Java engine 2.3.1 reuses the first
    # evaluation of an identical sub-query on the same binding (the tabling of the calls of
    # named queries) and leaves the second dataset empty
    q = PFX + '''PREFIX iter: <http://w3id.org/sparql-generate/iter/>
    GENERATE {
      GENERATE { ex:w ex:a ?t } FROM GENERATE { ex:n0 ex:next ex:n1 } . WHERE { ?t ex:next ?x } .
      GENERATE { ex:w ex:b ?t } FROM GENERATE { ex:n0 ex:next ex:n1 } . WHERE { ?t ex:next ?x } .
    } WHERE {}'''
    assert sorted(r[1] for r in sg.generate(q, allow_network=False)) == ["<http://ex.org/a>", "<http://ex.org/b>"]


DEPENDENT = PFX + '''PREFIX iter: <http://w3id.org/sparql-generate/iter/>
GENERATE {
  GENERATE { ?who ex:sees ?o . ?who ex:reach ?t . ?who ex:in ?g }
  FROM GENERATE { ex:x ex:v ?i . ex:x ex:v ?{ ?i * 10 } . ex:x ex:next ?end . ?end ex:next ex:z } .
  FROM GENERATE { ex:y ex:v ?i } NAMED <http://ex.org/g{?i}> .
  WHERE { ex:x ex:v ?o . ex:x ex:next+ ?t . GRAPH ?g { ex:y ex:v ?i } } .
} ITERATOR iter:for(0, 1, 40) AS ?i
  WHERE { BIND(IRI(CONCAT("http://ex.org/who", STR(?i))) AS ?who) BIND(IRI(CONCAT("http://ex.org/e", STR(?i))) AS ?end) }'''


def test_from_generate_per_group_compiled_once(monkeypatch):
    # rule FROM-SET: a FROM GENERATE that reads variables of the level is compiled once on
    # the table of its groups; the same output as one compilation per group
    from sparql_generate import datasets

    once = sorted(sg.generate(DEPENDENT, distinct=True, allow_network=False))
    assert len(once) == 40 * (2 + 2 + 1) - 1  # 0 * 10 = 0
    calls = []
    real = datasets._generate_into
    monkeypatch.setattr(datasets, "_set_based_ok", lambda compiler, sub: False)
    monkeypatch.setattr(datasets, "_generate_into", lambda *a, **k: (calls.append(1), real(*a, **k))[1])
    assert sorted(sg.generate(DEPENDENT, distinct=True, allow_network=False)) == once
    assert len(calls) == 2 * 40


def test_from_generate_per_group_falls_back_on_aggregates():
    # an aggregate of the sub-query ranges over the solutions of one group: one compilation
    # per group (the Java engine evaluates FROM GENERATE once per group)
    q = PFX + '''PREFIX iter: <http://w3id.org/sparql-generate/iter/>
    GENERATE {
      GENERATE { ?who ex:n ?n } FROM GENERATE { ex:x ex:n ?{ COUNT(?j) } } ITERATOR iter:for(0, 1, ?i) AS ?j . WHERE { ex:x ex:n ?n } .
    } ITERATOR iter:for(1, 1, 4) AS ?i WHERE { BIND(IRI(CONCAT("http://ex.org/who", STR(?i))) AS ?who) }'''
    got = sorted((r[0], r[2]) for r in sg.generate(q, allow_network=False))
    assert got == [(f"<http://ex.org/who{i}>", f'"{i}"{INT}') for i in (1, 2, 3)]


def test_from_generate_reads_the_enclosing_dataset():
    # the sub-query of FROM GENERATE queries the dataset of the level that holds the clause
    q = '''GENERATE { GENERATE { ?s ex:r ?o } FROM GENERATE { ?s ex:q ?o } WHERE { ?s ex:p ?o } . WHERE { ?s ex:q ?o } . }
    FROM <a.ttl> WHERE {}'''
    assert objs(q) == [f'"1"{INT}', f'"2"{INT}']


def test_from_relative_document_without_base(tmp_path):
    # FROM <a.ttl> without BASE: the document is found, its name is no base IRI for the parser
    (tmp_path / "a.ttl").write_text(A_TTL)
    q = "PREFIX ex: <http://ex.org/> GENERATE { ?s ex:q ?o } FROM <a.ttl> WHERE { ?s ex:p ?o }"
    with sg.Execution(q, base_dir=str(tmp_path), allow_network=False) as ex:
        assert len(list(ex.triples())) == 2
        assert not ex.warnings
