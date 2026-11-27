"""iter:CSV on a document computed per row (a variable, an IRI template): the columns are
read by name in the header of each document, the position of the row follows the named
columns, an empty cell or a missing column is unbound, an unbound document gives no row."""
import sparql_generate as sg

P = "PREFIX iter: <http://w3id.org/sparql-generate/iter/> "
DOCS = {
    "http://ex/a.csv": "id,x,y\n1,a,b\n2,,c\n3,d\n",
    "http://ex/b.csv": "Y , ID,x\nq,10,r\n",
    "http://ex/e.csv": "",
}


def rows(query, **kw):
    out = sg.select(P + query, documents=DOCS, allow_network=False, **kw)
    return sorted(tuple(r.get(k) for k in sorted(r)) for r in out)


def test_named_columns_per_document():
    got = rows('SELECT ?f ?id ?x ?y ?pos ITERATOR iter:Split("a,b,e,missing", ",") AS ?f '
               'ITERATOR iter:CSV(<http://ex/{?f}.csv>, "id", "x", "y") AS ?id ?x ?y ?pos WHERE { }')
    assert got == sorted([
        ('"a"', '"1"', '"0"^^<http://www.w3.org/2001/XMLSchema#integer>', '"a"', '"b"'),
        ('"a"', '"2"', '"1"^^<http://www.w3.org/2001/XMLSchema#integer>', None, '"c"'),
        ('"a"', '"3"', '"2"^^<http://www.w3.org/2001/XMLSchema#integer>', '"d"', None),
        ('"b"', '"10"', '"0"^^<http://www.w3.org/2001/XMLSchema#integer>', '"r"', '"q"'),
    ])


def test_missing_column_and_text_documents():
    got = rows('SELECT ?t ?id ?z ITERATOR iter:Split("k,id|v,1|w,2", "&") AS ?t0 '
               'BIND(REPLACE(?t0, "\\\\|", "\\n") AS ?t) '
               'ITERATOR iter:CSV(?t, "id", "z") AS ?id ?z WHERE { }')
    assert [g[0] for g in got] == ['"1"', '"2"']
    assert all(g[2] is None for g in got)


def test_without_names_and_dialect():
    got = rows('SELECT ?a ?b ITERATOR iter:Split("x;\'y;z\'|u;v", "&") AS ?t0 '
               'BIND(REPLACE(?t0, "\\\\|", "\\n") AS ?t) '
               'ITERATOR iter:CSV(?t, false, "\'", ";", "\\n") AS ?a ?b WHERE { }')
    assert got == [('"u"', '"v"'), ('"x"', '"y;z"')]


def test_many_documents_in_one_vector():
    docs = {f"http://ex/d{i}.csv": f"n,v\n{i},{i * 2}\n{i},{i * 2 + 1}\n" for i in range(3000)}
    out = sg.select(P + 'SELECT ?n ?v ?pos ITERATOR iter:CSV(<http://ex/keys.csv>, "k") AS ?k '
                    'ITERATOR iter:CSV(<http://ex/d{?k}.csv>, "v", "n") AS ?v ?n ?pos WHERE { }',
                    documents={**docs, "http://ex/keys.csv": "k\n" + "".join(f"{i}\n" for i in range(3000))},
                    allow_network=False)
    assert len(out) == 6000
    assert {r["pos"] for r in out} == {'"0"^^<http://www.w3.org/2001/XMLSchema#integer>',
                                       '"1"^^<http://www.w3.org/2001/XMLSchema#integer>'}
    assert all(int(r["v"].strip('"')) // 2 == int(r["n"].strip('"')) for r in out)
