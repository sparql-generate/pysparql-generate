"""Local documents read as the Java engine 2.5.0 reads them (DocumentStreams): decompressed
according to the extension of their name, transcoded to UTF-8 from UTF-16 or UTF-32 with a
byte order mark.  The end-to-end case is tests/cases/compressed-sources."""
import gzip
import io
import tarfile

from sparql_generate.documents import Document, compression, decoded_path
from sparql_generate.expressions import java_replacement_to_re2
from sparql_generate.iterators import header_index


def test_compression_from_the_name():
    assert compression("a.csv.gz") == "gz"
    assert compression("A.JSON.BZ2") == "bz2"
    assert compression("a.xz") == "xz"
    assert compression("a.zip") == "zip"
    for n in ("a.tar", "a.tar.gz", "a.tgz", "a.tar.xz", "a.txz", "a.tar.bz2"):
        assert compression(n) == "tar", n
    assert compression("a.csv.gz?x=1") == "gz"
    assert compression("a.csv") is None


def test_plain_utf8_file_is_read_in_place(tmp_path):
    p = tmp_path / "a.csv"
    p.write_bytes(b"\xef\xbb\xbfx,y\n")
    assert decoded_path(str(p)) == str(p)
    assert Document("a", path=str(p)).text() == "x,y\n"  # the UTF-8 byte order mark dropped


def test_utf16_and_gzip(tmp_path):
    p = tmp_path / "a.csv.gz"
    p.write_bytes(gzip.compress(b"\xfe\xff" + "x,Zoë\n".encode("utf-16-be")))
    d = Document("a", path=str(p))
    assert d.local_path() != str(p)
    assert d.bytes() == "x,Zoë\n".encode("utf-8")


def test_first_file_of_a_tgz(tmp_path):
    p = tmp_path / "a.tgz"
    with tarfile.open(p, "w:gz") as t:
        d = tarfile.TarInfo("dir")
        d.type = tarfile.DIRTYPE
        t.addfile(d)
        for name, data in (("dir/first.json", b"[1]"), ("second.json", b"[2]")):
            i = tarfile.TarInfo(name)
            i.size = len(data)
            t.addfile(i, io.BytesIO(data))
    assert Document("a", path=str(p)).text() == "[1]"


def test_missing_file_keeps_its_path(tmp_path):
    assert Document("a", path=str(tmp_path / "none.csv.gz")).local_path() == str(tmp_path / "none.csv.gz")


def test_java_replacement():
    assert java_replacement_to_re2("$1-$0") == "\\1-\\0"
    assert java_replacement_to_re2("\\$1") == "$1"
    assert java_replacement_to_re2("\\\\") == "\\\\"
    assert java_replacement_to_re2("a\\b") == "ab"


def test_csv_header_index():
    h = ["ID", "Name", " Score "]
    assert header_index(h, "Name") == 1
    assert header_index(h, "name") == 1
    assert header_index(h, "score") == 2
    assert header_index(h, "absent") is None
    assert header_index(["a", "A"], "A") == 1  # an exact match first
