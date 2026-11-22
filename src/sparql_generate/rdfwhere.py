"""WHERE clauses over an RDF dataset: the rdflib fallback (rule WHERE-RDF).

The current solutions are serialised as a ``VALUES`` block prefixed to the
SPARQL SELECT built from the group graph pattern, exactly as the Java engine
does (Theorem 1 of the ESWC 2017 paper); rdflib evaluates it and the result is
loaded back as an Arrow table whose columns use the dynamic term representation.
"""
from __future__ import annotations

from typing import Dict, List

import pyarrow as pa

from . import ast as A
from .expressions import CompileError
from .serializer import serialize_element, serialize_term
from .terms import parse_nt_term
from .termtypes import ANY_T, CExpr, to_nt


def compile_where_rdflib(compiler, ctx, rel, group: A.Group):
    engine = compiler.engine
    from .bgp import rdflib_of_dataset

    dataset = rdflib_of_dataset(engine)
    if getattr(ctx, "dataset", None) is not None:  # the level's dataset clauses (rule FROM)
        from .datasets import rdflib_dataset

        dataset = rdflib_dataset(engine, ctx.dataset)
    if dataset is None:
        raise CompileError("this WHERE clause needs an RDF dataset (triple patterns); none was given "
                           "(use dataset=..., or the 'graph'/'namedgraphs' keys of sparql-generate-conf.json)")
    # 1. current solutions as N-Triples terms
    # reserved columns (parent row id, shared blank nodes, aggregates) travel under
    # temporary variable names and are mapped back afterwards
    reserved = {n: f"_sgr{i}" for i, n in enumerate(k for k in ctx.scope if k.startswith("\x00"))}
    back = {v: k for k, v in reserved.items()}
    vars_ = [reserved.get(n, n) for n, e in ctx.scope.items() if not e.is_const]
    orig = {reserved.get(n, n): n for n in ctx.scope}
    consts = {n: e for n, e in ctx.scope.items() if e.is_const and not e.is_null and not n.startswith("\x00")}
    rows: List[tuple] = []
    if not rel.trivial:
        proj = ", ".join(f"{to_nt(ctx.scope[orig[n]], 'o')} AS \"{i}\"" for i, n in enumerate(vars_)) or "1"
        rows = engine.execute(f"SELECT {proj} FROM {rel.sql}").fetchall()
    else:
        rows = [()]
    # 2. the SPARQL query
    prefixes = ""
    sparql = prefixes + "SELECT * WHERE {\n"
    if vars_ and rows:
        sparql += "VALUES (" + " ".join("?" + v for v in vars_) + ") {\n"
        for r in rows:
            sparql += "(" + " ".join(c if c is not None else "UNDEF" for c in r) + ")\n"
        sparql += "}\n"
    for n, e in consts.items():
        t = engine.execute(f"SELECT {to_nt(e, 'o')}").fetchall()[0][0]
        if t is not None and len(t) < 10000:
            sparql += f"BIND({t} AS ?{n})\n"
    sparql += "\n".join(serialize_element(el) for el in group.elements) + "\n"
    sparql += "BIND(1 AS ?_sgone)\n}"  # every solution binds a variable (rdflib drops empty ones)
    # 3. evaluate
    try:
        res = dataset.query(sparql)
    except Exception as ex:
        raise CompileError(f"rdflib could not evaluate the WHERE clause: {ex}\n{sparql}")
    names = [back.get(str(v), str(v)) for v in res.vars if str(v) != "_sgone"]
    res = [[t for v, t in zip(res.vars, row) if str(v) != "_sgone"] for row in res]
    cols: Dict[str, list] = {n: [] for n in names}
    for row in res:
        for n, term in zip(names, row):
            cols[n].append(_term_to_any(term))
    if names:
        tbl = pa.table({n: pa.array(cols[n], type=engine.ANY_ARROW) for n in names})
    else:  # no variable: one row per solution, and Arrow needs a column
        tbl = pa.table({"__one": pa.array([1] * len(res), type=pa.int8())})
    tname = engine.register_arrow(tbl)
    new_scope: Dict[str, CExpr] = {}
    sel = []
    for n in names:
        col = compiler.new_column(n)
        sel.append(f'"{n}" AS {col}')
        if n in reserved:
            new_scope[n] = CExpr(f"TRY_CAST(({col}).v AS BIGINT)", ctx.scope[n].type) if n == "\x00prid" else CExpr(f"({col}).v", ctx.scope[n].type)
            new_scope[n] = CExpr(col, ANY_T) if False else new_scope[n]
        else:
            new_scope[n] = CExpr(col, ANY_T)
    ctx.scope = {**{k: v for k, v in ctx.scope.items() if k not in names and v.is_const}, **new_scope}
    from .compiler import Rel

    return Rel(f"(SELECT {', '.join(sel) or '1 AS __one'} FROM {tname})")


def _term_to_any(term):
    import rdflib

    if term is None:
        return None
    if isinstance(term, rdflib.URIRef):
        return {"k": 0, "v": str(term), "d": None}
    if isinstance(term, rdflib.BNode):
        return {"k": 1, "v": str(term), "d": None}
    if isinstance(term, rdflib.Literal):
        if term.language:
            return {"k": 2, "v": str(term), "d": "@" + term.language}
        dt = str(term.datatype) if term.datatype else None
        if dt == A.XSD_STRING:
            dt = None
        return {"k": 2, "v": str(term), "d": dt}
    return {"k": 2, "v": str(term), "d": None}


def _any_to_term(x):
    """A dynamic term (dict k, v, d) as an rdflib term."""
    import rdflib

    if x is None:
        return None
    if x["k"] == 0:
        return rdflib.URIRef(x["v"])
    if x["k"] == 1:
        return rdflib.BNode(x["v"])
    d = x["d"]
    if d and d.startswith("@"):
        return rdflib.Literal(x["v"], lang=d[1:])
    return rdflib.Literal(x["v"], datatype=rdflib.URIRef(d) if d else None)
