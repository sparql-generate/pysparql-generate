"""Batched iterators (rule BATCH): iter:CSV with a batch size and iter:JSONSurfer deliver their
solutions batch by batch, and the rest of the query is evaluated once per batch, as the Java
engine 2.2.3 documents (docs/language/generate.md): DISTINCT, GROUP BY and aggregates, ORDER BY,
LIMIT and OFFSET, LIST apply to each batch; the k-th batches of all the calls of an iterator
form one batch.  Expected values: the Java engine 2.2.3 on the same queries."""
import json

import sparql_generate as sg

P = "PREFIX iter: <http://w3id.org/sparql-generate/iter/>\nPREFIX ex: <http://ex.org/>\n"
CSV = "id,g\n1,a\n2,a\n3,b\n4,b\n5,a\n"
JSON = json.dumps([{"id": i, "g": g} for i, g in zip(range(1, 6), "aabba")])
D = {"http://ex.org/d.csv": CSV, "http://ex.org/d.json": JSON}


def col(q, var):
    return [r[var].split("^^")[0] if r[var] else r[var] for r in sg.select(P + q, documents=D)]


def test_modifiers_apply_per_batch():
    it = 'ITERATOR iter:CSV(<http://ex.org/d.csv>, 2, "id", "g") AS ?id ?g'
    assert col(f'SELECT ?id {it} WHERE {{}} LIMIT 1', "id") == ['"1"', '"3"', '"5"']
    assert col(f'SELECT (COUNT(*) AS ?n) {it} WHERE {{}}', "n") == ['"2"', '"2"', '"1"']
    assert col(f'SELECT DISTINCT ?g {it} WHERE {{}}', "g") == ['"a"', '"b"', '"a"']
    assert col(f'SELECT ?id {it} WHERE {{}} ORDER BY DESC(?id)', "id") == ['"2"', '"1"', '"4"', '"3"', '"5"']
    # without a batch size: one batch
    assert col('SELECT (COUNT(*) AS ?n) ITERATOR iter:CSV(<http://ex.org/d.csv>, "id") AS ?id WHERE {}', "n") == ['"5"']
    # JSONSurfer: the batch size follows the path; batches follow the order of the document
    it = 'ITERATOR iter:JSONSurfer(<http://ex.org/d.json>, "$[*]", 2, "$.id") AS ?e ?id'
    assert col(f'SELECT ?id {it} WHERE {{}} ORDER BY DESC(?id) LIMIT 1', "id") == ['"2"', '"4"', '"5"']


def test_two_batched_iterators_and_lists():
    q = ('SELECT ?id ?id2 ITERATOR iter:CSV(<http://ex.org/d.csv>, 2, "id") AS ?id '
         'ITERATOR iter:CSV(<http://ex.org/d.csv>, 3, "id") AS ?id2 WHERE {} LIMIT 1')
    rows = [(r["id"].split("^^")[0], r["id2"].split("^^")[0]) for r in sg.select(P + q, documents=D)]
    assert rows == [('"1"', '"1"'), ('"1"', '"4"'), ('"3"', '"1"'), ('"3"', '"4"'), ('"5"', '"1"'), ('"5"', '"4"')]
    # one RDF list per batch
    g = sg.to_graph(P + 'GENERATE { ex:l ex:has LIST(?id) } ITERATOR iter:CSV(<http://ex.org/d.csv>, 2, "id") AS ?id',
                    documents=D)
    import rdflib

    heads = set(g.objects(rdflib.URIRef("http://ex.org/l"), rdflib.URIRef("http://ex.org/has")))
    assert sorted(len(list(rdflib.collection.Collection(g, h))) for h in heads) == [1, 2, 2]


def test_order_by_compares_numbers_by_value():
    # a lexical comparison put "8" after "58" (JSON numbers and dynamic values)
    doc = json.dumps([{"s": 8}, {"s": 58}, {"s": 100}])
    q = P + 'SELECT ?s ITERATOR iter:JSONPath(<http://ex.org/n.json>, "$[*]", "$.s") AS ?e ?s WHERE {} ORDER BY DESC(?s)'
    assert [r["s"].split("^^")[0] for r in sg.select(q, documents={"http://ex.org/n.json": doc})] == ['"100"', '"58"', '"8"']


def test_select_distinct_applies_to_the_projection():
    q = 'SELECT DISTINCT ?g ITERATOR iter:CSV(<http://ex.org/d.csv>, "id", "g") AS ?id ?g WHERE {} ORDER BY ?g'
    assert col(q, "g") == ['"a"', '"b"']
