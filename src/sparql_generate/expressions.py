"""Compilation of SPARQL expressions to DuckDB SQL.

``compile_expr(expr, ctx)`` returns a :class:`CExpr`.  ``ctx`` is a
:class:`~sparql_generate.compiler.LevelContext`: it resolves variables to
compiled column references or constants, knows the BASE IRI, the row id column
of the current solution relation and the custom function registry.

SPARQL evaluation errors are represented by SQL ``NULL``; every rule below
preserves ``NULL`` (unbound/error) propagation, with the exceptions SPARQL
itself defines (``BOUND``, ``COALESCE``, ``IF``, ``||``, ``&&``, ``!``).
"""
from __future__ import annotations

import re
from typing import List, Optional

from . import ast as A
from .raising import MARKER, RAISE_MACRO
from .terms import resolve_iri, sql_string, valid_iri
from .termtypes import (
    ANY_T, BOOL_T, DATETIME_T, DOUBLE_T, INT_T, IRI_T, BNODE_T, JSON_T, NOCONST, NULL, STR_T,
    TType, CExpr, const_bool, const_int, const_iri, const_literal, const_str, datatype_sql,
    NUMERIC_DATATYPES, lang as lang_t, lex, numeric, string_literal_lex, to_any, to_nt, typed, unify, coerce,
    ANY_SQL_TYPE,
)


class CompileError(Exception):
    pass


class DocumentNotFound(CompileError):
    """A document, a graph or a named query that cannot be found, with ``fail_on_missing``
    (``--fail-on-missing``, key ``failOnMissing``, as the Java engine 2.2.3)."""

    def __init__(self, iri: str):
        super().__init__(f"Document not found: {iri}")
        self.iri = iri


def compile_expr(expr, ctx) -> CExpr:
    if isinstance(expr, A.Var):
        return ctx.lookup(expr)
    if isinstance(expr, A.Literal):
        return const_literal(expr)
    if isinstance(expr, A.IRI):
        return const_iri(expr.value)
    if isinstance(expr, A.BNode):
        return ctx.template_bnode(expr)
    if isinstance(expr, A.XIRI):
        return compile_xiri(expr, ctx)
    if isinstance(expr, A.XLiteral):
        return compile_xliteral(expr, ctx)
    if isinstance(expr, A.XExpr):
        return compile_expr(expr.expr, ctx)
    if isinstance(expr, A.Call):
        return compile_call(expr, ctx)
    if isinstance(expr, A.Aggregate):
        return ctx.aggregate(expr)
    if isinstance(expr, A.Exists):
        hook = getattr(ctx, "exists_hook", None)
        if hook is None:
            raise CompileError("EXISTS is only supported in the FILTER and BIND of a WHERE clause on an RDF dataset")
        return hook(expr)
    if isinstance(expr, A.TemplateExpr):
        return ctx.template_expr(expr)
    raise CompileError(f"cannot compile expression {expr!r}")


# -- embedded expressions -----------------------------------------------------


def compile_xiri(node: A.XIRI, ctx) -> CExpr:
    """``<a{ e1 }b{ e2 }>`` is ``IRI(CONCAT("a", ENCODE_FOR_URI(STR(e1)), "b", ...))``
    (the semantics applies STR first; the Java engine does not, divergence D11)."""
    parts: List[CExpr] = []
    for c in node.components:
        if isinstance(c, str):
            parts.append(const_str(c))
        else:
            parts.append(_encode_for_uri(_str(compile_expr(c, ctx))))
    s = _concat(parts)
    if isinstance(node.components[0], str) and valid_iri(node.components[0]) and not s.is_const:
        # constant absolute head and percent-encoded dynamic parts: always a valid IRI
        return CExpr(s.sql, IRI_T)
    return _iri(s, ctx)


def compile_xliteral(node: A.XLiteral, ctx) -> CExpr:
    parts: List[CExpr] = []
    for c in node.components:
        if isinstance(c, str):
            parts.append(const_str(c))
        else:
            parts.append(_str(compile_expr(c, ctx)))
    s = _concat(parts)
    if node.lang is not None:
        return CExpr(s.sql, lang_t(node.lang), s.const)
    if node.datatype is not None:
        dt = compile_expr(node.datatype, ctx)
        return _strdt(s, dt)
    return s


# -- helpers for the built-ins -------------------------------------------------


def _all_const(args: List[CExpr]) -> bool:
    return all(a.is_const for a in args)


def _concat(parts: List[CExpr]) -> CExpr:
    if not parts:
        return const_str("")
    if _all_const(parts):
        if any(p.const is None or not p.type.is_stringy for p in parts):
            return NULL
        return const_str("".join(str(p.const) for p in parts))
    sqls = []
    for p in parts:
        if p.is_const and p.type.is_stringy and p.const is not None:
            sqls.append(sql_string(str(p.const)))
            continue
        s = string_literal_lex(p)
        if s == "NULL":
            return NULL
        sqls.append(s)
    # merge adjacent constants
    if len(sqls) == 1:
        return CExpr(sqls[0], STR_T)
    return CExpr("(" + " || ".join(sqls) + ")", STR_T)


def _str(e: CExpr) -> CExpr:
    """``STR(e)``: lexical form of literals, IRI string of IRIs; error on blank nodes."""
    if e.is_null:
        return NULL
    if e.type.kind == "bnode":
        return NULL
    if e.type.kind == "str":
        return e
    if e.is_const and e.type.kind in ("iri", "lang", "typed"):
        return const_str(str(e.const))
    if e.is_const and e.type.kind == "int":
        return const_str(str(e.const))
    if e.is_const and e.type.kind == "bool":
        return const_str("true" if e.const else "false")
    if e.type.kind == "any":
        # a triple term: Jena's string "<< s p o >>" (the terms in N-Triples)
        return CExpr(f"CASE WHEN ({e.sql}).k IN (0, 2) THEN ({e.sql}).v WHEN ({e.sql}).k = 3 THEN "
                     f"'<< ' || substr(({e.sql}).v, 5, length(({e.sql}).v) - 8) || ' >>' END", STR_T)
    return CExpr(lex(e), STR_T)


def _encode_for_uri(e: CExpr) -> CExpr:
    if e.is_null:
        return NULL
    if e.is_const and e.type.is_stringy:
        from urllib.parse import quote

        return const_str(quote(str(e.const), safe="-_.~"))
    s = string_literal_lex(e)
    if s == "NULL":
        return NULL
    return CExpr(f"url_encode({s})", STR_T)


def _iri(e: CExpr, ctx) -> CExpr:
    """``IRI(e)``: an IRI stays; a string literal is resolved against BASE and checked."""
    if e.is_null:
        return NULL
    if e.type.kind == "iri":
        return e
    if e.type.kind == "any":
        ctx.engine.ensure_resolve_udf()
        s = f"CASE WHEN ({e.sql}).k = 0 THEN ({e.sql}).v WHEN ({e.sql}).k = 2 THEN sg_iri_ok(sg_resolve(({e.sql}).v, {_base_sql(ctx)})) END"
        return CExpr(s, IRI_T)
    s = string_literal_lex(e)
    if s == "NULL":
        return NULL
    if e.is_const:
        v = resolve_iri(str(e.const), ctx.base)
        return const_iri(v) if valid_iri(v) else NULL
    # resolve (Python UDF) only relative references
    ctx.engine.ensure_resolve_udf()
    return CExpr(f"sg_iri_ok(CASE WHEN regexp_matches({s}, '^[A-Za-z][A-Za-z0-9+.-]*:') THEN {s} ELSE sg_resolve({s}, {_base_sql(ctx)}) END)", IRI_T)


def _base_sql(ctx) -> str:
    return sql_string(ctx.base) if ctx.base else "NULL"


def _strdt(s: CExpr, dt: CExpr) -> CExpr:
    if s.is_null or dt.is_null:
        return NULL
    lexs = string_literal_lex(s)
    if lexs == "NULL":
        return NULL
    if dt.is_const and dt.type.kind == "iri":
        t = typed(str(dt.const))
        if t.kind == "int":
            if s.is_const:
                try:
                    return const_int(int(str(s.const)))
                except ValueError:
                    return CExpr(sql_string(str(s.const)), TType("typed", dt=A.XSD_INTEGER), s.const)
            return CExpr(f"COALESCE(TRY_CAST({lexs} AS BIGINT), NULL)", INT_T) if False else CExpr(lexs, TType("typed", dt=A.XSD_INTEGER))
        if t.kind in ("double", "bool"):
            return CExpr(lexs, TType("typed", dt=str(dt.const)))
        return CExpr(lexs, t, s.const if s.is_const else NOCONST)
    if dt.type.kind == "any":
        # a datatype known at run time (COALESCE(IRI(...), RAISE(...)), for instance)
        return CExpr(f"CASE WHEN ({dt.sql}).k = 0 THEN sg_any(2, {lexs}, NULLIF(({dt.sql}).v, "
                     f"{sql_string(A.XSD_STRING)})) END", ANY_T)
    if dt.type.kind != "iri":
        return NULL
    # xsd:string is the simple literal (RDF 1.1)
    return CExpr(f"sg_any(2, {lexs}, NULLIF({dt.sql}, {sql_string(A.XSD_STRING)}))", ANY_T)


def _strlang(s: CExpr, tag: CExpr) -> CExpr:
    if s.is_null or tag.is_null:
        return NULL
    lexs = string_literal_lex(s)
    tags = string_literal_lex(tag)
    if lexs == "NULL" or tags == "NULL":
        return NULL
    if tag.is_const:
        return CExpr(lexs, lang_t(str(tag.const)), s.const if s.is_const else NOCONST)
    return CExpr(f"sg_any(2, {lexs}, '@' || {tags})", ANY_T)


def _ebv(e: CExpr) -> str:
    """Effective boolean value as SQL BOOLEAN (NULL on error)."""
    if isinstance(e, RaiseExpr):
        # a RAISE has no value: evaluated, it raises; one copy of the call, not the six of
        # the effective boolean value of a dynamic term
        return f"CAST({e.raw} AS BOOLEAN)"
    t = e.type
    if t.kind == "bool":
        return e.sql
    if t.kind == "int" or t.kind == "double":
        return f"({e.sql} <> 0)"
    if t.kind in ("str", "lang"):
        return f"(length({e.sql}) > 0)"
    if t.kind == "typed":
        if t.dt == A.XSD_BOOLEAN:
            return f"CASE {e.sql} WHEN 'true' THEN TRUE WHEN '1' THEN TRUE WHEN 'false' THEN FALSE WHEN '0' THEN FALSE END"
        if t.is_numeric:
            return f"(TRY_CAST({e.sql} AS DOUBLE) <> 0)"
        return f"(length({e.sql}) > 0)"
    if t.kind == "json":
        return (f"CASE json_type({e.sql}) WHEN 'BOOLEAN' THEN CAST({e.sql} AS BOOLEAN) "
                f"WHEN 'VARCHAR' THEN length(json_extract_string({e.sql}, '$')) > 0 "
                f"WHEN 'NULL' THEN NULL ELSE sg_json_num({e.sql}) <> 0 END")
    if t.kind == "any":
        return (f"CASE WHEN ({e.sql}).k <> 2 THEN NULL WHEN ({e.sql}).d = '{A.XSD_BOOLEAN}' THEN ({e.sql}).v IN ('true','1') "
                f"WHEN sg_any_num({e.sql}) IS NOT NULL THEN sg_any_num({e.sql}) <> 0 ELSE length(({e.sql}).v) > 0 END")
    return "NULL"


def _compare(op: str, a: CExpr, b: CExpr) -> CExpr:
    """``=``, ``!=``, ``<``... following SPARQL operator mapping by static types."""
    if a.is_null or b.is_null:
        return NULL
    ta, tb = a.type, b.type
    sqlop = {"=": "=", "!=": "<>", "<": "<", ">": ">", "<=": "<=", ">=": ">="}[op]
    if a.is_const and b.is_const and ((ta.kind in ("int", "double") and tb.kind in ("int", "double"))
                                      or (ta == tb and ta.kind in ("str", "bool"))):
        import operator

        pyop = {"=": operator.eq, "!=": operator.ne, "<": operator.lt, ">": operator.gt,
                "<=": operator.le, ">=": operator.ge}[op]
        return const_bool(pyop(a.const, b.const))
    if ta.is_numeric and tb.is_numeric:
        return CExpr(f"({numeric(a)} {sqlop} {numeric(b)})", BOOL_T)
    if ta.kind == "bool" and tb.kind == "bool":
        return CExpr(f"({a.sql} {sqlop} {b.sql})", BOOL_T)
    stringy = {"str", "lang", "typed"}
    if ta.kind in stringy and tb.kind in stringy and (ta == tb or (ta.is_stringy and tb.is_stringy)):
        if op in ("=", "!=") and ta != tb:
            # different language tags: never equal
            return const_bool(op == "!=")
        return CExpr(f"({a.sql} {sqlop} {b.sql})", BOOL_T)
    if ta.kind == "iri" and tb.kind == "iri" or ta.kind == "bnode" and tb.kind == "bnode":
        if op in ("=", "!="):
            return CExpr(f"({a.sql} {sqlop} {b.sql})", BOOL_T)
        return NULL
    if ta.kind in ("iri", "bnode") and tb.kind in ("iri", "bnode") and op in ("=", "!="):
        return const_bool(op == "!=")
    if ta.kind in ("iri", "bnode") and tb.is_literal or tb.kind in ("iri", "bnode") and ta.is_literal:
        if op in ("=", "!="):
            return const_bool(op == "!=")
        return NULL
    # dynamic: compare numerically when both numeric, else on lexical form + kind
    na, nb = numeric(a), numeric(b)
    if ta.is_numeric or tb.is_numeric:
        if na == "NULL" or nb == "NULL":
            return NULL
        return CExpr(f"({na} {sqlop} {nb})", BOOL_T)
    if ta.kind == "bool" or tb.kind == "bool":
        ea, eb = _ebv(a), _ebv(b)
        return CExpr(f"({ea} {sqlop} {eb})", BOOL_T)
    if op in ("=", "!="):
        same = f"(sg_any_eq({to_any(a)}, {to_any(b)}))"
        return CExpr(same if op == "=" else f"(NOT {same})", BOOL_T)
    return CExpr(f"(CASE WHEN {na} IS NOT NULL AND {nb} IS NOT NULL THEN {na} {sqlop} {nb} ELSE {lex(a)} {sqlop} {lex(b)} END)", BOOL_T)


def _arith(op: str, a: CExpr, b: CExpr, ctx=None) -> CExpr:
    """``+ - * /`` with the numeric type promotion of XPath (SPARQL 1.1 §17.3): integer <
    decimal < float < double, and integer / integer is a decimal.  Static fast paths for
    integers and doubles; otherwise the result type is decided per row (``sg_arith``)."""
    if a.is_null or b.is_null:
        return NULL
    if numeric(a) == "NULL" or numeric(b) == "NULL":
        return NULL
    ka, kb = a.type.kind, b.type.kind
    folded = _fold_arith(op, a, b)
    if folded is not None:
        return folded
    if ka == "int" and kb == "int" and op in ("+", "-", "*"):
        if a.is_const and b.is_const:
            return const_int(eval(f"{a.const}{op}{b.const}"))  # noqa: S307 - integers only
        return CExpr(f"({a.sql} {op} {b.sql})", INT_T)
    if ka in ("int", "double") and kb in ("int", "double") and "double" in (ka, kb):
        if op == "/":
            return CExpr(f"CASE WHEN {b.sql} <> 0 THEN {a.sql} / {b.sql} END", DOUBLE_T)
        return CExpr(f"({a.sql} {op} {b.sql})", DOUBLE_T)
    ra, rb = _static_rank(a.type), _static_rank(b.type)
    if ra is not None and rb is not None and max(ra, rb) <= 1 and (max(ra, rb) == 1 or op == "/"):
        # integer and decimal operands, a decimal result: exact as Jena's BigDecimal
        return CExpr(let_sql({"a": a, "b": b}, lambda v: _decimal_sql(op, lex(v["a"]), lex(v["b"])), ctx), DECIMAL_T)

    # the type of the result is known per row only: a decimal result is exact (Jena's
    # BigDecimal: the UDF for * and /, and for + and - beyond DECIMAL(38,18)), the other
    # cases are sg_arith
    def body(v):
        va, vb = to_any(v["a"]), to_any(v["b"])
        ra_, rb_ = f"sg_num_rank({va})", f"sg_num_rank({vb})"
        dec = f"{ra_} IS NOT NULL AND {rb_} IS NOT NULL AND greatest({ra_}, {rb_})"  # greatest ignores NULL
        if op in ("*", "/"):
            cond = f"({dec} = 1 OR {dec} = 0 AND {sql_string(op)} = '/')"
        else:
            cond = f"({dec} = 1 AND NOT (sg_dec_fits(({va}).v) AND sg_dec_fits(({vb}).v)))"
        return (f"CASE WHEN {cond} THEN sg_any(2, sg_decimal_udf(({va}).v, ({vb}).v, {sql_string(op)}), "
                f"'{A.XSD_DECIMAL}') ELSE sg_arith({va}, {vb}, {sql_string(op)}) END")

    return CExpr(let_sql({"a": a, "b": b}, body, ctx), ANY_T)


def _cheap(e: CExpr) -> bool:
    """A constant or a column reference: copying its SQL costs nothing."""
    return e.is_const or bool(_SIMPLE_SQL.match(e.sql))


DECIMAL_T = TType("typed", dt=A.XSD_DECIMAL)


def _static_rank(t: TType):
    """The rank of a statically known numeric type (0 integer, 1 decimal, 2 float, 3 double),
    None when the type is not known before run time or not numeric."""
    if t.kind == "int":
        return 0
    if t.kind == "double":
        return 3
    if t.kind == "typed" and t.dt in NUMERIC_DATATYPES:
        return _RANK.get(t.dt, 0)
    return None


def _decimal_sql(op: str, a: str, b: str) -> str:
    """The canonical lexical form of the decimal ``a op b`` (lexical forms in, NULL on error):
    ``*`` and ``/`` by the exact UDF, ``+`` and ``-`` on DECIMAL(38,18) when the operands fit,
    by the UDF otherwise.  ``a`` and ``b`` are copied: they must be cheap."""
    if op in ("*", "/"):
        return f"sg_decimal_udf({a}, {b}, {sql_string(op)})"
    return (f"CASE WHEN sg_dec_fits({a}) AND sg_dec_fits({b}) THEN sg_arith_dec({a}, {b}, {sql_string(op)}) "
            f"ELSE sg_decimal_udf({a}, {b}, {sql_string(op)}) END")


_SIMPLE_SQL = re.compile(r"""^("[^"]+"|[A-Za-z_][A-Za-z0-9_.]*|-?[0-9.]+|'[^']*'|NULL)$""")
_LET_COUNTER = [0]


def let_sql(values, body, ctx=None) -> str:
    """``body`` applied to ``values`` (name -> CExpr), each value computed once.  DuckDB
    expands a macro argument at each of its occurrences (``sg_any``, ``lex``,
    ``sg_num_rank``... copy theirs), so nested operators that copied a compound operand
    would bind c^d copies of the innermost one.  The body receives expressions of the same
    types that are cheap to copy: the values themselves when they are constants or columns;
    else columns that the operator adds to its input relation before evaluating the
    expression (``ctx.hoist``, when the operator allows it: :meth:`Compiler.ensure_uid`
    adds them); else fields of a one-element ``list_transform`` that binds them (a lambda
    evaluates the branches of a CASE eagerly, and DuckDB binds the list of a lambda again
    for each enclosing lambda: the last resort)."""
    if all(_cheap(e) for e in values.values()) or any(has_raise(e.sql) for e in values.values()):
        # a RAISE is evaluated where the expression reaches it: neither hoisted into a
        # column of the operator's input nor bound by a lambda (which evaluates eagerly)
        return body(dict(values))
    if ctx is not None and getattr(ctx, "hoist_ok", False):
        return body({k: e if _cheap(e) else CExpr(ctx.hoist(e.sql), e.type) for k, e in values.items()})
    _LET_COUNTER[0] += 1
    w = f"__w{_LET_COUNTER[0]}"
    fields = ", ".join(f"'{k}': {e.sql}" for k, e in values.items())
    return f"list_transform([{{{fields}}}], lambda {w}: {body({k: CExpr(f'{w}.{k}', e.type) for k, e in values.items()})})[1]"


_RANK = {A.XSD_DOUBLE: 3, A.XSD + "float": 2, A.XSD_DECIMAL: 1}


def _const_number(e: CExpr):
    """(rank, value) of a constant number: rank 0 integer, 1 decimal, 2 float, 3 double."""
    from decimal import Decimal, InvalidOperation

    if not e.is_const or e.const is None:
        return None
    k = e.type.kind
    if k == "int":
        return 0, Decimal(int(e.const))
    if k == "double":
        return 3, float(e.const)
    if k == "typed" and e.type.dt in NUMERIC_DATATYPES:
        r = _RANK.get(e.type.dt, 0)
        try:
            return r, (float(e.const) if r >= 2 else Decimal(str(e.const)))
        except (ValueError, InvalidOperation):
            return None
    return None


def _fold_arith(op: str, a: CExpr, b: CExpr):
    """Constant arithmetic computed at compile time, with the promotion of ``sg_arith``."""
    x, y = _const_number(a), _const_number(b)
    if x is None or y is None:
        return None
    rank = max(x[0], y[0])
    if rank == 0 and op != "/":
        return const_int(int({"+": x[1] + y[1], "-": x[1] - y[1], "*": x[1] * y[1]}[op]))
    if rank <= 1:
        lexf = decimal_op(str(x[1]), str(y[1]), op)
        return NULL if lexf is None else CExpr(sql_string(lexf), TType("typed", dt=A.XSD_DECIMAL), lexf)
    return None  # float and double: computed by DuckDB, formatted by sg_double_str


def decimal_op(a: str, b: str, op: str):
    """Exact decimal arithmetic as Jena does it (BigDecimal): a quotient that does not
    terminate is rounded to 24 decimal places, half to even; canonical lexical form."""
    from decimal import Decimal, Inexact, InvalidOperation, ROUND_HALF_EVEN, localcontext

    if a is None or b is None:
        return None
    try:
        with localcontext() as c:
            c.prec = 1000
            x, y = Decimal(a), Decimal(b)
            if op == "+":
                r = x + y
            elif op == "-":
                r = x - y
            elif op == "*":
                r = x * y
            else:
                if y == 0:
                    return None
                c.clear_flags()
                r = x / y
                if c.flags[Inexact]:
                    r = r.quantize(Decimal(1).scaleb(-24), rounding=ROUND_HALF_EVEN)
    except (InvalidOperation, ArithmeticError):
        return None
    return decimal_lexical(r)


def decimal_lexical(r) -> str:
    """Jena's canonical decimal: no exponent, no trailing zero, ``.0`` for integers."""
    if r == 0:
        return "0.0"
    t = format(r.normalize(), "f")
    if "." not in t:
        return t + ".0"
    return t


def _numfn_sql(name: str, x: str) -> str:
    return {"ABS": f"abs({x})", "CEIL": f"ceil({x})", "FLOOR": f"floor({x})", "ROUND": f"floor({x} + 0.5)"}[name]


def _unify_branches(branches: List[CExpr]):
    """The common type of alternatives, and their SQL in that type; a RAISE alternative
    takes the type of the others (its value is never used)."""
    t = unify([b.type for b in branches if not b.is_null and not isinstance(b, RaiseExpr)])
    return t, [raise_as(b, t) if isinstance(b, RaiseExpr) else coerce(b, t) if not b.is_null else "NULL"
               for b in branches]


# -- RAISE ---------------------------------------------------------------------------------


class RaiseExpr(CExpr):
    """A compiled ``RAISE(message, value...)``: DuckDB's ``error()`` (macro ``sg_raise``),
    with the message and the values in its error message (:mod:`raising`).  Its static type
    is ``any``; as an alternative of ``IF`` or ``COALESCE``, it takes the type of the others.
    ``raw`` is the call, of SQL type NULL."""

    def __init__(self, raw: str):
        super().__init__(f"CAST({raw} AS {ANY_SQL_TYPE})", ANY_T)
        self.raw = raw


def raise_as(e: "RaiseExpr", t: TType) -> str:
    """The SQL of a RAISE in the SQL type of the term type ``t``."""
    return f"CAST({e.raw} AS {_SQL_TYPES[t.kind]})"


_SQL_TYPES = {"iri": "VARCHAR", "bnode": "VARCHAR", "str": "VARCHAR", "lang": "VARCHAR", "typed": "VARCHAR",
              "int": "BIGINT", "double": "DOUBLE", "bool": "BOOLEAN", "json": "JSON", "any": ANY_SQL_TYPE}


def has_raise(sql: str) -> bool:
    """The SQL evaluates a RAISE expression somewhere."""
    return RAISE_MACRO + "(" in sql


def compile_raise(call: A.Call, ctx) -> CExpr:
    """``RAISE(message, value...)``: the arguments are evaluated leniently (an unbound or
    erroneous one is undefined: JSON null), the message and the values are sent as
    N-Triples terms in the message of the error, after :data:`raising.MARKER`."""
    args = [compile_expr(a, ctx) for a in call.args]
    nts = [f"NULL::VARCHAR" if a.is_null else f"{to_nt(a, 'o')}::VARCHAR" for a in args]
    values = "[" + ", ".join(nts[1:]) + "]::VARCHAR[]"
    payload = f"({sql_string(MARKER)} || CAST(to_json({{'m': {nts[0]}, 'v': {values}}}) AS VARCHAR))"
    return RaiseExpr(f"{RAISE_MACRO}({payload})")


def conjunction(conds: List[str]) -> str:
    """The conditions of a FILTER list or of HAVING, all true; with a RAISE after the first
    one, evaluated in order up to the first that is not true (as Jena's filter does), not
    in the order DuckDB chooses."""
    if not any(has_raise(c) for c in conds[1:]):
        return " AND ".join(conds)
    return "(CASE " + " ".join(f"WHEN ({c}) IS NOT TRUE THEN FALSE" for c in conds) + " ELSE TRUE END)"


def _chain(call: A.Call, name: str) -> list:
    """The operands of a chain of ``||`` (or ``&&``) in the order of evaluation:
    ``(a || b) || c`` and ``a || (b || c)`` are ``[a, b, c]``."""
    out = []
    for arg in call.args:
        if isinstance(arg, A.Call) and arg.name == name and len(arg.args) == 2:
            out.extend(_chain(arg, name))
        else:
            out.append(arg)
    return out


def _lazy_chain(name: str, ops: List[str], raises: Optional[List[bool]] = None) -> str:
    """``a1 || ... || an`` evaluating the operands in order up to the first true one (``&&``:
    false one), as the Java engine does; else unbound (an error) if one of them is, else
    false (``&&``: true). An operand that is a RAISE (``raises``) is left out of the test for
    an unbound operand: that test is reached only after every operand was evaluated, and a
    RAISE evaluated has raised."""
    stop, other = ("TRUE", "FALSE") if name == "||" else ("FALSE", "TRUE")
    whens = " ".join(f"WHEN ({o}) IS {stop} THEN {stop}" for o in ops)
    nulls = " OR ".join(f"({o}) IS NULL" for i, o in enumerate(ops) if not (raises and raises[i]))
    if not nulls:
        return f"(CASE {whens} ELSE {other} END)"
    return f"(CASE {whens} WHEN {nulls} THEN NULL ELSE {other} END)"


# -- function calls -----------------------------------------------------------

_CASTS = {
    A.XSD_INTEGER, A.XSD_DECIMAL, A.XSD_DOUBLE, A.XSD + "float", A.XSD_STRING, A.XSD_BOOLEAN,
    A.XSD + "dateTime", A.XSD + "date", A.XSD + "time", A.XSD + "gYear", A.XSD + "gMonth", A.XSD + "gDay",
    A.XSD + "long", A.XSD + "int", A.XSD + "short", A.XSD + "byte", A.XSD + "nonNegativeInteger",
    A.XSD + "positiveInteger", A.XSD + "anyURI",
}


def _const_ebv(c: CExpr):
    """The effective boolean value of a constant, or None when it is not known statically."""
    if not c.is_const or c.const is None:
        return None
    k = c.type.kind
    if k == "bool":
        return bool(c.const)
    if k in ("int", "double"):
        return c.const != 0
    if c.type.is_stringy:
        return len(str(c.const)) > 0
    return None


def compile_call(call: A.Call, ctx) -> CExpr:
    name = call.name
    if name == "IF" and len(call.args) == 3:
        # a constant condition selects its branch at compile time; the other one is not
        # compiled (this ends the inline expansion of a recursive FUNCTION on constants)
        c = compile_expr(call.args[0], ctx)
        if c.is_null:
            return NULL
        v = _const_ebv(c)
        if v is not None:
            return compile_expr(call.args[1] if v else call.args[2], ctx)
    if name == "RAISE":
        return compile_raise(call, ctx)
    if name in ("||", "&&") and len(call.args) == 2:
        # a chain (a || b || c ...) is compiled once, as one operator
        compiled = [compile_expr(o, ctx) for o in _chain(call, name)]
        ops = [_ebv(o) for o in compiled]
        if any(has_raise(o) for o in ops[1:]):
            # DuckDB evaluates every operand of OR and AND: a RAISE is evaluated only when
            # the operands on its left do not decide (one CASE over the chain, each operand
            # copied twice; nested binary CASEs would copy the left operand at each level)
            return CExpr(_lazy_chain(name, ops, [isinstance(o, RaiseExpr) for o in compiled]), BOOL_T)
        return CExpr("(" + f" {'OR' if name == '||' else 'AND'} ".join(ops) + ")", BOOL_T)
    args = [compile_expr(a, ctx) for a in call.args]
    if name == "!":
        return CExpr(f"(NOT {_ebv(args[0])})", BOOL_T)
    if name in ("=", "!=", "<", ">", "<=", ">="):
        return _compare(name, args[0], args[1])
    if name in ("+", "-", "*", "/"):
        r = _arith(name, args[0], args[1], ctx)
        if "sg_decimal_udf(" in r.sql:
            ctx.engine.ensure_decimal_udf()
        return r
    if name == "unary-":
        n = numeric(args[0])
        if n == "NULL":
            return NULL
        if args[0].type.kind in ("int", "double"):
            return CExpr(f"(-{n})", args[0].type)
        r = _arith("-", const_int(0), args[0], ctx)
        if "sg_decimal_udf(" in r.sql:
            ctx.engine.ensure_decimal_udf()
        return r
    if name == "unary+":
        n = numeric(args[0])
        return NULL if n == "NULL" else args[0]
    if name in ("IN", "NOT IN"):
        tests = [_compare("=", args[0], a) for a in args[1:]]
        if not tests:
            return const_bool(name == "NOT IN")
        sql = "(" + " OR ".join(t.sql for t in tests) + ")"
        if any(has_raise(t.sql) for t in tests[1:]):
            # the members in order, up to the first one that is equal (as Jena's E_OneOf)
            sql = "(CASE " + " ".join(f"WHEN ({t.sql}) IS TRUE THEN TRUE" for t in tests) + f" ELSE {sql} END)"
        return CExpr(sql if name == "IN" else f"(NOT {sql})", BOOL_T)
    if name == "BOUND":
        e = args[0]
        if e.is_const:
            return const_bool(e.const is not None)
        return CExpr(f"({e.sql} IS NOT NULL)", BOOL_T)
    if name == "COALESCE":
        bs = [a for a in args if not a.is_null]
        if not bs:
            return NULL
        if bs[0].is_const:
            return bs[0]
        t, sqls = _unify_branches(bs)
        return CExpr("COALESCE(" + ", ".join(sqls) + ")", t)
    if name == "IF":
        c, a, b = args
        ec = _ebv(c)
        t, (sa, sb) = _unify_branches([a, b])
        return CExpr(f"(CASE WHEN {ec} IS NULL THEN NULL WHEN {ec} THEN {sa} ELSE {sb} END)", t)
    if name == "STR":
        return _str(args[0])
    if name in ("IRI", "URI"):
        return _iri(args[0], ctx)
    if name == "ENCODE_FOR_URI":
        return _encode_for_uri(args[0])
    if name == "CONCAT":
        return _concat(args)
    if name == "STRDT":
        return _strdt(args[0], args[1])
    if name == "STRLANG":
        return _strlang(args[0], args[1])
    if name == "LANG":
        e = args[0]
        if e.type.kind == "lang":
            return const_str(e.type.lang)
        if e.type.is_literal:
            return const_str("")
        if e.type.kind == "any":
            return CExpr(f"sg_any_lang({e.sql})", STR_T)
        return NULL
    if name == "LANGMATCHES":
        a, b = args
        la, lb = string_literal_lex(a), string_literal_lex(b)
        return CExpr(f"sg_langmatches({la}, {lb})", BOOL_T)
    if name == "DATATYPE":
        e = args[0]
        if e.type.kind in ("iri", "bnode"):
            return NULL
        d = datatype_sql(e)
        if d.startswith("'"):
            return const_iri(d[1:-1].replace("''", "'"))
        return CExpr(d, IRI_T)
    if name == "BNODE":
        return ctx.bnode_call(args)
    if name in ("ISIRI", "ISURI", "ISBLANK", "ISLITERAL", "ISNUMERIC"):
        e = args[0]
        k = e.type.kind
        if k == "any":
            test = {"ISIRI": "k = 0", "ISURI": "k = 0", "ISBLANK": "k = 1", "ISLITERAL": "k = 2"}.get(name)
            if test:
                return CExpr(f"(({e.sql}).{test})", BOOL_T)
            return CExpr(f"(sg_any_num({e.sql}) IS NOT NULL)", BOOL_T)
        if k == "json":
            if name == "ISLITERAL":
                return CExpr(f"({e.sql} IS NOT NULL)", BOOL_T)
            if name == "ISNUMERIC":
                return CExpr(f"(sg_json_num({e.sql}) IS NOT NULL)", BOOL_T)
            return CExpr(f"CASE WHEN {e.sql} IS NULL THEN NULL ELSE FALSE END", BOOL_T)
        val = {"ISIRI": k == "iri", "ISURI": k == "iri", "ISBLANK": k == "bnode",
               "ISLITERAL": e.type.is_literal, "ISNUMERIC": e.type.is_numeric}[name]
        if e.is_const:
            return NULL if e.is_null else const_bool(val)
        return CExpr(f"CASE WHEN {e.sql} IS NULL THEN NULL ELSE {'TRUE' if val else 'FALSE'} END", BOOL_T)
    if name == "SAMETERM":
        a, b = args
        return CExpr(f"sg_any_eq({to_any(a)}, {to_any(b)})", BOOL_T)
    if name in ("STRLEN", "UCASE", "LCASE", "CONTAINS", "STRSTARTS", "STRENDS", "STRBEFORE", "STRAFTER",
                "SUBSTR", "REPLACE", "REGEX", "MD5", "SHA1", "SHA256", "SHA384", "SHA512", "SHA224"):
        return _string_function(name, args)
    if name in ("ABS", "CEIL", "FLOOR", "ROUND"):
        # XPath: the result has the type of the argument; ROUND rounds halves towards +INF
        e = args[0]
        n = numeric(e)
        if n == "NULL":
            return NULL
        if e.type.kind == "int":
            return CExpr(f"abs({e.sql})", INT_T) if name == "ABS" else e
        if e.type.kind == "double":
            return CExpr(_numfn_sql(name, e.sql), DOUBLE_T)
        call = let_sql({"a": e}, lambda v: f"sg_numfn({to_any(v['a'])}, {sql_string(name)})", ctx)
        if _static_rank(e.type) is not None:
            return CExpr(f"({call}).v", e.type)
        return CExpr(call, ANY_T)
    if name == "RAND":
        return CExpr("random()", DOUBLE_T)
    if name == "NOW":
        return CExpr("strftime(now(), '%Y-%m-%dT%H:%M:%S%z')", DATETIME_T)
    if name == "UUID":
        return CExpr("('urn:uuid:' || CAST(uuid() AS VARCHAR))", IRI_T)
    if name == "STRUUID":
        return CExpr("CAST(uuid() AS VARCHAR)", STR_T)
    if name in ("YEAR", "MONTH", "DAY", "HOURS", "MINUTES", "SECONDS", "TZ", "TIMEZONE"):
        e = args[0]
        s = lex(e)
        ts = f"TRY_CAST(regexp_replace({s}, 'Z$|[+-][0-9][0-9]:[0-9][0-9]$', '') AS TIMESTAMP)"
        if name == "TZ":
            return CExpr(f"COALESCE(regexp_extract({s}, '(Z|[+-][0-9][0-9]:[0-9][0-9])$', 1), '')", STR_T)
        if name == "TIMEZONE":
            return CExpr(f"sg_timezone({s})", TType("typed", dt=A.XSD + "dayTimeDuration"))
        f = {"YEAR": "year", "MONTH": "month", "DAY": "day", "HOURS": "hour", "MINUTES": "minute"}.get(name)
        if name == "SECONDS":
            return CExpr(f"sg_decimal_fmt(CAST(second({ts}) + microsecond({ts}) % 1000000 / 1000000.0 AS VARCHAR))", TType("typed", dt=A.XSD_DECIMAL))
        return CExpr(f"{f}({ts})", INT_T)
    if name in _CASTS:
        return _cast(name, args[0])
    # custom functions
    fn = ctx.functions.get(name)
    if fn is not None:
        return fn(ctx, args)
    override = getattr(ctx, "function_override", None)
    if override and name in override:
        return override[name](args)
    lib = ctx.compiler.library_function(name)
    if lib is None:
        raise CompileError(f"unknown function <{name}>")
    return _call_function(name, lib, args, ctx)


MAX_FUNCTION_DEPTH = 64


def _call_function(name: str, lib, args: List[CExpr], ctx) -> CExpr:
    """A call of a FUNCTION of the library (rule K for functions), expanded inline: the
    body is compiled with the parameters bound to the arguments (call by value, the body
    sees its parameters only).  A call whose constant arguments are those of a pending
    call is unbound (tabling); a recursive call on run-time values is refused (its
    inline expansion would not terminate), and constant recursion stops at
    ``MAX_FUNCTION_DEPTH``."""
    sig, body, fbase = lib
    if len(args) != len(sig):
        return NULL  # wrong arity
    stack = ctx.compiler.function_stack
    key = (name, tuple((a.type, a.const) for a in args)) if all(a.is_const for a in args) else None
    if key is not None and key in stack:
        return NULL
    if key is None and any(k[0] == name for k in stack):
        raise _RuntimeRecursion(name)
    if sum(1 for k in stack if k[0] == name) >= MAX_FUNCTION_DEPTH:
        raise CompileError(f"FUNCTION <{name}>: recursion deeper than {MAX_FUNCTION_DEPTH}")
    saved, saved_base = ctx.scope, ctx.base
    ctx.scope = {p.name: a for p, a in zip(sig, args)}
    ctx.base = fbase  # the body against the base of the function, its arguments against the caller's
    stack.append(key or (name, None))
    try:
        return compile_expr(body, ctx)
    except _RuntimeRecursion as ex:
        if ex.name != name or any(k[0] == name for k in stack[:-1]):
            raise  # an enclosing expansion of the same function handles it
        ctx.scope = saved
        return _recursive_call(name, sig, body, args, ctx)
    finally:
        ctx.scope, ctx.base = saved, saved_base
        stack.pop()


class _RuntimeRecursion(Exception):
    """A FUNCTION calls itself on values known only at run time: its inline expansion would
    not terminate (it duplicates the arguments' SQL at each level)."""

    def __init__(self, name):
        super().__init__(f"FUNCTION <{name}>: recursion on values known only at run time")
        self.name = name


MAX_RECURSION_LEVELS = 10000


def _recursive_call(name: str, sig, body, args: List[CExpr], ctx) -> CExpr:
    """``u(args)`` for a FUNCTION ``u`` that calls itself on run-time values, evaluated
    iteratively on the argument tuples of the rows of the operator (rule K for functions,
    with tabling).  Top-down, the calls are demanded level by level: level 0 is the table
    of the distinct (arguments, chain) of the rows, the chain being the string of the keys
    of the calls pending on them; level k+1 holds, for each recursive call site of the
    body, the arguments of the rows of level k where the call is reached (the conditions
    of the ``IF`` and ``COALESCE`` branches that lead to it), minus those pending on the
    chain (such a call is unbound).  Bottom-up, the value of each level is the body
    evaluated on its rows, a recursive call being looked up in the next level (unbound when
    absent).  The call is a lookup in level 0.  Values are in the dynamic representation,
    keys are their N-Triples strings."""
    from .compiler import Rel

    engine = ctx.engine
    rel = getattr(ctx, "operator_rel", None)
    if rel is None or not getattr(ctx, "hoist_ok", False) or getattr(ctx, "uid_pending", False):
        raise CompileError(f"FUNCTION <{name}>: a recursion on values known only at run time is only "
                           "supported in BIND, FILTER and the clauses before the solutions are written")
    k = len(sig)

    def wrap(src, lets):
        for col, sql in lets:
            src = f"(SELECT *, {sql} AS {col} FROM {src})"
        return src

    def written(src, lets):
        """``src`` with its hoisted columns, each written to a temporary table: the tables of
        the levels are small, and DuckDB's optimizer, given the nested projections and a
        correlated lookup, inlines the columns into one another (the memory of the planner
        grows exponentially with the nesting)."""
        for col, sql in lets:
            t = engine.fresh("fnh")
            engine.execute(f"CREATE TEMP TABLE {t} AS SELECT *, {sql} AS {col} FROM {src}")
            src = t
        return src

    def any_of(a: CExpr) -> str:
        return to_any(a) if not a.is_null else "NULL::" + ANY_SQL_TYPE

    def key_of(ks):
        return f"concat_ws(chr(31), {sql_string(name)}{''.join(f', COALESCE({x}, chr(0))' for x in ks)})"

    def key_col(a: CExpr) -> str:
        """The N-Triples key of an argument, a column of the operator's relation (the
        condition of the correlated lookup stays a column comparison)."""
        if not ctx.hoist_ok:
            return f"sg_any_nt({any_of(a)}, 'o')"
        v = any_of(a)
        if not _cheap(CExpr(v, ANY_T)):
            v = ctx.hoist(v)  # sg_any_nt copies its argument at each of its occurrences
        return ctx.hoist(f"sg_any_nt({v}, 'o')")

    akeys = ", ".join(f'"__k{i}"' for i in range(k))
    avals = ", ".join(f'any_value("__a{i}") AS "__a{i}"' for i in range(k))
    anys = [f'sg_any_nt("__a{i}", \'o\') AS "__k{i}"' for i in range(k)]

    def distinct_table(inner: str) -> str:
        """The distinct (arguments, chain) of ``inner`` (columns ``__a{i}``, ``__chain``)."""
        t = engine.fresh("fnl")
        cols = (avals + ", " if k else "") + (akeys + ", " if k else "") + "__chain"
        engine.execute(f"CREATE TEMP TABLE {t} AS SELECT {cols} FROM (SELECT *{''.join(', ' + x for x in anys)} "
                       f"FROM {inner}) GROUP BY {(akeys + ', ') if k else ''}__chain")
        return t

    # level 0: the distinct argument tuples of the rows of the operator
    src = wrap(rel.sql if not rel.trivial else "(SELECT 1 AS __one)", ctx.lets)
    sel = [f"{any_of(a)} AS \"__a{i}\"" for i, a in enumerate(args)] + ["chr(30) AS __chain"]
    levels = [distinct_table(f"(SELECT {', '.join(sel)} FROM {src})")]
    saved = (ctx.scope, getattr(ctx, "function_override", None), ctx.hoist_ok, ctx.lets, ctx.uid_col,
             ctx.uid_pending, ctx.operator_rel)
    own = key_of([f'"__q{i}"' for i in range(k)])

    def base(table: str) -> str:
        """A level, its columns renamed apart from those of the tables it looks up."""
        cols = [f'"__a{i}" AS "__p{i}"' for i in range(k)] + [f'"__k{i}" AS "__q{i}"' for i in range(k)]
        return f"(SELECT {', '.join(cols + ['__chain AS __pc'])} FROM {table})"

    def on_level(table: str):
        """Compile the next expressions as one operator on the rows of ``table``."""
        b = base(table)
        ctx.begin_operator(rel=Rel(b))
        ctx.scope = {p.name: CExpr(f'"__p{i}"', ANY_T) for i, p in enumerate(sig)}
        return b

    try:
        # top-down: the demanded calls, level by level
        while True:
            if len(levels) > MAX_RECURSION_LEVELS:
                raise CompileError(f"FUNCTION <{name}>: recursion deeper than {MAX_RECURSION_LEVELS} levels")
            b = on_level(levels[-1])
            sites = _recursive_sites(name, body, ctx)
            if not sites:
                break
            src = written(b, ctx.lets)
            parts = []
            for guard, sargs in sites:
                cols = [f"{any_of(a)} AS \"__a{i}\"" for i, a in enumerate(sargs)]
                parts.append(f"SELECT {', '.join(cols + ['(__pc || ' + own + ' || chr(30)) AS __chain'])} FROM {src} WHERE {guard}")
            ks = [f'sg_any_nt("__a{i}", \'o\')' for i in range(k)]
            live = f"NOT contains(__chain, chr(30) || {key_of(ks)} || chr(30))"
            nxt = distinct_table(f"(SELECT * FROM ({' UNION ALL '.join(parts)}) WHERE {live})")
            if engine.execute(f"SELECT count(*) FROM {nxt}").fetchall()[0][0] == 0:
                break
            levels.append(nxt)
        # bottom-up: the value of each level, a recursive call looked up in the next level
        values = None
        for cur in reversed(levels):
            nxt_vals = values

            def lookup(cargs, nxt_vals=nxt_vals):
                if len(cargs) != k or nxt_vals is None:
                    return CExpr("NULL", ANY_T)
                conds = [f"__n.\"__k{i}\" IS NOT DISTINCT FROM {key_col(a)}" for i, a in enumerate(cargs)]
                conds.append(f"__n.__chain = {ctx.hoist('(__pc || ' + own + ' || chr(30))')}")
                return CExpr(f"(SELECT __n.__v FROM {nxt_vals} AS __n WHERE {' AND '.join(conds)})", ANY_T)

            b = on_level(cur)
            ctx.function_override = {**(saved[1] or {}), name: lookup}
            v = compile_expr(body, ctx)
            if "sg_decimal_udf(" in v.sql:
                engine.ensure_decimal_udf()
            values = engine.fresh("fnv")
            kept = ", ".join([f'"__q{i}" AS "__k{i}"' for i in range(k)] + ["__pc AS __chain"])
            engine.execute(f"CREATE TEMP TABLE {values} AS SELECT {kept}, {any_of(v)} AS __v FROM {written(b, ctx.lets)}")
    finally:
        (ctx.scope, ctx.function_override, ctx.hoist_ok, ctx.lets, ctx.uid_col, ctx.uid_pending,
         ctx.operator_rel) = saved
    conds = [f"__r.\"__k{i}\" IS NOT DISTINCT FROM {key_col(a)}" for i, a in enumerate(args)]
    conds.append("__r.__chain = chr(30)")
    return CExpr(f"(SELECT __r.__v FROM {values} AS __r WHERE {' AND '.join(conds)})", ANY_T)


def _recursive_sites(name: str, body, ctx):
    """The recursive call sites of a FUNCTION body: (SQL condition under which the call is
    evaluated, compiled arguments).  ``IF(c, a, b)`` reaches ``a`` when ``c`` is true and
    ``b`` when it is false; ``COALESCE(x, y)`` reaches ``y`` when ``x`` is unbound; a
    condition that itself contains a recursive call is not known before the call: the
    branch is reached unconditionally (the demand is over-approximated)."""
    sites = []

    def has_call(n):
        if isinstance(n, A.Call):
            return n.name == name or any(has_call(a) for a in n.args)
        return False

    def cond_sql(n):
        if has_call(n):
            return None
        return compile_expr(n, ctx)

    def walk(n, guard):
        if not isinstance(n, A.Call):
            return
        if n.name == name:
            if any(has_call(a) for a in n.args):
                raise CompileError(f"FUNCTION <{name}>: a recursive call in the arguments of a recursive call "
                                   "on run-time values is not supported")
            sites.append((" AND ".join(guard) or "TRUE", [compile_expr(a, ctx) for a in n.args]))
            return
        if n.name == "IF" and len(n.args) == 3:
            walk(n.args[0], guard)
            c = cond_sql(n.args[0])
            e = _ebv(c) if c is not None and not c.is_null else None
            walk(n.args[1], guard + ([f"({e}) IS TRUE"] if e else []))
            walk(n.args[2], guard + ([f"({e}) IS FALSE"] if e else []))
            return
        if n.name == "COALESCE":
            g = list(guard)
            for a in n.args:
                walk(a, g)
                c = cond_sql(a)
                if c is not None:
                    g = g + [f"({c.sql}) IS NULL" if not c.is_null else "TRUE"]
            return
        for a in n.args:
            walk(a, guard)

    walk(body, [])
    return sites


def _string_function(name: str, args: List[CExpr]) -> CExpr:
    if any(a.is_null for a in args):
        return NULL
    s = [string_literal_lex(a) for a in args]
    if name in ("MD5", "SHA1", "SHA256", "SHA384", "SHA512", "SHA224"):
        if s[0] == "NULL":
            return NULL
        f = {"MD5": "md5", "SHA1": "sha1", "SHA256": "sha256"}.get(name)
        if f is None:
            return CExpr(f"sg_hash({sql_string(name.lower())}, {s[0]})", STR_T)
        return CExpr(f"{f}({s[0]})", STR_T)
    if name == "STRLEN":
        return NULL if s[0] == "NULL" else CExpr(f"length({s[0]})", INT_T)
    if name in ("UCASE", "LCASE"):
        if s[0] == "NULL":
            return NULL
        t = args[0].type if args[0].type.kind in ("str", "lang") else STR_T
        return CExpr(f"{'upper' if name == 'UCASE' else 'lower'}({s[0]})", t)
    if name in ("CONTAINS", "STRSTARTS", "STRENDS"):
        if s[0] == "NULL" or s[1] == "NULL":
            return NULL
        f = {"CONTAINS": "contains", "STRSTARTS": "starts_with", "STRENDS": "ends_with"}[name]
        return CExpr(f"{f}({s[0]}, {s[1]})", BOOL_T)
    if name in ("STRBEFORE", "STRAFTER"):
        if s[0] == "NULL" or s[1] == "NULL":
            return NULL
        t = args[0].type if args[0].type.kind in ("str", "lang") else STR_T
        if name == "STRBEFORE":
            sql = f"CASE WHEN {s[1]} = '' THEN '' WHEN strpos({s[0]}, {s[1]}) > 0 THEN substr({s[0]}, 1, strpos({s[0]}, {s[1]}) - 1) ELSE '' END"
        else:
            sql = f"CASE WHEN {s[1]} = '' THEN {s[0]} WHEN strpos({s[0]}, {s[1]}) > 0 THEN substr({s[0]}, strpos({s[0]}, {s[1]}) + length({s[1]})) ELSE '' END"
        return CExpr(sql, t)
    if name == "SUBSTR":
        if s[0] == "NULL":
            return NULL
        start = numeric(args[1])
        t = args[0].type if args[0].type.kind in ("str", "lang") else STR_T
        if len(args) == 3:
            return CExpr(f"substr({s[0]}, CAST(round({start}) AS BIGINT), CAST(round({numeric(args[2])}) AS BIGINT))", t)
        return CExpr(f"substr({s[0]}, CAST(round({start}) AS BIGINT))", t)
    if name == "REPLACE":
        if s[0] == "NULL" or s[1] == "NULL" or s[2] == "NULL":
            return NULL
        flags = "'g'"
        if len(args) == 4:
            f = _regex_flags(args[3], s[3])
            flags = sql_string("g" + f) if f is not None else f"('g' || {s[3]})"
        pat = _xpath_regex(s[1])
        rep = _xpath_replacement(s[2])
        t = args[0].type if args[0].type.kind in ("str", "lang") else STR_T
        return CExpr(f"regexp_replace({s[0]}, {pat}, {rep}, {flags})", t)
    if name == "REGEX":
        if s[0] == "NULL" or s[1] == "NULL":
            return NULL
        pat = _xpath_regex(s[1])
        if len(args) == 3:
            f = _regex_flags(args[2], s[2])
            opts = sql_string(f) if f is not None else s[2]
            return CExpr(f"regexp_matches({s[0]}, {pat}, {opts})", BOOL_T)
        return CExpr(f"regexp_matches({s[0]}, {pat})", BOOL_T)
    raise CompileError(name)


def _regex_flags(arg: CExpr, sql: str):
    """The DuckDB options of constant XPath regular-expression flags, or None for flags
    known only at run time (passed as they are).  The flag ``q`` (the pattern is a literal
    string) is RE2's ``l``; as in Jena, the replacement keeps its ``$n`` and ``\\``
    escapes; the other flags are passed as they are."""
    if not arg.is_const or arg.const is None:
        return None
    return str(arg.const).replace("q", "l")


def _xpath_regex(pat_sql: str) -> str:
    return pat_sql


def _xpath_replacement(rep_sql: str) -> str:
    """A replacement string of ``REPLACE`` (Java's ``Matcher.replaceAll``, as Jena: ``$n``
    the n-th group, ``\\c`` the character ``c``) as an RE2 one (``\\n`` the n-th group,
    ``\\\\`` a backslash)."""
    if rep_sql.startswith("'") and rep_sql.endswith("'"):
        return sql_string(java_replacement_to_re2(rep_sql[1:-1].replace("''", "'")))
    # at run time: escaped backslashes and dollars set aside (\x01, \x02), the other escapes
    # removed, the groups renamed, then the literal backslashes and dollars put back
    r = f"replace(replace({rep_sql}, '\\\\', chr(1)), '\\$', chr(2))"
    r = f"regexp_replace({r}, '\\\\(.)', '\\1', 'g')"
    r = f"regexp_replace({r}, '\\$([0-9])', '\\\\\\1', 'g')"
    return f"replace(replace({r}, chr(1), '\\\\'), chr(2), '$')"


def java_replacement_to_re2(s: str) -> str:
    out = []
    i = 0
    while i < len(s):
        c = s[i]
        if c == "\\" and i + 1 < len(s):
            out.append("\\\\" if s[i + 1] == "\\" else s[i + 1])
            i += 2
        elif c == "$" and i + 1 < len(s) and s[i + 1].isdigit():
            out.append("\\" + s[i + 1])
            i += 2
        else:
            out.append("\\\\" if c == "\\" else c)
            i += 1
    return "".join(out)


_INT_RE = "^[+-]?[0-9]+$"
_DEC_RE = r"^[+-]?([0-9]+(\.[0-9]*)?|\.[0-9]+)$"
_DBL_RE = r"^[+-]?(([0-9]+(\.[0-9]*)?|\.[0-9]+)([eE][+-]?[0-9]+)?|INF|NaN)$"


def _cast(dt: str, e: CExpr) -> CExpr:
    if e.is_null:
        return NULL
    t = e.type
    if dt == A.XSD_STRING:
        if t.kind == "double":  # Jena: the value as Double.toString, without the e0 of its lexical form
            return CExpr(f"sg_jdouble({e.sql})", STR_T)
        return _str(e)
    if dt == A.XSD_INTEGER or dt in (A.XSD + "long", A.XSD + "int", A.XSD + "short", A.XSD + "byte",
                                      A.XSD + "nonNegativeInteger", A.XSD + "positiveInteger"):
        if t.kind == "int":
            return e
        if t.kind == "double":
            return CExpr(f"CAST(trunc({e.sql}) AS BIGINT)", INT_T)
        if t.kind == "bool":
            return CExpr(f"CASE WHEN {e.sql} THEN 1 ELSE 0 END", INT_T)
        if t.kind in ("json", "any") or t.is_numeric:
            n = numeric(e)
            s = lex(e)
            return CExpr(f"CASE WHEN regexp_matches({s}, '{_INT_RE}') THEN CAST({s} AS BIGINT) WHEN {n} IS NOT NULL THEN CAST(trunc({n}) AS BIGINT) END", INT_T)
        s = lex(e)
        if e.is_const:
            return const_int(int(e.const)) if re.match(_INT_RE, str(e.const)) else NULL
        return CExpr(f"CASE WHEN regexp_matches({s}, '{_INT_RE}') THEN CAST({s} AS BIGINT) END", INT_T)
    if dt == A.XSD_DECIMAL:
        if t.kind == "int":
            return CExpr(lex(e), TType("typed", dt=A.XSD_DECIMAL), e.const if e.is_const else NOCONST)
        if t.kind == "double":
            return CExpr(f"sg_decimal_fmt(CAST({e.sql} AS VARCHAR))", TType("typed", dt=A.XSD_DECIMAL))
        if t.kind == "bool":
            return CExpr(f"CASE WHEN {e.sql} THEN '1' ELSE '0' END", TType("typed", dt=A.XSD_DECIMAL))
        s = lex(e)
        if t.kind == "json":
            return CExpr(f"CASE WHEN json_type({e.sql}) = 'DOUBLE' THEN sg_decimal_fmt(CAST(CAST({e.sql} AS DOUBLE) AS VARCHAR)) WHEN regexp_matches({s}, '{_DEC_RE}') THEN {s} END", TType("typed", dt=A.XSD_DECIMAL))
        return CExpr(f"CASE WHEN regexp_matches({s}, '{_DEC_RE}') THEN {s} END", TType("typed", dt=A.XSD_DECIMAL))
    if dt in (A.XSD_DOUBLE, A.XSD + "float"):
        target = DOUBLE_T if dt == A.XSD_DOUBLE else TType("typed", dt=dt)
        if t.kind in ("int", "double"):
            return CExpr(f"CAST({e.sql} AS DOUBLE)", DOUBLE_T) if dt == A.XSD_DOUBLE else CExpr(lex(e), target)
        s = lex(e)
        # a valid lexical form is kept (Jena: xsd:double("3") is "3"^^xsd:double)
        return CExpr(f"CASE WHEN regexp_matches({s}, '{_DBL_RE}') THEN {s} END", TType("typed", dt=dt))
    if dt == A.XSD_BOOLEAN:
        if t.kind == "bool":
            return e
        if t.kind in ("int", "double"):
            return CExpr(f"({e.sql} <> 0)", BOOL_T)
        s = lex(e)
        return CExpr(f"CASE {s} WHEN 'true' THEN TRUE WHEN '1' THEN TRUE WHEN 'false' THEN FALSE WHEN '0' THEN FALSE END", BOOL_T)
    if dt == A.XSD + "dateTime":
        s = lex(e)
        return CExpr(f"CASE WHEN regexp_matches({s}, '^-?[0-9]{{4,}}-[0-9]{{2}}-[0-9]{{2}}T[0-9]{{2}}:[0-9]{{2}}:[0-9]{{2}}(\\.[0-9]+)?(Z|[+-][0-9]{{2}}:[0-9]{{2}})?$') THEN {s} END", DATETIME_T)
    if dt == A.XSD + "date":
        s = lex(e)
        return CExpr(f"CASE WHEN regexp_matches({s}, '^-?[0-9]{{4,}}-[0-9]{{2}}-[0-9]{{2}}(Z|[+-][0-9]{{2}}:[0-9]{{2}})?$') THEN {s} END", TType("typed", dt=dt))
    # other casts: keep the lexical form with the datatype (as Jena does when valid)
    return CExpr(lex(e), TType("typed", dt=dt), e.const if e.is_const else NOCONST)


EXTRA_SUPPORT_SQL = f"""
CREATE OR REPLACE MACRO {RAISE_MACRO}(m) AS error(m);
CREATE OR REPLACE MACRO sg_num_rank(x) AS CASE WHEN x.k <> 2 THEN NULL
    WHEN x.d = 'http://www.w3.org/2001/XMLSchema#double' THEN 3 WHEN x.d = 'http://www.w3.org/2001/XMLSchema#float' THEN 2 WHEN x.d = 'http://www.w3.org/2001/XMLSchema#decimal' THEN 1
    WHEN x.d IN ('http://www.w3.org/2001/XMLSchema#byte', 'http://www.w3.org/2001/XMLSchema#int', 'http://www.w3.org/2001/XMLSchema#integer', 'http://www.w3.org/2001/XMLSchema#long', 'http://www.w3.org/2001/XMLSchema#negativeInteger', 'http://www.w3.org/2001/XMLSchema#nonNegativeInteger', 'http://www.w3.org/2001/XMLSchema#nonPositiveInteger', 'http://www.w3.org/2001/XMLSchema#positiveInteger', 'http://www.w3.org/2001/XMLSchema#short', 'http://www.w3.org/2001/XMLSchema#unsignedByte', 'http://www.w3.org/2001/XMLSchema#unsignedInt', 'http://www.w3.org/2001/XMLSchema#unsignedLong', 'http://www.w3.org/2001/XMLSchema#unsignedShort') THEN 0 ELSE NULL END;
CREATE OR REPLACE MACRO sg_decimal_trim(s) AS CASE WHEN s IS NULL THEN NULL
    WHEN contains(s, '.') THEN regexp_replace(rtrim(s, '0'), '[.]$', '.0') ELSE s || '.0' END;
CREATE OR REPLACE MACRO sg_decimal_fmt(s) AS CASE WHEN contains(s, 'e')
    THEN sg_decimal_trim(CAST(CAST(CAST(s AS DOUBLE) AS DECIMAL(38,18)) AS VARCHAR)) ELSE sg_decimal_trim(s) END;
CREATE OR REPLACE MACRO sg_arith_int(x, y, op) AS CASE op WHEN '+' THEN x + y WHEN '-' THEN x - y WHEN '*' THEN x * y END;
CREATE OR REPLACE MACRO sg_arith_dec(x, y, op) AS CASE op
    WHEN '+' THEN sg_decimal_fmt(CAST(TRY_CAST(x AS DECIMAL(38,18)) + TRY_CAST(y AS DECIMAL(38,18)) AS VARCHAR))
    WHEN '-' THEN sg_decimal_fmt(CAST(TRY_CAST(x AS DECIMAL(38,18)) - TRY_CAST(y AS DECIMAL(38,18)) AS VARCHAR))
    ELSE NULL END;
CREATE OR REPLACE MACRO sg_arith_dbl(x, y, op) AS CASE op WHEN '+' THEN x + y WHEN '-' THEN x - y WHEN '*' THEN x * y
    WHEN '/' THEN CASE WHEN y <> 0 THEN x / y END END;
CREATE OR REPLACE MACRO sg_numfn_d(x, f) AS CASE f WHEN 'ABS' THEN abs(x) WHEN 'CEIL' THEN ceil(x)
    WHEN 'FLOOR' THEN floor(x) ELSE floor(x + 0.5) END;
CREATE OR REPLACE MACRO sg_dec_int(neg, ip, fr, f) AS CASE
    WHEN f = 'FLOOR' THEN CASE WHEN neg THEN -(ip + CASE WHEN fr <> '' THEN 1 ELSE 0 END) ELSE ip END
    WHEN f = 'CEIL' THEN CASE WHEN neg THEN -ip ELSE ip + CASE WHEN fr <> '' THEN 1 ELSE 0 END END
    ELSE CASE WHEN neg THEN -(ip + CASE WHEN fr > '5' THEN 1 ELSE 0 END) ELSE ip + CASE WHEN fr >= '5' THEN 1 ELSE 0 END END END;
CREATE OR REPLACE MACRO sg_dec_numfn(s, f) AS CASE WHEN NOT regexp_full_match(s, '[+-]?([0-9]+([.][0-9]*)?|[.][0-9]+)') THEN NULL
    WHEN f = 'ABS' THEN sg_decimal_trim(regexp_replace(regexp_replace(ltrim(s, '+-'), '^0+([0-9])', '\\1'), '^[.]', '0.'))
    ELSE CAST(sg_dec_int(starts_with(s, '-'), TRY_CAST(COALESCE(NULLIF(split_part(ltrim(s, '+-'), '.', 1), ''), '0') AS HUGEINT),
                         rtrim(split_part(s, '.', 2), '0'), f) AS VARCHAR) || '.0' END;
CREATE OR REPLACE MACRO sg_numfn(a, f) AS CASE WHEN a IS NULL OR sg_num_rank(a) IS NULL THEN NULL
    WHEN sg_num_rank(a) = 0 THEN sg_any(2, CAST(CASE WHEN f = 'ABS' THEN abs(TRY_CAST(a.v AS HUGEINT)) ELSE TRY_CAST(a.v AS HUGEINT) END AS VARCHAR), a.d)
    WHEN sg_num_rank(a) = 1 THEN sg_any(2, sg_dec_numfn(a.v, f), a.d)
    WHEN sg_num_rank(a) = 2 THEN sg_any(2, sg_jfloat(sg_numfn_d(TRY_CAST(a.v AS DOUBLE), f)), a.d)
    ELSE sg_any(2, sg_double_str(sg_numfn_d(TRY_CAST(a.v AS DOUBLE), f)), a.d) END;
CREATE OR REPLACE MACRO sg_dec_fits(s) AS length(split_part(ltrim(s, '+-'), '.', 1)) <= 19 AND length(split_part(s, '.', 2)) <= 18;
CREATE OR REPLACE MACRO sg_arith(a, b, op) AS CASE
    WHEN a IS NULL OR b IS NULL OR sg_num_rank(a) IS NULL OR sg_num_rank(b) IS NULL THEN NULL
    WHEN greatest(sg_num_rank(a), sg_num_rank(b)) = 0 AND op <> '/' THEN
        sg_any(2, CAST(sg_arith_int(TRY_CAST(a.v AS HUGEINT), TRY_CAST(b.v AS HUGEINT), op) AS VARCHAR), 'http://www.w3.org/2001/XMLSchema#integer')
    WHEN greatest(sg_num_rank(a), sg_num_rank(b)) <= 1 THEN
        sg_any(2, sg_arith_dec(a.v, b.v, op), 'http://www.w3.org/2001/XMLSchema#decimal')
    WHEN greatest(sg_num_rank(a), sg_num_rank(b)) = 2 THEN
        sg_any(2, sg_jfloat(sg_arith_dbl(TRY_CAST(a.v AS DOUBLE), TRY_CAST(b.v AS DOUBLE), op)), 'http://www.w3.org/2001/XMLSchema#float')
    ELSE sg_any(2, sg_double_str(sg_arith_dbl(TRY_CAST(a.v AS DOUBLE), TRY_CAST(b.v AS DOUBLE), op)), 'http://www.w3.org/2001/XMLSchema#double') END;
CREATE OR REPLACE MACRO sg_any_eq(a, b) AS CASE WHEN a IS NULL OR b IS NULL THEN NULL
    ELSE a.k = b.k AND a.v = b.v AND (a.d IS NOT DISTINCT FROM b.d) END;
CREATE OR REPLACE MACRO sg_langmatches(tag, range) AS CASE WHEN tag IS NULL OR range IS NULL THEN NULL
    WHEN range = '*' THEN tag <> ''
    ELSE lower(tag) = lower(range) OR starts_with(lower(tag), lower(range) || '-') END;
CREATE OR REPLACE MACRO sg_timezone(s) AS CASE
    WHEN regexp_matches(s, 'Z$') THEN 'PT0S'
    WHEN regexp_matches(s, '[+-][0-9][0-9]:[0-9][0-9]$') THEN
        (CASE WHEN regexp_extract(s, '([+-])[0-9][0-9]:[0-9][0-9]$', 1) = '-' THEN '-' ELSE '' END) || 'PT'
        || CAST(CAST(regexp_extract(s, '[+-]([0-9][0-9]):[0-9][0-9]$', 1) AS INTEGER) AS VARCHAR) || 'H'
        || CASE WHEN regexp_extract(s, '[+-][0-9][0-9]:([0-9][0-9])$', 1) <> '00' THEN CAST(CAST(regexp_extract(s, '[+-][0-9][0-9]:([0-9][0-9])$', 1) AS INTEGER) AS VARCHAR) || 'M' ELSE '' END
    ELSE NULL END;
"""
