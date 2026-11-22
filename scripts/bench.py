#!/usr/bin/env python
"""Micro-benchmark: pysparql-generate against the Java SPARQL-Generate CLI on
synthetic CSV and JSON documents (10k / 100k / 1M rows), a join-free mapping
and one with a nested GENERATE.  Best of N runs; wall time and peak RSS.

    python scripts/bench.py [--sizes 10000 100000 1000000] [--runs 3] [--jar PATH] [--no-java]
                            [--java-model] [--no-compare] [--out results.md]

The Java engine needs ``java`` (17+) and a JAR: ``bench/jars/sparql-generate-2.2.2.jar``
(the release asset: ``gh release download v2.2.2 -R sparql-generate/sparql-generate
-p sparql-generate-2.2.2.jar -D bench/jars``) if present, else the local build
``../sparql-generate/sparql-generate-all/target/sparql-generate.jar``, else the 2.1.0 asset.  Java runs with ``-s`` (streamed output, the
fair setting for memory) unless ``--java-model`` is given (in-memory Model,
written as N-Triples at the end).  Its ``-o`` is absolute: since 2.2.0 a
relative ``-o`` is resolved against the working directory, not ``-d``.

Both outputs are compared as *sets* of triples (``--no-compare`` skips it):
the Java streamed output (prefixed Turtle, one triple per line) is expanded to
N-Triples, and numeric literals are compared by value (the two engines write
different lexical forms for JSON doubles).  The report gives the number of
lines written, the number of distinct triples, and the size of the symmetric
difference between the two sets.
"""
from __future__ import annotations

import argparse
import json
import os
import random
import re
import shutil
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DATA = os.path.join(ROOT, "bench", "data")
RESULTS = os.path.join(ROOT, "bench", "results")
PINNED_JAR = os.path.join(ROOT, "bench", "jars", "sparql-generate-2.2.2.jar")  # GitHub release asset 2.2.2
LOCAL_JAR = os.path.join(ROOT, "..", "sparql-generate", "sparql-generate-all", "target", "sparql-generate.jar")
RELEASED_JAR = os.path.join(ROOT, "bench", "jars", "sparql-generate-2.1.0.jar")  # GitHub release asset 2.1.0
DEFAULT_JAR = next((j for j in (PINNED_JAR, LOCAL_JAR) if os.path.exists(j)), RELEASED_JAR)
# version of the generated data; bump it when make_data changes so that stale files are not reused
DATA_VERSION = 2

PREFIXES = """BASE <http://example.com/>
PREFIX iter: <http://w3id.org/sparql-generate/iter/>
PREFIX fun: <http://w3id.org/sparql-generate/fn/>
PREFIX xsd: <http://www.w3.org/2001/XMLSchema#>
PREFIX ex: <http://example.com/ns#>
PREFIX wgs84: <http://www.w3.org/2003/01/geo/wgs84_pos#>
"""

QUERIES = {
    # join-free: one row -> 5 triples
    "csv-flat": PREFIXES + """GENERATE {
  <stop/{ ?id }> a ex:Stop ; ex:name ?name ; wgs84:lat "{ ?lat }"^^xsd:decimal ; wgs84:long "{ ?long }"^^xsd:decimal ;
     ex:city <city/{ ?city }> .
}
ITERATOR iter:CSV(<http://example.com/data.csv>, "id", "name", "lat", "long", "city") AS ?id ?name ?lat ?long ?city
""",
    # nested GENERATE: one sub-query per row iterating over a split field -> 5 + 2 triples per row
    "csv-nested": PREFIXES + """GENERATE {
  <stop/{ ?id }> a ex:Stop ; ex:name ?name ; wgs84:lat "{ ?lat }"^^xsd:decimal ; wgs84:long "{ ?long }"^^xsd:decimal .
  GENERATE {
    <stop/{ ?id }> ex:tag <tag/{ ?tag }> .
  } ITERATOR iter:Split(?tags, ";") AS ?tag .
}
ITERATOR iter:CSV(<http://example.com/data.csv>, "id", "name", "lat", "long", "tags") AS ?id ?name ?lat ?long ?tags
""",
    "json-flat": PREFIXES + """GENERATE {
  <stop/{ ?id }> a ex:Stop ; ex:name ?name ; wgs84:lat ?lat ; wgs84:long ?long .
}
ITERATOR iter:JSONPath(<http://example.com/data.json>, "$[*]", "$.id", "$.name", "$.venue.lat", "$.venue.long") AS ?e ?id ?name ?lat ?long
""",
    "json-nested": PREFIXES + """GENERATE {
  <stop/{ ?id }> a ex:Stop ; ex:name ?name .
  GENERATE {
    <stop/{ ?id }> ex:tag <tag/{ ?tag }> .
  } ITERATOR iter:JSONPath(?e, "$.tags[*]") AS ?tag .
}
ITERATOR iter:JSONPath(<http://example.com/data.json>, "$[*]", "$.id", "$.name") AS ?e ?id ?name
""",
}


def make_data(n: int):
    os.makedirs(DATA, exist_ok=True)
    csv_path = os.path.join(DATA, f"data_v{DATA_VERSION}_{n}.csv")
    json_path = os.path.join(DATA, f"data_v{DATA_VERSION}_{n}.json")
    if os.path.exists(csv_path) and os.path.exists(json_path):
        return csv_path, json_path
    random.seed(1)
    import csv

    with open(csv_path, "w", newline="", encoding="utf-8") as f, open(json_path, "w", encoding="utf-8") as g:
        w = csv.writer(f)
        w.writerow(["id", "name", "lat", "long", "city", "tags"])
        g.write("[\n")
        for i in range(n):
            lat = round(random.uniform(-90, 90), 6)
            lon = round(random.uniform(-180, 180), 6)
            name = f"Stop {i}" + (" é" if i % 7 == 0 else "")
            tags = [f"t{i % 3}", f"u{i % 5}"]  # two distinct tags: no duplicate triple
            w.writerow([i, name, lat, lon, f"City {i % 1000}", ";".join(tags)])
            g.write(("," if i else "") + json.dumps({"id": i, "name": name, "venue": {"lat": lat, "long": lon}, "tags": tags}) + "\n")
        g.write("]\n")
    return csv_path, json_path


def run_measured(cmd, cwd=None):
    """Run a command under GNU time, return (seconds, peak RSS in MB)."""
    import re

    full = ["/usr/bin/time", "-f", "__TIME__ %e %M"] + cmd
    p = subprocess.run(full, cwd=cwd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    err = p.stderr.decode(errors="replace")
    m = re.search(r"__TIME__ ([0-9.]+) ([0-9]+)", err)
    if p.returncode != 0 or not m:
        print(err[-2000:], file=sys.stderr)
        raise RuntimeError(f"command failed: {' '.join(cmd)}")
    return float(m.group(1)), int(m.group(2)) / 1024.0


XSD = "http://www.w3.org/2001/XMLSchema#"
_NUM_LIT = re.compile(r'"([^"\\]*)"\^\^<' + re.escape(XSD) + r'(double|float|decimal|integer)> \.$')
_TERM = re.compile(r'<[^>]*>|"(?:[^"\\]|\\.)*"(?:@[A-Za-z0-9-]+|\^\^(?:<[^>]*>|[A-Za-z][\w.-]*:\S*))?'
                   r'|_:\S+|[+-]?(?:[0-9]+\.?[0-9]*|\.[0-9]+)(?:[eE][+-]?[0-9]+)?(?=\s|$)|true|false|[A-Za-z][\w.-]*:\S*|a(?=\s)')


def _canon_number(lex: str, dt: str) -> str:
    from decimal import Decimal, InvalidOperation

    try:
        if dt in ("double", "float"):
            return repr(float(lex))
        if dt == "decimal":
            return str(Decimal(lex).normalize())
        return str(int(lex))
    except (ValueError, InvalidOperation):
        return lex


def canonical_nt(line: str) -> str:
    """An N-Triples line with its numeric literal (object) written by value."""
    if "^^<" + XSD in line:
        m = _NUM_LIT.search(line)
        if m:
            return f'{line[:m.start()]}"{_canon_number(m.group(1), m.group(2))}"^^<{XSD}{m.group(2)}> .'
    return line


def java_stream_to_nt(lines):
    """N-Triples lines from the streamed output of the Java CLI (``-s``): one
    triple per line, prefixed names, Turtle abbreviations for numbers."""
    prefixes = {}
    for line in lines:
        line = line.rstrip("\n")
        if not line:
            continue
        if line.startswith("@prefix "):
            _, pfx, iri, _dot = line.split(" ", 3)
            prefixes[pfx[:-1]] = iri[1:-1]
            continue
        if line.startswith("@base "):
            continue
        out = []
        for m in _TERM.finditer(line[:-2] if line.endswith(" .") else line):
            t = m.group(0)
            if t[0] in '<_':
                out.append(t)
            elif t[0] == '"':
                k = t.rfind('"^^')
                if k > 0 and t[k + 3] != "<":
                    pfx, local = t[k + 3:].split(":", 1)
                    t = t[:k + 3] + "<" + prefixes.get(pfx, pfx + ":") + local + ">"
                out.append(t)
            elif t == "a":
                out.append("<http://www.w3.org/1999/02/22-rdf-syntax-ns#type>")
            elif t in ("true", "false"):
                out.append(f'"{t}"^^<{XSD}boolean>')
            elif t[0].isdigit() or t[0] in "+-.":
                dt = "double" if "e" in t.lower() else "decimal" if "." in t else "integer"
                out.append(f'"{t}"^^<{XSD}{dt}>')
            else:
                pfx, local = t.split(":", 1)
                out.append("<" + prefixes.get(pfx, pfx + ":") + local + ">")
        yield " ".join(out) + " ."


def triple_set(path: str, java_stream: bool = False):
    """(lines written, set of canonical N-Triples lines)."""
    with open(path, encoding="utf-8") as f:
        lines = java_stream_to_nt(f) if java_stream else (l.rstrip("\n") for l in f if l.strip())
        n = 0
        acc = set()
        for l in lines:
            n += 1
            acc.add(canonical_nt(l))
    return n, acc


def count_lines(path, java_stream=False):
    n = 0
    with open(path, "rb") as f:
        for l in f:
            if not (java_stream and l.startswith(b"@")):
                n += 1
    return n


def bench_python(qname, workdir, runs, python):
    out = os.path.join(workdir, f"py_{qname}.nt")
    best = None
    for _ in range(runs):
        dt, rss = run_measured([python, "-m", "sparql_generate.cli", "-d", workdir, "-q", f"{qname}.rqg", "-o", out, "--no-network"])
        if best is None or dt < best[0]:
            best = (dt, rss)
    return best + (out,)


def find_java() -> str:
    """A ``java`` of version 17 or more (the JAR targets Java 17)."""
    import glob
    import re

    candidates = [os.environ.get("JAVA_HOME", "") + "/bin/java"] + sorted(glob.glob("/usr/lib/jvm/*/bin/java"), reverse=True) + ["java"]
    for c in candidates:
        if not c or (c != "java" and not os.path.exists(c)):
            continue
        try:
            v = subprocess.run([c, "-version"], capture_output=True, text=True).stderr
        except OSError:
            continue
        m = re.search(r'version "(\d+)', v)
        if m and int(m.group(1)) >= 17:
            return c
    return "java"


JAVA = None


def bench_java(qname, workdir, runs, jar, stream=True):
    global JAVA
    JAVA = JAVA or find_java()
    out = os.path.abspath(os.path.join(workdir, f"java_{qname}.{'ttl' if stream else 'nt'}"))
    best = None
    for _ in range(runs):
        if os.path.exists(out):
            os.unlink(out)
        # -o must be absolute (2.2.0 resolves a relative -o against the working directory)
        dt, rss = run_measured([JAVA, "-Xmx8g", "-jar", jar, "-d", workdir, "-q", f"{qname}.rqg", "-o", out,
                                "-fo", "NT", "-l", "ERROR"] + (["-s"] if stream else []), cwd=workdir)
        if best is None or dt < best[0]:
            best = (dt, rss)
    if not os.path.exists(out):
        raise RuntimeError(f"no output file {out}")
    return best + (out,)


def java_version(jar):
    """Version of the engine in the JAR (from the Maven metadata)."""
    import zipfile

    try:
        with zipfile.ZipFile(jar) as z:
            props = z.read("META-INF/maven/fr.mines-stetienne.ci.sparql-generate/sparql-generate-jena/pom.properties").decode()
        return re.search(r"version=(\S+)", props).group(1)
    except (KeyError, OSError, AttributeError, zipfile.BadZipFile):
        return "?"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sizes", nargs="*", type=int, default=[10_000, 100_000, 1_000_000])
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--jar", default=DEFAULT_JAR)
    ap.add_argument("--no-java", action="store_true")
    ap.add_argument("--java-model", action="store_true", help="Java without -s (in-memory Model, set semantics)")
    ap.add_argument("--no-compare", action="store_true", help="do not compare the two outputs as sets")
    ap.add_argument("--queries", nargs="*", default=list(QUERIES))
    ap.add_argument("--java-max-size", type=int, default=1_000_000, help="skip Java above this size")
    ap.add_argument("--out", default=os.path.join(RESULTS, "bench.md"))
    args = ap.parse_args()
    os.makedirs(RESULTS, exist_ok=True)
    python = sys.executable
    java = not args.no_java and os.path.exists(args.jar) and shutil.which("java")
    if not args.no_java and not java:
        print(f"Java engine not available (jar {args.jar}); running Python only", file=sys.stderr)
    stream = not args.java_model
    rows = []
    for n in args.sizes:
        csv_path, json_path = make_data(n)
        workdir = os.path.join(DATA, f"work_{n}")
        os.makedirs(workdir, exist_ok=True)
        conf = {"documentset": [
            {"uri": "http://example.com/data.csv", "mediatype": "text/csv", "path": os.path.relpath(csv_path, workdir)},
            {"uri": "http://example.com/data.json", "mediatype": "application/json", "path": os.path.relpath(json_path, workdir)},
        ]}
        with open(os.path.join(workdir, "sparql-generate-conf.json"), "w") as f:
            json.dump(conf, f)
        for qname in args.queries:
            with open(os.path.join(workdir, f"{qname}.rqg"), "w") as f:
                f.write(QUERIES[qname])
            pt, prss, pout = bench_python(qname, workdir, args.runs, python)
            pn = count_lines(pout)
            row = {"rows": n, "query": qname, "py_s": pt, "py_mb": prss, "py_triples": pn}
            print(f"{qname:12s} {n:>9d} rows  python {pt:7.2f}s {prss:7.0f}MB {pn:>9d} triples  "
                  f"{pn / pt / 1000:8.0f}k triples/s", flush=True)
            if java and n <= args.java_max_size:
                try:
                    jt, jrss, jout = bench_java(qname, workdir, args.runs, args.jar, stream)
                    jn = count_lines(jout, java_stream=stream)
                    row.update({"java_s": jt, "java_mb": jrss, "java_triples": jn})
                    print(f"{'':12s} {'':>9s}       java   {jt:7.2f}s {jrss:7.0f}MB {jn:>9d} triples  speedup x{jt / pt:5.1f}", flush=True)
                    if not args.no_compare:
                        _, pset = triple_set(pout)
                        _, jset = triple_set(jout, java_stream=stream)
                        row.update({"py_distinct": len(pset), "java_distinct": len(jset), "diff": len(pset ^ jset)})
                        print(f"{'':12s} {'':>9s}       distinct triples: python {len(pset)}, java {len(jset)}, "
                              f"symmetric difference {row['diff']}", flush=True)
                        for t in list(pset - jset)[:3]:
                            print(f"{'':12s}   only python: {t}", flush=True)
                        for t in list(jset - pset)[:3]:
                            print(f"{'':12s}   only java:   {t}", flush=True)
                        del pset, jset
                except RuntimeError as ex:
                    print(f"             java failed: {ex}", file=sys.stderr)
                    row["java_error"] = str(ex)
            rows.append(row)
    import duckdb

    info = {"date": time.strftime("%Y-%m-%d"), "cpus": os.cpu_count(), "runs": args.runs, "duckdb": duckdb.__version__,
            "python": sys.version.split()[0]}
    if java:
        info.update({"jar": os.path.relpath(args.jar, ROOT), "engine": java_version(args.jar),
                     "java": subprocess.run([JAVA or find_java(), "-version"], capture_output=True, text=True).stderr.splitlines()[0],
                     "java_mode": "-s (streamed)" if stream else "in-memory Model"})
    with open(args.out, "w") as f:
        f.write("Benchmark `scripts/bench.py`: " + ", ".join(f"{k} {v}" for k, v in info.items()) +
                ". Best of the runs, wall time and peak RSS per process (GNU time). `triples`: lines written; "
                "`distinct`: distinct triples, numeric literals compared by value; `diff`: size of the symmetric "
                "difference of the two sets of triples.\n\n")
        f.write("| query | rows | pysparql-generate s | MB | triples | triples/s | Java s | MB | triples | distinct py / Java | diff | speedup |\n"
                "|---|---|---|---|---|---|---|---|---|---|---|---|\n")
        for r in rows:
            if "java_s" in r:
                d = f"{r['py_distinct']} / {r['java_distinct']} | {r['diff']}" if "diff" in r else "- | -"
                j = f"{r['java_s']:.2f} | {r['java_mb']:.0f} | {r['java_triples']} | {d} | x{r['java_s'] / r['py_s']:.1f}"
            else:
                j = "- | - | - | - | - | -"
            f.write(f"| {r['query']} | {r['rows']} | {r['py_s']:.2f} | {r['py_mb']:.0f} | {r['py_triples']} | {r['py_triples'] / r['py_s']:.0f} | {j} |\n")
    with open(os.path.splitext(args.out)[0] + ".json", "w") as f:
        json.dump({"info": info, "rows": rows}, f, indent=1)
    print(f"written {args.out}")


if __name__ == "__main__":
    main()
