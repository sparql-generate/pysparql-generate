"""``RAISE(message, value...)``: an expression whose evaluation stops the execution with
an error that carries a message and values (SPARQL-Generate 2.6).

The compiler writes it as DuckDB's ``error()`` (macro ``sg_raise``), evaluated only for
the rows that reach it: in a branch of ``IF``, ``COALESCE``, ``||``, ``&&`` or ``IN``,
only when the branch is taken, as the Java engine does.  The message of the DuckDB
error is :data:`MARKER` followed by a JSON object, the message (``m``) and the values
as N-Triples terms (``v``, null for a value that could not be evaluated);
:func:`raise_error_of` turns such a DuckDB error into a :class:`RaiseError`.
"""
from __future__ import annotations

import json
import re
from typing import List, Optional

from . import ast as A

#: the start of the message of a DuckDB error raised by RAISE
MARKER = "SG-RAISE:"

#: the SQL macro of RAISE: a function DuckDB never folds (``error`` is volatile)
RAISE_MACRO = "sg_raise"

#: what the Java engine writes when the message cannot be evaluated
NO_MESSAGE = "Error raised by the query"


class RaiseError(RuntimeError):
    """The error raised by a ``RAISE(message, value...)`` expression of the query.

    ``message`` is the message given to RAISE (the lexical form of its first argument),
    ``values`` the values after it, as N-Triples terms (None for a value that could not
    be evaluated, such as an unbound variable).  ``str(error)`` is the message followed by
    the values, in the syntax of the Java engine: ``No email address for: "Bob", 1``."""

    def __init__(self, message: str, values: Optional[List[Optional[str]]] = None):
        self.message = message
        self.values: List[Optional[str]] = list(values or [])
        super().__init__(format_raised(message, self.values))


def format_raised(message: str, values: List[Optional[str]]) -> str:
    """The message followed by the values, as the Java engine's ``RaiseException``:
    ``message: v1, v2``, each value as Jena's ``FmtUtils.stringForNode`` writes it (numbers
    and booleans abbreviated, the prefixes rdf, rdfs, xsd, owl... of Jena), ``UNDEF``
    for a value that could not be evaluated."""
    if not values:
        return message
    return message + ": " + ", ".join("UNDEF" if v is None else format_node(v) for v in values)


def raise_error_of(exc: BaseException) -> Optional[RaiseError]:
    """The :class:`RaiseError` that a DuckDB error carries, or None."""
    text = str(exc)
    i = text.find(MARKER)
    if i < 0:
        return None
    try:
        payload, _ = json.JSONDecoder().raw_decode(text, i + len(MARKER))
    except ValueError:
        return RaiseError(text[i + len(MARKER):].strip())
    return RaiseError(message_of(payload.get("m")), payload.get("v") or [])


def message_of(nt: Optional[str]) -> str:
    """The message of RAISE from its first argument (an N-Triples term, None when it could
    not be evaluated): the lexical form of a literal, else the term as Jena writes it."""
    if nt is None:
        return NO_MESSAGE
    from .terms import parse_nt_term

    try:
        t = parse_nt_term(nt)
    except ValueError:
        return format_node(nt)
    return t.lexical if isinstance(t, A.Literal) else format_term(t)


# -- Jena's FmtUtils.stringForNode, with the global prefix map of ARQ ------------------

_GLOBAL_PREFIXES = [
    ("rdf", "http://www.w3.org/1999/02/22-rdf-syntax-ns#"),
    ("rdfs", "http://www.w3.org/2000/01/rdf-schema#"),
    ("xsd", "http://www.w3.org/2001/XMLSchema#"),
    ("owl", "http://www.w3.org/2002/07/owl#"),
    ("fn", "http://www.w3.org/2005/xpath-functions#"),
    ("sparql", "http://www.w3.org/ns/sparql#"),
    ("math", "http://www.w3.org/2005/xpath-functions/math#"),
    ("afn", "http://jena.apache.org/ARQ/function#"),
]

_PN_CHARS_BASE = ("A-Za-z\u00C0-\u00D6\u00D8-\u00F6\u00F8-\u02FF\u0370-\u037D\u037F-\u1FFF"
                  "\u200C-\u200D\u2070-\u218F\u2C00-\u2FEF\u3001-\uD7FF\uF900-\uFDCF\uFDF0-\uFFFD"
                  "\U00010000-\U000EFFFF")
_PN_CHARS_U = _PN_CHARS_BASE + "_"
_PN_CHARS = _PN_CHARS_U + "\\-0-9\u00B7\u0300-\u036F\u203F-\u2040"
_LOCAL = re.compile(f"^(?:[{_PN_CHARS_U}0-9%](?:[{_PN_CHARS}.:%]*[{_PN_CHARS}:%])?)?$")
_INTEGER = re.compile(r"^[+-]?[0-9]+$")
_DECIMAL = re.compile(r"^[+-]?([0-9]+(\.[0-9]*)?|\.[0-9]+)([eE][+-]?[0-9]+)?$")
_DOUBLE = re.compile(r"^\s*[+-]?(NaN|Infinity|(([0-9]+\.?[0-9]*|\.[0-9]+)([eE][+-]?[0-9]+)?))[fFdD]?\s*$")


def _uri(iri: str) -> str:
    for prefix, ns in _GLOBAL_PREFIXES:
        if iri.startswith(ns) and _LOCAL.match(iri[len(ns):]):
            return prefix + ":" + iri[len(ns):]
    return "<" + iri + ">"


def _esc(s: str) -> str:
    out = []
    for c in s:
        if c in ('\\', '"'):
            out.append("\\" + c)
        elif c == "\n":
            out.append("\\n")
        elif c == "\t":
            out.append("\\t")
        elif c == "\r":
            out.append("\\r")
        elif c == "\f":
            out.append("\\f")
        else:
            out.append(c)
    return "".join(out)


def format_term(t) -> str:
    """An RDF term (:mod:`ast` node) as Jena's ``FmtUtils.stringForNode``."""
    if isinstance(t, A.IRI):
        return _uri(t.value)
    if isinstance(t, A.BNode):
        return "_:" + t.label
    lex, lang, dt = t.lexical, t.lang, t.datatype
    xsd = "http://www.w3.org/2001/XMLSchema#"
    if dt == xsd + "integer" and _INTEGER.match(lex):
        return lex
    if dt == xsd + "decimal" and lex.find(".") > 0 and _DECIMAL.match(lex):
        return lex
    if dt == xsd + "double" and ("e" in lex or "E" in lex) and _DOUBLE.match(lex):
        return lex
    if dt == xsd + "boolean" and lex in ("true", "false"):
        return lex
    out = '"' + _esc(lex) + '"'
    if lang:
        return out + "@" + lang
    if dt and dt != xsd + "string":
        return out + "^^" + _uri(dt)
    return out


def format_node(nt: str) -> str:
    """An N-Triples term (an RDF 1.2 triple term ``<<( s p o )>>`` included) as Jena's
    ``FmtUtils.stringForNode``."""
    nt = nt.strip()
    if nt.startswith("<<("):
        return "<<( " + " ".join(format_node(x) for x in _split_triple(nt)) + " )>>"
    from .terms import parse_nt_term

    try:
        return format_term(parse_nt_term(nt))
    except ValueError:
        return nt


_TERM = re.compile(r'<[^>]*>|_:\S+|"(?:[^"\\]|\\.)*"(?:@[A-Za-z0-9-]+|\^\^<[^>]*>)?')


def _split_triple(nt: str) -> List[str]:
    """The three terms of an N-Triples triple term ``<<( s p o )>>``."""
    inner = nt[3:-3].strip()
    terms, i = [], 0
    while i < len(inner):
        if inner[i].isspace():
            i += 1
            continue
        if inner.startswith("<<(", i):
            depth, j = 0, i
            while j < len(inner):
                if inner.startswith("<<(", j):
                    depth += 1
                    j += 3
                elif inner.startswith(")>>", j):
                    depth -= 1
                    j += 3
                    if depth == 0:
                        break
                else:
                    j += 1
            terms.append(inner[i:j])
            i = j
            continue
        m = _TERM.match(inner, i)
        if not m:
            terms.append(inner[i:].strip())
            break
        terms.append(m.group(0))
        i = m.end()
    return terms


def raise_payload(message: str, values: List[Optional[str]]) -> str:
    """The message of the DuckDB error of a RAISE with a constant message and constant values
    (N-Triples terms, None for UNDEF): what :func:`raise_error_of` reads back."""
    from .terms import nt_literal

    return MARKER + json.dumps({"m": nt_literal(message), "v": list(values)})


def raise_sql(message: str, values: List[Optional[str]]) -> str:
    """The SQL of a data error raised by an iterator or a function (``RaiseException`` of
    the Java engine), with a constant message and values: ``sg_raise('…')``, evaluated
    where the SQL is evaluated."""
    from .terms import sql_string

    return f"{RAISE_MACRO}({sql_string(raise_payload(message, values))})"


class DataError(Exception):
    """A data error raised in Python (an iterator's UDF): its message is the payload of
    :func:`raise_payload`, so that the DuckDB error that wraps it is read back by
    :func:`raise_error_of` as a :class:`RaiseError`."""

    def __init__(self, message: str, values: List[Optional[str]]):
        super().__init__(raise_payload(message, values))
        self.message, self.values = message, values
