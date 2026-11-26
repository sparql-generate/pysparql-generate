# Developing pysparql-generate

Architecture map for contributors. `README.md` is the user guide. The comments name the
compilation rules (BGP, FROM, BATCH, QUAD, ...) that each part of the compiler implements.

## How a query flows

```
query text ──lexer.py──▶ tokens ──parser.py──▶ ast.Query
    ──compiler.Compiler.compile()──▶ Plan (SQL statements) + DuckDB temp tables of solutions
    ──Execution.write()/triples()──▶ N-Triples (COPY), rdflib Graph, text, rows
```

1. **`lexer.py`** – pull-based, mode-aware tokenizer. Modes `xs1/xs2/xl1/xl2/xiri` are the
   continuations after an embedded `{ expr }` inside a string or an IRI (`spargl.jj`
   lexical states). `ParseError` carries line, column and a source snippet.
2. **`parser.py`** – recursive descent, one method per production of `spargl.jj`.
   Produces the dataclasses of **`ast.py`** (`Query`, `Triple`, `IteratorClause`,
   `SourceClause`, `Bind`, `Group`, `Values`, `Filter`, `XIRI`, `XLiteral`, `XExpr`,
   `ListNode`, `Call`, `Aggregate`, ...). Prefixed names and relative IRIs are resolved
   here (`terms.resolve_iri`). Embedded expressions stay as `XIRI`/`XLiteral`/`XExpr`
   nodes; the compiler normalises them. PERFORM and FUNCTION are parsed, not compiled.
3. **`compiler.py`** – `Compiler.compile(query, bindings)` returns a `Plan`
   (`triple_selects`, `text_sql`, `select_sql`) and, as a side effect, creates one
   `CREATE TEMP TABLE sol<n>` per *level* (main query, inline sub-GENERATE, called
   named query). `compile_level()` is the spine: binding clauses in order
   (`compile_iterator`, `compile_source`, `extend` for BIND) → `compile_group` (WHERE)
   → `compile_group_by` → `materialise` (ORDER/LIMIT/OFFSET/DISTINCT, row id `__rid`,
   embedded-expression and shared blank-node columns) → `output_generate` /
   `output_template` / `output_select`. `LevelContext` is the `ctx` seen by the
   expression compiler: scope (variable → compiled expression), base IRI, row id,
   blank-node allocation (`template_bnode`, `bnode_call`), aggregate columns.
4. **`termtypes.py`** – the static term type of every column (`TType`: iri, bnode, str,
   lang, typed, int, double, bool, json, any) and the conversions `lex`, `numeric`,
   `to_any`, `to_nt` (N-Triples serialisation by position, NULL when not allowed).
   `SUPPORT_SQL` defines the DuckDB macros `sg_*` (escaping, dynamic struct, JSON typing,
   IRI check, Split). `CExpr` = SQL text + type + optional compile-time constant. An RDF 1.2
   triple term (`afn:triple`, `functions.py`) is a dynamic term of kind 3 whose value is its
   N-Triples form `<<( s p o )>>`.
   Never name a module `types.py` (shadows the stdlib when run from the package dir).
5. **`expressions.py`** – SPARQL expressions → SQL (`compile_expr`). Error = NULL, with the
   SPARQL exceptions (BOUND, COALESCE, IF, &&, ||, !). Operator mapping decided on static
   types. `compile_xiri`/`compile_xliteral` are the embedded-expression normalisation
   (`IRI(CONCAT(... ENCODE_FOR_URI(STR(e)) ...))`, `STRDT(CONCAT(... STR(e) ...), d)`).
   `EXTRA_SUPPORT_SQL`: `sg_any_eq`, `sg_langmatches`, `sg_timezone`.
6. **`iterators.py`** – `ITERATORS[iri](ctx, args, nvars) -> IterPlan`. Two shapes:
   `table` (constant arguments: `read_csv`, `read_json_objects`, `unnest(json_extract)`,
   or an Arrow table computed in Python and registered) and `unnest` (row-dependent: a
   LIST-valued SQL expression, native or a Python UDF returning `LIST(STRUCT)`), optionally
   `positional` (position / hasNext outputs) or `lockstep` (several lists, `iter:regex`).
   `fixed_arity()` wraps UDFs because DuckDB reads the Python signature.
7. **`functions.py`** – `FUNCTIONS[iri](ctx, args) -> CExpr` for `fun:*`. Constant folding
   when all arguments are constants; native SQL when possible (`json_extract`, regex,
   `sg_split`); else a Python UDF. `resolve_doc_arg` turns a constant IRI argument into
   the document (compile time) or a per-row `sg_source` lookup. Documents are passed to
   UDFs as `"\x00" + iri` and resolved in Python (`engine.documents_by_uri`), never as
   per-row strings.
8. **`pyeval.py`** – the row-by-row Python fallback: JSONPath (jsonpath-ng, Jayway
   subset; `jsonpath_is_native` decides when DuckDB can do it), XPath (lxml, default
   namespace stripped like Jena, `xml_node_term` typing), CSS selectors (cssselect,
   `/text()` and `@attr` suffixes), GeoJSON → WKT (Java's exact spacing/parentheses),
   CBOR (base64 of JSON text, as the Java engine). Parsed-document caches keyed by text.
9. **`webapis.py`** – `fun:HTTPGet`/`HTTPPost`/... (Python UDFs on `urllib`, the response
   in the Java engine's text form, GET and HEAD cached per execution), the `fun:HTTPExtract*`
   functions in SQL, `iter:HTTPGet` (the calls made at compile time, each response a batch)
   and `iter:SQL` (SQLite through `sqlite3`, DuckDB files read-only, cells typed as XSD).
   Registered into `FUNCTIONS`/`ITERATORS` by `register_all()` (from `compiler.py`).
9. **`streams.py`** – `iter:WebSocket` and `iter:MQTTSubscribe`: minimal RFC 6455 and MQTT
   3.1.1 clients (no dependency), a bounded prefix of the stream read at compile time
   (`Engine.stream_messages`, `stream_seconds`), each message a batch.
9. **`documents.py`** – `DocumentSet` / `Document`: the documentset (IRI → path | bytes |
   str, media type → IANA datatype), lookup of `BASE`-relative IRIs in `base_dir`,
   optional network fetch (`allow_network`). A local file is read decompressed according to
   the extension of its name and transcoded to UTF-8 from UTF-16/UTF-32 with a byte order
   mark (`decoded_path`: a temporary file, removed at exit), as the Java engine's
   `DocumentStreams`.
10. **`engine.py`** – `Engine`: DuckDB connection, support macros, UDF registry
    (`udf(name, fn, params, ret, key)`, `null_handling='special'`), Arrow registration,
    temp files, `__docs` table for in-memory documents, named-query loading, `debug` →
    `sql_log` (what `--explain` prints).
11. **`bgp.py`** – WHERE graph patterns compiled to joins (rule BGP): the dataset is
    loaded once into `__quads(g, s, s_nt, p, o, o_nt)` (terms as `any` structs plus their
    N-Triples strings, the join keys), from an rdflib object or from a file: N-Triples and
    N-Quads by **`ntriples.py`** (DuckDB's CSV reader, one regex per line, no rdflib: 1M
    triples in ≈ 1.5 s), Turtle, TriG, RDF/XML, N3 by pyoxigraph (`oxigraph_quads`, optional
    extra `fast`), else rdflib; `rdflib_of_dataset()` converts a natively
    loaded dataset for the rdflib fallback; `PatternCompiler` folds a group as SPARQL §18.2.2.6
    does (BGP = self-join of `__quads`, `Join`/`LeftJoin`/`Minus`/`Union`, `GRAPH`), nested
    groups and operands compiled from the unit table. `supported()` decides; `Engine.native_where
    = False` forces the fallback (the tests compare both).
    Paths of `/`, `^`, `|` are expanded (`expand_paths`, fresh `"\x01path"` variables
    dropped from the scope at the end), `* + ?` are `WITH RECURSIVE` closures (`closure`), a sub-SELECT is a level of its own (`subselect`),
    `EXISTS` a correlated subquery folded from the one-row table of the current row
    (`exists`, through `ctx.exists_hook` set by `filter` and `bind`; it saves and restores
    the operator state of `ctx`).
12. **`rdfwhere.py`** + **`serializer.py`** – the rdflib fallback for nested closures,
    SERVICE, and EXISTS in OPTIONAL filters: the current relation is serialised as a `VALUES` block
    (reserved columns travel as `_sgrN`), the group is serialised back to SPARQL, rdflib
    evaluates it, the result comes back as an Arrow table of dynamic terms.
13. **`__init__.py`** – public API (`Execution`, `generate`, `generate_to`, `to_graph`,
    `template`, `select`, `load_conf`). **`cli.py`** – `sparql-generate-py`.
14. **`raising.py`** – `RAISE(message, value...)`: `RaiseError`, the payload of the DuckDB
    error (`MARKER` + JSON of the message and values as N-Triples terms) and its formatting
    as Jena's `FmtUtils` (`format_node`). `Engine.execute` and the readers of `__init__.py`
    turn a DuckDB error that carries the payload into a `RaiseError`.
15. **`targets.py`** – `--graphs-to-files`, `--targets`: `GraphTargets` resolves a graph
    name to a target file (folder, format, compression, encoding, `#g=` graph, safety checks,
    the messages of the Java engine), `write_target` writes one file.
    `Execution.graphs_to_files()` writes the quads of the named graphs into a temporary
    table, routes the graphs that name files to them, and leaves the others in the outputs.

## Where the delicate semantics live

* **Blank-node scoping** – `compiler.template_labels()` computes the labels a level owns
  (outermost level along the inline chain whose template uses the label, rule S3);
  `prepare_template_columns()` turns owned labels used by descendants into columns
  `'B<level>_<k>_' || __rid` carried in the scope under the reserved key `"\x00bn:<label>"`;
  `LevelContext.template_bnode()` inlines the same expression for purely local labels.
  `BNODE(s)` is reading R1 (SPARQL's, Jena's): one node per string and input row of the
  operator (`LevelContext.begin_operator()`: a BIND, the filters of a group, an iterator
  call, GROUP BY, HAVING, ORDER BY); the key of a row is `__rid` for the template and the
  select expressions, else a fresh `uuid()` column added by `ensure_uid` to the operator's
  input relation.
* **Nested levels** – `compile_subquery()` → `parent_relation()` feeds the whole parent
  table (`SELECT *, __rid AS prid FROM sol`) to the child level: a nested GENERATE is
  evaluated once on all parent solutions (semantics rule (G)); `ctx.partitioned` is true
  only for a nested TEMPLATE (rule (T): per-parent LIMIT, aggregates, text joined on
  `__prid`). Named calls: `compile_named_call()` passes only the parameters (call by
  value).
* **`iter:call-select`** – `Compiler.compile_call_select()` (rule CALL-SELECT): the rows are
  written with their call (name, argument keys, chain of pending calls, reserved key
  `"\x00csp"`); each named SELECT query is compiled once on its table of distinct calls as a
  level partitioned by the call key (`ctx.partitioned`, as a sub-TEMPLATE: modifiers and
  aggregates per call), its projection joined back. Tabling is per row: a call pending on
  the row's chain gives no row; recursion unfolds at compile time until no row is left.
* **Compound operands** – DuckDB expands a macro argument at each occurrence, and the
  arithmetic macros (`sg_arith`, `sg_numfn`) copy theirs: `expressions.let_sql()` gives an
  operator cheap copies of its operands, hoisted into columns of the operator's input
  relation (`LevelContext.hoist`, added by `Compiler.ensure_uid`, which every operator
  that compiles expressions calls after `begin_operator()`), or bound by a one-element
  `list_transform` in the output expressions (lambdas are slower: last resort).
* **Materialisation** – `Compiler.inline_level()`: a GENERATE level whose `__rid`/`__idx`/`__n`
  nothing reads (no sub-query, LIST, template blank node, BNODE() after the row id) and whose
  output is one statement (≤ 1 non-constant triple pattern) stays a subquery; otherwise
  `prune_scope()` writes only the columns read afterwards (template terms, sub-query
  variables, reserved and embedded-expression columns). Measured on DuckDB 2.0: k statements
  over an inline relation, or unnesting a list of k structs per row, are slower than one
  narrow temporary table. `Compiler.materialise_count` counts the temporary tables.
* **Start-up** – pyarrow, `urllib.request`, rdflib and the `sg_resolve` UDF (registering a
  Python UDF imports numpy) are loaded on first use: `import sparql_generate` ≈ 0.1 s.
* **Literals** – `sg_q(x)` (`to_json`) writes the quoted N-Triples string: JSON escapes are
  N-Triples escapes, and it is ≈ 5× faster than chained `replace()`. The BGP keys of the
  dataset use it too (`bgp.constant_nt` mirrors it with `json.dumps`).
* **RAISE** – `expressions.compile_raise` writes DuckDB's `error()` (macro `sg_raise`, which
  DuckDB never folds: `error` is volatile). It must be evaluated exactly where the Java engine
  evaluates it: `CASE` and `COALESCE` are lazy in DuckDB, `OR`, `AND` and the conjunctions
  of a `WHERE` are not, so `||`, `&&`, `IN` and the lists of filters are compiled to `CASE`
  chains when an operand after the first holds a `RAISE` (`has_raise`, `conjunction`);
  `let_sql` neither hoists nor binds by a lambda an operand with a `RAISE`; a `BIND` with a
  `RAISE` filters on its column (`col IS NULL OR col IS NOT NULL`), which DuckDB cannot drop
  when nothing reads the column; and a level whose relation holds a `RAISE` is materialised
  (`inline_level` skipped), so that a template that reads nothing (`GENERATE { }`) still
  evaluates it.
* **Unbound / errors** – NULL everywhere; `emit_triple()` filters `IS NOT NULL` on the
  N-Triples terms, `to_nt` returns NULL for literal subjects and non-IRI predicates.
* **LIST(e)** – `output_list()`: cells labelled by `__idx` within the level, `__n` rows.
* **Reserved scope keys** start with `"\x00"` (`prid`, `bn:`, `agg:`): excluded from
  `SELECT *` projections, carried through materialisation and the rdflib fallback.

## Running things

```sh
source ~/.venvs/sparql-generate/bin/activate          # the only venv (never a .venv here)
pip install -e '.[test]'
pytest -q                                              # parser, engine, BGP (native vs rdflib), tests/cases, Java cases (offline)
python scripts/conformance.py [-v] [--no-network] [--only bnode list]   # 45 Java cases, isomorphism
python scripts/bench.py --sizes 10000 100000 1000000 --runs 3           # vs Java (JDK >= 17 auto-detected)
sparql-generate-py -d DIR -q query.rqg -o out.nt --time --explain       # --explain prints the SQL
```

Java engine for comparisons: `bench/jars/sparql-generate-2.2.2.jar` (the GitHub release
asset, which `scripts/bench.py` uses; needs JDK 17+: `/usr/lib/jvm/java-21-openjdk-amd64/bin/java`);
its `-o` is relative to the current directory. A local build is
`../sparql-generate/sparql-generate-all/target/sparql-generate.jar`. Run a JVM or a 1M-row
benchmark under `systemd-run --user --scope -p MemoryMax=8G -p MemorySwapMax=0` on a shared
machine.

## Conventions

* Code, comments, commit messages, docs in English; no attribution trailers in commits.
* Every feature comes with its tests; `pytest` stays green at every commit.
* Prefer vectorised SQL to Python UDFs; a per-row UDF costs 2–3 s per million rows.
* A behaviour copied from the Java engine on purpose is marked in the code
  ("the Java implementation ...").
* The documented behaviour of the language wins over the Java engine when they
  disagree; such a divergence is reported to the Java engine and tested here.
