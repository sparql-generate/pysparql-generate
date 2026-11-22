"""Serialisation of AST fragments back to SPARQL 1.1 syntax (used to hand a
WHERE clause to rdflib).  Embedded expressions are expanded to their
``IRI(CONCAT(...))`` / ``STRDT(CONCAT(...))`` normal form."""
from __future__ import annotations

from typing import Optional

from . import ast as A
from .terms import nt_literal

_INFIX = {"||", "&&", "=", "!=", "<", ">", "<=", ">=", "+", "-", "*", "/"}


def serialize_term(t, base: Optional[str] = None) -> str:
    if isinstance(t, A.Var):
        return "?" + t.name
    if isinstance(t, A.IRI):
        return "<" + t.value + ">"
    if isinstance(t, A.Literal):
        return nt_literal(t.lexical, t.lang, t.datatype)
    if isinstance(t, A.BNode):
        return "_:" + t.label
    if isinstance(t, A.XExpr):
        return serialize_expr(t.expr)
    if isinstance(t, (A.XIRI, A.XLiteral, A.Call, A.Aggregate)):
        return serialize_expr(t)
    raise ValueError(f"cannot serialise {t!r}")


def serialize_expr(e) -> str:
    if isinstance(e, (A.Var, A.IRI, A.Literal, A.BNode)):
        return serialize_term(e)
    if isinstance(e, A.XExpr):
        return serialize_expr(e.expr)
    if isinstance(e, A.XIRI):
        parts = []
        for c in e.components:
            parts.append(nt_literal(c) if isinstance(c, str) else f"ENCODE_FOR_URI(STR({serialize_expr(c)}))")
        return f"IRI(CONCAT({', '.join(parts)}))"
    if isinstance(e, A.XLiteral):
        parts = []
        for c in e.components:
            parts.append(nt_literal(c) if isinstance(c, str) else f"STR({serialize_expr(c)})")
        s = f"CONCAT({', '.join(parts)})"
        if e.lang:
            return f"STRLANG({s}, {nt_literal(e.lang)})"
        if e.datatype is not None:
            return f"STRDT({s}, {serialize_expr(e.datatype)})"
        return s
    if isinstance(e, A.Call):
        n = e.name
        args = [serialize_expr(a) for a in e.args]
        if n in _INFIX:
            return f"({args[0]} {n} {args[1]})"
        if n == "!":
            return f"(!{args[0]})"
        if n in ("unary-", "unary+"):
            return f"({n[-1]}{args[0]})"
        if n in ("IN", "NOT IN"):
            return f"({args[0]} {n} ({', '.join(args[1:])}))"
        if n.startswith("http") or ":" in n and not n.isupper():
            return f"<{n}>({', '.join(args)})"
        return f"{n}({', '.join(args)})"
    if isinstance(e, A.Aggregate):
        d = "DISTINCT " if e.distinct else ""
        if e.star:
            return "COUNT(*)"
        inner = serialize_expr(e.args[0])
        if e.name == "GROUP_CONCAT" and e.separator is not None:
            return f"GROUP_CONCAT({d}{inner}; SEPARATOR={nt_literal(e.separator)})"
        return f"{e.name}({d}{inner})"
    if isinstance(e, A.Exists):
        return ("NOT EXISTS " if e.negated else "EXISTS ") + serialize_group(e.pattern)
    raise ValueError(f"cannot serialise expression {e!r}")


def serialize_path(p) -> str:
    if isinstance(p, A.IRI):
        return "<" + p.value + ">"
    if isinstance(p, (A.Var, A.XIRI)):
        return serialize_term(p)
    if isinstance(p, A.Path):
        a = [serialize_path(x) for x in p.args]
        if p.op == "inverse":
            return "^" + a[0]
        if p.op == "seq":
            return f"({a[0]}/{a[1]})"
        if p.op == "alt":
            return f"({a[0]}|{a[1]})"
        if p.op == "zeroOrOne":
            return f"({a[0]})?"
        if p.op == "zeroOrMore":
            return f"({a[0]})*"
        if p.op == "oneOrMore":
            return f"({a[0]})+"
        if p.op == "negated":
            return "!(" + "|".join(a) + ")"
    raise ValueError(f"cannot serialise path {p!r}")


def serialize_group(g, base: Optional[str] = None) -> str:
    out = ["{"]
    for el in g.elements:
        out.append(serialize_element(el))
    out.append("}")
    return "\n".join(out)


def serialize_element(el) -> str:
    if isinstance(el, A.TriplesBlock):
        return "\n".join(f"{serialize_term(t.subject)} {serialize_path(t.predicate)} {serialize_term(t.object)} ."
                         for t in el.triples)
    if isinstance(el, A.Filter):
        return f"FILTER({serialize_expr(el.expr)})"
    if isinstance(el, A.Bind):
        return f"BIND({serialize_expr(el.expr)} AS ?{el.var.name})"
    if isinstance(el, A.Values):
        head = "(" + " ".join("?" + v.name for v in el.vars) + ")"
        rows = " ".join("(" + " ".join(serialize_term(c) if c is not None else "UNDEF" for c in r) + ")" for r in el.rows)
        return f"VALUES {head} {{ {rows} }}"
    if isinstance(el, A.Optional_):
        return "OPTIONAL " + serialize_group(el.pattern)
    if isinstance(el, A.Union_):
        return " UNION ".join(serialize_group(p) for p in el.patterns)
    if isinstance(el, A.Minus):
        return "MINUS " + serialize_group(el.pattern)
    if isinstance(el, A.Graph):
        return f"GRAPH {serialize_term(el.node)} " + serialize_group(el.pattern)
    if isinstance(el, A.Service):
        return f"SERVICE {'SILENT ' if el.silent else ''}{serialize_term(el.node)} " + serialize_group(el.pattern)
    if isinstance(el, A.Group):
        return serialize_group(el)
    if isinstance(el, A.SubSelect):
        q = el.query
        proj = "*" if q.select_star else " ".join(f"?{v.name}" if e is None else f"({serialize_expr(e)} AS ?{v.name})" for v, e in q.select_vars)
        s = f"{{ SELECT {'DISTINCT ' if q.distinct else ''}{proj} WHERE {serialize_group(q.where)}"
        if q.group_by:
            s += " GROUP BY " + " ".join(f"?{g.name}" if isinstance(g, A.Var) else f"({serialize_expr(g[0])}{' AS ?' + g[1].name if g[1] else ''})" for g in q.group_by)
        if q.order_by:
            s += " ORDER BY " + " ".join(("DESC(" if o.direction < 0 else "ASC(") + serialize_expr(o.expr) + ")" for o in q.order_by)
        if q.limit is not None:
            s += f" LIMIT {q.limit}"
        if q.offset is not None:
            s += f" OFFSET {q.offset}"
        return s + " }"
    raise ValueError(f"cannot serialise {el!r}")
