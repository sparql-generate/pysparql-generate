#!/usr/bin/env python
"""Run the Java test cases (``sparql-generate-jena/src/test/resources/generate/*``)
and compare the generated graph with ``expected_output.ttl`` by RDF graph
isomorphism (or the generated dataset with ``expected_output.nq`` by RDF dataset
isomorphism, for a query with GRAPH blocks).

    python scripts/conformance.py [--tests DIR] [--only NAME ...] [--no-network] [-v]
"""
from __future__ import annotations

import argparse
import os
import sys
import time
import traceback

import rdflib
from rdflib.compare import isomorphic, graph_diff, to_isomorphic

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
import sparql_generate as sg  # noqa: E402

DEFAULT_TESTS = os.path.join(os.path.dirname(__file__), "..", "..", "sparql-generate", "sparql-generate-jena",
                             "src", "test", "resources", "generate")


def run_case(d: str, allow_network: bool, verbose: bool):
    conf_path = os.path.join(d, "sparql-generate-conf.json")
    conf = sg.load_conf(conf_path) if os.path.exists(conf_path) else {"_dir": d}
    qfile = os.path.join(d, conf.get("query", "query.rqg"))
    with open(qfile, encoding="utf-8") as f:
        text = f.read()
    nq_path = os.path.join(d, "expected_output.nq")
    if os.path.exists(nq_path):  # a dataset (GRAPH blocks): compared by dataset isomorphism
        return run_quads_case(text, conf, nq_path, allow_network, verbose)
    t0 = time.perf_counter()
    with sg.Execution(text, conf=conf, allow_network=allow_network) as ex:
        got = ex.to_graph()
        warnings = list(ex.warnings)
    dt = time.perf_counter() - t0
    exp = rdflib.Graph()
    exp_path = os.path.join(d, "expected_output.ttl")
    if os.path.exists(exp_path) and os.path.getsize(exp_path) > 0:
        exp.parse(exp_path, format="turtle")
    ok = isomorphic(got, exp)
    detail = ""
    if not ok and verbose:
        both, only_exp, only_got = graph_diff(to_isomorphic(exp), to_isomorphic(got))
        detail = (f"\n    expected {len(exp)} triples, got {len(got)}\n"
                  f"    missing ({len(only_exp)}):\n" + "".join(f"      {s} {p} {o}\n" for s, p, o in list(only_exp)[:8]) +
                  f"    unexpected ({len(only_got)}):\n" + "".join(f"      {s} {p} {o}\n" for s, p, o in list(only_got)[:8]))
    if warnings and verbose:
        detail += "".join(f"\n    warning: {w}" for w in warnings)
    return ok, len(got), len(exp), dt, detail


def run_quads_case(text, conf, nq_path, allow_network, verbose):
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tests"))
    from quadsutil import datasets_isomorphic

    t0 = time.perf_counter()
    with sg.Execution(text, conf=conf, allow_network=allow_network) as ex:
        got = "".join(f"{s} {p} {o}{' ' + g if g is not None else ''} .\n" for s, p, o, g in ex.quads(distinct=True))
        warnings = list(ex.warnings)
    dt = time.perf_counter() - t0
    with open(nq_path, encoding="utf-8") as f:
        exp = f.read()
    ok, detail = datasets_isomorphic(got, exp)
    detail = ("\n    " + detail) if not ok and verbose else ""
    if warnings and verbose:
        detail += "".join(f"\n    warning: {w}" for w in warnings)
    return ok, len(got.splitlines()), len(exp.splitlines()), dt, detail


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tests", default=DEFAULT_TESTS)
    ap.add_argument("--only", nargs="*", default=None)
    ap.add_argument("--no-network", action="store_true")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    names = sorted(n for n in os.listdir(args.tests) if os.path.isdir(os.path.join(args.tests, n)))
    if args.only:
        names = [n for n in names if n in args.only]
    passed = failed = errors = 0
    for n in names:
        d = os.path.join(args.tests, n)
        try:
            ok, ngot, nexp, dt, detail = run_case(d, not args.no_network, args.verbose)
        except Exception as ex:  # noqa: BLE001
            errors += 1
            msg = str(ex).splitlines()[0] if str(ex) else type(ex).__name__
            print(f"ERROR {n:22s} {msg[:150]}")
            if args.verbose:
                traceback.print_exc()
            continue
        if ok:
            passed += 1
            print(f"PASS  {n:22s} {ngot:6d} triples {dt * 1000:7.0f} ms")
        else:
            failed += 1
            print(f"FAIL  {n:22s} got {ngot} expected {nexp} {dt * 1000:7.0f} ms{detail}")
    print(f"\n{passed} passed, {failed} failed, {errors} errors, {len(names)} total")
    return 0 if failed == errors == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
