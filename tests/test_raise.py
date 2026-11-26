"""RAISE(message, value...) (SPARQL-Generate 2.6): an expression whose evaluation stops the
execution with an error that carries a message and values, wherever an expression is
evaluated, and only when it is reached.  The cases of the Java engine's RaiseTest and CliTest
(2.6.0), and the laziness of the operators that the compiled SQL must keep."""
import subprocess
import sys

import pytest
import rdflib

import sparql_generate as sg
from sparql_generate.raising import format_node

PREFIX = "PREFIX ex: <http://ex/> PREFIX iter: <http://w3id.org/sparql-generate/iter/> "
XSD = "http://www.w3.org/2001/XMLSchema#"


def generate(query, **kw):
    return list(sg.generate(PREFIX + query, **kw))


def raised(fn) -> sg.RaiseError:
    with pytest.raises(sg.RaiseError) as info:
        fn()
    return info.value


def raised_by_generate(query) -> sg.RaiseError:
    return raised(lambda: generate(query))


def objects(query):
    return sorted(o for _, _, o in generate(query))


def test_message_and_values():
    ex = raised_by_generate('GENERATE { ex:s ex:p ?x } '
                            'WHERE { BIND(RAISE("Column not found", "IDs", 3, ex:a, ?unbound, 1/0) AS ?x) }')
    assert ex.message == "Column not found"
    assert str(ex) == 'Column not found: "IDs", 3, <http://ex/a>, UNDEF, UNDEF'
    assert ex.values == ['"IDs"', f'"3"^^<{XSD}integer>', "<http://ex/a>", None, None]


def test_message_only():
    ex = raised_by_generate('GENERATE { ex:s ex:p ?x } WHERE { BIND(RAISE("stop") AS ?x) }')
    assert str(ex) == "stop" and ex.values == []


def test_message_with_embedded_expression():
    ex = raised_by_generate('GENERATE { ex:s ex:p ?x } WHERE { VALUES ?n { 2 } BIND(RAISE("row {?n} has no name") AS ?x) }')
    assert str(ex) == "row 2 has no name"


def test_message_that_cannot_be_evaluated():
    ex = raised_by_generate('GENERATE { ex:s ex:p ?x } WHERE { BIND(RAISE(?unbound, 1) AS ?x) }')
    assert str(ex) == "Error raised by the query: 1"
    ex = raised_by_generate('GENERATE { ex:s ex:p ?x } WHERE { BIND(RAISE(ex:a) AS ?x) }')
    assert str(ex) == "<http://ex/a>"


def test_needs_a_message():
    with pytest.raises(sg.ParseError, match="RAISE needs a message"):
        sg.parse(PREFIX + "GENERATE { ex:s ex:p ?x } WHERE { BIND(RAISE() AS ?x) }")


def test_keyword_is_case_insensitive():
    assert str(raised_by_generate('GENERATE { ex:s ex:p ?x } WHERE { BIND(raise("lower") AS ?x) }')) == "lower"


def test_only_when_reached():
    # IF, COALESCE, || and && evaluate RAISE only when they need its value
    assert objects('GENERATE { ex:s ex:p ?a, ?b, ?c, ?d } WHERE { BIND(1 AS ?x) '
                   'BIND(IF(BOUND(?x), ?x, RAISE("no x")) AS ?a) '
                   'BIND(COALESCE(?x + 1, RAISE("no x")) AS ?b) '
                   'BIND(IF(?x = 1 || RAISE("not 1"), 3, 0) AS ?c) '
                   'BIND(IF(?x = 2 && RAISE("not reached"), 0, 4) AS ?d) }') == \
        [f'"{i}"^^<{XSD}integer>' for i in (1, 2, 3, 4)]


def test_only_when_reached_per_row():
    # the same with values known only at run time: the SQL evaluates the operand of OR,
    # AND, COALESCE and IN only on the rows where the left operands do not decide
    q = ('GENERATE { ex:s ex:p ?a, ?b, ?c, ?d, ?e } ITERATOR iter:for(0, 1, 50) AS ?x '
         'BIND(IF(?x >= 0 || RAISE("or", ?x), 1, 0) AS ?a) '
         'BIND(IF(?x < 0 && RAISE("and", ?x), 0, 2) AS ?b) '
         'BIND(COALESCE(?x, RAISE("coalesce", ?x)) AS ?c) '
         'BIND(IF(?x IN (?x, RAISE("in", ?x)), 3, 0) AS ?d) '
         'BIND(IF(?x NOT IN (-1, ?x, RAISE("not in", ?x)), 0, 4) AS ?e)')
    assert len(generate(q, distinct=True)) == 50  # 0 to 49: ?a to ?e among them
    # and the operand is evaluated when the left ones do not decide
    assert str(raised_by_generate('GENERATE { ex:s ex:p ?a } ITERATOR iter:for(0, 1, 5) AS ?x '
                                  'BIND(IF(?x < 3 || RAISE("or", ?x), 1, 0) AS ?a)')) in ("or: 3", "or: 4")
    assert str(raised_by_generate('GENERATE { ex:s ex:p ?a } ITERATOR iter:for(0, 1, 5) AS ?x '
                                  'BIND(IF(?x = 3 && RAISE("and", ?x), 1, 0) AS ?a)')) == "and: 3"
    assert str(raised_by_generate('GENERATE { ex:s ex:p ?a } ITERATOR iter:for(0, 1, 5) AS ?x '
                                  'BIND(IF(?x IN (0, 1, 2, 4, RAISE("in", ?x)), 1, 0) AS ?a)')) == "in: 3"


def test_for_each_solution_that_reaches_it():
    ex = raised_by_generate('GENERATE { ex:s ex:p ?y } '
                            'WHERE { VALUES ?x { 1 2 3 } BIND(IF(?x < 3, ?x, RAISE("too large", ?x)) AS ?y) }')
    assert str(ex) == "too large: 3"


def test_not_caught_by_coalesce():
    assert str(raised_by_generate('GENERATE { ex:s ex:p ?y } WHERE { BIND(COALESCE(RAISE("first"), 1) AS ?y) }')) \
        == "first"


def test_in_filter():
    ex = raised_by_generate('GENERATE { ex:s ex:p ?x } '
                            'WHERE { VALUES ?x { 1 "a" } FILTER(isNumeric(?x) || RAISE("not a number", ?x)) }')
    assert str(ex) == 'not a number: "a"'
    assert objects('GENERATE { ex:s ex:p ?x } '
                   'WHERE { VALUES ?x { 1 2 } FILTER(isNumeric(?x) || RAISE("not a number", ?x)) }') == \
        [f'"1"^^<{XSD}integer>', f'"2"^^<{XSD}integer>']


def test_filters_in_order():
    # a list of filters is tested in order up to the first that is not true
    assert objects('GENERATE { ex:s ex:p ?x } ITERATOR iter:for(0, 1, 50) AS ?x '
                   'WHERE { FILTER(?x < 2) FILTER(?x < 2 || RAISE("filtered out", ?x)) }') == \
        [f'"{i}"^^<{XSD}integer>' for i in (0, 1)]


def test_in_binding_clause():
    ex = raised_by_generate('GENERATE { ex:s ex:p ?y } '
                            'ITERATOR iter:for(1, 1, 3) AS ?i BIND(IF(?i != 2, ?i, RAISE("two", ?i)) AS ?y)')
    assert str(ex) == "two: 2"


def test_in_iterator_argument():
    # an error of an iterator is a warning, RAISE in its arguments is not
    assert str(raised_by_generate('GENERATE { ex:s ex:p ?i } ITERATOR iter:for(1, 1, RAISE("no end")) AS ?i')) \
        == "no end"


def test_after_an_iterator():
    ex = raised_by_generate('GENERATE { ex:s ex:p ?y } '
                            'ITERATOR iter:for(1, 1, 5) AS ?i WHERE { BIND(IF(?i < 3, ?i, RAISE("three")) AS ?y) }')
    assert str(ex) == "three"


def test_in_template_of_generate():
    ex = raised_by_generate('GENERATE { ex:s ex:p ?{ IF(?x > 1, RAISE("large", ?x), ?x) } } WHERE { VALUES ?x { 1 2 } }')
    assert str(ex) == "large: 2"


def test_in_nested_generate():
    ex = raised_by_generate('GENERATE { GENERATE { ex:s ex:p ?y } '
                            'WHERE { BIND(IF(?x = 1, RAISE("nested", ?x), ?x) AS ?y) } . } WHERE { VALUES ?x { 1 } }')
    assert str(ex) == "nested: 1"


def test_variable_that_nothing_reads():
    # the BIND is evaluated for each solution, also when its variable is not used
    assert str(raised_by_generate('GENERATE { ex:s ex:p 1 } WHERE { BIND(RAISE("unused") AS ?y) }')) == "unused"
    assert str(raised_by_generate('GENERATE { ex:s ex:p ?i } ITERATOR iter:for(1, 1, 5) AS ?i '
                                  'BIND(IF(?i = 3, RAISE("three"), ?i) AS ?y)')) == "three"


def test_in_a_sub_query_without_template():
    # a GENERATE with an empty template, run for its RAISE only: the column checks of the
    # queries that rml-to-sparql-generate 0.3.0 writes
    q = ('GENERATE { GENERATE { } ITERATOR iter:CSVHeaders("a,b\\n1,2") AS ?c WHERE { } GROUP BY ?no '
         'HAVING (true && (SUM(IF(STR(?c) = "{col}", 1, 0)) > 0 || RAISE("no such column", "{col}"))) . '
         'ex:s ex:p 1 . }')
    assert len(generate(q.replace("{col}", "b"))) == 1
    assert str(raised_by_generate(q.replace("{col}", "z"))) == 'no such column: "z"'
    # an empty iterator: no group, no error
    assert len(generate(q.replace("{col}", "z").replace('"a,b\\n1,2"', '""'))) == 1


def test_in_select():
    q = PREFIX + 'SELECT ?y WHERE { VALUES ?x { 1 } OPTIONAL { ?x ex:p ?z } BIND(COALESCE(?z, RAISE("no z for", ?x)) AS ?y) }'
    assert str(raised(lambda: sg.select(q, dataset=rdflib.Graph()))) == "no z for: 1"


def test_in_template():
    q = PREFIX + 'TEMPLATE { "x: " IF(?x = 1, RAISE("template"), ?x) } WHERE { VALUES ?x { 1 } }'
    assert str(raised(lambda: sg.template(q))) == "template"


def test_iri_with_embedded_expressions():
    ex = raised_by_generate('GENERATE { <http://ex/s/{?x}> ex:p ?y } '
                            'WHERE { VALUES ?x { "a" } BIND(RAISE("iri <{?x}>", <http://ex/v/{?x}>) AS ?y) }')
    assert str(ex) == "iri <a>: <http://ex/v/a>"


def test_api_outputs():
    q = PREFIX + 'GENERATE { ex:s ex:p ?y } ITERATOR iter:for(1, 1, 5) AS ?i BIND(IF(?i < 3, ?i, RAISE("three")) AS ?y)'
    for out in (lambda: sg.generate_to(q, "/dev/null"), lambda: sg.to_graph(q), lambda: list(sg.generate_quads(q)),
                lambda: sg.Execution(q).arrow()):
        assert str(raised(out)) == "three"


def test_values_as_jena_writes_them():
    # FmtUtils.stringForNode with the global prefixes of ARQ
    assert format_node(f'"3"^^<{XSD}integer>') == "3"
    assert format_node(f'"+3"^^<{XSD}integer>') == "+3"
    assert format_node(f'"03.50"^^<{XSD}decimal>') == "03.50"
    assert format_node(f'".5"^^<{XSD}decimal>') == f'".5"^^xsd:decimal'
    assert format_node(f'"1.5e3"^^<{XSD}double>') == "1.5e3"
    assert format_node(f'"15"^^<{XSD}double>') == '"15"^^xsd:double'
    assert format_node(f'"true"^^<{XSD}boolean>') == "true"
    assert format_node(f'"1"^^<{XSD}boolean>') == '"1"^^xsd:boolean'
    assert format_node(f'"2026-11-26"^^<{XSD}date>') == '"2026-11-26"^^xsd:date'
    assert format_node('"x"^^<http://ex/d>') == '"x"^^<http://ex/d>'
    assert format_node('"chat"@fr') == '"chat"@fr'
    assert format_node('"a\\"b\\nc"') == '"a\\"b\\nc"'
    assert format_node(f'<{XSD}string>') == "xsd:string"
    assert format_node("<http://www.w3.org/1999/02/22-rdf-syntax-ns#type>") == "rdf:type"
    assert format_node("<http://www.w3.org/2000/01/rdf-schema#a.>") == "<http://www.w3.org/2000/01/rdf-schema#a.>"
    assert format_node("_:b0") == "_:b0"
    assert format_node(f'<<( <http://ex/s> <http://ex/p> "1"^^<{XSD}integer> )>>') == "<<( <http://ex/s> <http://ex/p> 1 )>>"


def run_cli(tmp_path, query, *args):
    (tmp_path / "query.rqg").write_text(query, encoding="utf-8")
    return subprocess.run([sys.executable, "-m", "sparql_generate.cli", "-d", str(tmp_path), *args],
                          capture_output=True, text=True)


RAISE = ('GENERATE { <http://ex/s> <http://ex/p> ?y } '
         'WHERE { VALUES ?x { 1 2 } BIND(IF(?x < 2, ?x, RAISE("Too large", ?x, <http://ex/a>)) AS ?y) }')


@pytest.mark.parametrize("args", [(), ("-fo", "NT"), ("-fo", "TTL"), ("-fo", "NQUADS")])
def test_cli_fails(tmp_path, args):
    # the message and values on the standard error, status 1, in every output mode
    r = run_cli(tmp_path, RAISE, *args)
    assert r.returncode == 1, r.stderr
    assert "Too large: 2, <http://ex/a>" in r.stderr
    assert "Traceback" not in r.stderr


def test_cli_select_and_template_fail(tmp_path):
    r = run_cli(tmp_path, 'SELECT ?y WHERE { BIND(RAISE("select") AS ?y) }')
    assert r.returncode == 1 and "select" in r.stderr
    r = run_cli(tmp_path, 'TEMPLATE { RAISE("template", 1) }')
    assert r.returncode == 1 and "template: 1" in r.stderr


def test_cli_not_reached_succeeds(tmp_path):
    r = run_cli(tmp_path, RAISE.replace("?x < 2", "?x < 3"))
    assert r.returncode == 0 and r.stderr.strip() == ""
    assert len(r.stdout.strip().splitlines()) == 2


def test_long_chains_stay_linear():
    # the column checks of a CSV file of 40 columns: a chain of 40 && whose operands hold
    # a RAISE (compiled as nested binary CASEs, the SQL doubled at each level)
    import time

    cols = [f"c{i}" for i in range(40)]
    checks = " ".join(f'&& (SUM(IF(STR(?c) = "{c}", 1, 0)) > 0 || RAISE("no such column", "{c}"))' for c in cols)
    q = (f'GENERATE {{ GENERATE {{ }} ITERATOR iter:CSVHeaders("{",".join(cols)}\\n") AS ?c WHERE {{ }} '
         f"GROUP BY ?no HAVING (true {checks}) . ex:s ex:p 1 . }}")
    t0 = time.perf_counter()
    assert len(generate(q)) == 1
    assert time.perf_counter() - t0 < 10
    assert str(raised_by_generate(q.replace('"c39", 1', '"c40", 1'))) == 'no such column: "c39"'


def test_datatype_known_at_run_time():
    # the datatype map of RML-Core (RMLTC0022e): an IRI checked with RAISE, whose type is
    # known at run time only; xsd:string gives the simple literal
    q = ('GENERATE { <http://ex/{?v}> ex:x ?o } ITERATOR iter:for(1, 1, 3) AS ?v '
         'BIND(IF(?v = 1, "http://www.w3.org/2001/XMLSchema#string", "http://www.w3.org/2001/XMLSchema#int") AS ?l) '
         'BIND(COALESCE(IRI(?l), IF(BOUND(?l), RAISE("not an IRI", ?l), ?none)) AS ?dt) '
         'BIND(STRDT(STR(?v), ?dt) AS ?o)')
    assert objects(q) == ['"1"', f'"2"^^<{XSD}int>']
