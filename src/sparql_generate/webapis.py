"""HTTP and SQL: ``fun:HTTPGet`` (and ``HTTPPost``, ``HTTPPut``, ``HTTPDelete``, ``HTTPHead``),
``fun:HTTPExtractBody``, ``fun:HTTPExtractHeader``, ``fun:HTTPExtractResponseCode``,
``iter:HTTPGet`` and ``iter:SQL``, as the Java engine 2.2.3 defines them.

* ``fun:HTTPGet(iri [, headers [, body]])`` is the whole response as a string, as the Java
  engine writes it: the status line, one ``Name: value`` line per header, then an empty line
  and the body.  ``headers`` is a string of ``Name: value`` lines.  Any status (4xx, 5xx
  included) gives its response; a connection error is an error (unbound).  GET and HEAD
  responses are cached for the execution (one request per distinct IRI and headers).
* ``iter:HTTPGet(iri, seconds [, times])`` issues ``times`` GET requests, ``seconds`` apart,
  and binds each response body, a literal typed with its IANA media type, as its own batch
  (rule BATCH: the rest of the query is evaluated once per response).  Without ``times`` the
  Java iterator never ends; here that is refused.
* ``iter:SQL(<jdbc:...>, query)`` runs a query on a database and binds the cells of each row
  (integers, decimals, doubles, booleans, strings, dates and timestamps typed as XSD).
  ``jdbc:sqlite:<path>`` and ``jdbc:duckdb:<path>`` are supported (the Java engine knows
  MySQL only), the path relative to the base directory of the documents.

HTTP requests are made when the network is allowed (``allow_network``, ``--no-network``).
"""
from __future__ import annotations

import datetime as _dt
import decimal as _decimal
import os
import time
from typing import Dict, List, Optional, Tuple

from . import ast as A
from .terms import sql_string
from .termtypes import ANY_T, INT_T, NULL, STR_T, CExpr, lex, string_literal_lex

TIMEOUT = 60  # seconds per request


class HTTPError(Exception):
    pass


def http_request(method: str, iri: str, headers: Optional[str] = None, body: Optional[str] = None
                 ) -> Tuple[str, Optional[str], Optional[str]]:
    """``(full response text, body, media type)`` of an HTTP request, the response text in
    the Java engine's form (status line, headers, empty line, body)."""
    import urllib.error
    import urllib.request

    req = urllib.request.Request(iri, method=method, data=body.encode("utf-8") if body is not None else None)
    for line in (headers or "").split("\n"):
        if not line.strip():
            continue
        name, _, value = line.partition(":")
        req.add_header(name.strip(), value.strip())
    try:
        resp = urllib.request.urlopen(req, timeout=TIMEOUT)  # noqa: S310 - the query asks for it
    except urllib.error.HTTPError as ex:  # a 4xx or 5xx status is a response too
        resp = ex
    except (urllib.error.URLError, OSError, ValueError) as ex:
        raise HTTPError(str(ex))
    with resp:
        data = resp.read() if method != "HEAD" else b""
        status = getattr(resp, "status", None) or resp.getcode()
        reason = getattr(resp, "reason", "") or ""
        version = {10: "HTTP/1.0", 11: "HTTP/1.1"}.get(getattr(resp, "version", 11), "HTTP/1.1")
        hdrs = list(resp.headers.items())
        ctype = resp.headers.get_content_type() if resp.headers.get("Content-Type") else None
        charset = resp.headers.get_content_charset() or "utf-8"
    text_body = data.decode(charset, errors="replace")
    lines = [f"{version} {status} {reason}".rstrip()] + [f"{k}: {v}" for k, v in hdrs]
    full = "\n".join(lines) + "\n"
    if method != "HEAD":
        full += "\n" + text_body
    return full, text_body, ctype


# -- functions -----------------------------------------------------------------------------


def _check_network(ctx, what: str) -> bool:
    if not ctx.engine.documents.allow_network:
        ctx.warnings.append(f"{what}: the network is not allowed (--no-network), unbound")
        return False
    return True


def http_function(method: str):
    def compile_(ctx, args: List[CExpr]) -> CExpr:
        if not 1 <= len(args) <= 3 or args[0].is_null or args[0].type.kind not in ("iri", "any"):
            return NULL
        if not _check_network(ctx, f"fun:HTTP{method.title()}"):
            return NULL
        engine = ctx.engine
        cache: Dict[tuple, Optional[str]] = engine.__dict__.setdefault("_http_cache", {})

        def udf(iri, headers=None, body=None):
            if iri is None:
                return None
            key = (method, iri, headers, body)
            if method in ("GET", "HEAD") and key in cache:
                return cache[key]
            try:
                full = http_request(method, iri, headers, body)[0]
            except HTTPError:
                full = None
            if method in ("GET", "HEAD"):
                cache[key] = full
            return full

        from .iterators import fixed_arity

        n = len(args)
        name = engine.udf(f"sg_http_{method.lower()}{n}", fixed_arity(udf, n), ["VARCHAR"] * n, "VARCHAR")
        iri = args[0].sql if args[0].type.kind == "iri" else f"CASE WHEN ({args[0].sql}).k = 0 THEN ({args[0].sql}).v END"
        rest = [string_literal_lex(a) if not a.is_null else "NULL" for a in args[1:]]
        return CExpr(f"{name}({', '.join([iri] + rest)})", STR_T)

    return compile_


def http_extract_body(ctx, args: List[CExpr]) -> CExpr:
    (r,) = args
    s = string_literal_lex(r)
    return NULL if s == "NULL" else CExpr(f"CASE WHEN strpos({s}, chr(10) || chr(10)) > 0 THEN "
                                          f"substr({s}, strpos({s}, chr(10) || chr(10)) + 2) END", STR_T)


def http_extract_header(ctx, args: List[CExpr]) -> CExpr:
    (r,) = args
    s = string_literal_lex(r)
    if s == "NULL":
        return NULL
    first = f"strpos({s}, chr(10))"
    two = f"strpos({s}, chr(10) || chr(10))"
    return CExpr(f"CASE WHEN {first} = 0 THEN NULL WHEN {two} = 0 THEN substr({s}, {first} + 1) "
                 f"ELSE substr({s}, {first} + 1, {two} - {first} - 1) END", STR_T)


def http_extract_response_code(ctx, args: List[CExpr]) -> CExpr:
    (r,) = args
    s = string_literal_lex(r)
    return NULL if s == "NULL" else CExpr(f"TRY_CAST(regexp_extract({s}, '^\\S+ ([0-9]{{3}})', 1) AS BIGINT)", INT_T)


# -- iterators -------------------------------------------------------------------------------


def iter_http_get(ctx, args: List[CExpr], nvars: int):
    from .iterators import IterPlan, IteratorError, EMPTY

    if not 2 <= len(args) <= 3:
        raise IteratorError("iter:HTTPGet needs an IRI, a number of seconds and a number of calls")
    iri, secs = args[0], args[1]
    times = args[2] if len(args) == 3 else None
    if times is None:
        raise IteratorError("iter:HTTPGet without a number of calls never ends (a stream): give the number of calls")
    if not (secs.is_const and times.is_const and secs.type.kind == "int" and times.type.kind == "int"):
        raise IteratorError("iter:HTTPGet: the number of seconds and of calls must be integer constants")
    if int(secs.const) <= 0 or int(times.const) <= 0:
        return EMPTY
    if iri.is_null or not _check_network(ctx, "iter:HTTPGet"):
        return EMPTY
    if not iri.is_const:
        raise IteratorError("iter:HTTPGet: the IRI must be a constant")
    import pyarrow as pa

    rows = []
    for i in range(int(times.const)):
        if i:
            time.sleep(int(secs.const))
        try:
            _, body, ctype = http_request("GET", str(iri.const))
        except HTTPError as ex:
            ctx.warnings.append(f"iter:HTTPGet <{iri.const}>: {ex}")
            continue
        rows.append({"k": 2, "v": body, "d": A.IANA + ctype if ctype else None})
    tbl = pa.table({"c0": pa.array(rows, type=ctx.engine.ANY_ARROW)})
    # each response is a batch of its own (rule BATCH), as in the Java engine
    return IterPlan("table", f"(SELECT * FROM {ctx.engine.register_arrow(tbl)})", [("c0", ANY_T)], batch=1)


def _sql_cell(v) -> Optional[dict]:
    """A cell of a SQL result in the dynamic representation, typed as the Java engine does."""
    X = A.XSD
    if v is None:
        return None
    if isinstance(v, bool):
        return {"k": 2, "v": "true" if v else "false", "d": X + "boolean"}
    if isinstance(v, int):
        return {"k": 2, "v": str(v), "d": X + "integer"}
    if isinstance(v, float):
        from .pyeval import java_double

        # Jena's form of a double value: Double.toString, and e0 without an exponent
        if v != v or v in (float("inf"), float("-inf")):
            lexf = "NaN" if v != v else ("INF" if v > 0 else "-INF")
        else:
            lexf = java_double(v)
            lexf = lexf if "E" in lexf else lexf + "e0"
        return {"k": 2, "v": lexf, "d": X + "double"}
    if isinstance(v, _decimal.Decimal):
        from .expressions import decimal_lexical

        return {"k": 2, "v": decimal_lexical(v), "d": X + "decimal"}
    if isinstance(v, _dt.datetime):
        return {"k": 2, "v": v.isoformat(), "d": X + "dateTime"}
    if isinstance(v, _dt.date):
        return {"k": 2, "v": v.isoformat(), "d": X + "date"}
    if isinstance(v, (bytes, bytearray, memoryview)):
        return {"k": 2, "v": bytes(v).hex().upper(), "d": X + "hexBinary"}
    return {"k": 2, "v": str(v), "d": None}


def iter_sql(ctx, args: List[CExpr], nvars: int):
    from .iterators import IterPlan, IteratorError

    if len(args) != 2:
        raise IteratorError("iter:SQL needs the IRI of a database and a SQL query")
    db, query = args
    if not (db.is_const and db.type.kind == "iri" and query.is_const and query.const is not None):
        raise IteratorError("iter:SQL: the database IRI and the query must be constants")
    iri, sql = str(db.const), str(query.const)
    base_dir = ctx.engine.documents.base_dir or os.getcwd()
    for prefix in ("jdbc:sqlite:", "jdbc:duckdb:"):
        if iri.startswith(prefix):
            path = iri[len(prefix):]
            if path.startswith("//"):
                path = path[2:]
            path = path if os.path.isabs(path) or path == ":memory:" else os.path.join(base_dir, path)
            break
    else:
        raise IteratorError(f"iter:SQL: unsupported database <{iri}> (jdbc:sqlite:<path> or jdbc:duckdb:<path>)")
    try:
        if prefix == "jdbc:sqlite:":
            import sqlite3

            con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, detect_types=sqlite3.PARSE_DECLTYPES)
        else:
            import duckdb

            con = duckdb.connect(path, read_only=True)
        try:
            cur = con.execute(sql)
            names = [d[0] for d in cur.description] if cur.description else []
            rows = cur.fetchall()
        finally:
            con.close()
    except Exception as ex:  # noqa: BLE001 - a database error: no solution, as in Java
        ctx.warnings.append(f"iter:SQL <{iri}>: {ex}")
        rows, names = [], []
    import pyarrow as pa

    ncols = max(len(names), 1)
    cols = {f"c{i}": pa.array([_sql_cell(r[i]) if i < len(r) else None for r in rows], type=ctx.engine.ANY_ARROW)
            for i in range(ncols)}
    if nvars > ncols:
        ctx.warnings.append(f"iter:SQL: the query has {ncols} columns, {nvars} variables")
    tbl = pa.table(cols)
    return IterPlan("table", f"(SELECT * FROM {ctx.engine.register_arrow(tbl)})", [(f"c{i}", ANY_T) for i in range(ncols)])


def register_all():
    from .functions import FUNCTIONS
    from .iterators import ITERATORS

    for m in ("GET", "POST", "PUT", "DELETE", "HEAD"):
        FUNCTIONS[A.SG_FUN + "HTTP" + m.title()] = http_function(m)
    FUNCTIONS[A.SG_FUN + "HTTPExtractBody"] = http_extract_body
    FUNCTIONS[A.SG_FUN + "HTTPExtractHeader"] = http_extract_header
    FUNCTIONS[A.SG_FUN + "HTTPExtractResponseCode"] = http_extract_response_code
    ITERATORS[A.SG_ITER + "HTTPGet"] = iter_http_get
    ITERATORS[A.SG_ITER + "SQL"] = iter_sql
