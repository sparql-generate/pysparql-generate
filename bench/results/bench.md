Benchmark `scripts/bench.py`: date 2026-11-07, cpus 16, runs 3, duckdb 2.0.0, python 3.12.3, jar bench/jars/sparql-generate-2.2.2.jar, engine 2.2.2, java openjdk version "25.0.4.1" 2026-08-18, java_mode in-memory Model. Best of the runs, wall time and peak RSS per process (GNU time). `triples`: lines written; `distinct`: distinct triples, numeric literals compared by value; `diff`: size of the symmetric difference of the two sets of triples.

| query | rows | pysparql-generate s | MB | triples | triples/s | Java s | MB | triples | distinct py / Java | diff | speedup |
|---|---|---|---|---|---|---|---|---|---|---|---|
| csv-flat | 10000 | 0.24 | 108 | 50000 | 208333 | 1.16 | 355 | 50000 | 50000 / 50000 | 0 | x4.8 |
| csv-nested | 10000 | 0.25 | 124 | 60000 | 240000 | 1.65 | 537 | 60000 | 60000 / 60000 | 0 | x6.6 |
| json-flat | 10000 | 0.28 | 134 | 40000 | 142857 | 1.17 | 391 | 40000 | 40000 / 40000 | 0 | x4.2 |
| json-nested | 10000 | 0.29 | 129 | 40000 | 137931 | 1.50 | 532 | 40000 | 40000 / 40000 | 0 | x5.2 |
| csv-flat | 100000 | 0.36 | 140 | 500000 | 1388889 | 3.24 | 827 | 500000 | 500000 / 500000 | 0 | x9.0 |
| csv-nested | 100000 | 0.34 | 191 | 600000 | 1764706 | 5.50 | 2029 | 600000 | 600000 / 600000 | 0 | x16.2 |
| json-flat | 100000 | 0.49 | 166 | 400000 | 816327 | 2.97 | 878 | 400000 | 400000 / 400000 | 0 | x6.1 |
| json-nested | 100000 | 0.54 | 178 | 400000 | 740741 | 4.89 | 1658 | 400000 | 400000 / 400000 | 0 | x9.1 |
| csv-flat | 1000000 | 1.50 | 510 | 5000000 | 3333333 | 27.07 | 5266 | 5000000 | 5000000 / 5000000 | 0 | x18.0 |
| csv-nested | 1000000 | 1.67 | 652 | 6000000 | 3592814 | 170.51 | 7231 | 6000000 | 6000000 / 6000000 | 0 | x102.1 |
| json-flat | 1000000 | 1.92 | 677 | 4000000 | 2083333 | 22.14 | 6252 | 4000000 | 4000000 / 4000000 | 0 | x11.5 |
| json-nested | 1000000 | 1.97 | 703 | 4000000 | 2030457 | 149.70 | 7244 | 4000000 | 4000000 / 4000000 | 0 | x76.0 |
