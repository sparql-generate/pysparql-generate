"""iter:SQL as the Java engine 2.6.1 (ITER_SQL, its SQLNaturalLiteralsTest): JDBC URLs, named
columns, the natural RDF literals of R2RML in canonical form, NULL unbound, a rejected query
or an unknown column as an error, an unreachable database as a missing document.

SQLite and DuckDB run here; PostgreSQL, MySQL and SQL Server when a JDBC URL with the user
and the password of a server is given (``PYSG_TEST_POSTGRESQL``, ``PYSG_TEST_MYSQL``,
``PYSG_TEST_SQLSERVER``), for instance the containers of kgc-bench's harness.database."""
import datetime as dt
import decimal
import os
import sqlite3

import pytest

import sparql_generate as sg
from sparql_generate import sqlsources as Q

ITER = "PREFIX iter: <http://w3id.org/sparql-generate/iter/> PREFIX ex: <http://ex.org/> "
XSD = "http://www.w3.org/2001/XMLSchema#"


def lit(lexical, datatype=None):
    from sparql_generate.terms import nt_literal

    return nt_literal(lexical, datatype=XSD + datatype if datatype else None)


# -- pure functions ----------------------------------------------------------------------------


def test_canonical_forms():
    D = decimal.Decimal
    assert Q.canonical_double("80.25") == "8.025E1"
    assert Q.canonical_double("1") == "1.0E0"
    assert Q.canonical_double("30.000") == "3.0E1"
    assert Q.canonical_double("-0.0015") == "-1.5E-3"
    assert Q.canonical_double("0") == "0.0E0"
    assert Q.canonical_decimal(D("30.00")) == "30.0"
    assert Q.canonical_decimal(D("-0.250")) == "-0.25"
    assert Q.canonical_decimal(D("1E+2")) == "100.0"
    assert Q.canonical_decimal(D("-0.00")) == "0.0"
    # a REAL: the decimal value of the single precision number, not of its double
    import numpy as np

    assert Q.natural_literal(float(np.float32(70.22)), "real") == {"k": 2, "v": "7.022E1", "d": XSD + "double"}
    assert Q.natural_literal(float("nan"), "double")["v"] == "NaN"
    assert Q.natural_literal(float("-inf"), "real")["v"] == "-INF"


def test_natural_literals_of_values():
    nl = Q.natural_literal
    assert nl(dt.datetime(2008, 11, 12, 9, 45, 44, 250000), "timestamp")["v"] == "2008-11-12T09:45:44.25"
    assert nl(dt.datetime(2009, 10, 10, 12, 12, 22), "timestamp")["v"] == "2009-10-10T12:12:22"
    tz = dt.timezone(dt.timedelta(hours=2))
    assert nl(dt.datetime(2009, 10, 10, 12, 12, 22, tzinfo=tz), "timestamptz")["v"] == "2009-10-10T10:12:22Z"
    assert nl(dt.datetime(2009, 10, 10, 12, 12, 22, tzinfo=tz), "timestampoffset")["v"] == "2009-10-10T12:12:22+02:00"
    assert nl(dt.timedelta(hours=8, minutes=30), "time")["v"] == "08:30:00"
    assert nl(2020, "date")["v"] == "2020-01-01"  # MySQL's YEAR
    assert nl(b"\x00\xff", "binary") == {"k": 2, "v": "00FF", "d": XSD + "hexBinary"}
    assert nl(b"\x01", "bool")["v"] == "true"
    assert nl("ab", "char", 6)["v"] == "ab    "
    assert nl(None, "int") is None
    assert nl("", "string") == {"k": 2, "v": "", "d": None}


def test_jdbc_urls():
    t = Q.parse_jdbc("jdbc:postgresql://h:5433/db?user=u&password=p%26q")
    assert (t.kind, t.host, t.port, t.database, t.user, t.password) == ("postgresql", "h", 5433, "db", "u", "p&q")
    t = Q.parse_jdbc("jdbc:mysql://localhost/test?user=root&password=")
    assert (t.kind, t.host, t.port, t.database, t.user, t.password) == ("mysql", "localhost", None, "test", "root", "")
    t = Q.parse_jdbc("jdbc:sqlserver://127.0.0.1:1433;databaseName=TestDB;encrypt=false;user={sa};password={a;b}}c}")
    assert (t.kind, t.host, t.port, t.database, t.user, t.password) == ("sqlserver", "127.0.0.1", 1433, "TestDB", "sa",
                                                                         "a;b}c")
    assert t.props["encrypt"] == "false"
    assert Q.parse_jdbc("jdbc:sqlite:data.db", "/d").path == "/d/data.db"
    with pytest.raises(ValueError):
        Q.parse_jdbc("jdbc:h2:mem:x")


def test_password_redacted():
    assert Q.redact("jdbc:postgresql://h/db?user=u&password=s3cret") == "jdbc:postgresql://h/db?user=u&password=***"
    assert Q.redact("jdbc:sqlserver://h;user=sa;password={a;b}}c};encrypt=false") == \
        "jdbc:sqlserver://h;user=sa;password=***;encrypt=false"


def test_select_columns():
    assert Q.select_columns(["ID", "Name", "name"], ["name", "id"]) == [2, 0]
    assert Q.select_columns(["a", "b"], []) == [0, 1]
    with pytest.raises(LookupError, match="no such column"):
        Q.select_columns(["a"], ["b"])
    with pytest.raises(LookupError, match="several columns"):
        Q.select_columns(["NAME", "NAME"], ["NAME"])
    with pytest.raises(LookupError, match="several columns"):
        Q.select_columns(["Name", "NAME"], ["name"])


# -- SQLite and DuckDB ---------------------------------------------------------------------------


@pytest.fixture
def duck(tmp_path):
    import duckdb

    path = tmp_path / "p.duckdb"
    con = duckdb.connect(str(path))
    con.execute("CREATE TABLE patient (id INTEGER, big BIGINT, name VARCHAR(20), weight REAL, height DOUBLE, "
                "price DECIMAL(10,2), paid BOOLEAN, birth DATE, arrival TIMESTAMP, alarm TIME, photo BLOB, note VARCHAR)")
    con.execute("INSERT INTO patient VALUES (10, 12345678901234, 'Monica', 80.25, 1.65, 30.00, FALSE, DATE '1981-10-10', "
                "TIMESTAMP '2009-10-10 12:12:22', TIME '08:30:00', '\\x89\\x50\\x4E\\x47'::BLOB, NULL)")
    con.execute("INSERT INTO patient VALUES (11, -1, 'Rachel', 70.22, 30, 0.5, TRUE, DATE '1982-11-12', "
                "TIMESTAMP '2008-11-12 09:45:44.25', TIME '23:59:59', '\\x00\\xFF'::BLOB, '')")
    con.close()
    return f"jdbc:duckdb:{path}"


def sql_row(url, query, names):
    args = "".join(f', "{n}"' for n in names)
    rows = sg.select(ITER + f'SELECT * ITERATOR iter:SQL("{url}", "{query}"{args}) AS '
                     + " ".join("?" + n for n in names) + " WHERE { }", allow_network=False)
    assert len(rows) == 1
    return rows[0]


def test_natural_literals_duckdb(duck):
    r = sql_row(duck, "SELECT * FROM patient WHERE id = 10",
                "ID BIG NAME WEIGHT HEIGHT PRICE PAID BIRTH ARRIVAL ALARM PHOTO NOTE".split())
    assert r == {"ID": lit("10", "integer"), "BIG": lit("12345678901234", "integer"), "NAME": lit("Monica"),
                 "WEIGHT": lit("8.025E1", "double"), "HEIGHT": lit("1.65E0", "double"), "PRICE": lit("30.0", "decimal"),
                 "PAID": lit("false", "boolean"), "BIRTH": lit("1981-10-10", "date"),
                 "ARRIVAL": lit("2009-10-10T12:12:22", "dateTime"), "ALARM": lit("08:30:00", "time"),
                 "PHOTO": lit("89504E47", "hexBinary"), "NOTE": None}
    r = sql_row(duck, "SELECT * FROM patient WHERE id = 11", "BIG WEIGHT HEIGHT PRICE PAID ARRIVAL PHOTO NOTE".split())
    assert r == {"BIG": lit("-1", "integer"), "WEIGHT": lit("7.022E1", "double"), "HEIGHT": lit("3.0E1", "double"),
                 "PRICE": lit("0.5", "decimal"), "PAID": lit("true", "boolean"),
                 "ARRIVAL": lit("2008-11-12T09:45:44.25", "dateTime"), "PHOTO": lit("00FF", "hexBinary"),
                 "NOTE": lit("")}


def test_named_columns_in_their_order_regardless_of_case(duck):
    assert sql_row(duck, "SELECT * FROM patient WHERE id = 10", ["name", "id"]) == {
        "name": lit("Monica"), "id": lit("10", "integer")}


def raised(query) -> sg.RaiseError:
    with pytest.raises(sg.RaiseError) as info:
        list(sg.generate(ITER + query, allow_network=False))
    return info.value


def test_unknown_or_duplicate_column_is_an_error(duck):
    e = raised(f'GENERATE {{ ex:s ex:p ?x }} ITERATOR iter:SQL("{duck}", "SELECT id FROM patient", "nocolumn") AS ?x')
    assert str(e) == 'SQL error: no such column in the result of the query: "nocolumn", "SELECT id FROM patient"'
    e = raised(f'GENERATE {{ ex:s ex:p ?x }} ITERATOR iter:SQL("{duck}", "SELECT name, name FROM patient", "NAME") AS ?x')
    assert str(e).startswith("SQL error: several columns of the result of the query have this name")


def test_rejected_query_is_an_error(duck):
    for q in ("SELECT id FROM nosuchtable", "SELEC id FROM patient"):
        e = raised(f'GENERATE {{ ex:s ex:p ?x }} ITERATOR iter:SQL("{duck}", "{q}") AS ?x')
        assert str(e).startswith("SQL error: ") and e.values == [f'"{q}"']


def test_error_only_when_evaluated(duck):
    assert list(sg.generate(ITER + f'GENERATE {{ GENERATE {{ ex:s ex:p ?x }} ITERATOR iter:SQL("{duck}", '
                            '"SELECT id FROM nosuchtable") AS ?x . } WHERE { FILTER(false) }')) == []


def test_unreachable_database_is_a_missing_document(tmp_path):
    url = f"jdbc:sqlite:{tmp_path}/none.db"
    q = ITER + f"GENERATE {{ ex:s ex:p ?x }} ITERATOR iter:SQL(<{url}>, \"SELECT 1\") AS ?x"
    with sg.Execution(q, allow_network=False) as ex:
        assert list(ex.triples()) == [] and any("can not connect" in w for w in ex.warnings)
    with pytest.raises(sg.DocumentNotFound):
        list(sg.generate(q, fail_on_missing=True))


def test_sqlite_by_values(tmp_path):
    db = tmp_path / "s.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE t (i INTEGER, r REAL, s TEXT, b BLOB, n TEXT)")
    con.execute("INSERT INTO t VALUES (3, 2.5, 'x', X'0A0B', NULL)")
    con.commit()
    con.close()
    r = sql_row(f"jdbc:sqlite:{db}", "SELECT * FROM t", ["i", "r", "s", "b", "n"])
    assert r == {"i": lit("3", "integer"), "r": lit("2.5E0", "double"), "s": lit("x"), "b": lit("0A0B", "hexBinary"),
                 "n": None}


def test_database_as_iri_or_string(duck):
    for db in (f"<{duck}>", f'"{duck}"'):
        rows = sg.select(ITER + f'SELECT ?n ITERATOR iter:SQL({db}, "SELECT name FROM patient ORDER BY id") AS ?n '
                         "WHERE { }")
        assert [r["n"] for r in rows] == ['"Monica"', '"Rachel"']


# -- PostgreSQL, MySQL, SQL Server (a server given by its JDBC URL) --------------------------------

DDL = {
    "postgresql": ["DROP TABLE IF EXISTS patient",
                   "CREATE TABLE patient (id INTEGER, big BIGINT, name VARCHAR(20), code CHAR(6), weight REAL, "
                   "height DOUBLE PRECISION, price DECIMAL(10,2), paid BOOLEAN, birth DATE, arrival TIMESTAMP, "
                   "alarm TIME, photo BYTEA, note VARCHAR(20))",
                   "INSERT INTO patient VALUES (10, 12345678901234, 'Monica', 'ab', 80.25, 1.65, 30.00, FALSE, "
                   "'1981-10-10', '2009-10-10 12:12:22', '08:30:00', decode('89504E47', 'hex'), NULL)",
                   "INSERT INTO patient VALUES (11, -1, 'Rachel', 'abcdef', 70.22, 30, 0.5, TRUE, "
                   "'1982-11-12', '2008-11-12 09:45:44.25', '23:59:59', decode('00FF', 'hex'), '')"],
    "mysql": ["DROP TABLE IF EXISTS patient",
              "CREATE TABLE patient (id INTEGER, big BIGINT, name VARCHAR(20), code CHAR(6), weight FLOAT, "
              "height DOUBLE, price DECIMAL(10,2), paid BOOLEAN, birth DATE, arrival DATETIME(2), alarm TIME, "
              "photo VARBINARY(8), note VARCHAR(20))",
              "INSERT INTO patient VALUES (10, 12345678901234, 'Monica', 'ab', 80.25, 1.65, 30.00, FALSE, "
              "'1981-10-10', '2009-10-10 12:12:22', '08:30:00', X'89504E47', NULL)",
              "INSERT INTO patient VALUES (11, -1, 'Rachel', 'abcdef', 70.22, 30, 0.5, TRUE, "
              "'1982-11-12', '2008-11-12 09:45:44.25', '23:59:59', X'00FF', '')"],
    "sqlserver": ["DROP TABLE IF EXISTS patient",
                  "CREATE TABLE patient (id INTEGER, big BIGINT, name VARCHAR(20), code CHAR(6), weight REAL, "
                  "height FLOAT, price DECIMAL(10,2), paid BIT, birth DATE, arrival DATETIME2, alarm TIME, "
                  "photo VARBINARY(8), note VARCHAR(20))",
                  "INSERT INTO patient VALUES (10, 12345678901234, 'Monica', 'ab', 80.25, 1.65, 30.00, 0, "
                  "'1981-10-10', '2009-10-10 12:12:22', '08:30:00', 0x89504E47, NULL)",
                  "INSERT INTO patient VALUES (11, -1, 'Rachel', 'abcdef', 70.22, 30, 0.5, 1, "
                  "'1982-11-12', '2008-11-12 09:45:44.25', '23:59:59', 0x00FF, '')"],
}
SERVERS = [(k, os.environ.get("PYSG_TEST_" + k.upper())) for k in DDL]


@pytest.mark.parametrize("kind,url", [s for s in SERVERS], ids=[k for k, _ in SERVERS])
def test_natural_literals_of_a_server(kind, url):
    if not url:
        pytest.skip(f"no server (PYSG_TEST_{kind.upper()})")
    con = Q.connect(Q.parse_jdbc(url))
    cur = con.cursor()
    for stmt in DDL[kind]:
        cur.execute(stmt)
    if kind == "mysql":
        con.commit()
    con.close()
    url = url.replace("\\", "\\\\").replace('"', '\\"').replace("{", "\\{")
    r = sql_row(url, "SELECT * FROM patient WHERE id = 10",
                "ID BIG NAME CODE WEIGHT HEIGHT PRICE PAID BIRTH ARRIVAL ALARM PHOTO NOTE".split())
    assert r == {"ID": lit("10", "integer"), "BIG": lit("12345678901234", "integer"), "NAME": lit("Monica"),
                 "CODE": lit("ab    "), "WEIGHT": lit("8.025E1", "double"), "HEIGHT": lit("1.65E0", "double"),
                 "PRICE": lit("30.0", "decimal"), "PAID": lit("false", "boolean"), "BIRTH": lit("1981-10-10", "date"),
                 "ARRIVAL": lit("2009-10-10T12:12:22", "dateTime"), "ALARM": lit("08:30:00", "time"),
                 "PHOTO": lit("89504E47", "hexBinary"), "NOTE": None}
    r = sql_row(url, "SELECT * FROM patient WHERE id = 11",
                "BIG CODE WEIGHT HEIGHT PRICE PAID ARRIVAL PHOTO NOTE".split())
    assert r == {"BIG": lit("-1", "integer"), "CODE": lit("abcdef"), "WEIGHT": lit("7.022E1", "double"),
                 "HEIGHT": lit("3.0E1", "double"), "PRICE": lit("0.5", "decimal"), "PAID": lit("true", "boolean"),
                 "ARRIVAL": lit("2008-11-12T09:45:44.25", "dateTime"), "PHOTO": lit("00FF", "hexBinary"),
                 "NOTE": lit("")}


def test_session_variables():
    assert Q.session_variables("sql_mode=CONCAT(@@sql_mode,',ANSI_QUOTES')") == [
        "sql_mode=CONCAT(@@sql_mode,',ANSI_QUOTES')"]
    assert Q.session_variables("a=1;b='x,y',c=2") == ["a=1", "b='x,y'", "c=2"]
    t = Q.parse_jdbc("jdbc:mysql://h/test?user=root&password=&sessionVariables=sql_mode%3DCONCAT(%40%40sql_mode%2C"
                     "%27%2CANSI_QUOTES%27)")
    assert t.props["sessionvariables"] == "sql_mode=CONCAT(@@sql_mode,',ANSI_QUOTES')"


def test_mysql_ansi_quotes():
    url = os.environ.get("PYSG_TEST_MYSQL")
    if not url:
        pytest.skip("no server (PYSG_TEST_MYSQL)")
    url += "&sessionVariables=sql_mode=CONCAT(@@sql_mode,',ANSI_QUOTES')"
    labels, _, rows = Q.query(Q.parse_jdbc(url), 'SELECT "x"."id" FROM (SELECT 1 AS "id") AS "x"')
    assert labels == ["id"] and rows == [(1,)]
