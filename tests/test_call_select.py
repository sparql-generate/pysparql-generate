"""``iter:call-select(u, e1..ek)``: the named SELECT query ``u`` evaluated, for each row, on
the one-row table of its arguments, read as the list of its solutions; tabled recursion."""
import os
import subprocess
import sys

import pytest

import sparql_generate as sg

PFX = "BASE <http://ex.org/> PREFIX ex: <http://ex.org/> PREFIX iter: <http://w3id.org/sparql-generate/iter/>\n"
INT = "^^<http://www.w3.org/2001/XMLSchema#integer>"


def rows(q, named, **kw):
    return sg.select(PFX + q, named_queries={f"http://ex.org/{k}": PFX + v for k, v in named.items()},
                     allow_network=False, **kw)


def i(n):
    return f'"{n}"{INT}'


def test_call_with_arguments_per_row():
    named = {"sq.rqg": "SELECT <http://ex.org/sq.rqg>(?n) ?m ?c WHERE { BIND(?n * ?n AS ?m) BIND(CONCAT(STR(?n), \"!\") AS ?c) }"}
    got = rows("SELECT ?x ?y ?z ITERATOR iter:for(2, 1, 4) AS ?x ITERATOR iter:call-select(<sq.rqg>, ?x) AS ?y ?z WHERE {} ORDER BY ?x", named)
    assert got == [{"x": i(2), "y": i(4), "z": '"2!"'}, {"x": i(3), "y": i(9), "z": '"3!"'}]
    # without argument, from the root level; extra output variables are unbound
    named = {"c.rqg": "SELECT <http://ex.org/c.rqg>() ?v WHERE { VALUES ?v { 1 2 } }"}
    got = rows("SELECT ?v ?w ITERATOR iter:call-select(<c.rqg>) AS ?v ?w WHERE {} ORDER BY ?v", named)
    assert got == [{"v": i(1), "w": None}, {"v": i(2), "w": None}]


def test_modifiers_and_aggregates_apply_per_call():
    # one call per row: LIMIT, ORDER BY and COUNT range over the solutions of that call only
    named = {"le.rqg": "SELECT <http://ex.org/le.rqg>(?n) ?i WHERE { VALUES ?i { 1 2 3 } FILTER(?i <= ?n) } ORDER BY DESC(?i) LIMIT 2",
             "cnt.rqg": "SELECT <http://ex.org/cnt.rqg>(?n) (COUNT(?i) AS ?c) WHERE { VALUES ?i { 1 2 3 } FILTER(?i <= ?n) }"}
    got = rows("SELECT ?n ?i ITERATOR iter:for(1, 2, 4) AS ?n ITERATOR iter:call-select(<le.rqg>, ?n) AS ?i WHERE {} ORDER BY ?n ?i", named)
    assert [(r["n"], r["i"]) for r in got] == [(i(1), i(1)), (i(3), i(2)), (i(3), i(3))]
    got = rows("SELECT ?n ?c ITERATOR iter:for(1, 1, 4) AS ?n ITERATOR iter:call-select(<cnt.rqg>, ?n) AS ?c WHERE {} ORDER BY ?n", named)
    assert [r["c"] for r in got] == [i(1), i(2), i(3)]


def test_duplicate_rows_unbound_arguments_and_bad_names():
    named = {"id.rqg": "SELECT <http://ex.org/id.rqg>(?a, ?b) ?a ?b WHERE {}",
             "gen.rqg": "GENERATE <http://ex.org/gen.rqg>(?a) { ex:s ex:p ?a } WHERE {}"}
    got = rows("SELECT ?x ?a ?b ITERATOR iter:for(1, 1, 3) AS ?d BIND(1 AS ?x) ITERATOR iter:call-select(<id.rqg>, ?x, ?u) AS ?a ?b WHERE {}", named)
    assert got == [{"x": i(1), "a": i(1), "b": None}] * 2  # ?u unbound: the parameter is left unbound
    # a name that is not a SELECT query of the library, or of another arity: no solution
    for call in ("<gen.rqg>, ?x", "<id.rqg>, ?x", "<missing.rqg>, ?x", '"id.rqg", ?x, ?x'):
        with sg.Execution(PFX + f"SELECT ?x ?a BIND(1 AS ?x) ITERATOR iter:call-select({call}) AS ?a WHERE {{}}",
                          named_queries={f"http://ex.org/{k}": PFX + v for k, v in named.items()},
                          allow_network=False) as ex:
            assert list(ex.rows()) == []


def test_dynamic_names_and_recursion():
    # sum(n) = n + sum(n - 1), sum(0) = 0, through a name computed per row
    named = {
        "sum.rqg": """SELECT <http://ex.org/sum.rqg>(?n) ?s
                      BIND(IF(?n = 0, <http://ex.org/zero.rqg>, <http://ex.org/sum.rqg>) AS ?next)
                      ITERATOR iter:call-select(?next, ?n - 1) AS ?r
                      WHERE { BIND(IF(?n = 0, 0, ?n + ?r) AS ?s) }""",
        "zero.rqg": "SELECT <http://ex.org/zero.rqg>(?m) ?z WHERE { BIND(0 AS ?z) }",
    }
    got = rows("SELECT ?n ?s ITERATOR iter:for(0, 3, 12) AS ?n ITERATOR iter:call-select(<sum.rqg>, ?n) AS ?s WHERE {} ORDER BY ?n", named)
    assert [r["s"] for r in got] == [i(0), i(6), i(21), i(45)]


def test_tabling_stops_a_cyclic_recursion():
    # a call whose (name, arguments) are pending on the row's chain gives the empty list
    named = {"loop.rqg": "SELECT <http://ex.org/loop.rqg>(?n) ?m ITERATOR iter:call-select(<http://ex.org/loop.rqg>, ?n) AS ?m WHERE {}",
             "mod.rqg": """SELECT <http://ex.org/mod.rqg>(?n) ?m
                           ITERATOR iter:call-select(<http://ex.org/mod.rqg>, (?n + 1) - 3 * FLOOR((?n + 1) / 3)) AS ?k
                           WHERE { BIND(COALESCE(?k, ?n) AS ?m) }"""}
    assert rows("SELECT ?m ITERATOR iter:call-select(<loop.rqg>, 1) AS ?m WHERE {}", named) == []
    # n -> n+1 mod 3 revisits n after three calls: the third call has no solution
    with sg.Execution(PFX + "SELECT ?m ITERATOR iter:call-select(<mod.rqg>, 0) AS ?m WHERE {}",
                      named_queries={f"http://ex.org/{k}": PFX + v for k, v in named.items()}, allow_network=False) as ex:
        assert list(ex.rows()) == []
    # pending only on the row's own chain: two rows whose chains cross are both evaluated
    named["chain.rqg"] = """SELECT <http://ex.org/chain.rqg>(?n, ?stop) ?m
                            BIND(IF(?n = ?stop, <http://ex.org/zero.rqg>, <http://ex.org/chain.rqg>) AS ?next)
                            ITERATOR iter:call-select(?next, ?n + 1, ?stop) AS ?k
                            WHERE { BIND(COALESCE(?k, ?n) AS ?m) }"""
    named["zero.rqg"] = "SELECT <http://ex.org/zero.rqg>(?n, ?stop) ?z WHERE { BIND(?n - 1 AS ?z) }"
    got = rows("SELECT ?a ?m ITERATOR iter:for(1, 1, 3) AS ?a ITERATOR iter:call-select(<chain.rqg>, ?a, 4) AS ?m WHERE {} ORDER BY ?a", named)
    assert [r["m"] for r in got] == [i(4), i(4)]


TURING = os.environ.get("SG_TURING_EXAMPLES", "")


@pytest.mark.skipif(not os.path.isdir(TURING), reason="SG_TURING_EXAMPLES (the Turing machine examples) is not set")
@pytest.mark.parametrize("machine", ["incrementer", "bb3", "cycle"])
def test_turing_machines_select_variant(machine, tmp_path):
    # the SELECT variant (recursive named SELECT queries called by iter:call-select) gives the
    # halting configuration of the reference simulator; the cycling machine repeats its
    # configuration, tabling stops it with no solution
    d = tmp_path / machine
    (d / "dataset").mkdir(parents=True)
    (d / "dataset" / "default.ttl").write_text(open(os.path.join(TURING, "machines", machine + ".ttl")).read())
    for f in os.listdir(TURING):
        if f.endswith("-select.rqg"):
            (d / f).write_text(open(os.path.join(TURING, f)).read())
    # like the CLI: DIR/dataset/default.ttl is the default graph, named queries are files of DIR
    got = sg.select((d / "turing-select.rqg").read_text(), base="http://example.org/tm/", conf={"_dir": str(d)},
                    allow_network=False)
    if machine == "cycle":
        assert got == []
        return
    sim = subprocess.run([sys.executable, os.path.join(TURING, "simulate.py"), os.path.join(TURING, "machines", machine + ".ttl")],
                         capture_output=True, text=True, check=True).stdout.strip().split("\n")[-1]
    _, state, tape = sim.split(" ")
    left, rest = tape.split("[")
    head, right = rest.split("]")
    assert len(got) == 1
    r = got[0]
    assert r["state"] == f"<http://example.org/tm#{state}>"
    assert (r["left"], r["head"], r["right"]) == (f'"{left}"', f'"{head}"', f'"{right}"')
