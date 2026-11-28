"""Compiler: SPARQL-Generate AST -> relational plan executed by DuckDB.

Every query level (the main query, each nested ``GENERATE { } ... .`` and each
called named query) is compiled to a *solution relation*: one DuckDB temporary
table whose rows are the solution mappings of that level, with one column per
bound variable (see :mod:`sparql_generate.termtypes` for the term representation),
a dense row id ``__rid`` and the row count ``__n``.

The level's output (triple patterns, text template or SELECT projection) is
then a set of ``SELECT`` statements over that table.  The comments name the
translation rule that each function implements.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from . import ast as A
from . import expressions as E
from .expressions import CompileError, compile_expr
from .functions import FUNCTIONS
from .iterators import ITERATORS, IterPlan, IteratorError
from . import streams as _streams
from . import webapis as _webapis

_webapis.register_all()
_streams.register_all()
from .terms import nt_term, sql_string
from .termtypes import (
    ANY_T, BNODE_T, BOOL_T, DOUBLE_T, INT_T, IRI_T, NOCONST, NULL, STR_T, TType, CExpr, const_iri,
    const_literal, const_str, lex, numeric, string_literal_lex, to_any, to_nt, typed, unify, coerce,
)


def _safe(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_]", "_", name)[:40]


CALL_SELECT = A.SG_ITER + "call-select"
BATCH = "\x00batch"  # reserved scope key of the batch key (rule BATCH)
BATCH_T = TType("typed", dt="urn:sg:batch")  # a BIGINT[] column, never an RDF term
# rule (QUAD): the graph in which a level generates its default graph (the N-Triples term of
# the graph name, set by the GRAPH block of an ancestor that contains the sub-query), and the
# key that partitions the levels below a GRAPH block whose graph varies with the solution
GRAPH = "\x00graph"
GPART = "\x00gpart"


@dataclass
class Rel:
    """A FROM-able SQL item producing the current solutions."""

    sql: str
    trivial: bool = False  # the single empty solution


TRIVIAL = Rel("(SELECT 1 AS __one)", trivial=True)


# Jena's names of the default graph (Quad.defaultGraphNodeGenerated, Quad.defaultGraphIRI): a
# quad pattern GRAPH <urn:x-arq:DefaultGraphNode> { ... } generates triples of the default
# graph, as in the Java engine
DEFAULT_GRAPH_NAMES = ("urn:x-arq:DefaultGraphNode", "urn:x-arq:DefaultGraph")
DEFAULT_GRAPH_NAMES_SQL = ", ".join(sql_string("<" + g + ">") for g in DEFAULT_GRAPH_NAMES)


@dataclass
class Plan:
    kind: str
    triple_selects: List[str] = field(default_factory=list)
    text_sql: Optional[str] = None
    select_sql: Optional[str] = None
    select_vars: List[str] = field(default_factory=list)
    select_types: List[TType] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    perform_selects: List[str] = field(default_factory=list)  # (ord, u, args) per action pattern
    quad_selects: List[str] = field(default_factory=list)  # (s, p, o, g) per quad pattern (rule QUAD)

    def actions_sql(self) -> str:
        """The action instances of a PERFORM query, in the order of rule (P)."""
        if not self.perform_selects:
            return "SELECT NULL::BIGINT[] AS ord, NULL::VARCHAR AS u, NULL::VARCHAR[] AS args WHERE FALSE"
        return "SELECT u, args FROM (" + " UNION ALL ".join(f"({q})" for q in self.perform_selects) + ") ORDER BY ord"

    def triples_sql(self, distinct: bool = False) -> str:
        """The triples of the default graph (those of the quad patterns whose graph is Jena's
        name of the default graph included)."""
        parts = [f"SELECT * FROM ({q})" for q in self.triple_selects]
        parts += [f"SELECT s, p, o FROM ({q}) WHERE g IN ({DEFAULT_GRAPH_NAMES_SQL})" for q in self.quad_selects]
        if not parts:
            return "SELECT NULL::VARCHAR AS s, NULL::VARCHAR AS p, NULL::VARCHAR AS o WHERE FALSE"
        body = "\nUNION ALL\n".join(parts)
        if distinct:
            return f"SELECT DISTINCT s, p, o FROM ({body})"
        return body

    @property
    def has_quads(self) -> bool:
        """The plan generates quads in named graphs (GRAPH blocks of a template)."""
        return bool(self.quad_selects)

    def quads_sql(self, distinct: bool = False) -> str:
        """The quads of the dataset: the triples of the default graph with ``g`` NULL, then the
        quads of the named graphs, ``g`` the N-Triples term of the graph name."""
        parts = [f"SELECT s, p, o, NULL::VARCHAR AS g FROM ({q})" for q in self.triple_selects]
        parts += [f"SELECT s, p, o, CASE WHEN g IN ({DEFAULT_GRAPH_NAMES_SQL}) THEN NULL ELSE g END AS g FROM ({q})"
                  for q in self.quad_selects]
        if not parts:
            return ("SELECT NULL::VARCHAR AS s, NULL::VARCHAR AS p, NULL::VARCHAR AS o, NULL::VARCHAR AS g "
                    "WHERE FALSE")
        body = "\nUNION ALL\n".join(parts)
        if distinct:
            return f"SELECT DISTINCT s, p, o, g FROM ({body})"
        return body


# ==========================================================================
# Level context (the ``ctx`` the expression compiler sees)
# ==========================================================================


class LevelContext:
    def __init__(self, compiler: "Compiler", query: A.Query, scope: Dict[str, CExpr],
                 shared_bnodes: Dict[str, CExpr], level_id: int):
        self.compiler = compiler
        self.engine = compiler.engine
        self.query = query
        self.base = query.base or compiler.engine.base
        self.prefixes = query.prefixes
        self.scope = scope
        self.shared_bnodes = shared_bnodes
        self.level_id = level_id
        self.functions = FUNCTIONS
        self.rid_sql = '"__rid"'   # unique row id of the level
        self.idx_sql = '"__idx"'   # 1-based index of the solution within its parent solution
        self.n_sql = '"__n"'       # number of solutions within the parent solution
        self.prid: Optional[CExpr] = None  # row id of the parent solution (nested levels)
        # rule (G): a nested GENERATE/PERFORM is evaluated once on the table of all parent
        # solutions; only a nested TEMPLATE is evaluated per parent solution (rule (T))
        self.partitioned = False
        self._bnode_counter = 0
        self.local_bnodes: Dict[str, CExpr] = {}
        self.agg_columns: Dict[A.Aggregate, CExpr] = {}
        self.in_aggregation = False
        self.descendant_labels: set = set()
        self.materialised = False
        # BNODE before the row id exists: one key per input row of the operator that
        # evaluates it (the reading of SPARQL 1.1 and Jena); see begin_operator()
        self.uid_col: Optional[str] = None
        self.uid_pending = False
        # compound operands computed once as columns of the operator's input relation
        # (expressions.let_sql), added with the per-row identifier by Compiler.ensure_uid
        self.hoist_ok = False
        self.lets: List[Tuple[str, str]] = []
        self.operator_rel = None
        self.warnings: List[str] = compiler.plan.warnings

    # -- variables ------------------------------------------------------------
    def lookup(self, var: A.Var) -> CExpr:
        e = self.scope.get(var.name)
        if e is None:
            return NULL
        return e

    # -- blank nodes ------------------------------------------------------------
    def partition_keys(self) -> List[str]:
        """The keys that partition the evaluation of this level's modifiers: the parent's row
        id for a level evaluated per parent solution (TEMPLATE, calls of iter:call-select), and
        the batch key after a batched iterator (rule BATCH)."""
        keys = [self.prid.sql] if self.partitioned and self.prid is not None else []
        b = self.scope.get(BATCH)
        if b is not None:
            keys.append(b.sql)
        gp = self.scope.get(GPART)
        if gp is not None and not gp.is_const:
            keys.append(gp.sql)  # rule (QUAD): one evaluation per graph of the GRAPH block
        return keys

    def begin_operator(self, hoist: bool = True, rel=None) -> None:
        """The expressions compiled next belong to a new operator (a BIND, the filters of a
        group, an iterator call, GROUP BY, ORDER BY): ``BNODE(s)`` gets a new key per input
        row, so two operators of one solution give two blank nodes (reading R1).  With
        ``hoist``, the operator applies :meth:`Compiler.ensure_uid` to its input relation
        before using its expressions, which may then hoist compound operands into columns."""
        self.uid_col = None
        self.uid_pending = False
        self.hoist_ok = hoist and not self.materialised
        self.lets = []
        # the operator's input relation (a recursive FUNCTION on run-time values reads the
        # argument tuples of its rows at compile time)
        self.operator_rel = rel if self.hoist_ok else None

    def hoist(self, sql: str) -> str:
        """A column of the operator's input relation holding the value of ``sql``."""
        col = f'"{self.engine.fresh("h")}"'
        self.lets.append((col, sql))
        return col

    def bnode_call(self, args: List[CExpr]) -> CExpr:
        """``BNODE()`` / ``BNODE(s)``, the reading of SPARQL 1.1 (as Jena reads
        it): ``BNODE(s)`` is the same blank node for the same string within the expressions
        that one operator evaluates on one input row, and a distinct one for another row
        (duplicates included) or another operator; ``BNODE()`` is distinct at every call.
        Before the row id exists, the key of a row is a column of per-row identifiers added
        to the operator's input relation (:meth:`Compiler.ensure_uid`); after it, the
        expressions of the template and the select expressions are one operator on the row
        ``__rid``."""
        self._bnode_counter += 1
        if self.materialised:
            row = f"CAST({self.rid_sql} AS VARCHAR)"
        else:
            if self.uid_col is None:
                self.uid_col = f'"{self.engine.fresh("uid")}"'
                self.uid_pending = True
            row = self.uid_col
        if not args:
            return CExpr(f"('E{self.level_id}_{self._bnode_counter}_' || {row})", BNODE_T)
        a = args[0]
        s = string_literal_lex(a)
        if s == "NULL":
            return NULL
        return CExpr(f"('L{self.level_id}_' || md5({s}) || '_' || {row})", BNODE_T)

    def template_bnode(self, b: A.BNode) -> CExpr:
        """Blank node of a GENERATE template (rule BNODE-SCOPE): fresh per
        solution of the outermost enclosing level whose template uses the label."""
        if b.label in self.shared_bnodes:
            return self.shared_bnodes[b.label]
        if b.label in self.local_bnodes:
            return self.local_bnodes[b.label]
        self._bnode_counter += 1
        e = CExpr(f"('B{self.level_id}_{self._bnode_counter}_' || CAST({self.rid_sql} AS VARCHAR))", BNODE_T)
        self.local_bnodes[b.label] = e
        return e

    # -- aggregates --------------------------------------------------------------
    def aggregate(self, agg: A.Aggregate) -> CExpr:
        e = self.agg_columns.get(agg)
        if e is None:
            raise CompileError("aggregate used outside of a grouped query")
        return e

    def template_expr(self, t: A.TemplateExpr) -> CExpr:
        raise CompileError("nested TEMPLATE expressions are not supported yet")


# ==========================================================================
# Compiler
# ==========================================================================


class Compiler:
    def __init__(self, engine, distinct_triples: bool = False):
        self.engine = engine
        self.plan = Plan("GENERATE")
        self._levels = 0
        self._pending: Dict[str, List[str]] = {}  # named query -> argument tables pending (rule K)
        self._pending_calls: List[str] = []  # tables of iter:call-select calls being compiled
        self.function_stack: List[tuple] = []  # FUNCTION calls being expanded
        self._perform_prefix: List[int] = []  # positions of the PERFORM elements being compiled
        self._functions: Dict[str, object] = {}
        self.materialise_count = 0
        # the reserved variable whose value tags the triples generated (FROM GENERATE compiled
        # on all its groups at once, datasets.TAG), or None
        self.triple_tag: Optional[str] = None
        # sequential batches (see union_of_batches): when ``sequential``, the batches of the
        # first iterator of the query, if it has a batch size and constant arguments, are
        # compiled one after another (``batch_lo``, ``batch_hi``); its rows, the dataset of the query and
        # the temporary tables in ``protected`` are kept from one batch to the next
        self.sequential = False
        # the batches of the current step, [batch_lo, batch_hi): the first step is the first
        # batch; the next ones group consecutive batches so that a step creates about
        # step_rows rows (Execution._next_batch): a step has a fixed cost (≈ 0.1 s per
        # materialised level), and the memory stays bounded by max(one batch, step_rows rows)
        self.batch_lo, self.batch_hi = 0, 1
        self.step_rows = 1_000_000
        self.n_batches: Optional[int] = None
        self.protected: Optional[set] = None
        self._root_query: Optional[A.Query] = None
        self._root_dataset = None
        self._batch_table: Optional[str] = None

    def library_function(self, iri: str):
        """``(signature, body, base)`` of the FUNCTION named ``iri`` in the library, or None:
        the body is evaluated against the base of the function (the Java engine 2.6.1)."""
        if iri not in self._functions:
            self._functions[iri] = None
            text = self.engine.named_query_text(iri, fetch=False)
            if text is not None:
                from .parser import parse

                try:
                    q = parse(text, base=self.engine.base)
                except Exception:  # noqa: BLE001 - not a query: not a function
                    q = None
                if q is not None and q.kind == "FUNCTION":
                    self._functions[iri] = (q.signature or [], q.function_expr, q.base or self.engine.base)
        return self._functions[iri]

    # -- entry points ----------------------------------------------------------------
    def compile(self, query: A.Query, bindings: Optional[List[Dict[str, object]]] = None) -> Plan:
        self.plan = Plan(query.kind)
        if query.kind not in ("GENERATE", "TEMPLATE", "SELECT", "PERFORM"):
            raise CompileError(f"{query.kind} queries are not supported")
        self._root_query = query
        rel, scope = self.root_relation(query, bindings)
        self.compile_level(query, rel, scope, {}, root=True)
        return self.plan

    def root_relation(self, query: A.Query, bindings) -> Tuple[Rel, Dict[str, CExpr]]:
        if not bindings:
            return TRIVIAL, {}
        vars_ = []
        for b in bindings:
            for k in b:
                if k not in vars_:
                    vars_.append(k)
        if query.signature is not None:
            extra = [v for v in vars_ if v not in {s.name for s in query.signature}]
            if extra:
                raise CompileError(f"initial bindings use variables not in the signature: {extra}")
        rows = [[b.get(v) for v in vars_] for b in bindings]
        return self.values_relation([A.Var(v) for v in vars_], rows, TRIVIAL, {})

    # -- a level -----------------------------------------------------------------------
    def compile_level(self, query: A.Query, rel: Rel, scope: Dict[str, CExpr],
                      shared_bnodes: Dict[str, CExpr], root: bool = False, dataset=None) -> Optional[str]:
        """Compile one level and its output; a nested TEMPLATE level returns the relation
        ``(__prid, t)`` of its text per parent solution (rule T)."""
        ctx, table = self.solutions(query, rel, scope, shared_bnodes, dataset)
        if query.kind == "GENERATE":
            self.output_generate(ctx, table, query)
        elif query.kind == "TEMPLATE":
            return self.output_template(ctx, table, query)
        elif query.kind == "SELECT":
            self.output_select(ctx, table, query)
        elif query.kind == "PERFORM":
            self.output_perform(ctx, table, query)
        return None

    def output_perform(self, ctx: LevelContext, table: str, query: A.Query) -> None:
        """Rule (P): for each element of the clause in order, the action instances
        ``<u, t1..tk>`` of the solutions in order, or the output of a sub-query evaluated
        on the table of all the solutions.  The order is a key ``ord``: the positions of
        the elements along the chain of sub-queries, then the row id."""
        prefix = list(self._perform_prefix)
        for j, el in enumerate(query.perform_clause or [], 1):
            sub = el.query
            self._perform_prefix = prefix + [j]
            try:
                if sub.is_named_call and self.library_query(sub, ctx, "PERFORM") is None:
                    self.output_action(ctx, table, sub)
                elif sub.is_named_call:
                    self.compile_named_call(ctx, table, sub)
                else:
                    sub.base = sub.base or ctx.query.base
                    rel, scope = self.parent_relation(ctx, table)
                    self.compile_level(sub, rel, scope, ctx.shared_bnodes, dataset=ctx.dataset)
            finally:
                self._perform_prefix = prefix

    def library_query(self, sub: A.Query, ctx: LevelContext, kind: str):
        name = compile_expr(sub.name, ctx)
        if not name.is_const or name.type.kind != "iri":
            return None
        text = self.engine.named_query_text(str(name.const), fetch=False)
        if text is None:
            return None
        from .parser import parse

        try:
            q = parse(text, base=self.engine.base)
        except Exception:  # noqa: BLE001 - not a query
            return None
        return q if q.kind == kind else None

    def output_action(self, ctx: LevelContext, table: str, sub: A.Query) -> None:
        """An action ``u(e1..ek)``: one instance per solution (per call-site solution when
        the action has call-site clauses), the arguments as N-Triples terms."""
        callsite = bool(sub.binding_clauses or sub.where is not None or sub.group_by or sub.order_by
                        or sub.limit is not None or sub.offset is not None or sub.values is not None)
        if callsite:
            rel, scope = self.parent_relation(ctx, table)
            cctx, ctable = self.solutions(sub, rel, scope, {}, ctx.dataset, partitioned=False)
        else:
            cctx, ctable = ctx, table
        u = compile_expr(sub.name, cctx)
        if u.is_null or u.type.kind != "iri":
            raise CompileError("an action needs an IRI")
        args = [compile_expr(p, cctx) for p in (sub.call_params or [])]
        nts = ", ".join("NULL::VARCHAR" if a.is_null else f"{to_nt(a, 'o')}::VARCHAR" for a in args)
        ord_ = "[" + ", ".join(str(k) for k in self._perform_prefix) + f", {cctx.rid_sql}]"
        self.plan.perform_selects.append(
            f"SELECT {ord_}::BIGINT[] AS ord, {u.sql} AS u, [{nts}]::VARCHAR[] AS args FROM {ctable}")

    def solutions(self, query: A.Query, rel: Rel, scope: Dict[str, CExpr], shared_bnodes: Dict[str, CExpr],
                  dataset=None, partitioned: Optional[bool] = None):
        """Rules (FROM), (C), (WHERE), (GROUP), (MAT): the level's solution table."""
        self._levels += 1
        ctx = LevelContext(self, query, dict(scope), dict(shared_bnodes), self._levels)
        ctx.prid = ctx.scope.get("\x00prid")
        ctx.partitioned = ctx.prid is not None and query.kind == "TEMPLATE" if partitioned is None else partitioned
        ctx.dataset = dataset  # the dataset of the enclosing level's clauses, if any
        seq_root = self.sequential and query is self._root_query
        if query.dataset_clauses:
            # rule (FROM): evaluated on the input table, before the binding clauses
            from .datasets import prepare

            if seq_root and self._root_dataset is not None:
                rel, ctx.dataset = self._root_dataset  # computed with the first batch
            else:
                rel, ctx.dataset = prepare(self, ctx, rel, query)
                if seq_root and not ctx.dataset.keyed:
                    self._root_dataset = (rel, ctx.dataset)

        # 1. binding clauses, in order (rules ITERATOR, SOURCE, BIND)
        for i, clause in enumerate(query.binding_clauses):
            ctx.begin_operator(rel=rel)
            if isinstance(clause, A.IteratorClause):
                rel = self.compile_iterator(ctx, rel, clause, sequential=seq_root and i == 0)
            elif isinstance(clause, A.SourceClause):
                rel = self.compile_source(ctx, rel, clause)
            elif isinstance(clause, A.Bind):
                rel = self.extend(ctx, rel, clause.var, compile_expr(clause.expr, ctx))
            else:
                raise CompileError(f"unknown binding clause {clause!r}")

        # 2. WHERE (rule WHERE) and trailing VALUES
        if query.where is not None:
            rel = self.compile_group(ctx, rel, query.where)
        if query.values is not None:
            rel = self.compile_values(ctx, rel, query.values)

        # 3. aggregation (rule GROUP)
        aggregates = self.collect_aggregates(query)
        if query.group_by or aggregates:
            rel = self.compile_group_by(ctx, rel, query, aggregates)

        # 4. solution modifiers + materialisation (rules ORDER/SLICE/DISTINCT)
        return ctx, self.materialise(ctx, rel, query)

    # -- helpers to build relations -------------------------------------------------------
    def new_column(self, name: str) -> str:
        return f'"{self.engine.fresh("v")}_{_safe(name)}"'

    def ensure_uid(self, ctx: LevelContext, rel: Rel) -> Rel:
        """Add to ``rel`` the columns that the current operator's expressions read: the
        per-row identifier of its ``BNODE`` calls (:meth:`LevelContext.bnode_call`), then
        its hoisted operands (:meth:`LevelContext.hoist`), in the order they were made."""
        ctx.hoist_ok = False
        if ctx.uid_pending:
            ctx.uid_pending = False
            src = rel.sql if not rel.trivial else "(SELECT 1 AS __one)"
            rel = Rel(f"(SELECT *, replace(CAST(uuid() AS VARCHAR), '-', '') AS {ctx.uid_col} FROM {src})")
        lets, ctx.lets = ctx.lets, []
        for col, sql in lets:
            src = rel.sql if not rel.trivial else "(SELECT 1 AS __one)"
            rel = Rel(f"(SELECT *, {sql} AS {col} FROM {src})")
        return rel

    def extend(self, ctx: LevelContext, rel: Rel, var: A.Var, e: CExpr) -> Rel:
        """Rule BIND: add a column; constants are kept in the scope only."""
        if e.is_const:
            ctx.scope[var.name] = e
            return rel
        rel = self.ensure_uid(ctx, rel)
        col = self.new_column(var.name)
        if rel.trivial:
            new = Rel(f"(SELECT {e.sql} AS {col})")
        else:
            new = Rel(f"(SELECT *, {e.sql} AS {col} FROM {rel.sql})")
        if E.has_raise(e.sql):
            # a RAISE is evaluated for each row that reaches the BIND, also when nothing reads
            # the variable (DuckDB drops the columns that nothing reads)
            new = Rel(f"(SELECT * FROM {new.sql} WHERE {col} IS NULL OR {col} IS NOT NULL)")
        ctx.scope[var.name] = CExpr(col, e.type)
        return new

    def compile_source(self, ctx: LevelContext, rel: Rel, clause: A.SourceClause) -> Rel:
        """Rule SOURCE: a constant IRI binds a constant document; a variable IRI
        is looked up per row."""
        accept = None
        if clause.accept is not None:
            acc = compile_expr(clause.accept, ctx)
            if acc.is_const and acc.const is not None:
                m = re.match(r"^https?://www\.iana\.org/assignments/media-types/(.*)$", str(acc.const))
                accept = m.group(1) if m else None
        src = compile_expr(clause.source, ctx)
        if src.is_const:
            if src.const is None:
                ctx.scope[clause.var.name] = NULL
                return rel
            doc = self.engine.document(str(src.const), accept)
            if doc is None:
                ctx.warnings.append(f"SOURCE <{src.const}>: document not found, ?{clause.var.name} is unbound")
                ctx.scope[clause.var.name] = NULL
                return rel
            path = doc.local_path()
            if path is not None:
                sql = f"(SELECT content FROM read_text({sql_string(path)}))"
            else:
                sql = self.engine.doc_sql_const(doc.text())
            ctx.scope[clause.var.name] = CExpr(sql, typed(doc.datatype), doc)
            return rel
        engine = self.engine

        def udf(uri):
            d = engine.document(uri, accept) if uri is not None else None
            if d is None:
                return None
            return {"k": 2, "v": d.text(), "d": d.datatype}

        name = engine.udf("sg_source", udf, ["VARCHAR"], engine.ANY_TYPE, key=accept)
        return self.extend(ctx, rel, clause.var, CExpr(f"{name}({lex(src)})", ANY_T))

    def compile_iterator(self, ctx: LevelContext, rel: Rel, clause: A.IteratorClause,
                         sequential: bool = False) -> Rel:
        """Rule ITERATOR: a table-valued iterator is cross-joined (constant
        arguments) or a list-valued one is unnested per row (lateral join). ``sequential``:
        the first iterator of a query whose batches are compiled one after another."""
        iri = clause.expr.name
        if iri == CALL_SELECT:
            return self.compile_call_select(ctx, rel, clause)
        impl = ITERATORS.get(iri)
        if impl is None and self.engine.named_query_text(iri, fetch=False) is not None:
            # a named SELECT query called as an iterator, ``ITERATOR <u>(e1..ek)``: the Java
            # engine's IteratorFunctionRegistry loads it as iter:call-select(<u>, e1..ek)
            call = A.Call(CALL_SELECT, (A.IRI(iri),) + tuple(clause.expr.args))
            return self.compile_call_select(ctx, rel, A.IteratorClause(call, clause.vars))
        if impl is None:
            raise CompileError(f"unknown iterator function <{iri}>")
        args = [compile_expr(a, ctx) for a in clause.expr.args]
        rel = self.ensure_uid(ctx, rel)
        nvars = len(clause.vars)
        try:
            plan: IterPlan = impl(ctx, args, nvars)
        except IteratorError as ex:
            raise CompileError(str(ex))
        cols = [self.new_column(v.name) for v in clause.vars]
        ncols = len(plan.columns)
        if plan.kind == "table":
            sel = []
            for i, v in enumerate(clause.vars):
                if i < ncols:
                    cname, t = plan.columns[i]
                    sel.append(f"t.{cname} AS {cols[i]}")
                    ctx.scope[v.name] = CExpr(cols[i], t)
                elif plan.positional and i == ncols:
                    sel.append(f"t.__pos AS {cols[i]}")
                    ctx.scope[v.name] = CExpr(cols[i], INT_T)
                elif plan.positional and i == ncols + 1:
                    sel.append(f"(t.__pos < t.__n - 1) AS {cols[i]}")
                    ctx.scope[v.name] = CExpr(cols[i], BOOL_T)
                else:
                    ctx.warnings.append(f"ITERATOR {iri}: too many variables, ?{v.name} is unbound")
                    ctx.scope[v.name] = NULL
            src = plan.sql
            if plan.batch:
                # rule (BATCH): the k-th batch of every call is evaluated together
                pos = plan.pos_column or ("__pos" if plan.positional else "(row_number() OVER () - 1)")  # file order
                src = f"(SELECT *, {pos} // {int(plan.batch)} AS __bk FROM {plan.sql})"
                sel.append(self.batch_key(ctx, "t.__bk"))
                if sequential:
                    src = self.sequential_batch(src)
            if rel.trivial:
                return Rel(f"(SELECT {', '.join(sel)} FROM {src} AS t)")
            return Rel(f"(SELECT r.*, {', '.join(sel)} FROM {rel.sql} AS r CROSS JOIN {src} AS t)")
        # unnest
        inner_cols = []
        outer_cols = []
        if plan.lockstep:
            for i, v in enumerate(clause.vars):
                if i < len(plan.lockstep):
                    inner_cols.append(f"unnest({plan.lockstep[i]}) AS {cols[i]}")
                    ctx.scope[v.name] = CExpr(cols[i], plan.columns[i][1])
                else:
                    ctx.scope[v.name] = NULL
            base = "SELECT *" if not rel.trivial else "SELECT 1 AS __one"
            return Rel(f"({base}, {', '.join(inner_cols)} FROM {rel.sql})")
        base = f"(SELECT *, {plan.sql} AS __l FROM {rel.sql})"
        if plan.struct:
            inner = f"(SELECT * EXCLUDE (__l), unnest(__l) AS __s, generate_subscripts(__l, 1) AS __i, len(__l) AS __len FROM {base})"
            for i, v in enumerate(clause.vars):
                if i < ncols:
                    cname, t = plan.columns[i]
                    outer_cols.append(f"__s.{cname} AS {cols[i]}")
                    ctx.scope[v.name] = CExpr(cols[i], t)
                elif plan.positional and i == ncols:
                    outer_cols.append(f"(__i - 1) AS {cols[i]}")
                    ctx.scope[v.name] = CExpr(cols[i], INT_T)
                elif plan.positional and i == ncols + 1:
                    outer_cols.append(f"(__i < __len) AS {cols[i]}")
                    ctx.scope[v.name] = CExpr(cols[i], BOOL_T)
                else:
                    ctx.warnings.append(f"ITERATOR {iri}: too many variables, ?{v.name} is unbound")
                    ctx.scope[v.name] = NULL
            if plan.batch:
                outer_cols.append(self.batch_key(ctx, f"(__i - 1) // {int(plan.batch)}"))
            return Rel(f"(SELECT * EXCLUDE (__s, __i, __len), {', '.join(outer_cols)} FROM {inner})")
        cname, t = plan.columns[0]
        for i, v in enumerate(clause.vars):
            if i == 0:
                inner_cols.append(f"unnest(__l) AS {cols[i]}")
                ctx.scope[v.name] = CExpr(cols[i], t)
            elif plan.positional and i == 1:
                inner_cols.append(f"(generate_subscripts(__l, 1) - 1) AS {cols[i]}")
                ctx.scope[v.name] = CExpr(cols[i], INT_T)
            elif plan.positional and i == 2:
                inner_cols.append(f"(generate_subscripts(__l, 1) < len(__l)) AS {cols[i]}")
                ctx.scope[v.name] = CExpr(cols[i], BOOL_T)
            else:
                ctx.warnings.append(f"ITERATOR {iri}: too many variables, ?{v.name} is unbound")
                ctx.scope[v.name] = NULL
        if plan.batch:
            inner_cols.append(self.batch_key(ctx, f"(generate_subscripts(__l, 1) - 1) // {int(plan.batch)}"))
        return Rel(f"(SELECT * EXCLUDE (__l), {', '.join(inner_cols)} FROM {base})")

    def sequential_batch(self, src: str) -> str:
        """The rows of the current batch of the first iterator of the query (sequential
        batches): its rows and their batch index are read once, kept in a temporary table."""
        if self._batch_table is None:
            t = self.engine.fresh("__batches")
            self.engine.execute(f"CREATE TEMP TABLE {t} AS SELECT * FROM {src}")
            self._batch_table = t
            n = self.engine.execute(f"SELECT max(__bk) FROM {t}").fetchall()[0][0]
            self.n_batches = 0 if n is None else int(n) + 1
            # what the next batches reuse: the rows, the dataset of the query, the documents
            self.protected = set(self.engine.temp_tables())
        return f"(SELECT * FROM {self._batch_table} WHERE __bk >= {int(self.batch_lo)} AND __bk < {int(self.batch_hi)})"

    def batch_key(self, ctx: LevelContext, index_sql: str) -> str:
        """Rule (BATCH): the select item of the batch key after a batched iterator, the list of
        the batch indexes of the batched iterators so far; the k-th batches that the calls of
        the iterator deliver for all the input rows form one batch, on which the rest of the
        query is evaluated (the Java engine's ``IteratorPlan.Batches``)."""
        col = self.new_column("batch")
        old = ctx.scope.get(BATCH)
        prev = old.sql if old is not None else "[]::BIGINT[]"
        ctx.scope[BATCH] = CExpr(col, BATCH_T)
        return f"list_append({prev}, CAST({index_sql} AS BIGINT)) AS {col}"

    def compile_call_select(self, ctx: LevelContext, rel: Rel, clause: A.IteratorClause) -> Rel:
        """Rule (C) for ``iter:call-select(u, e1..ek)``: for each row, the named SELECT query
        ``u`` evaluated on the one-row table of its arguments, read as the list of its
        solutions (projection order of ``u``); the empty list when ``(u, args)`` is pending
        on the row's chain of calls (tabling), or when ``u`` is not a SELECT query of the
        library with ``k`` parameters.  Compiled once for all the rows: the rows are written
        with a row key; the distinct (arguments, chain) pairs of each name form its table
        of calls, on which the named query is compiled once, as a level partitioned by the
        call (its modifiers and aggregates apply per call, as one call per row does); the
        solutions are joined back to the rows.  The chain of the calls pending on a row is
        a string column (reserved key ``\\x00csp``, keys separated by ``chr(30)``): the
        recursion is unfolded at compile time and stops when no row is left, which is when
        the per-row recursion of the semantics stops."""
        if not clause.expr.args:
            raise CompileError("iter:call-select needs the IRI of a named SELECT query")
        name = compile_expr(clause.expr.args[0], ctx)
        params = [compile_expr(a, ctx) for a in clause.expr.args[1:]]
        rel = self.ensure_uid(ctx, rel)
        k = len(params)
        p = self.engine.fresh("__cs") + "_"
        if name.is_null or (name.is_const and name.type.kind != "iri"):
            u_sql = "NULL::VARCHAR"
        elif name.type.kind == "iri":
            u_sql = name.sql
        elif name.type.kind == "any":
            u_sql = f"CASE WHEN ({name.sql}).k = 0 THEN ({name.sql}).v END"
        else:
            u_sql = "NULL::VARCHAR"
        pend = ctx.scope.get("\x00csp")
        own = [f"{p}u"] + [f"{p}a{i}" for i in range(k)] + [f"{p}k{i}" for i in range(k)] + \
              [f"{p}pend", f"{p}key", f"{p}cr"]
        sel = [f"{u_sql} AS {p}u"]
        for i, e in enumerate(params):
            sel.append(f"{'NULL' if e.is_null else e.sql} AS {p}a{i}")
            sel.append(f"{to_nt(e, 'o') if not e.is_null else 'NULL'}::VARCHAR AS {p}k{i}")
        sel.append(f"{pend.sql if pend is not None else 'chr(30)'} AS {p}pend")
        key = f"concat_ws(chr(31), {p}u{''.join(f', COALESCE({p}k{i}, chr(0))' for i in range(k))})"
        src = rel.sql if not rel.trivial else "(SELECT 1 AS __one)"
        rows = self.engine.fresh("csrows")
        self.engine.execute(
            f"CREATE TEMP TABLE {rows} AS SELECT *, {key} AS {p}key, row_number() OVER () AS {p}cr "
            f"FROM (SELECT *, {', '.join(sel)} FROM {src})")
        live = f"{p}u IS NOT NULL AND NOT contains({p}pend, chr(30) || {p}key || chr(30))"
        names = [r[0] for r in self.engine.execute(
            f"SELECT {p}u FROM {rows} WHERE {live} GROUP BY {p}u ORDER BY min({p}cr)").fetchall()]
        from .parser import parse

        outs: List[Tuple[str, List[CExpr]]] = []  # (the rows joined with their solutions, outputs)
        for u in names:
            text = self.engine.named_query_text(u)
            called = None
            if text is not None:
                try:
                    called = parse(text, base=self.engine.base)
                except Exception as ex:  # noqa: BLE001 - not a query: no solution
                    ctx.warnings.append(f"iter:call-select <{u}>: {ex}")
            if called is None or called.kind != "SELECT" or len(called.signature or []) != k:
                ctx.warnings.append(f"iter:call-select <{u}>: not a SELECT query with {k} parameters")
                continue
            if len(self._pending_calls) >= self.MAX_CALL_DEPTH:
                raise CompileError(f"named query calls nested deeper than {self.MAX_CALL_DEPTH}")
            # the table of the calls of u: the distinct (arguments, chain), the chain extended
            calls = self.engine.fresh("calls")
            scope: Dict[str, CExpr] = {}
            csel = [f"{p}key AS __key", f"{p}pend AS __pend"]
            for i, (v, e) in enumerate(zip(called.signature, params)):
                col = self.new_column(v.name)
                if e.is_null:
                    scope[v.name] = NULL
                else:
                    csel.append(f"any_value({p}a{i}) AS {col}")
                    scope[v.name] = CExpr(col, e.type)
            chain, ck = self.new_column("csp"), self.new_column("call")
            csel.append(f"{p}pend || {p}key || chr(30) AS {chain}")
            csel.append(f"row_number() OVER (ORDER BY min({p}cr)) AS {ck}")
            self.engine.execute(
                f"CREATE TEMP TABLE {calls} AS SELECT {', '.join(csel)} FROM {rows} "
                f"WHERE {live} AND {p}u = {sql_string(u)} GROUP BY {p}key, {p}pend")
            scope["\x00csp"] = CExpr(chain, STR_T)
            scope["\x00prid"] = CExpr(ck, INT_T)
            self._pending_calls.append(calls)
            try:
                cctx, table = self.solutions(called, Rel(f"(SELECT * FROM {calls})"), scope, {}, None,
                                             partitioned=True)
            finally:
                self._pending_calls.pop()
            if called.select_star or not called.select_vars:
                exprs = [cctx.scope[n] for n in cctx.scope if not n.startswith("\x00")]
            else:
                exprs = [compile_expr(e, cctx) if e is not None else cctx.lookup(v) for v, e in called.select_vars]
            osel = [f"{cctx.prid.sql} AS {p}ck", f"row_number() OVER () AS {p}os"]
            osel += [f"{'NULL' if e.is_null else e.sql} AS {p}o{j}" for j, e in enumerate(exprs)]
            out = self.engine.fresh("csout")
            self.engine.execute(f"CREATE TEMP TABLE {out} AS SELECT {', '.join(osel)} FROM {table}")
            joined = (f"SELECT r.*, o.* EXCLUDE ({p}ck) FROM {rows} AS r JOIN {calls} AS c "
                      f"ON r.{p}u = {sql_string(u)} AND r.{p}key = c.__key AND r.{p}pend = c.__pend "
                      f"JOIN {out} AS o ON o.{p}ck = c.{ck}")
            outs.append((joined, exprs))
        # the j-th output variable is the j-th projected variable of the called queries
        types: List[Optional[TType]] = []
        for j in range(len(clause.vars)):
            ts = [ex[j].type for _, ex in outs if j < len(ex) and not ex[j].is_null]
            types.append(unify(ts) if ts else None)
        cols = [self.new_column(v.name) for v in clause.vars]
        parts = []
        for joined, exprs in outs:
            sel = []
            for j, t in enumerate(types):
                if t is None:
                    continue
                if j < len(exprs) and not exprs[j].is_null:
                    sel.append(f"{coerce(CExpr(f'{p}o{j}', exprs[j].type), t)} AS {cols[j]}")
                else:
                    sel.append(f"NULL AS {cols[j]}")
            drop = ", ".join(own + [f"{p}o{j}" for j in range(len(exprs))] + [f"{p}os"])
            parts.append(f"SELECT * EXCLUDE ({drop}){''.join(', ' + s for s in sel)}, {p}cr AS {p}ocr, "
                         f"{p}os AS {p}oos FROM ({joined})")
        for j, v in enumerate(clause.vars):
            ctx.scope[v.name] = NULL if types[j] is None else CExpr(cols[j], types[j])
        if not parts:
            return Rel(f"(SELECT * EXCLUDE ({', '.join(own)}) FROM {rows} WHERE FALSE)")
        # the rows in their order, each followed by the solutions of its call in their order (the
        # order of the Java engine's iterator, which a TEMPLATE shows)
        union = " UNION ALL ".join(f"({x})" for x in parts)
        return Rel(f"(SELECT * EXCLUDE ({p}ocr, {p}oos) FROM ({union}) ORDER BY {p}ocr, {p}oos)")

    # -- WHERE ----------------------------------------------------------------------------
    def compile_group(self, ctx: LevelContext, rel: Rel, group: A.Group) -> Rel:
        """Rule WHERE: VALUES, BIND and FILTER are compiled here; triple patterns, GRAPH,
        OPTIONAL, UNION, MINUS and nested groups by :mod:`sparql_generate.bgp` (rule BGP);
        property paths, sub-SELECT, SERVICE and EXISTS by the rdflib fallback."""
        from . import bgp

        group = self.lift_embedded_terms(ctx, group)
        needs_dataset = self._needs_dataset(group)
        has_dataset = self.engine.dataset is not None or getattr(ctx, "dataset", None) is not None
        if needs_dataset and not (has_dataset and self.engine.native_where and bgp.supported(group)):
            from .rdfwhere import compile_where_rdflib

            return compile_where_rdflib(self, ctx, rel, group)
        if needs_dataset or any(isinstance(el, (A.Group, A.Optional_, A.Union_, A.Minus)) for el in group.elements):
            # rule (BGP): triple patterns, OPTIONAL, UNION, MINUS, GRAPH and nested groups (which
            # do not see the variables of the input table) compiled to joins
            rel = bgp.PatternCompiler(self, ctx).compile_where(rel, group)
            ctx.begin_operator()
            return rel
        filters: List[CExpr] = []
        for el in group.elements:
            if isinstance(el, A.Values):
                rel = self.compile_values(ctx, rel, el)
            elif isinstance(el, A.Bind):
                ctx.begin_operator(rel=rel)
                rel = self.extend(ctx, rel, el.var, compile_expr(el.expr, ctx))
            elif isinstance(el, A.Filter):
                filters.append(el.expr)
            else:
                raise CompileError(f"unsupported pattern in WHERE: {type(el).__name__}")
        if filters:
            # the filters of a group are one operator, on the rows that the group produced
            ctx.begin_operator(rel=rel)
            conds = [E._ebv(compile_expr(f, ctx)) for f in filters]
            rel = self.ensure_uid(ctx, rel)
            rel = Rel(f"(SELECT * FROM {rel.sql} WHERE {E.conjunction(conds)})")
        return rel

    def lift_embedded_terms(self, ctx: LevelContext, group: A.Group) -> A.Group:
        """A node of a triple pattern of the WHERE clause with embedded expressions
        (``<person/{?name}>``, ``"{?x}"``, ``?{e}``) constrains the pattern as a variable bound
        to its value does: it becomes a fresh variable, bound to the value before the patterns
        when the expression reads only variables of the input table (of the binding clauses or
        of the parent query) and the group is the WHERE clause itself, else equal to the value
        (``FILTER(sameTerm(...))``) in the group of the pattern.  A group without such nodes is
        returned as it is."""
        xnodes = (A.XIRI, A.XLiteral, A.XExpr)

        def has_x(g) -> bool:
            for el in g.elements:
                if isinstance(el, A.TriplesBlock) and any(
                        isinstance(n, xnodes) for t in el.triples for n in (t.subject, t.predicate, t.object)):
                    return True
                for sub in self._sub_patterns(el):
                    if has_x(sub):
                        return True
            return False

        if not has_x(group):
            return group

        def pattern_vars(g, acc: set) -> set:
            for el in g.elements:
                if isinstance(el, A.TriplesBlock):
                    for t in el.triples:
                        for n in (t.subject, t.predicate, t.object):
                            if isinstance(n, A.Var):
                                acc.add(n.name)
                for sub in self._sub_patterns(el):
                    pattern_vars(sub, acc)
                if isinstance(el, A.Graph) and isinstance(el.node, A.Var):
                    acc.add(el.node.name)
            return acc

        def lift(g, top: bool) -> A.Group:
            bound = pattern_vars(g, set())
            binds, out, filters = [], [], []

            def var_for(n):
                self._xterm_counter = getattr(self, "_xterm_counter", 0) + 1
                v = A.Var(f"__xterm{self._xterm_counter}")
                names = {x.name for x in A.iter_vars(n)}
                if top and names <= set(ctx.scope) and not names & bound:
                    binds.append(A.Bind(n, v))
                else:
                    filters.append(A.Filter(A.Call("SAMETERM", (v, n))))
                return v

            for el in g.elements:
                if isinstance(el, A.TriplesBlock):
                    triples = []
                    for t in el.triples:
                        triples.append(A.Triple(*(var_for(n) if isinstance(n, xnodes) else n
                                                  for n in (t.subject, t.predicate, t.object))))
                    out.append(A.TriplesBlock(triples))
                elif isinstance(el, A.Optional_):
                    out.append(A.Optional_(lift(el.pattern, False)))
                elif isinstance(el, A.Minus):
                    out.append(A.Minus(lift(el.pattern, False)))
                elif isinstance(el, A.Union_):
                    out.append(A.Union_([lift(p, False) for p in el.patterns]))
                elif isinstance(el, A.Graph):
                    out.append(A.Graph(el.node, lift(el.pattern, False)))
                elif isinstance(el, A.Group):
                    out.append(lift(el, False))
                else:
                    out.append(el)
            return A.Group(binds + out + filters)

        return lift(group, True)

    @staticmethod
    def _sub_patterns(el) -> list:
        if isinstance(el, (A.Optional_, A.Minus)):
            return [el.pattern]
        if isinstance(el, A.Union_):
            return list(el.patterns)
        if isinstance(el, A.Graph):
            return [el.pattern]
        if isinstance(el, A.Group):
            return [el]
        return []

    @staticmethod
    def _needs_dataset(group: A.Group) -> bool:
        """The group reads the RDF dataset (triple patterns, GRAPH, SERVICE, sub-SELECT, EXISTS)."""
        for el in group.elements:
            if isinstance(el, (A.TriplesBlock, A.Graph, A.Service, A.SubSelect)):
                return True
            if isinstance(el, (A.Optional_, A.Minus)) and Compiler._needs_dataset(el.pattern):
                return True
            if isinstance(el, A.Union_) and any(Compiler._needs_dataset(p) for p in el.patterns):
                return True
            if isinstance(el, A.Group) and Compiler._needs_dataset(el):
                return True
            if isinstance(el, (A.Filter, A.Bind)) and _has_exists(el.expr):
                return True
        return False

    def compile_values(self, ctx: LevelContext, rel: Rel, values: A.Values) -> Rel:
        ctx.begin_operator()
        rel2, scope2 = self.values_relation(values.vars, values.rows, rel, ctx.scope)
        ctx.scope.update(scope2)
        return rel2

    def values_relation(self, vars_: List[A.Var], rows: List[List[object]], rel: Rel,
                        scope: Dict[str, CExpr]) -> Tuple[Rel, Dict[str, CExpr]]:
        """Rule VALUES: an inline table joined on the variables already bound
        (SPARQL compatibility: unbound values are compatible with anything)."""
        ncol = len(vars_)
        consts = [[const_literal(v) if isinstance(v, A.Literal) else const_iri(v.value) if isinstance(v, A.IRI)
                   else CExpr(sql_string(v.label), BNODE_T, v.label) if isinstance(v, A.BNode) else NULL
                   for v in row] for row in rows]
        types = []
        for c in range(ncol):
            types.append(unify([r[c].type for r in consts if not r[c].is_null]))
        new_scope: Dict[str, CExpr] = {}
        cols = [self.new_column(v.name) for v in vars_]
        if not rows:
            sel = ", ".join(f"NULL AS {c}" for c in cols)
            vsql = f"(SELECT {sel} WHERE FALSE)"
        else:
            vrows = []
            for r in consts:
                cells = []
                for c in range(ncol):
                    cells.append("NULL" if r[c].is_null else coerce(r[c], types[c]))
                vrows.append("(" + ", ".join(cells) + ")")
            vsql = f"(SELECT * FROM (VALUES {', '.join(vrows)}) AS __v({', '.join(cols)}))"
        for i, v in enumerate(vars_):
            new_scope[v.name] = CExpr(cols[i], types[i])
        shared = [v for v in vars_ if v.name in scope]
        if rel.trivial:
            return Rel(vsql), new_scope
        if not shared:
            return Rel(f"(SELECT * FROM {rel.sql} CROSS JOIN {vsql})"), new_scope
        conds = []
        merged = []
        for v in shared:
            old = scope[v.name]
            new = new_scope[v.name]
            a = to_any(old) if old.type != new.type else old.sql
            b = to_any(new) if old.type != new.type else new.sql
            conds.append(f"({a} IS NULL OR {b} IS NULL OR {a} = {b})")
            col = self.new_column(v.name)
            t = unify([old.type, new.type])
            merged.append(f"COALESCE({coerce(old, t) if not old.is_null else 'NULL'}, {coerce(new, t)}) AS {col}")
            new_scope[v.name] = CExpr(col, t)
        return Rel(f"(SELECT *, {', '.join(merged)} FROM {rel.sql} JOIN {vsql} ON {' AND '.join(conds)})"), new_scope

    # -- aggregation ------------------------------------------------------------------------
    def collect_aggregates(self, query: A.Query) -> List[A.Aggregate]:
        found: List[A.Aggregate] = []

        def walk(n):
            if isinstance(n, A.Aggregate):
                if n not in found:
                    found.append(n)
                return
            if isinstance(n, A.Call):
                for a in n.args:
                    walk(a)
            elif isinstance(n, (A.XIRI, A.XLiteral)):
                for c in n.components:
                    if not isinstance(c, str):
                        walk(c)
            elif isinstance(n, A.XExpr):
                walk(n.expr)
            elif isinstance(n, A.ListNode):
                walk(n.expr)
            elif isinstance(n, A.Triple):
                walk(n.subject)
                walk(n.predicate)
                walk(n.object)
            elif isinstance(n, A.GenerateTriples):
                for t in n.triples:
                    walk(t)
            elif isinstance(n, (A.TExpr,)):
                walk(n.expr)
            elif isinstance(n, (A.TBox,)):
                for e in n.elements:
                    walk(e)
            elif isinstance(n, A.TFormat):
                walk(n.format)
                for e in n.elements:
                    walk(e)
            elif isinstance(n, A.TGroup):
                walk(tgroup_to_aggregate(n))
            elif isinstance(n, A.OrderCondition):
                walk(n.expr)
            elif isinstance(n, tuple):
                for a in n:
                    walk(a)

        for v, e in query.select_vars:
            walk(e)
        for v, e in query.post_select:
            walk(e)
        for h in query.having:
            walk(h)
        for o in query.order_by:
            walk(o)
        if query.generate_clause:
            for gnode, el in A.flat_generate(query.generate_clause):
                if isinstance(el, A.GenerateTriples):
                    walk(el)
                if gnode is not None:
                    walk(gnode)
        if query.template_clause:
            for el in query.template_clause:
                walk(el)
        return found

    def compile_group_by(self, ctx: LevelContext, rel: Rel, query: A.Query, aggregates: List[A.Aggregate]) -> Rel:
        """Rule GROUP: GROUP BY the keys, one column per aggregate; the scope
        is reduced to the keys and the aggregates."""
        keys: List[str] = []
        new_scope: Dict[str, CExpr] = {}
        sel: List[str] = []
        ctx.begin_operator(rel=rel)
        batch = ctx.scope.get(BATCH)
        if batch is not None:  # rule (BATCH): one evaluation per batch, so groups per batch
            sel.append(batch.sql)
            keys.append(batch.sql)
            new_scope[BATCH] = batch
        for name in (GPART, GRAPH):  # rule (QUAD): groups per graph, the graph kept
            e = ctx.scope.get(name)
            if e is None:
                continue
            if not e.is_const and e.sql not in keys:
                sel.append(e.sql)
                keys.append(e.sql)
            new_scope[name] = e
        if ctx.partitioned:
            # nested TEMPLATE: one group per parent solution; the parent's variables are
            # constant within a group and stay in scope
            parent_cols = set(self.parent_columns(ctx))
            for name, e in ctx.scope.items():
                if not e.is_const and e.sql in parent_cols and name != BATCH:
                    sel.append(e.sql)
                    keys.append(e.sql)
                    new_scope[name] = e
                elif e.is_const:
                    new_scope[name] = e
        for g in query.group_by:
            if isinstance(g, A.Var):
                e = ctx.lookup(g)
                if e.is_const:
                    new_scope[g.name] = e
                    continue
                col = self.new_column(g.name)
                sel.append(f"{e.sql} AS {col}")
                keys.append(e.sql)
                new_scope[g.name] = CExpr(col, e.type)
            else:
                expr, var = g
                e = compile_expr(expr, ctx)
                if var is not None:
                    if e.is_const:
                        new_scope[var.name] = e
                        continue
                    col = self.new_column(var.name)
                    sel.append(f"{e.sql} AS {col}")
                    new_scope[var.name] = CExpr(col, e.type)
                if not e.is_const:
                    keys.append(e.sql)
        for i, agg in enumerate(aggregates):
            col = self.new_column("agg")
            e = self.compile_aggregate(ctx, agg)
            sel.append(f"{e.sql} AS {col}")
            ctx.agg_columns[agg] = CExpr(col, e.type)
            new_scope[f"\x00agg:{i}"] = ctx.agg_columns[agg]  # carried to the solution table
        if not sel:
            sel.append("count(*) AS __cnt")
        if not keys and query.group_by:
            # GROUP BY on constant keys: one group, none on an empty input (SPARQL, Jena);
            # only an implicit group gives a solution on an empty input
            keys.append("NULL::VARCHAR")
        gb = f" GROUP BY {', '.join(keys)}" if keys else ""
        rel = self.ensure_uid(ctx, rel)
        new = Rel(f"(SELECT {', '.join(sel)} FROM {rel.sql}{gb})")
        ctx.scope = new_scope
        if query.having:
            ctx.begin_operator(rel=new)
            conds = [E._ebv(compile_expr(h, ctx)) for h in query.having]
            new = self.ensure_uid(ctx, new)
            new = Rel(f"(SELECT * FROM {new.sql} WHERE {E.conjunction(conds)})")
        return new

    def parent_columns(self, ctx: LevelContext) -> List[str]:
        """Columns inherited from the parent level (they precede ``?_a`` in the scope)."""
        cols = []
        for name, e in ctx.scope.items():
            if not e.is_const:
                cols.append(e.sql)
            if name == "\x00prid":
                break
        return cols

    def compile_aggregate(self, ctx: LevelContext, agg: A.Aggregate) -> CExpr:
        d = "DISTINCT " if agg.distinct else ""
        if agg.name == "COUNT":
            if agg.star:
                return CExpr("count(*)", INT_T)
            e = compile_expr(agg.args[0], ctx)
            return CExpr(f"count({d}{e.sql})", INT_T)
        e = compile_expr(agg.args[0], ctx)
        if agg.name == "SUM":
            t = INT_T if e.type.kind == "int" else DOUBLE_T
            return CExpr(f"sum({d}{numeric(e)})", t)
        if agg.name == "AVG":
            return CExpr(f"avg({d}{numeric(e)})", DOUBLE_T)
        if agg.name in ("MIN", "MAX"):
            f = agg.name.lower()
            if e.type.is_numeric:
                return CExpr(f"{f}({d}{numeric(e)})", DOUBLE_T if e.type.kind != "int" else INT_T)
            return CExpr(f"{f}({d}{e.sql})", e.type)
        if agg.name == "SAMPLE":
            return CExpr(f"any_value({e.sql})", e.type)
        if agg.name == "GROUP_CONCAT":
            sep = sql_string(agg.separator if agg.separator is not None else " ")
            return CExpr(f"COALESCE(string_agg({d}{lex(e)}, {sep}), '')", STR_T)
        raise CompileError(f"unsupported aggregate {agg.name}")

    # -- materialisation ----------------------------------------------------------------------
    def materialise(self, ctx: LevelContext, rel: Rel, query: A.Query) -> str:
        """Rules ORDER, SLICE, DISTINCT, POST-SELECT: write the level's solutions
        to a temporary table with a dense row id (``__rid``) and the row count."""
        order_sql = ""
        ctx.begin_operator(rel=rel)
        if query.order_by:
            keys = []
            for oc in query.order_by:
                e = compile_expr(oc.expr, ctx)
                # SPARQL: unbound sorts before everything else
                for k in self.order_keys(e):
                    keys.append(f"{k} {'DESC NULLS LAST' if oc.direction < 0 else 'ASC NULLS FIRST'}")
            order_sql = " ORDER BY " + ", ".join(keys)
            rel = self.ensure_uid(ctx, rel)
        inner = rel.sql if not rel.trivial else "(SELECT 1 AS __one)"
        distinct = "DISTINCT " if (query.distinct or query.reduced) and query.kind != "GENERATE" else ""
        cols = self.scope_columns(ctx, for_distinct=bool(distinct))
        if distinct and query.kind == "SELECT" and query.select_vars and not query.select_star \
                and all(e is None for _, e in query.select_vars):
            # DISTINCT applies to the projection: the projected variables (and the reserved
            # columns that partition the level), before ORDER BY ... LIMIT
            proj_names = {v.name for v, _ in query.select_vars}
            keep = {e.sql for n, e in ctx.scope.items() if not e.is_const and (n in proj_names or (
                n.startswith("\x00") and (n != "\x00prid" or ctx.partitioned)))}
            cols = [c for c in cols if c in keep]
            for n in list(ctx.scope):
                e = ctx.scope[n]
                if not e.is_const and e.sql not in keep and not n.startswith("\x00"):
                    del ctx.scope[n]
        proj = ", ".join(cols) if cols else "1 AS __one"
        pkeys = ctx.partition_keys()
        part = f"PARTITION BY {', '.join(pkeys)}" if pkeys else ""
        body = f"SELECT {distinct}{proj} FROM {inner}"
        if query.limit is not None or query.offset is not None:
            # rule (G): the slice applies to the table of all parent solutions (per parent
            # solution for a nested TEMPLATE only)
            lo = int(query.offset or 0)
            cond = f"__rn > {lo}"
            if query.limit is not None:
                cond += f" AND __rn <= {lo + int(query.limit)}"
            body = (f"SELECT * EXCLUDE (__rn) FROM (SELECT *, row_number() OVER ({part}{order_sql.replace(' ORDER BY ', ' ORDER BY ' if part else 'ORDER BY ')}) AS __rn "
                    f"FROM ({body})) WHERE {cond}")
        win = f"{part}{order_sql.replace(' ORDER BY ', ' ORDER BY ' if part else 'ORDER BY ')}"
        batch = ctx.scope.get(BATCH)
        if batch is not None:  # rule (BATCH): the solutions batch by batch
            order_sql = f" ORDER BY {batch.sql}" + (", " + order_sql[len(" ORDER BY "):] if order_sql else "")
        with_rid = (f"SELECT *, row_number() OVER ({order_sql.replace(' ORDER BY ', 'ORDER BY ')}) AS __rid, "
                    f"row_number() OVER ({win}) AS __idx, count(*) OVER ({part}) AS __n FROM ({body})")
        # post-select columns and shared blank nodes, evaluated after the row id exists
        ctx.materialised = True
        ctx.hoist_ok = False  # the output expressions read the solution table as it is
        extra = []
        post = [(var.name, compile_expr(expr, ctx)) for var, expr in query.post_select]
        for name, e in post:
            if e.is_const:
                ctx.scope[name] = e
                continue
            col = self.new_column(name)
            extra.append(f"{e.sql} AS {col}")
            ctx.scope[name] = CExpr(col, e.type)
        if query.kind == "GENERATE" and query.generate_clause:
            extra += self.prepare_template_columns(ctx, query)
        sep = ", " if extra else ""
        ctx.inline_patterns = None
        if not E.has_raise(body + "".join(extra)) and self.inline_level(ctx, query, extra):
            # (a level that evaluates a RAISE is written: every solution reaches it, whatever
            # the output reads)
            # rule (MAT), simple level: nothing reads the row id and the output is one
            # statement, which scans the relation once
            return f"(SELECT *{sep}{', '.join(extra)} FROM ({body}))"
        # only the columns that the output and the sub-queries read are written
        kept = self.prune_scope(ctx, query, {x.rsplit(" AS ", 1)[1] for x in extra})
        reserved = self.reserved_columns(ctx, query, extra)
        if not reserved:
            with_rid = body
        elif reserved == ["__rid"]:
            with_rid = f"SELECT *, row_number() OVER ({order_sql.replace(' ORDER BY ', 'ORDER BY ')}) AS __rid FROM ({body})"
        final = f"SELECT {', '.join(kept + reserved + extra) or '1 AS __one'} FROM ({with_rid})"
        table = self.engine.fresh("sol")
        self.engine.execute(f"CREATE TEMP TABLE {table} AS {final}")
        self.materialise_count += 1
        return table

    @staticmethod
    def reserved_columns(ctx: LevelContext, query: A.Query, extra: List[str]) -> List[str]:
        """The reserved columns that something reads: ``__rid`` (rules SUB, CALL,
        BNODE-SCOPE, the order of TEMPLATE and SELECT outputs), ``__idx`` and ``__n``
        (rule LIST).  The window functions that compute them are not free."""
        if query.kind != "GENERATE" or ctx.partitioned:
            return ["__rid", "__idx", "__n"]
        has_list = has_rid = False
        for _, el in A.flat_generate(query.generate_clause):
            if isinstance(el, A.SubQuery):
                has_rid = True
                continue
            for t in el.triples:
                if isinstance(t.object, A.ListNode):
                    has_list = True
                if any(isinstance(n, A.BNode) for n in (t.subject, t.predicate, t.object)):
                    has_rid = True
        if has_list:
            return ["__rid", "__idx", "__n"]
        if has_rid or any(ctx.rid_sql in x for x in extra):
            return ["__rid"]
        return []

    def prune_scope(self, ctx: LevelContext, query: A.Query, extra_cols: set) -> List[str]:
        """Columns of the scope that the rest of the level reads, ``extra_cols`` (computed
        with the materialisation) excepted: the variables used as terms of the GENERATE
        template or in ``LIST``, all the variables of the sub-queries (a sub-query sees its
        parent's solutions, rule SUB), those of TEMPLATE and SELECT outputs, and the reserved
        columns.  Embedded expressions of the template are already columns.  The other
        variables leave the scope."""
        if query.kind == "SELECT" and (query.select_star or not query.select_vars):
            names = set(ctx.scope)
        else:
            names = set()
            for part in (query.template_clause, query.template_before, query.template_separator,
                         query.template_after, query.select_vars, query.name, query.call_params,
                         query.perform_clause):
                _var_names(part, names)
            for el in query.generate_clause or []:
                if isinstance(el, A.GenerateGraph):
                    _var_names(el.node, names)
            for _, el in A.flat_generate(query.generate_clause):
                if isinstance(el, A.SubQuery):
                    _var_names(el.query, names)
                    continue
                for t in el.triples:
                    for n in (t.subject, t.predicate, t.object):
                        if isinstance(n, A.Var):
                            names.add(n.name)
                        elif isinstance(n, A.ListNode):
                            _var_names(n.expr, names)
                        elif isinstance(n, (A.XIRI, A.XLiteral, A.XExpr)) and \
                                getattr(ctx, "xnode_columns", {}).get(n) is None:
                            _var_names(n, names)
        cols: List[str] = []
        for name in list(ctx.scope):
            e = ctx.scope[name]
            if e.is_const or e.sql in extra_cols:
                continue
            if name.startswith("\x00") or name in names:
                if e.sql.startswith('"') and e.sql.endswith('"') and e.sql not in cols:
                    cols.append(e.sql)
            else:
                del ctx.scope[name]
        return cols

    def inline_level(self, ctx: LevelContext, query: A.Query, extra: List[str]) -> bool:
        """A GENERATE level is not materialised when nothing reads its row id — no nested
        query (rule SUB reads ``__rid``), no ``LIST`` (``__idx``, ``__n``), no blank node of
        the template and no ``BNODE()`` evaluated after the row id exists (rule BNODE-SCOPE) —
        and its output is a single statement: at most one triple pattern that is not
        constant.  Several statements would evaluate the relation several times, which costs
        more than writing it once (measured on DuckDB 2.0; unnesting a list of the k triples
        of a row is slower still).  The triple patterns are compiled here and kept in
        ``ctx.inline_patterns`` for :meth:`output_generate`."""
        ctx.inline_patterns = None
        if query.kind != "GENERATE":
            return False
        if not query.generate_clause:
            ctx.inline_patterns = []  # an empty template: no statement reads the relation
            return True
        triples = []
        for el in query.generate_clause:
            if not isinstance(el, A.GenerateTriples):
                return False
            for t in el.triples:
                if isinstance(t.object, A.ListNode) or any(isinstance(n, A.BNode) for n in (t.subject, t.predicate, t.object)):
                    return False
                triples.append(t)
        patterns = [(self.term_nt(ctx, t.subject, "s"), self.term_nt(ctx, t.predicate, "p"),
                     self.term_nt(ctx, t.object, "o")) for t in triples]
        live = [p for p in patterns if not any(e.is_null for e in p) and not all(e.is_const for e in p)]
        if len(live) > 1:
            return False
        sqls = list(extra) + [e.sql for p in patterns for e in p]
        if any(ctx.rid_sql in s or ctx.idx_sql in s or ctx.n_sql in s for s in sqls):
            return False
        ctx.inline_patterns = patterns
        return True

    def scope_columns(self, ctx: LevelContext, for_distinct: bool = False) -> List[str]:
        cols = []
        for name, e in ctx.scope.items():
            if for_distinct and name == "\x00prid" and not ctx.partitioned:
                continue  # DISTINCT ranges over the table of all parent solutions
            if not e.is_const and e.sql.startswith('"') and e.sql.endswith('"') and e.sql not in cols:
                cols.append(e.sql)
        return cols

    @staticmethod
    def order_keys(e: CExpr) -> List[str]:
        """The sort keys of an ORDER BY condition: numbers by value (a lexical comparison put
        "8" after "58"), then the lexical form."""
        if e.is_const:
            return ["1"]
        if e.type.is_numeric:
            return [numeric(e)]
        if e.type.kind == "typed" and numeric(e) != "NULL":
            return [numeric(e), e.sql]
        if e.type.kind in ("str", "lang", "typed", "iri", "bnode"):
            return [e.sql]
        if e.type.kind in ("any", "json"):
            return [numeric(e), lex(e)]
        return [lex(e)]

    def prepare_template_columns(self, ctx: LevelContext, query: A.Query) -> List[str]:
        """Rule POST-SELECT for templates: embedded expressions and blank nodes
        shared with nested queries become columns of the solution table."""
        extra: List[str] = []
        own_labels, desc_labels = template_labels(query)
        for label in own_labels:
            if label in ctx.shared_bnodes:
                continue
            if label in desc_labels:
                e = ctx.template_bnode(A.BNode(label))
                col = self.new_column("bn_" + label)
                extra.append(f"{e.sql} AS {col}")
                ce = CExpr(col, BNODE_T)
                ctx.local_bnodes[label] = ce
                ctx.shared_bnodes[label] = ce
                ctx.scope["\x00bn:" + label] = ce
        ctx.xnode_columns: Dict[object, CExpr] = {}
        for gnode, el in A.flat_generate(query.generate_clause):
            nodes = [gnode] if gnode is not None else []
            if isinstance(el, A.GenerateTriples):
                for t in el.triples:
                    nodes += [t.subject, t.predicate, t.object]
            if nodes:
                for node in nodes:
                    if isinstance(node, (A.XIRI, A.XLiteral, A.XExpr)):
                        try:
                            key = node
                            hash(key)
                        except TypeError:
                            key = id(node)
                        if key in ctx.xnode_columns:
                            continue
                        e = compile_expr(node, ctx)
                        if e.is_const:
                            ctx.xnode_columns[key] = e
                            continue
                        col = self.new_column("x")
                        extra.append(f"{e.sql} AS {col}")
                        ctx.xnode_columns[key] = CExpr(col, e.type)
        return extra

    # -- outputs ---------------------------------------------------------------------------------
    def output_generate(self, ctx: LevelContext, table: str, query: A.Query) -> None:
        """Rule GENERATE: one SELECT per triple pattern, dropping rows where a
        term is unbound or ill-typed for its position; then the nested queries.  Rule (QUAD):
        a pattern of a GRAPH block is a quad of the graph its node names, dropped where the
        node is not an IRI or a blank node; outside GRAPH blocks, the default graph of the
        level is the graph of the GRAPH block that contains it (``GRAPH`` in the scope), if
        any; a sub-query of a GRAPH block generates its default graph in that graph."""
        route = ctx.scope.get(GRAPH)
        if getattr(ctx, "inline_patterns", None) is not None:
            for t_s, t_p, t_o in ctx.inline_patterns:
                self.emit_triple(table, t_s, t_p, t_o, route, self.tag_of(ctx))
            return
        list_counter = 0
        for gnode, el in A.flat_generate(query.generate_clause):
            g = route if gnode is None else self.term_nt(ctx, gnode, "s")
            if g is not None and g.is_null:
                continue  # a graph that no solution names
            if isinstance(el, A.GenerateTriples):
                for t in el.triples:
                    if isinstance(t.object, A.ListNode):
                        list_counter += 1
                        self.output_list(ctx, table, t, list_counter, g)
                        continue
                    s = self.term_nt(ctx, t.subject, "s")
                    p = self.term_nt(ctx, t.predicate, "p")
                    o = self.term_nt(ctx, t.object, "o")
                    self.emit_triple(table, s, p, o, g, self.tag_of(ctx))
            elif isinstance(el, A.SubQuery):
                self.compile_subquery(ctx, table, el.query, graph=None if gnode is None else g)

    def tag_of(self, ctx: LevelContext) -> Optional[CExpr]:
        return ctx.scope.get(self.triple_tag) if self.triple_tag is not None else None

    def emit_triple(self, table: str, s: CExpr, p: CExpr, o: CExpr, g: Optional[CExpr] = None,
                    tag: Optional[CExpr] = None) -> None:
        """A triple pattern of the default graph (``g`` None) or a quad pattern of the graph
        ``g`` (an N-Triples term); with ``tag``, the rows carry its value first (``k``)."""
        if s.is_null or p.is_null or o.is_null or (g is not None and g.is_null):
            return
        if g is not None and g.is_const and g.sql in DEFAULT_GRAPH_NAMES_SQL.split(", "):
            g = None  # GRAPH <urn:x-arq:DefaultGraphNode>: the default graph
        terms = [("s", s), ("p", p), ("o", o)] + ([("g", g)] if g is not None else [])
        if tag is not None:
            terms = [("k", tag)] + terms
        names = ", ".join(n for n, _ in terms)
        sel = ", ".join(f"{e.sql} AS {n}" for n, e in terms)
        target = self.plan.triple_selects if g is None else self.plan.quad_selects
        if all(e.is_const for _, e in terms):
            target.append(f"SELECT {sel} FROM {table} LIMIT 1")
            return
        conds = [f"{n} IS NOT NULL" for n, e in terms if not e.is_const and n != "k"]
        if not conds:  # only the tag varies: one row per value
            target.append(f"SELECT DISTINCT {sel} FROM {table}")
            return
        target.append(f"SELECT {names} FROM (SELECT {sel} FROM {table}) WHERE {' AND '.join(conds)}")

    def term_nt(self, ctx: LevelContext, node, position: str) -> CExpr:
        """N-Triples serialisation of a template term (rule TERM)."""
        if isinstance(node, A.IRI):
            return CExpr(sql_string("<" + node.value + ">"), STR_T, node.value)
        if isinstance(node, A.Literal):
            if position != "o":
                return NULL
            return CExpr(sql_string(nt_term(node)), STR_T, node.lexical)
        if isinstance(node, A.BNode):
            if position == "p":
                return NULL
            e = ctx.template_bnode(node)
            return CExpr(to_nt(e, position), STR_T)
        if isinstance(node, A.Var):
            e = ctx.lookup(node)
        elif isinstance(node, (A.XIRI, A.XLiteral, A.XExpr)):
            try:
                key = node
                hash(key)
            except TypeError:
                key = id(node)
            e = getattr(ctx, "xnode_columns", {}).get(key)
            if e is None:
                e = compile_expr(node, ctx)
        else:
            raise CompileError(f"unsupported term {node!r}")
        if e.is_null:
            return NULL
        if e.is_const:
            sql = to_nt(e, position)
            if sql == "NULL":
                return NULL
            v = self.engine.execute(f"SELECT {sql}").fetchall()[0][0]
            return CExpr(sql_string(v), STR_T, v) if v is not None else NULL
        sql = to_nt(e, position)
        if sql == "NULL":
            return NULL
        return CExpr(sql, STR_T)

    def output_list(self, ctx: LevelContext, table: str, t: A.Triple, k: int, g: Optional[CExpr] = None) -> None:
        """Rule LIST: ``s p LIST(e)`` builds one RDF list per level over all
        solutions, in solution order."""
        s = self.term_nt(ctx, t.subject, "s")
        p = self.term_nt(ctx, t.predicate, "p")
        e = compile_expr(t.object.expr, ctx)
        # one list per evaluation of the level: over all parent solutions (rule (G))
        pk = ctx.partition_keys()
        b = ctx.scope.get(BATCH)
        group = " || ".join((f"array_to_string({k}, 'x')" if b is not None and k == b.sql else f"CAST({k} AS VARCHAR)")
                            + " || '_'" for k in pk) if pk else "''"
        prefix = f"('_:K{ctx.level_id}_{k}_' || {group} || "
        head = CExpr(prefix + "'1')", STR_T)
        cell = CExpr(prefix + f"CAST({ctx.idx_sql} AS VARCHAR))", STR_T)
        nxt = CExpr(f"(CASE WHEN {ctx.idx_sql} >= {ctx.n_sql} THEN '<{A.RDF_NIL}>' ELSE {prefix}CAST({ctx.idx_sql} + 1 AS VARCHAR)) END)", STR_T)
        first = CExpr(sql_string("<" + A.RDF_FIRST + ">"), STR_T, A.RDF_FIRST)
        rest = CExpr(sql_string("<" + A.RDF_REST + ">"), STR_T, A.RDF_REST)
        tag = self.tag_of(ctx)
        self.emit_triple(table, s, p, head, g, tag)
        o = CExpr(to_nt(e, "o"), STR_T) if not e.is_null else NULL
        self.emit_triple(table, cell, first, o, g, tag)
        self.emit_triple(table, cell, rest, nxt, g, tag)

    def compile_subquery(self, ctx: LevelContext, table: str, sub: A.Query, graph: Optional[CExpr] = None) -> None:
        """Rule SUB-GENERATE (nesting = lateral join): the nested query starts
        from the parent's solution table; rule CALL for named queries.  In a GRAPH block
        (``graph``, the N-Triples term of its graph name), the nested query starts from the
        parent solutions that name a graph, and generates its default graph there."""
        if sub.kind != "GENERATE" and not sub.is_named_call:
            raise CompileError(f"nested {sub.kind} queries are not supported in a GENERATE clause")
        if graph is not None:
            ctx, table = self.in_graph(ctx, table, graph)
        if sub.is_named_call:
            self.compile_named_call(ctx, table, sub)
            return
        sub.base = sub.base or ctx.query.base
        rel, scope = self.parent_relation(ctx, table)
        self.compile_level(sub, rel, scope, ctx.shared_bnodes, dataset=ctx.dataset)

    def in_graph(self, ctx: LevelContext, table: str, graph: CExpr):
        """Rule (QUAD): the parent's solutions as seen by a sub-query of a GRAPH block: those
        whose graph term is an IRI or a blank node, the term in ``GRAPH``; when it varies with
        the solution, ``GPART`` (the graphs along the chain of GRAPH blocks) partitions the
        evaluation of the sub-query, which the Java engine runs once per graph."""
        import copy

        sub_ctx = copy.copy(ctx)
        sub_ctx.scope = dict(ctx.scope)
        if graph.is_const:
            sub_ctx.scope[GRAPH] = graph
            return sub_ctx, table
        gcol, pcol = self.new_column("graph"), self.new_column("gpart")
        old = ctx.scope.get(GPART)
        part = graph.sql if old is None or old.is_const else f"({old.sql} || ' ' || {graph.sql})"
        t2 = self.engine.fresh("ing")
        self.engine.execute(f"CREATE TEMP TABLE {t2} AS SELECT *, {graph.sql} AS {gcol}, {part} AS {pcol} "
                            f"FROM {table} WHERE {graph.sql} IS NOT NULL")
        sub_ctx.scope[GRAPH] = CExpr(gcol, STR_T)
        sub_ctx.scope[GPART] = CExpr(pcol, STR_T)
        return sub_ctx, t2

    def parent_relation(self, ctx: LevelContext, table: str):
        """Rule SUB: the parent's solution table is the input table of the nested
        level; the parent's row id is kept as the reserved variable ?_a (address)."""
        col = self.new_column("prid")
        scope = dict(ctx.scope)
        scope["\x00prid"] = CExpr(col, INT_T)
        return Rel(f"(SELECT *, {ctx.rid_sql} AS {col} FROM {table})"), scope

    MAX_CALL_DEPTH = 10000

    def compile_named_call(self, ctx: LevelContext, table: str, sub: A.Query) -> Optional[str]:
        """Rule (K): the call-site clauses are evaluated on the parent's solutions (on all of
        them for GENERATE, rule G; per parent solution for TEMPLATE, rule T), one argument
        tuple per call-site solution, and the named query is evaluated on the table of the
        argument tuples (call by value: it sees its parameters only, S2, S3).  Calls are
        unfolded at compile time, levels being materialised as they are compiled: a call
        whose argument table is empty contributes nothing, and the argument tuples already
        pending along the branch of the call tree are dropped (tabling), so the unfolding
        stops when the tabled evaluation of the recursion does."""
        template = sub.kind == "TEMPLATE"
        callsite = bool(sub.binding_clauses or sub.where is not None or sub.group_by or sub.order_by or sub.having
                        or sub.limit is not None or sub.offset is not None or sub.values is not None
                        or sub.dataset_clauses or sub.post_select)
        if callsite:
            rel, scope = self.parent_relation(ctx, table)
            cctx, ctable = self.solutions(sub, rel, scope, {}, ctx.dataset, partitioned=template)
            parent_rid = cctx.prid.sql
        else:
            cctx, ctable, parent_rid = ctx, table, ctx.rid_sql
        name = compile_expr(sub.name, cctx)
        if not name.is_const or name.type.kind != "iri":
            raise CompileError("named query calls need a constant IRI name")
        text = self.engine.named_query_text(str(name.const))
        if text is None:
            raise CompileError(f"named query <{name.const}> not found")
        from .parser import parse

        called = parse(text, base=self.engine.base)
        if called.kind != sub.kind:
            raise CompileError(f"named query <{name.const}> is a {called.kind} query, expected {sub.kind}")
        params = [compile_expr(p, cctx) for p in (sub.call_params or [])]
        sig = called.signature or []
        if len(params) != len(sig):
            raise CompileError(f"named query <{name.const}> expects {len(sig)} parameters, got {len(params)}")
        # the table of argument tuples, with the N-Triples key of each argument (tabling)
        sel = []
        scope: Dict[str, CExpr] = {}
        for k, (v, e) in enumerate(zip(sig, params)):
            col = self.new_column(v.name)
            if e.is_null:
                sel.append(f"NULL AS {col}")
                scope[v.name] = NULL
            else:
                sel.append(f"{e.sql} AS {col}")
                scope[v.name] = CExpr(col, e.type)
            sel.append(f"{to_nt(e, 'o') if not e.is_null else 'NULL'}::VARCHAR AS __k{k}")
        # rule (QUAD) and FROM GENERATE on all its groups: the graph, its partition and the tag
        # go along with the arguments, and are keys of the call (a call in another graph is
        # another call)
        nkeys = len(sig)
        for rname in (GRAPH, GPART, self.triple_tag or "\x00notag"):
            e = cctx.scope.get(rname)
            if e is None:
                sel.append(f"NULL::VARCHAR AS __k{nkeys}")
            elif e.is_const:
                scope[rname] = e
                sel.append(f"{e.sql}::VARCHAR AS __k{nkeys}")
            else:
                col = self.new_column(rname.lstrip("\x00"))
                sel.append(f"{e.sql} AS {col}")
                sel.append(f"{e.sql}::VARCHAR AS __k{nkeys}")
                scope[rname] = CExpr(col, e.type)
            nkeys += 1
        pcol = self.new_column("prid")
        sel.append(f"{parent_rid} AS {pcol}")
        scope["\x00prid"] = CExpr(pcol, INT_T)
        args = self.engine.fresh("args")
        self.engine.execute(f"CREATE TEMP TABLE {args} AS SELECT {', '.join(sel)} FROM {ctable}")
        key = str(name.const)
        pending = self._pending.setdefault(key, [])
        for prev in pending:
            same = " AND ".join(f"__p.__k{k} IS NOT DISTINCT FROM {args}.__k{k}" for k in range(nkeys)) or "TRUE"
            self.engine.execute(f"DELETE FROM {args} WHERE EXISTS (SELECT 1 FROM {prev} AS __p WHERE {same})")
        if self.engine.execute(f"SELECT count(*) FROM {args}").fetchall()[0][0] == 0:
            return None  # r_m = 0 when the argument table is empty
        if sum(len(v) for v in self._pending.values()) >= self.MAX_CALL_DEPTH:
            raise CompileError(f"named query calls nested deeper than {self.MAX_CALL_DEPTH}")
        pending.append(args)
        try:
            return self.compile_level(called, Rel(f"(SELECT * FROM {args})"), scope, {})
        finally:
            pending.pop()

    def output_template(self, ctx: LevelContext, table: str, query: A.Query) -> Optional[str]:
        """Rule TEMPLATE: one text per solution, concatenated in solution order.  The
        sub-templates of a solution are evaluated on that solution (rule T): each is a
        level partitioned by the parent's row id, whose text per parent solution is joined
        here.  A nested level returns its own relation ``(__prid, t)``."""
        ctx.subtemplate_cols = {}
        joins = []
        self.compile_subtemplates(ctx, table, query.template_clause, joins)
        piece = self.template_text(ctx, query.template_clause)
        src = table + "".join(f" LEFT JOIN {rel} AS {alias} ON {alias}.__prid = {table}.__rid" for alias, rel in joins)
        sep = compile_expr(query.template_separator, ctx) if query.template_separator is not None else const_str("")
        before = compile_expr(query.template_before, ctx) if query.template_before is not None else const_str("")
        after = compile_expr(query.template_after, ctx) if query.template_after is not None else const_str("")
        agg = f"string_agg({piece.sql}, {string_literal_lex(sep) if sep.is_const else 'NULL'} ORDER BY {ctx.rid_sql})"
        if not sep.is_const:
            agg = f"string_agg({piece.sql}, first({lex(sep)}) ORDER BY {ctx.rid_sql})"
        b = f"COALESCE(first({lex(before)} ORDER BY {ctx.rid_sql}), '')"
        a = f"COALESCE(last({lex(after)} ORDER BY {ctx.rid_sql}), '')"
        if ctx.prid is not None and ctx.partitioned:
            return (f"(SELECT {ctx.prid.sql} AS __prid, {b} || COALESCE({agg}, '') || {a} AS t "
                    f"FROM {src} GROUP BY {ctx.prid.sql})")
        self.plan.text_sql = f"SELECT CASE WHEN count(*) = 0 THEN '' ELSE {b} || COALESCE({agg}, '') || {a} END AS t FROM {src}"
        return None

    def compile_subtemplates(self, ctx: LevelContext, table: str, elements, joins: list) -> None:
        """Compile the sub-templates of a template clause (in BOX and FORMAT too)."""
        for el in elements or []:
            sub = None
            if isinstance(el, A.SubQuery):
                sub = el.query
            elif isinstance(el, A.TExpr) and isinstance(el.expr, A.TemplateExpr):
                sub = el.expr.query
            elif isinstance(el, (A.TBox, A.TFormat)):
                self.compile_subtemplates(ctx, table, el.elements, joins)
                continue
            if sub is None:
                continue
            if sub.kind != "TEMPLATE":
                raise CompileError(f"nested {sub.kind} queries are not supported in a TEMPLATE clause")
            if sub.is_named_call:
                rel = self.compile_named_call(ctx, table, sub)
            else:
                sub.base = sub.base or ctx.query.base
                prel, scope = self.parent_relation(ctx, table)
                rel = self.compile_level(sub, prel, scope, ctx.shared_bnodes, dataset=ctx.dataset)
            if rel is None:
                continue  # no argument tuple: the text is empty
            alias = self.engine.fresh("__st")
            joins.append((alias, rel))
            ctx.subtemplate_cols[id(el)] = f"{alias}.t"

    def template_text(self, ctx: LevelContext, elements: List[object]) -> CExpr:
        parts = []
        for el in elements:
            parts.append(f"COALESCE({self.telem_sql(ctx, el)}, '')")
        if not parts:
            return const_str("")
        return CExpr("(" + " || ".join(parts) + ")", STR_T)

    def telem_sql(self, ctx: LevelContext, el) -> str:
        if isinstance(el, A.SubQuery) or isinstance(el, A.TExpr) and isinstance(el.expr, A.TemplateExpr):
            return getattr(ctx, "subtemplate_cols", {}).get(id(el), "NULL")
        if isinstance(el, A.TExpr):
            e = compile_expr(el.expr, ctx)
            return E._str(e).sql if not e.is_null else "NULL"
        if isinstance(el, A.TBox):
            inner = self.template_text(ctx, el.elements).sql
            # two spaces after every line break but a final one (the Java engine indents a
            # line when it writes its first character); RE2 has no look-ahead
            return f"sg_box({inner})"
        if isinstance(el, A.TFormat):
            fmt = compile_expr(el.format, ctx)
            args = [f"COALESCE({self.telem_sql(ctx, e)}, '')" for e in el.elements]
            return f"printf({lex(fmt)}, {', '.join(args)})"
        if isinstance(el, A.TGroup):
            return compile_expr(tgroup_to_aggregate(el), ctx).sql
        raise CompileError(f"unsupported template element {el!r}")

    def output_select(self, ctx: LevelContext, table: str, query: A.Query) -> None:
        """Rule SELECT: project the solution table."""
        sel = []
        if query.select_star or not query.select_vars:
            names = [n for n in ctx.scope if not n.startswith("\x00")]
            exprs = [ctx.scope[n] for n in names]
        else:
            names, exprs = [], []
            for v, e in query.select_vars:
                names.append(v.name)
                exprs.append(compile_expr(e, ctx) if e is not None else ctx.lookup(v))
        for n, e in zip(names, exprs):
            sel.append(f"{to_nt(e, 'o') if not e.is_null else 'NULL'} AS \"{_safe(n)}\"")
        self.plan.select_vars = names
        self.plan.select_types = [e.type for e in exprs]
        self.plan.select_sql = f"SELECT {', '.join(sel) or '1 AS __one'} FROM {table} ORDER BY {ctx.rid_sql}"


# -- small helpers --------------------------------------------------------------------------------


def _has_exists(expr) -> bool:
    if isinstance(expr, A.Exists):
        return True
    if isinstance(expr, A.Call):
        return any(_has_exists(a) for a in expr.args)
    return False


def _var_names(node, acc: set) -> None:
    """Names of all the variables mentioned in an AST fragment (conservative: the whole
    sub-tree, sub-queries included)."""
    if node is None or isinstance(node, (str, int, float, bool)):
        return
    if isinstance(node, A.Var):
        acc.add(node.name)
        return
    if isinstance(node, (list, tuple)):
        for n in node:
            _var_names(n, acc)
        return
    if isinstance(node, dict):
        for n in node.values():
            _var_names(n, acc)
        return
    fields = getattr(node, "__dataclass_fields__", None)
    if fields:
        for f in fields:
            _var_names(getattr(node, f), acc)


def template_labels(query: A.Query):
    """Blank node labels used in this level's template, and those used in nested templates."""
    own: set = set()
    desc: set = set()

    def labels_of(q: A.Query, acc: set, deep: set):
        for _, el in A.flat_generate(q.generate_clause):
            if isinstance(el, A.GenerateTriples):
                for t in el.triples:
                    for n in (t.subject, t.predicate, t.object):
                        if isinstance(n, A.BNode):
                            acc.add(n.label)
            elif isinstance(el, A.SubQuery) and not el.query.is_named_call:
                labels_of(el.query, deep, deep)

    labels_of(query, own, desc)
    return own, desc


def tgroup_to_aggregate(g: A.TGroup) -> A.Aggregate:
    pieces = []
    for el in g.elements:
        if isinstance(el, A.TExpr):
            pieces.append(A.Call("STR", (el.expr,)))
        else:
            raise CompileError("GROUP in TEMPLATE only supports plain expressions")
    expr = pieces[0] if len(pieces) == 1 else A.Call("CONCAT", tuple(pieces))
    return A.Aggregate("GROUP_CONCAT", (expr,), g.distinct, g.separator if g.separator is not None else "")


def union_of_batches(query: A.Query, root: bool = True) -> bool:
    """True if the output of ``query`` on a list of solutions is the union of its outputs on
    any partition of that list, so that the batches of its first iterator may be evaluated
    one after another (the condition of the Java engine 2.6.2, ``RootPlan.isUnionOfBatches``):
    a GENERATE query with a template, without DISTINCT, REDUCED, aggregate, GROUP BY, HAVING,
    ORDER BY, LIMIT, OFFSET or LIST, whose sub-queries are such queries without dataset
    clauses. Also excluded here: GRAPH blocks, named queries (a call, iter:call-select, a
    named query as an iterator) and initial bindings (checked by the caller)."""
    if query.kind != "GENERATE" or query.generate_clause is None:
        return False
    if (query.distinct or query.reduced or query.group_by or query.having or query.order_by
            or query.limit is not None or query.offset is not None):
        return False
    if not root and query.dataset_clauses:
        return False
    for clause in query.binding_clauses:
        if isinstance(clause, A.IteratorClause) and not clause.expr.name.startswith(A.SG_ITER):
            return False
        if isinstance(clause, A.IteratorClause) and clause.expr.name == CALL_SELECT:
            return False
    if Compiler.collect_aggregates(None, query):
        return False
    for el in query.generate_clause:
        if isinstance(el, A.GenerateGraph):
            return False
        if isinstance(el, A.GenerateTriples):
            if any(isinstance(t.object, A.ListNode) for t in el.triples):
                return False
        elif isinstance(el, A.SubQuery):
            if el.query.is_named_call or not union_of_batches(el.query, root=False):
                return False
    return True
