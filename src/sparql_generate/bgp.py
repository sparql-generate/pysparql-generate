"""Native compilation of WHERE graph patterns over the RDF dataset (rule BGP).

The dataset is loaded once into a DuckDB table ``__quads(g, s, s_nt, p, o, o_nt)``:
``g`` is the graph name (NULL for the default graph), ``s`` and ``o`` are terms in
the dynamic representation (``any``), ``p`` is the predicate IRI, and ``s_nt``,
``o_nt`` are the N-Triples serialisations of ``s`` and ``o``.  N-Triples is an
injective encoding of RDF terms, so two terms are the same term exactly when
their N-Triples strings are equal: joins and constant matches compare strings.

A group graph pattern is translated as in SPARQL 1.1 §18.2.2.6, by folding its
elements from the input relation (the variables of the input row are visible in the pattern):

* a basic graph pattern is a self-join of ``__quads`` (one alias per triple
  pattern), constants and repeated variables being equality conditions;
* a nested group, the branches of ``UNION``, ``OPTIONAL`` and ``MINUS`` operands
  are compiled independently (the variables of the input table are not visible
  in them) and combined with the current relation by ``Join``, ``LeftJoin``
  (``LEFT JOIN``, with the filters of the optional group as join condition),
  ``Minus`` (``NOT EXISTS``) and ``Union`` (``UNION ALL``);
* ``BIND``, ``VALUES`` and ``FILTER`` are those of the compiler (filters last);
* ``GRAPH <g>`` and ``GRAPH ?g`` restrict the patterns they contain to the
  named graphs.

SPARQL compatibility makes an unbound value compatible with any value; a join
on a variable that may be unbound on one side is split into an equi-join of the
rows where it is bound, and a join of the remaining rows with the compatibility
condition, so that the common case stays a hash join.

Property paths, sub-``SELECT``, ``SERVICE`` and ``EXISTS`` are not compiled
natively: :func:`supported` returns False and the rdflib fallback
(:mod:`sparql_generate.rdfwhere`) evaluates the whole group.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple

from . import ast as A
from .expressions import CompileError, compile_expr
from .datasets import DSK
from .terms import sql_string
from .termtypes import ANY_T, BOOL_T, IRI_T, STR_T, CExpr, TType, coerce, to_nt, unify

# -- what is compiled natively -------------------------------------------------------


def supported(group: A.Group, in_optional: bool = False, in_graph_var: bool = False) -> bool:
    """True when every element of the group (recursively) is compiled natively: triple
    patterns whose predicate is a variable, an IRI or a path of IRIs built with ``/``,
    ``^`` and ``|``; OPTIONAL, UNION, MINUS, GRAPH, nested groups, sub-SELECT; BIND, VALUES
    and FILTER, with EXISTS (not in the filters of an OPTIONAL, which are a join condition)."""
    for el in group.elements:
        if isinstance(el, A.TriplesBlock):
            for t in el.triples:
                if not (isinstance(t.predicate, (A.Var, A.IRI)) or _simple_path(t.predicate)
                        or _closure_path(t.predicate)):
                    return False  # a path with !, nested * + ?, or an IRI with embedded expressions
                for n in (t.subject, t.object):
                    if not isinstance(n, (A.Var, A.IRI, A.Literal, A.BNode)):
                        return False
            if _path_bnodes_shared(el):
                return False
        elif isinstance(el, A.Optional_):
            if not supported(el.pattern, in_optional=True, in_graph_var=in_graph_var):
                return False
        elif isinstance(el, A.Minus):
            if not supported(el.pattern):
                return False
        elif isinstance(el, A.Union_):
            if not all(supported(p) for p in el.patterns):
                return False
        elif isinstance(el, A.Group):
            if not supported(el):
                return False
        elif isinstance(el, A.Graph):
            if not isinstance(el.node, (A.Var, A.IRI)) or \
                    not supported(el.pattern, in_graph_var=in_graph_var or isinstance(el.node, A.Var)):
                return False
        elif isinstance(el, (A.Filter, A.Bind)):
            for ex in _exists_nodes(el.expr):
                if in_optional and isinstance(el, A.Filter) or not supported(ex.pattern):
                    return False
        elif isinstance(el, A.SubSelect):
            q = el.query
            if q.where is not None and not supported(q.where):
                return False
            if q.binding_clauses or q.dataset_clauses:
                return False
        elif not isinstance(el, A.Values):
            return False  # SERVICE
    return True


def _exists_nodes(expr) -> List[A.Exists]:
    if isinstance(expr, A.Exists):
        return [expr]
    if isinstance(expr, A.Call):
        return [x for a in expr.args for x in _exists_nodes(a)]
    return []


def _has_exists(expr) -> bool:
    return bool(_exists_nodes(expr))


def _simple_path(p) -> bool:
    """A path of IRIs built with sequence, inverse and alternative (SPARQL 1.1 §18.2.2.4
    translates it to triple patterns, joins and unions)."""
    if isinstance(p, A.IRI):
        return True
    if isinstance(p, A.Path) and p.op == "negated":  # !(p1|^p2...): IRIs and inverse IRIs
        return all(isinstance(m, A.IRI) or isinstance(m, A.Path) and m.op == "inverse" and isinstance(m.args[0], A.IRI)
                   for m in p.args)
    return isinstance(p, A.Path) and p.op in ("seq", "inverse", "alt") and all(_simple_path(a) for a in p.args)


def _closure_path(p) -> bool:
    """``P*``, ``P+``, ``P?`` of a simple path P, as a whole or in a sequence, inverse or
    alternative (arbitrary-length paths, SPARQL 1.1 §18.4: sets of pairs)."""
    if isinstance(p, A.IRI):
        return True
    if not isinstance(p, A.Path):
        return False
    if p.op in ("zeroOrMore", "oneOrMore", "zeroOrOne"):
        return _simple_path(p.args[0])
    if p.op == "negated":
        return _simple_path(p)
    return p.op in ("seq", "inverse", "alt") and all(_closure_path(a) for a in p.args)


CLOSURE_OPS = ("zeroOrMore", "oneOrMore", "zeroOrOne")


def _has_alt(p) -> bool:
    return isinstance(p, A.Path) and (p.op in ("alt", "negated") + CLOSURE_OPS or any(_has_alt(a) for a in p.args))


def _path_bnodes_shared(block: A.TriplesBlock) -> bool:
    """A blank node of a triple block whose path has an alternative, used by another triple:
    the alternative becomes a UNION outside the block, where the blank node (a variable of
    the block only) would not join."""
    alt = [t for t in block.triples if _has_alt(t.predicate)]
    if not alt:
        return False
    labels = {n.label for t in alt for n in (t.subject, t.object) if isinstance(n, A.BNode)}
    others = {n.label for t in block.triples if t not in alt for n in (t.subject, t.object) if isinstance(n, A.BNode)}
    return bool(labels & others) or len(alt) > 1 and bool(labels)


PATH_VAR = "\x01path"  # fresh variables of path sequences: joined, never projected


def expand_paths(block: A.TriplesBlock, fresh) -> List[object]:
    """The elements of a group equivalent to a triple block with simple paths (SPARQL 1.1
    §18.2.2.4): ``X P/Q Y`` is ``X P ?v . ?v Q Y`` (``?v`` fresh), ``X ^P Y`` is ``Y P X``,
    ``X P|Q Y`` is ``{ X P Y } UNION { X Q Y }``."""
    plain: List[A.Triple] = []
    unions: List[object] = []

    def expand(s, p, o, out_triples, out_unions):
        if isinstance(p, A.Path) and p.op == "inverse":
            expand(o, p.args[0], s, out_triples, out_unions)
        elif isinstance(p, A.Path) and p.op == "seq":
            v = A.Var(fresh())
            expand(s, p.args[0], v, out_triples, out_unions)
            expand(v, p.args[1], o, out_triples, out_unions)
        elif isinstance(p, A.Path) and p.op == "alt":
            branches = []
            for a in p.args:
                ts, us = [], []
                expand(s, a, o, ts, us)
                branches.append(A.Group(([A.TriplesBlock(ts)] if ts else []) + us))
            out_unions.append(A.Union_(branches))
        elif isinstance(p, A.Path) and p.op in CLOSURE_OPS + ("negated",):
            out_unions.append(A.Triple(s, p, o))  # compiled by PatternCompiler.closure
        else:
            out_triples.append(A.Triple(s, p, o))

    for t in block.triples:
        expand(t.subject, t.predicate, t.object, plain, unions)
    return ([A.TriplesBlock(plain)] if plain else []) + unions


# -- the dataset table ---------------------------------------------------------------


def quads_table(engine) -> Tuple[str, str]:
    """Load the dataset once; return (quads table, default-graph relation)."""
    cached = getattr(engine, "_quads", None)
    if cached is not None:
        return cached
    ds = engine.dataset
    if ds is None:
        raise CompileError("this WHERE clause needs an RDF dataset (triple patterns); none was given "
                           "(use dataset=..., or the 'graph'/'namedgraphs' keys of sparql-generate-conf.json)")
    if isinstance(ds, (str, os.PathLike)):
        # a file: N-Triples and N-Quads natively, other syntaxes through rdflib
        from .ntriples import is_ntriples_path, native_quads

        native = native_quads(engine, str(ds)) if is_ntriples_path(str(ds)) else None
        if native is None:  # Turtle, TriG, RDF/XML, N3, or escapes the fast path does not decode
            from .ntriples import oxigraph_quads

            try:
                native = oxigraph_quads(engine, path=str(ds))
            except ValueError as ex:
                raise CompileError(f"the dataset {ds} is not valid RDF: {ex}")
        if native is not None:  # a file may repeat a quad: a graph is a set
            engine.execute(f"CREATE TEMP TABLE __quads AS SELECT g, any_value(s) AS s, s_nt, p, any_value(o) AS o, o_nt "
                           f"FROM (SELECT g, s, sg_any_nt(s, 'o') AS s_nt, p, o, sg_any_nt(o, 'o') AS o_nt FROM {native}) "
                           f"GROUP BY g, s_nt, p, o_nt")
            engine._quads = ("__quads", "(SELECT * FROM __quads WHERE g IS NULL)")
            return engine._quads
        ds = engine.dataset = parse_rdf_file(str(ds))
    import rdflib

    default_ids = {str(rdflib.graph.DATASET_DEFAULT_GRAPH_ID)}
    union_default = False
    if isinstance(ds, rdflib.ConjunctiveGraph):  # Dataset is a ConjunctiveGraph
        union_default = bool(getattr(ds, "default_union", False))
        default_ids.add(str(ds.default_context.identifier))
        quads = ds.quads((None, None, None, None))
    else:
        quads = ((s, p, o, None) for s, p, o in ds.triples((None, None, None)))
    tbl = quads_arrow(engine, quads, default_ids)
    name = engine.register_arrow(tbl)
    engine.execute(f"CREATE TEMP TABLE __quads AS SELECT g, s, sg_any_nt(s, 'o') AS s_nt, p, o, "
                   f"sg_any_nt(o, 'o') AS o_nt FROM {name}")
    if union_default:
        default = "(SELECT DISTINCT NULL::VARCHAR AS g, s, s_nt, p, o, o_nt FROM __quads)"
    else:
        default = "(SELECT * FROM __quads WHERE g IS NULL)"
    engine._quads = ("__quads", default)
    return engine._quads


def parse_rdf_file(path: str):
    """An rdflib Dataset of an RDF file (the syntax guessed from its extension, Turtle
    by default); N-Quads and TriG files keep their named graphs."""
    import rdflib

    fmt = rdflib.util.guess_format(path) or "turtle"
    ds = rdflib.Dataset(default_union=False)
    if fmt in ("nquads", "trig"):
        ds.parse(path, format=fmt)
    else:
        ds.default_graph.parse(path, format=fmt)
    return ds


def rdflib_of_dataset(engine):
    """The engine's dataset as an rdflib Dataset (for the rdflib fallback): itself, or, for a
    file loaded natively, a Dataset built from the quads table once."""
    import rdflib

    if engine.dataset is None or isinstance(engine.dataset, rdflib.Graph):
        return engine.dataset
    quads, default = quads_table(engine)  # a file: parsed by rdflib unless it is N-Triples/N-Quads
    if isinstance(engine.dataset, rdflib.Graph):
        return engine.dataset
    cached = getattr(engine, "_rdflib_dataset", None)
    if cached is None:
        from .datasets import DatasetRel, rdflib_dataset

        cached = engine._rdflib_dataset = rdflib_dataset(engine, DatasetRel(quads, default))
    return cached


def quads_arrow(engine, quads, default_ids=frozenset()):
    """An Arrow table ``(g, s, p, o)`` of rdflib quads, terms in the dynamic representation;
    columns are built directly (no dict per term) and terms are converted once."""
    import pyarrow as pa
    import rdflib

    URIRef, BNode, Literal = rdflib.URIRef, rdflib.BNode, rdflib.Literal
    cache: Dict[object, tuple] = {}

    def conv(t):
        r = cache.get(t)
        if r is None:
            if isinstance(t, URIRef):
                r = (0, str(t), None)
            elif isinstance(t, BNode):
                r = (1, str(t), None)
            elif isinstance(t, Literal):
                if t.language:
                    r = (2, str(t), "@" + t.language)
                else:
                    dt = str(t.datatype) if t.datatype else None
                    r = (2, str(t), None if dt == A.XSD_STRING else dt)
            else:
                r = (2, str(t), None)
            cache[t] = r
        return r

    g_col, p_col = [], []
    sk, sv, sd, ok, ov, od = [], [], [], [], [], []
    for s_, p_, o_, c in quads:
        if c is not None and not isinstance(c, (str, rdflib.term.Node)):
            c = c.identifier
        g_col.append(None if c is None or str(c) in default_ids else str(c))
        p_col.append(str(p_))
        a = conv(s_)
        sk.append(a[0]); sv.append(a[1]); sd.append(a[2])  # noqa: E702
        b = conv(o_)
        ok.append(b[0]); ov.append(b[1]); od.append(b[2])  # noqa: E702

    def struct(k, v, d):
        return pa.StructArray.from_arrays([pa.array(k, pa.int8()), pa.array(v, pa.string()), pa.array(d, pa.string())],
                                          names=["k", "v", "d"])

    return pa.table({"g": pa.array(g_col, pa.string()), "s": struct(sk, sv, sd), "p": pa.array(p_col, pa.string()),
                     "o": struct(ok, ov, od)})


def _term_struct(term) -> dict:
    import rdflib

    if isinstance(term, rdflib.URIRef):
        return {"k": 0, "v": str(term), "d": None}
    if isinstance(term, rdflib.BNode):
        return {"k": 1, "v": str(term), "d": None}
    if isinstance(term, rdflib.Literal):
        if term.language:
            return {"k": 2, "v": str(term), "d": "@" + term.language}
        dt = str(term.datatype) if term.datatype else None
        return {"k": 2, "v": str(term), "d": None if dt == A.XSD_STRING else dt}
    return {"k": 2, "v": str(term), "d": None}


# -- pieces: a relation with its scope ------------------------------------------------


@dataclass
class Piece:
    """A relation (a FROM item, or None for the unit table) and the variables it binds."""

    sql: Optional[str]
    scope: Dict[str, CExpr]
    nonnull: Set[str] = field(default_factory=set)  # columns known to be bound in every row


class PatternCompiler:
    """Compiles one WHERE group of a level (``ctx``) over the dataset."""

    def __init__(self, compiler, ctx):
        self.compiler = compiler
        self.ctx = ctx
        self.engine = compiler.engine
        # column -> its N-Triples key column; valid within this WHERE clause only (the key
        # columns are not carried by materialisation)
        self.keys: Dict[str, str] = {}

    # -- helpers -------------------------------------------------------------------
    @property
    def quads(self) -> str:
        ds = getattr(self.ctx, "dataset", None)
        return ds.quads if ds is not None else quads_table(self.engine)[0]

    @property
    def default(self) -> str:
        """The default graph as a relation."""
        ds = getattr(self.ctx, "dataset", None)
        return ds.default if ds is not None else quads_table(self.engine)[1]

    @property
    def keyed(self) -> bool:
        """The quads carry the dataset key of a group of input rows (rule FROM, D9)."""
        ds = getattr(self.ctx, "dataset", None)
        return ds is not None and ds.keyed

    def fresh(self, name: str) -> str:
        return self.compiler.new_column(name)

    def alias(self) -> str:
        return self.engine.fresh("__q")

    def one(self) -> str:
        """The unit relation, with a column name that no other relation uses."""
        return f'(SELECT 1 AS "{self.engine.fresh("__one")}")'

    def key(self, e: CExpr) -> str:
        """N-Triples string of a term (the join key)."""
        k = self.keys.get(e.sql)
        if k is not None:
            return k
        return to_nt(e, "o")

    @staticmethod
    def is_var_name(name: str) -> bool:
        """Variables joined by the algebra: those of the query, and the dataset key."""
        return not name.startswith("\x00") or name == DSK

    # -- entry point ------------------------------------------------------------------
    def compile_where(self, rel, group: A.Group):
        """Fold the top-level group from the input relation; return the new Rel and set
        ``ctx.scope``."""
        from .compiler import Rel

        start = Piece(None if rel.trivial else rel.sql, dict(self.ctx.scope))
        if DSK in self.ctx.scope:
            start.nonnull.add(self.ctx.scope[DSK].sql)  # every input row has its dataset key
        out = self.group(group, start, graph=None)
        self.ctx.scope = {n: e for n, e in out.scope.items() if not n.startswith(PATH_VAR)}
        if out.sql is None:
            return rel if rel.trivial else Rel(rel.sql)
        return Rel(out.sql)

    # -- groups --------------------------------------------------------------------------
    def group(self, group: A.Group, g: Piece, graph, keep_filters: bool = True) -> Piece:
        filters = []
        for el in group.elements:
            if isinstance(el, A.TriplesBlock) and any(isinstance(t.predicate, A.Path) for t in el.triples):
                for sub in expand_paths(el, self.path_var):
                    if isinstance(sub, A.TriplesBlock):
                        g = self.join(g, self.bgp(sub.triples, graph))
                    elif isinstance(sub, A.Triple):
                        g = self.join(g, self.closure(sub, graph))
                    else:
                        g = self.join(g, self.union([self.group(b, self.unit(), graph) for b in sub.patterns]))
            elif isinstance(el, A.TriplesBlock):
                g = self.join(g, self.bgp(el.triples, graph))
            elif isinstance(el, A.SubSelect):
                g = self.join(g, self.subselect(el.query))
            elif isinstance(el, A.Triple):  # an arbitrary-length path (from expand_paths)
                g = self.join(g, self.closure(el, graph))
            elif isinstance(el, A.Group):
                g = self.join(g, self.group(el, self.unit(), graph))
            elif isinstance(el, A.Union_):
                g = self.join(g, self.union([self.group(p, self.unit(), graph) for p in el.patterns]))
            elif isinstance(el, A.Optional_):
                inner_filters = [f.expr for f in el.pattern.elements if isinstance(f, A.Filter)]
                inner = A.Group([f for f in el.pattern.elements if not isinstance(f, A.Filter)])
                g = self.join(g, self.group(inner, self.unit(), graph), left=True, condition=inner_filters)
            elif isinstance(el, A.Minus):
                g = self.minus(g, self.group(el.pattern, self.unit(), graph))
            elif isinstance(el, A.Graph):
                g = self.join(g, self.graph_pattern(el))
            elif isinstance(el, A.Bind):
                g = self.bind(g, el, graph)
            elif isinstance(el, A.Values):
                g = self.values(g, el)
            elif isinstance(el, A.Filter):
                filters.append(el.expr)
            else:  # pragma: no cover - excluded by supported()
                raise CompileError(f"unsupported pattern in WHERE: {type(el).__name__}")
        if filters and keep_filters:
            g = self.filter(g, filters, graph)
        return g

    def path_var(self) -> str:
        return PATH_VAR + self.engine.fresh("")

    def closure(self, t: A.Triple, graph) -> Piece:
        """``X P* Y``, ``X P+ Y``, ``X P? Y`` (SPARQL 1.1 §18.4, arbitrary-length paths): the
        set of the pairs joined by a path of P of length >= 0, >= 1, or 0 or 1, as a
        ``WITH RECURSIVE`` closure of the pairs of P, seeded by a constant end when there is
        one; the zero-length pairs are (t, t) for a constant end t, else the nodes of the
        active graph.  ``UNION`` keeps pairs distinct, which also ends the recursion.  On a
        dataset per group of input rows (rule FROM, D9), the pairs carry the dataset key
        ``kk``: a path only follows the edges of its own dataset."""
        if t.predicate.op == "negated":
            return self.negated(t, graph)
        keyed = self.keyed
        op, inner = t.predicate.op, t.predicate.args[0]
        gvar = graph[1] if graph is not None and graph[0] == "var" else None
        x, y = self.path_var(), self.path_var()
        p = self.group(A.Group(expand_paths(A.TriplesBlock([A.Triple(A.Var(x), inner, A.Var(y))]), self.path_var)),
                       self.unit(), graph)
        ex_, ey = p.scope[x], p.scope[y]
        e = self.engine.fresh("__pe")
        c = self.engine.fresh("__pc")
        gsel = f"{p.scope[gvar].sql}" if gvar is not None else "NULL::VARCHAR"
        ksel = f"{p.scope[DSK].sql}" if keyed else "NULL::VARCHAR"
        edges = (f"SELECT {coerce(ex_, ANY_T)} AS a, {self.key(ex_)} AS ak, {coerce(ey, ANY_T)} AS b, "
                 f"{self.key(ey)} AS bk, {gsel} AS gg, {ksel} AS kk FROM {p.sql}")
        ends = {}
        for pos, n in (("s", t.subject), ("o", t.object)):
            if isinstance(n, (A.IRI, A.Literal)):
                ends[pos] = sql_string(constant_nt(n))
        if graph is None:
            active = f"(SELECT NULL::VARCHAR AS gg, * FROM {self.default})"
        elif gvar is None:
            active = f"(SELECT g AS gg, * FROM {self.quads} WHERE g = {sql_string(graph[1])})"
        else:  # GRAPH ?g: one closure per named graph, the graph name carried along
            active = f"(SELECT g AS gg, * FROM {self.quads} WHERE g IS NOT NULL)"
        kact = "dsk" if keyed else "NULL::VARCHAR"
        same_g = f" AND {c}.gg IS NOT DISTINCT FROM {e}.gg AND {c}.kk IS NOT DISTINCT FROM {e}.kk"
        if "s" in ends:
            base, step = (f"SELECT * FROM {e} WHERE ak = {ends['s']}",
                          f"SELECT {c}.a, {c}.ak, {e}.b, {e}.bk, {c}.gg, {c}.kk FROM {c} JOIN {e} ON {c}.bk = {e}.ak{same_g}")
        elif "o" in ends:
            base, step = (f"SELECT * FROM {e} WHERE bk = {ends['o']}",
                          f"SELECT {e}.a, {e}.ak, {c}.b, {c}.bk, {c}.gg, {c}.kk FROM {e} JOIN {c} ON {e}.bk = {c}.ak{same_g}")
        else:
            base, step = (f"SELECT * FROM {e}",
                          f"SELECT {c}.a, {c}.ak, {e}.b, {e}.bk, {c}.gg, {c}.kk FROM {c} JOIN {e} ON {c}.bk = {e}.ak{same_g}")
        parts = []
        if op == "zeroOrOne":
            parts.append(f"SELECT * FROM {e}")
        else:
            parts.append(f"SELECT * FROM {c}")
        if op != "oneOrMore":
            end = ends.get("s", ends.get("o"))
            if end is not None:
                node = t.subject if "s" in ends else t.object
                v = _const_any(node)
                if gvar is None and not keyed:
                    parts.append(f"SELECT {v} AS a, {end} AS ak, {v} AS b, {end} AS bk, NULL::VARCHAR AS gg, "
                                 f"NULL::VARCHAR AS kk")
                elif gvar is None:  # in every dataset, empty ones included
                    parts.append(f"SELECT {v} AS a, {end} AS ak, {v} AS b, {end} AS bk, NULL::VARCHAR AS gg, "
                                 f"dsk AS kk FROM {self.ctx.dataset.keys}")
                else:  # in every named graph
                    parts.append(f"SELECT DISTINCT {v} AS a, {end} AS ak, {v} AS b, {end} AS bk, gg, {kact} AS kk "
                                 f"FROM {active}")
            else:
                parts.append(f"SELECT s AS a, s_nt AS ak, s AS b, s_nt AS bk, gg, {kact} AS kk FROM {active} "
                             f"UNION SELECT o, o_nt, o, o_nt, gg, {kact} FROM {active}")
        conds = [f"ak = {ends['s']}"] if "s" in ends else []
        conds += [f"bk = {ends['o']}"] if "o" in ends else []
        if isinstance(t.subject, A.Var) and isinstance(t.object, A.Var) and t.subject.name == t.object.name:
            conds.append("ak = bk")
        where = f" WHERE {' AND '.join(conds)}" if conds else ""
        sel, scope, nonnull = [], {}, set()
        for pos, n, col_v, col_k in (("s", t.subject, "a", "ak"), ("o", t.object, "b", "bk")):
            if isinstance(n, A.Var) and n.name not in scope:
                col = self.fresh(n.name)
                kcol = f'"{col[1:-1]}_k"'
                sel += [f"{col_v} AS {col}", f"{col_k} AS {kcol}"]
                self.keys[col] = kcol
                scope[n.name] = CExpr(col, ANY_T)
                nonnull.add(col)
        if gvar is not None:
            col = self.fresh(gvar)
            sel.append(f"gg AS {col}")
            scope[gvar] = CExpr(col, IRI_T)
            nonnull.add(col)
        if keyed:
            col = self.fresh("dsk")
            sel.append(f"kk AS {col}")
            scope[DSK] = CExpr(col, STR_T)
            nonnull.add(col)
        if not sel:
            sel.append(f'1 AS "{self.engine.fresh("__one")}"')
        sql = (f"(WITH RECURSIVE {e} AS MATERIALIZED ({edges}), {c}(a, ak, b, bk, gg, kk) AS ({base} UNION {step}) "
               f"SELECT {', '.join(sel)} FROM ({' UNION '.join(parts)}){where})")
        return Piece(sql, scope, nonnull)

    # -- sub-SELECT, EXISTS ------------------------------------------------------------------
    def negated(self, t: A.Triple, graph) -> Piece:
        """``X !(p1|...|^q1|...) Y`` (SPARQL 1.1 §18.2.2.4): the triples ``X p Y`` whose
        predicate is none of the forward IRIs, and the triples ``Y q X`` whose predicate is
        none of the inverse ones; the union of both when the set has both kinds."""
        fwd = [m.value for m in t.predicate.args if isinstance(m, A.IRI)]
        inv = [m.args[0].value for m in t.predicate.args if isinstance(m, A.Path)]

        def part(s, o, excluded):
            pv = self.path_var()
            piece = self.bgp([A.Triple(s, A.Var(pv), o)], graph)
            pcol = piece.scope[pv].sql
            cond = f"{pcol} NOT IN ({', '.join(sql_string(x) for x in excluded)})" if excluded else "TRUE"
            return Piece(f"(SELECT * FROM {piece.sql} WHERE {cond})",
                         {n: e for n, e in piece.scope.items() if n != pv}, piece.nonnull)

        pieces = []
        if fwd or not inv:
            pieces.append(part(t.subject, t.object, fwd))
        if inv:
            pieces.append(part(t.object, t.subject, inv))
        return pieces[0] if len(pieces) == 1 else self.union(pieces)

    def subselect(self, q: A.Query) -> Piece:
        """A sub-SELECT: a level of its own on the unit table (it does not see the variables
        of the enclosing group), materialised, whose projection is joined like a group."""
        from .compiler import TRIVIAL

        q.base = q.base or self.ctx.query.base
        cctx, table = self.compiler.solutions(q, TRIVIAL, {}, {}, dataset=getattr(self.ctx, "dataset", None),
                                              partitioned=False)
        if q.select_star or not q.select_vars:
            pairs = [(n, cctx.scope[n]) for n in cctx.scope if not n.startswith("\x00") and not n.startswith(PATH_VAR)]
        else:
            pairs = [(v.name, compile_expr(e, cctx) if e is not None else cctx.lookup(v)) for v, e in q.select_vars]
        sel, scope = [], {}
        for n, e in pairs:
            if e.is_null:
                continue
            col = self.fresh(n)
            sel.append(f"{e.sql} AS {col}")
            scope[n] = CExpr(col, e.type)
        if not sel:
            sel.append(f'1 AS "{self.engine.fresh("__one")}"')
        return Piece(f"(SELECT {', '.join(sel)} FROM {table})", scope)

    def exists(self, node: A.Exists, g: Piece, graph) -> CExpr:
        """``EXISTS { P }`` on the current row of ``g``: P is evaluated on the one-row table
        of that row (the variables of the row are visible in the
        top-level group of P, as substitution makes them in SPARQL), a correlated subquery
        (the columns of ``g`` have unique names: unqualified, they refer to the outer row)."""
        sel, scope, nonnull = [], {}, set()
        for n, e in g.scope.items():
            if e.is_const or not (e.sql.startswith('"') and e.sql.endswith('"')):
                scope[n] = e
                continue
            col = self.fresh(n.lstrip("\x00"))
            sel.append(f"{e.sql} AS {col}")
            scope[n] = CExpr(col, e.type)
            if e.sql in self.keys:
                kcol = f'"{col[1:-1]}_k"'
                sel.append(f"{self.keys[e.sql]} AS {kcol}")
                self.keys[col] = kcol
            if e.sql in g.nonnull:
                nonnull.add(col)
        start = Piece(f"(SELECT {', '.join(sel)})" if sel else None, scope, nonnull)
        c = self.ctx  # the operators of P must not disturb the state of the enclosing one
        state = (c.uid_col, c.uid_pending, c.hoist_ok, c.lets, c.scope, c.operator_rel)
        try:
            p = self.group(node.pattern, start, graph)
        finally:
            c.uid_col, c.uid_pending, c.hoist_ok, c.lets, c.scope, c.operator_rel = state
        src = p.sql if p.sql is not None else self.one()
        return CExpr(f"({'NOT ' if node.negated else ''}EXISTS (SELECT 1 FROM {src}))", BOOL_T)

    def unit(self) -> Piece:
        return Piece(None, {})

    def graph_pattern(self, el: A.Graph) -> Piece:
        if isinstance(el.node, A.IRI):
            return self.group(el.pattern, self.unit(), ("iri", el.node.value))
        # GRAPH ?g { P }: P evaluated on every named graph, ?g bound to its name
        col = self.fresh(el.node.name)
        if self.keyed:
            kcol = self.fresh("dsk")
            names = Piece(f"(SELECT DISTINCT g AS {col}, dsk AS {kcol} FROM {self.quads} WHERE g IS NOT NULL)",
                          {el.node.name: CExpr(col, IRI_T), DSK: CExpr(kcol, STR_T)}, {col, kcol})
        else:
            names = Piece(f"(SELECT DISTINCT g AS {col} FROM {self.quads} WHERE g IS NOT NULL)",
                          {el.node.name: CExpr(col, IRI_T)}, {col})
        return self.group(el.pattern, names, ("var", el.node.name))

    # -- basic graph patterns ------------------------------------------------------------
    def bgp(self, triples: List[A.Triple], graph) -> Piece:
        froms: List[str] = []
        conds: List[str] = []
        first: Dict[str, Tuple[str, str, TType]] = {}  # name -> (value sql, key sql, type)
        order: List[str] = []

        def bind(name: str, value: str, key: str, t: TType, direct: Optional[str] = None):
            if name in first:
                pv, pk, pt = first[name]
                if direct is not None and pt == t and pt == IRI_T:
                    conds.append(f"{pv} = {value}")
                else:
                    conds.append(f"{pk} = {key}")
            else:
                first[name] = (value, key, t)
                order.append(name)

        for t in triples:
            a = self.alias()
            if self.keyed:
                bind(DSK, f"{a}.dsk", f"{a}.dsk", STR_T)
            if graph is None:
                froms.append(f"{self.default} AS {a}")
            else:
                froms.append(f"{self.quads} AS {a}")
                if graph[0] == "iri":
                    conds.append(f"{a}.g = {sql_string(graph[1])}")
                else:
                    conds.append(f"{a}.g IS NOT NULL")
                    bind(graph[1], f"{a}.g", f"('<' || {a}.g || '>')", IRI_T, direct=f"{a}.g")
            for pos, node in (("s", t.subject), ("p", t.predicate), ("o", t.object)):
                if pos == "p":
                    value, key, typ = f"{a}.p", f"('<' || {a}.p || '>')", IRI_T
                else:
                    value, key, typ = f"{a}.{pos}", f"{a}.{pos}_nt", ANY_T
                if isinstance(node, A.Var):
                    bind(node.name, value, key, typ, direct=value if pos == "p" else None)
                elif isinstance(node, A.BNode):
                    bind("\x00bgp:" + node.label, value, key, typ, direct=value if pos == "p" else None)
                elif pos == "p":
                    conds.append(f"{a}.p = {sql_string(node.value)}")
                else:
                    conds.append(f"{key} = {sql_string(constant_nt(node))}")
        scope: Dict[str, CExpr] = {}
        sel: List[str] = []
        nonnull: Set[str] = set()
        for name in order:
            if not self.is_var_name(name):
                continue  # blank nodes of the pattern are not variables of the solutions
            value, key, typ = first[name]
            col = self.fresh(name)
            sel.append(f"{value} AS {col}")
            if typ == ANY_T:
                kcol = f'"{col[1:-1]}_k"'
                sel.append(f"{key} AS {kcol}")
                self.keys[col] = kcol
            scope[name] = CExpr(col, typ)
            nonnull.add(col)
        if not sel:
            sel.append(f'1 AS "{self.engine.fresh("__one")}"')
        where = f" WHERE {' AND '.join(conds)}" if conds else ""
        return Piece(f"(SELECT {', '.join(sel)} FROM {', '.join(froms)}{where})", scope, nonnull)

    # -- Join / LeftJoin --------------------------------------------------------------------
    def join(self, g: Piece, p: Piece, left: bool = False, condition: Optional[list] = None) -> Piece:
        """``Join(G, P)``, or ``LeftJoin(G, P, F)`` when ``left`` (``condition`` = F)."""
        if g.sql is None and not left and not condition:
            return self._constants_join(g, p)
        if left:
            # a constant of the optional side (BIND("x" AS ?v) in OPTIONAL) is bound on the rows
            # that the optional side extends only: it becomes a column of P
            consts = [n for n, e in p.scope.items() if e.is_const and not e.is_null
                      and self.is_var_name(n) and n not in g.scope]
            if consts:
                pscope, sel, cols = dict(p.scope), [], set()
                for n in consts:
                    col = self.fresh(n)
                    sel.append(f"{p.scope[n].sql} AS {col}")
                    pscope[n] = CExpr(col, p.scope[n].type)
                    cols.add(col)
                p = Piece(f"(SELECT *, {', '.join(sel)} FROM {p.sql if p.sql is not None else self.one()})",
                          pscope, set(p.nonnull) | cols)
        gsql = g.sql if g.sql is not None else self.one()
        psql = p.sql if p.sql is not None else self.one()
        shared = [n for n in p.scope if n in g.scope and self.is_var_name(n)]
        scope = dict(g.scope)
        nonnull = set(g.nonnull)
        conds: List[str] = []  # conditions on constants of G
        merged_sel: List[str] = []
        eqs: List[Tuple[str, str]] = []  # (G key, P key) of column pairs
        nullable_g: List[str] = []
        nullable_p: List[str] = []
        merge_scope: Dict[str, CExpr] = {}  # values of shared variables inside the join condition
        for n in p.scope:
            if n not in scope or not self.is_var_name(n):
                scope[n] = p.scope[n]
        nonnull |= p.nonnull if not left else set()
        for n in shared:
            a, b = g.scope[n], p.scope[n]
            if a.is_const:
                if a.is_null:
                    scope[n] = b
                    if not left:
                        nonnull |= {b.sql} & p.nonnull
                    continue
                conds.append(f"({self.key(b)} IS NULL OR {self.key(b)} = {self.key(a)})"
                             if b.sql not in p.nonnull else f"{self.key(b)} = {self.key(a)}")
                scope[n] = a
                continue
            if a.type == b.type and a.type.kind in ("iri", "bnode", "str", "int", "bool") and a.sql not in self.keys \
                    and b.sql not in self.keys:
                ka, kb = a.sql, b.sql
            else:
                ka, kb = self.key(a), self.key(b)
            eqs.append((ka, kb))
            if a.sql not in g.nonnull:
                nullable_g.append(ka)
            if b.sql not in p.nonnull:
                nullable_p.append(kb)
            t = unify([a.type, b.type])
            col = self.fresh(n)
            merged = f"COALESCE({coerce(a, t)}, {coerce(b, t)})"
            merged_sel.append(f"{merged} AS {col}")
            if t == ANY_T:
                kcol = f'"{col[1:-1]}_k"'
                merged_sel.append(f"COALESCE({self.key(a)}, {self.key(b)}) AS {kcol}")
                self.keys[col] = kcol
            scope[n] = CExpr(col, t)
            merge_scope[n] = CExpr(merged, t)
            if a.sql in g.nonnull or (b.sql in p.nonnull and not left):
                nonnull.add(col)
        on_filter = ""
        if condition:
            saved = self.ctx.scope
            self.ctx.scope = {**scope, **merge_scope}
            self.ctx.begin_operator(hoist=False)  # the condition of a join: no input relation
            try:
                fs = [_ebv(compile_expr(f, self.ctx)) for f in condition]
                if self.ctx.uid_pending:
                    raise CompileError("BNODE in the filter of an OPTIONAL group is not supported")
            finally:
                self.ctx.scope = saved
            on_filter = " AND ".join(fs)
        extra = (", " + ", ".join(merged_sel)) if merged_sel else ""
        jt = "LEFT JOIN" if left else "JOIN"

        def on(cs: List[str]) -> str:
            allc = cs + conds + ([on_filter] if on_filter else [])
            return " AND ".join(allc) if allc else "TRUE"

        eq = [f"{x} = {y}" for x, y in eqs]
        if not nullable_g and not nullable_p:
            sql = f"(SELECT *{extra} FROM {gsql} AS __g {jt} {psql} AS __p ON {on(eq)})"
        elif left and not nullable_p:
            # LeftJoin is computed per row of G: the rows whose shared variables are all bound
            # take a hash LEFT JOIN on equality, the others the compatibility condition (an
            # unbound value is compatible with every value: a nested-loop join, on few rows)
            compat = [f"({x} IS NULL OR {y} IS NULL OR {x} = {y})" for x, y in eqs]
            gnn = " AND ".join(f"{x} IS NOT NULL" for x in nullable_g)
            gg = self.engine.fresh("__gg")
            sql = (f"(WITH {gg} AS MATERIALIZED (SELECT * FROM {gsql}) "
                   f"SELECT *{extra} FROM (SELECT * FROM {gg} WHERE {gnn}) AS __g LEFT JOIN {psql} AS __p ON {on(eq)} "
                   f"UNION ALL SELECT *{extra} FROM (SELECT * FROM {gg} WHERE NOT ({gnn})) AS __g "
                   f"LEFT JOIN {psql} AS __p ON {on(compat)})")
        elif left:
            compat = [f"({x} IS NULL OR {y} IS NULL OR {x} = {y})" for x, y in eqs]
            sql = f"(SELECT *{extra} FROM {gsql} AS __g LEFT JOIN {psql} AS __p ON {on(compat)})"
        else:
            # equi-join of the rows bound on both sides; the compatibility condition
            # for the rows where a shared variable is unbound
            gnn = " AND ".join(f"{x} IS NOT NULL" for x in nullable_g) or "TRUE"
            pnn = " AND ".join(f"{y} IS NOT NULL" for y in nullable_p) or "TRUE"
            compat = [f"({x} IS NULL OR {y} IS NULL OR {x} = {y})" for x, y in eqs]
            parts = [f"SELECT *{extra} FROM (SELECT * FROM __gg WHERE {gnn}) AS __g JOIN "
                     f"(SELECT * FROM __pp WHERE {pnn}) AS __p ON {on(eq)}"]
            if nullable_g:
                parts.append(f"SELECT *{extra} FROM (SELECT * FROM __gg WHERE NOT ({gnn})) AS __g JOIN __pp AS __p ON {on(compat)}")
            if nullable_p:
                parts.append(f"SELECT *{extra} FROM (SELECT * FROM __gg WHERE {gnn}) AS __g JOIN "
                             f"(SELECT * FROM __pp WHERE NOT ({pnn})) AS __p ON {on(compat)}")
            gg, pp = self.engine.fresh("__gg"), self.engine.fresh("__pp")
            sql = (f"(WITH {gg} AS MATERIALIZED (SELECT * FROM {gsql}), {pp} AS MATERIALIZED (SELECT * FROM {psql}) "
                   + " UNION ALL ".join(x.replace("__gg", gg).replace("__pp", pp) for x in parts) + ")")
        return Piece(sql, scope, nonnull)

    def _constants_join(self, g: Piece, p: Piece) -> Piece:
        """Join of the unit table (possibly with constant bindings) with P."""
        scope = dict(g.scope)
        conds = []
        for n, b in p.scope.items():
            a = g.scope.get(n)
            if a is not None and self.is_var_name(n) and a.is_const and not a.is_null:
                conds.append(f"{self.key(b)} = {self.key(a)}" if b.sql in p.nonnull
                             else f"({self.key(b)} IS NULL OR {self.key(b)} = {self.key(a)})")
                continue
            scope[n] = b
        sql = p.sql
        if conds:
            sql = f"(SELECT * FROM {p.sql if p.sql is not None else self.one()} WHERE {' AND '.join(conds)})"
        return Piece(sql, scope, set(p.nonnull))

    # -- Minus, Union -------------------------------------------------------------------------
    def minus(self, g: Piece, p: Piece) -> Piece:
        shared = [n for n in p.scope if n in g.scope and self.is_var_name(n) and not g.scope[n].is_null]
        if not shared or p.sql is None:
            return g  # disjoint domains: nothing is removed
        gsql = g.sql if g.sql is not None else self.one()
        conds, overlap = [], []
        for n in shared:
            a, b = g.scope[n], p.scope[n]
            x, y = self.key(a), self.key(b)
            if (a.is_const or a.sql in g.nonnull) and b.sql in p.nonnull:
                conds.append(f"{x} = {y}")
                overlap.append("TRUE")
            else:
                conds.append(f"({x} IS NULL OR {y} IS NULL OR {x} = {y})")
                overlap.append(f"({x} IS NOT NULL AND {y} IS NOT NULL)")
        cond = " AND ".join(conds) + " AND (" + " OR ".join(overlap) + ")"
        return Piece(f"(SELECT * FROM {gsql} AS __g WHERE NOT EXISTS (SELECT 1 FROM {p.sql} AS __p WHERE {cond}))",
                     g.scope, g.nonnull)

    def union(self, branches: List[Piece]) -> Piece:
        names: List[str] = []
        for b in branches:
            for n in b.scope:
                if self.is_var_name(n) and n not in names:
                    names.append(n)
        types = {n: unify([b.scope[n].type for b in branches if n in b.scope and not b.scope[n].is_null]) for n in names}
        cols = {n: self.fresh(n) for n in names}
        parts = []
        nonnull = set()
        for n in names:
            if all(n in b.scope and (b.scope[n].sql in b.nonnull or (b.scope[n].is_const and not b.scope[n].is_null))
                   for b in branches):
                nonnull.add(cols[n])
        scope = {n: CExpr(cols[n], types[n]) for n in names}
        for b in branches:
            sel = []
            for n in names:
                e = b.scope.get(n)
                t = types[n]
                if e is None or e.is_null:
                    sel.append(f"NULL::{_sql_type(t)} AS {cols[n]}")
                else:
                    sel.append(f"{coerce(e, t)} AS {cols[n]}")
                if t == ANY_T:
                    k = "NULL::VARCHAR" if e is None or e.is_null else self.key(e)
                    sel.append(f"{k} AS \"{cols[n][1:-1]}_k\"")
            src = b.sql if b.sql is not None else self.one()
            parts.append(f"SELECT {', '.join(sel) or '1 AS __one'} FROM {src}")
        for n in names:
            if types[n] == ANY_T:
                self.keys[cols[n]] = f"\"{cols[n][1:-1]}_k\""
        return Piece("(" + " UNION ALL ".join(parts) + ")", scope, nonnull)

    # -- BIND, VALUES, FILTER (shared with the compiler) ----------------------------------------
    def bind(self, g: Piece, el: A.Bind, graph=None) -> Piece:
        from .compiler import TRIVIAL, Rel

        saved, saved_hook = self.ctx.scope, getattr(self.ctx, "exists_hook", None)
        self.ctx.scope = dict(g.scope)
        self.ctx.exists_hook = lambda node: self.exists(node, g, graph)
        try:
            rel = Rel(g.sql) if g.sql is not None else TRIVIAL
            self.ctx.begin_operator(rel=rel)
            rel = self.compiler.extend(self.ctx, rel, el.var, compile_expr(el.expr, self.ctx))
            scope = self.ctx.scope
        finally:
            self.ctx.scope, self.ctx.exists_hook = saved, saved_hook
        return Piece(None if rel.trivial else rel.sql, scope, set(g.nonnull))

    def values(self, g: Piece, el: A.Values) -> Piece:
        from .compiler import TRIVIAL, Rel

        rel = Rel(g.sql) if g.sql is not None else TRIVIAL
        rel2, scope2 = self.compiler.values_relation(el.vars, el.rows, rel, g.scope)
        scope = dict(g.scope)
        scope.update(scope2)
        return Piece(rel2.sql, scope, set(g.nonnull))

    def filter(self, g: Piece, filters: list, graph=None) -> Piece:
        from .compiler import Rel

        saved, saved_hook = self.ctx.scope, getattr(self.ctx, "exists_hook", None)
        self.ctx.scope = dict(g.scope)
        self.ctx.exists_hook = lambda node: self.exists(node, g, graph)
        self.ctx.begin_operator(rel=Rel(g.sql if g.sql is not None else self.one()))
        try:
            conds = [_ebv(compile_expr(f, self.ctx)) for f in filters]
        finally:
            self.ctx.scope, self.ctx.exists_hook = saved, saved_hook
        src = self.compiler.ensure_uid(self.ctx, Rel(g.sql if g.sql is not None else self.one())).sql
        from .expressions import conjunction

        return Piece(f"(SELECT * FROM {src} WHERE {conjunction(conds)})", g.scope, g.nonnull)


def _ebv(e: CExpr) -> str:
    from .expressions import _ebv as ebv

    return ebv(e)


def _sql_type(t: TType) -> str:
    from .engine import sql_type_of

    return sql_type_of(t)


def _const_any(node) -> str:
    """SQL of a constant term of a pattern in the dynamic representation."""
    if isinstance(node, A.IRI):
        return f"sg_any(0, {sql_string(node.value)}, NULL)"
    d = "@" + node.lang if node.lang else (None if not node.datatype or node.datatype == A.XSD_STRING else node.datatype)
    return f"sg_any(2, {sql_string(node.lexical)}, {sql_string(d) if d else 'NULL'})"


def constant_nt(node) -> str:
    """N-Triples string of a constant term of a pattern, escaped as ``sg_q`` (JSON string
    escaping) does."""
    import json

    if isinstance(node, A.IRI):
        return "<" + node.value + ">"
    lit: A.Literal = node
    q = json.dumps(lit.lexical, ensure_ascii=False)
    if lit.lang:
        return f"{q}@{lit.lang}"
    if lit.datatype and lit.datatype != A.XSD_STRING:
        return f"{q}^^<{lit.datatype}>"
    return q
