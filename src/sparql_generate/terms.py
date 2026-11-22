"""RDF term helpers: IRI resolution and validation, N-Triples serialisation."""
from __future__ import annotations

import re
from typing import Optional
from urllib.parse import urljoin, urlsplit

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
    # urljoin knows a fixed list of schemes for hierarchical references; force
    # generic behaviour by swapping the scheme when needed.
    parts = urlsplit(base)
    scheme = parts.scheme
    if scheme and scheme not in ("http", "https", "file", "ftp", "urn"):
        fake = "http" + base[len(scheme):]
        out = urljoin(fake, ref)
        return scheme + out[len("http"):]
    if scheme == "urn":
        return base + ref
    return urljoin(base, ref)


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
