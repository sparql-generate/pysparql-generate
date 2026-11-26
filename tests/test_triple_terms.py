"""RDF 1.2 triple terms (``afn:triple``), against the output of the Java engine 2.5.0
(``tests/triple-terms``, written with ``-fo NT``).  rdflib reads no triple term: the
N-Triples lines are compared as strings (the case has no blank node)."""
import os
import subprocess
import sys

import sparql_generate as sg
from sparql_generate.functions import triple_parts

CASE = os.path.join(os.path.dirname(__file__), "triple-terms")


def expected():
    with open(os.path.join(CASE, "expected_output.nt"), encoding="utf-8") as f:
        return {line.strip() for line in f if line.strip()}


def test_triple_terms_as_the_java_engine():
    conf = sg.load_conf(os.path.join(CASE, "sparql-generate-conf.json"))
    with open(os.path.join(CASE, "query.rqg"), encoding="utf-8") as f:
        query = f.read()
    with sg.Execution(query, conf=conf, allow_network=False) as ex:
        got = {f"{s} {p} {o} ." for s, p, o in ex.triples()}
    assert got == expected()


def test_triple_terms_cli(tmp_path):
    out = tmp_path / "out.nt"
    env = dict(os.environ, PYTHONPATH=os.path.join(os.path.dirname(__file__), "..", "src"))
    r = subprocess.run([sys.executable, "-m", "sparql_generate.cli", "-d", CASE, "-q", "query.rqg",
                        "-o", str(out), "-fo", "NT"], env=env, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert {line.strip() for line in out.read_text(encoding="utf-8").splitlines() if line.strip()} == expected()


def test_triple_parts():
    assert triple_parts('<<( <a> <b> "c \\" )>>"@en )>>') == ["<a>", "<b>", '"c \\" )>>"@en']
    assert triple_parts("<<( _:b0 <p> <<( <s> <p> <o> )>> )>>") == ["_:b0", "<p>", "<<( <s> <p> <o> )>>"]
    assert triple_parts("<<( <a> <b> )>>") is None
