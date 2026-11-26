"""Recursive-descent parser for SPARQL-Generate (grammar ``spargl.jj``).

``parse(text, base=None)`` returns an :class:`~sparql_generate.ast.Query`.
Prefixed names are expanded, relative IRIs are resolved against the BASE, and
embedded expressions (``{ ?x }`` in strings and IRIs, ``?{ expr }`` nodes) are
kept as :class:`XIRI`, :class:`XLiteral` and :class:`XExpr` nodes for the
compiler to normalise.
"""
from __future__ import annotations

from typing import List, Optional

from . import ast as A
from .lexer import Lexer, ParseError, Token
from .terms import resolve_iri

BUILTIN_1 = {
    "STR", "LANG", "DATATYPE", "IRI", "URI", "ABS", "CEIL", "FLOOR", "ROUND", "STRLEN",
    "UCASE", "LCASE", "ENCODE_FOR_URI", "YEAR", "MONTH", "DAY", "HOURS", "MINUTES", "SECONDS",
    "TIMEZONE", "TZ", "MD5", "SHA1", "SHA224", "SHA256", "SHA384", "SHA512", "ISIRI", "ISURI",
    "ISBLANK", "ISLITERAL", "ISNUMERIC",
}
BUILTIN_2 = {"LANGMATCHES", "CONTAINS", "STRSTARTS", "STRENDS", "STRBEFORE", "STRAFTER", "STRLANG",
             "STRDT", "SAMETERM"}
BUILTIN_0 = {"RAND", "NOW", "UUID", "STRUUID"}
AGGREGATES = {"COUNT", "SUM", "MIN", "MAX", "AVG", "SAMPLE", "GROUP_CONCAT"}


class Parser:
    def __init__(self, text: str, base: Optional[str] = None):
        self.lexer = Lexer(text)
        self.text = self.lexer.text
        self.base = base
        self.prefixes: dict = {}
        self.cur: Token = self.lexer.token(0)
        self._bnode_counter = 0
        self._in_template = False
        self._allow_aggregates = False

    # -- token helpers ----------------------------------------------------
    def error(self, message: str, tok: Optional[Token] = None) -> ParseError:
        tok = tok or self.cur
        return ParseError(message, tok.line, tok.col, self.text)

    def advance(self) -> Token:
        tok = self.cur
        self.cur = self.lexer.token(tok.end)
        return tok

    def rescan(self, mode: str) -> Token:
        """Re-read the current token (which must start with ``}``) in a
        string/IRI continuation mode."""
        self.cur = self.lexer.token(self.cur.pos, mode)
        return self.cur

    def at(self, type_: str, value=None) -> bool:
        if self.cur.type != type_:
            return False
        return value is None or self.cur.value == value

    def at_kw(self, *names: str) -> bool:
        return self.cur.type == "KW" and self.cur.value in names

    def accept(self, type_: str, value=None) -> Optional[Token]:
        if self.at(type_, value):
            return self.advance()
        return None

    def accept_kw(self, *names: str) -> Optional[Token]:
        if self.at_kw(*names):
            return self.advance()
        return None

    def expect(self, type_: str, value=None) -> Token:
        if not self.at(type_, value):
            want = value if value is not None else type_
            raise self.error(f"expected {want!r} but found {self._describe()}")
        return self.advance()

    def expect_kw(self, *names: str) -> Token:
        if not self.at_kw(*names):
            raise self.error(f"expected {' or '.join(names)} but found {self._describe()}")
        return self.advance()

    def _describe(self) -> str:
        t = self.cur
        if t.type == "EOF":
            return "end of input"
        if t.type in ("KW",):
            return t.value
        if t.type in ("VAR",):
            return "?" + t.value
        return repr(self.text[t.pos:t.end])

    def fresh_bnode(self) -> A.BNode:
        self._bnode_counter += 1
        return A.BNode(f"b{self._bnode_counter}", anonymous=True)

    # -- entry point ------------------------------------------------------
    def parse_unit(self) -> A.Query:
        q = self.parse_query()
        if not self.at("EOF"):
            raise self.error(f"unexpected {self._describe()} after the end of the query")
        q.source_text = self.text
        return q

    def parse_query(self) -> A.Query:
        q = A.Query()
        self.parse_prologue(q)
        if self.at_kw("SELECT"):
            self.parse_select_query(q)
            self.parse_values_clause(q)
        elif self.at_kw("GENERATE"):
            self.parse_generate_query(q)
            self.parse_values_clause(q)
        elif self.at_kw("TEMPLATE"):
            self.parse_template_query(q)
            self.parse_values_clause(q)
        elif self.at_kw("PERFORM"):
            self.parse_perform_query(q)
            self.parse_values_clause(q)
        elif self.at_kw("FUNCTION"):
            self.parse_function(q)
        else:
            raise self.error(f"expected GENERATE, TEMPLATE, SELECT, PERFORM or FUNCTION but found {self._describe()}")
        return q

    def parse_prologue(self, q: A.Query):
        while True:
            if self.accept_kw("BASE"):
                tok = self.expect("IRIREF")
                self.base = resolve_iri(tok.value, self.base)
                q.base = self.base
            elif self.accept_kw("PREFIX"):
                tok = self.expect("PNAME_NS")
                iri = self.expect("IRIREF")
                self.prefixes[tok.value] = resolve_iri(iri.value, self.base)
            else:
                break
        q.prefixes = dict(self.prefixes)
        if q.base is None:
            q.base = self.base

    # -- query forms ------------------------------------------------------
    def parse_function(self, q: A.Query):
        self.expect_kw("FUNCTION")
        q.kind = "FUNCTION"
        q.name = A.IRI(self.parse_iri())
        q.signature = self.parse_var_list()
        self.expect("{")
        q.function_expr = self.parse_primary_expression()
        self.expect("}")

    def parse_generate_query(self, q: A.Query):
        self.expect_kw("GENERATE")
        q.kind = "GENERATE"
        if self.at("IRIREF") or self.at("PNAME_LN") or self.at("PNAME_NS"):
            q.name = A.IRI(self.parse_iri())
            if self.at("NIL") or self.at("("):
                q.signature = self.parse_var_list()
        q.generate_clause = self.parse_generate_clause()
        self.parse_dataset_clauses(q)
        self.parse_binding_clauses(q)
        if self.at_kw("WHERE") or self.at("{"):
            self.parse_where_clause(q)
        self.parse_solution_modifier(q)
        self.parse_post_select(q)

    def parse_template_query(self, q: A.Query):
        self.expect_kw("TEMPLATE")
        q.kind = "TEMPLATE"
        if self.at("IRIREF") or self.at("PNAME_LN") or self.at("PNAME_NS"):
            q.name = A.IRI(self.parse_iri())
            if self.at("NIL") or self.at("("):
                q.signature = self.parse_var_list()
        self.parse_template_clause(q)
        self.parse_dataset_clauses(q)
        self.parse_binding_clauses(q)
        if self.at_kw("DISTINCT", "REDUCED", "WHERE") or self.at("{"):
            if self.accept_kw("DISTINCT"):
                q.distinct = True
            elif self.accept_kw("REDUCED"):
                q.reduced = True
            self.parse_where_clause(q)
        self.parse_solution_modifier(q)

    def parse_perform_query(self, q: A.Query):
        self.expect_kw("PERFORM")
        q.kind = "PERFORM"
        if self.at("IRIREF") or self.at("PNAME_LN") or self.at("PNAME_NS"):
            q.name = A.IRI(self.parse_iri())
            if self.at("NIL") or self.at("("):
                q.signature = self.parse_var_list()
        q.perform_clause = self.parse_perform_clause()
        self.parse_dataset_clauses(q)
        self.parse_binding_clauses(q)
        if self.at_kw("DISTINCT", "REDUCED", "WHERE") or self.at("{"):
            if self.accept_kw("DISTINCT"):
                q.distinct = True
            elif self.accept_kw("REDUCED"):
                q.reduced = True
            self.parse_where_clause(q)
        self.parse_solution_modifier(q)
        self.parse_post_select(q)

    def parse_select_query(self, q: A.Query):
        self.expect_kw("SELECT")
        q.kind = "SELECT"
        if self.at("IRIREF") or self.at("PNAME_LN") or self.at("PNAME_NS"):
            q.name = A.IRI(self.parse_iri())
            if self.at("NIL") or self.at("("):
                q.signature = self.parse_var_list()
        self.parse_select_clause_body(q)
        self.parse_dataset_clauses(q)
        self.parse_binding_clauses(q)
        self.parse_where_clause(q)
        self.parse_solution_modifier(q)

    def parse_select_clause_body(self, q: A.Query):
        if self.accept_kw("DISTINCT"):
            q.distinct = True
        elif self.accept_kw("REDUCED"):
            q.reduced = True
        self._allow_aggregates = True
        if self.accept("*"):
            q.select_star = True
        else:
            found = False
            while True:
                if self.at("VAR"):
                    q.select_vars.append((A.Var(self.advance().value), None))
                    found = True
                elif self.at("("):
                    self.advance()
                    e = self.parse_expression()
                    self.expect_kw("AS")
                    v = self.parse_var()
                    self.expect(")")
                    q.select_vars.append((v, e))
                    found = True
                else:
                    break
            if not found:
                raise self.error(f"expected a variable, (expression AS ?var) or * but found {self._describe()}")
        self._allow_aggregates = False

    def parse_sub_select(self) -> A.Query:
        q = A.Query(kind="SELECT", base=self.base, prefixes=dict(self.prefixes), is_sub=True)
        self.expect_kw("SELECT")
        self.parse_select_clause_body(q)
        self.parse_where_clause(q)
        self.parse_solution_modifier(q)
        self.parse_values_clause(q)
        return q

    def parse_var_list(self) -> List[A.Var]:
        if self.accept("NIL"):
            return []
        self.expect("(")
        out = [self.parse_var()]
        while self.accept(","):
            out.append(self.parse_var())
        self.expect(")")
        return out

    def parse_var(self) -> A.Var:
        tok = self.expect("VAR")
        return A.Var(tok.value)

    # -- GENERATE clause ---------------------------------------------------
    def parse_generate_clause(self) -> List[object]:
        self.expect("{")
        self._allow_aggregates = True
        out = self.parse_generate_clause_sub(in_graph=False)
        self._allow_aggregates = False
        self.expect("}")
        return out

    def parse_generate_clause_sub(self, in_graph: bool) -> List[object]:
        """Triples, sub-queries ``GENERATE ... .`` and ``GRAPH VarOrXIri { ... }`` blocks (quad
        patterns, as the QuadsNotTriples of SPARQL 1.1 Update); a GRAPH block cannot contain
        another one, but a sub-query in a GRAPH block can have its own."""
        out: List[object] = []

        def triples():
            if not self.at("}") and not self.at_kw("GENERATE", "GRAPH"):
                out.append(A.GenerateTriples(self.parse_construct_triples()))

        triples()
        while self.at_kw("GENERATE", "GRAPH"):
            if self.at_kw("GENERATE"):
                sub = self.parse_sub_generate_query()
                self.expect(".")
                out.append(A.SubQuery(sub))
            else:
                tok = self.cur
                if in_graph:
                    raise self.error("GRAPH blocks of a GENERATE template cannot be nested", tok)
                self.advance()
                node = self.parse_var_or_xiri()
                self.expect("{")
                elements = self.parse_generate_clause_sub(in_graph=True)
                self.expect("}")
                self.accept(".")
                out.append(A.GenerateGraph(node, elements))
            triples()
        return out

    def parse_sub_generate_query(self) -> A.Query:
        tok = self.expect_kw("GENERATE")
        q = A.Query(kind="GENERATE", base=self.base, prefixes=dict(self.prefixes), is_sub=True)
        if self.at("{"):
            q.generate_clause = self.parse_generate_clause()
        else:
            q.name = self.parse_name_node()
            if self.at("NIL") or self.at("("):
                q.call_params = self.parse_arg_list()
        self.parse_dataset_clauses(q)
        self.parse_binding_clauses(q)
        if self.accept_kw("WHERE"):
            q.where = self.parse_group_graph_pattern()
        self.parse_solution_modifier(q)
        self.parse_post_select(q)
        return q

    def parse_name_node(self):
        """Name of a called query: VarOrXIri | IRI(expr) | URI(expr)."""
        if self.at_kw("IRI", "URI"):
            self.advance()
            self.expect("(")
            e = self.parse_expression()
            self.expect(")")
            return A.Call("IRI", (e,))
        return self.parse_var_or_xiri()

    def parse_sub_template_query(self) -> A.Query:
        self.expect_kw("TEMPLATE")
        q = A.Query(kind="TEMPLATE", base=self.base, prefixes=dict(self.prefixes), is_sub=True)
        if self.at("{"):
            self.parse_template_clause(q)
        else:
            q.name = self.parse_name_node()
            q.call_params = self.parse_arg_list()
        self.parse_dataset_clauses(q)
        self.parse_binding_clauses(q)
        if self.at_kw("DISTINCT", "REDUCED", "WHERE"):
            if self.accept_kw("DISTINCT"):
                q.distinct = True
            elif self.accept_kw("REDUCED"):
                q.reduced = True
            self.expect_kw("WHERE")
            q.where = self.parse_group_graph_pattern()
        self.parse_solution_modifier(q)
        self.parse_post_select(q)
        return q

    def parse_sub_perform_query(self) -> A.Query:
        self.expect_kw("PERFORM")
        q = A.Query(kind="PERFORM", base=self.base, prefixes=dict(self.prefixes), is_sub=True)
        if self.at("{"):
            q.perform_clause = self.parse_perform_clause()
        else:
            q.name = self.parse_name_node()
            if self.at("NIL") or self.at("("):
                q.call_params = self.parse_arg_list()
        self.parse_binding_clauses(q)
        if self.accept_kw("WHERE"):
            q.where = self.parse_group_graph_pattern()
        self.parse_solution_modifier(q)
        self.parse_post_select(q)
        return q

    def parse_perform_clause(self) -> List[object]:
        self.expect("{")
        out: List[object] = []
        while self.at_kw("PERFORM"):
            sub = self.parse_sub_perform_query()
            self.expect(".")
            out.append(A.SubQuery(sub))
        self.expect("}")
        return out

    # -- TEMPLATE clause ---------------------------------------------------
    def parse_template_clause(self, q: A.Query):
        self.expect("{")
        self._allow_aggregates = True
        if self.at_kw("BEFORE"):
            self.advance()
            self.expect("=")
            q.template_before = self.parse_expression()
            self.expect(";")
        elems: List[object] = []
        while not self.at("}") and not self.at(";") and not self.at("EOF"):
            elems.append(self.parse_texpression())
        q.template_clause = elems
        if self.accept(";"):
            if self.accept_kw("SEPARATOR"):
                self.expect("=")
                q.template_separator = self.parse_expression()
                if self.accept(";"):
                    self.expect_kw("AFTER")
                    self.expect("=")
                    q.template_after = self.parse_expression()
            else:
                self.expect_kw("AFTER")
                self.expect("=")
                q.template_after = self.parse_expression()
        self._allow_aggregates = False
        self.expect("}")

    def parse_texpression(self):
        if self.at_kw("BOX"):
            return self.parse_box()
        if self.at_kw("FORMAT"):
            return self.parse_format()
        if self.at_kw("GROUP"):
            return self.parse_tgroup()
        if self.at_kw("TEMPLATE"):
            q = self.parse_sub_template_query()
            self.expect(".")
            return A.SubQuery(q)
        return A.TExpr(self.parse_primary_expression_not_bracketted())

    def parse_primary_expression_not_bracketted(self):
        if self.at("KW") and self.cur.value not in ("TRUE", "FALSE"):
            return self.parse_builtin_call()
        if self.at("IRIREF") or self.at("PNAME_LN") or self.at("PNAME_NS") or self.at("XIRI_START"):
            return self.parse_xiri_or_function()
        if self.at("STRING") or self.at("STRING_START"):
            return self.parse_xrdf_literal()
        if self.at("INTEGER") or self.at("DECIMAL") or self.at("DOUBLE") or self.at("+") or self.at("-"):
            return self.parse_numeric_literal()
        if self.at_kw("TRUE", "FALSE"):
            return self.parse_boolean_literal()
        if self.at("VAR"):
            return self.parse_var()
        raise self.error(f"expected a template expression but found {self._describe()}")

    def parse_box(self) -> A.TBox:
        self.expect_kw("BOX")
        self.expect("{")
        elems = []
        while not self.at("}"):
            elems.append(self.parse_texpression())
        self.expect("}")
        return A.TBox(elems)

    def parse_format(self) -> A.TFormat:
        self.expect_kw("FORMAT")
        self.expect("{")
        fmt = self.parse_primary_expression_not_bracketted()
        elems = [self.parse_texpression()]
        while not self.at("}"):
            elems.append(self.parse_texpression())
        self.expect("}")
        return A.TFormat(fmt, elems)

    def parse_tgroup(self) -> A.TGroup:
        self.expect_kw("GROUP")
        distinct = bool(self.accept_kw("DISTINCT"))
        self.expect("{")
        elems = []
        sep = None
        while not self.at("}"):
            if self.at(";"):
                self.advance()
                self.expect_kw("SEPARATOR")
                self.expect("=")
                sep = self.expect("STRING").value
                break
            if self.at_kw("BOX"):
                elems.append(self.parse_box())
            elif self.at_kw("FORMAT"):
                elems.append(self.parse_format())
            else:
                elems.append(A.TExpr(self.parse_primary_expression_not_bracketted()))
        self.expect("}")
        return A.TGroup(elems, distinct, sep)

    # -- binding clauses, dataset, where, modifiers --------------------------
    def parse_dataset_clauses(self, q: A.Query):
        while self.accept_kw("FROM"):
            if self.accept_kw("NAMED"):
                q.dataset_clauses.append(A.DatasetClause(expr=self.parse_expression(), named=True))
            elif self.at_kw("GENERATE"):
                sub = self.parse_sub_generate_query()
                dc = A.DatasetClause(generate=sub)
                if self.accept_kw("NAMED"):
                    dc.named = True
                    dc.expr = self.parse_expression()
                self.expect(".")
                q.dataset_clauses.append(dc)
            else:
                q.dataset_clauses.append(A.DatasetClause(expr=self.parse_expression()))

    def parse_binding_clauses(self, q: A.Query):
        while True:
            if self.accept_kw("ITERATOR"):
                expr = self.parse_function_call()
                self.expect_kw("AS")
                vars_ = [self.parse_var()]
                while self.at("VAR"):
                    vars_.append(self.parse_var())
                q.binding_clauses.append(A.IteratorClause(expr, vars_))
            elif self.accept_kw("SOURCE"):
                source = self.parse_var_or_xiri()
                accept = None
                if self.accept_kw("ACCEPT"):
                    accept = self.parse_var_or_xiri()
                self.expect_kw("AS")
                var = self.parse_var()
                q.binding_clauses.append(A.SourceClause(source, accept, var))
            elif self.at_kw("BIND"):
                q.binding_clauses.append(self.parse_bind())
            else:
                return

    def parse_bind(self) -> A.Bind:
        self.expect_kw("BIND")
        self.expect("(")
        e = self.parse_expression()
        self.expect_kw("AS")
        v = self.parse_var()
        self.expect(")")
        return A.Bind(e, v)

    def parse_where_clause(self, q: A.Query):
        self.accept_kw("WHERE")
        q.where = self.parse_group_graph_pattern()

    def parse_solution_modifier(self, q: A.Query):
        if self.accept_kw("GROUP"):
            self.expect_kw("BY")
            found = False
            while True:
                if self.at("VAR"):
                    q.group_by.append(self.parse_var())
                elif self.at("("):
                    self.advance()
                    e = self.parse_expression()
                    v = None
                    if self.accept_kw("AS"):
                        v = self.parse_var()
                    self.expect(")")
                    q.group_by.append((e, v))
                elif self.at("KW") and self.cur.value not in ("HAVING", "ORDER", "LIMIT", "OFFSET", "VALUES", "EXPRESSIONS", "TRUE", "FALSE"):
                    q.group_by.append((self.parse_builtin_call(), None))
                elif self.at("IRIREF") or self.at("PNAME_LN") or self.at("PNAME_NS"):
                    q.group_by.append((self.parse_function_call(), None))
                else:
                    break
                found = True
            if not found:
                raise self.error("expected a GROUP BY condition")
        if self.accept_kw("HAVING"):
            self._allow_aggregates = True
            q.having.append(self.parse_constraint())
            while self.at("(") or self.at("KW") or self.at("IRIREF") or self.at("PNAME_LN") or self.at("PNAME_NS"):
                if self.at_kw("ORDER", "LIMIT", "OFFSET", "VALUES", "EXPRESSIONS"):
                    break
                q.having.append(self.parse_constraint())
            self._allow_aggregates = False
        if self.accept_kw("ORDER"):
            self.expect_kw("BY")
            self._allow_aggregates = True
            found = False
            while True:
                if self.at_kw("ASC", "DESC"):
                    d = 1 if self.advance().value == "ASC" else -1
                    e = self.parse_bracketted_expression()
                    q.order_by.append(A.OrderCondition(e, d))
                elif self.at("VAR"):
                    q.order_by.append(A.OrderCondition(self.parse_var()))
                elif self.at("(") or self.at("IRIREF") or self.at("PNAME_LN") or self.at("PNAME_NS") or (
                        self.at("KW") and self.cur.value not in ("LIMIT", "OFFSET", "VALUES", "EXPRESSIONS", "TRUE", "FALSE")):
                    q.order_by.append(A.OrderCondition(self.parse_constraint()))
                else:
                    break
                found = True
            self._allow_aggregates = False
            if not found:
                raise self.error("expected an ORDER BY condition")
        if self.accept_kw("LIMIT"):
            q.limit = int(self.expect("INTEGER").value)
            if self.accept_kw("OFFSET"):
                q.offset = int(self.expect("INTEGER").value)
        elif self.accept_kw("OFFSET"):
            q.offset = int(self.expect("INTEGER").value)
            if self.accept_kw("LIMIT"):
                q.limit = int(self.expect("INTEGER").value)

    def parse_post_select(self, q: A.Query):
        if not self.accept_kw("EXPRESSIONS"):
            return
        self._allow_aggregates = True
        while self.at("("):
            self.advance()
            e = self.parse_expression()
            self.expect_kw("AS")
            v = self.parse_var()
            self.expect(")")
            q.post_select.append((v, e))
        self._allow_aggregates = False
        if not q.post_select:
            raise self.error("expected (expression AS ?var) after EXPRESSIONS")

    def parse_values_clause(self, q: A.Query):
        if self.accept_kw("VALUES"):
            q.values = self.parse_data_block()

    # -- group graph patterns -----------------------------------------------
    def parse_group_graph_pattern(self) -> A.Group:
        self.expect("{")
        if self.at_kw("SELECT"):
            sub = self.parse_sub_select()
            self.expect("}")
            return A.Group([A.SubSelect(sub)])
        g = A.Group()
        if self._at_triples_start():
            g.elements.append(self.parse_triples_block())
        while not self.at("}"):
            el = self.parse_graph_pattern_not_triples()
            g.elements.append(el)
            self.accept(".")
            if self._at_triples_start():
                g.elements.append(self.parse_triples_block())
        self.expect("}")
        return g

    def _at_triples_start(self) -> bool:
        t = self.cur
        if t.type in ("VAR", "IRIREF", "PNAME_LN", "PNAME_NS", "BLANK_NODE_LABEL", "ANON", "NIL", "STRING",
                      "STRING_START", "XIRI_START", "START_XEXPR", "INTEGER", "DECIMAL", "DOUBLE", "(", "[", "+", "-"):
            return True
        return t.type == "KW" and t.value in ("TRUE", "FALSE")

    def parse_graph_pattern_not_triples(self):
        if self.at("{"):
            first = self.parse_group_graph_pattern()
            if self.at_kw("UNION"):
                pats = [first]
                while self.accept_kw("UNION"):
                    pats.append(self.parse_group_graph_pattern())
                return A.Union_(pats)
            return first
        if self.accept_kw("OPTIONAL"):
            return A.Optional_(self.parse_group_graph_pattern())
        if self.accept_kw("MINUS"):
            return A.Minus(self.parse_group_graph_pattern())
        if self.accept_kw("GRAPH"):
            n = self.parse_var_or_xiri()
            return A.Graph(n, self.parse_group_graph_pattern())
        if self.accept_kw("SERVICE"):
            silent = bool(self.accept_kw("SILENT"))
            n = self.parse_var_or_xiri()
            return A.Service(n, self.parse_group_graph_pattern(), silent)
        if self.accept_kw("FILTER"):
            return A.Filter(self.parse_constraint())
        if self.at_kw("BIND"):
            return self.parse_bind()
        if self.accept_kw("VALUES"):
            return self.parse_data_block()
        raise self.error(f"expected a graph pattern but found {self._describe()}")

    def parse_data_block(self) -> A.Values:
        if self.at("VAR"):
            v = self.parse_var()
            self.expect("{")
            rows = []
            while not self.at("}"):
                rows.append([self.parse_data_block_value()])
            self.expect("}")
            return A.Values([v], rows)
        vars_: List[A.Var] = []
        if not self.accept("NIL"):
            self.expect("(")
            while self.at("VAR"):
                vars_.append(self.parse_var())
            self.expect(")")
        self.expect("{")
        rows = []
        while not self.at("}"):
            if self.accept("NIL"):
                rows.append([])
                continue
            self.expect("(")
            row = []
            while not self.at(")"):
                row.append(self.parse_data_block_value())
            self.expect(")")
            if len(row) != len(vars_):
                raise self.error(f"VALUES row has {len(row)} values for {len(vars_)} variables")
            rows.append(row)
        self.expect("}")
        return A.Values(vars_, rows)

    def parse_data_block_value(self):
        if self.accept_kw("UNDEF"):
            return None
        if self.at("IRIREF") or self.at("PNAME_LN") or self.at("PNAME_NS"):
            return A.IRI(self.parse_iri())
        if self.at("STRING"):
            return self.parse_rdf_literal()
        if self.at("INTEGER") or self.at("DECIMAL") or self.at("DOUBLE") or self.at("+") or self.at("-"):
            return self.parse_numeric_literal()
        if self.at_kw("TRUE", "FALSE"):
            return self.parse_boolean_literal()
        raise self.error(f"expected a VALUES term but found {self._describe()}")

    # -- triples -------------------------------------------------------------
    def parse_triples_block(self) -> A.TriplesBlock:
        block = A.TriplesBlock()
        while True:
            self.parse_triples_same_subject(block.triples, paths=True)
            if not self.accept("."):
                break
            if not self._at_triples_start():
                break
        return block

    def parse_construct_triples(self) -> List[A.Triple]:
        triples: List[A.Triple] = []
        while True:
            self.parse_triples_same_subject(triples, paths=False)
            if not self.accept("."):
                break
            if self.at("}") or self.at_kw("GENERATE", "GRAPH"):
                break
        return triples

    def parse_triples_same_subject(self, acc: List[A.Triple], paths: bool):
        if self.at("(") or self.at("["):
            s = self.parse_triples_node(acc, paths)
            if not (self.at(".") or self.at("}") or self.at(";") or self.at_kw("GENERATE") or self.at("EOF")) \
                    and not (self.at("KW") and self.cur.value not in ("A",)) or self.at_kw("A"):
                if self._at_verb_start():
                    self.parse_property_list_not_empty(s, acc, paths)
            return
        s = self.parse_var_or_term(acc, paths)
        self.parse_property_list_not_empty(s, acc, paths)

    def _at_verb_start(self) -> bool:
        t = self.cur
        return t.type in ("VAR", "IRIREF", "PNAME_LN", "PNAME_NS", "XIRI_START", "START_XEXPR", "^", "!", "(") \
            or (t.type == "KW" and t.value == "A")

    def parse_property_list_not_empty(self, s, acc: List[A.Triple], paths: bool):
        while True:
            p = self.parse_verb(paths)
            self.parse_object_list(s, p, acc, paths)
            if not self.accept(";"):
                return
            if not self._at_verb_start():
                return

    def parse_verb(self, paths: bool):
        if self.accept_kw("A"):
            return A.IRI(A.RDF_TYPE)
        if self.at("VAR"):
            return self.parse_var()
        if self.at("START_XEXPR"):
            return self.parse_xexpr()
        if paths:
            return self.parse_path()
        return self.parse_xiri()

    def parse_object_list(self, s, p, acc: List[A.Triple], paths: bool):
        while True:
            o = self.parse_object(acc, paths)
            acc.append(A.Triple(s, p, o))
            if not self.accept(","):
                return

    def parse_object(self, acc: List[A.Triple], paths: bool):
        if self.at_kw("LIST") and not paths:
            self.advance()
            self.expect("(")
            e = self.parse_expression()
            self.expect(")")
            return A.ListNode(e)
        if self.at("(") or self.at("["):
            return self.parse_triples_node(acc, paths)
        return self.parse_var_or_term(acc, paths)

    def parse_triples_node(self, acc: List[A.Triple], paths: bool):
        if self.accept("["):
            n = self.fresh_bnode()
            self.parse_property_list_not_empty(n, acc, paths)
            self.expect("]")
            return n
        self.expect("(")
        head = None
        last = None
        while not self.at(")"):
            cell = self.fresh_bnode()
            if head is None:
                head = cell
            if last is not None:
                acc.append(A.Triple(last, A.IRI(A.RDF_REST), cell))
            o = self.parse_object(acc, paths)
            acc.append(A.Triple(cell, A.IRI(A.RDF_FIRST), o))
            last = cell
        self.expect(")")
        if last is None:
            return A.IRI(A.RDF_NIL)
        acc.append(A.Triple(last, A.IRI(A.RDF_REST), A.IRI(A.RDF_NIL)))
        return head

    def parse_var_or_term(self, acc=None, paths: bool = False):
        t = self.cur
        if t.type == "VAR":
            return self.parse_var()
        if t.type == "NIL":
            self.advance()
            return A.IRI(A.RDF_NIL)
        if t.type == "BLANK_NODE_LABEL":
            self.advance()
            return A.BNode(t.value)
        if t.type == "ANON":
            self.advance()
            return self.fresh_bnode()
        if t.type in ("IRIREF", "PNAME_LN", "PNAME_NS", "XIRI_START"):
            return self.parse_xiri()
        if t.type in ("STRING", "STRING_START"):
            return self.parse_xrdf_literal()
        if t.type in ("INTEGER", "DECIMAL", "DOUBLE", "+", "-"):
            return self.parse_numeric_literal()
        if t.type == "KW" and t.value in ("TRUE", "FALSE"):
            return self.parse_boolean_literal()
        if t.type == "START_XEXPR":
            return self.parse_xexpr()
        raise self.error(f"expected a graph term but found {self._describe()}")

    def parse_var_or_xiri(self):
        if self.at("VAR"):
            return self.parse_var()
        if self.at("START_XEXPR"):
            return self.parse_xexpr()
        return self.parse_xiri()

    def parse_xexpr(self) -> A.XExpr:
        self.expect("START_XEXPR")
        e = self.parse_expression()
        self.expect("}")
        return A.XExpr(e)

    # -- property paths -------------------------------------------------------
    def parse_path(self):
        p = self.parse_path_sequence()
        while self.accept("|"):
            p2 = self.parse_path_sequence()
            p = A.Path("alt", (p, p2))
        return p

    def parse_path_sequence(self):
        p = self.parse_path_elt_or_inverse()
        while self.accept("/"):
            p2 = self.parse_path_elt_or_inverse()
            p = A.Path("seq", (p, p2))
        return p

    def parse_path_elt_or_inverse(self):
        if self.accept("^"):
            return A.Path("inverse", (self.parse_path_elt(),))
        return self.parse_path_elt()

    def parse_path_elt(self):
        p = self.parse_path_primary()
        if self.accept("?"):
            return A.Path("zeroOrOne", (p,))
        if self.accept("*"):
            return A.Path("zeroOrMore", (p,))
        if self.accept("+"):
            return A.Path("oneOrMore", (p,))
        return p

    def parse_path_primary(self):
        if self.accept_kw("A"):
            return A.IRI(A.RDF_TYPE)
        if self.accept("!"):
            return A.Path("negated", tuple(self.parse_path_negated_property_set()))
        if self.accept("("):
            p = self.parse_path()
            self.expect(")")
            return p
        return self.parse_xiri()

    def parse_path_negated_property_set(self):
        if self.accept("("):
            out = []
            if not self.at(")"):
                out.append(self.parse_path_one_in_property_set())
                while self.accept("|"):
                    out.append(self.parse_path_one_in_property_set())
            self.expect(")")
            return out
        return [self.parse_path_one_in_property_set()]

    def parse_path_one_in_property_set(self):
        if self.accept("^"):
            if self.accept_kw("A"):
                return A.Path("inverse", (A.IRI(A.RDF_TYPE),))
            return A.Path("inverse", (self.parse_xiri(),))
        if self.accept_kw("A"):
            return A.IRI(A.RDF_TYPE)
        return self.parse_xiri()

    # -- expressions ---------------------------------------------------------
    def parse_expression(self):
        e = self.parse_conditional_and()
        while self.accept("||"):
            e = A.Call("||", (e, self.parse_conditional_and()))
        return e

    def parse_conditional_and(self):
        e = self.parse_relational()
        while self.accept("&&"):
            e = A.Call("&&", (e, self.parse_relational()))
        return e

    def parse_relational(self):
        e = self.parse_additive()
        for op in ("=", "!=", "<", ">", "<=", ">="):
            if self.at(op):
                self.advance()
                return A.Call(op, (e, self.parse_additive()))
        if self.accept_kw("IN"):
            return A.Call("IN", (e,) + tuple(self.parse_expression_list()))
        if self.at_kw("NOT"):
            self.advance()
            self.expect_kw("IN")
            return A.Call("NOT IN", (e,) + tuple(self.parse_expression_list()))
        return e

    def parse_additive(self):
        e = self.parse_multiplicative()
        while True:
            if self.accept("+"):
                e = A.Call("+", (e, self.parse_multiplicative()))
            elif self.accept("-"):
                e = A.Call("-", (e, self.parse_multiplicative()))
            else:
                return e

    def parse_multiplicative(self):
        e = self.parse_unary()
        while True:
            if self.accept("*"):
                e = A.Call("*", (e, self.parse_unary()))
            elif self.accept("/"):
                e = A.Call("/", (e, self.parse_unary()))
            else:
                return e

    def parse_unary(self):
        if self.accept("!"):
            return A.Call("!", (self.parse_primary_expression(),))
        if self.accept("+"):
            return A.Call("unary+", (self.parse_primary_expression(),))
        if self.accept("-"):
            return A.Call("unary-", (self.parse_primary_expression(),))
        return self.parse_primary_expression()

    def parse_primary_expression(self):
        t = self.cur
        if t.type == "(":
            return self.parse_bracketted_expression()
        if t.type == "KW" and t.value == "TEMPLATE":
            q = self.parse_sub_template_query()
            self.expect(".")
            return A.TemplateExpr(q)
        if t.type == "KW" and t.value not in ("TRUE", "FALSE"):
            return self.parse_builtin_call()
        if t.type in ("IRIREF", "PNAME_LN", "PNAME_NS", "XIRI_START"):
            return self.parse_xiri_or_function()
        if t.type in ("STRING", "STRING_START"):
            return self.parse_xrdf_literal()
        if t.type in ("INTEGER", "DECIMAL", "DOUBLE"):
            return self.parse_numeric_literal()
        if t.type == "KW":
            return self.parse_boolean_literal()
        if t.type == "VAR":
            return self.parse_var()
        raise self.error(f"expected an expression but found {self._describe()}")

    def parse_bracketted_expression(self):
        self.expect("(")
        e = self.parse_expression()
        self.expect(")")
        return e

    def parse_constraint(self):
        if self.at("("):
            return self.parse_bracketted_expression()
        if self.at("KW"):
            return self.parse_builtin_call()
        return self.parse_function_call()

    def parse_expression_list(self) -> List[object]:
        if self.accept("NIL"):
            return []
        self.expect("(")
        out = [self.parse_expression()]
        while self.accept(","):
            out.append(self.parse_expression())
        self.expect(")")
        return out

    def parse_arg_list(self) -> List[object]:
        if self.accept("NIL"):
            return []
        self.expect("(")
        if self.accept_kw("DISTINCT"):
            raise self.error("DISTINCT is only allowed in aggregates")
        out = [self.parse_expression()]
        while self.accept(","):
            out.append(self.parse_expression())
        self.expect(")")
        return out

    def parse_function_call(self) -> A.Call:
        iri = self.parse_iri()
        args = self.parse_arg_list()
        return A.Call(iri, tuple(args))

    def parse_xiri_or_function(self):
        iri = self.parse_xiri()
        if self.at("NIL") or self.at("("):
            if isinstance(iri, A.XIRI):
                raise self.error("a function name cannot contain embedded expressions")
            args = self.parse_arg_list()
            return A.Call(iri.value, tuple(args))
        return iri

    def parse_builtin_call(self):
        tok = self.cur
        name = tok.value
        if name in AGGREGATES:
            return self.parse_aggregate()
        if name in BUILTIN_0:
            self.advance()
            self.expect("NIL")
            return A.Call(name, ())
        if name in BUILTIN_1:
            self.advance()
            self.expect("(")
            e = self.parse_expression()
            self.expect(")")
            return A.Call(name, (e,))
        if name in BUILTIN_2:
            self.advance()
            self.expect("(")
            e1 = self.parse_expression()
            self.expect(",")
            e2 = self.parse_expression()
            self.expect(")")
            return A.Call(name, (e1, e2))
        if name == "BOUND":
            self.advance()
            self.expect("(")
            v = self.parse_var()
            self.expect(")")
            return A.Call("BOUND", (v,))
        if name == "BNODE":
            self.advance()
            if self.accept("NIL"):
                return A.Call("BNODE", ())
            self.expect("(")
            e = self.parse_expression()
            self.expect(")")
            return A.Call("BNODE", (e,))
        if name in ("CONCAT", "COALESCE"):
            self.advance()
            return A.Call(name, tuple(self.parse_expression_list()))
        if name == "RAISE":
            # RAISE(message, value...): an expression that stops the execution (SPARQL-Generate 2.6)
            self.advance()
            args = self.parse_expression_list()
            if not args:
                raise self.error("RAISE needs a message: RAISE(message, value...)", tok)
            return A.Call("RAISE", tuple(args))
        if name in ("SUBSTR", "REGEX", "REPLACE", "IF"):
            self.advance()
            self.expect("(")
            args = [self.parse_expression()]
            while self.accept(","):
                args.append(self.parse_expression())
            self.expect(")")
            n = len(args)
            ok = {"SUBSTR": (2, 3), "REGEX": (2, 3), "REPLACE": (3, 4), "IF": (3,)}[name]
            if n not in ok:
                raise self.error(f"{name} takes {' or '.join(map(str, ok))} arguments, got {n}", tok)
            return A.Call(name, tuple(args))
        if name == "EXISTS":
            self.advance()
            return A.Exists(self.parse_group_graph_pattern())
        if name == "NOT":
            self.advance()
            self.expect_kw("EXISTS")
            return A.Exists(self.parse_group_graph_pattern(), negated=True)
        raise self.error(f"unexpected keyword {name} in expression", tok)

    def parse_aggregate(self) -> A.Aggregate:
        tok = self.advance()
        name = tok.value
        if not self._allow_aggregates:
            raise self.error("aggregate expression not legal at this point", tok)
        self.expect("(")
        distinct = bool(self.accept_kw("DISTINCT"))
        if name == "COUNT" and self.accept("*"):
            self.expect(")")
            return A.Aggregate("COUNT", (), distinct, star=True)
        e = self.parse_expression()
        sep = None
        if name == "GROUP_CONCAT" and self.accept(";"):
            self.expect_kw("SEPARATOR")
            self.expect("=")
            sep = self.expect("STRING").value
        self.expect(")")
        return A.Aggregate(name, (e,), distinct, sep)

    # -- terminals -------------------------------------------------------------
    def parse_iri(self) -> str:
        t = self.cur
        if t.type == "IRIREF":
            self.advance()
            return resolve_iri(t.value, self.base)
        if t.type == "PNAME_LN":
            self.advance()
            prefix, local = t.value
            if prefix not in self.prefixes:
                raise self.error(f"undefined prefix {prefix!r}", t)
            return self.prefixes[prefix] + self._unescape_local(local)
        if t.type == "PNAME_NS":
            self.advance()
            if t.value not in self.prefixes:
                raise self.error(f"undefined prefix {t.value!r}", t)
            return self.prefixes[t.value]
        raise self.error(f"expected an IRI but found {self._describe()}")

    @staticmethod
    def _unescape_local(local: str) -> str:
        if "\\" not in local:
            return local
        return local.replace("\\", "")

    def parse_xiri(self):
        if self.at("XIRI_START"):
            tok = self.advance()
            comps: List[object] = []
            if tok.value:
                comps.append(tok.value)
            while True:
                e = self.parse_expression()
                comps.append(e)
                if not self.at("}"):
                    raise self.error(f"expected '}}' closing the embedded expression but found {self._describe()}")
                t = self.rescan("xiri")
                self.advance()
                if t.value:
                    comps.append(t.value)
                if t.type == "XIRI_END":
                    break
            # a relative IRI template is resolved against the base when its first
            # component is a string
            if self.base and comps and isinstance(comps[0], str):
                comps[0] = resolve_iri(comps[0], self.base, template=True)
            elif self.base and comps and not isinstance(comps[0], str):
                comps.insert(0, resolve_iri("", self.base, template=True))
            return A.XIRI(tuple(comps))
        return A.IRI(self.parse_iri())

    def parse_rdf_literal(self) -> A.Literal:
        lex = self.expect("STRING").value
        if self.at("LANGTAG"):
            return A.Literal(lex, lang=self.advance().value)
        if self.accept("^^"):
            return A.Literal(lex, datatype=self.parse_iri())
        return A.Literal(lex)

    def parse_xrdf_literal(self):
        if self.at("STRING"):
            lex = self.advance().value
            comps: Optional[List[object]] = None
        else:
            tok = self.expect("STRING_START")
            lex0, mode = tok.value
            comps = [lex0] if lex0 else []
            while True:
                comps.append(self.parse_expression())
                if not self.at("}"):
                    raise self.error(f"expected '}}' closing the embedded expression but found {self._describe()}")
                t = self.rescan(mode)
                self.advance()
                if t.value:
                    comps.append(t.value)
                if t.type == "STRING_END":
                    break
            lex = None
        lang = None
        dt = None
        if self.at("LANGTAG"):
            lang = self.advance().value
        elif self.accept("^^"):
            dt = self.parse_xiri()
        if comps is None:
            if isinstance(dt, A.XIRI):
                return A.XLiteral((lex,), lang, dt)
            return A.Literal(lex, lang, dt.value if dt is not None else None)
        return A.XLiteral(tuple(comps), lang, dt)

    def parse_numeric_literal(self) -> A.Literal:
        sign = ""
        if self.at("+") or self.at("-"):
            sign = self.advance().value
        t = self.cur
        if t.type == "INTEGER":
            self.advance()
            return A.Literal(sign + t.value, datatype=A.XSD_INTEGER)
        if t.type == "DECIMAL":
            self.advance()
            return A.Literal(sign + t.value, datatype=A.XSD_DECIMAL)
        if t.type == "DOUBLE":
            self.advance()
            return A.Literal(sign + t.value, datatype=A.XSD_DOUBLE)
        raise self.error(f"expected a number but found {self._describe()}")

    def parse_boolean_literal(self) -> A.Literal:
        t = self.expect_kw("TRUE", "FALSE")
        return A.Literal("true" if t.value == "TRUE" else "false", datatype=A.XSD_BOOLEAN)


def parse(text: str, base: Optional[str] = None) -> A.Query:
    """Parse a SPARQL-Generate query string into an AST."""
    return Parser(text, base).parse_unit()
