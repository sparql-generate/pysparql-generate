# Changelog

## Unreleased (branch `next`)

Parity with the Java engine 2.6.1 (branch `fix-2.6.1`) and the RML translator
rml-to-sparql-generate `next`.

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
