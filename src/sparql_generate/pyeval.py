"""Row-by-row evaluation helpers used by the Python UDF fallback: JSONPath
(Jayway-compatible subset through jsonpath-ng), XPath (lxml), CSS selectors
(lxml + cssselect), GeoJSON to WKT.

Values exchanged with DuckDB use the dynamic term representation: a dict
``{"k": kind, "v": lexical, "d": datatype}`` (see :mod:`sparql_generate.termtypes`).
"""
from __future__ import annotations

import base64
import copy
import json
import re
from functools import lru_cache
from typing import Any, Dict, List, Optional

from . import ast as A

XML_DT = A.IANA + "application/xml"
HTML_DT = A.IANA + "text/html"
JSON_DT = A.IANA + "application/json"
WKT_DT = "http://www.opengis.net/ont/geosparql#wktLiteral"


def any_iri(v: str) -> Dict[str, Any]:
    return {"k": 0, "v": v, "d": None}


def any_bnode(v: str) -> Dict[str, Any]:
    return {"k": 1, "v": v, "d": None}


def any_lit(v: str, dt: Optional[str] = None, lang: Optional[str] = None) -> Dict[str, Any]:
    if lang:
        return {"k": 2, "v": v, "d": "@" + lang}
    if dt == A.XSD_STRING:
        dt = None
    return {"k": 2, "v": v, "d": dt}


def any_from_python(value) -> Optional[Dict[str, Any]]:
    """The term Jena's ``nodeForObject`` builds for a JSON value."""
    if value is None:
        return None
    if isinstance(value, bool):
        return any_lit("true" if value else "false", A.XSD_BOOLEAN)
    if isinstance(value, int):
        return any_lit(str(value), A.XSD_INTEGER)
    if isinstance(value, JsonFloat) and not ("e" in value.text or "E" in value.text):
        from decimal import Decimal

        from .expressions import decimal_lexical

        return any_lit(decimal_lexical(Decimal(value.text)), A.XSD_DECIMAL)
    if isinstance(value, float):
        return any_lit(java_double(value), A.XSD_DOUBLE)
    if isinstance(value, str):
        return any_lit(value)
    return any_lit(json.dumps(value, ensure_ascii=False, separators=(",", ":")))


def java_double(d: float) -> str:
    """``Double.toString`` lookalike."""
    if d != d:
        return "NaN"
    if d in (float("inf"), float("-inf")):
        return "Infinity" if d > 0 else "-Infinity"
    r = repr(d)
    if "e" in r or "E" in r:
        mant, exp = r.lower().split("e")
        if "." not in mant:
            mant += ".0"
        return f"{mant}E{int(exp)}"
    return r


def json_text(value) -> str:
    """A JSON value as the compact text DuckDB's JSON type expects."""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


# -- JSONPath -------------------------------------------------------------------

_NATIVE_JSONPATH = re.compile(r"^\$(\.[A-Za-z_][A-Za-z0-9_\-]*|\[[0-9]+\]|\[\*\])*$")
_DEFINITE = re.compile(r"^\$(\.[A-Za-z_][A-Za-z0-9_\-]*|\[[0-9]+\])*$")


def jsonpath_is_native(path: str) -> bool:
    """Can DuckDB's ``json_extract`` evaluate this path exactly like Jayway?"""
    return bool(_NATIVE_JSONPATH.match(path))


def jsonpath_is_definite(path: str) -> bool:
    return bool(_DEFINITE.match(path))


@lru_cache(maxsize=4096)
def _compile_jsonpath(path: str):
    from jsonpath_ng.ext import parse as jp_parse

    p = path
    # Jayway accepts unquoted keys with characters jsonpath-ng needs quoted
    p = re.sub(r"\.([A-Za-z_][\w\-]*[\-][\w\-]*)", lambda m: f"['{m.group(1)}']", p)
    # Jayway filters: [?(@.a=='x')] -> jsonpath-ng ext: [?(@.a=='x')] is accepted; normalise quotes
    p = re.sub(r"==\s*'([^']*)'", lambda m: '== "' + m.group(1) + '"', p)
    return jp_parse(p)


class _ParsedCache:
    """Parsed-document cache keyed by the document text."""

    def __init__(self, maxsize: int = 64):
        self.maxsize = maxsize
        self.data: Dict[str, Any] = {}

    def get(self, text: str, loader):
        v = self.data.get(text)
        if v is None:
            if len(self.data) >= self.maxsize:
                self.data.clear()
            v = loader(text)
            self.data[text] = v
        return v


_json_cache = _ParsedCache()
_xml_cache = _ParsedCache()
_html_cache = _ParsedCache()


class JsonFloat(float):
    """A JSON number with a fraction or an exponent, with its text: a decimal point without an
    exponent gives an exact xsd:decimal, an exponent an xsd:double (the Java engine 2.3)."""


def _parse_float(text: str) -> float:
    f = JsonFloat(text)
    f.text = text
    return f


def json_loads(text: str):
    return json.loads(text, parse_float=_parse_float)


def parse_json(text: str):
    return _json_cache.get(text, json_loads)


def jsonpath_all(doc, path: str) -> List[Any]:
    """All matches of ``path`` in the (parsed) document, Jayway ALWAYS_RETURN_LIST style."""
    if path == "$":
        return [doc]
    expr = _compile_jsonpath(path)
    return [m.value for m in expr.find(doc)]


def jsonpath_first(doc, path: str):
    """Jayway ``limit(1).read(path)`` for both definite and indefinite paths; raises
    when nothing matches."""
    matches = jsonpath_all(doc, path)
    if not matches:
        raise LookupError(path)
    return matches[0]


# -- XPath ---------------------------------------------------------------------

_XMLNS = re.compile(r'\sxmlns="[^"]*"')
_XMLDECL = re.compile(r"^\s*<\?xml[^>]*\?>")


def _parse_xml(text: str):
    from lxml import etree

    cleaned = _XMLNS.sub("", text)
    cleaned = _XMLDECL.sub("", cleaned)
    parser = etree.XMLParser(recover=True, huge_tree=True, remove_blank_text=False)
    try:
        return etree.fromstring(cleaned.encode("utf-8"), parser)
    except Exception:
        from lxml import html as lhtml

        return lhtml.fromstring(cleaned)


def parse_xml(text: str):
    return _xml_cache.get(text, _parse_xml)


def xml_node_term(node) -> Optional[Dict[str, Any]]:
    """Jena ``nodeForNode``: text/attribute nodes give a string, elements the
    serialised element typed ``application/xml``."""
    from lxml import etree

    if node is None:
        return None
    if isinstance(node, str):
        return any_lit(str(node))
    if isinstance(node, (int, float, bool)):
        return any_from_python(node)
    if isinstance(node, etree._Comment):
        return any_lit(node.text or "")
    return any_lit(etree.tostring(node, encoding="unicode", with_tail=False), XML_DT)


def xpath_nodes(root, path: str) -> List[Any]:
    from lxml import etree

    if root is None:
        return []
    tree = root.getroottree() if hasattr(root, "getroottree") else root
    if path.strip() == "/":
        # the document node: Jena serialises the whole document
        return [root]
    try:
        res = tree.xpath(path)
    except etree.XPathEvalError:
        res = etree.ElementTree(root).xpath(path)
    if isinstance(res, list):
        return res
    return [res]


def xpath_sub_root(element):
    """The element as the root of its own document (Jena re-parses the serialised
    element before evaluating auxiliary XPath expressions)."""
    from lxml import etree

    c = copy.deepcopy(element)
    c.tail = None
    etree.ElementTree(c)
    return c


# -- CSS selectors --------------------------------------------------------------

_WS = re.compile(r"\s+")


def _parse_html(text: str):
    from lxml import html as lhtml

    return lhtml.fromstring(text)


def parse_html(text: str):
    return _html_cache.get(text, _parse_html)


@lru_cache(maxsize=1024)
def _css(selector: str):
    from lxml.cssselect import CSSSelector

    return CSSSelector(selector)


def css_select(root, selector: str) -> List[Any]:
    if root is None:
        return []
    return _css(selector)(root)


def css_value(root, selector: str) -> Optional[Dict[str, Any]]:
    """``fun:CSSPath`` on an element: ``/text()`` suffix gives normalised text,
    ``@attr`` an attribute, otherwise the outer HTML."""
    from lxml import etree

    if selector.endswith("/text()"):
        els = css_select(root, selector[:-7]) if selector[:-7] else [root]
        if not els:
            return None
        return any_lit(_WS.sub(" ", els[0].text_content()).strip())
    m = re.search(r"@([^\"'>/=^$*~@]+)$", selector)
    if m:
        attr = m.group(1)
        sel = selector[: m.start()]
        els = css_select(root, sel) if sel else [root]
        if not els or attr not in els[0].attrib:
            return None
        return any_lit(els[0].get(attr))
    els = css_select(root, selector)
    if not els:
        return None
    return any_lit(etree.tostring(els[0], method="html", encoding="unicode", with_tail=False), HTML_DT)


# -- GeoJSON ---------------------------------------------------------------------


def _coord(c) -> str:
    return " ".join(java_double(float(x)) for x in c)


def _coords(cs) -> str:
    return "(" + ", ".join(_coord(c) for c in cs) + ")"


def geojson_to_wkt(geom) -> Optional[str]:
    """WKT exactly as the Java implementation writes it (JTS-like spacing:
    ``POINT(x y)``, ``LINESTRING (x y, ...)``, ``POLYGON((...))``, ``MULTIPOINT (...)``)."""
    if not geom:
        return None
    t = geom.get("type")
    c = geom.get("coordinates")
    if t == "Point":
        return "POINT(" + _coord(c) + ")"
    if t == "LineString":
        return "LINESTRING " + _coords(c)
    if t == "Polygon":
        return "POLYGON(" + ", ".join(_coords(r) for r in c) + ")"
    if t == "MultiPoint":
        return "MULTIPOINT " + _coords(c)
    if t == "MultiLineString":
        return "MULTILINESTRING(" + ", ".join(_coords(ls) for ls in c) + ")"
    if t == "MultiPolygon":
        # one parenthesised list of rings per polygon (the Java engine flattened them into
        # one polygon until 2.2.3, ac4fa37)
        return "MULTIPOLYGON(" + ", ".join("(" + ", ".join(_coords(r) for r in poly) + ")" for poly in c) + ")"
    if t == "GeometryCollection":
        return "GEOMETRYCOLLECTION(" + ", ".join(geojson_to_wkt(g) for g in geom.get("geometries", [])) + ")"
    return None


def geojson_features(text: str):
    doc = parse_json(text)
    feats = doc.get("features", []) if isinstance(doc, dict) else []
    out = []
    for f in feats:
        wkt = geojson_to_wkt(f.get("geometry"))
        props = f.get("properties")
        out.append((any_lit(wkt, WKT_DT) if wkt else None,
                    any_lit(json_text(props if props is not None else {}), JSON_DT)))
    return out


# -- CBOR ------------------------------------------------------------------------


def cbor_to_json(lexical: str):
    """The Java implementation base64-decodes the literal and reads the bytes as
    JSON text; fall back to a real CBOR decoding."""
    raw = base64.b64decode(lexical)
    try:
        return json_loads(raw.decode("utf-8"))
    except Exception:
        import cbor2

        return cbor2.loads(raw)
