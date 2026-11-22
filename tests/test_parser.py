import pytest

from sparql_generate import ast as A
from sparql_generate.lexer import ParseError
from sparql_generate.parser import parse

PFX = "PREFIX ex: <http://ex.org/> PREFIX iter: <http://w3id.org/sparql-generate/iter/> PREFIX fun: <http://w3id.org/sparql-generate/fn/> "


def test_generate_with_embedded_expressions():
    q = parse(PFX + 'GENERATE { <http://ex.org/{ ?id }> ex:name "{ ?name }"@en ; ex:n "{?n}"^^<http://www.w3.org/2001/XMLSchema#integer> ; ex:v ?{ STR(?x) } . } '
              'ITERATOR iter:CSV(<http://ex.org/f.csv>, "id", "name") AS ?id ?name WHERE { BIND(1 AS ?n) FILTER(?n > 0) }')
    assert q.kind == "GENERATE"
    triples = q.generate_clause[0].triples
    assert isinstance(triples[0].subject, A.XIRI)
    assert triples[0].subject.components == ("http://ex.org/", A.Var("id"))
    assert isinstance(triples[0].object, A.XLiteral) and triples[0].object.lang == "en"
    assert triples[1].object.datatype == A.IRI(A.XSD_INTEGER)
    assert isinstance(triples[2].object, A.XExpr)
    assert isinstance(q.binding_clauses[0], A.IteratorClause)
    assert [v.name for v in q.binding_clauses[0].vars] == ["id", "name"]
    assert isinstance(q.where.elements[0], A.Bind)
    assert isinstance(q.where.elements[1], A.Filter)


def test_nested_generate_and_named_call():
    q = parse(PFX + 'GENERATE { ?s ex:p ?o . GENERATE { ?s ex:q ?j } ITERATOR iter:for(1,1,3) AS ?j . GENERATE <http://ex.org/q>(?s, ?o) . } WHERE { VALUES (?s ?o) { (ex:a 1) (ex:b UNDEF) } }')
    sub = q.generate_clause[1].query
    assert sub.is_sub and sub.generate_clause is not None
    call = q.generate_clause[2].query
    assert call.is_named_call and call.name == A.IRI("http://ex.org/q")
    assert len(call.call_params) == 2
    values = q.where.elements[0]
    assert values.rows[1][1] is None


def test_blank_nodes_lists_and_strings():
    q = parse('BASE <http://ex.org/> GENERATE { _:b <p> [ <q> ( 1 2.5 "x\\"y" ) ] ; <l> LIST(?v) . } WHERE { VALUES ?v { "a" } }')
    triples = q.generate_clause[0].triples
    assert triples[-1].object == A.ListNode(A.Var("v"))
    assert any(isinstance(t.object, A.Literal) and t.object.lexical == 'x"y' for t in triples)
    assert any(t.subject == A.BNode("b") and t.predicate == A.IRI("http://ex.org/p") for t in triples)


def test_template_select_and_modifiers():
    q = parse(PFX + 'TEMPLATE { before = "["; "{?x}" ; separator = ", " ; after = "]" } WHERE { ?s ex:p ?x } ORDER BY DESC(?x) LIMIT 5 OFFSET 2')
    assert q.kind == "TEMPLATE" and q.limit == 5 and q.offset == 2 and q.order_by[0].direction == -1
    q = parse(PFX + 'SELECT (COUNT(*) AS ?n) ?g WHERE { ?s ?p ?o } GROUP BY ?g HAVING (COUNT(*) > 1)')
    assert q.select_vars[0][0] == A.Var("n") and isinstance(q.select_vars[0][1], A.Aggregate)


def test_long_string_with_expression_and_escapes():
    q = parse(PFX + 'GENERATE { ex:s ex:p """a "quoted" {?x}\\{literal\\}""" } WHERE { BIND("v" AS ?x) }')
    lit = q.generate_clause[0].triples[0].object
    assert lit.components == ('a "quoted" ', A.Var("x"), "{literal}")


def test_errors_have_positions():
    with pytest.raises(ParseError) as ei:
        parse('GENERATE { <s> <p> } WHERE {}')
    assert ei.value.line == 1 and ei.value.col > 1
    with pytest.raises(ParseError, match="undefined prefix"):
        parse("GENERATE { ex:s ex:p 1 }")
