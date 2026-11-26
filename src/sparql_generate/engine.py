"""Execution engine: a DuckDB session with the support macros, the registry
of Python UDFs, registered Arrow tables, temporary files and documents."""
from __future__ import annotations

import os
import tempfile
from typing import Callable, Dict, List, Optional, Tuple

import duckdb

from . import ast as A
from .documents import Document, DocumentSet
from .expressions import EXTRA_SUPPORT_SQL
from .raising import raise_error_of
from .terms import sql_string
from .termtypes import ANY_SQL_TYPE, SUPPORT_SQL, TType


def sql_type_of(t: TType) -> str:
    return {
        "iri": "VARCHAR", "bnode": "VARCHAR", "str": "VARCHAR", "lang": "VARCHAR", "typed": "VARCHAR",
        "int": "BIGINT", "double": "DOUBLE", "bool": "BOOLEAN", "json": "JSON", "any": ANY_SQL_TYPE,
    }[t.kind]


class Engine:
    """One execution context.  Not thread-safe; cheap to create."""

    @property
    def ANY_ARROW(self):
        """The Arrow type of the dynamic term representation (pyarrow is imported on use)."""
        import pyarrow as pa

        return pa.struct([("k", pa.int8()), ("v", pa.string()), ("d", pa.string())])

    def __init__(self, documents: Optional[DocumentSet] = None, base: Optional[str] = None,
                 named_queries: Optional[Dict[str, str]] = None, dataset=None,
                 threads: Optional[int] = None, memory_limit: Optional[str] = None,
                 preserve_order: bool = False, fail_on_missing: bool = False):
        self.con = duckdb.connect()
        if threads:
            self.con.execute(f"SET threads={int(threads)}")
        if memory_limit:
            self.con.execute(f"SET memory_limit={sql_string(memory_limit)}")
        if not preserve_order:
            self.con.execute("SET preserve_insertion_order=false")
        self.con.execute("SET lambda_syntax='DISABLE_SINGLE_ARROW'")
        self.con.execute(SUPPORT_SQL + EXTRA_SUPPORT_SQL)
        self.con.execute("CREATE TEMP TABLE __docs(id INTEGER, content VARCHAR)")
        self.ANY_TYPE = duckdb.struct_type({"k": "TINYINT", "v": "VARCHAR", "d": "VARCHAR"})
        self.documents = documents if documents is not None else DocumentSet()
        self.documents_by_uri: Dict[str, Document] = {}
        self.base = base
        self.named_queries: Dict[str, str] = dict(named_queries or {})
        self.dataset = dataset
        # a document, graph or named query that cannot be found is an error (DocumentNotFound)
        # instead of a warning and an unbound variable, an empty iterator or graph
        self.fail_on_missing = fail_on_missing
        # the prefix of an endless stream that iter:WebSocket and iter:MQTTSubscribe read
        self.stream_messages: Optional[int] = None
        self.stream_seconds: Optional[float] = None
        # WHERE graph patterns over the dataset are compiled natively (rule BGP); False forces
        # the rdflib fallback, which is kept for property paths, sub-SELECT, SERVICE and EXISTS
        self.native_where = True
        self._quads = None
        self._udfs: Dict[Tuple[str, object], str] = {}
        self._counter = 0
        self._tempfiles: List[str] = []
        self._doc_ids: Dict[int, int] = {}
        self.max_json = 4294967295
        self._resolve_registered = False
        self.sql_log: List[str] = []
        self.debug = False

    # -- naming ---------------------------------------------------------------
    def fresh(self, prefix: str = "t") -> str:
        self._counter += 1
        return f"{prefix}{self._counter}"

    # -- execution ---------------------------------------------------------------
    def execute(self, sql: str):
        if self.debug:
            self.sql_log.append(sql)
        try:
            return self.con.execute(sql)
        except duckdb.Error as ex:
            raised = raise_error_of(ex)
            if raised is not None:
                raise raised from None
            raise

    def relation(self, sql: str):
        """A DuckDB relation of a query, for Arrow output without the extra copy that a
        DB-API result makes."""
        if self.debug:
            self.sql_log.append(sql)
        return self.con.sql(sql)

    def describe(self, sql: str) -> List[Tuple[str, str]]:
        rows = self.execute("DESCRIBE " + sql).fetchall()
        return [(r[0], r[1]) for r in rows]

    # -- UDFs ----------------------------------------------------------------------
    def udf(self, name: str, fn: Callable, params: List, ret, key=None) -> str:
        """Register ``fn`` once under ``name`` (``key`` distinguishes closures with
        different captured parameters) and return the SQL function name."""
        k = (name, key)
        if k in self._udfs:
            return self._udfs[k]
        sqlname = name if (name, None) not in self._udfs.values() and name not in self._udfs.values() else self.fresh(name + "_")
        if sqlname in self._udfs.values():
            sqlname = self.fresh(name + "_")
        ptypes = [p if not isinstance(p, str) else duckdb.sqltype(p) for p in params]
        rtype = ret if not isinstance(ret, str) else duckdb.sqltype(ret)
        self.con.create_function(sqlname, fn, ptypes, rtype, null_handling="special")
        self._udfs[k] = sqlname
        return sqlname

    def list_struct_type(self, cols: List[Tuple[str, TType]]):
        return duckdb.list_type(duckdb.struct_type({c: sql_type_of(t) for c, t in cols}))

    def ensure_resolve_udf(self):
        """``sg_resolve(ref, base)``: RFC 3986 resolution for IRI() on relative references,
        registered on first use (registering a Python UDF imports numpy, ≈ 70 ms)."""
        if self._resolve_registered:
            return
        from .terms import resolve_iri

        def resolve(ref, base):
            if ref is None:
                return None
            try:
                return resolve_iri(ref, base)
            except Exception:
                return None

        self.con.create_function("sg_resolve", resolve, [duckdb.sqltype("VARCHAR"), duckdb.sqltype("VARCHAR")],
                                 duckdb.sqltype("VARCHAR"), null_handling="special")
        self._resolve_registered = True

    def ensure_decimal_udf(self):
        """``sg_decimal_udf(a, b, op)``: exact decimal ``*`` and ``/`` (Jena's BigDecimal
        semantics, see ``expressions.decimal_op``), registered on first use."""
        if getattr(self, "_decimal_registered", False):
            return
        from .expressions import decimal_op

        self.con.create_function("sg_decimal_udf", decimal_op, [duckdb.sqltype("VARCHAR")] * 3,
                                 duckdb.sqltype("VARCHAR"), null_handling="special")
        self._decimal_registered = True

    # -- data ------------------------------------------------------------------------
    def register_arrow(self, table) -> str:
        name = self.fresh("arrow")
        self.con.register(name, table)
        return name

    def temp_file(self, text: str, suffix: str = ".txt") -> str:
        fd, path = tempfile.mkstemp(suffix=suffix, prefix="sparql_generate_")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        self._tempfiles.append(path)
        return path

    def doc_sql_const(self, text: str) -> str:
        """SQL constant expression for a (possibly large) string stored once."""
        key = id(text)
        if key not in self._doc_ids:
            self._counter += 1
            self._doc_ids[key] = self._counter
            self.con.execute("INSERT INTO __docs VALUES (?, ?)", [self._counter, text])
        return f"(SELECT content FROM __docs WHERE id = {self._doc_ids[key]})"

    def document(self, uri: str, accept: Optional[str] = None) -> Optional[Document]:
        doc = self.documents.get(uri, accept)
        if doc is not None:
            self.documents_by_uri[uri] = doc
        elif self.fail_on_missing:
            from .expressions import DocumentNotFound

            raise DocumentNotFound(uri)
        return doc

    def named_query_text(self, uri: str, fetch: bool = True) -> Optional[str]:
        """The text of the named query (or FUNCTION) ``uri``: from ``named_queries``, the
        document set, a file relative to the base, or the Web unless ``fetch`` is False."""
        if uri in self.named_queries:
            p = self.named_queries[uri]
            if os.path.exists(p):
                with open(p, encoding="utf-8") as f:
                    return f.read()
            return p  # inline text
        if not fetch and uri not in self.documents.docs:
            saved = self.documents.allow_network
            self.documents.allow_network = False
            try:
                doc = self.documents._locate(uri, "application/vnd.sparql-generate")
            finally:
                self.documents.allow_network = saved
            return doc.text() if doc is not None else None
        doc = self.document(uri, "application/vnd.sparql-generate")
        if doc is not None:
            return doc.text()
        return None

    def close(self):
        for p in self._tempfiles:
            try:
                os.unlink(p)
            except OSError:
                pass
        self._tempfiles.clear()
        try:
            self.con.close()
        except Exception:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
