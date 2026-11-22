"""Nested TEMPLATE queries (rule T: a sub-template is evaluated on each solution) and
named query calls (rule K: call-site clauses, table of argument tuples, recursion
unfolded until the argument table is empty, tabling of pending calls)."""
import rdflib

import sparql_generate as sg

PFX = "BASE <http://ex.org/> PREFIX ex: <http://ex.org/>\n"


def t(q, **kw):
    return sg.template(PFX + q, allow_network=False, **kw)


def test_inline_sub_template_per_solution():
    q = 'TEMPLATE { "[" ?x ":" TEMPLATE { ?y ; separator = "," } WHERE { VALUES ?y { "a" "b" } } ORDER BY ?y . "]" } WHERE { VALUES ?x { 1 2 } } ORDER BY ?x'
    assert t(q) == "[1:a,b][2:a,b]"


def test_sub_template_sees_its_parent_and_slices_per_parent():
    q = '''TEMPLATE { ?x "(" TEMPLATE { ?y } WHERE { VALUES ?y { 1 2 3 } FILTER(?y <= ?x) } ORDER BY ?y LIMIT 2 . ")" ; separator = " " }
           WHERE { VALUES ?x { 1 3 } } ORDER BY ?x'''
    assert t(q) == "1(1) 3(12)"
    # aggregates of a sub-template are per parent solution
    q = '''TEMPLATE { ?x "=" TEMPLATE { COUNT(?y) } WHERE { VALUES ?y { 1 2 3 } FILTER(?y <= ?x) } . ; separator = " " }
           WHERE { VALUES ?x { 1 3 } } ORDER BY ?x'''
    assert t(q) == "1=1 3=3"


def test_empty_sub_template_is_the_empty_string():
    q = 'TEMPLATE { "<" TEMPLATE { before = "("; ?y ; after = ")" } WHERE { VALUES ?y { 1 } FILTER(false) } . ">" } WHERE {}'
    assert t(q) == "<>"
    q = 'TEMPLATE { "<" BOX { TEMPLATE { before = "("; ?y ; after = ")" } WHERE { VALUES ?y { 1 } } . } ">" } WHERE {}'
    assert t(q) == "<(1)>"
    assert t('TEMPLATE { "a" BOX { "\\nb\\nc\\n" } "d" } WHERE {}') == "a\n  b\n  c\nd"


def test_named_template_call_with_call_site_clauses():
    # rule K: one call on the table of the argument tuples of the call site (D8 in Java)
    named = {"http://ex.org/t.rqg": PFX + 'TEMPLATE <http://ex.org/t.rqg>(?x) { ?x ; separator = "," } WHERE {} ORDER BY ?x'}
    assert t('TEMPLATE { "(" TEMPLATE <t.rqg>(?x) WHERE { VALUES ?x { "a" "b" } } . ")" } WHERE {}', named_queries=named) == "(a,b)"
    # per parent solution
    q = 'TEMPLATE { ?p ":" TEMPLATE <t.rqg>(?x) WHERE { VALUES (?q ?x) { (1 "a") (1 "b") (2 "c") } FILTER(?q = ?p) } . ; separator = " " } WHERE { VALUES ?p { 1 2 3 } } ORDER BY ?p'
    assert t(q, named_queries=named) == "1:a,b 2:c 3:"


def test_recursion_stops_on_an_empty_argument_table():
    named = {"http://ex.org/c.rqg": PFX + 'TEMPLATE <http://ex.org/c.rqg>(?n) { ?n TEMPLATE <http://ex.org/c.rqg>(?m) WHERE { FILTER(?n > 0) BIND(?n - 1 AS ?m) } . } WHERE {}'}
    assert t('TEMPLATE { TEMPLATE <c.rqg>(?n) WHERE { BIND(3 AS ?n) } . } WHERE {}', named_queries=named) == "3210"


def test_tabling_of_pending_calls():
    # a call with the arguments of a pending call contributes nothing (rule K)
    named = {"http://ex.org/loop.rqg": PFX + 'TEMPLATE <http://ex.org/loop.rqg>(?n) { ?n "." TEMPLATE <http://ex.org/loop.rqg>(?n) . } WHERE {}'}
    assert t('TEMPLATE { TEMPLATE <loop.rqg>(?n) WHERE { BIND(1 AS ?n) } . } WHERE {}', named_queries=named) == "1."
    # transitive closure of a cyclic graph by a recursive GENERATE (Prop. tabling)
    g = rdflib.Graph()
    g.parse(data="@prefix ex: <http://ex.org/> . ex:a ex:edge ex:b . ex:b ex:edge ex:c . ex:c ex:edge ex:a . ex:c ex:edge ex:d .", format="turtle")
    tc = PFX + '''GENERATE <http://ex.org/tc.rqg>(?x, ?y) { ?x ex:reach ?y . GENERATE <http://ex.org/tc.rqg>(?x, ?z) WHERE { ?y ex:edge ?z } . } WHERE {}'''
    q = PFX + 'GENERATE { GENERATE <tc.rqg>(?x, ?y) WHERE { ?x ex:edge ?y } . } WHERE {}'
    got = {(s, o) for s, p, o in sg.generate(q, dataset=g, named_queries={"http://ex.org/tc.rqg": tc}, distinct=True)}
    nodes = ["a", "b", "c"]
    expected = {(f"<http://ex.org/{x}>", f"<http://ex.org/{y}>") for x in nodes for y in nodes + ["d"]}
    assert got == expected
