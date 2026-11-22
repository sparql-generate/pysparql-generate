"""Native reader of N-Triples and N-Quads files, for the dataset table of rule (BGP).

The file is read by DuckDB's parallel CSV reader, one column per line (a delimiter
that cannot occur in practice), each line is split into its terms by one regular
expression, and the terms are decoded by the macros that decode N-Triples terms
everywhere else (``sg_nt_any``): string escapes are JSON's.  A file that this fast
path cannot read exactly (a ``\\U`` or ``\\'`` escape, an escaped IRI, a blank node
graph name, a line that is not plain N-Triples or N-Quads) returns ``None``: the
caller parses it with rdflib.  Blank node labels of the file are prefixed with
``D``, so that they never meet the labels that the compiler creates (``B``, ``L``,
``E``, ``K``, ``F``).
"""
from __future__ import annotations

import os
from typing import Optional

from .terms import sql_string

_IRI = r'<[^>]*>'
_BNODE = r'_:[^ \t<>"]+'
_LIT = r'"(?:[^"\\]|\\.)*"(?:@[A-Za-z]+(?:-[A-Za-z0-9]+)*|\^\^<[^>]*>)?'
LINE = (rf'^[ \t]*({_IRI}|{_BNODE})[ \t]*({_IRI})[ \t]*({_IRI}|{_BNODE}|{_LIT})'
        rf'[ \t]*({_IRI}|{_BNODE})?[ \t]*\.[ \t]*(?:#.*)?$')
SKIP = r'^[ \t]*(?:#.*)?$'
NT_EXTENSIONS = (".nt", ".ntriples", ".nq", ".nquads")


def is_ntriples_path(path: str) -> bool:
    return str(path).lower().endswith(NT_EXTENSIONS)


def _lines(path: str) -> str:
    sep = chr(30) + chr(31)
    return (f"read_csv({sql_string(os.path.abspath(path))}, columns={{'line': 'VARCHAR'}}, header=false, "
            f"delim={sql_string(sep)}, quote='', escape='', auto_detect=false, strict_mode=false)")


def native_quads(engine, path: str) -> Optional[str]:
    """A FROM item ``(g, s, p, o)`` of the quads of the file (``g`` NULL in the default
    graph, ``s`` and ``o`` in the dynamic representation, ``p`` the predicate IRI), or
    None when the file needs the rdflib parser."""
    src = _lines(path)
    line, skip = sql_string(LINE), sql_string(SKIP)
    try:
        bad, = engine.execute(
            f"SELECT count(*) FILTER (WHERE (NOT regexp_full_match(line, {skip}) AND NOT regexp_full_match(line, {line}))"
            f" OR contains(line, '\\U') OR contains(line, '\\''') OR regexp_matches(line, '<[^>]*\\\\')"
            f" OR starts_with(regexp_extract(line, {line}, 4), '_:'))"
            f" FROM {src}").fetchall()[0]
    except Exception:  # noqa: BLE001 - unreadable as lines: let rdflib report it
        return None
    if bad:
        return None

    def term(x: str) -> str:
        return (f"CASE WHEN starts_with({x}, '_:') THEN sg_any(1, 'D' || substr({x}, 3), NULL) "
                f"ELSE sg_nt_any({x}) END")

    return (f"(SELECT CASE WHEN t.g = '' THEN NULL ELSE substr(t.g, 2, length(t.g) - 2) END AS g, {term('t.s')} AS s, "
            f"substr(t.p, 2, length(t.p) - 2) AS p, {term('t.o')} AS o FROM (SELECT x['s'] AS s, x['p'] AS p, "
            f"x['o'] AS o, x['g'] AS g FROM (SELECT regexp_extract(line, {line}, ['s', 'p', 'o', 'g']) AS x "
            f"FROM {src} WHERE NOT regexp_full_match(line, {skip}))) AS t)")


# -- other syntaxes, through pyoxigraph (Rust) when it is installed ----------------------------

OXIGRAPH_FORMATS = {".ttl": "TURTLE", ".turtle": "TURTLE", ".trig": "TRIG", ".rdf": "RDF_XML",
                    ".owl": "RDF_XML", ".xml": "RDF_XML", ".n3": "N3", ".nt": "N_TRIPLES",
                    ".ntriples": "N_TRIPLES", ".nq": "N_QUADS", ".nquads": "N_QUADS"}
MEDIA_FORMATS = {"text/turtle": "TURTLE", "application/x-turtle": "TURTLE", "application/trig": "TRIG",
                 "application/rdf+xml": "RDF_XML", "application/n-triples": "N_TRIPLES",
                 "application/n-quads": "N_QUADS", "text/n3": "N3"}


def oxigraph_quads(engine, path: Optional[str] = None, data: Optional[str] = None, fmt: Optional[str] = None,
                   base: Optional[str] = None) -> Optional[str]:
    """A FROM item ``(g, s, p, o)`` of an RDF file or text parsed by pyoxigraph, or None when
    pyoxigraph is not installed or the syntax is not one it reads (the caller uses rdflib).
    A syntax error raises ``ValueError``."""
    try:
        import pyoxigraph as ox
    except ImportError:
        return None
    if fmt is None and path is not None:
        fmt = OXIGRAPH_FORMATS.get(os.path.splitext(str(path))[1].lower(), "TURTLE")
    if fmt is None:
        return None
    import pyarrow as pa

    from . import ast as A

    rdf_format = getattr(ox.RdfFormat, fmt)
    if base is None and path is not None:
        base = "file://" + os.path.abspath(path)
    kwargs = {"path": path} if path is not None else {"input": data.encode("utf-8")}
    try:
        quads = ox.parse(format=rdf_format, base_iri=base, **kwargs)
    except (SyntaxError, OSError) as ex:
        raise ValueError(str(ex))
    cache: dict = {}

    def conv(t):
        r = cache.get(t)
        if r is None:
            if isinstance(t, ox.NamedNode):
                r = (0, t.value, None)
            elif isinstance(t, ox.BlankNode):
                r = (1, "D" + t.value, None)
            else:
                dt = t.datatype.value
                r = (2, t.value, "@" + t.language if t.language else (None if dt == A.XSD_STRING else dt))
            cache[t] = r
        return r

    g_col, p_col, sk, sv, sd, ok, ov, od = [], [], [], [], [], [], [], []
    try:
        for q in quads:
            gn = q.graph_name
            g_col.append(None if isinstance(gn, ox.DefaultGraph) else (gn.value if isinstance(gn, ox.NamedNode)
                                                                       else "_:D" + gn.value))
            p_col.append(q.predicate.value)
            a, b = conv(q.subject), conv(q.object)
            sk.append(a[0]); sv.append(a[1]); sd.append(a[2])  # noqa: E702
            ok.append(b[0]); ov.append(b[1]); od.append(b[2])  # noqa: E702
    except SyntaxError as ex:
        raise ValueError(str(ex))

    def struct(k, v, d):
        return pa.StructArray.from_arrays([pa.array(k, pa.int8()), pa.array(v, pa.string()), pa.array(d, pa.string())],
                                          names=["k", "v", "d"])

    tbl = pa.table({"g": pa.array(g_col, pa.string()), "s": struct(sk, sv, sd), "p": pa.array(p_col, pa.string()),
                    "o": struct(ok, ov, od)})
    return f"(SELECT * FROM {engine.register_arrow(tbl)})"
