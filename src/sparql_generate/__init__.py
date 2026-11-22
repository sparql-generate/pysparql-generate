"""pysparql-generate: SPARQL-Generate compiled to DuckDB.

Quick start::

    import sparql_generate as sg

    query = '''
    PREFIX iter: <http://w3id.org/sparql-generate/iter/>
    GENERATE { <http://ex.org/{ ?id }> <http://ex.org/name> ?name . }
    ITERATOR iter:CSV(<http://ex.org/people.csv>, "id", "name") AS ?id ?name
    '''
    for s, p, o in sg.generate(query, documents={"http://ex.org/people.csv": "people.csv"}):
        print(s, p, o)
    sg.generate_to(query, "out.nt", documents={...})
    graph = sg.to_graph(query, documents={...})   # an rdflib.Graph

A template with ``GRAPH`` blocks generates quads: ``sg.generate_quads(query, ...)`` iterates
over ``(s, p, o, g)`` (``g`` None for the default graph), ``sg.to_dataset(query, ...)`` gives an
``rdflib.Dataset``, ``sg.generate_to(query, "out.nq", format="nq")`` writes N-Quads (or
``"trig"``).  The triple outputs (``generate``, ``to_graph``, ``format="nt"``...) give the
default graph, with a warning when the query generates quads in named graphs.
"""
from __future__ import annotations

import json
import os
from typing import Dict, Iterator, List, Optional, Tuple, Union

from . import ast
from .compiler import Compiler, Plan
from .documents import DocumentSet
from .engine import Engine
from .expressions import CompileError, DocumentNotFound
from .lexer import ParseError
from .parser import parse

__all__ = [
    "parse", "generate", "generate_quads", "generate_to", "to_graph", "to_dataset", "template", "select", "perform",
    "Execution", "load_conf", "QuadsNotSupported", "QUAD_FORMATS",
    "ParseError", "CompileError", "DocumentNotFound", "Engine", "DocumentSet", "__version__",
]
__version__ = "0.1.0"

Documents = Union[DocumentSet, Dict[str, object], List[dict], None]

#: output formats that hold the named graphs of a dataset
QUAD_FORMATS = ("nq", "nquads", "n-quads", "application/n-quads", "trig", "application/trig")
_NQ = ("nq", "nquads", "n-quads", "application/n-quads")
DEFAULT_GRAPH_ONLY = ("the query generates quads in named graphs; this output holds the default graph only "
                      "(use quads(), to_dataset(), or the format nq or trig)")


class QuadsNotSupported(ValueError):
    """The query generates quads in named graphs, and the output format asked for holds
    triples only (raised by the command line, as the Java engine does)."""

    def __init__(self, fmt: str):
        super().__init__(f"The query generates quads in named graphs, which {fmt} cannot hold: "
                         "use --format-output TRIG or NQUADS.")


def load_conf(path: str) -> dict:
    """Read a ``sparql-generate-conf.json`` and resolve its paths against its directory."""
    with open(path, encoding="utf-8") as f:
        conf = json.load(f)
    conf["_dir"] = os.path.dirname(os.path.abspath(path))
    return conf


class Execution:
    """Compile a query once and read its results.

    Parameters
    ----------
    query: query text (or a parsed :class:`ast.Query`).
    base: base IRI when the query has none.
    documents: the document set: ``{iri: path | bytes | str | {"path":..., "mediatype":...}}``,
        a list of conf.json ``documentset`` entries, or a :class:`DocumentSet`.
    base_dir: directory against which relative document paths are resolved (and
        where ``base``-relative IRIs are looked up as files, like the Java CLI).
    named_queries: ``{iri: path-or-text}`` for ``GENERATE <iri>(...)`` calls.
    dataset: an ``rdflib.Graph``/``Dataset``, or the path of an RDF file (N-Triples and N-Quads
        read natively, other syntaxes by rdflib), for WHERE clauses with triple patterns.
    bindings: initial solutions ``[{var: term}]`` (terms are :mod:`ast` nodes).
    conf: a parsed ``sparql-generate-conf.json`` (see :func:`load_conf`).
    fail_on_missing: a document, a graph or a named query that cannot be found raises
        :class:`DocumentNotFound` (``Document not found: <iri>``) instead of a warning (the
        variable unbound, the iterator or graph empty); default: the key ``failOnMissing`` of
        ``conf``, else False (as the Java engine's ``--fail-on-missing``).
    stream_messages, stream_seconds: the bound of the endless streams of ``iter:WebSocket`` and
        ``iter:MQTTSubscribe`` (read at compile time): at most so many messages, for at most so
        many seconds; without a bound such a query is refused.
    """

    def __init__(self, query: Union[str, ast.Query], *, base: Optional[str] = None, documents: Documents = None,
                 base_dir: Optional[str] = None, named_queries: Optional[Dict[str, str]] = None, dataset=None,
                 bindings: Optional[List[Dict[str, object]]] = None, conf: Optional[dict] = None,
                 allow_network: bool = True, threads: Optional[int] = None, memory_limit: Optional[str] = None,
                 debug: bool = False, fail_on_missing: Optional[bool] = None,
                 stream_messages: Optional[int] = None, stream_seconds: Optional[float] = None):
        if conf is not None:
            if fail_on_missing is None:
                fail_on_missing = bool(conf.get("failOnMissing", False))
            base_dir = base_dir or conf.get("_dir")
            base = base or conf.get("base")
            if documents is None:
                documents = conf.get("documentset") or []
            nq = dict(named_queries or {})
            for q in conf.get("namedqueries") or []:
                nq[q["uri"]] = os.path.join(base_dir or "", q["path"])
            named_queries = nq
            if dataset is None:
                dataset = _load_dataset(conf, base_dir)
        if isinstance(query, ast.Query):
            self.query = query
        else:
            self.query = parse(query, base=base)
        base = self.query.base or base
        if isinstance(documents, DocumentSet):
            docset = documents
        else:
            docset = DocumentSet(documents, base_dir=base_dir, base=base, allow_network=allow_network)
        self.engine = Engine(docset, base=base, named_queries=named_queries, dataset=dataset,
                             threads=threads, memory_limit=memory_limit, fail_on_missing=bool(fail_on_missing))
        self.engine.debug = debug
        self.engine.stream_messages, self.engine.stream_seconds = stream_messages, stream_seconds
        self.compiler = Compiler(self.engine)
        self.plan: Plan = self.compiler.compile(self.query, bindings)

    @property
    def warnings(self) -> List[str]:
        return self.plan.warnings

    @property
    def kind(self) -> str:
        return self.plan.kind

    # -- GENERATE -----------------------------------------------------------------
    @property
    def has_quads(self) -> bool:
        """The query generates quads in named graphs (GRAPH blocks of a GENERATE template)."""
        return self.plan.kind == "GENERATE" and self.plan.has_quads

    def _default_graph_only(self) -> None:
        """A triple output of a query with quads: the default graph, with a warning."""
        if self.has_quads and DEFAULT_GRAPH_ONLY not in self.plan.warnings:
            import warnings

            self.plan.warnings.append(DEFAULT_GRAPH_ONLY)
            warnings.warn(DEFAULT_GRAPH_ONLY, stacklevel=3)

    def triples(self, distinct: bool = False, batch_size: int = 65536) -> Iterator[Tuple[str, str, str]]:
        """Iterate over the generated triples of the default graph as N-Triples term strings
        (read by Arrow record batches: about twice as fast as fetching Python tuples)."""
        for b in self.batches(distinct, batch_size):
            yield from zip(b.column(0).to_pylist(), b.column(1).to_pylist(), b.column(2).to_pylist())

    def batches(self, distinct: bool = False, batch_size: int = 65536):
        """The generated triples as a stream of ``pyarrow.RecordBatch`` (columns s, p, o of
        N-Triples term strings), computed as they are read."""
        if self.plan.kind != "GENERATE":
            raise TypeError(f"not a GENERATE query ({self.plan.kind})")
        self._default_graph_only()
        return self.engine.relation(self.plan.triples_sql(distinct)).to_arrow_reader(batch_size)

    def arrow(self, distinct: bool = False):
        """The generated triples of the default graph as a ``pyarrow.Table`` with columns s, p, o."""
        if self.plan.kind != "GENERATE":
            raise TypeError(f"not a GENERATE query ({self.plan.kind})")
        self._default_graph_only()
        return self.engine.relation(self.plan.triples_sql(distinct)).to_arrow_table()

    def quads(self, distinct: bool = False, batch_size: int = 65536) -> Iterator[Tuple[str, str, str, Optional[str]]]:
        """Iterate over the generated quads ``(s, p, o, g)`` as N-Triples term strings: the
        triples of the default graph with ``g`` None, then the quads of the named graphs (the
        graph name an IRI ``<...>`` or a blank node ``_:...``)."""
        for b in self.quad_batches(distinct, batch_size):
            yield from zip(*(b.column(i).to_pylist() for i in range(4)))

    def quad_batches(self, distinct: bool = False, batch_size: int = 65536):
        """The generated quads as a stream of ``pyarrow.RecordBatch`` (columns s, p, o, g)."""
        if self.plan.kind != "GENERATE":
            raise TypeError(f"not a GENERATE query ({self.plan.kind})")
        return self.engine.relation(self.plan.quads_sql(distinct)).to_arrow_reader(batch_size)

    def to_dataset(self):
        """The generated quads as an ``rdflib.Dataset`` (default graph and named graphs)."""
        import rdflib

        ds = rdflib.Dataset(default_union=False)
        for prefix, iri in self.query.prefixes.items():
            ds.bind(prefix, iri, override=False)
        data = "".join(f"{s} {p} {o}{' ' + g if g is not None else ''} .\n"
                       for s, p, o, g in self.quads(distinct=True))
        ds.parse(data=data, format="nquads")
        return ds

    def write(self, out, format: str = "nt", distinct: bool = False) -> int:
        """Write the output: N-Triples (bulk ``COPY``) or any rdflib format.
        Returns the number of triples written for N-Triples, else -1."""
        if self.plan.kind == "TEMPLATE":
            text = self.text()
            _write_text(out, text)
            return -1
        if self.plan.kind == "SELECT":
            return self._write_select(out, format)
        if self.plan.kind == "PERFORM":
            lines = [f"<{u}>({', '.join(a if a is not None else 'UNDEF' for a in args)})\n" for u, args in self.actions()]
            _write_text(out, "".join(lines))
            return len(lines)
        if format.lower() in QUAD_FORMATS:
            return self._write_quads(out, format.lower(), distinct)
        self._default_graph_only()
        if format in ("nt", "ntriples", "n-triples", "application/n-triples"):
            if isinstance(out, (str, os.PathLike)):
                path = os.fspath(out)
                from .terms import sql_string

                sql = (f"COPY (SELECT s || ' ' || p || ' ' || o || ' .' AS t FROM ({self.plan.triples_sql(distinct)})) "
                       f"TO {sql_string(path)} (FORMAT CSV, HEADER false, QUOTE '', ESCAPE '')")
                return self.engine.execute(sql).fetchall()[0][0]
            n = 0
            for s, p, o in self.triples(distinct):
                out.write(f"{s} {p} {o} .\n")
                n += 1
            return n
        g = self.to_graph()
        data = g.serialize(format=_rdflib_format(format))
        _write_text(out, data if isinstance(data, str) else data.decode("utf-8"))
        return len(g)

    def _write_quads(self, out, format: str, distinct: bool) -> int:
        """N-Quads (bulk ``COPY``; a triple of the default graph is a line of N-Triples) or
        TriG; returns the number of quads written for N-Quads, else -1."""
        if format in _NQ:
            line = "s || ' ' || p || ' ' || o || CASE WHEN g IS NULL THEN '' ELSE ' ' || g END || ' .'"
            if isinstance(out, (str, os.PathLike)):
                from .terms import sql_string

                sql = (f"COPY (SELECT {line} AS t FROM ({self.plan.quads_sql(distinct)})) "
                       f"TO {sql_string(os.fspath(out))} (FORMAT CSV, HEADER false, QUOTE '', ESCAPE '')")
                return self.engine.execute(sql).fetchall()[0][0]
            n = 0
            for s, p, o, g in self.quads(distinct):
                out.write(f"{s} {p} {o}{' ' + g if g is not None else ''} .\n")
                n += 1
            return n
        ds = self.to_dataset()
        data = ds.serialize(format="trig")
        _write_text(out, data if isinstance(data, str) else data.decode("utf-8"))
        return -1

    def to_graph(self):
        """The generated triples of the default graph as an ``rdflib.Graph`` (with a warning
        when the query generates quads in named graphs: see :meth:`to_dataset`)."""
        import rdflib

        g = rdflib.Graph()
        for prefix, iri in self.query.prefixes.items():
            g.bind(prefix, iri, override=False)
        data = "".join(f"{s} {p} {o} .\n" for s, p, o in self.triples(distinct=True))
        g.parse(data=data, format="nt")
        return g

    # -- TEMPLATE -----------------------------------------------------------------
    def text(self) -> str:
        if self.plan.kind != "TEMPLATE":
            raise TypeError(f"not a TEMPLATE query ({self.plan.kind})")
        row = self.engine.execute(self.plan.text_sql).fetchall()
        return row[0][0] if row and row[0][0] is not None else ""

    # -- PERFORM -----------------------------------------------------------------
    def actions(self) -> Iterator[Tuple[str, List[Optional[str]]]]:
        """PERFORM results: the action instances ``(u, [t1, ..., tk])`` in order, ``u`` the
        action IRI and ``ti`` N-Triples terms (None when unbound)."""
        if self.plan.kind != "PERFORM":
            raise TypeError(f"not a PERFORM query ({self.plan.kind})")
        for u, args in self.engine.execute(self.plan.actions_sql()).fetchall():
            yield u, list(args)

    # -- SELECT ------------------------------------------------------------------
    def rows(self) -> Iterator[Dict[str, Optional[str]]]:
        """SELECT results, one dict of N-Triples term strings per solution."""
        if self.plan.kind != "SELECT":
            raise TypeError(f"not a SELECT query ({self.plan.kind})")
        res = self.engine.execute(self.plan.select_sql)
        for r in res.fetchall():
            yield dict(zip(self.plan.select_vars, r))

    def _write_select(self, out, format: str) -> int:
        rows = list(self.rows())
        if format.lower() in ("csv", "tsv"):
            import csv

            sep = "\t" if format.lower() == "tsv" else ","
            f, close = _open_text(out)
            w = csv.writer(f, delimiter=sep)
            w.writerow(self.plan.select_vars)
            for r in rows:
                w.writerow([r[v] if r[v] is not None else "" for v in self.plan.select_vars])
            if close:
                f.close()
        else:
            _write_text(out, json.dumps({"head": {"vars": self.plan.select_vars},
                                         "results": {"bindings": [_json_binding(r) for r in rows]}}, indent=1))
        return len(rows)

    def close(self):
        self.engine.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def generate(query: Union[str, ast.Query], *, distinct: bool = False, **kwargs) -> Iterator[Tuple[str, str, str]]:
    """Run a GENERATE query and iterate over ``(s, p, o)`` N-Triples term strings.
    Keyword arguments are those of :class:`Execution`."""
    ex = Execution(query, **kwargs)
    try:
        yield from ex.triples(distinct=distinct)
    finally:
        ex.close()


def generate_quads(query: Union[str, ast.Query], *, distinct: bool = False,
                   **kwargs) -> Iterator[Tuple[str, str, str, Optional[str]]]:
    """Run a GENERATE query and iterate over ``(s, p, o, g)`` N-Triples term strings, ``g``
    None for the default graph.  Keyword arguments are those of :class:`Execution`."""
    ex = Execution(query, **kwargs)
    try:
        yield from ex.quads(distinct=distinct)
    finally:
        ex.close()


def to_dataset(query: Union[str, ast.Query], **kwargs):
    """Run a GENERATE query into an ``rdflib.Dataset``."""
    with Execution(query, **kwargs) as ex:
        return ex.to_dataset()


def generate_to(query: Union[str, ast.Query], out, *, format: str = "nt", distinct: bool = False, **kwargs) -> int:
    """Run a query and write its output to a path or file object (``format="nq"`` or
    ``"trig"`` for the named graphs of a query with GRAPH blocks)."""
    with Execution(query, **kwargs) as ex:
        return ex.write(out, format=format, distinct=distinct)


def to_graph(query: Union[str, ast.Query], **kwargs):
    """Run a GENERATE query into an ``rdflib.Graph``."""
    with Execution(query, **kwargs) as ex:
        return ex.to_graph()


def template(query: Union[str, ast.Query], **kwargs) -> str:
    """Run a TEMPLATE query and return the text."""
    with Execution(query, **kwargs) as ex:
        return ex.text()


def perform(query: Union[str, ast.Query], **kwargs) -> List[Tuple[str, List[Optional[str]]]]:
    """Run a PERFORM query; the action instances ``(u, [t1..tk])`` in order (nothing is
    executed: what an action does is up to the caller)."""
    with Execution(query, **kwargs) as ex:
        return list(ex.actions())


def select(query: Union[str, ast.Query], **kwargs) -> List[Dict[str, Optional[str]]]:
    """Run a SELECT query; terms are N-Triples strings (None when unbound)."""
    with Execution(query, **kwargs) as ex:
        return list(ex.rows())


# -- helpers ---------------------------------------------------------------------------


def _rdflib_format(fmt: str) -> str:
    f = fmt.lower()
    return {"ttl": "turtle", "turtle": "turtle", "nt": "nt", "n3": "n3", "xml": "xml", "rdf": "xml",
            "rdfxml": "xml", "jsonld": "json-ld", "json-ld": "json-ld", "trig": "trig", "nq": "nquads"}.get(f, f)


def _open_text(out):
    if isinstance(out, (str, os.PathLike)):
        return open(out, "w", encoding="utf-8", newline=""), True
    return out, False


def _write_text(out, text: str):
    f, close = _open_text(out)
    f.write(text)
    if close:
        f.close()


def _json_binding(row: Dict[str, Optional[str]]) -> dict:
    from .terms import parse_nt_term

    b = {}
    for k, v in row.items():
        if v is None:
            continue
        t = parse_nt_term(v)
        if isinstance(t, ast.IRI):
            b[k] = {"type": "uri", "value": t.value}
        elif isinstance(t, ast.BNode):
            b[k] = {"type": "bnode", "value": t.label}
        else:
            d = {"type": "literal", "value": t.lexical}
            if t.lang:
                d["xml:lang"] = t.lang
            elif t.datatype:
                d["datatype"] = t.datatype
            b[k] = d
    return b


def _load_dataset(conf: dict, base_dir: Optional[str]):
    g = conf.get("graph", "dataset/default.ttl")
    p = os.path.join(base_dir or "", g)
    named = [ng for ng in conf.get("namedgraphs") or [] if os.path.exists(os.path.join(base_dir or "", ng["path"]))]
    if not os.path.exists(p) and not named:
        return None
    if not named:
        return p  # loaded when a WHERE clause reads it: N-Triples natively, else by rdflib
    import rdflib

    ds = None
    if os.path.exists(p):
        ds = rdflib.Dataset(default_union=False)
        ds.default_context.parse(p, format="turtle")
    for ng in conf.get("namedgraphs") or []:
        np_ = os.path.join(base_dir or "", ng["path"])
        if os.path.exists(np_):
            if ds is None:
                ds = rdflib.Dataset(default_union=False)
            ds.graph(rdflib.URIRef(ng["uri"])).parse(np_, format="turtle")
    return ds
