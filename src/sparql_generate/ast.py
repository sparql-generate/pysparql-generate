"""Abstract syntax tree of SPARQL-Generate (language version 2).

The tree mirrors the productions of ``spargl.jj`` (the JavaCC grammar of the
Java implementation).  Every node is a plain dataclass; the compiler walks the
tree without any behaviour attached to the nodes.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Sequence, Union

RDF = "http://www.w3.org/1999/02/22-rdf-syntax-ns#"
XSD = "http://www.w3.org/2001/XMLSchema#"
RDF_TYPE = RDF + "type"
RDF_FIRST = RDF + "first"
RDF_REST = RDF + "rest"
RDF_NIL = RDF + "nil"
RDF_LANGSTRING = RDF + "langString"
XSD_STRING = XSD + "string"
XSD_INTEGER = XSD + "integer"
XSD_DECIMAL = XSD + "decimal"
XSD_DOUBLE = XSD + "double"
XSD_BOOLEAN = XSD + "boolean"
SG_NS = "http://w3id.org/sparql-generate/"
SG_FUN = SG_NS + "fn/"
SG_ITER = SG_NS + "iter/"
SG_ST = SG_NS + "st/"
IANA = "https://www.iana.org/assignments/media-types/"


# --------------------------------------------------------------------------
# Terms (graph nodes) -------------------------------------------------------
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Var:
    name: str

    def __str__(self) -> str:
        return "?" + self.name


@dataclass(frozen=True)
class IRI:
    value: str

    def __str__(self) -> str:
        return "<" + self.value + ">"


@dataclass(frozen=True)
class Literal:
    lexical: str
    lang: Optional[str] = None
    datatype: Optional[str] = None

    def __str__(self) -> str:
        from .terms import nt_literal

        return nt_literal(self.lexical, self.lang, self.datatype)


@dataclass(frozen=True)
class BNode:
    """A blank node of a template.  ``label`` is the syntactic label (``_:b``)
    or a parser-generated one for ``[]``; ``anonymous`` tells which."""

    label: str
    anonymous: bool = False

    def __str__(self) -> str:
        return "_:" + self.label


@dataclass(frozen=True)
class XIRI:
    """``<http://ex.org/{ ?id }>``: an IRI with embedded expressions."""

    components: tuple  # of str | Expr


@dataclass(frozen=True)
class XLiteral:
    """``"{ ?x } m"@en`` or ``"{ ?x }"^^xsd:int``: a literal with embedded expressions."""

    components: tuple  # of str | Expr
    lang: Optional[str] = None
    datatype: Optional[object] = None  # IRI | XIRI | None


@dataclass(frozen=True)
class XExpr:
    """``?{ expr }``: an expression used as a graph node."""

    expr: object


@dataclass(frozen=True)
class ListNode:
    """``LIST( expr )`` as the object of a template triple."""

    expr: object


Term = Union[Var, IRI, Literal, BNode, XIRI, XLiteral, XExpr, ListNode]


# --------------------------------------------------------------------------
# Expressions ---------------------------------------------------------------
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Call:
    """A built-in (``name`` is the upper-case keyword, e.g. ``CONCAT``, ``&&``,
    ``=``, ``!``) or custom function call (``name`` is the function IRI)."""

    name: str
    args: tuple


@dataclass(frozen=True)
class Aggregate:
    name: str  # COUNT SUM MIN MAX AVG SAMPLE GROUP_CONCAT or custom IRI
    args: tuple
    distinct: bool = False
    separator: Optional[str] = None  # GROUP_CONCAT only
    star: bool = False  # COUNT(*)


@dataclass(frozen=True)
class Exists:
    pattern: object
    negated: bool = False


@dataclass(frozen=True)
class TemplateExpr:
    """A nested ``TEMPLATE { ... } . `` used as an expression."""

    query: object


Expr = Union[Var, IRI, Literal, XIRI, XLiteral, Call, Aggregate, Exists, TemplateExpr]


# --------------------------------------------------------------------------
# Query pattern elements ----------------------------------------------------
# --------------------------------------------------------------------------


@dataclass
class Triple:
    subject: object
    predicate: object  # Term or Path
    object: object


@dataclass
class Path:
    """Property path; ``op`` in ``link inverse seq alt zeroOrOne zeroOrMore
    oneOrMore negated``."""

    op: str
    args: tuple


@dataclass
class TriplesBlock:
    triples: List[Triple] = field(default_factory=list)


@dataclass
class Filter:
    expr: object


@dataclass
class Bind:
    expr: object
    var: Var


@dataclass
class Values:
    vars: List[Var]
    rows: List[List[Optional[object]]]  # None means UNDEF


@dataclass
class Optional_:
    pattern: object


@dataclass
class Union_:
    patterns: List[object]


@dataclass
class Minus:
    pattern: object


@dataclass
class Graph:
    node: object
    pattern: object


@dataclass
class Service:
    node: object
    pattern: object
    silent: bool = False


@dataclass
class Group:
    elements: List[object] = field(default_factory=list)


@dataclass
class SubSelect:
    query: object


# --------------------------------------------------------------------------
# Binding clauses -----------------------------------------------------------
# --------------------------------------------------------------------------


@dataclass
class IteratorClause:
    expr: Call
    vars: List[Var]


@dataclass
class SourceClause:
    source: object  # IRI | Var | XIRI | XExpr
    accept: Optional[object]
    var: Var


BindingClause = Union[IteratorClause, SourceClause, Bind]


# --------------------------------------------------------------------------
# Output clauses ------------------------------------------------------------
# --------------------------------------------------------------------------


@dataclass
class GenerateTriples:
    triples: List[Triple]


@dataclass
class SubQuery:
    query: "Query"


@dataclass
class GenerateGraph:
    """``GRAPH VarOrXIri { ... }`` in a GENERATE template: its triples are quads of the graph
    that the node names, and its sub-queries generate their default graph in that graph."""
    node: object  # Var | IRI | XIRI | XExpr
    elements: List[object] = field(default_factory=list)  # GenerateTriples | SubQuery


def flat_generate(clause) -> List[tuple]:
    """The elements of a GENERATE clause as ``(graph node or None, GenerateTriples |
    SubQuery)``, in the order of the template."""
    out: List[tuple] = []
    for el in clause or []:
        if isinstance(el, GenerateGraph):
            out.extend((el.node, x) for x in el.elements)
        else:
            out.append((None, el))
    return out


def has_quad_patterns(query) -> bool:
    """A GRAPH block in the template of the query or of its inline sub-queries (named
    queries are not looked at: their text is read at compile time)."""
    for el in getattr(query, "generate_clause", None) or []:
        if isinstance(el, GenerateGraph):
            return True
        if isinstance(el, SubQuery) and has_quad_patterns(el.query):
            return True
    return False


@dataclass
class TExpr:
    expr: object


@dataclass
class TBox:
    elements: List[object]


@dataclass
class TFormat:
    format: object
    elements: List[object]


@dataclass
class TGroup:
    elements: List[object]
    distinct: bool = False
    separator: Optional[str] = None


@dataclass
class OrderCondition:
    expr: object
    direction: int = 0  # 0 default, 1 asc, -1 desc


@dataclass
class DatasetClause:
    expr: Optional[object] = None
    named: bool = False
    generate: Optional["Query"] = None


@dataclass
class Query:
    kind: str = "GENERATE"  # GENERATE TEMPLATE SELECT PERFORM FUNCTION
    base: Optional[str] = None
    prefixes: dict = field(default_factory=dict)
    name: Optional[object] = None  # IRI | Var | XIRI | Call(IRI)
    signature: Optional[List[Var]] = None
    call_params: Optional[List[object]] = None
    generate_clause: Optional[List[object]] = None  # GenerateTriples | SubQuery | GenerateGraph
    template_clause: Optional[List[object]] = None
    template_before: Optional[object] = None
    template_separator: Optional[object] = None
    template_after: Optional[object] = None
    perform_clause: Optional[List[object]] = None
    function_expr: Optional[object] = None
    dataset_clauses: List[DatasetClause] = field(default_factory=list)
    binding_clauses: List[object] = field(default_factory=list)
    where: Optional[Group] = None
    group_by: List[object] = field(default_factory=list)  # Var | (expr, Var|None)
    having: List[object] = field(default_factory=list)
    order_by: List[OrderCondition] = field(default_factory=list)
    limit: Optional[int] = None
    offset: Optional[int] = None
    distinct: bool = False
    reduced: bool = False
    select_star: bool = False
    select_vars: List[object] = field(default_factory=list)  # (Var, expr|None)
    post_select: List[object] = field(default_factory=list)  # (Var, expr)
    values: Optional[Values] = None
    is_sub: bool = False
    source_text: Optional[str] = None

    @property
    def is_named_call(self) -> bool:
        """A sub-query of the form ``GENERATE <iri>(args) .``."""
        return self.is_sub and self.name is not None and self.generate_clause is None \
            and self.template_clause is None and self.perform_clause is None


def iter_vars(node) -> Sequence[Var]:
    """All variables mentioned in an expression or term, in order."""
    out: List[Var] = []

    def walk(n):
        if isinstance(n, Var):
            out.append(n)
        elif isinstance(n, (Call, Aggregate)):
            for a in n.args:
                walk(a)
        elif isinstance(n, (XIRI, XLiteral)):
            for c in n.components:
                if not isinstance(c, str):
                    walk(c)
            if isinstance(n, XLiteral) and n.datatype is not None:
                walk(n.datatype)
        elif isinstance(n, XExpr):
            walk(n.expr)
        elif isinstance(n, ListNode):
            walk(n.expr)
        elif isinstance(n, (list, tuple)):
            for a in n:
                walk(a)

    walk(node)
    return out
