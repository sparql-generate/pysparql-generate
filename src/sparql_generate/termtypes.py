"""Static term types of compiled columns and conversions between them.

A SPARQL variable becomes a DuckDB column.  The compiler tracks, for every
column, a *term type* that says how the native SQL value must be read as an RDF
term :

====== ================== ==============================================
kind   SQL type           RDF term
====== ================== ==============================================
iri    VARCHAR            IRI
bnode  VARCHAR            blank node, VARCHAR is the label
str    VARCHAR            simple literal / xsd:string
lang   VARCHAR            literal with the constant language tag ``lang``
typed  VARCHAR            literal with the constant datatype ``dt`` (lexical form)
int    BIGINT             xsd:integer
double DOUBLE             xsd:double
bool   BOOLEAN            xsd:boolean
json   JSON               a JSON value: string, number, boolean, object, array
any    STRUCT(k,v,d)      dynamic: k=0 IRI, 1 blank node, 2 literal, 3 triple term
                          (RDF 1.2); v lexical (a triple term: its N-Triples form
                          ``<<( s p o )>>``); d datatype IRI, or '@lang' for
                          language tags, or NULL
====== ================== ==============================================

``NULL`` always means *unbound* (or an evaluation error).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from . import ast as A
from .terms import sql_string

ANY_SQL_TYPE = "STRUCT(k TINYINT, v VARCHAR, d VARCHAR)"


@dataclass(frozen=True)
class TType:
    kind: str
    dt: Optional[str] = None
    lang: Optional[str] = None

    @property
    def is_literal(self) -> bool:
        return self.kind in ("str", "lang", "typed", "int", "double", "bool", "json")

    @property
    def is_numeric(self) -> bool:
        return self.kind in ("int", "double") or (
            self.kind == "typed" and self.dt in NUMERIC_DATATYPES
        )

    @property
    def is_stringy(self) -> bool:
        """A literal CONCAT and friends accept: simple, xsd:string or language-tagged."""
        return self.kind in ("str", "lang") or (self.kind == "typed" and self.dt == A.XSD_STRING)

    def datatype(self) -> Optional[str]:
        """Constant datatype IRI, or None when unknown/none."""
        if self.kind == "str":
            return A.XSD_STRING
        if self.kind == "lang":
            return A.RDF_LANGSTRING
        if self.kind == "typed":
            return self.dt
        if self.kind == "int":
            return A.XSD_INTEGER
        if self.kind == "double":
            return A.XSD_DOUBLE
        if self.kind == "bool":
            return A.XSD_BOOLEAN
        return None


IRI_T = TType("iri")
BNODE_T = TType("bnode")
STR_T = TType("str")
INT_T = TType("int")
DOUBLE_T = TType("double")
BOOL_T = TType("bool")
JSON_T = TType("json")
ANY_T = TType("any")
DECIMAL_T = TType("typed", dt=A.XSD_DECIMAL)
DATETIME_T = TType("typed", dt=A.XSD + "dateTime")

NUMERIC_DATATYPES = {
    A.XSD_INTEGER, A.XSD_DECIMAL, A.XSD_DOUBLE, A.XSD + "float", A.XSD + "long", A.XSD + "int",
    A.XSD + "short", A.XSD + "byte", A.XSD + "nonNegativeInteger", A.XSD + "positiveInteger",
    A.XSD + "nonPositiveInteger", A.XSD + "negativeInteger", A.XSD + "unsignedLong",
    A.XSD + "unsignedInt", A.XSD + "unsignedShort", A.XSD + "unsignedByte",
}


def typed(dt: str) -> TType:
    if dt == A.XSD_STRING:
        return STR_T
    if dt == A.XSD_INTEGER:
        return INT_T
    if dt == A.XSD_DOUBLE:
        return DOUBLE_T
    if dt == A.XSD_BOOLEAN:
        return BOOL_T
    return TType("typed", dt=dt)


def lang(tag: str) -> TType:
    return TType("lang", lang=tag)


class _NoConst:
    def __repr__(self):
        return "NOCONST"


NOCONST = _NoConst()


@dataclass
class CExpr:
    """A compiled expression: SQL text, its term type, and its Python value when
    it is a compile-time constant (``NOCONST`` otherwise)."""

    sql: str
    type: TType
    const: object = NOCONST

    @property
    def is_const(self) -> bool:
        return self.const is not NOCONST

    @property
    def is_null(self) -> bool:
        return self.is_const and self.const is None


NULL = CExpr("NULL", ANY_T, None)


def const_str(s: str) -> CExpr:
    return CExpr(sql_string(s), STR_T, s)


def const_iri(s: str) -> CExpr:
    return CExpr(sql_string(s), IRI_T, s)


def const_int(i: int) -> CExpr:
    return CExpr(str(int(i)), INT_T, int(i))


def const_bool(b: bool) -> CExpr:
    return CExpr("TRUE" if b else "FALSE", BOOL_T, bool(b))


def const_literal(lit: A.Literal) -> CExpr:
    if lit.lang:
        return CExpr(sql_string(lit.lexical), lang(lit.lang), lit.lexical)
    if lit.datatype is None or lit.datatype == A.XSD_STRING:
        return const_str(lit.lexical)
    if lit.datatype == A.XSD_INTEGER:
        try:
            return const_int(int(lit.lexical))
        except ValueError:
            return CExpr(sql_string(lit.lexical), TType("typed", dt=A.XSD_INTEGER), lit.lexical)
    if lit.datatype == A.XSD_DOUBLE:
        try:
            v = float(lit.lexical)
        except ValueError:
            return CExpr(sql_string(lit.lexical), TType("typed", dt=A.XSD_DOUBLE), lit.lexical)
        return CExpr(f"CAST({lit.lexical} AS DOUBLE)", DOUBLE_T, v)
    if lit.datatype == A.XSD_BOOLEAN:
        return const_bool(lit.lexical in ("true", "1"))
    return CExpr(sql_string(lit.lexical), TType("typed", dt=lit.datatype), lit.lexical)


# -- SQL fragments -----------------------------------------------------------

def sql_quoted(x: str) -> str:
    """SQL for the N-Triples quoted string of a lexical form (``"..."`` with escapes)."""
    return f"sg_q({x})"


def lex(e: CExpr) -> str:
    """SQL for the lexical form (VARCHAR) of a term; IRIs give the IRI string,
    blank nodes their label."""
    k = e.type.kind
    if k in ("iri", "bnode", "str", "lang", "typed"):
        return e.sql
    if k == "int":
        return f"CAST({e.sql} AS VARCHAR)"
    if k == "double":
        return f"sg_double_str({e.sql})"
    if k == "bool":
        return f"CASE WHEN {e.sql} THEN 'true' WHEN NOT {e.sql} THEN 'false' END"
    if k == "json":
        return f"sg_json_lex({e.sql})"
    if k == "any":
        return f"({e.sql}).v"
    raise ValueError(k)


def string_literal_lex(e: CExpr) -> str:
    """SQL for the lexical form when the term is a string literal (simple,
    xsd:string or language-tagged), NULL otherwise.  This is the argument check
    of CONCAT, ENCODE_FOR_URI, STRLEN, REGEX... in Jena."""
    t = e.type
    if t.is_stringy:
        return e.sql
    if t.kind == "json":
        return f"sg_json_str({e.sql})"
    if t.kind == "any":
        return f"sg_any_str({e.sql})"
    return "NULL"


def numeric(e: CExpr) -> str:
    """SQL DOUBLE value of a numeric literal, NULL when not numeric."""
    t = e.type
    if t.kind in ("int", "double"):
        return e.sql
    if t.kind == "typed" and t.dt in NUMERIC_DATATYPES:
        return f"TRY_CAST({e.sql} AS DOUBLE)"
    if t.kind == "json":
        return f"sg_json_num({e.sql})"
    if t.kind == "any":
        return f"sg_any_num({e.sql})"
    return "NULL"


def datatype_sql(e: CExpr) -> str:
    t = e.type
    dt = t.datatype()
    if dt is not None:
        return sql_string(dt)
    if t.kind == "json":
        return f"sg_json_dt({e.sql})"
    if t.kind == "any":
        return f"sg_any_dt({e.sql})"
    return "NULL"


def to_any(e: CExpr) -> str:
    """SQL converting a term to the dynamic STRUCT representation."""
    t = e.type
    k = t.kind
    if k == "any":
        return e.sql
    if k == "iri":
        return f"sg_any(0, {e.sql}, NULL)"
    if k == "bnode":
        return f"sg_any(1, {e.sql}, NULL)"
    if k == "str":
        return f"sg_any(2, {e.sql}, NULL)"
    if k == "lang":
        return f"sg_any(2, {e.sql}, {sql_string('@' + t.lang)})"
    if k == "typed":
        return f"sg_any(2, {e.sql}, {sql_string(t.dt)})"
    if k in ("int", "double", "bool"):
        return f"sg_any(2, {lex(e)}, {sql_string(t.datatype())})"
    if k == "json":
        return f"sg_json_any({e.sql})"
    raise ValueError(k)


def to_nt(e: CExpr, position: str = "o") -> str:
    """SQL producing the N-Triples serialisation of a term, NULL if unbound or
    not allowed in that position (``s``: IRI or blank node; ``p``: IRI)."""
    t = e.type
    k = t.kind
    if k == "iri":
        return f"('<' || {e.sql} || '>')"
    if k == "bnode":
        return "NULL" if position == "p" else f"('_:' || {e.sql})"
    if k == "any":
        return f"sg_any_nt({e.sql}, {sql_string(position)})"
    if position in ("s", "p"):
        return "NULL"
    if k == "str":
        return sql_quoted(e.sql)
    if k == "lang":
        return f"({sql_quoted(e.sql)} || {sql_string('@' + t.lang)})"
    if k == "typed":
        return f"({sql_quoted(e.sql)} || {sql_string('^^<' + t.dt + '>')})"
    if k in ("int", "double", "bool"):
        return f"('\"' || {lex(e)} || {sql_string(chr(34) + '^^<' + t.datatype() + '>')})"
    if k == "json":
        return f"sg_json_nt({e.sql})"
    if k == "any":
        return f"sg_any_nt({e.sql}, {sql_string(position)})"
    raise ValueError(k)


def unify(types) -> TType:
    """Common type of alternatives (IF, COALESCE): identical types stay, others
    collapse to ``any``."""
    ts = [t for t in types if t is not None]
    if not ts:
        return ANY_T
    first = ts[0]
    if all(t == first for t in ts):
        return first
    return ANY_T


def coerce(e: CExpr, target: TType) -> str:
    if e.type == target:
        return e.sql
    if target.kind == "any":
        return to_any(e)
    raise ValueError(f"cannot coerce {e.type} to {target}")


SUPPORT_SQL = f"""
CREATE OR REPLACE MACRO sg_any(k, v, d) AS CASE WHEN v IS NULL THEN NULL ELSE {{'k': k::TINYINT, 'v': v::VARCHAR, 'd': d::VARCHAR}} END;
CREATE OR REPLACE MACRO sg_q(x) AS CAST(to_json(CAST(x AS VARCHAR)) AS VARCHAR);
CREATE OR REPLACE MACRO sg_jd_digits(s) AS CASE WHEN contains(s, 'e') THEN rtrim(replace(split_part(s, 'e', 1), '.', ''), '0')
    ELSE rtrim(ltrim(replace(s, '.', ''), '0'), '0') END;
CREATE OR REPLACE MACRO sg_jd_exp(s) AS CASE WHEN contains(s, 'e') THEN CAST(split_part(s, 'e', 2) AS INTEGER)
    WHEN split_part(s, '.', 1) <> '0' THEN length(split_part(s, '.', 1)) - 1
    ELSE -(length(split_part(s, '.', 2)) - length(ltrim(split_part(s, '.', 2), '0')) + 1) END;
CREATE OR REPLACE MACRO sg_jd_fmt(d, e) AS CASE WHEN e >= -3 AND e < 7 THEN
        (CASE WHEN e >= 0 THEN rpad(left(d, e + 1), CAST(e + 1 AS INTEGER), '0') || '.' || COALESCE(NULLIF(substr(d, e + 2), ''), '0')
              ELSE '0.' || repeat('0', -e - 1) || d END)
    ELSE left(d, 1) || '.' || COALESCE(NULLIF(substr(d, 2), ''), '0') || 'E' || CAST(e AS VARCHAR) END;
CREATE OR REPLACE MACRO sg_jd_abs(a) AS list_transform([{{'d': sg_jd_digits(a), 'e': sg_jd_exp(a)}}], lambda z: sg_jd_fmt(z.d, z.e))[1];
CREATE OR REPLACE MACRO sg_jnum(x, s) AS list_transform([{{'x': x, 's': s}}], lambda w: CASE WHEN w.x IS NULL THEN NULL
    WHEN isnan(w.x) THEN 'NaN' WHEN isinf(w.x) THEN CASE WHEN w.x > 0 THEN 'Infinity' ELSE '-Infinity' END
    WHEN w.x = 0 THEN CASE WHEN starts_with(w.s, '-') THEN '-0.0' ELSE '0.0' END
    ELSE (CASE WHEN w.x < 0 THEN '-' ELSE '' END) || list_transform([ltrim(w.s, '-')], lambda a: sg_jd_abs(a))[1] END)[1];
CREATE OR REPLACE MACRO sg_jdouble(x) AS list_transform([CAST(x AS DOUBLE)], lambda v: CASE
    WHEN abs(v) >= 0.001 AND abs(v) < 10000000 THEN CAST(v AS VARCHAR) ELSE sg_jnum(v, CAST(v AS VARCHAR)) END)[1];
CREATE OR REPLACE MACRO sg_jfloat(x) AS list_transform([CAST(x AS DOUBLE)], lambda v: sg_jnum(v, CAST(CAST(v AS FLOAT) AS VARCHAR)))[1];
CREATE OR REPLACE MACRO sg_double_str(x) AS CASE
    WHEN abs(x) >= 0.001 AND abs(x) < 10000000 THEN CAST(CAST(x AS DOUBLE) AS VARCHAR) || 'e0'
    ELSE list_transform([CAST(x AS DOUBLE)], lambda v: list_transform([sg_jnum(v, CAST(v AS VARCHAR))], lambda j: CASE
        WHEN j = 'Infinity' THEN 'INF' WHEN j = '-Infinity' THEN '-INF' WHEN j = 'NaN' OR contains(j, 'E') THEN j
        ELSE j || 'e0' END)[1])[1] END;
CREATE OR REPLACE MACRO sg_json_type(x) AS json_type(x);
CREATE OR REPLACE MACRO sg_json_kind(x) AS CASE WHEN regexp_matches(CAST(x AS VARCHAR), '[eE]') THEN 'double'
    WHEN contains(CAST(x AS VARCHAR), '.') THEN 'decimal' ELSE 'integer' END;
CREATE OR REPLACE MACRO sg_json_lex(x) AS CASE json_type(x)
    WHEN 'VARCHAR' THEN json_extract_string(x, '$')
    WHEN 'DOUBLE' THEN CASE sg_json_kind(x) WHEN 'double' THEN sg_double_str(CAST(x AS DOUBLE))
        WHEN 'decimal' THEN CASE WHEN CAST(x AS VARCHAR) = '-0.0' THEN '0.0' ELSE CAST(x AS VARCHAR) END
        ELSE CAST(x AS VARCHAR) END
    WHEN 'BOOLEAN' THEN CAST(x AS VARCHAR)
    WHEN 'NULL' THEN NULL
    ELSE CAST(x AS VARCHAR) END;
CREATE OR REPLACE MACRO sg_json_str(x) AS CASE json_type(x)
    WHEN 'VARCHAR' THEN json_extract_string(x, '$')
    WHEN 'OBJECT' THEN CAST(x AS VARCHAR)
    WHEN 'ARRAY' THEN CAST(x AS VARCHAR)
    ELSE NULL END;
CREATE OR REPLACE MACRO sg_json_num(x) AS CASE json_type(x)
    WHEN 'DOUBLE' THEN CAST(x AS DOUBLE) WHEN 'UBIGINT' THEN CAST(x AS DOUBLE)
    WHEN 'BIGINT' THEN CAST(x AS DOUBLE) WHEN 'HUGEINT' THEN CAST(x AS DOUBLE)
    WHEN 'UHUGEINT' THEN CAST(x AS DOUBLE) ELSE NULL END;
CREATE OR REPLACE MACRO sg_json_dt(x) AS CASE json_type(x)
    WHEN 'VARCHAR' THEN '{A.XSD_STRING}' WHEN 'DOUBLE' THEN 'http://www.w3.org/2001/XMLSchema#' || sg_json_kind(x)
    WHEN 'BOOLEAN' THEN '{A.XSD_BOOLEAN}' WHEN 'NULL' THEN NULL
    WHEN 'OBJECT' THEN '{A.XSD_STRING}' WHEN 'ARRAY' THEN '{A.XSD_STRING}'
    ELSE '{A.XSD_INTEGER}' END;
CREATE OR REPLACE MACRO sg_json_any(x) AS CASE WHEN x IS NULL OR json_type(x) = 'NULL' THEN NULL
    ELSE sg_any(2, sg_json_lex(x), CASE sg_json_dt(x) WHEN '{A.XSD_STRING}' THEN NULL ELSE sg_json_dt(x) END) END;
CREATE OR REPLACE MACRO sg_json_nt(x) AS CASE json_type(x)
    WHEN 'VARCHAR' THEN sg_q(json_extract_string(x, '$'))
    WHEN 'DOUBLE' THEN '"' || sg_json_lex(x) || '"^^<http://www.w3.org/2001/XMLSchema#' || sg_json_kind(x) || '>'
    WHEN 'BOOLEAN' THEN '"' || CAST(x AS VARCHAR) || '"^^<{A.XSD_BOOLEAN}>'
    WHEN 'NULL' THEN NULL
    WHEN 'OBJECT' THEN sg_q(x)
    WHEN 'ARRAY' THEN sg_q(x)
    ELSE '"' || CAST(x AS VARCHAR) || '"^^<{A.XSD_INTEGER}>' END;
CREATE OR REPLACE MACRO sg_any_str(x) AS CASE WHEN x.k = 2 AND (x.d IS NULL OR x.d = '{A.XSD_STRING}' OR x.d LIKE '@%') THEN x.v ELSE NULL END;
CREATE OR REPLACE MACRO sg_any_num(x) AS CASE WHEN x.k = 2 AND x.d IN ({", ".join(sql_string(d) for d in sorted(NUMERIC_DATATYPES))}) THEN TRY_CAST(x.v AS DOUBLE) ELSE NULL END;
CREATE OR REPLACE MACRO sg_any_dt(x) AS CASE WHEN x.k <> 2 THEN NULL WHEN x.d IS NULL THEN '{A.XSD_STRING}' WHEN x.d LIKE '@%' THEN '{A.RDF_LANGSTRING}' ELSE x.d END;
CREATE OR REPLACE MACRO sg_any_lang(x) AS CASE WHEN x.k = 2 AND x.d LIKE '@%' THEN substr(x.d, 2) WHEN x.k = 2 THEN '' ELSE NULL END;
-- an IRI that fun:unsafeIRI made (white space allowed) in N-Triples: the characters that an IRIREF
-- forbids as UCHAR escapes (a backslash cannot be escaped: kept)
CREATE OR REPLACE MACRO sg_iri_nt_escape(x) AS CASE WHEN NOT regexp_matches(x, '[\\x00-\\x20<>"{{}}|^`]') THEN x
    ELSE replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(x, ' ', '\\u0020'), chr(9), '\\u0009'), chr(10), '\\u000A'), chr(13), '\\u000D'), '<', '\\u003C'), '>', '\\u003E'), '"', '\\u0022'), '{{', '\\u007B'), '}}', '\\u007D'), '|', '\\u007C'), '^', '\\u005E'), '`', '\\u0060') END;
CREATE OR REPLACE MACRO sg_any_nt(x, pos) AS CASE
    WHEN x IS NULL THEN NULL
    WHEN x.k = 0 THEN '<' || sg_iri_nt_escape(x.v) || '>'
    WHEN pos = 'p' THEN NULL
    WHEN x.k = 1 THEN '_:' || x.v
    WHEN x.k = 3 THEN x.v
    WHEN pos <> 'o' THEN NULL
    WHEN x.d IS NULL THEN sg_q(x.v)
    WHEN x.d LIKE '@%' THEN sg_q(x.v) || x.d
    ELSE sg_q(x.v) || '^^<' || x.d || '>' END;
CREATE OR REPLACE MACRO sg_nt_lq(x) AS length(x) - strpos(reverse(x), '"') + 1;
CREATE OR REPLACE MACRO sg_nt_dt(sfx) AS CASE WHEN sfx = '' THEN NULL WHEN starts_with(sfx, '@') THEN sfx
    WHEN sfx = '^^<{A.XSD_STRING}>' THEN NULL ELSE substr(sfx, 4, length(sfx) - 4) END;
CREATE OR REPLACE MACRO sg_nt_any(x) AS CASE WHEN x IS NULL THEN NULL
    WHEN starts_with(x, '<<') THEN sg_any(3, x, NULL)
    WHEN starts_with(x, '<') THEN sg_any(0, substr(x, 2, length(x) - 2), NULL)
    WHEN starts_with(x, '_:') THEN sg_any(1, substr(x, 3), NULL)
    ELSE sg_any(2, json_extract_string(CAST(substr(x, 1, sg_nt_lq(x)) AS JSON), '$'), sg_nt_dt(substr(x, sg_nt_lq(x) + 1))) END;
CREATE OR REPLACE MACRO sg_box(x) AS CASE WHEN ends_with(x, chr(10))
    THEN replace(left(x, length(x) - 1), chr(10), chr(10) || '  ') || chr(10)
    ELSE replace(x, chr(10), chr(10) || '  ') END;
CREATE OR REPLACE MACRO sg_iri_ok(x) AS CASE WHEN x IS NULL THEN NULL
    WHEN regexp_matches(x, '[\\x00-\\x20<>"{{}}|\\\\^`]') THEN NULL
    WHEN regexp_matches(x, '^[A-Za-z][A-Za-z0-9+.-]*:') THEN x ELSE NULL END;
CREATE OR REPLACE MACRO sg_split(x, re) AS (
    WITH l AS (SELECT string_split_regex(x, re) AS l)
    SELECT CASE WHEN list_position(list_reverse(list_transform(l, lambda e: e <> '')), true) IS NULL THEN l[1:1]
        ELSE l[1:len(l) - list_position(list_reverse(list_transform(l, lambda e: e <> '')), true) + 1] END FROM l);
"""
