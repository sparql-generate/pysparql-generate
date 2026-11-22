"""PERFORM queries (rule P): the sequence of action instances <u, t1..tk>."""
import sparql_generate as sg

PFX = "BASE <http://ex.org/> PREFIX ex: <http://ex.org/> PREFIX iter: <http://w3id.org/sparql-generate/iter/>\n"
I = "^^<http://www.w3.org/2001/XMLSchema#integer>"


def test_actions_in_the_order_of_rule_p():
    acts = sg.perform(PFX + 'PERFORM { PERFORM ex:send(?x, "a") . PERFORM ex:log(?x) . } WHERE { VALUES ?x { 2 1 } } ORDER BY ?x')
    assert acts == [("http://ex.org/send", [f'"1"{I}', '"a"']), ("http://ex.org/send", [f'"2"{I}', '"a"']),
                    ("http://ex.org/log", [f'"1"{I}']), ("http://ex.org/log", [f'"2"{I}'])]


def test_sub_perform_on_all_parent_solutions_and_named_perform():
    q = PFX + '''PERFORM { PERFORM ex:a(?x) .
                         PERFORM { PERFORM ex:b(?x, ?y) . } ITERATOR iter:for(1, 1, 3) AS ?y WHERE {} ORDER BY ?x ?y . }
                 WHERE { VALUES ?x { 1 2 } } ORDER BY ?x'''
    acts = [(u.rsplit("/", 1)[1], [a.split('"')[1] for a in args]) for u, args in sg.perform(q)]
    assert acts == [("a", ["1"]), ("a", ["2"]), ("b", ["1", "1"]), ("b", ["1", "2"]), ("b", ["2", "1"]), ("b", ["2", "2"])]
    lib = {"http://ex.org/p.rqg": PFX + 'PERFORM <http://ex.org/p.rqg>(?v) { PERFORM ex:log(?v) . } WHERE {} ORDER BY ?v'}
    acts = sg.perform(PFX + 'PERFORM { PERFORM <p.rqg>(?x) . } WHERE { VALUES ?x { 1 2 } }', named_queries=lib)
    assert acts == [("http://ex.org/log", [f'"1"{I}']), ("http://ex.org/log", [f'"2"{I}'])]


def test_unbound_arguments_and_write(tmp_path):
    acts = sg.perform(PFX + 'PERFORM { PERFORM ex:f(?x, ?nothing) . } WHERE { VALUES ?x { ex:s } }')
    assert acts == [("http://ex.org/f", ["<http://ex.org/s>", None])]
    out = tmp_path / "actions.txt"
    sg.generate_to(PFX + 'PERFORM { PERFORM ex:f(?x) . } WHERE { VALUES ?x { ex:s } }', str(out))
    assert out.read_text() == "<http://ex.org/f>(<http://ex.org/s>)\n"
