"""FUNCTION queries of the library, called from expressions (rule K for functions)."""
import pytest

import sparql_generate as sg

PFX = "BASE <http://ex.org/> PREFIX ex: <http://ex.org/>\n"


def test_function_call():
    # D4 of the Java engine (every call failed before 2.2.0)
    lib = {"http://ex.org/id.rqg": "FUNCTION <http://ex.org/id.rqg>(?x) { ?x }"}
    assert sg.template(PFX + 'TEMPLATE { "<" <id.rqg>("a") ">" } WHERE { }', named_queries=lib) == "<a>"
    lib["http://ex.org/full.rqg"] = PFX + 'FUNCTION <http://ex.org/full.rqg>(?f, ?l) { CONCAT(?f, " ", UCASE(?l)) }'
    rows = sorted(sg.generate(PFX + 'GENERATE { ex:s ex:name ?n } WHERE { VALUES (?a ?b) { ("Ada" "lovelace") ("Alan" "turing") } BIND(<full.rqg>(?a, ?b) AS ?n) }',
                              named_queries=lib))
    assert [r[2] for r in rows] == ['"Ada LOVELACE"', '"Alan TURING"']
    # the body sees its parameters only (call by value)
    lib["http://ex.org/leak.rqg"] = "FUNCTION <http://ex.org/leak.rqg>(?x) { COALESCE(?outer, ?x) }"
    assert sg.template(PFX + 'TEMPLATE { <leak.rqg>("in") } WHERE { BIND("out" AS ?outer) }', named_queries=lib) == "in"
    # wrong arity: unbound
    assert sg.template(PFX + 'TEMPLATE { "[" <id.rqg>("a", "b") "]" } WHERE { }', named_queries=lib) == "[]"


def test_recursive_function_on_constants():
    lib = {"http://ex.org/fact.rqg": PFX + "FUNCTION <http://ex.org/fact.rqg>(?n) { IF(?n <= 1, 1, ?n * <fact.rqg>(?n - 1)) }"}
    assert sg.template(PFX + 'TEMPLATE { <fact.rqg>(5) } WHERE { }', named_queries=lib) == "120"
    # tabling: a call with the arguments of a pending call is unbound
    lib["http://ex.org/loop.rqg"] = PFX + "FUNCTION <http://ex.org/loop.rqg>(?n) { COALESCE(<loop.rqg>(?n), ?n + 1) }"
    assert sg.template(PFX + 'TEMPLATE { <loop.rqg>(1) } WHERE { }', named_queries=lib) == "2"


INT = "^^<http://www.w3.org/2001/XMLSchema#integer>"


def test_recursive_function_on_run_time_values():
    # evaluated iteratively on the argument tuples of the rows: demanded calls level by level
    # (the IF and COALESCE branches that reach them), values bottom-up, tabling per chain
    lib = {"http://ex.org/fact.rqg": PFX + "FUNCTION <http://ex.org/fact.rqg>(?n) { IF(?n <= 1, 1, ?n * <fact.rqg>(?n - 1)) }",
           "http://ex.org/fib.rqg": PFX + "FUNCTION <http://ex.org/fib.rqg>(?n) { IF(?n < 2, ?n, <fib.rqg>(?n - 1) + <fib.rqg>(?n - 2)) }",
           "http://ex.org/mod.rqg": PFX + "FUNCTION <http://ex.org/mod.rqg>(?n) { COALESCE(<mod.rqg>(?n + 1 - 3 * FLOOR((?n + 1) / 3)), ?n) }"}

    def vals(f, values):
        q = PFX + f'GENERATE {{ ex:s ex:v ?r ; ex:n ?n }} WHERE {{ VALUES ?n {{ {values} }} BIND(<{f}.rqg>(?n) AS ?r) }}'
        rows = {}
        for s_, p_, o_ in sg.generate(q, named_queries=lib):
            rows.setdefault(p_, []).append(o_)
        return sorted(rows.get("<http://ex.org/v>", []))

    assert vals("fact", "0 5 20") == sorted([f'"1"{INT}', f'"120"{INT}', f'"2432902008176640000"{INT}'])
    assert vals("fib", "1 6 10") == sorted([f'"1"{INT}', f'"8"{INT}', f'"55"{INT}'])
    # n -> n+1 mod 3 comes back to a pending call, which is unbound: COALESCE falls back to ?n
    dec = "^^<http://www.w3.org/2001/XMLSchema#decimal>"
    assert vals("mod", "0 1") == [f'"0.0"{dec}', f'"1.0"{dec}']
    # an unbound argument and a non-numeric one: errors, unbound results
    assert vals("fact", '"x" UNDEF') == []
    # in the output template, after the solutions are written: refused
    with pytest.raises(sg.CompileError):
        sg.Execution(PFX + 'TEMPLATE { <fact.rqg>(?n) } WHERE { VALUES ?n { 3 } }', named_queries=lib)


def test_unknown_function_is_an_error_without_network_access():
    with pytest.raises(sg.CompileError):
        sg.Execution(PFX + 'TEMPLATE { <http://example.invalid/f>(1) } WHERE { }')
