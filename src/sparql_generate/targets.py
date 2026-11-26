"""Named graphs written to files (``--graphs-to-files``, ``--targets``), as the command
line of the Java engine 2.6.0 does.

A named graph whose IRI is the folder of the base IRI followed by a relative path that
names an RDF file, by its extension or in the description of the targets, is written to
that file, in the output folder; the other graphs stay in the main output.

* The output folder is the folder of the base IRI if it is a ``file:`` IRI, and else the
  working directory, which the base IRI identifies for the documents read by the query.
* Format and compression come from the extension of the path (``dump.nq.gz``), unless the
  description of the targets gives them, with the character encoding.
* ``<dump.nq#g=IRI>`` names the graph ``IRI`` inside the file ``dump.nq``: the fragment is
  ``g=`` followed by the IRI of the graph, percent-encoded or not.
* A path that would leave the output folder (``..`` or ``.`` segments, empty segments, a
  backslash, encoded or not) is an error.

The messages of the errors are those of the Java engine.
"""
from __future__ import annotations

import codecs
import json
import os
import re
from typing import Dict, List, Optional, Tuple
from urllib.parse import unquote_to_bytes, urlsplit
from urllib.request import url2pathname


class TargetError(ValueError):
    """A graph name or a description of the targets that cannot be written to a file."""


#: the compressions of a target file, by extension, in the order the Java engine tests them
COMPRESSIONS = [("tar.gz", ".tar.gz"), ("tar.xz", ".tar.xz"), ("tar.bz2", ".tar.bz2"), ("gzip", ".gz"),
                ("zip", ".zip"), ("bzip2", ".bz2"), ("xz", ".xz")]
_EXTENSION_OF = dict(COMPRESSIONS)
_COMPRESSION_NAMES = {"": "none", "none": "none", "gzip": "gzip", "gz": "gzip", "zip": "zip", "bzip2": "bzip2",
                      "bz2": "bzip2", "xz": "xz", "tar.gz": "tar.gz", "targz": "tar.gz", "tgz": "tar.gz",
                      "tar.xz": "tar.xz", "tarxz": "tar.xz", "tar.bz2": "tar.bz2", "tarbz2": "tar.bz2"}

#: the RDF formats of a target file, by extension
EXTENSIONS = {"nq": "nq", "nt": "nt", "ttl": "ttl", "trig": "trig", "n3": "n3", "jsonld": "jsonld",
              "rdf": "rdfxml", "rdfxml": "rdfxml", "rj": "rj", "rdfjson": "rj"}
#: Jena's labels of these formats (the messages of the errors)
LABELS = {"nq": "N-Quads", "nt": "N-Triples", "ttl": "Turtle", "trig": "TriG", "n3": "N3", "jsonld": "JSON-LD",
          "rdfxml": "RDF/XML", "rj": "RDF/JSON"}
QUAD_LANGS = ("nq", "trig", "jsonld")
# the names and media types of these formats that Jena's RDFLanguages.nameToLang knows
_NAMES = {
    "n-quads": "nq", "nq": "nq", "nquads": "nq", "nquad": "nq", "n-quad": "nq", "application/n-quads": "nq",
    "text/n-quads": "nq",
    "n-triples": "nt", "nt": "nt", "ntriples": "nt", "ntriple": "nt", "n-triple": "nt", "application/n-triples": "nt",
    "text/plain": "nt",
    "turtle": "ttl", "ttl": "ttl", "text/turtle": "ttl", "application/turtle": "ttl",
    "trig": "trig", "application/trig": "trig", "text/trig": "trig",
    "n3": "n3", "text/rdf+n3": "n3", "application/n3": "n3", "text/n3": "n3",
    "json-ld": "jsonld", "jsonld": "jsonld", "application/ld+json": "jsonld",
    "rdf/xml": "rdfxml", "rdfxml": "rdfxml", "rdf/xml-abbrev": "rdfxml", "rdfxml-abbrev": "rdfxml",
    "application/rdf+xml": "rdfxml",
    "rdf/json": "rj", "rdfjson": "rj", "application/rdf+json": "rj",
}
_ABSOLUTE_IRI = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")


def read_descriptions(path: str) -> Dict[str, dict]:
    """The description of the targets: a JSON object whose keys are paths relative to the
    output folder, and whose values have the keys ``format``, ``compression`` and
    ``encoding``."""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except OSError as ex:
        raise TargetError(f"Cannot read the description of the targets {path}") from ex
    except ValueError as ex:
        raise TargetError(f"Invalid description of the targets {path}: {ex}") from ex
    if data is None:
        return {}
    if not isinstance(data, dict) or any(v is not None and not isinstance(v, dict) for v in data.values()):
        raise TargetError(f"Invalid description of the targets {path}: an object of objects is expected")
    return data


def check_safe(path: str, name: str) -> None:
    """Refuses a relative path that could leave the output folder."""
    if not path or path.startswith("/") or "\\" in path or "\0" in path:
        raise TargetError(f"Unsafe target path {path} ({name})")
    for segment in path.split("/"):
        if segment in ("", ".", ".."):
            raise TargetError(f"Unsafe target path {path} ({name}): a target is written in the output folder only")


def percent_decode(s: str, iri: str) -> str:
    """Decodes the %XX sequences of a string as UTF-8."""
    if "%" not in s:
        return s
    if re.search(r"%(?![0-9A-Fa-f]{2})", s):
        raise TargetError(f"Invalid percent-encoding in the graph <{iri}>")
    return unquote_to_bytes(s).decode("utf-8", errors="replace")


def compression_of_name(name: str) -> str:
    """The compression of a file name (``none`` if its end gives none)."""
    lower = name.lower()
    for c, ext in COMPRESSIONS:
        if lower.endswith(ext):
            return c
    return "none"


class TargetFile:
    """A file that named graphs are written to: its path, the path relative to the output
    folder (``name``), its format, compression and character encoding."""

    def __init__(self, path: str, name: str, lang: str, compression: str, encoding: str):
        self.path, self.name, self.lang, self.compression, self.encoding = path, name, lang, compression, encoding

    def entry_name(self) -> str:
        """The name of the entry of an archive: the name of the file without its compression
        extension."""
        name = os.path.basename(self.path)
        ext = _EXTENSION_OF.get(self.compression, "")
        if ext and name.lower().endswith(ext):
            name = name[:-len(ext)]
        return name

    def __repr__(self):  # pragma: no cover - debugging aid
        return f"TargetFile({self.path!r}, {self.lang}, {self.compression}, {self.encoding})"


class GraphTargets:
    """The resolution of graph names to target files.

    ``base`` is the base IRI of the query (None: the working directory), ``working_dir`` the
    working directory, ``descriptions`` the description of the targets (by relative path)."""

    def __init__(self, base: Optional[str], working_dir: str, descriptions: Optional[Dict[str, dict]] = None):
        from pathlib import Path

        b = base if base is not None else Path(os.path.abspath(working_dir)).as_uri() + "/"
        slash = b.rfind("/")
        if slash < 0:
            raise TargetError(f"--graphs-to-files needs a base IRI with a path, got {b}")
        self.base = b
        self.folder_iri = b[:slash + 1]
        if self.folder_iri.startswith("file:"):
            parts = urlsplit(self.folder_iri)
            if parts.netloc not in ("", "localhost") or not parts.path.startswith("/"):
                raise TargetError(f"--graphs-to-files: the base IRI is not a folder: {b}")
            self.folder = os.path.normpath(url2pathname(parts.path))
        else:
            self.folder = os.path.normpath(os.path.abspath(working_dir))
        self.descriptions: Dict[str, dict] = {}
        for key, value in (descriptions or {}).items():
            path = key[2:] if key.startswith("./") else key
            check_safe(path, key)
            self.descriptions[path] = value or {}
        self.files: Dict[str, TargetFile] = {}
        self._resolved: Dict[str, Optional[Tuple[TargetFile, Optional[str]]]] = {}

    def resolve(self, graph: str) -> Optional[Tuple[TargetFile, Optional[str]]]:
        """The file that a graph name (an N-Triples term) names, and the IRI of the graph in
        that file (None for its default graph); None if it names no file."""
        if graph not in self._resolved:
            self._resolved[graph] = self._resolve(graph)
        return self._resolved[graph]

    def _resolve(self, graph: str):
        if not (graph.startswith("<") and graph.endswith(">")):
            return None
        iri = graph[1:-1]
        if not _ABSOLUTE_IRI.match(iri):
            from .terms import resolve_iri

            iri = resolve_iri(iri, self.base)
        if not iri.startswith(self.folder_iri):
            return None
        rest = iri[len(self.folder_iri):]
        fragment = None
        if "#" in rest:
            rest, fragment = rest.split("#", 1)
        if not rest or "?" in rest:
            return None  # the folder itself, or a query: not a file
        path = percent_decode(rest, iri)
        description = self.descriptions.get(path)
        lang = self._lang(path, description)
        if lang is None:
            if description is not None:
                raise TargetError(f"No format for the target {path}: give it in the description of the targets, "
                                  "or as an extension of the file name")
            return None  # not the name of an RDF file
        check_safe(path, iri)
        file = os.path.normpath(os.path.join(self.folder, path))
        if file == self.folder or not file.startswith(self.folder.rstrip(os.sep) + os.sep):
            raise TargetError(f"The graph <{iri}> names a file outside of the output folder {self.folder}")
        if fragment is None:
            in_file = None
        elif fragment.startswith("g=") and len(fragment) > 2:
            in_file = percent_decode(fragment[2:], iri)
            if not _ABSOLUTE_IRI.match(in_file):
                raise TargetError(f"The graph <{iri}> names a graph in the file {path} whose IRI is not absolute: "
                                  f"{in_file}")
        else:
            raise TargetError(f"The graph <{iri}> names the file {path} with a fragment that is not g= followed by "
                              "the IRI of a graph")
        target = self.files.get(file)
        if target is None:
            target = TargetFile(file, path, lang, self._compression(path, description), self._encoding(path, description))
            self.files[file] = target
        if in_file is not None and lang not in QUAD_LANGS:
            raise TargetError(f"The graph <{iri}> names a graph in the file {path}, which {LABELS[lang]} cannot hold: "
                              "use .nq, .trig or .jsonld")
        return target, in_file

    @staticmethod
    def _lang(path: str, description: Optional[dict]) -> Optional[str]:
        if description is not None and description.get("format") is not None:
            f = str(description["format"]).strip()
            lang = _NAMES.get(f.lower()) or EXTENSIONS.get(f.lower().removeprefix("."))
            if lang is None:
                raise TargetError(f"Unknown format in the description of the targets: {f}")
            return lang
        name = path.rsplit("/", 1)[-1]
        ext = _EXTENSION_OF.get(compression_of_name(name), "")
        stem = name[:len(name) - len(ext)]
        dot = stem.rfind(".")
        if dot <= 0:
            return None
        return EXTENSIONS.get(stem[dot + 1:].lower())

    @staticmethod
    def _compression(path: str, description: Optional[dict]) -> str:
        if description is not None and description.get("compression") is not None:
            name = str(description["compression"]).strip()
            c = _COMPRESSION_NAMES.get(name.lower().replace("-", "").replace("_", ""))
            if c is None:
                raise TargetError(f"Unknown compression in the description of the targets: {name} "
                                  "(none, gzip, zip, bzip2, xz, tar.gz, tar.xz, tar.bz2)")
            return c
        return compression_of_name(path.rsplit("/", 1)[-1])

    @staticmethod
    def _encoding(path: str, description: Optional[dict]) -> str:
        if description is not None and description.get("encoding") is not None:
            enc = str(description["encoding"]).strip()
            try:
                codecs.lookup(_python_codec(enc)[0])
            except LookupError:
                raise TargetError(f"Unknown character encoding for the target {path}: {description['encoding']}") \
                    from None
            return enc
        return "UTF-8"


def _python_codec(encoding: str) -> Tuple[str, bytes]:
    """The Python codec of a Java character encoding, and the byte order mark Java writes:
    ``UTF-16`` is big-endian with a mark, ``UTF-32`` big-endian without."""
    e = encoding.lower().replace("_", "-")
    if e in ("utf-16", "utf16"):
        return "utf-16-be", codecs.BOM_UTF16_BE
    if e in ("utf-32", "utf32"):
        return "utf-32-be", b""
    return encoding, b""


# -- writing -----------------------------------------------------------------------------------


class _Sink:
    """The binary stream of a target file: transcoded from text, compressed; a tar archive is
    written from a temporary file at the end, with one entry."""

    def __init__(self, target: TargetFile):
        self.target = target
        os.makedirs(os.path.dirname(target.path) or ".", exist_ok=True)
        codec, self.bom = _python_codec(target.encoding)
        self.encoder = codecs.getincrementalencoder(codec)(errors="replace")
        c = target.compression
        self.tar_content = None
        self.zip = None
        if c.startswith("tar."):
            import tempfile

            fd, self.tar_content = tempfile.mkstemp(prefix=".sparql-generate-", suffix=".tmp",
                                                    dir=os.path.dirname(target.path) or ".")
            self.out = os.fdopen(fd, "wb")
        elif c == "gzip":
            import gzip

            self.out = gzip.open(target.path, "wb")
        elif c == "zip":
            import zipfile

            self.zip = zipfile.ZipFile(target.path, "w", compression=zipfile.ZIP_DEFLATED)
            self.out = self.zip.open(target.entry_name(), "w", force_zip64=True)
        elif c == "bzip2":
            import bz2

            self.out = bz2.open(target.path, "wb")
        elif c == "xz":
            import lzma

            self.out = lzma.open(target.path, "wb", format=lzma.FORMAT_XZ)
        else:
            self.out = open(target.path, "wb")
        self.started = False

    def write(self, text: str) -> None:
        if not self.started:
            self.started = True
            if self.bom:
                self.out.write(self.bom)
        self.out.write(self.encoder.encode(text))

    def close(self) -> None:
        if not self.started and self.bom:
            self.out.write(self.bom)
        self.out.write(self.encoder.encode("", final=True))
        self.out.close()
        if self.zip is not None:
            self.zip.close()
        if self.tar_content is not None:
            import tarfile

            mode = {"tar.gz": "w:gz", "tar.xz": "w:xz", "tar.bz2": "w:bz2"}[self.target.compression]
            try:
                with tarfile.open(self.target.path, mode, format=tarfile.PAX_FORMAT) as tar:
                    tar.add(self.tar_content, arcname=self.target.entry_name(), recursive=False)
            finally:
                os.unlink(self.tar_content)


def write_target(target: TargetFile, batches, prefixes: Dict[str, str]) -> int:
    """Write the quads ``(s, p, o, g)`` of a target file (record batches of N-Triples terms,
    ``g`` None for the default graph of the file) in its format; returns the number of
    quads."""
    sink = _Sink(target)
    n = 0
    try:
        if target.lang in ("nt", "nq"):
            for b in batches:
                cols = [b.column(i).to_pylist() for i in range(4)]
                lines = []
                for s, p, o, g in zip(*cols):
                    lines.append(f"{s} {p} {o} {g} .\n" if g is not None and target.lang == "nq" else f"{s} {p} {o} .\n")
                sink.write("".join(lines))
                n += len(lines)
            return n
        quads = [q for b in batches for q in zip(*(b.column(i).to_pylist() for i in range(4)))]
        n = len(quads)
        sink.write(_serialize(target.lang, quads, prefixes))
        return n
    finally:
        sink.close()


def _serialize(lang: str, quads, prefixes: Dict[str, str]) -> str:
    if lang == "rj":
        return _rdf_json([q for q in quads if q[3] is None])
    import rdflib

    data = "".join(f"{s} {p} {o}{' ' + g if g is not None else ''} .\n" for s, p, o, g in quads)
    ds = rdflib.Dataset(default_union=False)
    ds.parse(data=data, format="nquads")
    for prefix, iri in prefixes.items():
        ds.bind(prefix, iri, override=False)
    if lang in ("trig", "jsonld"):
        out = ds.serialize(format="trig" if lang == "trig" else "json-ld")
    else:
        g = rdflib.Graph()
        for prefix, iri in prefixes.items():
            g.bind(prefix, iri, override=False)
        for t in ds.default_graph:
            g.add(t)
        out = g.serialize(format={"ttl": "turtle", "n3": "n3", "rdfxml": "xml"}[lang])
    return out if isinstance(out, str) else out.decode("utf-8")


def _rdf_json(triples) -> str:
    """RDF/JSON (W3C note): ``{ subject: { predicate: [ object ] } }``."""
    from .terms import parse_nt_term
    from . import ast as A

    out: Dict[str, Dict[str, List[dict]]] = {}
    for s, p, o, _ in triples:
        st, pt, ot = parse_nt_term(s), parse_nt_term(p), parse_nt_term(o)
        sk = st.value if isinstance(st, A.IRI) else "_:" + st.label
        if isinstance(ot, A.IRI):
            obj = {"type": "uri", "value": ot.value}
        elif isinstance(ot, A.BNode):
            obj = {"type": "bnode", "value": "_:" + ot.label}
        else:
            obj = {"type": "literal", "value": ot.lexical}
            if ot.lang:
                obj["lang"] = ot.lang
            elif ot.datatype:
                obj["datatype"] = ot.datatype
        objs = out.setdefault(sk, {}).setdefault(pt.value, [])
        if obj not in objs:
            objs.append(obj)
    return json.dumps(out, indent=2, ensure_ascii=False) + "\n"
