"""Native compilation of WHERE graph patterns over the RDF dataset (rule BGP), checked
against the rdflib fallback on the same queries."""
import time

import pytest
import rdflib

import sparql_generate as sg
from sparql_generate import rdfwhere

PFX = """PREFIX ex: <http://ex.org/>
PREFIX iter: <http://w3id.org/sparql-generate/iter/>
PREFIX xsd: <http://www.w3.org/2001/XMLSchema#>
"""

DATA = """@prefix ex: <http://ex.org/> .
ex:a ex:p 1 ; ex:name "A" ; ex:knows ex:b , ex:c ; a ex:Person .
ex:b ex:p 2 ; ex:name "B"@en ; ex:knows ex:c ; a ex:Person .
ex:c ex:p 3 ; ex:label "C \\"quoted\\"\\n" ; a ex:Thing .
ex:d ex:p "4"^^xsd:decimal ; ex:knows [ ex:name "anon" ] .
"""
NAMED = {"http://ex.org/g1": "@prefix ex: <http://ex.org/> . ex:a ex:in ex:g1 . ex:b ex:in ex:g1 .",
         "http://ex.org/g2": "@prefix ex: <http://ex.org/> . ex:c ex:in ex:g2 . ex:a ex:p 1 ."}


def dataset():
    ds = rdflib.Dataset(default_union=False)
    ds.default_graph.parse(data=PFX.replace("PREFIX ", "@prefix ").replace(">\n", "> .\n") + DATA, format="turtle")
    for g, ttl in NAMED.items():
        ds.graph(rdflib.URIRef(g)).parse(data=ttl, format="turtle")
    return ds


def run(q, native, **kw):
    """Sorted triples, blank node labels replaced by ``_:`` (each run parses the data anew)."""
    with (sg.Execution(q, dataset=dataset(), **kw) if native else _fallback(q, **kw)) as ex:
        return sorted(tuple("_:" if t.startswith("_:") else t for t in tr) for tr in ex.triples())


def _fallback(q, **kw):
    import sparql_generate.engine as E

    orig = E.Engine.__init__

    def init(self, *a, **k):
        orig(self, *a, **k)
        self.native_where = False

    E.Engine.__init__ = init
    try:
        return sg.Execution(q, dataset=dataset(), **kw)
    finally:
        E.Engine.__init__ = orig


QUERIES = {
    "bgp": "GENERATE { ?s ex:q ?o } WHERE { ?s ex:p ?o }",
    "join": "GENERATE { ?s ex:q ?n } WHERE { ?s ex:knows ?x . ?x ex:name ?n }",
    "constant-object": "GENERATE { ?s ex:q ex:x } WHERE { ?s a ex:Person ; ex:p 2 }",
    "literal-escape": 'GENERATE { ?s ex:q ?l } WHERE { ?s ex:label "C \\"quoted\\"\\n" ; ex:label ?l }',
    "lang": 'GENERATE { ?s ex:q ?n } WHERE { ?s ex:name "B"@en ; ex:name ?n }',
    "decimal": 'GENERATE { ?s ex:q ?o } WHERE { ?s ex:p "4"^^xsd:decimal ; ex:p ?o }',
    "variable-predicate": "GENERATE { ?s ?p ?o } WHERE { ?s ?p ?o . FILTER(?s = ex:a) }",
    "same-variable-twice": "GENERATE { ?s ex:q ?s } WHERE { ?s ex:knows ?s2 . ?s2 ex:knows ?s3 . ?s ex:knows ?s3 }",
    "blank-node-pattern": "GENERATE { ?s ex:q ?n } WHERE { ?s ex:knows [ ex:name ?n ] }",
    "optional": "GENERATE { ?s ex:q ?n } WHERE { ?s ex:p ?v OPTIONAL { ?s ex:name ?n } }",
    "optional-unbound": "GENERATE { ?s ex:q ?v . ?s ex:r ?n } WHERE { ?s ex:p ?v OPTIONAL { ?s ex:name ?n } }",
    "optional-filter": "GENERATE { ?s ex:q ?x } WHERE { ?s ex:p ?v OPTIONAL { ?s ex:knows ?x FILTER(?v > 1) } }",
    "union": "GENERATE { ?s ex:q ?o } WHERE { { ?s ex:name ?o } UNION { ?s ex:label ?o } UNION { ?s ex:knows ?o } }",
    "union-different-vars": "GENERATE { ?s ex:q ?a . ?s ex:r ?b } WHERE { { ?s ex:name ?a } UNION { ?s ex:p ?b } }",
    "minus": "GENERATE { ?s ex:q ?v } WHERE { ?s ex:p ?v MINUS { ?s a ex:Person } }",
    "minus-disjoint": "GENERATE { ?s ex:q ?v } WHERE { ?s ex:p ?v MINUS { ?x a ex:Person } }",
    "filter-bind": "GENERATE { ?s ex:q ?w } WHERE { ?s ex:p ?v BIND(CONCAT(\"v\", STR(?v)) AS ?w) FILTER(?v >= 2) }",
    "values": "GENERATE { ?s ex:q ?v } WHERE { VALUES ?s { ex:a ex:c ex:zz } ?s ex:p ?v }",
    "nested-group": "GENERATE { ?s ex:q ?n } WHERE { ?s a ex:Person { ?s ex:name ?n } }",
    "graph-iri": "GENERATE { ?s ex:q ?o } WHERE { GRAPH ex:g1 { ?s ex:in ?o } }",
    "graph-var": "GENERATE { ?s ex:q ?g } WHERE { GRAPH ?g { ?s ?p ?o } }",
    "graph-and-default": "GENERATE { ?s ex:q ?g } WHERE { ?s a ex:Person GRAPH ?g { ?s ex:in ?o } }",
    "iterator-input": ("GENERATE { ?s ex:q ?k } ITERATOR iter:for(1, 1, 4) AS ?k "
                       "WHERE { ?s ex:p ?k }"),
    "path-seq": "GENERATE { ?s ex:q ?n } WHERE { ?s ex:knows/ex:name ?n }",
    "path-inverse": "GENERATE { ?s ex:q ?o } WHERE { ?o ^ex:knows ?s }",
    "path-alt": "GENERATE { ?s ex:q ?o } WHERE { ?s ex:name|ex:label|ex:in ?o }",
    "path-mixed": "GENERATE { ?s ex:q ?n } WHERE { ?s (ex:knows|^ex:knows)/ex:p ?n . ?s a ex:Person }",
    "path-constant-ends": "GENERATE { ex:a ex:q ex:yes } WHERE { ex:a ex:knows/ex:knows ex:c }",
    "path-in-graph": "GENERATE { ?s ex:q ?g } WHERE { GRAPH ?g { ?s ex:in/^ex:in ?t } }",
    "exists": "GENERATE { ?s ex:q ?v } WHERE { ?s ex:p ?v FILTER EXISTS { ?s ex:knows ?x } }",
    "not-exists": "GENERATE { ?s ex:q ?v } WHERE { ?s ex:p ?v FILTER NOT EXISTS { ?s a ex:Person } }",
    "exists-filter-outer": "GENERATE { ?s ex:q ?v } WHERE { ?s ex:p ?v FILTER EXISTS { ?x ex:p ?w FILTER(?w > ?v) } }",
    "exists-in-expression": "GENERATE { ?s ex:q ?v } WHERE { ?s ex:p ?v FILTER(?v = 1 || NOT EXISTS { ?s ex:knows ex:c }) }",
    "exists-bind": "GENERATE { ?s ex:q ?b } WHERE { ?s ex:p ?v BIND(EXISTS { ?s ex:name ?n } AS ?b) }",
    "subselect": "GENERATE { ?s ex:q ?c } WHERE { ?s a ex:Person { SELECT ?s (COUNT(?x) AS ?c) WHERE { ?s ex:knows ?x } GROUP BY ?s } }",
    "subselect-limit": "GENERATE { ?s ex:q ?o } WHERE { { SELECT ?s ?o WHERE { ?s ex:p ?o } ORDER BY DESC(?o) LIMIT 2 } }",
    "exists-graph": "GENERATE { ?s ex:q ?g } WHERE { GRAPH ?g { ?s ex:in ?o FILTER EXISTS { ?s ex:in ?g } } }",
    "exists-input-table": ("GENERATE { ?s ex:q ?k } ITERATOR iter:for(1, 1, 4) AS ?k "
                           "WHERE { ?s a ex:Person FILTER EXISTS { ?s ex:p ?k } }"),
    "path-plus": "GENERATE { ?s ex:q ?o } WHERE { ?s ex:knows+ ?o }",
    "path-star-constant": "GENERATE { ex:a ex:q ?o } WHERE { ex:a ex:knows* ?o }",
    "path-star-object-constant": "GENERATE { ?s ex:q ex:c } WHERE { ?s ex:knows* ex:c }",
    "path-star-vars": "GENERATE { ?s ex:q ?o } WHERE { ?s (ex:knows|ex:in)* ?o }",
    "path-optional": "GENERATE { ?s ex:q ?o } WHERE { ?s a ex:Person . ?s ex:knows? ?o }",
    "path-plus-seq": "GENERATE { ?s ex:q ?n } WHERE { ?s ex:knows+/ex:p ?n }",
    "path-plus-inverse": "GENERATE { ?s ex:q ?o } WHERE { ?s ^ex:knows+ ?o }",
    "path-cycle-same-var": "GENERATE { ?s ex:q ?s } WHERE { ?s (ex:knows|^ex:knows)+ ?s }",
    "path-in-graph-iri": "GENERATE { ?s ex:q ?o } WHERE { GRAPH ex:g1 { ?s ex:in* ?o } }",
    "path-negated": "GENERATE { ?s ex:q ?o } WHERE { ?s !(ex:p|ex:name|a) ?o }",
    "path-negated-closure": "GENERATE { ex:a ex:q ?o } WHERE { ex:a (!ex:p)+ ?o }",
    "path-closure-graph-var": "GENERATE { ?s ex:q ?g } WHERE { GRAPH ?g { ?s ex:in* ?o } }",
    "path-closure-graph-var-constant": "GENERATE { ex:a ex:q ?g . ex:a ex:r ?o } WHERE { GRAPH ?g { ex:a ex:in* ?o } }",
    "optional-key-bound-or-not": ("GENERATE { ?s ex:q ?x } WHERE { VALUES (?s ?k) { (ex:a 1) (ex:b UNDEF) (ex:c 3) } "
                                  "OPTIONAL { ?x ex:p ?k } }"),
    "iterator-input-unbound": ("GENERATE { ?s ex:q ?v } ITERATOR iter:Split(\"ex:a,,ex:b\", \",\") AS ?x "
                               "WHERE { BIND(IF(?x = '', ?nothing, IRI(CONCAT('http://ex.org/', STRAFTER(?x, ':')))) AS ?s) ?s ex:p ?v }"),
}


@pytest.mark.parametrize("name", sorted(QUERIES))
def test_native_where_agrees_with_rdflib(name, monkeypatch):
    q = PFX + QUERIES[name]
    called = []
    monkeypatch.setattr(rdfwhere, "compile_where_rdflib", lambda *a: called.append(1) or (_ for _ in ()).throw(AssertionError))
    native = run(q, True)
    assert not called, "the rdflib fallback was used"
    monkeypatch.undo()
    expected = run(q, False)
    assert native == expected
    assert native or name in ("minus-disjoint",), "the query should produce triples"


def test_where_on_100k_triples_is_fast():
    g = rdflib.Graph()
    EX = "http://ex.org/"
    for i in range(50_000):
        s = rdflib.URIRef(f"{EX}s{i}")
        g.add((s, rdflib.URIRef(EX + "p"), rdflib.Literal(i)))
        g.add((s, rdflib.URIRef(EX + "next"), rdflib.URIRef(f"{EX}s{i + 1}")))
    q = PFX + "GENERATE { ?s ex:q ?w } WHERE { ?s ex:next ?n . ?n ex:p ?v BIND(?v + 1 AS ?w) FILTER(?v > 10) }"
    t0 = time.perf_counter()
    with sg.Execution(q, dataset=g) as ex:
        n = sum(1 for _ in ex.triples())
    dt = time.perf_counter() - t0
    assert n == 50_000 - 1 - 10
    assert dt < 2.0, f"{dt:.2f} s"


def test_nested_groups_do_not_see_the_input_table():
    # Def. input-table: the variables of the input table are visible to the top-level group
    # only; a nested group, UNION branch or OPTIONAL operand is evaluated independently
    rows = list(sg.generate(PFX + 'GENERATE { ex:s ex:x ?x ; ex:y ?y } WHERE { VALUES ?x { 1 } { BIND(?x + 1 AS ?y) } }'))
    assert rows == [("<http://ex.org/s>", "<http://ex.org/x>", '"1"^^<http://www.w3.org/2001/XMLSchema#integer>')]
    rows = sorted(sg.generate(PFX + '''GENERATE { ?s ex:y ?y } WHERE {
        VALUES (?s ?x) { (ex:a 1) (ex:b 2) } OPTIONAL { VALUES (?x ?y) { (1 "one") } } }'''))
    assert rows == [("<http://ex.org/a>", "<http://ex.org/y>", '"one"')]
    rows = sorted(sg.generate(PFX + '''GENERATE { ex:s ex:v ?v } WHERE { { BIND("a" AS ?v) } UNION { BIND("b" AS ?v) } }'''))
    assert [r[2] for r in rows] == ['"a"', '"b"']
    rows = list(sg.generate(PFX + '''GENERATE { ex:s ex:v ?v } WHERE { VALUES ?v { 1 2 } MINUS { VALUES ?v { 2 } } }'''))
    assert [r[2] for r in rows] == ['"1"^^<http://www.w3.org/2001/XMLSchema#integer>']


def test_triple_patterns_without_dataset_is_an_error():
    with pytest.raises(sg.CompileError):
        sg.Execution(PFX + "GENERATE { ?s ex:q ?o } WHERE { ?s ex:p ?o }")


def test_negated_property_sets_with_inverse_members():
    # rdflib cannot parse ^ inside !( ): compared with equivalent patterns evaluated by rdflib
    pairs = [
        ("?s !(^ex:knows) ?o", "?o ?p ?s FILTER(?p != ex:knows)"),
        ("?s !(ex:p|^ex:knows|ex:knows|a) ?o",
         "{ ?s ?p ?o FILTER(?p NOT IN (ex:p, ex:knows, rdf:type)) } UNION { ?o ?p ?s FILTER(?p != ex:knows) }"),
    ]
    for native_pattern, reference in pairs:
        native = run(PFX + f"GENERATE {{ ?s ex:q ?o }} WHERE {{ {native_pattern} }}", True)
        expected = run(PFX + "PREFIX rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#>\n"
                       f"GENERATE {{ ?s ex:q ?o }} WHERE {{ {reference} }}", False)
        assert native == expected and native
