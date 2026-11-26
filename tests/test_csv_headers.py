"""iter:CSVHeaders: the cells of the header line of a CSV document, given as text or by its
IRI, of which only the header line is read (the Java engine 2.6.0, CSVHeadersTest)."""
import gzip

import sparql_generate as sg

P = "PREFIX iter: <http://w3id.org/sparql-generate/iter/> "
CSV = 'id,"full name",city\n1,Ada,London\n2,Alan,"Wilmslow"\n'


def headers(source, **kw):
    rows = sg.select(P + f"SELECT ?h ITERATOR iter:CSVHeaders({source}) AS ?h WHERE {{ }}", **kw)
    return [r["h"] for r in rows]


def test_of_text():
    assert headers('"id,\\"full name\\",city\\n1,Ada,London"') == ['"id"', '"full name"', '"city"']


def test_of_iri():
    assert headers("<http://ex/people.csv>", documents={"http://ex/people.csv": CSV}) == \
        ['"id"', '"full name"', '"city"']


def test_reads_the_header_line_only(tmp_path):
    # the rest of the file is not read: an unterminated quoted cell after the header, and
    # the header of a compressed file
    p = tmp_path / "people.csv"
    p.write_text('a,,"b\nc"\n1,"2\n' + "x" * 100000)
    assert headers("<http://ex/p.csv>", documents={"http://ex/p.csv": str(p)}) == ['"a"', '""', '"b\\nc"']
    gz = tmp_path / "people.csv.gz"
    gz.write_bytes(gzip.compress(("﻿id,name\n" + "1,a\n" * 1000).encode("utf-8")))
    assert headers("<http://ex/p.csv.gz>", documents={"http://ex/p.csv.gz": str(gz)}) == ['"id"', '"name"']
    u16 = tmp_path / "u16.csv"
    u16.write_bytes(b"\xff\xfe" + "é,b\n1,2\n".encode("utf-16-le"))
    assert headers("<http://ex/u16.csv>", documents={"http://ex/u16.csv": str(u16)}) == ['"é"', '"b"']


def test_missing_and_empty_documents():
    assert headers("<http://ex/missing.csv>", allow_network=False) == []
    assert headers('""') == []


def test_per_row():
    rows = sg.select(P + 'SELECT ?t ?h ITERATOR iter:Split("x,y|z", "\\\\|") AS ?t ITERATOR iter:CSVHeaders(?t) AS ?h '
                     'WHERE { }')
    assert sorted(r["h"] for r in rows) == ['"x"', '"y"', '"z"']
