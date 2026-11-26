"""--targets on a query with many target files, compared with what the Java command line
2.6.0 wrote (tests/graphs-to-files/expected, from
``java -jar sparql-generate-2.6.0.jar -d . -b http://example.com/base/ --targets targets.json
-fo NQUADS -o main.nq``): the same files, each with the same entry name and character
encoding, and the same dataset."""
import bz2
import gzip
import io
import json
import lzma
import os
import shutil
import tarfile
import zipfile

import rdflib

from sparql_generate import cli

from quadsutil import datasets_isomorphic

HERE = os.path.join(os.path.dirname(__file__), "graphs-to-files")
EXPECTED = os.path.join(HERE, "expected")
FORMATS = {".nq": "nquads", ".nt": "nt", ".ttl": "turtle", ".trig": "trig", ".n3": "n3", ".jsonld": "json-ld",
           ".rdf": "xml", ".rj": "rj"}


def files(root):
    return sorted(os.path.relpath(os.path.join(d, f), root) for d, _, fs in os.walk(root) for f in fs)


def content(path):
    """(name of the content, its bytes): decompressed, the entry of an archive."""
    with open(path, "rb") as f:
        data = f.read()
    name = os.path.basename(path)
    if name.endswith(".zip"):
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            (entry,) = z.namelist()
            return entry, z.read(entry)
    if ".tar." in name:
        with tarfile.open(fileobj=io.BytesIO(data)) as t:
            (m,) = t.getmembers()
            return m.name, t.extractfile(m).read()
    for ext, fn in ((".gz", gzip.decompress), (".bz2", bz2.decompress), (".xz", lzma.decompress)):
        if name.endswith(ext):
            return name[:-len(ext)], fn(data)
    if data.startswith(b"\x1f\x8b"):  # gzip by the description of the targets (noext)
        return name, gzip.decompress(data)
    return name, data


def dataset(name, data, fmt):
    text = data.decode("utf-16") if data.startswith(b"\xfe\xff") else data.decode("utf-8")
    if fmt == "rj":
        return {s: {p: sorted(map(json.dumps, os_)) for p, os_ in ps.items()} for s, ps in json.loads(text).items()}
    ds = rdflib.Dataset(default_union=False)
    ds.parse(data=text, format=fmt)
    return ds.serialize(format="nquads")


def test_same_files_as_the_java_engine(tmp_path, monkeypatch):
    for f in ("query.rqg", "targets.json"):
        shutil.copy(os.path.join(HERE, f), tmp_path)
    monkeypatch.chdir(tmp_path)
    assert cli.main(["-d", ".", "-b", "http://example.com/base/", "--targets", "targets.json", "-fo", "NQUADS",
                     "-o", "main.nq"]) == 0
    got = [f for f in files(tmp_path) if f not in ("query.rqg", "targets.json")]
    assert got == files(EXPECTED)
    for f in got:
        name, data = content(tmp_path / f)
        ename, edata = content(os.path.join(EXPECTED, f))
        assert name == ename, f
        assert data[:2] == edata[:2] or not edata.startswith(b"\xfe\xff"), f  # the byte order mark
        ext = os.path.splitext(name)[1]
        fmt = FORMATS.get(ext, "nt")  # noext: N-Triples by the description of the targets
        a, b = dataset(name, data, fmt), dataset(ename, edata, fmt)
        if fmt == "rj":
            assert a == b, f
        else:
            ok, detail = datasets_isomorphic(a, b)
            assert ok, f"{f}: {detail}"
