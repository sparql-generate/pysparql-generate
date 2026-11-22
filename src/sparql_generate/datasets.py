"""Dataset clauses: ``FROM e``, ``FROM NAMED e``, ``FROM GENERATE S [NAMED g]`` (rule FROM).

A level with dataset clauses has its own dataset, a table with the columns of
``__quads`` (see :mod:`sparql_generate.bgp`) and a dataset key ``dsk``.  The
clauses are evaluated on the input table of the level, as the semantics says
(§``sec:sem:other``): ``FROM e`` loads the graph named by the IRI ``e`` (the named
graph of the dataset given to the engine when it has one, else the document
``e`` parsed as RDF), ``FROM NAMED e`` makes it a named graph, ``FROM GENERATE S``
adds to the default graph the output of ``S`` evaluated on the input solution,
and ``FROM GENERATE S NAMED g`` makes it the named graph ``g``.

When the clauses depend on variables of the input table (a query called on
several rows, divergence D9), the rows are grouped by the dataset they
determine: each group gets a key, stored in the reserved column ``\\x00dsk`` of
the input relation and in the ``dsk`` column of the quads of its dataset, and
every triple pattern binds ``\\x00dsk``, so that the joins of the WHERE clause
keep each row with the quads of its own dataset.  Otherwise there is one
dataset and no key.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, List, Optional

from . import ast as A
from .expressions import CompileError, compile_expr
from .terms import parse_nt_term, sql_string
from .termtypes import ANY_SQL_TYPE, BNODE_T, STR_T, CExpr, const_iri, const_literal, to_nt

DSK = "\x00dsk"  # reserved scope key of the dataset key


@dataclass
class DatasetRel:
    quads: str  # FROM item: (dsk,) g, s, s_nt, p, o, o_nt
    default: str  # FROM item: the quads of the default graph
    keyed: bool = False  # each quad carries the key of a group of input rows
    table: Optional[str] = None  # the temporary table, for a level dataset
    keys: str = "(SELECT '0' AS dsk)"  # FROM item: the keys of the datasets, empty ones included


RDF_FORMATS = {
    "text/turtle": "turtle", "application/x-turtle": "turtle", "application/n-triples": "nt",
    "text/plain": "turtle", "application/rdf+xml": "xml", "application/xml": "xml",
    "application/ld+json": "json-ld", "application/json": "json-ld", "text/n3": "n3",
    "application/trig": "trig", "application/n-quads": "nquads",
}


def prepare(compiler, ctx, rel, query: A.Query):
    """Build the dataset of a level from its dataset clauses; return the input relation,
    extended with the dataset key when the dataset depends on the input rows."""
    from .compiler import Rel

    engine = compiler.engine
    clauses = query.dataset_clauses
    # the dataset of the enclosing level: the one the sub-queries of FROM GENERATE query
    outer = getattr(ctx, "dataset", None)
    # the expressions that determine the dataset, evaluated on the input table
    det: List[CExpr] = []
    outer_key = None  # the key of the enclosing dataset, when it depends on the input rows
    if outer is not None and outer.keyed and any(dc.generate is not None for dc in clauses):
        outer_key = ctx.scope[DSK]
        det.append(outer_key)  # one enclosing dataset per group
    gen_vars: List[List[str]] = []  # per clause: the input variables a FROM GENERATE reads
    for dc in clauses:
        if dc.expr is not None:
            det.append(compile_expr(dc.expr, ctx))
        names: List[str] = []
        if dc.generate is not None:
            seen: set = set()
            _free_vars(dc.generate, seen)
            for v in sorted(seen):
                e = ctx.scope.get(v)
                if e is not None and not e.is_const:
                    names.append(v)
                    det.append(e)
        gen_vars.append(names)
    dyn = [e for e in det if not e.is_const]
    table = engine.fresh("__ds")
    engine.execute(f"CREATE TEMP TABLE {table}(dsk VARCHAR, g VARCHAR, s {ANY_SQL_TYPE}, s_nt VARCHAR, "
                   f"p VARCHAR, o {ANY_SQL_TYPE}, o_nt VARCHAR)")
    if not dyn:
        groups = [("0", {})]
        keyed = False
    else:
        nts: List[str] = []
        for e in dyn:
            n = to_nt(e, "o")
            if n not in nts:
                nts.append(n)
        key = "md5(concat_ws(chr(1), " + ", ".join(f"COALESCE({n}, '')" for n in nts) + "))"
        if rel.trivial:
            raise CompileError("dataset clause depends on unbound variables")
        rows = engine.execute(f"SELECT DISTINCT {key}, {', '.join(nts)} FROM {rel.sql}").fetchall()
        groups = [(r[0], {n: r[i + 1] for i, n in enumerate(nts)}) for r in rows]
        col = compiler.new_column("dsk")
        rel = Rel(f"(SELECT *, {key} AS {col} FROM {rel.sql})")
        ctx.scope[DSK] = CExpr(col, STR_T)
        keyed = True
    # a FROM GENERATE per group of input rows: compiled once on the table of the groups when
    # its sub-query evaluates the same on each group as on its own (rule FROM-SET)
    set_based = [keyed and dc.generate is not None and not dc.generate.is_named_call and
                 _set_based_ok(compiler, dc.generate) for dc in clauses]
    for i, (dc, names) in enumerate(zip(clauses, gen_vars)):
        if set_based[i]:
            names_of = {}
            if dc.expr is not None:
                for dsk, vals in groups:
                    t = _value(dc.expr, ctx, vals)
                    if not isinstance(t, A.IRI):
                        ctx.warnings.append(f"FROM: {dc.expr} is not an IRI; the clause is ignored")
                        continue
                    names_of[dsk] = t.value
            _generate_groups(compiler, ctx, table, rel, dc.generate, names, outer, outer_key,
                             names_of if dc.named else None)
    for dsk, vals in groups:
        for i, (dc, names) in enumerate(zip(clauses, gen_vars)):
            if set_based[i]:
                continue
            name_term = _value(dc.expr, ctx, vals) if dc.expr is not None else None
            if dc.expr is not None and not isinstance(name_term, A.IRI):
                ctx.warnings.append(f"FROM: {dc.expr} is not an IRI; the clause is ignored")
                continue
            iri = name_term.value if name_term is not None else None
            if dc.generate is None:
                _load_graph(compiler, ctx, table, dsk, iri if dc.named else None, iri)
            else:
                _generate_into(compiler, ctx, table, dsk, iri if dc.named else None, dc.generate, names, vals,
                               outer, outer_key)
    # a graph is a set of triples: the duplicates that FROM GENERATE produces (its sub-query
    # writes a bag) would multiply the solutions of every join on the dataset
    # (each clause inserts a set: the union of several clauses may repeat a triple)
    if len(clauses) > 1:
        dedup = engine.fresh("__ds")
        engine.execute(f"CREATE TEMP TABLE {dedup} AS SELECT dsk, g, any_value(s) AS s, s_nt, p, any_value(o) AS o, o_nt "
                       f"FROM {table} GROUP BY dsk, g, s_nt, p, o_nt")
        engine.execute(f"DROP TABLE {table}")
        table = dedup
    keys = "(SELECT '0' AS dsk)"
    if keyed:
        keys = engine.fresh("__dsk")
        engine.execute(f"CREATE TEMP TABLE {keys}(dsk VARCHAR)")
        engine.execute(f"INSERT INTO {keys} VALUES " + ", ".join(f"({sql_string(k)})" for k, _ in groups))
    return rel, DatasetRel(table, f"(SELECT * FROM {table} WHERE g IS NULL)", keyed, table, keys)


def _free_vars(node, acc: set) -> None:
    from .compiler import _var_names

    _var_names(node, acc)


def _value(expr, ctx, vals: Dict[str, Optional[str]]):
    """The term of an expression in a group of input rows (an AST term or None)."""
    e = compile_expr(expr, ctx)
    if e.is_const:
        if e.const is None:
            return None
        if e.type.kind == "iri":
            return A.IRI(str(e.const))
        return A.Literal(str(e.const))
    nt = vals.get(to_nt(e, "o"))
    return parse_nt_term(nt) if nt is not None else None


def _const(term) -> CExpr:
    if isinstance(term, A.IRI):
        return const_iri(term.value)
    if isinstance(term, A.BNode):
        return CExpr(sql_string(term.label), BNODE_T, term.label)
    return const_literal(term)


def _load_graph(compiler, ctx, table: str, dsk: str, g: Optional[str], iri: str) -> None:
    engine = compiler.engine
    if engine.dataset is not None:
        from .bgp import quads_table

        quads, _ = quads_table(engine)
        n = engine.execute(f"SELECT count(*) FROM {quads} WHERE g = {sql_string(iri)}").fetchall()[0][0]
        if n:
            engine.execute(f"INSERT INTO {table} SELECT {sql_string(dsk)}, {_sql_or_null(g)}, s, s_nt, p, o, o_nt "
                           f"FROM {quads} WHERE g = {sql_string(iri)}")
            return
    doc = engine.document(iri, None)
    if doc is None:
        ctx.warnings.append(f"FROM <{iri}>: document not found, the graph is empty")
        return
    from .ntriples import MEDIA_FORMATS, OXIGRAPH_FORMATS, oxigraph_quads

    import os as _os

    oxfmt = MEDIA_FORMATS.get(doc.guess_mediatype()) or OXIGRAPH_FORMATS.get(_os.path.splitext(doc.path or iri)[1].lower())
    try:
        # a relative name (no BASE) is no base IRI: the file's own IRI is
        base = iri if re.match(r"[A-Za-z][A-Za-z0-9+.-]*:", iri) else None
        native = oxigraph_quads(engine, path=doc.local_path(), data=None if doc.local_path() else doc.text(),
                                fmt=oxfmt, base=base) if oxfmt else None
    except ValueError as ex:
        ctx.warnings.append(f"FROM <{iri}>: not RDF ({ex}); the graph is empty")
        return
    if native is not None:  # pyoxigraph: no rdflib
        engine.execute(f"INSERT INTO {table} SELECT {sql_string(dsk)}, {_sql_or_null(g)}, any_value(s), s_nt, p, any_value(o), "
                       f"o_nt FROM (SELECT s, sg_any_nt(s, 'o') AS s_nt, p, o, sg_any_nt(o, 'o') AS o_nt FROM {native}) "
                       f"GROUP BY s_nt, p, o_nt")  # a file may repeat a triple: a graph is a set
        return
    import rdflib

    fmt = RDF_FORMATS.get(doc.guess_mediatype()) or rdflib.util.guess_format(doc.path or iri) or "turtle"
    graph = rdflib.Graph()
    try:
        if doc.local_path():
            graph.parse(doc.local_path(), format=fmt, publicID=iri)
        else:
            graph.parse(data=doc.text(), format=fmt, publicID=iri)
    except Exception as ex:  # noqa: BLE001 - any parse error makes the graph empty
        ctx.warnings.append(f"FROM <{iri}>: not RDF ({ex}); the graph is empty")
        return
    from .bgp import quads_arrow

    name = engine.register_arrow(quads_arrow(engine, ((s, p, o, None) for s, p, o in graph)))
    engine.execute(f"INSERT INTO {table} SELECT {sql_string(dsk)}, {_sql_or_null(g)}, s, sg_any_nt(s, 'o'), p, o, "
                   f"sg_any_nt(o, 'o') FROM {name}")


def _generate_into(compiler, ctx, table: str, dsk: str, g: Optional[str], sub: A.Query, names: List[str],
                   vals: Dict[str, Optional[str]], outer: Optional[DatasetRel] = None,
                   outer_key: Optional[CExpr] = None) -> None:
    """FROM GENERATE: evaluate the sub-query on the input solution (one group of rows), on
    the dataset of the enclosing level, and add its triples to the dataset."""
    from .compiler import TRIVIAL, Plan

    # the input solution, as constants: the constants of the scope and the group's values
    scope: Dict[str, CExpr] = {n: e for n, e in ctx.scope.items() if e.is_const and not n.startswith("\x00")}
    for n in names:
        nt = vals.get(to_nt(ctx.scope[n], "o"))
        scope[n] = _const(parse_nt_term(nt)) if nt is not None else CExpr("NULL", ctx.scope[n].type, None)
    if outer_key is not None:  # the group's enclosing dataset
        k = parse_nt_term(vals[to_nt(outer_key, "o")]).lexical
        scope[DSK] = CExpr(sql_string(k), STR_T, k)
    saved = compiler.plan
    compiler.plan = Plan("GENERATE", warnings=saved.warnings)
    try:
        if sub.is_named_call:
            from .parser import parse

            saved_scope = ctx.scope
            ctx.scope = scope
            try:
                name = compile_expr(sub.name, ctx)
                params = [compile_expr(p, ctx) for p in (sub.call_params or [])]
            finally:
                ctx.scope = saved_scope
            if not name.is_const or name.type.kind != "iri":
                raise CompileError("FROM GENERATE needs a constant query name")
            text = compiler.engine.named_query_text(str(name.const))
            if text is None:
                raise CompileError(f"named query <{name.const}> not found")
            called = parse(text, base=compiler.engine.base)
            sig = called.signature or []
            if len(sig) != len(params):
                raise CompileError(f"named query <{name.const}> expects {len(sig)} parameters, got {len(params)}")
            if not all(p.is_const for p in params):
                raise CompileError("FROM GENERATE parameters must be bound by the input solution")
            cscope = {v.name: p for v, p in zip(sig, params)}
            if DSK in scope:
                cscope[DSK] = scope[DSK]
            compiler.compile_level(called, TRIVIAL, cscope, {}, dataset=outer)
        else:
            sub.base = sub.base or ctx.query.base
            compiler.compile_level(sub, TRIVIAL, scope, {}, dataset=outer)
        triples = compiler.plan.triples_sql()
        _ignore_quads(ctx, compiler.plan)
    finally:
        compiler.plan = saved
    # the generated terms are canonical N-Triples already (the keys of the dataset): the
    # triples are made distinct first (a graph is a set), and only parsed into terms
    compiler.engine.execute(
        f"INSERT INTO {table} SELECT {sql_string(dsk)}, {_sql_or_null(g)}, sg_nt_any(s), s, "
        f"substr(p, 2, length(p) - 2), sg_nt_any(o), o FROM (SELECT DISTINCT s, p, o FROM ({triples}))")


TAG = "\x00fgk"  # reserved scope key: the group of a FROM GENERATE compiled on all groups


def _set_based_ok(compiler, sub: A.Query) -> bool:
    """A sub-query that, evaluated once on the table of all the groups, gives for each group
    what it gives evaluated on that group alone (the Java engine evaluates FROM GENERATE once
    per group): no aggregate, grouping, ordering, slice, DISTINCT, LIST, named call or
    iter:call-select, in it and in its sub-queries (they would range over all the groups)."""
    from .compiler import CALL_SELECT

    def ok(q: A.Query) -> bool:
        if q.kind != "GENERATE" or q.is_named_call:
            return False
        if q.group_by or q.having or q.order_by or q.limit is not None or q.offset is not None \
                or q.distinct or q.reduced or compiler.collect_aggregates(q):
            return False
        for c in q.binding_clauses:
            if isinstance(c, A.IteratorClause) and isinstance(c.expr.name, str) and c.expr.name == CALL_SELECT:
                return False
        for dc in q.dataset_clauses:
            if dc.generate is not None and not ok(dc.generate):
                return False
        for el in q.generate_clause or []:
            if isinstance(el, A.SubQuery):
                if not ok(el.query):
                    return False
            elif isinstance(el, A.GenerateGraph):
                if not all(ok(x.query) for x in el.elements if isinstance(x, A.SubQuery)):
                    return False
                if any(isinstance(t.object, A.ListNode) for x in el.elements
                       if isinstance(x, A.GenerateTriples) for t in x.triples):
                    return False
            elif any(isinstance(t.object, A.ListNode) for t in el.triples):
                return False
        return True

    return ok(sub)


def _generate_groups(compiler, ctx, table: str, rel, sub: A.Query, names: List[str],
                     outer: Optional[DatasetRel], outer_key: Optional[CExpr],
                     graph_names: Optional[Dict[str, str]]) -> None:
    """FROM GENERATE on a dataset per group of input rows, compiled once: the sub-query is
    evaluated on the table of the groups (one row per group: its key, the values of the
    variables it reads, the key of its enclosing dataset), and its triples carry the key of
    their group (the reserved variable ``TAG``)."""
    from .compiler import Plan, Rel

    engine = compiler.engine
    key = ctx.scope[DSK]
    cols, scope = [f"{key.sql} AS k"], {}
    for n, e in ctx.scope.items():
        if e.is_const and not n.startswith("\x00"):
            scope[n] = e
    for n in names:
        col = compiler.new_column(n)
        cols.append(f"any_value({ctx.scope[n].sql}) AS {col}")
        scope[n] = CExpr(col, ctx.scope[n].type)
    if outer_key is not None:
        col = compiler.new_column("odsk")
        cols.append(f"any_value({outer_key.sql}) AS {col}")
        scope[DSK] = CExpr(col, STR_T)
    gtable = engine.fresh("__dsg")
    engine.execute(f"CREATE TEMP TABLE {gtable} AS SELECT {', '.join(cols)} FROM {rel.sql} GROUP BY {key.sql}")
    tcol = compiler.new_column("fgk")
    scope[TAG] = CExpr(tcol, STR_T)
    saved, saved_tag = compiler.plan, compiler.triple_tag
    compiler.plan = Plan("GENERATE", warnings=saved.warnings)
    compiler.triple_tag = TAG
    try:
        sub.base = sub.base or ctx.query.base
        compiler.compile_level(sub, Rel(f"(SELECT *, k AS {tcol} FROM {gtable})"), scope, {}, dataset=outer)
        selects = list(compiler.plan.triple_selects)
        _ignore_quads(ctx, compiler.plan)
    finally:
        compiler.plan, compiler.triple_tag = saved, saved_tag
    if not selects:
        return
    body = "\nUNION ALL\n".join(f"SELECT * FROM ({q})" for q in selects)
    if graph_names is None:
        gsel, join = "NULL::VARCHAR", ""
    else:
        if not graph_names:
            return
        m = engine.fresh("__dsn")
        engine.execute(f"CREATE TEMP TABLE {m}(dsk VARCHAR, g VARCHAR)")
        engine.execute(f"INSERT INTO {m} VALUES " + ", ".join(f"({sql_string(k)}, {sql_string(g)})"
                                                              for k, g in graph_names.items()))
        gsel, join = f"{m}.g", f" JOIN {m} ON {m}.dsk = t.k"
    engine.execute(
        f"INSERT INTO {table} SELECT t.k, {gsel}, sg_nt_any(t.s), t.s, substr(t.p, 2, length(t.p) - 2), "
        f"sg_nt_any(t.o), t.o FROM (SELECT DISTINCT k, s, p, o FROM ({body})) AS t{join}")


def _ignore_quads(ctx, plan) -> None:
    """A FROM GENERATE gives one graph: the quads of the GRAPH blocks of its sub-query are
    ignored, with a warning (as the Java engine does)."""
    if plan.quad_selects:
        msg = "FROM GENERATE: the quads of GRAPH blocks are ignored (a FROM GENERATE gives one graph)"
        if msg not in ctx.warnings:
            ctx.warnings.append(msg)


def _sql_or_null(v: Optional[str]) -> str:
    return "NULL::VARCHAR" if v is None else sql_string(v)


def rdflib_dataset(engine, ds: DatasetRel):
    """An rdflib Dataset holding a level dataset, for the rdflib fallback."""
    import rdflib

    from .rdfwhere import _any_to_term

    if ds.keyed:
        raise CompileError("this WHERE clause (property path, sub-SELECT, SERVICE or EXISTS) cannot be "
                           "evaluated on a dataset that depends on the input rows")
    out = rdflib.Dataset(default_union=False)
    for g, s, p, o in engine.execute(f"SELECT g, s, p, o FROM {ds.quads}").fetchall():
        graph = out.default_graph if g is None else out.graph(rdflib.URIRef(g))
        graph.add((_any_to_term(s), rdflib.URIRef(p), _any_to_term(o)))
    return out
