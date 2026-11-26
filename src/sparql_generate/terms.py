"""RDF term helpers: IRI resolution and validation, N-Triples serialisation."""
from __future__ import annotations

import re
from typing import Optional
from urllib.parse import urlsplit

from . import ast as A

_ABSOLUTE = re.compile(r"^[A-Za-z][A-Za-z0-9+.\-]*:")
_BAD_IRI_CHARS = re.compile(r"[\x00-\x20<>\"{}|\\^`]")


def is_absolute_iri(s: str) -> bool:
    return bool(_ABSOLUTE.match(s))


def resolve_iri(ref: str, base: Optional[str], template: bool = False) -> str:
    """Resolve ``ref`` against ``base`` following RFC 3986.

    With ``template=True`` the reference is the constant head of an IRI
    template (``<http://ex.org/{ ?x }>``) and is only resolved when it is
    relative; a head that is already an absolute IRI is returned unchanged.
    """
    if base is None or is_absolute_iri(ref):
        return ref
    if ref == "":
        return base
    if template and not ref:
        return base
    if urlsplit(base).scheme == "urn":
        return base + ref
    return _resolve_rfc3986(ref, base)


_URI_REF = re.compile(r"^(?:([^:/?#]+):)?(?://([^/?#]*))?([^?#]*)(?:\?([^#]*))?(?:#(.*))?$", re.S)


def _resolve_rfc3986(ref: str, base: str) -> str:
    """RFC 3986 section 5.2 (as Jena resolves IRIs; ``urljoin`` drops empty segments)."""
    rs, ra, rp, rq, rf = _URI_REF.match(ref).groups()
    bs, ba, bp, bq, _ = _URI_REF.match(base).groups()
    if rs is not None:
        s, a, p, q = rs, ra, _remove_dot_segments(rp), rq
    elif ra is not None:
        s, a, p, q = bs, ra, _remove_dot_segments(rp), rq
    else:
        s, a = bs, ba
        if rp == "":
            p, q = bp, (rq if rq is not None else bq)
        else:
            if rp.startswith("/"):
                p = _remove_dot_segments(rp)
            elif ba is not None and bp == "":
                p = _remove_dot_segments("/" + rp)
            else:
                p = _remove_dot_segments(bp[:bp.rfind("/") + 1] + rp)
            q = rq
    out = (s + ":" if s is not None else "") + ("//" + a if a is not None else "") + p
    if q is not None:
        out += "?" + q
    if rf is not None:
        out += "#" + rf
    return out


def _remove_dot_segments(path: str) -> str:
    out = []
    while path:
        if path.startswith("../"):
            path = path[3:]
        elif path.startswith("./"):
            path = path[2:]
        elif path.startswith("/./"):
            path = path[2:]
        elif path == "/.":
            path = "/"
        elif path.startswith("/../"):
            path = path[3:]
            if out:
                out.pop()
        elif path == "/..":
            path = "/"
            if out:
                out.pop()
        elif path in (".", ".."):
            path = ""
        else:
            i = path.find("/", 1)
            seg, path = (path, "") if i < 0 else (path[:i], path[i:])
            out.append(seg)
    return "".join(out)


def valid_iri(s: str) -> bool:
    """Loose IRI well-formedness: absolute and free of forbidden characters."""
    return bool(s) and not _BAD_IRI_CHARS.search(s) and is_absolute_iri(s)


def escape_literal(s: str) -> str:
    return (
        s.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("\t", "\\t")
    )


def nt_literal(lex: str, lang: Optional[str] = None, datatype: Optional[str] = None) -> str:
    out = '"' + escape_literal(lex) + '"'
    if lang:
        return out + "@" + lang
    if datatype and datatype != A.XSD_STRING:
        return out + "^^<" + datatype + ">"
    return out


def nt_term(term) -> str:
    if isinstance(term, A.IRI):
        return "<" + term.value + ">"
    if isinstance(term, A.Literal):
        return nt_literal(term.lexical, term.lang, term.datatype)
    if isinstance(term, A.BNode):
        return "_:" + term.label
    raise TypeError(f"not a concrete term: {term!r}")


def sql_string(s: str) -> str:
    """A SQL string literal (single-quoted, quotes doubled)."""
    return "'" + s.replace("'", "''") + "'"


_NT_LITERAL = re.compile(r'^"((?:[^"\\]|\\.)*)"(?:@([A-Za-z0-9\-]+)|\^\^<([^>]*)>)?$')


def parse_nt_term(s: str):
    """Inverse of :func:`nt_term` for a single N-Triples term."""
    if s.startswith("<") and s.endswith(">"):
        return A.IRI(s[1:-1])
    if s.startswith("_:"):
        return A.BNode(s[2:])
    m = _NT_LITERAL.match(s)
    if not m:
        raise ValueError(f"not an N-Triples term: {s!r}")
    lex = m.group(1).encode("utf-8").decode("unicode_escape") if "\\" in m.group(1) else m.group(1)
    if "\\" in m.group(1):
        # unicode_escape mangles non-ASCII; redo properly
        lex = _unescape_nt(m.group(1))
    return A.Literal(lex, m.group(2), m.group(3))


def _unescape_nt(s: str) -> str:
    out = []
    i = 0
    while i < len(s):
        c = s[i]
        if c == "\\" and i + 1 < len(s):
            d = s[i + 1]
            if d == "n":
                out.append("\n")
            elif d == "r":
                out.append("\r")
            elif d == "t":
                out.append("\t")
            elif d == "u":
                out.append(chr(int(s[i + 2:i + 6], 16)))
                i += 6
                continue
            elif d == "U":
                out.append(chr(int(s[i + 2:i + 10], 16)))
                i += 10
                continue
            else:
                out.append(d)
            i += 2
        else:
            out.append(c)
            i += 1
    return "".join(out)
