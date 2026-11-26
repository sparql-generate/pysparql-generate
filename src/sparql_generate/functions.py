"""Custom binding functions of the ``fun:`` namespace (``http://w3id.org/sparql-generate/fn/``).

Each entry maps a function IRI to ``compile(ctx, args: list[CExpr]) -> CExpr``.
Vectorised SQL is produced when DuckDB can evaluate the function natively;
otherwise a Python UDF is registered on the engine (row-by-row fallback).
Constant arguments are folded at compile time.
"""
from __future__ import annotations

from typing import Callable, Dict, List

from . import ast as A
from . import pyeval as P
from .documents import Document
from .terms import sql_string
from .termtypes import (
    ANY_T, BNODE_T, JSON_T, NULL, STR_T, TType, CExpr, const_bool, const_int, const_iri, const_str,
    lex, numeric, string_literal_lex, typed, lang as lang_t,
)

FUN = A.SG_FUN
Registry = Dict[str, Callable]
FUNCTIONS: Registry = {}


def register(name: str):
    def deco(fn):
        FUNCTIONS[FUN + name] = fn
        return fn

    return deco


def const_from_any(d) -> CExpr:
    """A constant CExpr from a dynamic term dict (as returned by :mod:`pyeval`)."""
    if d is None:
        return NULL
    if d["k"] == 0:
        return const_iri(d["v"])
    if d["k"] == 1:
        return CExpr(sql_string(d["v"]), BNODE_T, d["v"])
    dt = d["d"]
    if dt is None:
        return const_str(d["v"])
    if dt.startswith("@"):
        return CExpr(sql_string(d["v"]), lang_t(dt[1:]), d["v"])
    if dt == A.XSD_INTEGER:
        return const_int(int(d["v"]))
    if dt == A.XSD_BOOLEAN:
        return const_bool(d["v"] == "true")
    return CExpr(sql_string(d["v"]), typed(dt), d["v"])


def resolve_doc_arg(ctx, e: CExpr) -> CExpr:
    """A document-valued argument: a constant IRI is looked up in the document
    set at compile time; an IRI column is looked up per row (rule SOURCE)."""
    if e.type.kind != "iri":
        return e
    if e.is_const:
        if e.const is None:
            return NULL
        doc = ctx.engine.document(str(e.const))
        if doc is None:
            ctx.warnings.append(f"document <{e.const}> not found")
            return NULL
        path = doc.local_path()
        sql = f"(SELECT content FROM read_text({sql_string(path)}))" if path else ctx.engine.doc_sql_const(doc.text())
        return CExpr(sql, typed(doc.datatype), doc)
    engine = ctx.engine

    def udf(uri):
        d = engine.document(uri) if uri is not None else None
        return None if d is None else {"k": 2, "v": d.text(), "d": d.datatype}

    name = engine.udf("sg_source", udf, ["VARCHAR"], engine.ANY_TYPE, key=None)
    return CExpr(f"{name}({e.sql})", ANY_T)


def doc_text_of(e: CExpr):
    """The text of a constant document or constant string argument, else None."""
    if e.is_const:
        if isinstance(e.const, Document):
            return e.const.text()
        if e.const is not None:
            return str(e.const)
    return None


def doc_lookup_sql(ctx, e: CExpr) -> str:
    """SQL giving the text of a document-valued argument for a UDF.  Constant
    documents are passed by IRI (``\\x00uri``) and resolved in Python, so that
    large documents are not shipped once per row."""
    if e.is_const and isinstance(e.const, Document):
        ctx.engine.documents_by_uri[e.const.uri] = e.const
        return sql_string("\x00" + e.const.uri)
    return lex(e)


def _resolve_doc_text(engine, s):
    if s is None:
        return None
    if s.startswith("\x00"):
        doc = engine.documents_by_uri.get(s[1:])
        return doc.text() if doc is not None else None
    return s


# -- JSON ------------------------------------------------------------------------


@register("JSONPath")
def fun_jsonpath(ctx, args: List[CExpr]) -> CExpr:
    args = [resolve_doc_arg(ctx, args[0])] + list(args[1:]) if args else args
    if len(args) != 2:
        raise ValueError("fun:JSONPath takes 2 arguments")
    doc, path = args
    if doc.is_null or path.is_null:
        return NULL
    text = doc_text_of(doc)
    if text is not None and path.is_const:
        try:
            value = P.jsonpath_first(P.parse_json(text), str(path.const))
        except Exception:
            return NULL
        return const_from_any(P.any_from_python(value))
    if text is None and path.is_const and P.jsonpath_is_native(str(path.const)):
        src = doc.sql if doc.type.kind == "json" else lex(doc)
        return CExpr(f"json_extract({src}, {path.sql})", JSON_T)
    engine = ctx.engine

    def udf(d, p):
        d = _resolve_doc_text(engine, d)
        if d is None or p is None:
            return None
        try:
            return P.json_text(P.jsonpath_first(P.parse_json(d), p))
        except Exception:
            return None

    name = engine.udf("sg_jsonpath", udf, ["VARCHAR", "VARCHAR"], "JSON")
    return CExpr(f"{name}({doc_lookup_sql(ctx, doc)}, {string_literal_lex(path)})", JSON_T)


@register("CBOR")
def fun_cbor(ctx, args: List[CExpr]) -> CExpr:
    args = [resolve_doc_arg(ctx, args[0])] + list(args[1:]) if args else args
    doc, path = args
    if doc.is_null or path.is_null:
        return NULL
    text = doc_text_of(doc)
    if text is not None and path.is_const:
        try:
            value = P.jsonpath_first(P.cbor_to_json(text), str(path.const))
        except Exception:
            return NULL
        return const_from_any(P.any_from_python(value))
    engine = ctx.engine

    def udf(d, p):
        d = _resolve_doc_text(engine, d)
        if d is None or p is None:
            return None
        try:
            return P.json_text(P.jsonpath_first(P.cbor_to_json(d), p))
        except Exception:
            return None

    name = engine.udf("sg_cbor", udf, ["VARCHAR", "VARCHAR"], "JSON")
    return CExpr(f"{name}({doc_lookup_sql(ctx, doc)}, {string_literal_lex(path)})", JSON_T)


# -- XML / HTML --------------------------------------------------------------------


def _xpath_value(text: str, path: str):
    root = P.parse_xml(text)
    nodes = P.xpath_nodes(root, path)
    if not nodes:
        return None
    return P.xml_node_term(nodes[0])


@register("XPath")
def fun_xpath(ctx, args: List[CExpr]) -> CExpr:
    args = [resolve_doc_arg(ctx, args[0])] + list(args[1:]) if args else args
    doc, path = args
    if doc.is_null or path.is_null:
        return NULL
    text = doc_text_of(doc)
    if text is not None and path.is_const:
        try:
            return const_from_any(_xpath_value(text, str(path.const)))
        except Exception:
            return NULL
    engine = ctx.engine

    def udf(d, p):
        d = _resolve_doc_text(engine, d)
        if d is None or p is None:
            return None
        try:
            return _xpath_value(d, p)
        except Exception:
            return None

    name = engine.udf("sg_xpath", udf, ["VARCHAR", "VARCHAR"], engine.ANY_TYPE)
    return CExpr(f"{name}({doc_lookup_sql(ctx, doc)}, {string_literal_lex(path)})", ANY_T)


def _css_value(text: str, selector: str):
    return P.css_value(P.parse_html(text), selector)


@register("CSSPath")
def fun_csspath(ctx, args: List[CExpr]) -> CExpr:
    args = [resolve_doc_arg(ctx, args[0])] + list(args[1:]) if args else args
    doc, sel = args
    if doc.is_null or sel.is_null:
        return NULL
    text = doc_text_of(doc)
    if text is not None and sel.is_const:
        try:
            return const_from_any(_css_value(text, str(sel.const)))
        except Exception:
            return NULL
    engine = ctx.engine

    def udf(d, p):
        d = _resolve_doc_text(engine, d)
        if d is None or p is None:
            return None
        try:
            return _css_value(d, p)
        except Exception:
            return None

    name = engine.udf("sg_csspath", udf, ["VARCHAR", "VARCHAR"], engine.ANY_TYPE)
    return CExpr(f"{name}({doc_lookup_sql(ctx, doc)}, {string_literal_lex(sel)})", ANY_T)


@register("HTMLtoXML")
def fun_htmltoxml(ctx, args: List[CExpr]) -> CExpr:
    args = [resolve_doc_arg(ctx, args[0])] + list(args[1:]) if args else args
    (doc,) = args
    engine = ctx.engine

    def udf(d):
        d = _resolve_doc_text(engine, d)
        if d is None:
            return None
        from lxml import etree, html

        return etree.tostring(html.fromstring(d), method="xml", encoding="unicode")

    name = engine.udf("sg_htmltoxml", udf, ["VARCHAR"], "VARCHAR")
    return CExpr(f"{name}({doc_lookup_sql(ctx, doc)})", typed(P.XML_DT))


# -- strings ----------------------------------------------------------------------


@register("regex")
def fun_regex(ctx, args: List[CExpr]) -> CExpr:
    s, pat, grp = args
    if any(a.is_null for a in args):
        return NULL
    ls = lex(s)
    lp = string_literal_lex(pat)
    g = numeric(grp)
    if lp == "NULL" or g == "NULL":
        return NULL
    pat_sql = f"('(?m)' || {lp})"
    return CExpr(f"CASE WHEN regexp_matches({ls}, {pat_sql}) THEN regexp_extract({ls}, {pat_sql}, CAST({g} AS INTEGER)) END", STR_T)


@register("SplitAtPosition")
def fun_split_at_position(ctx, args: List[CExpr]) -> CExpr:
    s, pat, pos = args
    if any(a.is_null for a in args):
        return NULL
    ls = string_literal_lex(s)
    lp = string_literal_lex(pat)
    p = numeric(pos)
    if ls == "NULL" or lp == "NULL" or p == "NULL":
        return NULL
    return CExpr(f"list_extract(sg_split({ls}, {lp}), CAST({p} AS BIGINT) + 1)", STR_T)


@register("bnode")
def fun_bnode(ctx, args: List[CExpr]) -> CExpr:
    """``fun:bnode(label)``: one blank node per distinct label for the whole execution."""
    (e,) = args
    if e.is_null:
        return NULL
    if e.is_const:
        import hashlib

        label = "F" + hashlib.md5(str(e.const).encode("utf-8")).hexdigest()
        return CExpr(sql_string(label), BNODE_T, label)
    return CExpr(f"('F' || md5({lex(e)}))", BNODE_T)


@register("camelCase")
def fun_camelcase(ctx, args):
    (e,) = args
    s = string_literal_lex(e)
    if s == "NULL":
        return NULL
    name = ctx.engine.udf("sg_camelcase", _camel, ["VARCHAR"], "VARCHAR")
    return CExpr(f"{name}({s})", STR_T)


def _camel(s):
    if s is None:
        return None
    parts = [p for p in __import__("re").split(r"[^A-Za-z0-9]+", s) if p]
    return "".join([parts[0].lower()] + [p[:1].upper() + p[1:].lower() for p in parts[1:]]) if parts else ""


@register("titleCase")
def fun_titlecase(ctx, args):
    (e,) = args
    s = string_literal_lex(e)
    if s == "NULL":
        return NULL
    if e.is_const and e.const is not None and e.type.is_stringy:
        return const_str(title_case(str(e.const)))
    name = ctx.engine.udf("sg_titlecase", title_case, ["VARCHAR"], "VARCHAR")
    return CExpr(f"{name}({s})", STR_T)


def title_case(s):
    """``fun:titleCase``, as the Java engine: the characters that are neither letters, digits
    nor white space removed, the first letter or digit of each word in upper case, the other
    characters kept, a run of white space after a word replaced by one space."""
    if s is None:
        return None
    out = []
    first_space, convert_next = False, True
    for ch in s:
        if ch in " \t\n\x0b\x0c\r\x1c\x1d\x1e\x1f" or (ch.isspace() and ch not in "\xa0\u2007\u202f"):
            if first_space:
                out.append(" ")
                first_space, convert_next = False, True
        elif not ch.isalnum():
            pass
        elif convert_next:
            out.append(ch.upper())
            convert_next, first_space = False, True
        else:
            out.append(ch)
            first_space = True
    return "".join(out)


@register("mixedCase")
def fun_mixedcase(ctx, args):
    return fun_camelcase(ctx, args)


@register("prefixedIRI")
def fun_prefixed_iri(ctx, args):
    (e,) = args
    if e.type.kind != "iri":
        return NULL
    prefixes = ctx.prefixes
    cases = " ".join(
        f"WHEN starts_with({e.sql}, {sql_string(ns)}) THEN {sql_string(p + ':')} || substr({e.sql}, {len(ns) + 1})"
        for p, ns in sorted(prefixes.items(), key=lambda kv: -len(kv[1]))
    )
    if not cases:
        return NULL
    return CExpr(f"CASE {cases} END", STR_T)


@register("dateTime")
def fun_datetime(ctx, args):
    """``fun:dateTime(string[, java-format])``: parse with a Java ``SimpleDateFormat``
    pattern (subset) into an ``xsd:dateTime``."""
    s = string_literal_lex(args[0])
    if s == "NULL":
        return NULL
    if len(args) == 1:
        return CExpr(f"strftime(TRY_CAST({s} AS TIMESTAMP), '%Y-%m-%dT%H:%M:%S')", TType("typed", dt=A.XSD + "dateTime"))
    fmt = string_literal_lex(args[1])

    def udf(v, f):
        if v is None or f is None:
            return None
        from datetime import datetime

        py = (f.replace("yyyy", "%Y").replace("MM", "%m").replace("dd", "%d").replace("HH", "%H")
              .replace("mm", "%M").replace("ss", "%S").replace("'T'", "T").replace("XXX", "%z").replace("Z", "%z"))
        try:
            return datetime.strptime(v, py).strftime("%Y-%m-%dT%H:%M:%S")
        except Exception:
            return None

    name = ctx.engine.udf("sg_datetime", udf, ["VARCHAR", "VARCHAR"], "VARCHAR")
    return CExpr(f"{name}({s}, {fmt})", TType("typed", dt=A.XSD + "dateTime"))


@register("log")
def fun_log(ctx, args):
    return args[0] if args else NULL


@register("property")
def fun_property(ctx, args):
    (e,) = args
    s = string_literal_lex(e)
    if s == "NULL":
        return NULL
    import os

    name = ctx.engine.udf("sg_property", lambda k: None if k is None else os.environ.get(k), ["VARCHAR"], "VARCHAR")
    return CExpr(f"{name}({s})", STR_T)


# -- RDF 1.2 triple terms (Jena's afn:triple, afn:isTriple, afn:subject, afn:predicate, afn:object)

AFN = "http://jena.apache.org/ARQ/function#"


def afn_triple(ctx, args):
    """``afn:triple(s, p, o)``: the triple term ``<<( s p o )>>``, a dynamic term of kind 3
    whose value is its N-Triples form (Jena 5, RDF 1.2); unbound when ``p`` is not an IRI or
    an argument is unbound (as Jena, a literal subject is accepted)."""
    from .termtypes import to_nt

    if len(args) != 3:
        raise ValueError("afn:triple takes three arguments")
    s, p, o = (to_nt(a, pos) for a, pos in zip(args, "opo"))  # Jena accepts a literal subject
    if "NULL" in (s, p, o):
        return NULL
    return CExpr(f"sg_any(3, '<<( ' || {s} || ' ' || {p} || ' ' || {o} || ' )>>', NULL)", ANY_T)


def afn_is_triple(ctx, args):
    (e,) = args
    if e.type.kind != "any":
        return CExpr(f"CASE WHEN {e.sql} IS NULL THEN NULL ELSE FALSE END", TType("bool")) if not e.is_const \
            else (NULL if e.is_null else const_bool(False))
    return CExpr(f"(({e.sql}).k = 3)", TType("bool"))


def _triple_part(i: int):
    def compile_(ctx, args):
        (e,) = args
        if e.type.kind != "any":
            return NULL
        name = ctx.engine.udf("sg_triple_part", _triple_part_udf, ["VARCHAR", "INTEGER"], "VARCHAR")
        return CExpr(f"sg_nt_any({name}(CASE WHEN ({e.sql}).k = 3 THEN ({e.sql}).v END, {i}))", ANY_T)

    return compile_


def triple_parts(nt: str):
    """The three terms (N-Triples) of the N-Triples form of a triple term ``<<( s p o )>>``."""
    import re

    body = nt.strip()
    if not (body.startswith("<<(") and body.endswith(")>>")):
        return None
    body = body[3:-3].strip()
    parts, i = [], 0
    token = re.compile(r'<[^>]*>|_:\S+|"(?:[^"\\]|\\.)*"(?:@[A-Za-z0-9-]+|\^\^<[^>]*>)?')
    while i < len(body):
        if body[i].isspace():
            i += 1
            continue
        if body.startswith("<<(", i):
            depth, j = 0, i
            while j < len(body):
                if body.startswith("<<(", j):
                    depth, j = depth + 1, j + 3
                elif body.startswith(")>>", j):
                    depth, j = depth - 1, j + 3
                    if depth == 0:
                        break
                else:
                    m = token.match(body, j)
                    j = m.end() if m else j + 1
            parts.append(body[i:j])
            i = j
            continue
        m = token.match(body, i)
        if not m:
            return None
        parts.append(m.group(0))
        i = m.end()
    return parts if len(parts) == 3 else None


def _triple_part_udf(nt, i):
    if nt is None:
        return None
    parts = triple_parts(nt)
    return parts[i] if parts else None


FUNCTIONS[AFN + "triple"] = afn_triple
FUNCTIONS[AFN + "isTriple"] = afn_is_triple
FUNCTIONS[AFN + "subject"] = _triple_part(0)
FUNCTIONS[AFN + "predicate"] = _triple_part(1)
FUNCTIONS[AFN + "object"] = _triple_part(2)


_SCHEME = __import__("re").compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")
_IRIREF_FORBIDDEN = __import__("re").compile(r'[\x00-\x20<>"{}|^`\\]')


@register("unsafeIRI")
def fun_unsafe_iri(ctx, args):
    """``fun:unsafeIRI(s)`` (the Java engine 2.6.1, for ``rml:UnsafeIRI``): a string that starts
    with a scheme (the lexical form of any literal) is the IRI as it is, neither validated nor
    resolved (white space allowed); an IRI stays; another term is an evaluation error
    (unbound); the Java engine's ``FUN_unsafeIRI`` (fix-2.6.1 ``6cb4cd0``).  Such an IRI is a dynamic
    term, written in N-Triples with its forbidden characters as ``\\uXXXX``
    (``sg_any_nt``)."""
    if len(args) != 1:
        return NULL
    (e,) = args
    k = e.type.kind
    if e.is_null:
        return NULL
    if k == "iri":
        return e
    if k in ("int", "double", "bool"):
        return NULL  # a number or a boolean has no scheme
    if e.is_const and k in ("str", "lang", "typed"):
        s = str(e.const)
        if not _SCHEME.match(s):
            return NULL
        if not _IRIREF_FORBIDDEN.search(s):
            return const_iri(s)
        return CExpr(f"sg_any(0, {sql_string(s)}, NULL)", ANY_T)
    if k in ("str", "lang", "typed"):  # any literal: its lexical form
        v = lex(e)
        return CExpr(f"CASE WHEN regexp_matches({v}, '^[A-Za-z][A-Za-z0-9+.-]*:') THEN sg_any(0, {v}, NULL) END",
                     ANY_T)
    if k == "any":
        x = e.sql
        return CExpr(f"CASE WHEN ({x}).k = 0 THEN {x} WHEN ({x}).k = 2 AND regexp_matches(({x}).v, "
                     f"'^[A-Za-z][A-Za-z0-9+.-]*:') THEN sg_any(0, ({x}).v, NULL) END", ANY_T)
    return NULL
