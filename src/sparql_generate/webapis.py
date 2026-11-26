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

import time
from typing import Dict, List, Optional, Tuple

from . import ast as A
from .termtypes import ANY_T, INT_T, NULL, STR_T, CExpr, string_literal_lex

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


def iter_sql(ctx, args: List[CExpr], nvars: int):
    """``iter:SQL(database, query, "column"...)`` as the Java engine 2.6.1 (``ITER_SQL``,
    :mod:`sqlsources`): the database a JDBC URL, an IRI or a string; the named columns (the
    label, exactly, else regardless of case), or all of them; each cell its natural RDF
    literal, NULL unbound; the rows one batch.  A name that no column has or that several
    have, or a query that the database rejects, is an error that stops the execution (as a
    RAISE, raised where the iterator is evaluated); a database that cannot be reached gives
    no row, with a warning, as a missing document (``fail_on_missing``: an error)."""
    from . import sqlsources as Q
    from .expressions import DocumentNotFound
    from .iterators import IterPlan, IteratorError, raising_plan
    from .terms import nt_literal

    if len(args) < 2:
        raise IteratorError("iter:SQL needs the JDBC URL of a database and a SQL query")
    db, sql_arg, names_args = args[0], args[1], list(args[2:])
    if not all(a.is_const for a in args):
        raise IteratorError("iter:SQL: the database, the query and the column names must be constants")
    if db.is_null or sql_arg.is_null:
        return IterPlan("table", "(SELECT NULL::VARCHAR AS c0 WHERE FALSE)", [("c0", ANY_T)])
    if db.type.kind not in ("iri", "str") and not (db.type.kind == "typed" and db.type.dt == A.XSD_STRING):
        raise IteratorError("iter:SQL: the database must be the JDBC URL of the database, an IRI or a string")
    if sql_arg.type.kind not in ("str",) and not (sql_arg.type.kind == "typed" and sql_arg.type.dt == A.XSD_STRING):
        raise IteratorError("iter:SQL: the query must be a string")
    names = []
    for a in names_args:
        if a.type.kind != "str":
            raise IteratorError(f"iter:SQL: the names of the columns must be strings, got {a.const!r}")
        names.append(str(a.const))
    url, sql = str(db.const), str(sql_arg.const)
    n = len(names)

    def plan_of(rows, ncols):
        import pyarrow as pa

        ncols = max(ncols, 1)
        cols = {f"c{i}": pa.array([r[i] if i < len(r) else None for r in rows], type=ctx.engine.ANY_ARROW)
                for i in range(ncols)}
        return IterPlan("table", f"(SELECT * FROM {ctx.engine.register_arrow(pa.table(cols))})",
                        [(f"c{i}", ANY_T) for i in range(ncols)])

    def raising(message, values):
        return raising_plan(ctx, [(f"c{i}", ANY_T) for i in range(max(n, nvars, 1))], message, values)

    try:
        target = Q.parse_jdbc(url, ctx.engine.documents.base_dir)
        labels, kinds, rows = Q.query(target, sql)
    except (ValueError, Q.SQLConnectError) as ex:
        if ctx.engine.fail_on_missing:
            raise DocumentNotFound(Q.redact(url))
        ctx.warnings.append(f"iter:SQL: can not connect to the data base {Q.redact(url)}: {ex}")
        return plan_of([], max(n, nvars))
    except Q.SQLQueryError as ex:
        return raising(f"SQL error: {ex}", [nt_literal(sql)])
    try:
        columns = Q.select_columns(labels, names)
    except LookupError as ex:
        return raising(ex.args[0], [nt_literal(ex.args[1]), nt_literal(sql)])
    if nvars > len(columns):
        ctx.warnings.append(f"iter:SQL: the query has {len(columns)} columns, {nvars} variables")
    return plan_of(Q.natural_rows(kinds, rows, columns), len(columns))


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
