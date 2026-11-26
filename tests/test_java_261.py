"""The corrections and the new iterators of the Java engine 2.6.1, case by case from its tests:
CallArgumentTest, QueryBaseTest, LiteralDatatypeTest, EmbeddedExpressionInWhereTest,
CSVRaiseTest, JSONSurferRaiseTest, SQLRaiseTest, CSVStrictTest, SPARQLIteratorTest and
XPathTest (the relative sub-queries); and a named SELECT query called as an iterator
(``ITERATOR <q>(args)``, the conformance cases ``calls-*``)."""
import sqlite3

import pytest
import rdflib
from rdflib.compare import isomorphic

import sparql_generate as sg

XSD = "http://www.w3.org/2001/XMLSchema#"
ITER = "PREFIX iter: <http://w3id.org/sparql-generate/iter/> "


def graph_of(query, **kw) -> rdflib.Graph:
    with sg.Execution(query, allow_network=False, **kw) as ex:
        return ex.to_graph()


def assert_generates(expected_turtle, query, **kw):
    got = graph_of(query, **kw)
    exp = rdflib.Graph().parse(data=expected_turtle, format="turtle")
    assert isomorphic(got, exp), f"expected\n{exp.serialize(format='nt')}\ngot\n{got.serialize(format='nt')}"


def raised(query, **kw) -> sg.RaiseError:
    with pytest.raises(sg.RaiseError) as info:
        with sg.Execution(query, allow_network=False, **kw) as ex:
            ex.text() if ex.plan.kind == "TEMPLATE" else list(ex.triples())
    return info.value


# -- CallArgumentTest: arguments with embedded expressions of the calls ------------------------

CP = "PREFIX ex: <http://ex.org/> "
CALLED = {
    "http://ex.org/q": CP + "GENERATE ex:q(?a, ?b) { ?a ex:p ?b . }",
    "http://ex.org/t": CP + 'TEMPLATE ex:t(?a, ?b) { "[" STR(?a) " " STR(?b) "]" }',
    "http://ex.org/s": CP + 'SELECT ex:s(?a, ?b) ?c WHERE { BIND(CONCAT(STR(?a), " ", STR(?b)) AS ?c) }',
    "http://ex.org/f": CP + 'FUNCTION ex:f(?a, ?b) { CONCAT(STR(?a), " ", STR(?b)) }',
}


def calls_generate(expected, query):
    assert_generates(CP + expected, CP + query, named_queries=CALLED)


def calls_template(query):
    return sg.template(CP + query, named_queries=CALLED, allow_network=False)


def test_nested_generate_call():
    calls_generate('<http://ex.org/line/1> ex:p "x1" . <http://ex.org/line/2> ex:p "x2" .',
                   'GENERATE { GENERATE ex:q(<http://ex.org/line/{?v}>, "x{?v}") . } WHERE { VALUES ?v { 1 2 } }')


def test_nested_generate_call_with_expression_argument():
    calls_generate("<http://ex.org/line/1> ex:p 2 . <http://ex.org/line/2> ex:p 3 .",
                   "GENERATE { GENERATE ex:q(<http://ex.org/line/{?v}>, ?v + 1) . } WHERE { VALUES ?v { 1 2 } }")


def test_nested_generate_call_in_graph_block():
    with sg.Execution(CP + "GENERATE { GRAPH ex:g { GENERATE ex:q(<http://ex.org/line/{?v}>, ?v) . } } "
                      "WHERE { VALUES ?v { 1 } }", named_queries=CALLED, allow_network=False) as ex:
        quads = list(ex.quads())
    assert quads == [("<http://ex.org/line/1>", "<http://ex.org/p>", f'"1"^^<{XSD}integer>', "<http://ex.org/g>")]


def test_call_in_nested_generate():
    calls_generate('<http://ex.org/line/1> ex:p "x1" . <http://ex.org/line/2> ex:p "x2" .',
                   'GENERATE { GENERATE { GENERATE ex:q(<http://ex.org/line/{?v}>, "x{?v}") . } '
                   'WHERE { VALUES ?v { 1 2 } } . } WHERE { }')


def test_iterator_call():
    calls_generate('ex:s ex:c "http://ex.org/line/1 x1" .',
                   'GENERATE { ex:s ex:c ?c } BIND(1 AS ?v) ITERATOR ex:s(<http://ex.org/line/{?v}>, "x{?v}") AS ?c')


def test_iterator_call_in_nested_generate():
    calls_generate('ex:s ex:c "http://ex.org/line/1 x1" .',
                   'GENERATE { GENERATE { ex:s ex:c ?c } ITERATOR ex:s(<http://ex.org/line/{?v}>, "x{?v}") AS ?c . } '
                   'WHERE { BIND(1 AS ?v) }')


def test_function_call_in_generate():
    calls_generate('ex:s ex:c "http://ex.org/line/1 x1" .',
                   'GENERATE { ex:s ex:c ?{ ex:f(<http://ex.org/line/{?v}>, "x{?v}") } } WHERE { BIND(1 AS ?v) }')


def test_template_call():
    assert calls_template('TEMPLATE { TEMPLATE ex:t(<http://ex.org/line/{?v}>, "x{?v}") . } '
                          'WHERE { VALUES ?v { 1 2 } }') == "[http://ex.org/line/1 x1][http://ex.org/line/2 x2]"


def test_function_call_in_template():
    assert calls_template('TEMPLATE { ex:f(<http://ex.org/line/{?v}>, "x{?v}") } WHERE { BIND(1 AS ?v) }') == \
        "http://ex.org/line/1 x1"


# -- QueryBaseTest: IRI() and embedded IRIs resolved against the base of the query ------------

BP = "BASE <http://ex.org/> PREFIX ex: <http://ex.org/ns#> "
BASED = {
    "http://ex.org/q": BP + "GENERATE <q>(?a) { ?a ex:p <in-q> . }",
    "http://ex.org/t": BP + 'TEMPLATE <t>(?a) { STR(?a) " " STR(IRI("in-t")) }',
    "http://ex.org/s": BP + "SELECT <s>(?a) ?c WHERE { BIND(STR(?a) AS ?c) }",
    "http://other.org/f": 'BASE <http://other.org/> FUNCTION <f>(?a) { CONCAT(STR(?a), " ", STR(IRI("in-f"))) }',
}


def based(expected, query):
    assert_generates(BP + expected, BP + query, named_queries=BASED)


def based_template(query):
    return sg.template(BP + query, named_queries=BASED, allow_network=False)


def test_iri_in_binding_clause():
    based("<s> ex:p <rel> .", 'GENERATE { <s> ex:p ?o } BIND(IRI("rel") AS ?o)')


def test_iri_with_embedded_expression_in_binding_clause():
    based("<s> ex:p <rel/1> .", "GENERATE { <s> ex:p ?o } BIND(1 AS ?v) BIND(<rel/{?v}> AS ?o)")


def test_iri_in_where_clause():
    based("<s> ex:p <rel> .", 'GENERATE { <s> ex:p ?o } WHERE { BIND(IRI("rel") AS ?o) }')


def test_iri_in_iterator_argument():
    based('<s> ex:p "http://ex.org/rel/1" .',
          'GENERATE { <s> ex:p ?o } BIND(1 AS ?v) '
          'ITERATOR <http://w3id.org/sparql-generate/iter/Split>(STR(<rel/{?v}>), ",") AS ?o')


def test_iri_in_nested_generate_binding_clause():
    based("<s> ex:p <rel/1> .",
          'GENERATE { GENERATE { <s> ex:p ?o } BIND(IRI(CONCAT("rel/", STR(?v))) AS ?o) . } WHERE { BIND(1 AS ?v) }')


def test_iri_in_template_clause():
    assert based_template("TEMPLATE { STR(<rel/{?v}>) } WHERE { BIND(1 AS ?v) }") == "http://ex.org/rel/1"


def test_iri_in_arguments_of_calls():
    based('<line/1> ex:p <in-q> . <s> ex:c "http://ex.org/line/1" .',
          "GENERATE { GENERATE <q>(<line/{?v}>) . <s> ex:c ?c } BIND(1 AS ?v) ITERATOR <s>(<line/{?v}>) AS ?c")
    assert based_template("TEMPLATE { TEMPLATE <t>(<line/{?v}>) . } WHERE { BIND(1 AS ?v) }") == \
        "http://ex.org/line/1 http://ex.org/in-t"


def test_function_has_its_own_base():
    assert based_template("TEMPLATE { <http://other.org/f>(<line/{?v}>) } WHERE { BIND(1 AS ?v) }") == \
        "http://ex.org/line/1 http://other.org/in-f"
    based('<s> ex:c "http://ex.org/line/1 http://other.org/in-f" .',
          "GENERATE { <s> ex:c ?{ <http://other.org/f>(<line/{?v}>) } } WHERE { BIND(1 AS ?v) }")


# -- LiteralDatatypeTest: a constant literal of a datatype outside XSD keeps it ----------------

LP = "PREFIX ex: <http://ex.org/> "


def test_datatype_in_generate_template():
    assert_generates(LP + 'ex:s ex:p "x"^^ex:dt1 , "w1"^^ex:dt1 .',
                     LP + 'GENERATE { ex:s ex:p "x"^^ex:dt1 , "w{?n}"^^ex:dt1 . } WHERE { BIND(1 AS ?n) }')


def test_datatype_in_expressions():
    assert_generates(LP + 'ex:s ex:p "y"^^ex:dt2 , "z"^^ex:dt2 ; ex:same true .',
                     LP + 'GENERATE { ex:s ex:p ?b , ?{ "z"^^ex:dt2 } ; ex:same ?same . } '
                          'WHERE { BIND("y"^^ex:dt2 AS ?b) BIND(sameTerm(?b, STRDT("y", ex:dt2)) AS ?same) }')


def test_datatype_in_binding_clause():
    assert_generates(LP + 'ex:s ex:p "y"^^ex:dt3 .', LP + 'GENERATE { ex:s ex:p ?b . } BIND("y"^^ex:dt3 AS ?b)')


def test_datatype_in_template():
    assert sg.template(LP + 'TEMPLATE { STR(DATATYPE("x"^^ex:dt4)) } WHERE { }') == "http://ex.org/dt4"


# -- EmbeddedExpressionInWhereTest: where an embedded IRI of the WHERE constrains its pattern --

WP = "BASE <http://ex/> PREFIX ex: <http://ex/ns#> "
WINPUT = (WP + '<p/1> ex:id "1" ; ex:name "one" . <p/2> ex:id "2" ; ex:name "two" . <p/3> ex:id "3" . '
          '<q/1> ex:label "label 1" . <q/2> ex:label "label 2" .')
NAMES = '<out/1> ex:name "one" . <out/2> ex:name "two" .'
LABELS = '<out/1> ex:label "label 1" . <out/2> ex:label "label 2" .'


def where(expected, query):
    assert_generates(WP + expected, WP + query, dataset=rdflib.Graph().parse(data=WINPUT, format="turtle"))


def test_variable_of_another_triple_of_the_pattern():
    where(NAMES, "GENERATE { <out/{?i}> ex:name ?n } WHERE { ?x ex:id ?i . <p/{?i}> ex:name ?n }")


def test_variable_of_a_later_triple_of_the_pattern():
    where(NAMES, "GENERATE { <out/{?i}> ex:name ?n } WHERE { <p/{?i}> ex:name ?n . ?x ex:id ?i }")


def test_literal_on_a_variable_of_another_triple():
    where(LABELS, 'GENERATE { <out/{?i}> ex:label ?l } WHERE { ?x ex:id ?i . ?y ex:label "label {?i}" . ?y ex:label ?l }')


def test_variable_of_a_later_element_of_the_group():
    where('<out/2> ex:name "two" .', 'GENERATE { <out/{?i}> ex:name ?n } WHERE { <p/{?i}> ex:name ?n . BIND("2" AS ?i) }')


def test_variable_of_an_earlier_element_of_the_group():
    where(NAMES, "GENERATE { <out/{?i}> ex:name ?n } WHERE { ?x ex:id ?i . FILTER(true) <p/{?i}> ex:name ?n }")


def test_same_node_twice():
    where(LABELS, "GENERATE { <out/{?i}> ex:label ?l } WHERE { ?x ex:id ?i . <q/{?i}> ex:label ?l . <q/{?i}> ex:label ?l2 }")


def test_variable_of_the_same_triple():
    self3 = '<p/1> ex:self "1" . <p/2> ex:self "2" . <p/3> ex:self "3" .'
    where(self3, "GENERATE { ?x ex:self ?i } WHERE { ?x ex:id ?i . FILTER(?x = <p/{?i}>) }")
    where(self3, "GENERATE { <p/{?i}> ex:self ?i } WHERE { <p/{?i}> ex:id ?i }")


def test_optional_on_a_variable_of_the_left_side():
    where('<out/1> ex:name "one" . <out/2> ex:name "two" . <out/3> ex:id "3" .',
          "GENERATE { <out/{?i}> ex:name ?n ; ex:id ?i3 } "
          "WHERE { ?x ex:id ?i OPTIONAL { <p/{?i}> ex:name ?n } BIND(IF(BOUND(?n), ?unbound, ?i) AS ?i3) }")


def test_optional_on_a_variable_of_the_parent_query():
    where('<out/1> ex:name "one" . <out/2> ex:name "two" . <out/3> ex:none "none" .',
          "GENERATE { GENERATE { <out/{?i}> ex:name ?n ; ex:none ?none } "
          'WHERE { OPTIONAL { <p/{?i}> ex:name ?n } BIND(IF(BOUND(?n), ?unbound, "none") AS ?none) } . } '
          "WHERE { ?x ex:id ?i }")


def test_optional_on_a_variable_of_the_optional():
    where(LABELS, "GENERATE { <out/{?i}> ex:label ?l } WHERE { ?x ex:id ?i OPTIONAL { ?x ex:id ?j . <q/{?j}> ex:label ?l } }")


def test_expression_in_error_matches_nothing():
    where("", "GENERATE { ex:s ex:name ?n } WHERE { <p/{?unbound}> ex:name ?n }")
    where('<out/3> ex:id "3" .', "GENERATE { <out/{?i}> ex:id ?i } "
          "WHERE { ?x ex:id ?i OPTIONAL { <p/{?i}> ex:name ?n } FILTER(!BOUND(?n)) }")


# -- CSVRaiseTest, JSONSurferRaiseTest, SQLRaiseTest: a RAISE under a streaming iterator -------

RP = ITER + "PREFIX ex: <http://ex.org/> "


def test_raise_under_csv_binding_clause():
    e = raised(RP + 'GENERATE { ex:s ex:p ?x } ITERATOR iter:CSV("id,lat\\nS1,45\\nS3,", "id", "lat") AS ?id ?lat '
               f'BIND(COALESCE(<{XSD}decimal>(?lat), RAISE("No valid latitude for stop", ?id)) AS ?x)')
    assert str(e) == 'No valid latitude for stop: "S3"'


def test_raise_under_csv_where_clause():
    e = raised(RP + 'GENERATE { ex:s ex:p ?x } ITERATOR iter:CSV("id\\nS1", "id") AS ?id '
               'WHERE { BIND(RAISE("stop", ?id) AS ?x) }')
    assert str(e) == 'stop: "S1"'


def test_raise_under_csv_batch():
    e = raised(RP + 'GENERATE { ex:s ex:p ?x } ITERATOR iter:CSV("id\\nS1\\nS2\\nS3", 2, "id") AS ?id '
               'BIND(IF(?id = "S3", RAISE("stop", ?id), 1) AS ?x)')
    assert str(e) == 'stop: "S3"'


def test_raise_under_csv_nested_generate():
    e = raised(RP + 'GENERATE { GENERATE { ex:s ex:p ?x } WHERE { BIND(RAISE("stop", ?id) AS ?x) } . } '
               'ITERATOR iter:CSV("id\\nS1", "id") AS ?id')
    assert str(e) == 'stop: "S1"'


def test_raise_under_jsonsurfer():
    e = raised(RP + "GENERATE { ex:s ex:p ?x } ITERATOR iter:JSONSurfer('[\\{\"id\": 1\\}, \\{\"id\": 2\\}]', \"$[*]\", "
               '"$.id") AS ?o ?id BIND(IF(?id = 2, RAISE("stop", ?id), 1) AS ?x)')
    assert str(e) == "stop: 2"
    e = raised(RP + "GENERATE { ex:s ex:p ?x } ITERATOR iter:JSONSurfer('[\\{\"id\": 1\\}, \\{\"id\": 2\\}]', \"$[*]\", 1, "
               '"$.id") AS ?o ?id WHERE { BIND(IF(?id = 2, RAISE("stop", ?id), 1) AS ?x) }')
    assert str(e) == "stop: 2"


def test_raise_under_sql(tmp_path):
    db = tmp_path / "stops.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE stops (id VARCHAR(10), lat VARCHAR(10))")
    con.execute("INSERT INTO stops VALUES ('S1', '45'), ('S3', 'none')")
    con.commit()
    con.close()
    e = raised(RP + f'GENERATE {{ ex:s ex:p ?x }} ITERATOR iter:SQL(<jdbc:sqlite:{db}>, "SELECT id, lat FROM stops") '
               f'AS ?id ?lat BIND(COALESCE(<{XSD}decimal>(?lat), RAISE("No valid latitude for stop", ?id)) AS ?x)')
    assert str(e) == 'No valid latitude for stop: "S3"'


# -- CSVStrictTest -------------------------------------------------------------------------------


def strict_rows(csv, args, vars_):
    return sg.select(ITER + f'SELECT * ITERATOR iter:CSVStrict("{csv}"{args}) AS {vars_} WHERE {{ }}')


def test_strict_empty_cell_is_the_empty_string():
    rows = strict_rows('id,name\\n1,a\\n2,\\n3,\\"\\"', ', "id", "name"', "?id ?name ?row")
    assert [(r['id'], r['name'], r['row']) for r in rows] == [('"1"', '"a"', f'"0"^^<{XSD}integer>'), ('"2"', '""', f'"1"^^<{XSD}integer>'),
                                        ('"3"', '""', f'"2"^^<{XSD}integer>')]


def test_strict_batches():
    assert len(strict_rows("id\\n1\\n2\\n3", ', 2, "id"', "?id")) == 3


def strict_error(csv, args, vars_) -> sg.RaiseError:
    with pytest.raises(sg.RaiseError) as info:
        strict_rows(csv, args, vars_)
    return info.value


def test_strict_row_with_fewer_fields_is_an_error():
    e = strict_error("id,name,age\\n6,Phoebe 37", ', "id", "name"', "?id ?name")
    assert str(e) == 'A row of the CSV document does not have as many fields as the header: "(inline document)", 0, 2, 3'


def test_strict_row_with_more_fields_is_an_error():
    strict_error("id,name\\n6,Phoebe,37", ', "id"', "?id")


def test_strict_unknown_column_is_an_error():
    e = strict_error("id,name\\n6,Phoebe", ', "age"', "?age")
    assert str(e) == 'The CSV document has no column: "(inline document)", "age"'


def test_strict_empty_lines_are_skipped():
    assert len(strict_rows("id,name\\n6,Phoebe\\n\\n", ', "id"', "?id")) == 1


def test_strict_white_space_and_quotes(tmp_path):
    """As univocity (the Java engine, checked with its 2.6.1 JAR): the white space around an
    unquoted value is removed, a quoted value is kept, blank lines are skipped, a quoted
    value may span lines; the IRI of the document in the messages."""
    (tmp_path / "a.csv").write_bytes(b'id, name ,x\r\n1, a ," b "\r\n\r\n2,,""\r\n  \r\n3,c,"d\ne"\n')
    q = (ITER + 'PREFIX ex: <http://ex/> GENERATE { ex:s ex:p "[{?id}|{?name}|{?x}|{?r}]" . } '
         f'ITERATOR iter:CSVStrict(<file://{tmp_path}/a.csv>, "id", "name", "x") AS ?id ?name ?x ?r')
    assert sorted(o for _, _, o in sg.generate(q)) == ['"[1|a| b |0]"', '"[2|||1]"', '"[3|c|d\\ne|2]"']
    (tmp_path / "b.csv").write_text("id,name,age\n6,Phoebe 37\n")
    with pytest.raises(sg.RaiseError) as info:
        list(sg.generate(q.replace("a.csv", "b.csv").replace('"x")', '"age")')))
    assert str(info.value) == (f"A row of the CSV document does not have as many fields as the header: "
                               f"<file://{tmp_path}/b.csv>, 0, 2, 3")


def test_strict_error_only_when_evaluated():
    """A data error of the iterator is raised where the iterator is evaluated: not in a
    sub-query that no solution reaches."""
    q = (ITER + 'PREFIX ex: <http://ex/> GENERATE { GENERATE { ex:s ex:p ?id } '
         'ITERATOR iter:CSVStrict("id\\n1,2", "id") AS ?id . } WHERE { FILTER(false) }')
    assert list(sg.generate(q)) == []


# -- SPARQLIteratorTest --------------------------------------------------------------------------

SPX = ITER + "PREFIX ex: <http://ex/> "
FRIENDS = ('<http://ex/0> <http://xmlns.com/foaf/0.1/name> "Monica" ; <http://xmlns.com/foaf/0.1/age> 33 . '
           '<http://ex/1> <http://xmlns.com/foaf/0.1/name> "Rachel" .')
SQUERY = ("PREFIX foaf: <http://xmlns.com/foaf/0.1/> "
          "SELECT ?person ?name ?age WHERE \\\\{ ?person foaf:name ?name OPTIONAL \\\\{ ?person foaf:age ?age } } ORDER BY ?person")


def test_sparql_inline_document():
    assert_generates('PREFIX ex: <http://ex/> <http://ex/0> ex:name "Monica" ; ex:age 33 ; ex:row 0 . '
                     '<http://ex/1> ex:name "Rachel" ; ex:row 1 .',
                     SPX + f'GENERATE {{ ?person ex:name ?name ; ex:age ?age ; ex:row ?row }} ITERATOR iter:SPARQL("""{FRIENDS}""", '
                     f'"{SQUERY}", "person", "name", "age") AS ?person ?name ?age ?row'.replace("\\\\", "\\"))


def test_sparql_projected_variables_by_default():
    assert_generates('PREFIX ex: <http://ex/> <http://ex/0> ex:name "Monica" . <http://ex/1> ex:name "Rachel" .',
                     SPX + f'GENERATE {{ ?person ex:name ?name }} ITERATOR iter:SPARQL("""{FRIENDS}""", "{SQUERY}") '
                     'AS ?person ?name'.replace("\\\\", "\\"))


def test_sparql_rdflib_fallback(monkeypatch):
    """Without pyoxigraph, rdflib evaluates the query, the lexical forms kept."""
    import builtins

    real = builtins.__import__

    def no_oxigraph(name, *a, **k):
        if name == "pyoxigraph":
            raise ImportError(name)
        return real(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", no_oxigraph)
    from sparql_generate.iterators import _sparql_rows

    rows, vars_ = _sparql_rows('<http://ex/0> <http://ex/v> "01"^^<http://www.w3.org/2001/XMLSchema#integer> .',
                               "N_TRIPLES", None, "SELECT ?v WHERE { ?s ?p ?v }", [])
    assert vars_ == ["v"] and rows == [[{"k": 2, "v": "01", "d": XSD + "integer"}]]


# -- XPathTest: relative sub-queries ----------------------------------------------------------

XP = "PREFIX ex: <http://ex/> PREFIX ite: <http://w3id.org/sparql-generate/iter/> "


def test_xpath_relative_sub_query_reaches_ancestors():
    xml = ("<companies><company id='25'><name>Tech</name><departments><department><name>Engineering</name></department>"
           "<department><name>Marketing</name></department></departments></company>"
           "<company id='35'><name>Retail</name><departments><department><name>Sales</name></department></departments>"
           "</company></companies>")
    assert_generates(XP + 'ex:c25 ex:department "Engineering", "Marketing" ; ex:in "Tech" . '
                     'ex:c35 ex:department "Sales" ; ex:in "Retail" .',
                     XP + f'GENERATE {{ ?s ex:department ?name ; ex:in ?company }} ITERATOR ite:XPath("{xml}", '
                     '"/companies/company/departments/department", "../../@id", "name/text()", '
                     '"ancestor::company/name/text()") AS ?d ?id ?name ?company '
                     'WHERE { BIND(IRI(CONCAT("http://ex/c", ?id)) AS ?s) }')


def test_xpath_absolute_sub_query_is_from_the_node():
    xml = "<companies><company id='25'><department><name>Engineering</name></department></company></companies>"
    assert_generates(XP + 'ex:d ex:name "Engineering" .',
                     XP + f'GENERATE {{ ex:d ex:name ?name ; ex:parent ?parent }} ITERATOR ite:XPath("{xml}", '
                     '"//department", "/department/name/text()", "/department/../@id") AS ?d ?name ?parent')


# -- a named SELECT query called as an iterator ---------------------------------------------------


def test_named_select_as_iterator_keeps_the_order():
    """The rows in their order, each followed by the solutions of its call in their order
    (a TEMPLATE shows the order of the Java engine's iterators)."""
    named = {"http://ex/s": ITER + 'SELECT <http://ex/s>(?n) ?i ITERATOR iter:Split(?n, "-") AS ?i WHERE { }'}
    text = sg.template(ITER + 'TEMPLATE { ?n ":" ?i " " } ITERATOR iter:Split("c-a-b,b-a,a", ",") AS ?n '
                       "ITERATOR <http://ex/s>(?n) AS ?i", named_queries=named, allow_network=False)
    assert text == "c-a-b:c c-a-b:a c-a-b:b b-a:b b-a:a a:a "


def test_unknown_iterator_is_still_an_error():
    with pytest.raises(sg.CompileError):
        list(sg.generate(ITER + "GENERATE { <s> <p> ?x } ITERATOR <http://ex/none>(1) AS ?x", allow_network=False))


# -- fun:unsafeIRI (rml:UnsafeIRI) ----------------------------------------------------------------


def test_unsafe_iri():
    q = ('PREFIX fun: <http://w3id.org/sparql-generate/fn/> PREFIX ex: <http://ex/> '
         'GENERATE { ?a ex:p ?b . ex:s ex:ok ?c . ex:s ex:rel ?d . ex:s ex:str ?e . ex:s ex:k ?{ fun:unsafeIRI("http://x/a b") } } '
         'WHERE { VALUES ?x { "http://ex/a b" "nope" } BIND(fun:unsafeIRI(?x) AS ?a) BIND(fun:unsafeIRI(ex:i) AS ?b) '
         'BIND(fun:unsafeIRI("http://ex/ok") AS ?c) BIND(fun:unsafeIRI("rel") AS ?d) BIND(STR(?a) AS ?e) }')
    got = sorted(set(sg.generate(q)))
    assert got == [("<http://ex/a\\u0020b>", "<http://ex/p>", "<http://ex/i>"),
                   ("<http://ex/s>", "<http://ex/k>", "<http://x/a\\u0020b>"),
                   ("<http://ex/s>", "<http://ex/ok>", "<http://ex/ok>"),
                   ("<http://ex/s>", "<http://ex/str>", '"http://ex/a b"')]


def test_xpath_prefixes_of_the_document():
    """The prefixes declared by the document may be used in the XPath expressions (RMLSTC0007d
    of RML-IO, the Java engine's namespace context); the sub-queries of the matched node too."""
    xml = ("<Friends xmlns:ex='http://example.org'><ex:Character id='0'><ex:name>Monica</ex:name></ex:Character>"
           "<ex:Character id='1'><ex:name>Rachel</ex:name></ex:Character></Friends>")
    assert_generates(XP + 'ex:c0 ex:name "Monica" . ex:c1 ex:name "Rachel" .',
                     XP + f'GENERATE {{ ?s ex:name ?name }} ITERATOR ite:XPath("{xml}", "//Friends/ex:Character", '
                     '"@id", "ex:name/text()") AS ?c ?id ?name WHERE { BIND(IRI(CONCAT("http://ex/c", ?id)) AS ?s) }')
    assert_generates(XP + 'ex:c0 ex:name "Monica" . ex:c1 ex:name "Rachel" .',
                     XP + f'GENERATE {{ GENERATE {{ ?s ex:name ?name }} ITERATOR ite:XPath(?c, "/*/ex:name/text()") AS ?name '
                     f'ITERATOR ite:XPath(?c, "/*/@id") AS ?id WHERE {{ BIND(IRI(CONCAT("http://ex/c", ?id)) AS ?s) }} . }} '
                     f'ITERATOR ite:XPath("{xml}", "//Friends/ex:Character") AS ?c')
