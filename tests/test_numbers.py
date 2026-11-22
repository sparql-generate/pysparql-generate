"""Numeric results and their lexical forms, as the Java engine (Jena) writes them: doubles
as Double.toString with e0 when there is no exponent, floats as Float.toString, decimal
arithmetic exact (BigDecimal) with non-terminating quotients rounded to 24 places, casts
from strings keeping the lexical form, ABS/CEIL/FLOOR/ROUND keeping the type.  Expected
values produced by sparql-generate 2.2.0 on 47 expressions."""
import pytest

import sparql_generate as sg

CASES = [
    ('1 + 2', '"3"^^<http://www.w3.org/2001/XMLSchema#integer>'),
    ('1.5 * 2', '"3.0"^^<http://www.w3.org/2001/XMLSchema#decimal>'),
    ('1 / 3', '"0.333333333333333333333333"^^<http://www.w3.org/2001/XMLSchema#decimal>'),
    ('10 / 4', '"2.5"^^<http://www.w3.org/2001/XMLSchema#decimal>'),
    ('1.1 + 2.2', '"3.3"^^<http://www.w3.org/2001/XMLSchema#decimal>'),
    ('2.5e0 * 2', '"5.0e0"^^<http://www.w3.org/2001/XMLSchema#double>'),
    ('1.0e10 * 1', '"1.0E10"^^<http://www.w3.org/2001/XMLSchema#double>'),
    ('1.0e-5 * 1', '"1.0E-5"^^<http://www.w3.org/2001/XMLSchema#double>'),
    ('1234567.0e0 * 1', '"1234567.0e0"^^<http://www.w3.org/2001/XMLSchema#double>'),
    ('12345678.9e0 * 1', '"1.23456789E7"^^<http://www.w3.org/2001/XMLSchema#double>'),
    ('"1.5"^^xsd:float + 1', '"2.5"^^<http://www.w3.org/2001/XMLSchema#float>'),
    ('-(1.5)', '"-1.5"^^<http://www.w3.org/2001/XMLSchema#decimal>'),
    ('1 - 1.0', '"0.0"^^<http://www.w3.org/2001/XMLSchema#decimal>'),
    ('100.5 * 2', '"201.0"^^<http://www.w3.org/2001/XMLSchema#decimal>'),
    ('xsd:double("3")', '"3"^^<http://www.w3.org/2001/XMLSchema#double>'),
    ('xsd:decimal("3")', '"3"^^<http://www.w3.org/2001/XMLSchema#decimal>'),
    ('STR(1.5e0 * 1)', '"1.5e0"'),
    ('0.1 + 0.2', '"0.3"^^<http://www.w3.org/2001/XMLSchema#decimal>'),
    ('2 / 2', '"1.0"^^<http://www.w3.org/2001/XMLSchema#decimal>'),
    ('ABS(-1.5)', '"1.5"^^<http://www.w3.org/2001/XMLSchema#decimal>'),
    ('ROUND(2.5)', '"3.0"^^<http://www.w3.org/2001/XMLSchema#decimal>'),
    ('CEIL(1.2)', '"2.0"^^<http://www.w3.org/2001/XMLSchema#decimal>'),
    ('1e0 / 3', '"0.3333333333333333e0"^^<http://www.w3.org/2001/XMLSchema#double>'),
    ('7 / 2.0e0', '"3.5e0"^^<http://www.w3.org/2001/XMLSchema#double>'),
    ('0.0001e0 * 1', '"1.0E-4"^^<http://www.w3.org/2001/XMLSchema#double>'),
    ('0.001e0 * 1', '"0.001e0"^^<http://www.w3.org/2001/XMLSchema#double>'),
    ('1e7 * 1', '"1.0E7"^^<http://www.w3.org/2001/XMLSchema#double>'),
    ('123.0 * 1', '"123.0"^^<http://www.w3.org/2001/XMLSchema#decimal>'),
    ('-0.5e0 * 2', '"-1.0e0"^^<http://www.w3.org/2001/XMLSchema#double>'),
    ('2 * 3', '"6"^^<http://www.w3.org/2001/XMLSchema#integer>'),
    ('2 / 3 * 3', '"2.000000000000000000000001"^^<http://www.w3.org/2001/XMLSchema#decimal>'),
    ('1.000 + 0', '"1.0"^^<http://www.w3.org/2001/XMLSchema#decimal>'),
    ('"01"^^xsd:integer + 0', '"1"^^<http://www.w3.org/2001/XMLSchema#integer>'),
    ('1e0 + 0', '"1.0e0"^^<http://www.w3.org/2001/XMLSchema#double>'),
    ('-(1)', '"-1"^^<http://www.w3.org/2001/XMLSchema#integer>'),
    ('FLOOR(-1.5)', '"-2.0"^^<http://www.w3.org/2001/XMLSchema#decimal>'),
    ('ABS(-2)', '"2"^^<http://www.w3.org/2001/XMLSchema#integer>'),
    ('ROUND(-2.5)', '"-2.0"^^<http://www.w3.org/2001/XMLSchema#decimal>'),
    ('xsd:float(1.5)', '"1.5"^^<http://www.w3.org/2001/XMLSchema#float>'),
    ('xsd:integer(2.9)', '"2"^^<http://www.w3.org/2001/XMLSchema#integer>'),
    ('xsd:decimal(1e0)', '"1.0"^^<http://www.w3.org/2001/XMLSchema#decimal>'),
    ('xsd:string(1.5e0 * 1)', '"1.5"'),
    ('1.5 = 1.50', '"true"^^<http://www.w3.org/2001/XMLSchema#boolean>'),
    ('"1"^^xsd:integer = 1.0', '"true"^^<http://www.w3.org/2001/XMLSchema#boolean>'),
    ('10 / 3', '"3.333333333333333333333333"^^<http://www.w3.org/2001/XMLSchema#decimal>'),
    ('1.0 / 8', '"0.125"^^<http://www.w3.org/2001/XMLSchema#decimal>'),
    ('STR(0.1e0 + 0.2e0)', '"0.30000000000000004e0"'),
]


@pytest.mark.parametrize("expr,expected", CASES)
def test_numeric_result_as_jena_writes_it(expr, expected):
    q = f"PREFIX xsd: <http://www.w3.org/2001/XMLSchema#> GENERATE {{ <http://s> <http://p> ?v }} WHERE {{ BIND({expr} AS ?v) }}"
    rows = list(sg.generate(q))
    assert rows and rows[0][2] == expected


def test_dynamic_values_follow_the_same_rules():
    # the same operations on values only known at run time (the dynamic representation)
    q = """PREFIX xsd: <http://www.w3.org/2001/XMLSchema#> GENERATE { <http://s> <http://p> ?r }
    WHERE { VALUES (?a ?b) { (1 3) (1.5 2) ("2.5"^^xsd:double 2) (10 4) } BIND(IF(?a = 1, ?a / ?b, ?a * ?b) AS ?r) }"""
    got = sorted(r[2].split("^^")[0] for r in sg.generate(q))
    assert got == ['"0.333333333333333333333333"', '"3.0"', '"40"', '"5.0e0"']


RUNTIME_QUERY = """PREFIX xsd: <http://www.w3.org/2001/XMLSchema#>
GENERATE { <http://s> <http://p/{?i}> ?r } WHERE {
  VALUES (?i ?a ?b) { (1 1 3) (2 2 3) (3 0.5 4) (4 "x" 1) (5 "2.5"^^xsd:double 2) (6 0.1234567890123456789 1) }
  BIND(?a / ?b + ?a AS ?r1)
  BIND((?a + 1) - 3 * FLOOR((?a + 1) / 3) AS ?r2)
  BIND(ABS(?a / ?b - 1) AS ?r3)
  BIND(?a + 0.0000000000000000001 AS ?r4)
  BIND(CONCAT(STR(?r1), " ", STR(?r2), " ", STR(?r3), " ", STR(?r4)) AS ?r)
}"""

ROUNDING_QUERY = """PREFIX xsd: <http://www.w3.org/2001/XMLSchema#>
GENERATE { <http://s> <http://p/{?i}> ?r } WHERE {
  VALUES (?i ?a) { (1 -2.5) (2 -0.5) (3 0.5) (4 2.5) (5 2.49) (6 -2.51) (7 1.0) (8 "-0.0"^^xsd:decimal) (9 12345678901234567890123.5) (10 "007.50"^^xsd:decimal) (11 ".5"^^xsd:decimal) (12 -3.0) (13 "+1.5"^^xsd:decimal) }
  BIND(?a + 0 AS ?b)
  BIND(CONCAT(STR(FLOOR(?b)), " ", STR(CEIL(?b)), " ", STR(ROUND(?b)), " ", STR(ABS(?b)), " ", STR(FLOOR(?a)), " ", STR(ABS(?a))) AS ?r)
}"""


def test_runtime_decimals_are_exact_as_in_jena():
    # values known at run time only (VALUES of mixed types): + and - exact beyond 18 decimal
    # places, ABS/FLOOR/CEIL/ROUND exact on decimals; expected: the Java engine 2.2.2
    got = sorted(o for s, p, o in sg.generate(RUNTIME_QUERY))
    assert got == sorted([
        '"1.333333333333333333333333 2.0 0.666666666666666666666667 1.0000000000000000001"',
        '"2.666666666666666666666667 0.0 0.333333333333333333333333 2.0000000000000000001"',
        '"0.625 1.5 0.875 0.5000000000000000001"',
        '"3.75e0 0.5e0 0.25e0 2.5e0"',
        '"0.2469135780246913578 1.1234567890123456789 0.8765432109876543211 0.123456789012345679"',
    ])
    got = {p: o for s, p, o in sg.generate(ROUNDING_QUERY)}
    expected = {f"<http://p/{i}>": f'"{t}"' for i, t in enumerate([
        "-3.0 -2.0 -2.0 2.5 -3.0 2.5", "-1.0 0.0 0.0 0.5 -1.0 0.5", "0.0 1.0 1.0 0.5 0.0 0.5",
        "2.0 3.0 3.0 2.5 2.0 2.5", "2.0 3.0 2.0 2.49 2.0 2.49", "-3.0 -2.0 -3.0 2.51 -3.0 2.51",
        "1.0 1.0 1.0 1.0 1.0 1.0", "0.0 0.0 0.0 0.0 0.0 0.0",
        "12345678901234567890123.0 12345678901234567890124.0 12345678901234567890124.0 "
        "12345678901234567890123.5 12345678901234567890123.0 12345678901234567890123.5",
        "7.0 8.0 8.0 7.5 7.0 7.5", "0.0 1.0 1.0 0.5 0.0 0.5", "-3.0 -3.0 -3.0 3.0 -3.0 3.0",
        "1.0 2.0 2.0 1.5 1.0 1.5"], 1)}
    assert got == expected


def test_nested_arithmetic_compiles_to_linear_sql():
    # each operator binds a compound operand once: the SQL grows linearly with the nesting
    # (DuckDB expands a macro argument at each occurrence; copying operands made it exponential)
    sizes = []
    for depth in (1, 2, 3):
        e = "?d"
        for k in range(depth):
            e = f"FLOOR(({e} + 1) / 3) * 2"
        with sg.Execution(f'SELECT ?a WHERE {{ VALUES ?d {{ 1.5 "2" }} BIND({e} AS ?a) }}', debug=True) as ex:
            rows = list(ex.rows())
            sizes.append(max(len(s) for s in ex.engine.sql_log))
        assert len(rows) == 2 and rows[1]["a"] is None
    assert sizes[2] - sizes[1] <= 1.2 * (sizes[1] - sizes[0])


JSON_DOC = ('{"a": 0.10, "b": -0.0, "c": 1.5e3, "d": 1E-7, "e": 123456789012345678901234567890, "f": -65.814436, '
            '"g": 18446744073709551615, "h": 3, "i": 1.0, "j": 0.1000000000000000055511151231257827, "k": 1.0e0}')
X = "^^<http://www.w3.org/2001/XMLSchema#"
# the Java engine on master (d44496a, future 2.3.0): a decimal point without an exponent gives
# an exact xsd:decimal, an exponent an xsd:double, no fraction an xsd:integer
JAVA_23 = {"a": f'"0.1"{X}decimal>', "b": f'"0.0"{X}decimal>', "c": f'"1500.0e0"{X}double>',
           "d": f'"1.0E-7"{X}double>', "e": f'"123456789012345678901234567890"{X}integer>',
           "f": f'"-65.814436"{X}decimal>', "g": f'"18446744073709551615"{X}integer>', "h": f'"3"{X}integer>',
           "i": f'"1.0"{X}decimal>', "j": f'"0.1000000000000000055511151231257827"{X}decimal>',
           "k": f'"1.0e0"{X}double>'}


def _json_values(path_of):
    q = ("PREFIX fun: <http://w3id.org/sparql-generate/fn/>\nPREFIX iter: <http://w3id.org/sparql-generate/iter/>\n"
         "SELECT ?k ?v SOURCE <http://ex.org/d.json> AS ?doc ITERATOR iter:JSONListKeys(?doc) AS ?k "
         f"WHERE {{ BIND(fun:JSONPath(?doc, {path_of}) AS ?v) }}")
    return {r["k"].strip('"'): r["v"] for r in sg.select(q, documents={"http://ex.org/d.json": JSON_DOC})}


def test_json_numbers_as_the_java_engine_types_them():
    # integers, decimals and doubles as in Java; the JSON values pass through DuckDB's reader,
    # which writes 1.5e3 and 1.0e0 back without exponent (typed decimal here, double in Java)
    # and keeps 17 significant digits 
    gaps = {"c": f'"1500.0"{X}decimal>', "j": f'"0.1"{X}decimal>', "k": f'"1.0"{X}decimal>'}
    expected = {**JAVA_23, **gaps}
    assert _json_values('CONCAT("$.", ?k)') == expected
    assert _json_values('CONCAT("$..", ?k)') == expected  # the Python JSONPath reader too
