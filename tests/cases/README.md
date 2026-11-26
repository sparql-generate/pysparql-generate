Conformance cases of pysparql-generate itself (format of the Java test cases:
`query.rqg`, `sparql-generate-conf.json`, `expected_output.ttl`, or
`expected_output.nq` for a query with GRAPH blocks, compared by RDF dataset
isomorphism).

- `nested-*`: expected outputs produced by the Java engine 2.2.0-SNAPSHOT (branch
  release-2.2, 2026-11-07, JDK 21) and checked against the documented behaviour (rule (G):
  a nested GENERATE is evaluated once on the table of all parent solutions).
- `from-generate-*`: expected outputs produced by the Java engine 2.3.1 (release JAR,
  2026-11-22, JDK 21). None of them puts two identical FROM GENERATE sub-queries in
  sibling sub-queries: the Java engine 2.3.1 then leaves the second dataset empty
  (`tests/test_datasets.py::test_identical_from_generate_in_sibling_subqueries`).
- `quads-*`: expected outputs produced by the Java engine 2.3.2-SNAPSHOT of the branch
  `quad-patterns` (GRAPH blocks in GENERATE templates, work in progress on 2026-11-22,
  JDK 21), with `-fo NQUADS`.
- `csv-row-position`, `compressed-sources`, `quads-default-graph-node`, `replace-flags`,
  `title-case`, `optional-bind-constant`: expected outputs produced by the Java engine 2.5.0
  (release JAR, 2026-11-26, JDK 21), with `-fo NT`, or `-fo NQUADS` for the quads.
  `tests/triple-terms` (outside this directory: rdflib reads no RDF 1.2 triple term) is
  compared line by line with the 2.5.0 output by `tests/test_triple_terms.py`.

- `raise-*`, `csv-headers-iri`, `group-by-empty-input`, `iri-empty-segments`: expected outputs
  produced by the Java engine 2.6.0 (release JAR, 2026-11-26, JDK 21), with `-fo NT`; for a
  query whose execution stops with the error of a `RAISE` expression, `expected_error.txt`
  holds the message that the Java command line writes on the standard error.

Every case of this directory was run again on 2026-11-26 with the Java engine 2.5.0 (release
JAR): its output is the expected one (graph or dataset isomorphism), the `quads-*` cases
included. Every case was run again on 2026-11-26 with the Java engine 2.6.0 (release JAR):
its output is the expected one, or its error the expected message.
