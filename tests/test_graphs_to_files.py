"""--graphs-to-files and --targets (the Java command line 2.6.0): named graphs written to the
files they name; the cases of the Java engine's GraphsToFilesTest, and the resolution of
graph names by targets.GraphTargets."""
import bz2
import gzip
import io
import json
import lzma
import os
import tarfile
import zipfile

import pytest
import rdflib

from sparql_generate import cli
from sparql_generate.targets import GraphTargets, TargetError, percent_decode

from quadsutil import datasets_isomorphic

BASE = "http://ex/base/"


def query(file: str) -> str:
    """Triples in the default graph, in a graph that names a file, in a graph that does
    not, and in a graph of a file."""
    return (f"BASE <{BASE}> PREFIX ex: <http://ex/> PREFIX ite: <http://w3id.org/sparql-generate/iter/> "
            f"GENERATE {{ ex:s ex:p ?i . GRAPH <{file}> {{ ex:s ex:q ?i }} GRAPH ex:g {{ ex:s ex:r ?i }} }} "
            "ITERATOR ite:for(1, 1, 3) AS ?i")


MAIN = "<http://ex/s> <http://ex/p> 1, 2 . <http://ex/g> { <http://ex/s> <http://ex/r> 1, 2 }"
FILE = "<http://ex/s> <http://ex/q> 1, 2 ."


def nquads(text: str, fmt: str) -> str:
    ds = rdflib.Dataset(default_union=False)
    ds.parse(data=text, format=fmt)
    return ds.serialize(format="nquads")


def assert_dataset(expected_trig: str, actual: str, fmt: str = "nquads"):
    ok, detail = datasets_isomorphic(nquads(actual, fmt), nquads(expected_trig, "trig"))
    assert ok, detail


class Run:
    def __init__(self, code, out, err):
        self.code, self.out, self.err = code, out, err


@pytest.fixture
def run(tmp_path, monkeypatch, capsys):
    """The command line in the working directory tmp_path (also the current directory)."""
    monkeypatch.chdir(tmp_path)

    def run(q, *args):
        (tmp_path / "query.rqg").write_text(q, encoding="utf-8")
        capsys.readouterr()
        code = cli.main(["-d", str(tmp_path), *args])
        out, err = capsys.readouterr()
        return Run(code, out, err)

    return run


def read(tmp_path, name, fmt):
    return (tmp_path / name).read_text(encoding="utf-8"), fmt


@pytest.mark.parametrize("args", [(), ("-fo", "NQUADS"), ("-fo", "TRIG")])
def test_named_graph_to_file(run, tmp_path, args):
    r = run(query("out/people.nq"), "-b", BASE, "--graphs-to-files", *args)
    assert r.code == 0, r.err
    assert_dataset(MAIN, r.out, "trig" if "TRIG" in args else "nquads")
    assert_dataset(FILE, (tmp_path / "out/people.nq").read_text())


@pytest.mark.parametrize("graph", ["people", "http://ex/elsewhere.nq", "../up.nq"])
def test_other_graphs_stay_in_the_output(run, tmp_path, graph):
    # a graph name relative to the base without RDF extension stays a graph; an absolute
    # graph name too, and one resolved outside of the folder
    r = run(query(graph), "-b", BASE, "--graphs-to-files", "-fo", "NQUADS")
    assert r.code == 0, r.err
    assert "<http://ex/q>" in r.out
    assert not (tmp_path.parent / "up.nq").exists() and not (tmp_path / "people").exists()


def test_without_the_flag_nothing_changes(run, tmp_path):
    r = run(query("people.nq"), "-b", BASE, "-fo", "NQUADS")
    assert r.code == 0 and "<http://ex/base/people.nq>" in r.out
    assert not (tmp_path / "people.nq").exists()


@pytest.mark.parametrize("name,fmt", [("f.nt", "nt"), ("f.ttl", "turtle"), ("f.trig", "trig"), ("f.n3", "n3"),
                                      ("f.jsonld", "json-ld"), ("f.rdf", "xml"), ("f.rdfxml", "xml"),
                                      ("f.rj", "rj"), ("f.rdfjson", "rj")])
def test_formats_by_extension(run, tmp_path, name, fmt):
    r = run(query(name), "-b", BASE, "--graphs-to-files")
    assert r.code == 0, r.err
    text = (tmp_path / name).read_text(encoding="utf-8")
    if fmt == "rj":
        data = json.loads(text)
        assert list(data) == ["http://ex/s"] and list(data["http://ex/s"]) == ["http://ex/q"]
        objs = data["http://ex/s"]["http://ex/q"]
        assert sorted(o["value"] for o in objs) == ["1", "2"]
        assert all(o["type"] == "literal" and o["datatype"].endswith("#integer") for o in objs)
        return
    assert_dataset(FILE, text, fmt)


def test_n3_uses_at_prefix(run, tmp_path):
    q = query("f.n3").replace("PREFIX ex: <http://ex/>", "PREFIX ex: <http://ex/> PREFIX q: <http://ex/q>")
    r = run(q, "-b", BASE, "--graphs-to-files")
    assert r.code == 0, r.err
    n3 = (tmp_path / "f.n3").read_text()
    assert "PREFIX" not in n3 and "@prefix" in n3


def decompress(name: str, data: bytes, inner: str) -> bytes:
    if name.endswith(".zip"):
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            assert z.namelist() == [inner]
            return z.read(inner)
    if ".tar." in name:
        with tarfile.open(fileobj=io.BytesIO(data)) as t:
            members = t.getmembers()
            assert [m.name for m in members] == [inner]
            return t.extractfile(members[0]).read()
    if name.endswith(".gz"):
        return gzip.decompress(data)
    if name.endswith(".bz2"):
        return bz2.decompress(data)
    return lzma.decompress(data)


@pytest.mark.parametrize("name", ["c.nq.gz", "c.nq.zip", "c.nq.tar.gz", "c.nq.tar.xz", "c.nq.bz2", "c.nq.xz",
                                  "c.ttl.tar.bz2"])
def test_compressions_by_extension(run, tmp_path, name):
    r = run(query(name), "-b", BASE, "--graphs-to-files")
    assert r.code == 0, r.err
    inner = "c.ttl" if name.endswith(".ttl.tar.bz2") else "c.nq"
    content = decompress(name, (tmp_path / name).read_bytes(), inner).decode("utf-8")
    assert_dataset(FILE, content, "turtle" if inner.endswith(".ttl") else "nquads")
    assert not [f for f in os.listdir(tmp_path) if f.endswith(".tmp")]  # no temporary file left


def test_named_graphs_inside_a_file(run, tmp_path):
    # GRAPH <dump.nq#g=IRI>: the graph IRI in the file dump.nq; the IRI can be
    # percent-encoded, as the expressions embedded in an IRI give it
    q = (f"BASE <{BASE}> PREFIX ex: <http://ex/> PREFIX ite: <http://w3id.org/sparql-generate/iter/> "
         "GENERATE { GRAPH <dump.nq> { ex:s ex:p ?i } GRAPH <dump.nq#g=http://ex/g1> { ex:s ex:q ?i } "
         "GRAPH <dump.nq#g={STR(?g)}> { ex:s ex:r ?i } GRAPH <dump.trig#g=http://ex/g%23a%2525b> { ex:s ex:p ?i } } "
         'ITERATOR ite:for(1, 1, 3) AS ?i BIND(IRI(CONCAT("http://ex/h?x=", STR(?i), "#f")) AS ?g)')
    r = run(q, "-b", BASE, "--graphs-to-files", "-fo", "NQUADS")
    assert r.code == 0, r.err
    assert r.out.strip() == ""
    assert_dataset("<http://ex/s> <http://ex/p> 1, 2 . <http://ex/g1> { <http://ex/s> <http://ex/q> 1, 2 } "
                   "<http://ex/h?x=1#f> { <http://ex/s> <http://ex/r> 1 } <http://ex/h?x=2#f> { <http://ex/s> <http://ex/r> 2 }",
                   (tmp_path / "dump.nq").read_text())
    assert_dataset("<http://ex/g#a%25b> { <http://ex/s> <http://ex/p> 1, 2 }", (tmp_path / "dump.trig").read_text(),
                   "trig")


def test_named_graph_in_a_triples_only_file_fails(run):
    r = run(query("dump.ttl#g=http://ex/g1"), "-b", BASE, "--graphs-to-files")
    assert r.code == 1 and "which Turtle cannot hold" in r.err


@pytest.mark.parametrize("graph", ["dump.nq#other", "dump.nq#g=relative", "dump.nq#g="])
def test_invalid_fragment_fails(run, graph):
    r = run(query(graph), "-b", BASE, "--graphs-to-files")
    assert r.code == 1 and "error" in r.err


@pytest.mark.parametrize("graph", ["%2E%2E/evil.nq", "a/%2e%2e/%2E%2E/evil.nq", "a//evil.nq", "%2Fevil.nq",
                                   "a%5C..%5Cevil.nq", "a/%2E/evil.nq"])
def test_unsafe_paths_fail(run, tmp_path, graph):
    # a graph name can come from the data: a path that would leave the folder is refused,
    # encoded or not
    r = run(query(graph), "-b", BASE, "--graphs-to-files")
    assert r.code == 1 and ("Unsafe target path" in r.err or "outside" in r.err), r.err
    assert not (tmp_path.parent / "evil.nq").exists()


def test_unsafe_path_from_the_data_fails(run, tmp_path):
    q = (f"BASE <{BASE}> PREFIX ex: <http://ex/> "
         'GENERATE { GRAPH ?g { ex:s ex:p ?x } } WHERE { VALUES ?x { "ok" "%2E%2E/evil" } '
         'BIND(IRI(CONCAT(?x, ".nq")) AS ?g) }')
    r = run(q, "-b", BASE, "--graphs-to-files", "-fo", "NQUADS")
    assert r.code == 1 and "Unsafe target path" in r.err
    assert not (tmp_path.parent / "evil.nq").exists()


def test_file_base(run, tmp_path):
    # with a file: base, the files are written in its folder
    out = tmp_path / "elsewhere"
    out.mkdir()
    r = run(query("people.ttl").replace(f"BASE <{BASE}> ", ""), "-b", out.as_uri() + "/", "--graphs-to-files",
            "-fo", "NQUADS")
    assert r.code == 0, r.err
    assert_dataset(FILE, (out / "people.ttl").read_text(), "turtle")
    assert not (tmp_path / "people.ttl").exists()


def test_no_base(run, tmp_path):
    # without base, the relative IRIs of the query are resolved against the current
    # directory: the files are written there
    r = run(query("people.ttl").replace(f"BASE <{BASE}> ", ""), "--graphs-to-files", "-fo", "NQUADS")
    assert r.code == 0, r.err
    assert_dataset(FILE, (tmp_path / "people.ttl").read_text(), "turtle")


@pytest.mark.parametrize("args", [("-fo", "NT"), ("-fo", "TTL")])
def test_triples_only_output_when_every_graph_goes_to_a_file(run, tmp_path, args):
    q = f"BASE <{BASE}> PREFIX ex: <http://ex/> GENERATE {{ ex:s ex:p 1 . GRAPH <people.nq> {{ ex:s ex:q 2 }} }}"
    r = run(q, "-b", BASE, "--graphs-to-files", *args)
    assert r.code == 0, r.err
    assert_dataset("<http://ex/s> <http://ex/p> 1 .", r.out, "turtle")
    assert_dataset("<http://ex/s> <http://ex/q> 2 .", (tmp_path / "people.nq").read_text())
    # and refused when a named graph reaches the output
    r = run(query("people.nq"), "-b", BASE, "--graphs-to-files", *args)
    assert r.code == 1 and "cannot hold" in r.err


def test_targets_description(run, tmp_path):
    # the description of the targets: encoding, format of a file without extension, compression
    (tmp_path / "targets.json").write_text(json.dumps({
        "./dump1.nq": {"encoding": "UTF-16"},
        "data/dump2": {"format": "Turtle", "compression": "gzip"},
        "dump3.out": {"format": "application/n-triples"}}))
    q = (f"BASE <{BASE}> PREFIX ex: <http://ex/> "
         'GENERATE { GRAPH <dump1.nq> { ex:s ex:p "é" } GRAPH <data/dump2> { ex:s ex:q 2 } '
         "GRAPH <dump3.out> { ex:s ex:r 3 } }")
    r = run(q, "-b", BASE, "--targets", str(tmp_path / "targets.json"), "-fo", "NQUADS")
    assert r.code == 0, r.err
    utf16 = (tmp_path / "dump1.nq").read_bytes()
    assert utf16[:2] == b"\xfe\xff"
    assert_dataset('<http://ex/s> <http://ex/p> "é" .', utf16.decode("utf-16"))
    assert_dataset("<http://ex/s> <http://ex/q> 2 .", gzip.decompress((tmp_path / "data/dump2").read_bytes()).decode(),
                   "turtle")
    assert_dataset("<http://ex/s> <http://ex/r> 3 .", (tmp_path / "dump3.out").read_text(), "nt")
    # the same with the configuration file, relative to the working directory
    (tmp_path / "sparql-generate-conf.json").write_text('{ "targets": "targets.json" }')
    (tmp_path / "dump1.nq").unlink()
    r = run(q, "-b", BASE, "-fo", "NQUADS")
    assert r.code == 0, r.err
    assert (tmp_path / "dump1.nq").exists()


def test_targets_relative_to_the_current_directory(run, tmp_path, monkeypatch):
    (tmp_path / "t.json").write_text(json.dumps({"x.out": {"format": "nq"}}))
    q = f"BASE <{BASE}> PREFIX ex: <http://ex/> GENERATE {{ GRAPH <x.out> {{ ex:s ex:p 1 }} }}"
    r = run(q, "-b", BASE, "--targets", "t.json")
    assert r.code == 0, r.err
    assert (tmp_path / "x.out").exists()


def test_invalid_targets_description_fails(run, tmp_path):
    (tmp_path / "targets.json").write_text('{ "../x.nq": {} }')
    r = run(query("x.nq"), "-b", BASE, "--targets", str(tmp_path / "targets.json"))
    assert r.code == 1
    (tmp_path / "targets.json").write_text('{ "x.nq": { "compression": "rar" } }')
    r = run(query("x.nq"), "-b", BASE, "--targets", str(tmp_path / "targets.json"))
    assert r.code == 1 and "Unknown compression" in r.err
    (tmp_path / "targets.json").write_text('{ "x.nq": { "encoding": "no-such-charset" } }')
    r = run(query("x.nq"), "-b", BASE, "--targets", str(tmp_path / "targets.json"))
    assert r.code == 1 and "Unknown character encoding" in r.err


def test_no_triple_no_file(run, tmp_path):
    # a file is created at its first triple: a graph without triples gives no file
    q = f"BASE <{BASE}> PREFIX ex: <http://ex/> GENERATE {{ GRAPH <empty.nq> {{ ex:s ex:p ?x }} }} WHERE {{ VALUES ?x {{ UNDEF }} }}"
    r = run(q, "-b", BASE, "--graphs-to-files")
    assert r.code == 0, r.err
    assert not (tmp_path / "empty.nq").exists()


def test_raise_writes_no_file(run, tmp_path):
    # the solutions are computed before anything is written: RAISE stops the execution
    # before the files are written (the Java engine writes the files of the triples generated
    # before the error)
    q = (f"BASE <{BASE}> PREFIX ex: <http://ex/> "
         "GENERATE { GENERATE { GRAPH <part.nq.gz> { ex:s ex:p 1 } } . "
         'GENERATE { ex:s ex:p ?y } WHERE { BIND(RAISE("stop") AS ?y) } . }')
    r = run(q, "-b", BASE, "--graphs-to-files", "-fo", "NQUADS")
    assert r.code == 1 and "stop" in r.err


def test_api(tmp_path):
    import sparql_generate as sg

    with sg.Execution(query("people.nq.gz"), base=BASE) as ex:
        written = ex.graphs_to_files(output_dir=str(tmp_path))
        assert written == {str(tmp_path / "people.nq.gz"): 2}
        quads = sorted(ex.quads())
        assert [g for *_, g in quads] == [None, None, "<http://ex/g>", "<http://ex/g>"]
        assert len(list(ex.triples())) == 2
    assert_dataset(FILE, gzip.decompress((tmp_path / "people.nq.gz").read_bytes()).decode())
    with sg.Execution(query("people.trig"), base=BASE) as ex:
        ex.graphs_to_files(targets={"people.trig": {"format": "N-Triples"}}, output_dir=str(tmp_path))
    assert_dataset(FILE, (tmp_path / "people.trig").read_text(), "nt")


# -- GraphTargets alone ------------------------------------------------------------------------


def test_resolution(tmp_path):
    t = GraphTargets(BASE + "query.rqg", str(tmp_path))
    assert t.folder_iri == BASE
    assert t.resolve("<http://ex/other/a.nq>") is None
    assert t.resolve(f"<{BASE}a>") is None
    assert t.resolve(f"<{BASE}a.nq?x=1>") is None
    assert t.resolve(f"<{BASE}>") is None
    assert t.resolve("_:b0") is None
    f, g = t.resolve(f"<{BASE}sub/a%20b.nq.gz>")
    assert f.path == str(tmp_path / "sub" / "a b.nq.gz") and f.lang == "nq" and f.compression == "gzip"
    assert g is None
    f, g = t.resolve(f"<{BASE}a.trig#g=http%3A%2F%2Fex%2Fg%C3%A9>")
    assert g == "http://ex/gé" and f.lang == "trig"
    assert t.resolve(f"<{BASE}a.trig>")[0] is f  # the same file for the same path


def test_percent_decoding():
    assert percent_decode("a%20b/%C3%A9%23%25", "x") == "a b/é#%"
    with pytest.raises(TargetError):
        percent_decode("a%2", "x")


def test_folder_of_a_file_base(tmp_path):
    t = GraphTargets((tmp_path / "q.rqg").as_uri(), "/nonexistent")
    assert t.folder == str(tmp_path)
    t = GraphTargets(None, str(tmp_path))
    assert t.folder == str(tmp_path) and t.folder_iri == tmp_path.as_uri() + "/"
