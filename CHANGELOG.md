# Changelog

## Unreleased

- Sequential batches, on request (`Execution(sequential=True)`; the command line and
  `generate_to` in N-Triples): when the output of a GENERATE query is the union of its
  outputs on the batches of its first iterator (`iter:CSV`, `iter:CSVStrict` or
  `iter:JSONSurfer` with a batch size and constant arguments, such as
  `iter:CSV(<FEEDS.csv>, 1, "feed")`; no DISTINCT, REDUCED, aggregate, GROUP BY, HAVING,
  ORDER BY, LIMIT, OFFSET or LIST, sub-queries without dataset clauses: the condition of the
  Java engine 2.6.2; and here no GRAPH block, named query, `BNODE()`, `UUID()`, `STRUUID()`,
  `RAND()` or `NOW()` in a WHERE clause, `NOW()` anywhere, and at least one sub-query),
  `write()` in N-Triples compiles and writes the batches one step after another; the rows of
  the iterator and the dataset of the query are computed once. A step groups as many
  consecutive batches as fit in about a million rows, from the largest batch seen so far: the
  memory is that of one step (gtfs-feeds, 100 feeds: 3.7 GB to 1.5 GB; 1,000 feeds in 9.4 GB
  instead of more than 24 GB). A regular file is written under a hidden temporary name next
  to it and renamed at the end, with the mode of the existing file or that of a new one: with
  the API, an error leaves the destination as it was (the command line empties the output
  file first, as before and as the Java engine); a process killed by SIGKILL leaves the
  hidden file behind. A compressed file (`.gz`, `.zst`), a FIFO and `/dev/stdout` receive the
  steps in turn. A `BNODE()` in a WHERE clause, even in a `BIND`, keeps the batches together.
  Any other output evaluates all the batches at once; `--explain` shows the SQL of the first
  step.
- The command line reports an output that cannot be written (a directory, a permission, a
  full disk) as an error, without a traceback.
- `iter:CSV` on a document computed per row (a variable, an IRI template) is read about
  three times faster: the rows of a vector of documents are returned to DuckDB as Arrow
  arrays built column by column, instead of a Python dict per row (gtfs-feeds, 100 feeds:
  70 s to 25 s before the output is written). Same bindings. The reader receives the IRI of
  the document, not its text: a vector of large documents passed as text exceeded the 2 GB
  of an Arrow string buffer (1,000 feeds).

## 0.4.1 (2026-11-27)

A correction of `iter:CSV` on a document computed from the solution, and a missing dependency.

- `iter:CSV` on a document that depends on the solution (an IRI template such as
  `<T-{?feed}.csv>`, under an outer iteration): each call reads its own named columns. The
  Python reader of these calls was registered once per execution, with the column names
  and the dialect of the first such call, so that a later call with other names gave
  unbound variables and the position of the row in the wrong variable, or failed
  (`Binder Error: Could not find key "c2" in struct`) when it had more columns.
- `cssselect` is a dependency: without it, `iter:CSSPath` and `fun:CSSPath` gave no result.

## 0.4.0 (2026-11-27)

Parity with the Java engine 2.6.1 and the RML translator rml-to-sparql-generate 0.4.0.

- `iter:SQL` reads relational databases as R2RML does, as the Java engine 2.6.1: the
  database is a JDBC URL, an IRI or a string, of PostgreSQL (psycopg2), MySQL (PyMySQL, with
  Connector/J's `sessionVariables`), SQL Server (pyodbc and Microsoft's ODBC driver), SQLite
  or DuckDB, with the user and the password in the URL (`?user=…&password=…`,
  `;user={…};password={…}`); the drivers are the extra `pip install pysparql-generate[sql]`.
  The names of the columns follow the query (the label, exactly, else regardless of case); a
  name that no column has, or that several have, and a query that the database rejects, are
  errors that stop the execution (`RaiseError`, `SQL error: …`); a database that cannot be
  reached gives no row with a warning, as a missing document (`--fail-on-missing`: an error).
  Each cell is its natural RDF literal in canonical form (`xsd:integer`, `xsd:decimal`
  `30.0`, `xsd:double` `8.025E1`, a `REAL` from its single precision value, `xsd:boolean`
  also for `BIT(1)` and MySQL's `TINYINT(1)`, `xsd:date`, `xsd:time`, `xsd:dateTime` without
  a zero fraction, `xsd:hexBinary` in upper case, `CHAR(n)` padded), `NULL` unbound.
- `iter:CSVStrict(doc, [batch size], "col"...)`: the CSV of RML-IO, as univocity reads it in
  the Java engine; an empty cell is the empty string; a row with another number of fields
  than the header, or a column absent from it, is a data error that stops the execution.
- `iter:SPARQL(doc, query, "var"...)`: the solutions of a SPARQL SELECT query on an RDF
  document (pyoxigraph, else rdflib), then the position of the solution.
- `iter:XPath`: a sub-query that does not start with `/` is evaluated from the matched node
  in its document, so that the parent and ancestor axes reach outside it (`../@id`).
- `fun:unsafeIRI(s)`: a string that starts with a scheme is the IRI as it is, neither
  validated nor resolved (white space allowed), written in N-Triples with `\u0020` escapes.
- `ITERATOR <q>(args)`: a named SELECT query called as an iterator, as
  `iter:call-select(<q>, args)`; the rows keep their order, each followed by the solutions of
  its call in their order.
- The body of a `FUNCTION` resolves relative IRIs against the base of the function, its
  arguments against the base of the caller.
- `iter:JSONPath`: an auxiliary query is read as the Java engine reads it (a definite path
  gives its value; an indefinite one `[]` when nothing matches, its first match when a
  scalar, nothing when an object or an array); several auxiliary queries with wildcards no
  longer fail.
- `iter:XPath`: the prefixes that the document declares may be used in the expressions.
- The command line creates its output file, empty, before the execution, as the Java command
  line does: an error that stops the execution leaves an empty file.
- `scripts/conformance.py` compares a `TEMPLATE` case with `expected_output.txt`.

## 0.3.0 (2026-11-26)

Parity with the Java engine 2.6.0 and the RML translator rml-to-sparql-generate 0.3.0.

- `RAISE(message, value...)`: an expression whose evaluation stops the execution with an
  error that carries a message and values, raised as `sparql_generate.RaiseError`
  (`message`, `values` as N-Triples terms, `None` for a value that could not be evaluated;
  `str(e)` as the Java engine writes it, `No email address for: "Bob", 1`). It is evaluated
  only when its value is needed, as in the Java engine: in a branch of `IF` or `COALESCE`, on
  the right of `||` and `&&`, in a member of `IN`, in a `FILTER` after others; a `BIND` is
  evaluated for each solution, also when nothing reads its variable. The command line writes
  the message and the values on the standard error, with status 1. `raise` is a keyword.
- `--graphs-to-files` (key `graphsToFiles`) and `--targets targets.json` (key `targets`):
  the named graphs whose IRI is a relative path to an RDF file are written to these files, as
  the Java command line 2.6.0 does: format and compression by the extension (`.nq`, `.nt`,
  `.ttl`, `.trig`, `.n3`, `.jsonld`, `.rdf`, `.rj`; `.gz`, `.zip`, `.bz2`, `.xz`, `.tar.gz`,
  `.tar.xz`, `.tar.bz2`) or by the description of the targets (format, compression, character
  encoding), `GRAPH <dump.nq#g=IRI>` for a graph inside a file, the folder of a `file:` base
  or else the working directory, unsafe paths refused with the messages of the Java engine.
  In the API, `Execution.graphs_to_files(targets=None, output_dir=None)`, and
  `sparql_generate.TargetError`.
- `iter:CSVHeaders` on the IRI of a CSV document reads its header line only (also from a
  compressed or UTF-16 file); an empty header cell is the empty string.
- `GROUP BY` on keys that are constant (a variable that is never bound) gives no group on an
  empty input, as SPARQL and Jena; it gave one.
- Relative IRIs are resolved as RFC 3986 says, as Jena does: empty path segments are kept
  (`<a//b>`); Python's `urljoin` dropped them.
- The cases of `tests/cases` and the test cases of the Java engine with an expected error
  (`expected_error.txt`, the message of a `RAISE`) are checked against the Java engine 2.6.0.
- `CITATION.cff`.

## 0.2.0 (2026-11-26)

Parity with the Java engine 2.5.0 on the queries that the RML translator
rml-to-sparql-generate 0.2.0 writes: on the seven RML test suites (rml-test-cases, rml-core,
rml-io, rml-fnml, rml-cc, rml-star, rml-lv), the output of every translated case is the Java
engine's, by RDF dataset isomorphism.

- RDF 1.2 triple terms: `afn:triple(s, p, o)`, `afn:isTriple`, `afn:subject`,
  `afn:predicate`, `afn:object` (Jena's function library); a triple term is written
  `<<( s p o )>>` in N-Triples and N-Quads, as a subject or an object, is stored in the
  dataset of a `FROM GENERATE` and matched by triple patterns; `STR` gives Jena's
  `<< s p o >>`.
- `iter:CSV` with named columns binds the position of the row (from 0, the header
  excluded, across batches) to the variable after the last column; column names match the
  header ignoring case and the surrounding white space, as univocity does in the Java engine.
- Local documents are read decompressed according to the extension of their name (`.gz`,
  `.bz2`, `.xz`; the first file of a `.zip`, `.tar`, `.tar.gz`/`.tgz`, `.tar.bz2`,
  `.tar.xz`/`.txz` archive), and transcoded to UTF-8 from UTF-16 or UTF-32 with a byte order
  mark.
- `GRAPH <urn:x-arq:DefaultGraphNode> { ... }` (or `<urn:x-arq:DefaultGraph>`, Jena's names
  of the default graph), constant or computed, generates triples of the default graph.
- `REPLACE` and `REGEX` with the flag `q` (a literal pattern); the replacement string of
  `REPLACE` is read as Java reads it (`$n`, `\$`, `\\`), constant or computed.
- `fun:titleCase` as the Java engine: the characters that are neither letters, digits nor
  white space removed, the first character of each word in upper case, the others kept.
- An IRI or a literal with embedded expressions in a triple pattern of a `WHERE` clause
  (`<person/{?name}> ex:friend ?f`) constrains the pattern as the value of its expression
  does (Java test case `xexpr-where`).
- Fixed: a constant bound in an `OPTIONAL` group (`BIND("x" AS ?v)`) was bound on every row,
  matched or not; a document IRI left unbound on a row of `SOURCE` or of an iterator raised
  an error.
- The Java test cases with a TriG expected output (`expected_output.trig`) are compared by
  dataset isomorphism: 51 of 51 pass.

## 0.1.0 (2026-11-22)

First release: SPARQL-Generate compiled to DuckDB, with a Python API and a command line
compatible with the Java CLI.
