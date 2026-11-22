import json
import os

import pytest
import rdflib
from rdflib.compare import isomorphic

import sparql_generate as sg

PFX = """PREFIX ex: <http://ex.org/>
PREFIX iter: <http://w3id.org/sparql-generate/iter/>
PREFIX fun: <http://w3id.org/sparql-generate/fn/>
PREFIX xsd: <http://www.w3.org/2001/XMLSchema#>
"""


def graph(nt: str) -> rdflib.Graph:
    g = rdflib.Graph()
    g.parse(data=nt, format="nt")
    return g


def test_csv_in_memory_document(tmp_path):
    csv = "id,name,lat\n1,Alice,45.1\n2,,46.2\n3,Carol,\n"
    q = PFX + 'GENERATE { <http://ex.org/p/{ ?id }> ex:name ?name ; ex:lat "{?lat}"^^xsd:decimal . } ITERATOR iter:CSV(<http://ex.org/d.csv>) AS ?id ?name ?lat'
    g = sg.to_graph(q, documents={"http://ex.org/d.csv": csv})
    assert len(g) == 4  # empty cells are unbound: the triple is dropped
    assert (rdflib.URIRef("http://ex.org/p/2"), rdflib.URIRef("http://ex.org/name"), None) not in g
    p = tmp_path / "d.csv"
    p.write_text(csv)
    rows = list(sg.generate(q, documents={"http://ex.org/d.csv": str(p)}))
    assert len(rows) == 4


def test_json_iterator_and_functions():
    doc = json.dumps({"items": [{"id": 1, "tags": ["a", "b"], "v": 2.5}, {"id": 2, "tags": [], "v": True}]})
    q = PFX + '''GENERATE {
      <http://ex.org/i/{?id}> ex:v ?v ; ex:tag ?tag ; ex:pos ?pos .
    }
    SOURCE <http://ex.org/d.json> AS ?src
    ITERATOR iter:JSONPath(?src, "$.items[*]", "$.id", "$.v") AS ?item ?id ?v ?pos ?hasNext
    ITERATOR iter:JSONPath(?item, "$.tags[*]") AS ?tag'''
    g = sg.to_graph(q, documents={"http://ex.org/d.json": doc})
    exp = graph('''<http://ex.org/i/1> <http://ex.org/v> "2.5"^^<http://www.w3.org/2001/XMLSchema#decimal> .
    <http://ex.org/i/1> <http://ex.org/tag> "a" .
    <http://ex.org/i/1> <http://ex.org/tag> "b" .
    <http://ex.org/i/1> <http://ex.org/pos> "0"^^<http://www.w3.org/2001/XMLSchema#integer> .''')
    assert isomorphic(g, exp)
    # fun:JSONPath with a filter (Python fallback) and a native path
    q2 = PFX + 'GENERATE { ex:s ex:a ?a ; ex:b ?b } SOURCE <http://ex.org/d.json> AS ?src WHERE { BIND(fun:JSONPath(?src, "$.items[?(@.id==2)].v") AS ?a) BIND(fun:JSONPath(?src, "$.items[0].id") AS ?b) }'
    rows = sorted(sg.generate(q2, documents={"http://ex.org/d.json": doc}))
    assert rows == [("<http://ex.org/s>", "<http://ex.org/a>", '"true"^^<http://www.w3.org/2001/XMLSchema#boolean>'),
                    ("<http://ex.org/s>", "<http://ex.org/b>", '"1"^^<http://www.w3.org/2001/XMLSchema#integer>')]


def test_xpath_nested_generate_and_bnode_scoping():
    xml = "<r><a id='1'><b>x</b><b>y</b></a><a id='2'><b>z</b></a></r>"
    q = PFX + '''GENERATE {
      _:n ex:id ?id .
      GENERATE { _:n ex:b ?b } ITERATOR iter:XPath(?a, "/a/b/text()") AS ?b .
    } ITERATOR iter:XPath(<http://ex.org/d.xml>, "//a", "/a/@id") AS ?a ?id'''
    g = sg.to_graph(q, documents={"http://ex.org/d.xml": xml})
    subjects = {s for s in g.subjects()}
    assert len(subjects) == 2  # one blank node per solution of the owner level
    for s in subjects:
        n = len(list(g.objects(s, rdflib.URIRef("http://ex.org/b"))))
        assert n in (1, 2)


def test_unbound_and_errors_drop_triples():
    q = PFX + 'GENERATE { ex:s ex:p ?x ; ex:q ?y ; ex:r ?z } WHERE { VALUES ?x { 1 } BIND(xsd:integer("abc") AS ?y) BIND(IRI("not a valid iri") AS ?z) }'
    rows = list(sg.generate(q))
    assert rows == [("<http://ex.org/s>", "<http://ex.org/p>", '"1"^^<http://www.w3.org/2001/XMLSchema#integer>')]
    # literal subject: ill-formed triple dropped (SPARQL 1.1 §16.2)
    assert list(sg.generate(PFX + 'GENERATE { ?x ex:p ex:o } WHERE { BIND("lit" AS ?x) }')) == []


def test_nested_generate_is_evaluated_once_on_all_parent_solutions():
    # rule (G) of the semantics (decision of 2026-11-07): LIMIT, DISTINCT and aggregates of a
    # nested GENERATE apply across the parent solutions, as in the Java engine
    q = PFX + 'GENERATE { GENERATE { <http://ex.org/s{STR(?i)}> ex:q ?j } ITERATOR iter:for(1,1,4) AS ?j WHERE {} ORDER BY ?i ?j LIMIT 1 . } WHERE { VALUES ?i { 1 2 } }'
    rows = list(sg.generate(q))
    assert rows == [("<http://ex.org/s1>", "<http://ex.org/q>", '"1"^^<http://www.w3.org/2001/XMLSchema#integer>')]
    q = PFX + 'GENERATE { GENERATE { ex:c ex:n ?{ COUNT(?j) } } ITERATOR iter:for(1,1,3) AS ?j WHERE {} . } WHERE { VALUES ?i { 1 2 } }'
    # iter:for(1,1,3) yields 1 and 2 (the bound is exclusive): 2 parents x 2 = 4
    assert list(sg.generate(q)) == [("<http://ex.org/c>", "<http://ex.org/n>", '"4"^^<http://www.w3.org/2001/XMLSchema#integer>')]
    # per-parent aggregation is obtained by grouping on the parent's variables
    q = PFX + 'GENERATE { GENERATE { <http://ex.org/s{STR(?i)}> ex:n ?{ COUNT(?j) } } ITERATOR iter:for(1,1,3) AS ?j WHERE {} GROUP BY ?i . } WHERE { VALUES ?i { 1 2 } }'
    assert sorted(r[2] for r in sg.generate(q)) == ['"2"^^<http://www.w3.org/2001/XMLSchema#integer>'] * 2


def test_list_builds_one_rdf_list():
    q = 'BASE <http://ex.org/> GENERATE { <l> <has> LIST(?i) } WHERE { VALUES ?i { 1 2 3 } }'
    g = sg.to_graph(q)
    head = g.value(rdflib.URIRef("http://ex.org/l"), rdflib.URIRef("http://ex.org/has"))
    items = list(rdflib.collection.Collection(g, head))
    assert [int(i) for i in items] == [1, 2, 3]


def test_named_query_call(tmp_path):
    (tmp_path / "q2.rqg").write_text('BASE <http://ex.org/> GENERATE <q2.rqg>(?s, ?i) { ?s <p{STR(?i)}{STR(?j)}> "in" } WHERE { VALUES ?j { 1 2 } }')
    q = 'BASE <http://ex.org/> GENERATE { ?bn <p{STR(?i)}> "out" . GENERATE <q2.rqg>(?bn, ?i) . } WHERE { VALUES ?i { 1 2 } BIND(BNODE("bn") AS ?bn) }'
    g = sg.to_graph(q, base_dir=str(tmp_path), named_queries={"http://ex.org/q2.rqg": str(tmp_path / "q2.rqg")})
    assert len(g) == 6 and len(set(g.subjects())) == 2


def test_template_and_select():
    t = sg.template(PFX + 'TEMPLATE { before = "["; "{?x}" ; separator = ", " ; after = "]" } WHERE { VALUES ?x { 1 2 3 } } ORDER BY DESC(?x)')
    assert t == "[3, 2, 1]"
    rows = sg.select(PFX + 'SELECT ?y (CONCAT(?a, "!") AS ?z) WHERE { VALUES (?a ?y) { ("hi" 1) } }')
    assert rows == [{"y": '"1"^^<http://www.w3.org/2001/XMLSchema#integer>', "z": '"hi!"'}]


def test_where_on_rdf_dataset():
    ds = rdflib.Dataset(default_union=False)
    ds.default_context.parse(data="<http://ex.org/a> <http://ex.org/p> 1 . <http://ex.org/b> <http://ex.org/p> 2 .", format="turtle")
    q = PFX + 'GENERATE { ?s ex:q ?o2 } ITERATOR iter:for(10,10,21) AS ?k WHERE { ?s ex:p ?o . BIND(?o + ?k AS ?o2) FILTER(?o > 1) }'
    rows = sorted(sg.generate(q, dataset=ds))
    assert [r[2] for r in rows] == ['"12"^^<http://www.w3.org/2001/XMLSchema#integer>', '"22"^^<http://www.w3.org/2001/XMLSchema#integer>']


def test_generate_to_file(tmp_path):
    out = tmp_path / "o.nt"
    n = sg.generate_to('BASE <http://ex.org/> GENERATE { <s> <p> ?i } WHERE { VALUES ?i { 1 2 } }', str(out))
    assert n == 2 and out.read_text().count(" .\n") == 2
    out2 = tmp_path / "o.ttl"
    sg.generate_to('BASE <http://ex.org/> GENERATE { <s> <p> ?i } WHERE { VALUES ?i { 1 2 } }', str(out2), format="ttl")
    g = rdflib.Graph()
    g.parse(str(out2), format="turtle")
    assert len(g) == 2


def test_simple_levels_are_not_materialised():
    # rule (MAT): a level whose row id is read by nothing (no nested query, LIST, template
    # blank node or BNODE()) and whose output is one statement (one non-constant triple
    # pattern) is not written to a temporary table
    csv = "id,name,city\n1,Alice,Lyon\n2,,Paris\n3,Carol,\n"
    docs = {"http://ex.org/d.csv": csv}
    q = PFX + '''GENERATE { <http://ex.org/p/{ ?id }> a ex:Person ; ex:name ?name ; ex:city <http://ex.org/c/{ ?city }> .
                  ex:dataset ex:size "3" . }
    ITERATOR iter:CSV(<http://ex.org/d.csv>, "id", "name", "city") AS ?id ?name ?city'''
    with sg.Execution(q, documents=docs) as ex:
        assert ex.compiler.materialise_count == 1  # three statements read the relation
        rows = sorted(ex.triples())
    one = PFX + 'GENERATE { <http://ex.org/p/{ ?id }> ex:name ?name . ex:dataset ex:size "3" . } ITERATOR iter:CSV(<http://ex.org/d.csv>, "id", "name") AS ?id ?name'
    with sg.Execution(one, documents=docs) as ex:
        assert ex.compiler.materialise_count == 0
        assert len(list(ex.triples())) == 3
    T = "<http://www.w3.org/1999/02/22-rdf-syntax-ns#type>"
    assert rows == sorted([
        ("<http://ex.org/dataset>", "<http://ex.org/size>", '"3"'),
        ("<http://ex.org/p/1>", T, "<http://ex.org/Person>"),
        ("<http://ex.org/p/2>", T, "<http://ex.org/Person>"),
        ("<http://ex.org/p/3>", T, "<http://ex.org/Person>"),
        ("<http://ex.org/p/1>", "<http://ex.org/name>", '"Alice"'),
        ("<http://ex.org/p/3>", "<http://ex.org/name>", '"Carol"'),
        ("<http://ex.org/p/1>", "<http://ex.org/city>", "<http://ex.org/c/Lyon>"),
        ("<http://ex.org/p/2>", "<http://ex.org/city>", "<http://ex.org/c/Paris>"),
    ])
    # the parent of a nested GENERATE is materialised (rule SUB reads its row id), its leaf is not
    q = PFX + '''GENERATE { <http://ex.org/p/{ ?id }> a ex:Person .
                  GENERATE { <http://ex.org/p/{ ?id }> ex:tag ?t . } ITERATOR iter:Split(?name, "i") AS ?t . }
    ITERATOR iter:CSV(<http://ex.org/d.csv>, "id", "name") AS ?id ?name'''
    with sg.Execution(q, documents=docs) as ex:
        assert ex.compiler.materialise_count == 1
        assert len(list(ex.triples())) == 3 + 3  # "Al", "ce", "Carol"; ?name of row 2 is unbound
    # blank nodes and BNODE() read the row id: materialised
    for gen in ('_:b ex:id ?id ; ex:name ?name', '?{ BNODE() } ex:id ?id . ex:s ex:name ?name'):
        with sg.Execution(PFX + f'GENERATE {{ {gen} }} ITERATOR iter:CSV(<http://ex.org/d.csv>, "id", "name") AS ?id ?name',
                          documents=docs) as ex:
            assert ex.compiler.materialise_count == 1
            assert len(set(s for s, p, o in ex.triples() if p == "<http://ex.org/id>")) == 3


def test_bnode_str_reading_r1():
    # BNODE(s): one blank node per string and input row of the operator that evaluates it
    # (reading R1 of the semantics, SPARQL's as Jena reads it; outputs of the Java engine
    # 2.2.2 on the same queries)
    def nodes(q, ds=None):
        return [o for s, p, o in sorted(sg.generate(PFX + q, dataset=ds))]

    # two operators of one solution: two blank nodes
    x, y = nodes('GENERATE { ex:s ex:x ?x ; ex:y ?y } WHERE { BIND(BNODE("a") AS ?x) BIND(BNODE("a") AS ?y) }')
    assert x != y
    x, y = nodes('GENERATE { ex:s ex:x ?x ; ex:y ?y } BIND(BNODE("a") AS ?x) BIND(BNODE("a") AS ?y)')
    assert x != y
    assert nodes('GENERATE { ex:s ex:p ?x } WHERE { BIND(BNODE("a") AS ?x) FILTER(?x = BNODE("a")) }') == []
    rows = sorted(sg.generate(PFX + 'GENERATE { ?x ex:p ?{BNODE("a")} } WHERE { BIND(BNODE("a") AS ?x) }'))
    assert rows[0][0] != rows[0][2]
    # one operator, one row: one blank node
    assert nodes('GENERATE { ex:s ex:x ?x } WHERE { BIND(sameTerm(BNODE("a"), BNODE("a")) AS ?x) }') == \
        ['"true"^^<http://www.w3.org/2001/XMLSchema#boolean>']
    (s, p, o), = sg.generate(PFX + 'GENERATE { ?{BNODE("a")} ex:p ?{BNODE("a")} } WHERE { }')
    assert s == o
    # rows derived from one input row share its node; duplicate rows get their own
    assert len(set(r[0] for r in sg.generate(PFX + 'GENERATE { ?x ex:p ?i } BIND(BNODE("a") AS ?x) ITERATOR iter:for(1,1,3) AS ?i'))) == 1
    assert len(set(nodes('GENERATE { ex:s ex:p ?x } WHERE { VALUES ?i { 1 1 } BIND(BNODE("a") AS ?x) }'))) == 2
    # the same in a WHERE compiled to joins (rule BGP)
    ds = rdflib.Dataset(default_union=False)
    ds.default_context.parse(data="<http://ex.org/a> <http://ex.org/p> 1 .", format="turtle")
    q = 'GENERATE { ?s ex:x ?x ; ex:y ?y ; ex:z ?z } WHERE { ?s ex:p ?o BIND(BNODE("a") AS ?x) BIND(BNODE("a") AS ?y) BIND(IF(sameTerm(?x, BNODE("a")), 1, 0) AS ?z) }'
    x, y, z = nodes(q, ds)
    assert x != y and z == '"0"^^<http://www.w3.org/2001/XMLSchema#integer>'


def test_arrow_outputs():
    q = 'BASE <http://ex.org/> GENERATE { <s> <p> ?i . <s> <q> "{?i}" } WHERE { VALUES ?i { 1 2 3 } }'
    with sg.Execution(q) as ex:
        t = ex.arrow()
        assert t.column_names == ["s", "p", "o"] and t.num_rows == 6
        batches = list(ex.batches(batch_size=2))
        assert sum(b.num_rows for b in batches) == 6 and all(b.num_rows <= 2 for b in batches)
        assert sorted(ex.triples(batch_size=4)) == sorted(zip(*(t.column(i).to_pylist() for i in range(3))))
        assert ex.arrow(distinct=True).num_rows == 6
    with sg.Execution('SELECT ?x WHERE { VALUES ?x { 1 } }') as ex:
        with pytest.raises(TypeError):
            ex.arrow()


def test_empty_templates_and_projections():
    # an empty GENERATE template (RMLTC0007h of the RML test cases), with or without solutions
    for q in ('GENERATE { } WHERE { }', 'GENERATE { } WHERE { VALUES ?x { 1 } }',
              PFX + 'GENERATE { } ITERATOR iter:for(1, 1, 3) AS ?i', 'GENERATE { GENERATE { } WHERE { } . } WHERE { VALUES ?x { 1 2 } }'):
        assert list(sg.generate(q)) == []
    assert sg.select('SELECT * WHERE { }') == [{}]
    assert sg.template('TEMPLATE { } WHERE { }') == ""


def test_csv_column_absent_from_the_header_is_unbound():
    # as the Java engine 2.2.2: the variable is unbound, the other columns are read
    csv = "id,name\n1,a\n2,b\n"
    q = PFX + 'GENERATE { <http://e/s{?id}> ex:p ?name ; ex:q ?nope } ITERATOR iter:CSV(<http://ex.org/d.csv>, "id", "nope", "name") AS ?id ?nope ?name'
    with sg.Execution(q, documents={"http://ex.org/d.csv": csv}) as ex:
        assert sorted(ex.triples()) == [("<http://e/s1>", "<http://ex.org/p>", '"a"'), ("<http://e/s2>", "<http://ex.org/p>", '"b"')]
        assert any("nope" in w for w in ex.warnings)
