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

import duckdb
from typing import Dict, Iterator, List, Optional, Tuple, Union

from . import ast
from .compiler import Compiler, Plan, union_of_batches
from .documents import DocumentSet
from .engine import Engine
from .expressions import CompileError, DocumentNotFound
from .lexer import ParseError
from .parser import parse
from .raising import RaiseError, raise_error_of
from .targets import TargetError

__all__ = [
    "parse", "generate", "generate_quads", "generate_to", "to_graph", "to_dataset", "template", "select", "perform",
    "Execution", "load_conf", "QuadsNotSupported", "QUAD_FORMATS", "TargetError",
    "ParseError", "CompileError", "DocumentNotFound", "RaiseError", "Engine", "DocumentSet", "__version__",
]
__version__ = "0.4.1"

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


_NT = ("nt", "ntriples", "n-triples", "application/n-triples")

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
                 stream_messages: Optional[int] = None, stream_seconds: Optional[float] = None,
                 sequential: bool = False):
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
        self._engine_args = (docset, dict(base=base, named_queries=named_queries, dataset=dataset, threads=threads,
                                          memory_limit=memory_limit, fail_on_missing=bool(fail_on_missing)),
                             debug, stream_messages, stream_seconds)
        self._new_engine()
        self.base_dir = base_dir
        # the quads of the named graphs left in the output by graphs_to_files(), or None
        self._named_sql: Optional[str] = None
        self._bindings = bindings
        # sequential batches (on request: the command line, generate_to in N-Triples): when
        # the output of the query is the union of its outputs on the batches of its first
        # iterator (union_of_batches), they are compiled and written one step after another,
        # so that the memory is that of one step. Only the first batch is compiled here;
        # write() in N-Triples compiles and writes the next ones. Any other output compiles
        # the query again on all the batches at once (_unbatch); ``plan`` is that of the
        # current step (--explain shows the first one)
        self.compiler.sequential = bool(sequential) and not bindings and union_of_batches(self.query) and \
            not ast.has_quad_patterns(self.query)
        self.plan: Plan = self.compiler.compile(self.query, bindings)
        self._sequential = (self.compiler.n_batches or 0) > 1
        self._written = False  # the batches were written: another output compiles them all again

    def _new_engine(self) -> None:
        docset, kw, debug, stream_messages, stream_seconds = self._engine_args
        self.engine = Engine(docset, **kw)
        self.engine.debug = debug
        self.engine.stream_messages, self.engine.stream_seconds = stream_messages, stream_seconds
        self.compiler = Compiler(self.engine)

    def _unbatch(self) -> None:
        """Compile the query on all the batches of its first iterator at once (an output
        other than write() in N-Triples, of a query whose batches were compiled one by one)."""
        if not self._sequential:
            return
        warnings = self.plan.warnings
        self.engine.close()
        self._new_engine()
        self.plan = self.compiler.compile(self.query, self._bindings)
        self.plan.warnings[:0] = [w for w in warnings if w not in self.plan.warnings]
        self._sequential = False

    def _next_batch(self, lines: int = 0) -> bool:
        """Compile the next step of a sequential evaluation; False after the last one. A step
        groups as many consecutive batches as fit in ``Compiler.step_rows`` rows, from the
        largest number of rows per batch seen so far (rows of the tables a step created, or
        lines it wrote, ``lines``), at most twice as many batches as the previous step."""
        c = self.compiler
        n = c.n_batches or 0
        if c.batch_hi >= n:
            return False
        rows = self.engine.con.execute(
            "SELECT table_name, estimated_size FROM duckdb_tables() WHERE temporary").fetchall()
        created = sum(sz for name, sz in rows if name not in c.protected)
        per_batch = max(created, lines) / max(1, c.batch_hi - c.batch_lo)
        self._per_batch = max(getattr(self, "_per_batch", 0), per_batch)
        prev = c.batch_hi - c.batch_lo
        self._step = max(1, min(2 * prev, int(c.step_rows // max(1.0, self._per_batch))))
        warnings = self.plan.warnings
        self.engine.drop_temp_tables(c.protected)
        c.batch_lo, c.batch_hi = c.batch_hi, min(n, c.batch_hi + self._step)
        self.plan = c.compile(self.query, self._bindings)
        warnings.extend(w for w in self.plan.warnings if w not in warnings)
        self.plan.warnings = warnings
        return True

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
        if self.has_quads and DEFAULT_GRAPH_ONLY not in self.plan.warnings and (
                self._named_sql is None or self.generates_named_graphs()):
            import warnings

            self.plan.warnings.append(DEFAULT_GRAPH_ONLY)
            warnings.warn(DEFAULT_GRAPH_ONLY, stacklevel=3)

    def triples(self, distinct: bool = False, batch_size: int = 65536) -> Iterator[Tuple[str, str, str]]:
        """Iterate over the generated triples of the default graph as N-Triples term strings
        (read by Arrow record batches: about twice as fast as fetching Python tuples)."""
        self._unbatch()
        for b in self.batches(distinct, batch_size):
            yield from zip(b.column(0).to_pylist(), b.column(1).to_pylist(), b.column(2).to_pylist())

    def batches(self, distinct: bool = False, batch_size: int = 65536):
        """The generated triples as a stream of ``pyarrow.RecordBatch`` (columns s, p, o of
        N-Triples term strings), computed as they are read."""
        self._unbatch()
        if self.plan.kind != "GENERATE":
            raise TypeError(f"not a GENERATE query ({self.plan.kind})")
        self._default_graph_only()
        return _reader(self.engine.relation(self._triples_sql(distinct)), batch_size)

    def arrow(self, distinct: bool = False):
        """The generated triples of the default graph as a ``pyarrow.Table`` with columns s, p, o."""
        self._unbatch()
        if self.plan.kind != "GENERATE":
            raise TypeError(f"not a GENERATE query ({self.plan.kind})")
        self._default_graph_only()
        return _raising(lambda: self.engine.relation(self._triples_sql(distinct)).to_arrow_table())

    def quads(self, distinct: bool = False, batch_size: int = 65536) -> Iterator[Tuple[str, str, str, Optional[str]]]:
        """Iterate over the generated quads ``(s, p, o, g)`` as N-Triples term strings: the
        triples of the default graph with ``g`` None, then the quads of the named graphs (the
        graph name an IRI ``<...>`` or a blank node ``_:...``)."""
        for b in self.quad_batches(distinct, batch_size):
            yield from zip(*(b.column(i).to_pylist() for i in range(4)))

    def quad_batches(self, distinct: bool = False, batch_size: int = 65536):
        """The generated quads as a stream of ``pyarrow.RecordBatch`` (columns s, p, o, g)."""
        self._unbatch()
        if self.plan.kind != "GENERATE":
            raise TypeError(f"not a GENERATE query ({self.plan.kind})")
        return _reader(self.engine.relation(self._quads_sql(distinct)), batch_size)

    def _triples_sql(self, distinct: bool = False) -> str:
        """The triples of the default graph of the output."""
        if self._named_sql is None:
            return self.plan.triples_sql(distinct)
        parts = [f"SELECT * FROM ({q})" for q in self.plan.triple_selects]
        parts.append(f"SELECT s, p, o FROM {self._named_sql} WHERE g IS NULL")
        body = "\nUNION ALL\n".join(parts)
        return f"SELECT DISTINCT s, p, o FROM ({body})" if distinct else body

    def _quads_sql(self, distinct: bool = False) -> str:
        """The quads of the output: the default graph (``g`` NULL) and the named graphs."""
        if self._named_sql is None:
            return self.plan.quads_sql(distinct)
        parts = [f"SELECT s, p, o, NULL::VARCHAR AS g FROM ({q})" for q in self.plan.triple_selects]
        parts.append(f"SELECT s, p, o, g FROM {self._named_sql}")
        body = "\nUNION ALL\n".join(parts)
        return f"SELECT DISTINCT s, p, o, g FROM ({body})" if distinct else body

    def generates_named_graphs(self) -> bool:
        """The output holds quads in named graphs (those that :meth:`graphs_to_files` wrote
        to files excepted)."""
        if self.plan.kind != "GENERATE" or not self.plan.quad_selects:
            return False
        sql = f"SELECT count(*) FROM (SELECT g FROM ({self._quads_sql()}) WHERE g IS NOT NULL LIMIT 1)"
        return _raising(lambda: self.engine.execute(sql).fetchall()[0][0]) > 0

    def graphs_to_files(self, targets: Union[None, str, Dict[str, dict]] = None,
                        output_dir: Optional[str] = None) -> Dict[str, int]:
        """Write the named graphs that name RDF files to these files, as the Java command line
        does with ``--graphs-to-files``: a named graph whose IRI is a relative path to an RDF
        file, resolved against the base (``GRAPH <out/people.ttl> { ... }``), is written to
        that file, in the folder of a ``file:`` base IRI, else in ``output_dir`` (default: the
        ``base_dir`` of the execution, else the current directory).  Format and compression
        come from the extension (``.nq``, ``.nt``, ``.ttl``, ``.trig``, ``.n3``, ``.jsonld``,
        ``.rdf``, ``.rj``; ``.gz``, ``.zip``, ``.bz2``, ``.xz``, ``.tar.gz``, ``.tar.xz``,
        ``.tar.bz2``), or from ``targets``, the description of the targets (a dict, or the path
        of its JSON file: ``{"dump1.nq": {"format": "N-Quads", "compression": "gzip",
        "encoding": "UTF-16"}}``).  ``GRAPH <dump.nq#g=IRI>`` names the graph ``IRI`` in
        ``dump.nq``.  Afterwards, the outputs of this execution hold the other graphs only.
        Returns the number of quads written to each file, by path.  A graph name that cannot
        be written (a path that leaves the folder...) raises :class:`targets.TargetError`."""
        self._unbatch()
        from .compiler import DEFAULT_GRAPH_NAMES_SQL
        from .targets import GraphTargets, read_descriptions, write_target

        if self.plan.kind != "GENERATE" or not self.plan.quad_selects or self._named_sql is not None:
            return {}
        descriptions = read_descriptions(targets) if isinstance(targets, (str, os.PathLike)) else targets
        folder = output_dir or self.base_dir or os.getcwd()
        resolver = GraphTargets(self.query.base or self.engine.base, folder, descriptions)
        named = "\nUNION ALL\n".join(
            f"SELECT s, p, o, CASE WHEN g IN ({DEFAULT_GRAPH_NAMES_SQL}) THEN NULL ELSE g END AS g FROM ({q})"
            for q in self.plan.quad_selects)
        table = self.engine.fresh("__named")
        self.engine.execute(f"CREATE TEMP TABLE {table} AS {named}")
        graphs = [r[0] for r in self.engine.execute(f"SELECT DISTINCT g FROM {table} WHERE g IS NOT NULL").fetchall()]
        routes = {}
        for g in sorted(graphs):
            r = resolver.resolve(g)
            if r is not None:
                routes[g] = r
        self._named_sql = table
        if not routes:
            return {}
        files = list(dict.fromkeys(f for f, _ in routes.values()))
        import pyarrow as pa

        mapping = pa.table({"g": list(routes), "f": [files.index(f) for f, _ in routes.values()],
                            "ig": [None if ig is None else f"<{ig}>" for _, ig in routes.values()]})
        mtable = self.engine.register_arrow(mapping)
        written = {}
        for k, f in enumerate(files):
            sql = (f"SELECT DISTINCT q.s, q.p, q.o, m.ig AS g FROM {table} AS q JOIN {mtable} AS m ON q.g = m.g "
                   f"WHERE m.f = {k}")
            written[f.path] = write_target(f, _reader(self.engine.relation(sql), 65536), self.query.prefixes)
        self._named_sql = f"(SELECT * FROM {table} WHERE g IS NULL OR g NOT IN (SELECT g FROM {mtable}))"
        return written

    def to_dataset(self):
        """The generated quads as an ``rdflib.Dataset`` (default graph and named graphs)."""
        self._unbatch()
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
        if format in _NT:
            if self._sequential and not self._written and not distinct and self._named_sql is None:
                self._written = True
                return self._write_sequential(out)
            self._unbatch()
            return self._write_nt(out, distinct)
        self._unbatch()
        g = self.to_graph()
        data = g.serialize(format=_rdflib_format(format))
        _write_text(out, data if isinstance(data, str) else data.decode("utf-8"))
        return len(g)

    def _write_sequential(self, out) -> int:
        """N-Triples, one step after another (sequential batches). To a stream, each step in
        turn. To a path, each step is copied into the destination, opened once, from a
        temporary file of the same compression (gzip members and zstd frames concatenate);
        a regular file is written under a temporary name and renamed at the end, so that an
        error leaves it as it was; ``/dev/null`` takes each step directly."""
        import shutil
        import tempfile

        if not isinstance(out, (str, os.PathLike)):
            n = 0
            while True:
                k = self._write_nt(out, False)
                n += k
                if not self._next_batch(k):
                    return n
        path = os.fspath(out)
        if os.path.abspath(path) == os.devnull:
            n = 0
            while True:
                k = self._write_nt(path, False)
                n += k
                if not self._next_batch(k):
                    return n
        ext = next((e for e in (".gz", ".zst") if path.endswith(e)), "")
        # /dev/stdout and /proc/self/fd/N may name a regular file (a redirection): written in
        # place, never replaced
        special = os.path.abspath(path).startswith(("/dev/", "/proc/"))
        regular = not special and (not os.path.exists(path) or os.path.isfile(path))
        if regular:
            fd, dest = tempfile.mkstemp(prefix="." + os.path.basename(path) + ".",
                                        dir=os.path.dirname(os.path.abspath(path)))
            os.close(fd)
        else:  # a FIFO, /dev/stdout: opened once, in append mode
            dest = path
        n = 0
        try:
            with open(dest, "wb" if regular else "ab") as dst:
                while True:
                    fd, part = tempfile.mkstemp(prefix="sparql_generate_", suffix=".nt" + ext)
                    os.close(fd)
                    try:
                        k = self._write_nt(part, False)
                        with open(part, "rb") as src:
                            shutil.copyfileobj(src, dst, 16 * 1024 * 1024)
                    finally:
                        os.remove(part)
                    n += k
                    if not self._next_batch(k):
                        break
            if regular:
                os.replace(dest, path)
        except BaseException:
            if regular and os.path.exists(dest):
                os.remove(dest)
            raise
        return n

    def _write_nt(self, out, distinct: bool) -> int:
        """The triples of the current plan in N-Triples (bulk ``COPY`` to a path)."""
        if isinstance(out, (str, os.PathLike)):
            from .terms import sql_string

            sql = (f"COPY (SELECT s || ' ' || p || ' ' || o || ' .' AS t FROM ({self._triples_sql(distinct)})) "
                   f"TO {sql_string(os.fspath(out))} (FORMAT CSV, HEADER false, QUOTE '', ESCAPE '')")
            return self.engine.execute(sql).fetchall()[0][0]
        n = 0
        for b in _reader(self.engine.relation(self._triples_sql(distinct)), 65536):
            for s, p, o in zip(b.column(0).to_pylist(), b.column(1).to_pylist(), b.column(2).to_pylist()):
                out.write(f"{s} {p} {o} .\n")
                n += 1
        return n

    def _write_quads(self, out, format: str, distinct: bool) -> int:
        """N-Quads (bulk ``COPY``; a triple of the default graph is a line of N-Triples) or
        TriG; returns the number of quads written for N-Quads, else -1."""
        self._unbatch()
        if format in _NQ:
            line = "s || ' ' || p || ' ' || o || CASE WHEN g IS NULL THEN '' ELSE ' ' || g END || ' .'"
            if isinstance(out, (str, os.PathLike)):
                from .terms import sql_string

                sql = (f"COPY (SELECT {line} AS t FROM ({self._quads_sql(distinct)})) "
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
        self._unbatch()
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
    kwargs.setdefault("sequential", format in _NT and not distinct)
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


def _raising(fn):
    """``fn()``, a DuckDB error that RAISE raised turned into a :class:`RaiseError`."""
    try:
        return fn()
    except duckdb.Error as ex:
        raised = raise_error_of(ex)
        if raised is not None:
            raise raised from None
        raise


def _reader(relation, batch_size: int):
    """The record batches of a relation, computed as they are read; an error that RAISE
    raised while they are computed is a :class:`RaiseError`."""
    import pyarrow as pa

    reader = _raising(lambda: relation.to_arrow_reader(batch_size))

    def batches():
        it = iter(reader)
        while True:
            try:
                b = next(it)
            except StopIteration:
                return
            except duckdb.Error as ex:
                raised = raise_error_of(ex)
                if raised is not None:
                    raise raised from None
                raise
            yield b

    return pa.RecordBatchReader.from_batches(reader.schema, batches())


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
