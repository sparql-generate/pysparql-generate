# pysparql-generate

A new implementation of [SPARQL-Generate](https://github.com/sparql-generate/sparql-generate)
(language version 2) with a Python API, built for speed: the query is **compiled
to a set-at-a-time relational plan** and executed by [DuckDB](https://duckdb.org),
instead of being evaluated solution by solution. Triples are produced as columns
of N-Triples terms and written in bulk.

SPARQL-Generate extends SPARQL 1.1 to generate RDF (`GENERATE`) or text
(`TEMPLATE`) from RDF datasets and heterogeneous documents: CSV, JSON, XML, HTML,
GeoJSON, CBOR, plain text...

```sparql
PREFIX iter: <http://w3id.org/sparql-generate/iter/>
PREFIX fun: <http://w3id.org/sparql-generate/fn/>
PREFIX ex: <http://example.org/>

GENERATE {
  <http://example.org/stop/{ ?id }> a ex:Stop ; ex:name ?name ;
      ex:lat "{ ?lat }"^^xsd:decimal .
  GENERATE { <http://example.org/stop/{ ?id }> ex:tag <http://example.org/tag/{ ?tag }> . }
  ITERATOR iter:Split(?tags, ";") AS ?tag .
}
ITERATOR iter:CSV(<http://example.org/stops.csv>, "id", "name", "lat", "tags") AS ?id ?name ?lat ?tags
```

## Install

```sh
pip install -e .            # Python 3.11+; pulls duckdb, pyarrow, rdflib, lxml, jsonpath-ng, cbor2
pip install -e '.[test]'    # + pytest
pip install -e '.[sql]'     # iter:SQL on PostgreSQL (psycopg2), MySQL (PyMySQL), SQL Server (pyodbc + Microsoft's ODBC driver)
```

## Python API

```python
import sparql_generate as sg

docs = {"http://example.org/stops.csv": "data/stops.csv"}      # IRI -> path | bytes | str | {"path":..., "mediatype":...}

for s, p, o in sg.generate(query, documents=docs):            # N-Triples term strings
    ...
n = sg.generate_to(query, "out.nt", documents=docs)            # bulk N-Triples (DuckDB COPY); format="ttl"/"xml"/... via rdflib
g = sg.to_graph(query, documents=docs)                         # rdflib.Graph
# GRAPH blocks in a template: quads
for s, p, o, g in sg.generate_quads(query, documents=docs): ...  # g is None in the default graph
ds = sg.to_dataset(query, documents=docs)                       # rdflib.Dataset
sg.generate_to(query, "out.nq", format="nq", documents=docs)   # N-Quads (or format="trig")
text = sg.template(template_query, documents=docs)             # TEMPLATE queries
rows = sg.select(select_query, dataset=rdflib_graph)           # SELECT queries: [{var: nt-term-or-None}]
acts = sg.perform(perform_query)                               # PERFORM queries: [(action IRI, [nt-term-or-None])]

ex = sg.Execution(query, documents=docs, base_dir="dir", conf=sg.load_conf("dir/sparql-generate-conf.json"),
                  named_queries={"http://example.org/q2": "dir/q2.rqg"}, dataset=None, bindings=None,
                  allow_network=False, threads=8, memory_limit="4GB")
ex.plan.kind            # GENERATE | TEMPLATE | SELECT | PERFORM
ex.triples()            # iterator of (s, p, o)
ex.arrow()              # pyarrow.Table with columns s, p, o
ex.batches()            # stream of pyarrow.RecordBatch (s, p, o), computed as it is read
ex.actions()            # PERFORM: iterator of (action IRI, [t1, ..., tk]), in order
ex.write(path_or_file, format="nt", distinct=False)
ex.graphs_to_files(targets=None, output_dir="dir")  # named graphs that name RDF files written to them
ex.warnings             # e.g. documents not found
ex.close()

try:
    sg.generate_to(query, "out.nt", documents=docs)
except sg.RaiseError as e:    # RAISE(message, value...) evaluated by the query
    print(e.message, e.values)  # "No email address for", ['"Bob"', '"1"^^<http://www.w3.org/2001/XMLSchema#integer>']
    print(e)                    # No email address for: "Bob", 1
```

Keyword arguments (all optional): `base`, `documents`, `base_dir` (relative
document paths, and `BASE`-relative IRIs looked up as files like the Java CLI),
`named_queries`, `dataset` (an `rdflib.Graph`/`Dataset`, or the path of an RDF
file, for `WHERE` clauses with triple patterns: N-Triples and N-Quads are read by
DuckDB without rdflib, about ten times faster; Turtle, TriG, RDF/XML and N3 by pyoxigraph
when it is installed, `pip install 'pysparql-generate[fast]'`, about seven times faster;
else by rdflib), `bindings` (`[{var: ast term}]`), `conf` (a parsed
`sparql-generate-conf.json`), `allow_network` (default `True`: `http(s)`
documents not in the document set are fetched), `threads`, `memory_limit`,
`distinct` (deduplicate triples; off by default, outputs may contain duplicates).

`RAISE(message, value...)` (SPARQL-Generate 2.6) stops the execution with a
`sg.RaiseError`: `message` is the lexical form of its first argument, `values` the other
arguments as N-Triples terms (None for an unbound variable or an error), and `str(e)` the
message followed by the values as the Java engine writes them. It is raised when the query
is compiled or when its output is read, before anything is written to the output.

`ex.graphs_to_files(targets=None, output_dir=None)` writes the named graphs whose IRI is a
relative path to an RDF file, resolved against the base (`GRAPH <out/people.ttl> { ... }`),
to that file, and leaves the other graphs in the outputs of the execution, as the Java
command line does with `--graphs-to-files` (see below). `targets` is the description of the
targets, a dict or the path of its JSON file; `output_dir` the folder of the files when the
base IRI is not a `file:` IRI (default: `base_dir`, else the current directory). It returns
the number of quads written to each file; a graph name that cannot be written raises
`sg.TargetError` with the message of the Java engine.

## Command line

Option-compatible with the Java CLI where it makes sense:

```sh
sparql-generate-py -d DIR [-q query.rqg] [-o out.nt] [-fo NT|NQ|TRIG|TTL|JSON-LD|CSV|JSON] [-c conf.json] [-l WARN]
sparql-generate-py -q query.rqg --document http://ex.org/d.csv=data.csv -o out.nt
sparql-generate-py -q query.rqg --no-network --threads 8 --memory-limit 4GB --time
sparql-generate-py -q query.rqg --explain          # print the compiled SQL plan
sparql-generate-py -q query.rqg -x                 # syntax check only
sparql-generate-py -d DIR -b http://example.org/ --graphs-to-files -fo NQUADS
sparql-generate-py -d DIR -b http://example.org/ --targets targets.json -fo NQUADS
```

`-d DIR` reads `DIR/sparql-generate-conf.json` (`query`, `base`, `documentset`,
`namedqueries`, `graph`, `namedgraphs`, `failOnMissing`), as the Java CLI does; `-o`
is relative to `DIR`. Exit code 1 on a parse or compilation error (message on stderr).
The default output format is N-Triples, or N-Quads for a query that generates quads in
named graphs; such a query with `-fo NT` (or another format that holds triples only) is
an error, as in the Java CLI.
A document that cannot be found is a warning: an iterator over it gives no solution,
the variable of a `SOURCE` clause stays unbound, the graph of a `FROM` clause is empty.
With `--fail-on-missing` (key `failOnMissing`; `fail_on_missing=True` in the API, which
raises `sg.DocumentNotFound`), a document, a graph or a named query that cannot be found
stops the execution with status 1 and the message `Document not found: <iri>`, as in the
Java engine 2.2.3. A `RAISE` expression that is evaluated stops the execution with status 1,
its message and values on the standard error (`error: No email address for: "Bob", 1`).

With `--graphs-to-files` (key `graphsToFiles`), a `GENERATE` query writes the named graphs
whose IRI is a relative path to an RDF file, resolved against the base IRI, to that file,
as the Java command line 2.6.0 does: `GRAPH <people.ttl> { ... }` writes `people.ttl`, and a
graph computed from the data, `GRAPH <out/{?country}.nq> { ... }`, one file per country. The
other named graphs, and the default graph, stay in the output.

- **Folder.** The files are written in the folder of the base IRI if it is a `file:` IRI, and
  else in the working directory (`-d`). Without base, the relative IRIs of the query are
  resolved against the current directory, and the files are written there.
- **Format and compression** come from the extension: `.nq`, `.nt`, `.ttl`, `.trig`, `.n3`,
  `.jsonld`, `.rdf` or `.rdfxml`, `.rj` or `.rdfjson`; then, optionally, `.gz`, `.zip`,
  `.bz2`, `.xz`, `.tar.gz`, `.tar.xz` or `.tar.bz2`. An archive holds one entry, named after
  the file without its compression extension.
- **Named graphs inside a file.** `GRAPH <dump.nq#g=http://example.org/people> { ... }`
  writes the graph `http://example.org/people` in `dump.nq` (`.nq`, `.trig` or `.jsonld`);
  `GRAPH <dump.nq> { ... }` writes its default graph. The IRI after `g=` can be
  percent-encoded: `GRAPH <dump.nq#g={STR(?g)}> { ... }`.
- **Safety.** A path that would leave the folder (`..` or `.` segments, an empty segment, a
  backslash, encoded or not) is an error, status 1. A graph name that resolves outside of the
  folder of the base stays a named graph of the output.
- Each triple is written once in a file, and a file is created only if a triple goes to it.
- A main output format that holds triples only, such as `-fo NT`, is refused only if a named
  graph reaches the output.

`--targets targets.json` (key `targets`, relative to the working directory) describes the
target files, for a format, a compression or a character encoding that the extension does not
give; it implies `--graphs-to-files`. Its keys are paths relative to the folder; each value
has the optional keys `format` (`N-Quads`, `Turtle`, `JSON-LD`, a media type such as
`application/n-triples`, or an extension), `compression` (`none`, `gzip`, `zip`, `bzip2`,
`xz`, `tar.gz`, `tar.xz`, `tar.bz2`) and `encoding` (`UTF-8` by default; `UTF-16` writes a
byte order mark, big-endian):

```json
{ "dump1.nq": { "encoding": "UTF-16" }, "out/people": { "format": "Turtle", "compression": "gzip" } }
```

## How it works

`sparql_generate/parser.py` parses the v2 grammar (`spargl.jj`) into an AST,
including embedded expressions in IRIs and strings. `compiler.py` compiles every
query level to a DuckDB temporary table of solutions with one column per
variable (statically typed: IRI, string, integer, JSON, dynamic struct...),
`ITERATOR` clauses to table functions (`read_csv`, `read_json_objects`,
`json_extract` + `unnest`) or lateral `unnest` of Python UDFs (XPath, CSS
selectors, GeoJSON), nested `GENERATE` to levels that carry the parent's row id,
and the templates to `SELECT ... UNION ALL` statements producing N-Triples
terms. Blank nodes of templates are scoped as the Java engine's
`QueryBNodeNormalizer` defines: one blank node
per solution of the outermost level whose template uses the label.

Supported: `GENERATE` (nested, named with parameters, recursive; quad patterns
`GRAPH VarOrIri { ... }` in templates, whose sub-queries generate their default graph in that
graph, as in the Java engine), `TEMPLATE`
(`BEFORE`, `SEPARATOR`, `AFTER`, `BOX`, `FORMAT`, `GROUP`, nested and named
sub-templates), `SELECT`, `PERFORM` (the action instances are returned, not
executed), `FUNCTION`; named calls with call-site clauses, unfolded until their
argument table is empty, with tabling of pending calls; `iter:call-select` (named `SELECT` queries called per row,
recursive, tabled); `FROM`, `FROM NAMED`, `FROM GENERATE [NAMED]`, one dataset
per group of input rows when they depend on them; `SOURCE ... ACCEPT`,
`ITERATOR`, `BIND`; `WHERE` with triple patterns, `OPTIONAL`, `UNION`, `MINUS`,
`GRAPH`, `VALUES`, `BIND`, `FILTER` compiled to joins over the dataset, and
property paths, sub-`SELECT` and `EXISTS` too; nested closures (`(p*)*`)
and `SERVICE` through rdflib; `GROUP
BY`/aggregates, `HAVING`, `ORDER BY`, `LIMIT`/`OFFSET`, `DISTINCT`,
`EXPRESSIONS`; `LIST(e)`; the SPARQL 1.1 built-in functions and XSD casts, with
the numeric type promotion of XPath, exact decimals and Jena's lexical forms
(`"5.0e0"^^xsd:double`); iterators `iter:CSV`, `iter:CSVHeaders`,
`iter:JSONPath`, `iter:JSONListKeys`, `iter:XPath`, `iter:CSSPath`,
`iter:GeoJSON`, `iter:CBOR`, `iter:Split`, `iter:regex`, `iter:for`; functions
`fun:JSONPath`, `fun:XPath`, `fun:CSSPath`, `fun:CBOR`, `fun:regex`,
`fun:SplitAtPosition`, `fun:bnode`, `fun:HTMLtoXML`, `fun:dateTime`,
`fun:camelCase`, `fun:titleCase`, `fun:prefixedIRI`; RDF 1.2 triple terms
(`afn:triple(s, p, o)`, `afn:isTriple`, `afn:subject`, `afn:predicate`, `afn:object` of
Jena's function library), written `<<( s p o )>>` in N-Triples and N-Quads and matched by
the `WHERE` clause of a query that reads them from a `FROM GENERATE`.
`RAISE(message, value...)`, evaluated only when its value is needed, as in the Java
engine: in `IF(c, a, RAISE(...))` when `c` is false, in `COALESCE(e, RAISE(...))` when `e`
is unbound or an error, in `a || RAISE(...)` when `a` is not true, in `a && RAISE(...)` when
`a` is not false, in `x IN (a, RAISE(...))` when `x` is not `a`, and in a list of `FILTER`s
when the filters before it are true; a `BIND` with `RAISE` is evaluated for each solution,
also when nothing reads its variable; `BIND`, `FILTER`, `COALESCE` and the iterators do not
catch its error. `iter:CSVHeaders` on the IRI of a CSV document reads its header line only.
`iter:CSV` with named columns binds the position of the row (from 0, across batches) after
the last column, and matches the names with the header ignoring case, as the Java engine
2.5.0 does. Local documents are read decompressed according to the extension of their name
(`.gz`, `.bz2`, `.xz`; the first file of a `.zip`, `.tar`, `.tar.gz`, `.tar.bz2` or
`.tar.xz` archive) and transcoded to UTF-8 from UTF-16 or UTF-32 with a byte order mark.
A quad pattern whose graph is Jena's name of the default graph
(`GRAPH <urn:x-arq:DefaultGraphNode> { ... }`) generates triples of the default graph.
Batched iterators (`iter:CSV(<doc>, n, ...)`, `iter:JSONSurfer(<doc>, path, n, ...)`)
evaluate the rest of the query once per batch, as the Java engine does (modifiers and `LIST`
apply per batch). HTTP: `fun:HTTPGet` (and `HTTPPost`, `HTTPPut`, `HTTPDelete`,
`HTTPHead`, with headers and a body) returns the whole response as the Java engine does,
`fun:HTTPExtractBody`, `fun:HTTPExtractHeader` and `fun:HTTPExtractResponseCode` read it;
`iter:HTTPGet(<iri>, seconds, calls)` binds each response, typed with its media type, as
a batch of its own (a call count is required: an endless stream is refused). SQL:
`iter:SQL(database, query, "column"...)` reads a relational database as R2RML does, as the
Java engine 2.6.1: the JDBC URL of PostgreSQL, MySQL, SQL Server, SQLite or DuckDB (an IRI or
a string, with the user and the password in it), the named columns (exactly, else ignoring
case), each cell its natural RDF literal in canonical form, `NULL` unbound; an unknown
column or a query that the database rejects stops the execution, an unreachable database
is a missing document. `iter:CSVStrict` reads the CSV of RML-IO (an empty cell is the empty
string, a row of the wrong length an error), `iter:SPARQL` the solutions of a SELECT query
on an RDF document, `fun:unsafeIRI` makes an IRI of a string unchecked (white space allowed).
Streams:
`iter:WebSocket(<ws://...>, [first message])` and `iter:MQTTSubscribe(<tcp://...>, topic...)`
never end in the Java engine; here a bounded prefix is read at compile time, at most
`stream_messages` messages for at most `stream_seconds` seconds (`--stream-messages`,
`--stream-seconds`), each message a batch of its own; without a bound the query is refused. A `FUNCTION` that recurses on values known only at
run time is evaluated iteratively, in `BIND`, `FILTER` and the binding clauses.

## Tests and conformance

```sh
pytest                                   # unit tests + the Java test cases (if ../sparql-generate is checked out)
python scripts/conformance.py [-v]       # the Java test cases, compared by graph or dataset isomorphism, or by text
python scripts/bench.py --sizes 10000 100000 1000000   # against the Java CLI (JDK >= 17)
```

All 58 test cases of the Java implementation 2.6.1 pass but one, offline (`capital` fetches
a document from the Web). On the queries that the RML translator
[rml-to-sparql-generate](https://github.com/sparql-generate/rml-to-sparql-generate) 0.4.0
writes for the RML test suites, run by the conformance harness of
[kgc-bench](https://github.com/sparql-generate/kgc-bench) (`--graphs-to-files` or
`--targets` for the logical targets of RML-IO; the SQL tracks on PostgreSQL, MySQL and SQL
Server servers), every case has the status of the Java engine 2.6.1:

| suite | cases | PASS | FAIL |
|---|---|---|---|
| RMLio/rml-test-cases@803dd3ec (CSV, JSON, XML) | 117 | 117 | 0 |
| RMLio/rml-test-cases@803dd3ec (PostgreSQL) | 60 | 57 | 3 |
| RMLio/rml-test-cases@803dd3ec (MySQL) | 60 | 59 | 1 |
| RMLio/rml-test-cases@803dd3ec (SQL Server) | 60 | 58 | 2 |
| kg-construct/rml-core@82ab28d4 | 76 | 76 | 0 |
| kg-construct/rml-io@0cb26a2 | 73 | 72 | 1 |
| kg-construct/rml-fnml@f573f79 | 22 | 22 | 0 |
| kg-construct/rml-cc@d97fddf | 35 | 35 | 0 |
| kg-construct/rml-lv@94beb94 | 41 | 41 | 0 |
| kg-construct/rml-star@711f72e | 18 | 18 | 0 |

The translated GTFS-Madrid-Bench mapping gives the Java engine's set of triples (395,953 at
scale 1, 3,959,530 at scale 10). Where the Java engine 2.2 diverged from the
documented behaviour of the language, this implementation follows the documentation
(these divergences are fixed in later Java releases):
ill-formed triples are dropped, `VALUES` in `WHERE` works with input bindings,
`<s{?i}>` works with non-string `?i`, `GROUP` in `TEMPLATE` aggregates, `FROM`
of a query called on several rows reads one dataset per row group. A nested
`GENERATE` is evaluated once on the table of all parent solutions (its `LIMIT`,
`DISTINCT` and aggregates range across parents), as the Java
engine does; `tests/cases/` holds cases whose expected outputs come from the
Java engine, each checked against the Java engine 2.6.0. `BNODE(str)` gives one blank node per string and input
row of the operator that evaluates it (two `BIND`s give two nodes), as SPARQL and
Jena read it.

## Performance

`scripts/bench.py` runs four synthetic workloads (flat and nested CSV and JSON, 10k to
1M rows) against the Java CLI and checks that both engines produce the same set of
triples. The latest table, `bench/results/bench.md`, was measured against the 2.2.2
release on a shared, loaded machine: it is provisional. Order of magnitude at 1M rows:
1.5–2 s and 0.5–0.7 GB for pysparql-generate, 22–170 s and 5–7 GB for the Java engine
with `-Xmx8g`, identical sets of triples.

## License

Apache License 2.0, like the Java implementation.
