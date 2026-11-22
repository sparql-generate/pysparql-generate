"""RDF dataset isomorphism for the tests: two N-Quads documents are isomorphic when one
bijection of their blank nodes maps one onto the other, graph names included.

The quads are encoded as one graph: the triples of the default graph as they are, a quad
``s p o g`` as a node ``q`` with ``q :s s``, ``q :p p``, ``q :o o``, ``q :g g``; two
datasets are isomorphic exactly when their encodings are (rdflib's graph isomorphism)."""
from __future__ import annotations

from typing import Tuple

import rdflib
from rdflib.compare import graph_diff, isomorphic, to_isomorphic

_Q = rdflib.Namespace("urn:x-quad:")


def encode(nquads: str) -> rdflib.Graph:
    ds = rdflib.Dataset(default_union=False)
    if nquads.strip():
        ds.parse(data=nquads, format="nquads")
    out = rdflib.Graph()
    for s, p, o, g in ds.quads((None, None, None, None)):
        g = g.identifier if isinstance(g, rdflib.Graph) else g
        if g is None or g == rdflib.graph.DATASET_DEFAULT_GRAPH_ID:
            out.add((s, p, o))
            continue
        q = rdflib.BNode()
        out.add((q, _Q.s, s))
        out.add((q, _Q.p, p))
        out.add((q, _Q.o, o))
        out.add((q, _Q.g, g))
    return out


def datasets_isomorphic(a: str, b: str) -> Tuple[bool, str]:
    """(isomorphic, a short description of the difference)."""
    ga, gb = encode(a), encode(b)
    if isomorphic(ga, gb):
        return True, ""
    _, only_a, only_b = graph_diff(to_isomorphic(ga), to_isomorphic(gb))
    return False, (f"{len(ga)} vs {len(gb)} encoded triples; only in the first: "
                   + "; ".join(" ".join(x.n3() for x in t) for t in list(only_a)[:4])
                   + " | only in the second: " + "; ".join(" ".join(x.n3() for x in t) for t in list(only_b)[:4]))
