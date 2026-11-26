"""Relational databases for ``iter:SQL``, as the Java engine 2.6.1 reads them (``ITER_SQL``).

The database is named by a JDBC URL (an IRI or a string): ``jdbc:postgresql://host:port/db``
(psycopg2), ``jdbc:mysql://host:port/db`` (PyMySQL), ``jdbc:sqlserver://host:port;k=v;...``
(pyodbc and Microsoft's ODBC driver), with the user and the password in the URL
(``?user=…&password=…``, ``;user={…};password={…}``); and, without a driver to install,
``jdbc:sqlite:<file>`` and ``jdbc:duckdb:<file>``.  The drivers are optional dependencies
(``pip install pysparql-generate[sql]``).

A cell is the natural RDF literal of R2RML (section 10.2) in the canonical lexical form of
its datatype, as the Java engine writes it: ``xsd:integer`` for an integer type,
``xsd:decimal`` for ``DECIMAL`` and ``NUMERIC``, ``xsd:double`` for ``REAL`` (from the decimal
value of the single precision number), ``FLOAT`` and ``DOUBLE``, ``xsd:boolean`` (also a
``BIT`` of one bit, and MySQL's ``TINYINT(1)``), ``xsd:date``, ``xsd:time``, ``xsd:dateTime``
(without a zero fraction), ``xsd:hexBinary`` (upper case), a string otherwise, a ``CHAR(n)``
padded with spaces to ``n`` characters; ``NULL`` is unbound.
"""
from __future__ import annotations

import datetime as _dt
import decimal as _decimal
import os
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple
from urllib.parse import parse_qsl, unquote

from . import ast as A

X = A.XSD


class SQLConnectError(Exception):
    """The database cannot be reached (a missing document, for the Java engine)."""


class SQLQueryError(Exception):
    """The database rejects the query (an error that stops the execution)."""


def redact(url: str) -> str:
    """The JDBC URL without its password, for the messages (``ITER_SQL.redact``)."""
    return re.sub(r"(?i)(password=)(\{(?:[^}]|\}\})*\}|[^;&]*)", r"\1***", url)


# -- JDBC URLs ------------------------------------------------------------------------------


@dataclass
class Target:
    kind: str  # postgresql | mysql | sqlserver | sqlite | duckdb
    host: Optional[str] = None
    port: Optional[int] = None
    database: Optional[str] = None
    user: Optional[str] = None
    password: Optional[str] = None
    path: Optional[str] = None
    props: Dict[str, str] = field(default_factory=dict)


def _sqlserver_props(text: str) -> Dict[str, str]:
    """The ``;key=value`` properties of a SQL Server JDBC URL, a value in braces read with
    ``}}`` for ``}``."""
    props: Dict[str, str] = {}
    i, n = 0, len(text)
    while i < n:
        if text[i] == ";":
            i += 1
            continue
        j = text.find("=", i)
        if j < 0:
            break
        key = text[i:j].strip()
        i = j + 1
        if i < n and text[i] == "{":
            i += 1
            out = []
            while i < n:
                if text[i] == "}":
                    if i + 1 < n and text[i + 1] == "}":
                        out.append("}")
                        i += 2
                        continue
                    i += 1
                    break
                out.append(text[i])
                i += 1
            value = "".join(out)
        else:
            k = text.find(";", i)
            k = n if k < 0 else k
            value = text[i:k]
            i = k
        props[key.lower()] = value
    return props


def parse_jdbc(url: str, base_dir: Optional[str] = None) -> Target:
    """The target of a JDBC URL; ``ValueError`` for a URL of no supported database."""
    if url.startswith("jdbc:sqlite:") or url.startswith("jdbc:duckdb:"):
        kind = url[5:url.index(":", 5)]
        path = url[len(kind) + 6:]
        if path.startswith("//"):
            path = path[2:]
        if path != ":memory:" and not os.path.isabs(path):
            path = os.path.join(base_dir or os.getcwd(), path)
        return Target(kind, path=path)
    m = re.match(r"^jdbc:(postgresql|mysql):(?://([^/?]*))?/?([^?]*)(?:\?(.*))?$", url)
    if m:
        kind, authority, db, query = m.group(1), m.group(2) or "", m.group(3), m.group(4) or ""
        props = {k.lower(): v for k, v in parse_qsl(query, keep_blank_values=True)}
        host, port = authority, None
        if "@" in host:  # user:password@host
            cred, host = host.rsplit("@", 1)
            u, _, p = cred.partition(":")
            props.setdefault("user", unquote(u))
            props.setdefault("password", unquote(p))
        hm = re.match(r"^\[?([^\]]*?)\]?(?::(\d+))?$", host.split(",")[0])
        if hm:
            host, port = hm.group(1) or "localhost", int(hm.group(2)) if hm.group(2) else None
        return Target(kind, host or "localhost", port, unquote(db) or None, props.get("user"), props.get("password"),
                      props=props)
    m = re.match(r"^jdbc:sqlserver://([^;]*)(;.*)?$", url)
    if m:
        server, rest = m.group(1), m.group(2) or ""
        props = _sqlserver_props(rest)
        hm = re.match(r"^([^:\\]*)(?:\\([^:]*))?(?::(\d+))?$", server)
        host, port = (hm.group(1), int(hm.group(3)) if hm.group(3) else None) if hm else (server, None)
        if hm and hm.group(2):
            props.setdefault("instancename", hm.group(2))
        host = props.get("servername", host) or "localhost"
        if "portnumber" in props:
            port = int(props["portnumber"])
        return Target("sqlserver", host, port, props.get("databasename") or props.get("database"),
                      props.get("user") or props.get("username"), props.get("password"), props=props)
    raise ValueError(f"unsupported database {redact(url)} (PostgreSQL, MySQL, SQL Server, SQLite or DuckDB)")


# -- natural RDF literals --------------------------------------------------------------------


def _lit(lexical: str, datatype: Optional[str]) -> dict:
    return {"k": 2, "v": lexical, "d": datatype}


def canonical_decimal(d: _decimal.Decimal) -> str:
    """``xsd:decimal`` in its canonical form: no exponent, no trailing zero, a decimal
    point and a digit on each side (``ITER_SQL.canonicalDecimal``)."""
    s = format(d.normalize(), "f") if d != 0 else "0"
    if s in ("-0", "0", "-0.0"):
        s = "0"
    return s if "." in s else s + ".0"


def canonical_double(text: str) -> str:
    """``xsd:double`` in its canonical form from a decimal numeral: one digit before the
    point, at least one after, ``E``, the exponent (``8.025E1``, ``1.0E0``, ``0.0E0``)."""
    d = _decimal.Decimal(text)
    if d == 0:
        return "0.0E0"
    sign, digits, exp = d.normalize().as_tuple()
    ds = "".join(map(str, digits))
    exponent = len(ds) - 1 + exp
    return ("-" if sign else "") + ds[0] + "." + (ds[1:] or "0") + "E" + str(exponent)


def _special(v: float) -> Optional[str]:
    if v != v:
        return "NaN"
    if v in (float("inf"), float("-inf")):
        return "INF" if v > 0 else "-INF"
    return None


def double_literal(v: float, single: bool = False) -> dict:
    """A ``REAL`` (``single``: the shortest decimal of the single precision number, as
    Java's ``Float.toString``), ``FLOAT`` or ``DOUBLE`` value."""
    sp = _special(v)
    if sp is not None:
        return _lit(sp, X + "double")
    if single:
        import numpy as np

        text = str(np.float32(v))
    else:
        text = repr(float(v))
    return _lit(canonical_double(text), X + "double")


def _fraction(micros: int) -> str:
    return ("." + f"{micros:06d}".rstrip("0")) if micros else ""


def time_lexical(t) -> str:
    if isinstance(t, _dt.timedelta):  # MySQL's TIME
        total = int(t.total_seconds())
        micros = t.microseconds
        sign = "-" if total < 0 else ""
        total = abs(total)
        return f"{sign}{total // 3600:02d}:{total // 60 % 60:02d}:{total % 60:02d}{_fraction(micros)}"
    return f"{t.hour:02d}:{t.minute:02d}:{t.second:02d}{_fraction(t.microsecond)}"


def offset_lexical(off: _dt.timedelta) -> str:
    secs = int(off.total_seconds())
    if secs == 0:
        return "Z"
    sign = "+" if secs > 0 else "-"
    secs = abs(secs)
    out = f"{sign}{secs // 3600:02d}:{secs // 60 % 60:02d}"
    return out + (f":{secs % 60:02d}" if secs % 60 else "")


def datetime_lexical(v: _dt.datetime, utc: bool = False) -> str:
    if v.tzinfo is not None and utc:
        v = v.astimezone(_dt.timezone.utc)
    s = v.date().isoformat() + "T" + time_lexical(v.time())
    if v.tzinfo is not None:
        s += offset_lexical(v.utcoffset())
    return s


def hex_lexical(b) -> str:
    return bytes(b).hex().upper()


def natural_literal(v, kind: str = "auto", length: int = 0) -> Optional[dict]:
    """The natural RDF literal of a cell, by the kind of its column (``auto``: by the Python
    type of the value), None for NULL."""
    if v is None:
        return None
    if kind == "auto":
        if isinstance(v, bool):
            kind = "bool"
        elif isinstance(v, int):
            kind = "int"
        elif isinstance(v, float):
            kind = "double"
        elif isinstance(v, _decimal.Decimal):
            kind = "decimal"
        elif isinstance(v, _dt.datetime):
            kind = "timestampoffset" if v.tzinfo is not None else "timestamp"
        elif isinstance(v, _dt.date):
            kind = "date"
        elif isinstance(v, (_dt.time, _dt.timedelta)):
            kind = "time"
        elif isinstance(v, (bytes, bytearray, memoryview)):
            kind = "binary"
        else:
            kind = "string"
    if kind == "bool":
        if isinstance(v, (bytes, bytearray)):
            v = any(v)
        elif isinstance(v, str):
            v = v not in ("0", "f", "false", "")
        return _lit("true" if v else "false", X + "boolean")
    if kind == "int":
        return _lit(str(int(v)), X + "integer")
    if kind == "decimal":
        return _lit(canonical_decimal(_decimal.Decimal(v)), X + "decimal")
    if kind in ("real", "double"):
        return double_literal(float(v), single=kind == "real")
    if kind == "date":
        if isinstance(v, int):  # MySQL's YEAR, a DATE for Connector/J
            return _lit(f"{v:04d}-01-01", X + "date")
        if isinstance(v, _dt.datetime):
            v = v.date()
        return _lit(v.isoformat(), X + "date")
    if kind == "time":
        s = time_lexical(v)
        if isinstance(v, _dt.time) and v.tzinfo is not None:
            s += offset_lexical(v.utcoffset())
        return _lit(s, X + "time")
    if kind in ("timestamp", "timestamptz"):
        if not isinstance(v, _dt.datetime):
            return _lit(str(v), None)
        return _lit(datetime_lexical(v, utc=kind == "timestamptz"), X + "dateTime")
    if kind == "timestampoffset":
        return _lit(datetime_lexical(v), X + "dateTime")
    if kind == "binary":
        if isinstance(v, str):
            return _lit(v, None)
        return _lit(hex_lexical(v), X + "hexBinary")
    if kind == "bits":  # a BIT of several bits: its bytes
        if isinstance(v, (bytes, bytearray)):
            return _lit(hex_lexical(v), X + "hexBinary")
        return _lit(str(v), None)
    if kind == "char":
        s = str(v)
        if 0 < length < 100000 and len(s) < length:
            s += " " * (length - len(s))
        return _lit(s, None)
    if kind == "money":  # PostgreSQL's money, a DOUBLE for its JDBC driver
        return double_literal(float(re.sub(r"[^0-9.\-]", "", str(v)) or "nan"))
    if kind == "uuid":
        return _lit(str(v).upper(), None)
    if isinstance(v, (bytes, bytearray, memoryview)):
        return _lit(bytes(v).decode("utf-8", errors="replace"), None)
    return _lit(str(v), None)


# -- the drivers -----------------------------------------------------------------------------

CONNECT_TIMEOUT = 10  # seconds, as the PostgreSQL JDBC driver


def _import(name: str, extra: str):
    try:
        return __import__(name)
    except ImportError:
        raise SQLConnectError(f"the Python driver {extra} is not installed (pip install pysparql-generate[sql])")


_PG_TEXT_OIDS = (114, 3802, 1186, 199, 3807, 1000, 1001, 1002, 1003, 1005, 1006, 1007, 1009, 1014, 1015, 1016,
                 1017, 1021, 1022, 1028, 1115, 1182, 1183, 1185, 1187, 1231, 1263, 1270, 2951, 3904, 3906, 3908,
                 3910, 3912, 3926)


def _pg_kinds(cur) -> List[Tuple[str, int]]:
    oid_kinds = {16: "bool", 20: "int", 21: "int", 23: "int", 26: "int", 700: "real", 701: "double",
                 1700: "decimal", 1082: "date", 1083: "time", 1266: "time", 1114: "timestamp",
                 1184: "timestamptz", 17: "binary", 1042: "char", 18: "char", 1560: "pgbit", 790: "money"}
    out = []
    for d in cur.description:
        k = oid_kinds.get(d.type_code, "string")
        length = d.internal_size if k == "char" and d.internal_size and d.internal_size > 0 else 0
        out.append((k, length))
    return out


_MBLEN = {"utf8mb4": 4, "utf8": 3, "utf8mb3": 3, "ucs2": 2, "utf16": 4, "utf16le": 4, "utf32": 4, "sjis": 2,
          "cp932": 2, "ujis": 3, "eucjpms": 3, "euckr": 2, "gbk": 2, "gb2312": 2, "big5": 2, "gb18030": 4}


def _mysql_kinds(cur) -> List[Tuple[str, int]]:
    from pymysql.charset import charset_by_id
    from pymysql.constants import FIELD_TYPE as F

    out = []
    for f in cur._result.fields:
        t, binary = f.type_code, f.charsetnr == 63
        if t == F.TINY and f.length == 1:
            out.append(("bool", 0))  # TINYINT(1), BOOLEAN: a BIT for Connector/J
        elif t in (F.TINY, F.SHORT, F.LONG, F.LONGLONG, F.INT24):
            out.append(("int", 0))
        elif t in (F.DECIMAL, F.NEWDECIMAL):
            out.append(("decimal", 0))
        elif t == F.FLOAT:
            out.append(("real", 0))
        elif t == F.DOUBLE:
            out.append(("double", 0))
        elif t in (F.DATE, F.NEWDATE, F.YEAR):
            out.append(("date", 0))
        elif t == F.TIME:
            out.append(("time", 0))
        elif t in (F.TIMESTAMP, F.DATETIME):
            out.append(("timestamp", 0))
        elif t == F.BIT:
            out.append(("bool" if f.length == 1 else "bits", 0))
        elif t in (F.TINY_BLOB, F.MEDIUM_BLOB, F.LONG_BLOB, F.BLOB, F.VAR_STRING, F.VARCHAR, F.GEOMETRY) and binary:
            out.append(("binary", 0))
        elif t == F.STRING and binary:
            out.append(("binary", 0))
        elif t in (F.STRING, F.ENUM, F.SET):
            try:
                mb = _MBLEN.get(charset_by_id(f.charsetnr).name, 1)
            except KeyError:
                mb = 1
            out.append(("char", f.length // mb))
        else:
            out.append(("string", 0))
    return out


def _mssql_kinds(cur) -> List[Tuple[str, int]]:
    import uuid

    out = []
    for d in cur.description:
        t = d[1]
        if t is bool:
            out.append(("bool", 0))
        elif t is int:
            out.append(("int", 0))
        elif t is float:
            out.append(("real" if (d[4] or 53) <= 24 else "double", 0))
        elif t is _decimal.Decimal:
            out.append(("decimal", 0))
        elif t is _dt.datetime:
            out.append(("timestamp", 0))
        elif t is _dt.date:
            out.append(("date", 0))
        elif t is _dt.time:
            out.append(("time", 0))
        elif t in (bytes, bytearray):
            out.append(("binary", 0))
        elif t is uuid.UUID:
            out.append(("uuid", 0))
        else:
            # strings, and the types that the ODBC driver does not describe (TIME,
            # DATETIMEOFFSET, read by the output converters): by the value
            out.append(("auto", 0))
    return out


def _duckdb_kinds(cur) -> List[Tuple[str, int]]:
    out = []
    for d in cur.description:
        t = str(d[1]).upper()
        if t in ("TINYINT", "SMALLINT", "INTEGER", "BIGINT", "HUGEINT", "UTINYINT", "USMALLINT", "UINTEGER",
                 "UBIGINT", "UHUGEINT"):
            out.append(("int", 0))
        elif t.startswith("DECIMAL"):
            out.append(("decimal", 0))
        elif t == "FLOAT":
            out.append(("real", 0))
        elif t == "DOUBLE":
            out.append(("double", 0))
        elif t == "BOOLEAN":
            out.append(("bool", 0))
        elif t == "DATE":
            out.append(("date", 0))
        elif t in ("TIME", "TIME WITH TIME ZONE"):
            out.append(("time", 0))
        elif t == "TIMESTAMP WITH TIME ZONE":
            out.append(("timestamptz", 0))
        elif t.startswith("TIMESTAMP"):
            out.append(("timestamp", 0))
        elif t == "BLOB":
            out.append(("binary", 0))
        else:
            out.append(("string", 0))
    return out


def _mssql_datetimeoffset(raw: bytes):
    import struct

    y, mo, d, h, mi, s, ns, th, tm = struct.unpack("<6hI2h", raw)
    tz = _dt.timezone(_dt.timedelta(hours=th, minutes=tm))
    return _dt.datetime(y, mo, d, h, mi, s, ns // 1000, tz)


def _mssql_time2(raw: bytes):
    import struct

    h, mi, s, _pad, ns = struct.unpack("<4HI", raw)
    return _dt.time(h, mi, s, ns // 1000)


def session_variables(text: str) -> List[str]:
    """The ``name=value`` pairs of Connector/J's ``sessionVariables``, separated by commas or
    semicolons outside quotes and parentheses (``sql_mode=CONCAT(@@sql_mode,',ANSI_QUOTES')``)."""
    out, cur, depth, quote = [], [], 0, None
    for ch in text:
        if quote:
            cur.append(ch)
            if ch == quote:
                quote = None
        elif ch in "'\"":
            quote = ch
            cur.append(ch)
        elif ch == "(":
            depth += 1
            cur.append(ch)
        elif ch == ")":
            depth -= 1
            cur.append(ch)
        elif ch in ",;" and depth == 0:
            out.append("".join(cur).strip())
            cur = []
        else:
            cur.append(ch)
    out.append("".join(cur).strip())
    return [v for v in out if v]


def connect(t: Target):
    """A DB-API connection to the target; :class:`SQLConnectError` if it cannot be reached."""
    try:
        if t.kind == "sqlite":
            import sqlite3

            if t.path != ":memory:" and not os.path.exists(t.path):
                raise SQLConnectError(f"no SQLite database {t.path}")
            return sqlite3.connect(f"file:{t.path}?mode=ro" if t.path != ":memory:" else t.path, uri=True)
        if t.kind == "duckdb":
            import duckdb

            return duckdb.connect(t.path, read_only=t.path != ":memory:")
        if t.kind == "postgresql":
            psycopg2 = _import("psycopg2", "psycopg2")
            kw = {"host": t.host, "dbname": t.database or t.user or "postgres", "connect_timeout": CONNECT_TIMEOUT}
            if t.port:
                kw["port"] = t.port
            if t.user is not None:
                kw["user"] = t.user
            if t.password:
                kw["password"] = t.password
            con = psycopg2.connect(**kw)
            con.set_session(readonly=False, autocommit=True)
            # the text of the values that psycopg2 would turn into Python objects but JDBC's
            # getString gives as PostgreSQL writes them: json, jsonb, interval, arrays
            ext = psycopg2.extensions
            ext.register_type(ext.new_type(_PG_TEXT_OIDS, "SG_TEXT", lambda v, cur: v), con)
            return con
        if t.kind == "mysql":
            pymysql = _import("pymysql", "PyMySQL")
            con = pymysql.connect(host=t.host, port=t.port or 3306, user=t.user or "", password=t.password or "",
                                  database=t.database, charset="utf8mb4", connect_timeout=CONNECT_TIMEOUT,
                                  init_command="SET time_zone = '+00:00'")
            # Connector/J's sessionVariables: SET SESSION name=value for each pair
            variables = session_variables(t.props.get("sessionvariables", ""))
            if variables:
                with con.cursor() as cur:
                    cur.execute("SET SESSION " + ", ".join(variables))
            return con
        if t.kind == "sqlserver":
            pyodbc = _import("pyodbc", "pyodbc")
            drivers = [d for d in pyodbc.drivers() if "SQL Server" in d]
            if not drivers:
                raise SQLConnectError("no ODBC driver for SQL Server is installed")

            def q(v: str) -> str:
                return "{" + v.replace("}", "}}") + "}"

            server = t.host + (("\\" + t.props["instancename"]) if t.props.get("instancename") else "")
            if t.port:
                server += f",{t.port}"
            parts = [f"DRIVER={q(sorted(drivers)[-1])}", f"SERVER={q(server)}"]
            if t.database:
                parts.append(f"DATABASE={q(t.database)}")
            if t.user is not None:
                parts += [f"UID={q(t.user)}", f"PWD={q(t.password or '')}"]
            else:
                parts.append("Trusted_Connection=yes")
            encrypt = t.props.get("encrypt", "true").lower()
            parts.append("Encrypt=" + ("no" if encrypt == "false" else "yes"))
            if t.props.get("trustservercertificate", "false").lower() == "true":
                parts.append("TrustServerCertificate=yes")
            con = pyodbc.connect(";".join(parts), timeout=CONNECT_TIMEOUT, autocommit=True)
            con.add_output_converter(-155, _mssql_datetimeoffset)
            con.add_output_converter(-154, _mssql_time2)
            return con
    except SQLConnectError:
        raise
    except Exception as ex:  # noqa: BLE001 - the driver's error: the database cannot be reached
        raise SQLConnectError(str(ex).strip().splitlines()[0] if str(ex).strip() else type(ex).__name__)
    raise SQLConnectError(f"unsupported database kind {t.kind}")


def query(t: Target, sql: str) -> Tuple[List[str], List[Tuple[str, int]], List[tuple]]:
    """``(labels, kinds, rows)`` of the result of ``sql``: the labels of the columns, the kind
    and length of each (for :func:`natural_literal`), the rows.  :class:`SQLConnectError` if
    the database cannot be reached, :class:`SQLQueryError` if it rejects the query."""
    con = connect(t)
    try:
        try:
            cur = con.cursor()
            cur.execute(sql)
            desc = cur.description or []
            labels = [d[0] for d in desc]
            rows = cur.fetchall() if desc else []
            if t.kind == "postgresql":
                kinds = _pg_kinds(cur)
            elif t.kind == "mysql":
                kinds = _mysql_kinds(cur)
            elif t.kind == "sqlserver":
                kinds = _mssql_kinds(cur)
            elif t.kind == "duckdb":
                kinds = _duckdb_kinds(cur)
            else:
                kinds = [("auto", 0)] * len(labels)
        except Exception as ex:  # noqa: BLE001 - the database rejects the query
            msg = str(ex).strip()
            raise SQLQueryError(msg.splitlines()[0] if msg else type(ex).__name__)
    finally:
        try:
            con.close()
        except Exception:  # noqa: BLE001
            pass
    return labels, kinds, [tuple(r) for r in rows]


def select_columns(labels: List[str], names: List[str]) -> List[int]:
    """The indexes of the columns named ``names`` (the label compared exactly, else
    regardless of case, as JDBC), or all of them; ``LookupError(message, name)`` for a name
    that no column has, or that several have (``ITER_SQL.columns``)."""
    if not names:
        return list(range(len(labels)))
    out = []
    for name in names:
        exact = [i for i, lab in enumerate(labels) if lab == name]
        folded = [i for i, lab in enumerate(labels) if lab != name and lab.lower() == name.lower()]
        if len(exact) > 1 or (not exact and len(folded) > 1):
            raise LookupError("SQL error: several columns of the result of the query have this name", name)
        if not exact and not folded:
            raise LookupError("SQL error: no such column in the result of the query", name)
        out.append(exact[0] if exact else folded[0])
    return out


def natural_rows(kinds: List[Tuple[str, int]], rows: List[tuple], columns: List[int]) -> List[List[Optional[dict]]]:
    out = []
    for r in rows:
        row = []
        for i in columns:
            v = r[i]
            k, length = kinds[i]
            if k == "pgbit":
                k = "bool" if isinstance(v, str) and len(v) == 1 else "string"
            row.append(natural_literal(v, k, length))
        out.append(row)
    return out
