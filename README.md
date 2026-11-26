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
ex.warnings             # e.g. documents not found
ex.close()
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

## Command line

Option-compatible with the Java CLI where it makes sense:

```sh
sparql-generate-py -d DIR [-q query.rqg] [-o out.nt] [-fo NT|NQ|TRIG|TTL|JSON-LD|CSV|JSON] [-c conf.json] [-l WARN]
sparql-generate-py -q query.rqg --document http://ex.org/d.csv=data.csv -o out.nt
sparql-generate-py -q query.rqg --no-network --threads 8 --memory-limit 4GB --time
sparql-generate-py -q query.rqg --explain          # print the compiled SQL plan
sparql-generate-py -q query.rqg -x                 # syntax check only
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
Java engine 2.2.3.

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
`iter:SQL(<jdbc:sqlite:path>, query)` or `<jdbc:duckdb:path>`. Streams:
`iter:WebSocket(<ws://...>, [first message])` and `iter:MQTTSubscribe(<tcp://...>, topic...)`
never end in the Java engine; here a bounded prefix is read at compile time, at most
`stream_messages` messages for at most `stream_seconds` seconds (`--stream-messages`,
`--stream-seconds`), each message a batch of its own; without a bound the query is refused. A `FUNCTION` that recurses on values known only at
run time is evaluated iteratively, in `BIND`, `FILTER` and the binding clauses.

## Tests and conformance

```sh
pytest                                   # unit tests + the Java test cases (if ../sparql-generate is checked out)
python scripts/conformance.py [-v]       # the 51 Java test cases, compared by graph or dataset isomorphism
python scripts/bench.py --sizes 10000 100000 1000000   # against the Java CLI (JDK >= 17)
```

All 51 test cases of the Java implementation 2.5.0 pass (50 offline; `capital`
fetches a document from the Web). On the queries that the RML translator
[rml-to-sparql-generate](https://github.com/sparql-generate/rml-to-sparql-generate) 0.2.0
writes for the RML test suites (rml-test-cases, rml-core and the modules rml-io, rml-fnml,
rml-cc, rml-star, rml-lv), the output is the Java engine 2.5.0's, case by case, by RDF dataset
isomorphism. Where the Java engine 2.2 diverged from the
documented behaviour of the language, this implementation follows the documentation
(these divergences are fixed in later Java releases):
ill-formed triples are dropped, `VALUES` in `WHERE` works with input bindings,
`<s{?i}>` works with non-string `?i`, `GROUP` in `TEMPLATE` aggregates, `FROM`
of a query called on several rows reads one dataset per row group. A nested
`GENERATE` is evaluated once on the table of all parent solutions (its `LIMIT`,
`DISTINCT` and aggregates range across parents), as the Java
engine does; `tests/cases/` holds cases whose expected outputs come from the
Java engine 2.2.0-SNAPSHOT. `BNODE(str)` gives one blank node per string and input
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
