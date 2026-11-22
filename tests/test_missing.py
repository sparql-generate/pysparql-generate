"""--fail-on-missing (key failOnMissing, as the Java engine 2.2.3): a document, a graph or a
named query that cannot be found is an error with status 1; by default a warning, and the
variable unbound, the iterator or the graph empty (the semantics)."""
import json
import subprocess
import sys

import pytest

import sparql_generate as sg

PFX = "PREFIX ex: <http://ex.org/> PREFIX iter: <http://w3id.org/sparql-generate/iter/>\n"
QUERIES = {
    "iterator": 'GENERATE { ex:s ex:p ?x } ITERATOR iter:CSV(<http://ex.org/missing.csv>, "a") AS ?x',
    "source": 'GENERATE { ex:s ex:p ex:o ; ex:q ?d } SOURCE <http://ex.org/missing.json> AS ?d',
    "source-per-row": 'GENERATE { ex:s ex:p ex:o ; ex:q ?d } BIND(IRI(CONCAT("http://ex.org/", "missing.json")) AS ?u) SOURCE ?u AS ?d',
    "from": 'GENERATE { ?s ex:p ?o } FROM <http://ex.org/missing.ttl> WHERE { ?s ?p ?o }',
    "named-query": 'GENERATE { GENERATE <http://ex.org/missing.rqg>(1) . } WHERE { }',
}


@pytest.mark.parametrize("name", sorted(QUERIES))
def test_fail_on_missing(name):
    q = PFX + QUERIES[name]
    if name != "named-query":  # by default: a warning, and nothing from the document
        with sg.Execution(q, allow_network=False) as ex:
            rows = list(ex.triples())
            assert all("missing" not in o for s, p, o in rows)
    with pytest.raises(Exception, match="Document not found: http://ex.org/missing"):
        with sg.Execution(q, allow_network=False, fail_on_missing=True) as ex:
            list(ex.triples())


def test_cli_option_and_conf_key(tmp_path):
    (tmp_path / "query.rqg").write_text(PFX + QUERIES["iterator"])

    def run(*opts):
        return subprocess.run([sys.executable, "-m", "sparql_generate.cli", "-d", str(tmp_path), "--no-network", *opts],
                              capture_output=True, text=True)

    r = run()
    assert r.returncode == 0 and r.stdout == ""
    r = run("--fail-on-missing")
    assert r.returncode == 1 and "Document not found: http://ex.org/missing.csv" in r.stderr
    (tmp_path / "sparql-generate-conf.json").write_text(json.dumps({"failOnMissing": True}))
    r = run()
    assert r.returncode == 1 and "Document not found" in r.stderr
