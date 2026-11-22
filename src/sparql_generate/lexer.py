"""Tokenizer for SPARQL-Generate.

The tokenizer is pull-based and mode-aware: the parser asks for the next token
from a given offset in a given lexical mode.  Modes other than ``default`` are
the continuation modes entered after an embedded expression ``{ ... }`` inside
a string literal or an IRI (``"a{ ?x }b"``, ``<http://ex.org/{ ?id }>``),
mirroring the lexical states ``PARSING_XS1/XS2/XL1/XL2/XIRI`` of ``spargl.jj``.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional


class ParseError(Exception):
    """A syntax error, with the 1-based line and column of the offending text."""

    def __init__(self, message: str, line: int, col: int, text: Optional[str] = None):
        self.line = line
        self.col = col
        self.message = message
        snippet = ""
        if text is not None:
            lines = text.splitlines()
            if 1 <= line <= len(lines):
                src = lines[line - 1]
                snippet = "\n  " + src + "\n  " + " " * (col - 1) + "^"
        super().__init__(f"line {line}, column {col}: {message}{snippet}")


@dataclass
class Token:
    type: str
    value: object
    pos: int  # start offset in the source
    end: int  # end offset (exclusive)
    line: int
    col: int

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"Token({self.type}, {self.value!r}, {self.line}:{self.col})"


KEYWORDS = {
    # SPARQL-Generate
    "GENERATE", "SOURCE", "ACCEPT", "ITERATOR", "LIST", "EXPRESSIONS", "TEMPLATE", "BOX",
    "FORMAT", "PRAGMA", "FUNCTION", "BEFORE", "AFTER", "PERFORM",
    # SPARQL
    "BASE", "PREFIX", "SELECT", "DISTINCT", "REDUCED", "DESCRIBE", "CONSTRUCT", "ASK",
    "LIMIT", "OFFSET", "ORDER", "BY", "VALUES", "UNDEF", "ASC", "DESC", "NAMED", "FROM",
    "WHERE", "AND", "GRAPH", "OPTIONAL", "UNION", "MINUS", "BIND", "SERVICE", "EXISTS",
    "NOT", "AS", "GROUP", "HAVING", "SEPARATOR", "AGG", "COUNT", "MIN", "MAX", "SUM", "AVG",
    "STDEV", "SAMPLE", "GROUP_CONCAT", "FILTER", "BOUND", "COALESCE", "IN", "IF", "BNODE",
    "IRI", "URI", "STR", "STRLANG", "STRDT", "DATATYPE", "LANG", "LANGMATCHES", "ISURI",
    "ISIRI", "ISBLANK", "ISLITERAL", "ISNUMERIC", "REGEX", "SAMETERM", "RAND", "ABS",
    "CEIL", "FLOOR", "ROUND", "CONCAT", "SUBSTR", "STRLEN", "REPLACE", "UCASE", "LCASE",
    "ENCODE_FOR_URI", "CONTAINS", "STRSTARTS", "STRENDS", "STRBEFORE", "STRAFTER", "YEAR",
    "MONTH", "DAY", "HOURS", "MINUTES", "SECONDS", "TIMEZONE", "TZ", "NOW", "UUID",
    "STRUUID", "MD5", "SHA1", "SHA224", "SHA256", "SHA384", "SHA512", "TRUE", "FALSE",
    "SILENT", "DEFAULT", "ALL", "WITH", "USING", "A",
}
# WHEREVER is an alias of WHERE in spargl.jj
KEYWORD_ALIASES = {"WHEREVER": "WHERE"}

PN_CHARS_BASE = (
    "A-Za-z\u00C0-\u00D6\u00D8-\u00F6\u00F8-\u02FF\u0370-\u037D\u037F-\u1FFF"
    "\u200C-\u200D\u2070-\u218F\u2C00-\u2FEF\u3001-\uD7FF\uF900-\uFDCF\uFDF0-\uFFFD"
    "\U00010000-\U000EFFFF"
)
PN_CHARS_U = PN_CHARS_BASE + "_"
PN_CHARS = PN_CHARS_U + "\\-0-9\u00B7\u0300-\u036F\u203F-\u2040"
PN_PREFIX = f"[{PN_CHARS_BASE}](?:[{PN_CHARS}.]*[{PN_CHARS}])?"
PLX = r"(?:%[0-9A-Fa-f]{2}|\\[_~.\-!$&'()*+,;=/?#@%])"
PN_LOCAL = f"(?:[{PN_CHARS_U}:0-9]|{PLX})(?:(?:[{PN_CHARS}.:]|{PLX})*(?:[{PN_CHARS}:]|{PLX}))?"
VARNAME = f"[{PN_CHARS_U}0-9][{PN_CHARS_U}0-9\u00B7\u0300-\u036F\u203F-\u2040]*"

IRI_CHARS = "[^<>\"{}|^`\\\\\x00-\x20]"
RE_IRIREF = re.compile(f"<({IRI_CHARS}*)>")
RE_XIRI_START = re.compile(f"<({IRI_CHARS}*)\\{{")
RE_XIRI_MID = re.compile(f"\\}}({IRI_CHARS}*)\\{{")
RE_XIRI_END = re.compile(f"\\}}({IRI_CHARS}*)>")
RE_PNAME_LN = re.compile(f"({PN_PREFIX})?:({PN_LOCAL})")
RE_PNAME_NS = re.compile(f"({PN_PREFIX})?:")
RE_BLANK = re.compile(f"_:((?:[{PN_CHARS_U}0-9])(?:[{PN_CHARS}.]*[{PN_CHARS}])?)")
RE_VAR = re.compile(f"[?$]({VARNAME})")
RE_LANGTAG = re.compile(r"@([a-zA-Z]+(?:-[a-zA-Z0-9]+)*)")
RE_DOUBLE = re.compile(r"(?:[0-9]+\.[0-9]*[eE][+-]?[0-9]+|\.[0-9]+[eE][+-]?[0-9]+|[0-9]+[eE][+-]?[0-9]+)")
RE_DECIMAL = re.compile(r"[0-9]*\.[0-9]+")
RE_INTEGER = re.compile(r"[0-9]+")
RE_WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
RE_WS = re.compile(r"(?:[ \t\r\n\f]+|#[^\r\n]*)*")
RE_NIL = re.compile(r"\((?:[ \t\r\n\f]|#[^\r\n]*[\r\n])*\)")
RE_ANON = re.compile(r"\[(?:[ \t\r\n\f]|#[^\r\n]*[\r\n])*\]")

ECHAR = r"\\[tbnrf\\\"'{}]"
# short strings: body up to a closing quote or an unescaped '{'
RE_STR2 = re.compile(f'"((?:[^"\\\\\\n\\r{{]|{ECHAR}|\\\\u[0-9A-Fa-f]{{4}}|\\\\U[0-9A-Fa-f]{{8}})*)(["{{])')
RE_STR1 = re.compile(f"'((?:[^'\\\\\\n\\r{{]|{ECHAR}|\\\\u[0-9A-Fa-f]{{4}}|\\\\U[0-9A-Fa-f]{{8}})*)(['{{])")
RE_STR2_CONT = re.compile(f'\\}}((?:[^"\\\\\\n\\r{{]|{ECHAR}|\\\\u[0-9A-Fa-f]{{4}}|\\\\U[0-9A-Fa-f]{{8}})*)(["{{])')
RE_STR1_CONT = re.compile(f"\\}}((?:[^'\\\\\\n\\r{{]|{ECHAR}|\\\\u[0-9A-Fa-f]{{4}}|\\\\U[0-9A-Fa-f]{{8}})*)(['{{])")
# long strings: body may contain one or two quotes in a row, up to ''' or an unescaped '{'
RE_LONG2 = re.compile(f'"""((?:(?:"|"")?(?:[^"\\\\{{]|{ECHAR}|\\\\u[0-9A-Fa-f]{{4}}|\\\\U[0-9A-Fa-f]{{8}}))*)(?:(""")|((?:"|"")?)\\{{)')
RE_LONG1 = re.compile(f"'''((?:(?:'|'')?(?:[^'\\\\{{]|{ECHAR}|\\\\u[0-9A-Fa-f]{{4}}|\\\\U[0-9A-Fa-f]{{8}}))*)(?:(''')|((?:'|'')?)\\{{)")
RE_LONG2_CONT = re.compile(f'\\}}((?:(?:"|"")?(?:[^"\\\\{{]|{ECHAR}|\\\\u[0-9A-Fa-f]{{4}}|\\\\U[0-9A-Fa-f]{{8}}))*)(?:(""")|((?:"|"")?)\\{{)')
RE_LONG1_CONT = re.compile(f"\\}}((?:(?:'|'')?(?:[^'\\\\{{]|{ECHAR}|\\\\u[0-9A-Fa-f]{{4}}|\\\\U[0-9A-Fa-f]{{8}}))*)(?:(''')|((?:'|'')?)\\{{)")

PUNCT = [
    "^^", "||", "&&", "!=", "<=", ">=", "->", "<-",
    "(", ")", "{", "}", "[", "]", ";", ",", ".", "=", "<", ">", "!", "~", "|", "^",
    "+", "-", "*", "/", "@", "?",
]

_UNESCAPE = {"t": "\t", "b": "\b", "n": "\n", "r": "\r", "f": "\f", "\\": "\\", '"': '"', "'": "'", "{": "{", "}": "}"}


def unescape(s: str, line: int = 0, col: int = 0) -> str:
    """Resolve SPARQL string escapes (ECHAR, ``\\{``, ``\\}``, ``\\uXXXX``, ``\\UXXXXXXXX``)."""
    if "\\" not in s:
        return s
    out = []
    i = 0
    n = len(s)
    while i < n:
        c = s[i]
        if c != "\\":
            out.append(c)
            i += 1
            continue
        if i + 1 >= n:
            raise ParseError("dangling backslash in string", line, col)
        d = s[i + 1]
        if d in _UNESCAPE:
            out.append(_UNESCAPE[d])
            i += 2
        elif d == "u":
            out.append(chr(int(s[i + 2:i + 6], 16)))
            i += 6
        elif d == "U":
            out.append(chr(int(s[i + 2:i + 10], 16)))
            i += 10
        else:
            raise ParseError(f"illegal escape sequence \\{d}", line, col)
    return "".join(out)


class Lexer:
    def __init__(self, text: str):
        if text.startswith("\ufeff"):
            text = text[1:]
        self.text = text
        self._line_starts = [0]
        for m in re.finditer("\n", text):
            self._line_starts.append(m.end())

    def position(self, pos: int):
        """1-based (line, column) of an offset."""
        import bisect

        li = bisect.bisect_right(self._line_starts, pos) - 1
        return li + 1, pos - self._line_starts[li] + 1

    def error(self, message: str, pos: int) -> ParseError:
        line, col = self.position(pos)
        return ParseError(message, line, col, self.text)

    def _tok(self, type_: str, value, pos: int, end: int) -> Token:
        line, col = self.position(pos)
        return Token(type_, value, pos, end, line, col)

    # -- modes ------------------------------------------------------------
    def token(self, pos: int, mode: str = "default") -> Token:
        if mode == "default":
            return self._default(pos)
        return self._continuation(pos, mode)

    def _continuation(self, pos: int, mode: str) -> Token:
        text = self.text
        if pos >= len(text) or text[pos] != "}":
            raise self.error("expected '}' closing the embedded expression", pos)
        line, col = self.position(pos)
        if mode == "xiri":
            m = RE_XIRI_MID.match(text, pos)
            if m:
                return self._tok("XIRI_MID", m.group(1), pos, m.end())
            m = RE_XIRI_END.match(text, pos)
            if m:
                return self._tok("XIRI_END", m.group(1), pos, m.end())
            raise self.error("unterminated IRI with embedded expression", pos)
        if mode in ("xs1", "xs2"):
            m = (RE_STR1_CONT if mode == "xs1" else RE_STR2_CONT).match(text, pos)
            if not m:
                raise self.error("unterminated string literal after embedded expression", pos)
            body = unescape(m.group(1), line, col)
            if m.group(2) == "{":
                return self._tok("STRING_MID", body, pos, m.end())
            return self._tok("STRING_END", body, pos, m.end())
        if mode in ("xl1", "xl2"):
            m = (RE_LONG1_CONT if mode == "xl1" else RE_LONG2_CONT).match(text, pos)
            if not m:
                raise self.error("unterminated long string literal after embedded expression", pos)
            if m.group(2) is not None:
                return self._tok("STRING_END", unescape(m.group(1), line, col), pos, m.end())
            body = m.group(1) + (m.group(3) or "")
            return self._tok("STRING_MID", unescape(body, line, col), pos, m.end())
        raise ValueError(mode)

    def _default(self, pos: int) -> Token:
        text = self.text
        m = RE_WS.match(text, pos)
        pos = m.end()
        if pos >= len(text):
            return self._tok("EOF", None, pos, pos)
        c = text[pos]
        line, col = self.position(pos)
        # variables and ?{ ${
        if c in "?$":
            if pos + 1 < len(text) and text[pos + 1] == "{":
                return self._tok("START_XEXPR", c, pos, pos + 2)
            m = RE_VAR.match(text, pos)
            if m:
                return self._tok("VAR", m.group(1), pos, m.end())
            return self._tok("?", "?", pos, pos + 1)
        if c == "<":
            m = RE_IRIREF.match(text, pos)
            if m:
                return self._tok("IRIREF", m.group(1), pos, m.end())
            m = RE_XIRI_START.match(text, pos)
            if m:
                return self._tok("XIRI_START", m.group(1), pos, m.end())
        if c == '"' or c == "'":
            return self._string(pos, c, line, col)
        if c == "_" and text.startswith("_:", pos):
            m = RE_BLANK.match(text, pos)
            if m:
                return self._tok("BLANK_NODE_LABEL", m.group(1), pos, m.end())
            raise self.error("malformed blank node label", pos)
        if c == "@":
            m = RE_LANGTAG.match(text, pos)
            if m:
                return self._tok("LANGTAG", m.group(1), pos, m.end())
        if c == "(":
            m = RE_NIL.match(text, pos)
            if m:
                return self._tok("NIL", None, pos, m.end())
        if c == "[":
            m = RE_ANON.match(text, pos)
            if m:
                return self._tok("ANON", None, pos, m.end())
        if c.isdigit() or (c == "." and pos + 1 < len(text) and text[pos + 1].isdigit()):
            for typ, rx in (("DOUBLE", RE_DOUBLE), ("DECIMAL", RE_DECIMAL), ("INTEGER", RE_INTEGER)):
                m = rx.match(text, pos)
                if m:
                    return self._tok(typ, m.group(0), pos, m.end())
        # prefixed names (before words, since a word may be a prefix)
        m = RE_PNAME_LN.match(text, pos)
        if m:
            return self._tok("PNAME_LN", (m.group(1) or "", m.group(2)), pos, m.end())
        m = RE_PNAME_NS.match(text, pos)
        if m:
            return self._tok("PNAME_NS", m.group(1) or "", pos, m.end())
        m = RE_WORD.match(text, pos)
        if m:
            word = m.group(0)
            upper = word.upper()
            upper = KEYWORD_ALIASES.get(upper, upper)
            if upper in KEYWORDS:
                return self._tok("KW", upper, pos, m.end())
            raise self.error(f"unexpected word {word!r}", pos)
        for p in PUNCT:
            if text.startswith(p, pos):
                return self._tok(p, p, pos, pos + len(p))
        raise self.error(f"unexpected character {c!r}", pos)

    def _string(self, pos: int, quote: str, line: int, col: int) -> Token:
        text = self.text
        if text.startswith(quote * 3, pos):
            m = (RE_LONG1 if quote == "'" else RE_LONG2).match(text, pos)
            if not m:
                raise self.error("unterminated long string literal", pos)
            if m.group(2) is not None:
                return self._tok("STRING", unescape(m.group(1), line, col), pos, m.end())
            body = m.group(1) + (m.group(3) or "")
            tok = self._tok("STRING_START", unescape(body, line, col), pos, m.end())
            tok.value = (tok.value, "xl1" if quote == "'" else "xl2")
            return tok
        m = (RE_STR1 if quote == "'" else RE_STR2).match(text, pos)
        if not m:
            raise self.error("unterminated string literal", pos)
        body = unescape(m.group(1), line, col)
        if m.group(2) == "{":
            return self._tok("STRING_START", (body, "xs1" if quote == "'" else "xs2"), pos, m.end())
        return self._tok("STRING", body, pos, m.end())
