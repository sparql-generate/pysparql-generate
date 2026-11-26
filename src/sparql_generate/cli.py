"""Command line: ``sparql-generate-py``, option-compatible with the Java CLI
where it makes sense.

    sparql-generate-py -d DIR [-q query.rqg] [-o out.nt] [-fo NT|TTL|...] [-c conf.json] [-l LEVEL]
    sparql-generate-py -q query.rqg --document IRI=PATH ... -o out.nt
"""
from __future__ import annotations

import argparse
import logging
import os
import re
import sys
import time

import duckdb

from . import (Execution, ParseError, CompileError, QUAD_FORMATS, QuadsNotSupported, RaiseError, TargetError,
               load_conf, __version__)
from .ast import has_quad_patterns


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="sparql-generate-py",
                                description="SPARQL-Generate compiled to DuckDB: generate RDF or text from CSV, JSON, XML, HTML...")
    p.add_argument("-d", "--dir", default=None, help="working directory holding the query, documentset and sparql-generate-conf.json (default: directory of the query, or '.')")
    p.add_argument("-q", "--query-file", default=None, help="query file (default: 'query' of conf.json, else query.rqg)")
    p.add_argument("-c", "--conf", default=None, help="configuration file (default: DIR/sparql-generate-conf.json)")
    p.add_argument("-o", "--output", default=None, help="output file (default: standard output)")
    p.add_argument("-fo", "--format-output", default=None,
                   help="output format: NT (default; NQ for a query with GRAPH blocks), NQ, TRIG, TTL, XML, "
                        "JSON-LD... for GENERATE; JSON, CSV, TSV for SELECT")
    p.add_argument("-ao", "--append-output", action="store_true", help="append to the output file")
    p.add_argument("-b", "--base", default=None, help="base IRI")
    p.add_argument("-l", "--log-level", default="WARN", help="TRACE, DEBUG, INFO, WARN, ERROR, OFF")
    p.add_argument("-f", "--log-file", default=None, help="log file")
    p.add_argument("--document", action="append", default=[], metavar="IRI=PATH[;MEDIATYPE]", help="add a document to the document set")
    p.add_argument("--named-query", action="append", default=[], metavar="IRI=PATH", help="add a named query")
    p.add_argument("--dataset", default=None, help="RDF file used as the dataset (N-Triples/N-Quads read natively, Turtle and others by rdflib)")
    p.add_argument("--distinct", action="store_true", help="remove duplicate triples")
    p.add_argument("--threads", type=int, default=None, help="DuckDB threads")
    p.add_argument("--memory-limit", default=None, help="DuckDB memory limit, e.g. 4GB")
    p.add_argument("--no-network", action="store_true", help="never fetch documents from the Web")
    p.add_argument("--stream-messages", type=int, default=None,
                   help="read at most so many messages of iter:WebSocket and iter:MQTTSubscribe streams")
    p.add_argument("--stream-seconds", type=float, default=None,
                   help="read the streams of iter:WebSocket and iter:MQTTSubscribe for at most so many seconds")
    p.add_argument("--fail-on-missing", action="store_true", default=None,
                   help="fail (status 1) when a document, a graph or a named query cannot be found")
    p.add_argument("--graphs-to-files", action="store_true", default=None,
                   help="GENERATE: write each named graph whose IRI is the folder of the base followed by the path "
                        "of an RDF file (GRAPH <out/people.ttl>, format and compression by extension) to that file, in "
                        "the folder of a file: base or else in the working directory; GRAPH <dump.nq#g=IRI> names "
                        "the graph IRI in dump.nq. The other graphs stay in the output.")
    p.add_argument("--targets", default=None, metavar="FILE",
                   help="a JSON file that describes the target files of --graphs-to-files (implied): "
                        "{\"path\": {\"format\": \"N-Quads\", \"compression\": \"gzip\", \"encoding\": \"UTF-16\"}}, "
                        "paths relative to the output folder")
    p.add_argument("-x", "--syntax", action="store_true", help="only check the syntax of the query")
    p.add_argument("--explain", action="store_true", help="print the compiled SQL plan")
    p.add_argument("--time", action="store_true", help="print timings to stderr")
    p.add_argument("-B", dest="bindings", action="append", default=[], metavar="var=term", help="initial binding ?var = Turtle term")
    p.add_argument("--version", action="version", version=f"pysparql-generate {__version__}")
    return p


_LEVELS = {"TRACE": logging.DEBUG, "DEBUG": logging.DEBUG, "INFO": logging.INFO, "WARN": logging.WARNING,
           "WARNING": logging.WARNING, "ERROR": logging.ERROR, "OFF": logging.CRITICAL + 10}


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=_LEVELS.get(args.log_level.upper(), logging.WARNING),
                        filename=args.log_file, format="%(levelname)s %(message)s")
    log = logging.getLogger("sparql-generate")
    t0 = time.perf_counter()

    dir_ = args.dir
    if dir_ is None:
        dir_ = os.path.dirname(os.path.abspath(args.query_file)) if args.query_file else "."
    conf = None
    conf_path = args.conf or os.path.join(dir_, "sparql-generate-conf.json")
    if os.path.exists(conf_path):
        conf = load_conf(conf_path)
    elif args.dir is not None:
        conf = {"_dir": os.path.abspath(dir_)}  # like the Java CLI: DIR/dataset/default.ttl is the default graph
    qfile = args.query_file or (conf or {}).get("query") or "query.rqg"
    if not os.path.isabs(qfile):
        cand = os.path.join(dir_, qfile)
        qfile = cand if os.path.exists(cand) else qfile
    try:
        with open(qfile, encoding="utf-8") as f:
            query_text = f.read()
    except OSError as ex:
        print(f"cannot read query {qfile}: {ex}", file=sys.stderr)
        return 2

    documents = None
    if args.document:
        documents = list((conf or {}).get("documentset") or [])
        for d in args.document:
            iri, _, rest = d.partition("=")
            path, _, mt = rest.partition(";")
            documents.append({"uri": iri, "path": os.path.abspath(path), "mediatype": mt or None})
    named = {}
    for nq in args.named_query:
        iri, _, path = nq.partition("=")
        named[iri] = path
    dataset = args.dataset  # a file: N-Triples/N-Quads read natively, other syntaxes by rdflib
    bindings = None
    if args.bindings:
        from .parser import Parser

        b = {}
        for s in args.bindings:
            var, _, term = s.partition("=")
            b[var.lstrip("?$")] = Parser(term).parse_var_or_term()
        bindings = [b]

    # --graphs-to-files, --targets (relative to the current directory; the key targets of the
    # configuration file, to the working directory), as the Java command line 2.6.0
    graphs_to_files = bool(args.graphs_to_files or (conf or {}).get("graphsToFiles"))
    targets = os.path.abspath(args.targets) if args.targets else None
    if targets is None and (conf or {}).get("targets"):
        targets = os.path.join(os.path.abspath(dir_), conf["targets"])
    graphs_to_files = graphs_to_files or targets is not None
    base = args.base
    if graphs_to_files and base is None and not (conf or {}).get("base"):
        # as Jena: without base, the relative IRIs of the query are resolved against the
        # current directory, where the files are written
        from pathlib import Path

        base = Path.cwd().as_uri() + "/"

    if args.syntax:
        from .parser import parse

        try:
            parse(query_text, base=args.base)
        except ParseError as ex:
            print(ex, file=sys.stderr)
            return 1
        print("syntax OK")
        return 0

    if args.output and not args.append_output:
        # the output file exists, empty, before the execution, as the Java command line opens it:
        # an error that stops the execution (RAISE, iter:SQL) leaves it empty, not absent
        out0 = args.output if os.path.isabs(args.output) or not args.dir else os.path.join(args.dir, args.output)
        try:
            open(out0, "w").close()
        except OSError:
            pass
    try:
        ex = Execution(query_text, base=base, documents=documents, base_dir=dir_, named_queries=named or None,
                       dataset=dataset, bindings=bindings, conf=conf, allow_network=not args.no_network,
                       threads=args.threads, memory_limit=args.memory_limit, debug=args.explain,
                       fail_on_missing=args.fail_on_missing, stream_messages=args.stream_messages,
                       stream_seconds=args.stream_seconds)
    except (ParseError, CompileError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    except RaiseError as e:
        # RAISE(message, value...) in the query: its message and values, status 1
        print(f"error: {e}", file=sys.stderr)
        return 1
    for w in ex.warnings:
        log.warning(w)
    if graphs_to_files:
        if ex.plan.kind != "GENERATE":
            log.warning("--graphs-to-files and --targets apply to GENERATE queries only.")
        else:
            try:
                ex.graphs_to_files(targets=targets, output_dir=dir_)
            except (TargetError, RaiseError, CompileError) as e:
                print(f"error: {e}", file=sys.stderr)
                ex.close()
                return 1
    t1 = time.perf_counter()
    if args.explain:
        for s in ex.engine.sql_log:
            print(s + ";\n", file=sys.stderr)
        if ex.plan.kind == "GENERATE":
            print(ex.plan.triples_sql(args.distinct) + ";", file=sys.stderr)
        elif ex.plan.kind == "TEMPLATE":
            print(ex.plan.text_sql + ";", file=sys.stderr)
        elif ex.plan.kind == "PERFORM":
            print(ex.plan.actions_sql() + ";", file=sys.stderr)
        else:
            print(ex.plan.select_sql + ";", file=sys.stderr)
    fmt = (args.format_output or ("TEXT" if ex.plan.kind != "GENERATE" else "NQ" if ex.has_quads else "NT")).lower()
    try:
        refused = ex.has_quads and fmt not in QUAD_FORMATS and _quads_generated(ex, graphs_to_files)
    except RaiseError as e:
        print(f"error: {e}", file=sys.stderr)
        ex.close()
        return 1
    if refused:
        # as the Java engine: a format that holds triples only cannot hold the named graphs
        print(f"error: {QuadsNotSupported(_FORMAT_LABELS.get(fmt, args.format_output))}", file=sys.stderr)
        ex.close()
        return 1
    if ex.plan.kind == "SELECT" and fmt == "text":
        fmt = "csv"
    try:
        if args.output:
            out = args.output
            if not os.path.isabs(out) and args.dir:
                out = os.path.join(args.dir, out)
            if args.append_output:
                with open(out, "a", encoding="utf-8") as f:
                    n = ex.write(f, format=fmt, distinct=args.distinct)
            else:
                n = ex.write(out, format=fmt, distinct=args.distinct)
        else:
            n = ex.write(sys.stdout, format=fmt, distinct=args.distinct)
    except (CompileError, RaiseError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    except duckdb.Error as e:  # an error raised while the rows are computed (a UDF's lookup)
        m = re.search(r"Document not found: \S+", str(e))
        print(f"error: {m.group(0) if m else e}", file=sys.stderr)
        return 1
    finally:
        ex.close()
    t2 = time.perf_counter()
    if args.time:
        print(f"compile {t1 - t0:.3f}s  output {t2 - t1:.3f}s  total {t2 - t0:.3f}s  triples {n}", file=sys.stderr)
    return 0


_FORMAT_LABELS = {"nt": "N-Triples", "ntriples": "N-Triples", "n-triples": "N-Triples", "ttl": "Turtle",
                  "turtle": "Turtle", "n3": "N3", "xml": "RDF/XML", "rdf": "RDF/XML", "json-ld": "JSON-LD",
                  "jsonld": "JSON-LD"}


def _quads_generated(ex, graphs_to_files: bool = False) -> bool:
    """The query has GRAPH blocks (refused before the execution, as the Java engine does),
    or the named queries it calls generate quads; with --graphs-to-files, a named graph that
    no file holds reaches the output."""
    if has_quad_patterns(ex.query) and not graphs_to_files:
        return True
    return ex.generates_named_graphs()


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
