"""Iterator functions of the ``iter:`` namespace (``http://w3id.org/sparql-generate/iter/``).

An iterator clause ``ITERATOR iter:f(args) AS ?v1 ... ?vn`` is compiled to one
of two relational shapes (rule ITERATOR):

* ``table``: the arguments are constants, the iterator is a table-valued
  expression (``read_csv``, ``read_json_objects``, a registered Arrow table...)
  that is cross-joined with the current solutions;
* ``unnest``: the arguments depend on the current solution; the iterator is a
  list-valued SQL expression (native DuckDB or a Python UDF) that is unnested
  per row, keeping the association with its row (a lateral join).
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

from . import ast as A
from . import pyeval as P
from .documents import Document
from .functions import doc_lookup_sql, doc_text_of, resolve_doc_arg
from .terms import sql_string
from .termtypes import ANY_T, BOOL_T, INT_T, JSON_T, STR_T, TType, CExpr, lex, numeric, string_literal_lex, typed

ITER = A.SG_ITER


class IteratorError(Exception):
    pass


@dataclass
class IterPlan:
    kind: str  # 'table' | 'unnest'
    sql: str  # table: a FROM item "(SELECT ...)"; unnest: a LIST-valued expression
    columns: List[Tuple[str, TType]]  # output columns, in order
    positional: bool = False  # extra outputs position (int) and hasNext (bool) are defined
    lockstep: Optional[List[str]] = None  # unnest: several list expressions unnested together
    struct: bool = False  # unnest: the list holds STRUCTs whose fields are ``columns``
    batch: Optional[int] = None  # rows per batch (iter:CSV, iter:JSONSurfer): see Compiler.compile_iterator
    pos_column: Optional[str] = None  # table: the column of the position of the row in the document (batches)


ITERATORS: Dict[str, Callable] = {}


def register(name: str):
    def deco(fn):
        ITERATORS[ITER + name] = fn
        return fn

    return deco


def fixed_arity(fn, n: int):
    """A wrapper with exactly ``n`` positional parameters (DuckDB reads the Python signature)."""
    params = ", ".join(f"a{i}" for i in range(n))
    ns: dict = {"fn": fn}
    exec(f"def w({params}):\n    return fn({params})", ns)  # noqa: S102
    return ns["w"]


EMPTY = IterPlan("table", "(SELECT NULL::VARCHAR AS c0 WHERE FALSE)", [("c0", STR_T)])


def _all_const(args) -> bool:
    return all(a.is_const for a in args)


def _doc_of(e: CExpr) -> Optional[Document]:
    return e.const if e.is_const and isinstance(e.const, Document) else None


def _const_text(e: CExpr) -> Optional[str]:
    return doc_text_of(e)


# -- CSV ---------------------------------------------------------------------------


@register("CSV")
def iter_csv(ctx, args: List[CExpr], nvars: int) -> IterPlan:
    args = [resolve_doc_arg(ctx, args[0])] + list(args[1:]) if args else args
    if not args:
        raise IteratorError("iter:CSV needs at least the CSV document")
    doc, rest = args[0], list(args[1:])
    if doc.is_null:
        return EMPTY
    header, quote, delim = True, '"', ","
    if rest and rest[0].type.kind == "bool":
        if not _all_const(rest[:4]) or len(rest) < 4:
            raise IteratorError("iter:CSV: the dialect block (header, quote, delimiter, end of line) must be 4 constants")
        header = bool(rest[0].const)
        quote = str(rest[1].const)[:1]
        delim = str(rest[2].const)[:1]
        rest = rest[4:]
    batch = None
    if rest and rest[0].type.kind == "int":
        batch = _batch_size(rest[0])
        rest = rest[1:]
    names = None
    if rest:
        if not _all_const(rest):
            raise IteratorError("iter:CSV: column names must be constants")
        names = [str(r.const) for r in rest]
    text = _const_text(doc)
    if text is None and not doc.is_const:
        plan = _csv_unnest(ctx, doc, header, quote, delim, names, nvars)
        plan.batch = batch
        return plan
    d = _doc_of(doc)
    path = d.local_path() if d is not None else None
    if path is None:
        path = ctx.engine.temp_file(text if text is not None else "")
    import os

    small = os.path.getsize(path) < 32 * 1024 * 1024
    # univocity pads short rows with nulls; DuckDB's parallel reader cannot pad
    # when quoted fields span lines, so small files are read serially
    padding = "null_padding=true, parallel=false" if small else "parallel=true"
    opts = (f"header={'true' if header else 'false'}, delim={sql_string(delim)}, quote={sql_string(quote)}, "
            f"escape={sql_string(quote)}, all_varchar=true, {padding}, strict_mode=false, "
            f"max_line_size=16777216")
    src = f"read_csv({sql_string(path)}, {opts})"
    try:
        cols = ctx.engine.describe(f"SELECT * FROM {src} LIMIT 0")
    except Exception as ex:  # DuckDB could not sniff the dialect: read with Python's csv module
        ctx.warnings.append(f"iter:CSV: DuckDB could not read {path} ({str(ex).splitlines()[0]}); using the Python csv reader")
        src = _csv_python_table(ctx, path, header, quote, delim)
        cols = ctx.engine.describe(f"SELECT * FROM {src} LIMIT 0")
    colnames = [c for c, _ in cols]
    if names is not None:
        idx = [header_index(colnames, n) for n in names]
        missing = [n for n, j in zip(names, idx) if j is None]
        if missing:
            # as the Java engine: the variable of a column absent from the header is unbound
            ctx.warnings.append(f"iter:CSV: columns {missing} not found in the header {colnames}: unbound")
        sel = ", ".join(f'{_sql_ident(colnames[j])} AS c{i}' if j is not None else f"NULL::VARCHAR AS c{i}"
                        for i, j in enumerate(idx))
        out = [(f"c{i}", STR_T) for i in range(len(names))]
        if nvars > len(names) or batch:
            # named columns: the output after the last one is the position of the row (from 0,
            # the header excluded, across batches), as the Java engine 2.5.0
            sel += f", (row_number() OVER () - 1) AS c{len(names)}"
            out.append((f"c{len(names)}", INT_T))
            return IterPlan("table", f"(SELECT {sel} FROM {src})", out, batch=batch, pos_column=f"c{len(names)}")
    else:
        sel = ", ".join(f'{_sql_ident(n)} AS c{i}' for i, n in enumerate(colnames))
        out = [(f"c{i}", STR_T) for i in range(len(colnames))]
    return IterPlan("table", f"(SELECT {sel} FROM {src})", out, batch=batch)


def header_index(header: List[str], name: str) -> Optional[int]:
    """The index of the column ``name`` in a CSV header, or None: the first column of that
    name, else, as the univocity parser of the Java engine (``selectFields``), the first one
    equal to it ignoring case and the surrounding white space."""
    if name in header:
        return header.index(name)
    norm = name.strip().lower()
    for j, h in enumerate(header):
        if h is not None and h.strip().lower() == norm:
            return j
    return None


def _sql_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _csv_python_table(ctx, path: str, header: bool, quote: str, delim: str) -> str:
    import csv

    import pyarrow as pa

    with open(path, encoding="utf-8-sig", newline="") as f:
        rows = list(csv.reader(f, delimiter=delim, quotechar=quote))
    if header and rows:
        names, rows = rows[0], rows[1:]
    else:
        names = [f"column{i}" for i in range(max((len(r) for r in rows), default=0))]
    data = {n: pa.array([r[i] if i < len(r) and r[i] != "" else None for r in rows], type=pa.string())
            for i, n in enumerate(names)}
    return ctx.engine.register_arrow(pa.table(data))


def _csv_unnest(ctx, doc: CExpr, header: bool, quote: str, delim: str, names, nvars: int) -> IterPlan:
    import csv
    import io

    engine = ctx.engine
    n = len(names) if names else nvars

    def udf(text):
        text = text if text is None or not text.startswith("\x00") else engine.documents_by_uri[text[1:]].text()
        if text is None:
            return None
        rows = list(csv.reader(io.StringIO(text), delimiter=delim, quotechar=quote))
        if not rows:
            return []
        if header:
            hdr = rows[0]
            rows = rows[1:]
            idx = [header_index(hdr, nm) for nm in names] if names else list(range(n))
        else:
            idx = list(range(n))
        out = []
        for k, r in enumerate(rows):
            row = {f"c{i}": (r[j] if j is not None and j < len(r) and r[j] != "" else None) for i, j in enumerate(idx)}
            if names:
                row[f"c{n}"] = k
            out.append(row)
        return out

    cols = [(f"c{i}", STR_T) for i in range(n)]
    if names:
        cols.append((f"c{n}", INT_T))  # the position of the row, after the named columns
    # one UDF per dialect and selection of columns: the closure reads them, and so does the
    # struct type of its result
    key = (header, quote, delim, tuple(names) if names else None, n)
    name = engine.udf("sg_csv", udf, ["VARCHAR"], engine.list_struct_type(cols), key=key)
    return IterPlan("unnest", f"{name}({doc_lookup_sql(ctx, doc)})", cols, struct=True)


# -- CSVStrict (the Java engine 2.6.1) ----------------------------------------------------

_WS = "".join(chr(i) for i in range(33))  # univocity trims the characters <= ' '
_STRICT_FIELD = re.compile(
    r'[\x00-\x09\x0b\x0c\x0e-\x20]*(?:"((?:[^"]|"")*)"[\x00-\x09\x0b\x0c\x0e-\x20]*(?=,|\r\n|\n|\r|\Z)'
    r'|([^,\r\n]*))(,|\r\n|\n|\r|\Z)')


def strict_csv_records(text: str):
    """The records of a CSV text as univocity's parser reads them with the settings of the
    Java engine's ``iter:CSVStrict``: comma, double quote, ``""`` inside quotes, the white
    space around an unquoted value (and outside the quotes of a quoted one) removed, blank
    lines skipped, an empty cell (quoted or not) the empty string."""
    if '"' not in text:  # fast path: no quoted value
        for line in re.split(r"\r\n|\n|\r", text):
            if line.strip(_WS) == "":
                continue
            yield [f.strip(_WS) for f in line.split(",")]
        return
    pos, n, row, quoted_any = 0, len(text), [], False
    while pos <= n:
        m = _STRICT_FIELD.match(text, pos)
        q, u, term = m.group(1), m.group(2), m.group(3)
        if q is not None:
            row.append(q.replace('""', '"').replace("\r\n", "\n"))
            quoted_any = True
        else:
            row.append(u.strip(_WS))
        pos = m.end()
        if term == ",":
            continue
        if not (len(row) == 1 and row[0] == "" and not quoted_any):
            yield row
        row, quoted_any = [], False
        if term == "":
            break


def raising_plan(ctx, cols: List[Tuple[str, TType]], message: str, values: List[Optional[str]],
                 batch: Optional[int] = None) -> IterPlan:
    """An iterator whose evaluation is a data error (``RaiseException`` of the Java engine):
    a list evaluated for each input row that raises, so that, as in Java, nothing is raised
    when no solution reaches the iterator."""
    from .raising import raise_sql

    t = ctx.engine.list_struct_type(cols)
    return IterPlan("unnest", f"CAST(CASE WHEN {raise_sql(message, values)} IS NULL THEN NULL END AS {t})", cols,
                    struct=True, batch=batch)


class StrictCSVError(Exception):
    def __init__(self, message: str, values: List[Optional[str]]):
        super().__init__(message)
        self.message, self.values = message, values


def strict_csv_table(text: str, columns: List[str], doc_name: str):
    """``(rows, positions)`` of ``iter:CSVStrict``: for each record after the header, the
    values of the named columns (the first header cell equal to the name), then its
    position from 0.  A column absent from the header, a document without header, a record
    whose number of fields is not the header's: :class:`StrictCSVError`, the data error of
    the Java engine (``doc_name`` is the N-Triples of the IRI of the document, or of the
    string ``(inline document)``)."""
    from .terms import nt_literal

    def integer(i):
        return nt_literal(str(i), datatype=A.XSD + "integer")

    recs = strict_csv_records(text)
    header = next(recs, None)
    if header is None:
        raise StrictCSVError("The CSV document has no header", [doc_name])
    idx = []
    for c in columns:
        if c not in header:
            raise StrictCSVError("The CSV document has no column", [doc_name, nt_literal(c)])
        idx.append(header.index(c))
    fields = len(header)
    rows = []
    for k, r in enumerate(recs):
        if len(r) != fields:
            raise StrictCSVError("A row of the CSV document does not have as many fields as the header",
                                 [doc_name, integer(k), integer(len(r)), integer(fields)])
        rows.append([r[j] for j in idx])
    return rows


@register("CSVStrict")
def iter_csv_strict(ctx, args: List[CExpr], nvars: int) -> IterPlan:
    """``iter:CSVStrict(doc, [batch size], "col"...)`` (the Java engine 2.6.1, for RML-IO
    sources): the named columns of a CSV document with a header, then the position of the
    row; an empty cell is the empty string; a row with another number of fields than the
    header, or a column absent from it, is a data error that stops the execution, as a
    RAISE (raised where the iterator is evaluated)."""
    from .raising import DataError
    from .terms import nt_literal

    if not args:
        raise IteratorError("iter:CSVStrict needs the CSV document")
    is_iri = args[0].type.kind == "iri"
    iri = str(args[0].const) if is_iri and args[0].is_const and args[0].const is not None else None
    args = [resolve_doc_arg(ctx, args[0])] + list(args[1:])
    doc, rest = args[0], list(args[1:])
    if doc.is_null:
        return EMPTY
    batch = None
    if rest and rest[0].type.kind == "int":
        batch = _batch_size(rest[0])
        rest = rest[1:]
    if not _all_const(rest) or any(r.type.kind != "str" for r in rest):
        raise IteratorError("iter:CSVStrict: expecting the names of the columns (constant strings)")
    names = [str(r.const) for r in rest]
    n = len(names)
    cols = [(f"c{i}", STR_T) for i in range(n)] + [(f"c{n}", INT_T)]
    if nvars > n + 1:
        ctx.warnings.append(f"iter:CSVStrict: {n} columns and the position, {nvars} variables")
    inline = nt_literal("(inline document)")
    text = _const_text(doc)
    if text is not None:
        import pyarrow as pa

        try:
            rows = strict_csv_table(text, names, f"<{iri}>" if iri is not None else inline)
        except StrictCSVError as ex:
            return raising_plan(ctx, cols, ex.message, ex.values, batch)
        data = {f"c{i}": pa.array([r[i] for r in rows], type=pa.string()) for i in range(n)}
        data[f"c{n}"] = pa.array(list(range(len(rows))), type=pa.int64())
        name = ctx.engine.register_arrow(pa.table(data))
        return IterPlan("table", f"(SELECT * FROM {name})", cols, batch=batch, pos_column=f"c{n}")
    engine = ctx.engine

    def udf(t):
        uri = None
        if t is not None and t.startswith("\x00"):
            uri, t = t[1:], engine.documents_by_uri[t[1:]].text()
        if t is None:
            return None
        try:
            rows = strict_csv_table(t, names, f"<{uri}>" if uri is not None and is_iri else inline)
        except StrictCSVError as ex:
            raise DataError(ex.message, ex.values)
        return [{**{f"c{i}": r[i] for i in range(n)}, f"c{n}": k} for k, r in enumerate(rows)]

    name = engine.udf("sg_csv_strict", udf, ["VARCHAR"], engine.list_struct_type(cols), key=tuple(names))
    return IterPlan("unnest", f"{name}({doc_lookup_sql(ctx, doc)})", cols, struct=True, batch=batch)


@register("CSVHeaders")
def iter_csv_headers(ctx, args: List[CExpr], nvars: int) -> IterPlan:
    """The cells of the header line of a CSV document (the first record, as the Java
    engine's CsvMapReader reads it: an empty cell is the empty string).  On the IRI of a
    document, only its header line is read (the Java engine 2.6.0)."""
    args = [resolve_doc_arg(ctx, args[0])] + list(args[1:]) if args else args
    if not args:
        raise IteratorError("iter:CSVHeaders needs the CSV document")
    doc = args[0]
    if doc.is_null:
        return EMPTY
    d = _doc_of(doc)
    if d is not None or doc.is_const:
        path = d.path if d is not None and d.data is None and d.path and os.path.isfile(d.path) else None
        if path is not None:
            from .documents import open_text

            with open_text(path) as f:  # the header line only
                headers = csv_header(f)
        else:
            headers = csv_header(_const_text(doc) or "")
        if not headers:
            return EMPTY
        return IterPlan("table", "(SELECT unnest(" + sql_list(headers) + ") AS c0)", [("c0", STR_T)])
    engine = ctx.engine

    def udf(text):
        text = text if text is None or not text.startswith("\x00") else engine.documents_by_uri[text[1:]].text()
        return None if text is None else csv_header(text)

    name = engine.udf("sg_csv_header", udf, ["VARCHAR"], "VARCHAR[]")
    return IterPlan("unnest", f"{name}({doc_lookup_sql(ctx, doc)})", [("c0", STR_T)])


def csv_header(source) -> List[str]:
    """The cells of the first record of a CSV text or text file (a UTF-8 byte order mark
    removed), read without reading the rest."""
    import csv
    import io

    f = io.StringIO(source) if isinstance(source, str) else source
    try:
        row = next(csv.reader(f))
    except (StopIteration, csv.Error):
        return []
    if row and row[0].startswith("\ufeff"):
        row[0] = row[0][1:]
    return row


def sql_list(strings: List[str]) -> str:
    return "[" + ", ".join(sql_string(s) for s in strings) + "]"


# -- JSON --------------------------------------------------------------------------


def _json_limit(path: str) -> int:
    """``maximum_object_size`` for a JSON file: no object is larger than the file.  DuckDB
    allocates buffers of twice that size: the former constant 4 GiB made it ask for 7.9 GiB
    on any file (refused under a memory limit, and a hazard on a shared machine)."""
    import os

    return min(4294967295, max(1 << 24, os.path.getsize(path) + 1))


def _json_source_sql(ctx, doc: CExpr) -> str:
    """SQL expression (JSON) for a constant document."""
    d = _doc_of(doc)
    path = d.local_path() if d is not None else None
    if path is not None:
        return f"(SELECT json FROM read_json_objects({sql_string(path)}, format='unstructured', maximum_object_size={_json_limit(path)}))"
    return f"CAST({ctx.engine.doc_sql_const(_const_text(doc))} AS JSON)"


@register("JSONPath")
def iter_jsonpath(ctx, args: List[CExpr], nvars: int) -> IterPlan:
    args = [resolve_doc_arg(ctx, args[0])] + list(args[1:]) if args else args
    if len(args) < 2:
        raise IteratorError("iter:JSONPath needs a document and a JSONPath expression")
    doc, path, aux = args[0], args[1], list(args[2:])
    if doc.is_null or path.is_null:
        return EMPTY
    engine = ctx.engine
    cols = [("c0", JSON_T)]
    if path.is_const and path.const is not None and doc.is_const:
        p = str(path.const)
        text = _const_text(doc)
        d = _doc_of(doc)
        need_pos = nvars > 1 + len(aux)
        if p == "$":
            elems = f"(SELECT {_json_source_sql(ctx, doc)} AS c0)"
        elif p == "$[*]" and d is not None and d.local_path() and not need_pos:
            elems = f"(SELECT json AS c0 FROM read_json_objects({sql_string(d.local_path())}, format='array', maximum_object_size={_json_limit(d.local_path())}))"
        elif P.jsonpath_is_native(p):
            src = _json_source_sql(ctx, doc)
            if P.jsonpath_is_definite(p):
                lst = f"CASE WHEN json_extract(__j, {sql_string(p)}) IS NULL THEN [] ELSE [json_extract(__j, {sql_string(p)})] END"
            else:
                lst = f"CAST(json_extract(__j, {sql_string(p)}) AS JSON[])"
            elems = (f"(SELECT unnest(__l) AS c0, generate_subscripts(__l, 1) - 1 AS __pos, len(__l) AS __n "
                     f"FROM (SELECT {lst} AS __l FROM (SELECT {src} AS __j)))")
        else:
            try:
                values = P.jsonpath_all(P.parse_json(text), p)
            except Exception:
                values = []
            import pyarrow as pa

            tbl = pa.table({"c0": pa.array([P.json_text(v) for v in values], type=pa.string()),
                            "__pos": pa.array(range(len(values)), type=pa.int64()),
                            "__n": pa.array([len(values)] * len(values), type=pa.int64())})
            name = engine.register_arrow(tbl)
            elems = f"(SELECT CAST(c0 AS JSON) AS c0, __pos, __n FROM {name})"
        has_pos = "__pos" in elems
        # a JSON document is scanned by one thread, and so is everything computed in the same
        # pipeline: the elements are written once to a temporary table, whose scan (and the
        # extraction of the auxiliary paths, the templates...) runs in parallel
        tname = engine.fresh("json")
        engine.execute(f"CREATE TEMP TABLE {tname} AS SELECT * FROM {elems}")
        # DuckDB extracts several paths at once only without wildcards: the definite ones
        native = [i for i, a in enumerate(aux) if a.is_const and a.const is not None
                  and P.jsonpath_is_native(str(a.const)) and P.jsonpath_is_definite(str(a.const))]
        multi = len(native) >= 2  # one parse of the element for all the native paths
        sel = ["c0"]
        for i, a in enumerate(aux):
            if multi and i in native:
                sel.append(f"__x[{native.index(i) + 1}] AS c{i + 1}")
            else:
                sel.append(f"{_json_aux_sql(ctx, 'c0', a)} AS c{i + 1}")
            cols.append((f"c{i + 1}", JSON_T))
        if has_pos:
            sel += ["__pos", "__n"]
        src = tname
        if multi:
            paths = ", ".join(sql_string(str(aux[i].const)) for i in native)
            src = f"(SELECT *, json_extract(c0, [{paths}]) AS __x FROM {tname})"
        return IterPlan("table", f"(SELECT {', '.join(sel)} FROM {src})", cols, positional=has_pos)
    # per-row evaluation
    src = doc.sql if doc.type.kind == "json" else lex(doc)
    if path.is_const and path.const is not None and P.jsonpath_is_native(str(path.const)):
        p = str(path.const)
        if P.jsonpath_is_definite(p):
            lst = f"CASE WHEN json_extract({src}, {sql_string(p)}) IS NULL THEN [] ELSE [json_extract({src}, {sql_string(p)})] END"
        else:
            lst = f"CAST(json_extract({src}, {sql_string(p)}) AS JSON[])"
    else:
        def udf(d, p):
            d = d if d is None or not d.startswith("\x00") else engine.documents_by_uri[d[1:]].text()
            if d is None or p is None:
                return None
            try:
                return [P.json_text(v) for v in P.jsonpath_all(P.parse_json(d), p)]
            except Exception:
                return None

        name = engine.udf("sg_jsonpath_all", udf, ["VARCHAR", "VARCHAR"], "JSON[]")
        lst = f"{name}({doc_lookup_sql(ctx, doc)}, {string_literal_lex(path)})"
    if aux:
        # wrap elements with their auxiliary values in a struct list
        fields = ["__e := __e"]
        for i, a in enumerate(aux):
            fields.append(f"c{i + 1} := {_json_aux_sql(ctx, '__e', a)}")
            cols.append((f"c{i + 1}", JSON_T))
        lst = f"list_transform({lst}, lambda __e: struct_pack(c0 := __e, {', '.join(fields[1:])}))"
        return IterPlan("unnest", lst, cols, positional=True, struct=True)
    return IterPlan("unnest", lst, cols, positional=True)


def _json_aux_sql(ctx, elem_sql: str, a: CExpr) -> str:
    """An auxiliary JSONPath query on an element, as the Java engine's
    ``context.limit(1).read(path)`` (checked with its fix-2.6.1 JAR): a definite path gives its
    value; an indefinite one ``[]`` when nothing matches, the value of its first match when it
    is a scalar, and nothing (unbound) when it is an object or an array."""
    if a.is_const and a.const is not None and P.jsonpath_is_native(str(a.const)):
        if P.jsonpath_is_definite(str(a.const)):
            return f"json_extract({elem_sql}, {a.sql})"
        x = f"json_extract({elem_sql}, {a.sql})"
        return (f"CASE WHEN len({x}) = 0 THEN '[]'::JSON WHEN json_type({x}[1]) IN ('OBJECT', 'ARRAY') THEN NULL "
                f"ELSE {x}[1] END")
    engine = ctx.engine

    def udf(d, p):
        if d is None or p is None:
            return None
        try:
            doc = P.parse_json(d)
            if not P.jsonpath_is_definite(p):
                first = P.jsonpath_all(doc, p)[:1]
                if not first:
                    return "[]"
                return None if isinstance(first[0], (dict, list)) else P.json_text(first[0])
            return P.json_text(P.jsonpath_first(doc, p))
        except Exception:
            return None

    name = engine.udf("sg_jsonpath", udf, ["VARCHAR", "VARCHAR"], "JSON")
    return f"{name}(CAST({elem_sql} AS VARCHAR), {string_literal_lex(a)})"


@register("JSONListKeys")
def iter_json_list_keys(ctx, args: List[CExpr], nvars: int) -> IterPlan:
    args = [resolve_doc_arg(ctx, args[0])] + list(args[1:]) if args else args
    (doc,) = args
    cols = [("c0", STR_T)]
    if doc.is_null:
        return EMPTY
    if doc.is_const:
        text = _const_text(doc)
        try:
            keys = list(P.parse_json(text).keys())
        except Exception:
            keys = []
        return IterPlan("table", f"(SELECT unnest({sql_list(keys)}) AS c0)", cols)
    src = doc.sql if doc.type.kind == "json" else lex(doc)
    return IterPlan("unnest", f"json_keys({src})", cols)


@register("JSONSurfer")
def iter_json_surfer(ctx, args, nvars):
    """``iter:JSONSurfer(doc, path, [batch size], paths...)``: iter:JSONPath read in batches."""
    batch = None
    if len(args) > 2 and args[2].type.kind == "int":
        batch = _batch_size(args[2])
        args = list(args[:2]) + list(args[3:])
    # a batch is a range of positions in the document: the elements carry their position
    plan = iter_jsonpath(ctx, args, max(nvars, len(args)) if batch is not None else nvars)
    if batch is not None:
        plan = IterPlan(plan.kind, plan.sql, plan.columns, plan.positional, plan.lockstep, plan.struct, batch)
    return plan


def _batch_size(e: CExpr) -> Optional[int]:
    """The rows per batch of a batched iterator: a positive constant, else one batch."""
    if e.is_const and e.const is not None and int(e.const) > 0:
        return int(e.const)
    return None


@register("CBOR")
def iter_cbor(ctx, args: List[CExpr], nvars: int) -> IterPlan:
    args = [resolve_doc_arg(ctx, args[0])] + list(args[1:]) if args else args
    doc, path, aux = args[0], args[1], list(args[2:])
    text = _const_text(doc)
    if text is None or not path.is_const:
        raise IteratorError("iter:CBOR needs a constant document and path")
    values = P.jsonpath_all(P.cbor_to_json(text), str(path.const))
    import pyarrow as pa

    tbl = pa.table({"c0": pa.array([P.json_text(v) for v in values], type=pa.string())})
    name = ctx.engine.register_arrow(tbl)
    cols = [("c0", JSON_T)]
    sel = ["CAST(c0 AS JSON) AS c0"]
    for i, a in enumerate(aux):
        sel.append(f"{_json_aux_sql(ctx, 'CAST(c0 AS JSON)', a)} AS c{i + 1}")
        cols.append((f"c{i + 1}", JSON_T))
    return IterPlan("table", f"(SELECT {', '.join(sel)} FROM {name})", cols)


# -- XML / HTML (Python) ------------------------------------------------------------


def _xpath_rows(text: str, path: str, aux: List[str]) -> List[list]:
    root = P.parse_xml(text)
    out = []
    for node in P.xpath_nodes(root, path):
        row = [P.xml_node_term(node)]
        element = not isinstance(node, (str, int, float, bool))
        sub = None
        for a in aux:
            if not a.startswith("/"):
                # a relative sub-query (the Java engine 2.6.1): from the node in its document,
                # so that the parent and ancestor axes reach outside it; its first node
                row.append(_xpath_relative(node, a) if element else None)
                continue
            if sub is None and element:
                sub = P.xpath_sub_root(node)
            try:
                nodes = P.xpath_nodes(sub, a) if sub is not None else []
                row.append(P.xml_node_term(nodes[0]) if nodes else None)
            except Exception:
                row.append(None)
        out.append(row)
    return out


def _xpath_relative(node, path: str):
    """The first node that ``path`` selects from ``node`` in its document, as a term (None
    when nothing is selected, or the result is not a node set, as ``XPathConstants.NODE``)."""
    try:
        try:
            res = node.xpath(path, namespaces=P.namespaces(node))
        except Exception:  # noqa: BLE001 - a prefix declared below the root
            res = node.xpath(path, namespaces=P.namespaces(node, everywhere=True))
    except Exception:  # noqa: BLE001 - not an XPath expression: unbound, as in Java
        return None
    if not isinstance(res, list) or not res:
        return None
    return P.xml_node_term(res[0])


def _css_rows(text: str, selector: str, aux: List[str]) -> List[list]:
    from lxml import etree

    root = P.parse_html(text)
    out = []
    for el in P.css_select(root, selector):
        row = [P.any_lit(etree.tostring(el, method="html", encoding="unicode", with_tail=False), P.HTML_DT)]
        for a in aux:
            try:
                row.append(P.css_value(el, a))
            except Exception:
                row.append(None)
        out.append(row)
    return out


def _python_rows_iterator(ctx, args: List[CExpr], nvars: int, rows_fn, label: str) -> IterPlan:
    args = [resolve_doc_arg(ctx, args[0])] + list(args[1:])
    doc, path, aux = args[0], args[1], list(args[2:])
    if doc.is_null or path.is_null:
        return EMPTY
    ncols = 1 + len(aux)
    cols = [(f"c{i}", ANY_T) for i in range(ncols)]
    engine = ctx.engine
    text = _const_text(doc)
    if text is not None and path.is_const and _all_const(aux):
        try:
            rows = rows_fn(text, str(path.const), [str(a.const) for a in aux])
        except Exception:
            rows = []
        import pyarrow as pa

        data = {f"c{i}": pa.array([r[i] for r in rows], type=engine.ANY_ARROW) for i in range(ncols)}
        data["__pos"] = pa.array(range(len(rows)), type=pa.int64())
        data["__n"] = pa.array([len(rows)] * len(rows), type=pa.int64())
        name = engine.register_arrow(pa.table(data))
        return IterPlan("table", f"(SELECT * FROM {name})", cols, positional=True)

    def udf(d, p, *aux_paths):
        d = d if d is None or not d.startswith("\x00") else engine.documents_by_uri[d[1:]].text()
        if d is None or p is None:
            return None
        try:
            rows = rows_fn(d, p, [a for a in aux_paths])
        except Exception:
            return None
        return [{f"c{i}": r[i] for i in range(ncols)} for r in rows]

    name = engine.udf(f"sg_{label}_iter{ncols}", fixed_arity(udf, 2 + len(aux)), ["VARCHAR"] * (2 + len(aux)), engine.list_struct_type(cols))
    argsql = [doc_lookup_sql(ctx, doc), string_literal_lex(path)] + [string_literal_lex(a) for a in aux]
    return IterPlan("unnest", f"{name}({', '.join(argsql)})", cols, positional=True, struct=True)


@register("XPath")
def iter_xpath(ctx, args, nvars):
    return _python_rows_iterator(ctx, args, nvars, _xpath_rows, "xpath")


@register("CSSPath")
def iter_csspath(ctx, args, nvars):
    return _python_rows_iterator(ctx, args, nvars, _css_rows, "css")


@register("GeoJSON")
def iter_geojson(ctx, args: List[CExpr], nvars: int) -> IterPlan:
    args = [resolve_doc_arg(ctx, args[0])] + list(args[1:]) if args else args
    (doc,) = args
    cols = [("c0", ANY_T), ("c1", ANY_T)]
    if doc.is_null:
        return EMPTY
    engine = ctx.engine
    text = _const_text(doc)
    if text is not None:
        rows = P.geojson_features(text)
        import pyarrow as pa

        tbl = pa.table({"c0": pa.array([r[0] for r in rows], type=engine.ANY_ARROW),
                        "c1": pa.array([r[1] for r in rows], type=engine.ANY_ARROW)})
        return IterPlan("table", f"(SELECT * FROM {engine.register_arrow(tbl)})", cols)

    def udf(d):
        d = d if d is None or not d.startswith("\x00") else engine.documents_by_uri[d[1:]].text()
        if d is None:
            return None
        try:
            return [{"c0": r[0], "c1": r[1]} for r in P.geojson_features(d)]
        except Exception:
            return None

    name = engine.udf("sg_geojson", udf, ["VARCHAR"], engine.list_struct_type(cols))
    return IterPlan("unnest", f"{name}({doc_lookup_sql(ctx, doc)})", cols, struct=True)


# -- strings and numbers (native SQL) ------------------------------------------------


@register("Split")
def iter_split(ctx, args: List[CExpr], nvars: int) -> IterPlan:
    s, sep = args
    ls = lex(s)
    lp = string_literal_lex(sep)
    return IterPlan("unnest", f"sg_split({ls}, {lp})", [("c0", STR_T)])


@register("regex")
def iter_regex(ctx, args: List[CExpr], nvars: int) -> IterPlan:
    s, pat, groups = args[0], args[1], list(args[2:])
    ls = lex(s)
    lp = string_literal_lex(pat)
    pat_sql = f"('(?m)' || {lp})"
    if groups:
        if not _all_const(groups):
            raise IteratorError("iter:regex: group numbers must be constants")
        gs = [int(g.const) for g in groups]
    else:
        if not pat.is_const:
            raise IteratorError("iter:regex: the pattern must be a constant when no group is selected")
        gs = list(range(re.compile(str(pat.const)).groups + 1))
    lists = [f"regexp_extract_all({ls}, {pat_sql}, {g})" for g in gs]
    cols = [(f"c{i}", STR_T) for i in range(len(gs))]
    return IterPlan("unnest", lists[0], cols, lockstep=lists)


@register("for")
def iter_for(ctx, args: List[CExpr], nvars: int) -> IterPlan:
    start, incr, stop = args
    if all(a.type.kind == "int" for a in args):
        return IterPlan("unnest", f"CASE WHEN {incr.sql} = 0 THEN NULL ELSE range({start.sql}, {stop.sql}, {incr.sql}) END", [("c0", INT_T)])
    ns, ni, nst = numeric(start), numeric(incr), numeric(stop)
    lst = (f"CASE WHEN {ni} = 0 THEN NULL ELSE list_transform(range(0, CAST(ceil(({nst} - {ns}) / {ni}) AS BIGINT)), "
           f"lambda i: sg_decimal_fmt(CAST({ns} + i * {ni} AS VARCHAR))) END")
    return IterPlan("unnest", lst, [("c0", typed(A.XSD_DECIMAL))])


@register("defaultGraphNamespaces")
def iter_default_graph_namespaces(ctx, args, nvars):
    raise IteratorError("iter:defaultGraphNamespaces is not supported")


# -- SPARQL ------------------------------------------------------------------------


@register("SPARQL")
def iter_sparql(ctx, args: List[CExpr], nvars: int) -> IterPlan:
    """``iter:SPARQL(doc, query, "var"...)``: the solutions of a SPARQL 1.1 SELECT query on an
    RDF document (Turtle, N-Triples, RDF/XML... by its media type or its extension, Turtle
    otherwise; or a string, the document in Turtle), one row per solution: the values of the
    variables named (by default the projected ones; unbound when a solution does not bind
    one), then the position of the solution, from 0 (the Java engine 2.6.1, ``ITER_SPARQL``).
    A document, query or variable name that is not one: a warning and no row."""
    args = [resolve_doc_arg(ctx, args[0])] + list(args[1:]) if args else args
    if len(args) < 2 or args[0].is_null or args[1].is_null:
        if len(args) < 2:
            raise IteratorError("iter:SPARQL needs an RDF document and a SELECT query")
        return EMPTY
    if not _all_const(args):
        raise IteratorError("iter:SPARQL: the document, the query and the variable names must be constants")
    doc, query = args[0], args[1]
    if query.type.kind not in ("str", "typed") or not isinstance(query.const, str):
        ctx.warnings.append("iter:SPARQL: expecting an RDF document and a SELECT query")
        return EMPTY
    names = []
    for a in args[2:]:
        if a.type.kind != "str" or a.const is None:
            ctx.warnings.append(f"iter:SPARQL: the names of the variables must be strings, got {a.const!r}")
            return EMPTY
        names.append(str(a.const))
    d = _doc_of(doc)
    try:
        if d is not None:
            fmt = _rdf_format(d)
            rows, vars_ = _sparql_rows(d.text(), fmt, d.uri, str(query.const), names)
        else:
            rows, vars_ = _sparql_rows(_const_text(doc) or "", "TURTLE", None, str(query.const), names)
    except ValueError as ex:
        ctx.warnings.append(f"iter:SPARQL: {ex}")
        return EMPTY
    import pyarrow as pa

    n = len(vars_)
    cols = {f"c{i}": pa.array([r[i] for r in rows], type=ctx.engine.ANY_ARROW) for i in range(n)}
    cols[f"c{n}"] = pa.array(list(range(len(rows))), type=pa.int64())
    out = [(f"c{i}", ANY_T) for i in range(n)] + [(f"c{n}", INT_T)]
    return IterPlan("table", f"(SELECT * FROM {ctx.engine.register_arrow(pa.table(cols))})", out)


def _rdf_format(d: Document) -> str:
    """The syntax of an RDF document, by its media type, else its extension, else Turtle (as
    ``RDFLanguages.contentTypeToLang``, then ``filenameToLang`` with Turtle by default)."""
    from .ntriples import MEDIA_FORMATS, OXIGRAPH_FORMATS

    mt = d.mediatype.split(";")[0].strip().lower() if d.mediatype else None
    if mt in MEDIA_FORMATS:
        return MEDIA_FORMATS[mt]
    from urllib.parse import urlparse

    ext = os.path.splitext(urlparse(d.uri).path)[1].lower() or os.path.splitext(d.path or "")[1].lower()
    return OXIGRAPH_FORMATS.get(ext, "TURTLE")


_RDFLIB_FORMATS = {"TURTLE": "turtle", "TRIG": "trig", "RDF_XML": "xml", "N3": "n3", "N_TRIPLES": "nt",
                   "N_QUADS": "nquads"}


def _sparql_rows(text: str, fmt: str, base: Optional[str], query: str, names: List[str]):
    """``(rows, variables)``: the solutions of ``query`` on the RDF document ``text``, each a
    list of dynamic terms ``{k, v, d}`` (None when unbound); by pyoxigraph when installed (it
    keeps lexical forms, as Jena), else rdflib (literals not normalised)."""
    try:
        import pyoxigraph as ox
    except ImportError:
        ox = None
    if ox is not None:
        store = ox.Store()
        try:
            store.load(input=text.encode("utf-8"), format=getattr(ox.RdfFormat, fmt), base_iri=base)
        except (SyntaxError, OSError, ValueError) as ex:
            raise ValueError(f"not an RDF document: {ex}")
        try:
            res = store.query(query)
        except (SyntaxError, ValueError, OSError) as ex:
            raise ValueError(f"not a SPARQL query: {ex}")
        if not isinstance(res, ox.QuerySolutions):
            raise ValueError(f"not a SELECT query: {query}")
        vars_ = names or [v.value for v in res.variables]

        def conv(t):
            if t is None:
                return None
            if isinstance(t, ox.NamedNode):
                return {"k": 0, "v": t.value, "d": None}
            if isinstance(t, ox.BlankNode):
                return {"k": 1, "v": "D" + t.value, "d": None}
            if isinstance(t, ox.Literal):
                dt = t.datatype.value
                return {"k": 2, "v": t.value, "d": "@" + t.language if t.language else (None if dt == A.XSD_STRING else dt)}
            return {"k": 3, "v": str(t), "d": None}

        rows = []
        for sol in res:
            row = []
            for v in vars_:
                try:
                    row.append(conv(sol[v]))
                except (KeyError, IndexError, TypeError):
                    row.append(None)
            rows.append(row)
        return rows, vars_
    import rdflib
    import rdflib.term

    old = rdflib.NORMALIZE_LITERALS
    rdflib.NORMALIZE_LITERALS = False
    try:
        g = rdflib.Graph()
        try:
            g.parse(data=text, format=_RDFLIB_FORMATS.get(fmt, "turtle"), publicID=base)
        except Exception as ex:  # noqa: BLE001
            raise ValueError(f"not an RDF document: {ex}")
        try:
            res = g.query(query)
        except Exception as ex:  # noqa: BLE001
            raise ValueError(f"not a SPARQL query: {ex}")
        if res.type != "SELECT":
            raise ValueError(f"not a SELECT query: {query}")
        vars_ = names or [str(v) for v in res.vars]

        def conv_r(t):
            if t is None:
                return None
            if isinstance(t, rdflib.URIRef):
                return {"k": 0, "v": str(t), "d": None}
            if isinstance(t, rdflib.BNode):
                return {"k": 1, "v": "D" + str(t), "d": None}
            dt = str(t.datatype) if t.datatype else None
            return {"k": 2, "v": str(t), "d": "@" + t.language if t.language else (None if dt == A.XSD_STRING else dt)}

        rows = [[conv_r(sol.get(rdflib.Variable(v))) for v in vars_] for sol in res.bindings]
        return rows, vars_
    finally:
        rdflib.NORMALIZE_LITERALS = old
