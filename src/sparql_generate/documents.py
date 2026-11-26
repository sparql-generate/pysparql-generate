"""The document set: named documents (CSV, JSON, XML, ...) a query may read.

Mirrors the ``documentset`` of ``sparql-generate-conf.json`` of the Java CLI:
a document is identified by an IRI, has a media type, and lives in a file, in
memory, or on the Web.
"""
from __future__ import annotations

import mimetypes
import os
from dataclasses import dataclass, field
from typing import Dict, Optional, Union
from urllib.parse import urlparse

from .ast import IANA

_EXT_TYPES = {
    ".csv": "text/csv", ".tsv": "text/tab-separated-values", ".json": "application/json",
    ".geojson": "application/geo+json", ".xml": "application/xml", ".html": "text/html",
    ".htm": "text/html", ".txt": "text/plain", ".cbor": "application/cbor", ".md": "text/markdown",
    ".ttl": "text/turtle", ".nt": "application/n-triples", ".rqg": "application/vnd.sparql-generate",
}

mimetypes.add_type("application/vnd.sparql-generate", ".rqg")


@dataclass
class Document:
    uri: str
    path: Optional[str] = None
    data: Optional[Union[bytes, str]] = None
    mediatype: Optional[str] = None
    _text: Optional[str] = field(default=None, repr=False)
    _decoded: Optional[str] = field(default=None, repr=False)

    def guess_mediatype(self) -> str:
        if self.mediatype:
            return self.mediatype.split(";")[0].strip()
        name = self.path or self.uri
        ext = os.path.splitext(urlparse(name).path)[1].lower()
        return _EXT_TYPES.get(ext) or mimetypes.guess_type(name)[0] or "text/plain"

    @property
    def datatype(self) -> str:
        return IANA + self.guess_mediatype()

    def bytes(self) -> bytes:
        if self.data is not None:
            return self.data if isinstance(self.data, bytes) else self.data.encode("utf-8")
        if self.path is not None:
            with open(self.local_path(), "rb") as f:
                return f.read()
        raise FileNotFoundError(self.uri)

    def text(self) -> str:
        if self._text is None:
            if isinstance(self.data, str):
                self._text = self.data
            else:
                self._text = self.bytes().decode("utf-8", errors="replace")
                if self._text.startswith("﻿"):
                    self._text = self._text[1:]
        return self._text

    def local_path(self) -> Optional[str]:
        """A file path for engines that read files directly, or None: the file itself, or,
        for a compressed file or a UTF-16 or UTF-32 one, a temporary file of its content
        decompressed and in UTF-8 (:func:`decoded_path`)."""
        if self.path is not None and self.data is None:
            if self._decoded is None:
                self._decoded = decoded_path(self.path)
            return self._decoded
        return None


# -- compressed and UTF-16/UTF-32 files (as the Java engine 2.5.0, DocumentStreams) -----------

_BOMS = ((b"\x00\x00\xfe\xff", "utf-32-be"), (b"\xff\xfe\x00\x00", "utf-32-le"),
         (b"\xfe\xff", "utf-16-be"), (b"\xff\xfe", "utf-16-le"))
_TAR = (".tar.gz", ".tgz", ".tar.xz", ".txz", ".tar.bz2", ".tar")
_TEMP_DIR: Optional[str] = None


def compression(name: str) -> Optional[str]:
    """The compression of a file, from the extension of its name: ``zip``, ``tar`` (with the
    ``.tar.gz``, ``.tgz``, ``.tar.xz``, ``.txz``, ``.tar.bz2`` and ``.tar`` archives),
    ``gz``, ``xz``, ``bz2``, or None."""
    n = name.lower().split("?", 1)[0]
    if n.endswith(".zip"):
        return "zip"
    if n.endswith(_TAR):
        return "tar"
    for ext in ("gz", "xz", "bz2"):
        if n.endswith("." + ext):
            return ext
    return None


def open_content(path: str):
    """A binary stream of the content of a file, decompressed according to the extension of
    its name: ``.gz``, ``.bz2``, ``.xz``; the first file of a ``.zip``, ``.tar``, ``.tar.gz``
    (``.tgz``), ``.tar.bz2`` or ``.tar.xz`` (``.txz``) archive."""
    kind = compression(os.path.basename(path))
    if kind == "zip":
        import zipfile

        z = zipfile.ZipFile(path)
        entry = next((i for i in z.infolist() if not i.is_dir()), None)
        if entry is None:
            raise OSError(f"empty zip archive: {path}")
        return z.open(entry)
    if kind == "tar":
        import tarfile

        t = tarfile.open(path, "r:*")
        entry = next((m for m in t if m.isfile()), None)
        if entry is None:
            raise OSError(f"no file in tar archive: {path}")
        return t.extractfile(entry)
    if kind == "gz":
        import gzip

        return gzip.open(path, "rb")
    if kind == "xz":
        import lzma

        return lzma.open(path, "rb")
    if kind == "bz2":
        import bz2

        return bz2.open(path, "rb")
    return open(path, "rb")


def open_text(path: str):
    """A text stream of the content of a local file, decompressed according to the extension
    of its name and decoded from UTF-8, or from UTF-16 or UTF-32 after a byte order mark,
    read as it is consumed (``decoded_path`` writes the whole content)."""
    import io

    f = open_content(path) if compression(os.path.basename(path)) is not None else open(path, "rb")
    f = io.BufferedReader(f) if not hasattr(f, "peek") else f
    head = f.peek(4)[:4]
    bom, enc = next(((b, e) for b, e in _BOMS if head.startswith(b)), (b"", "utf-8"))
    if bom:
        f.read(len(bom))
    return io.TextIOWrapper(f, encoding=enc, errors="replace", newline="")


def decoded_path(path: str) -> str:
    """``path`` itself, or the path of a temporary file of its content decompressed
    (:func:`open_content`) and in UTF-8 when it starts with a UTF-16 or UTF-32 byte order mark
    (the mark removed), as the Java engine reads a local document.  The temporary files are
    removed at exit."""
    if not os.path.isfile(path):
        return path
    compressed = compression(os.path.basename(path)) is not None
    with open_content(path) if compressed else open(path, "rb") as f:
        head = f.read(4)
        enc = next((e for bom, e in _BOMS if head.startswith(bom)), None)
        if not compressed and enc is None:
            return path
        global _TEMP_DIR
        if _TEMP_DIR is None:
            import atexit
            import shutil
            import tempfile

            _TEMP_DIR = tempfile.mkdtemp(prefix="pysparql-generate-")
            atexit.register(shutil.rmtree, _TEMP_DIR, True)
        import tempfile

        fd, out_path = tempfile.mkstemp(dir=_TEMP_DIR, suffix="-" + _inner_name(path))
        with os.fdopen(fd, "wb") as out:
            if enc is None:
                out.write(head)
                while True:
                    b = f.read(1 << 20)
                    if not b:
                        break
                    out.write(b)
            else:
                import codecs

                bom = next(b for b, e in _BOMS if e == enc)
                rest = head[len(bom):]
                dec = codecs.getincrementaldecoder(enc)()
                out.write(dec.decode(rest).encode("utf-8"))
                while True:
                    b = f.read(1 << 20)
                    if not b:
                        break
                    out.write(dec.decode(b).encode("utf-8"))
                out.write(dec.decode(b"", final=True).encode("utf-8"))
    return out_path


def _inner_name(path: str) -> str:
    """The name of a file without its compression extension (``data.csv.gz`` → ``data.csv``)."""
    n = os.path.basename(path)
    low = n.lower()
    for ext in _TAR + (".zip", ".gz", ".xz", ".bz2"):
        if low.endswith(ext):
            return n[: len(n) - len(ext)]
    return n


class DocumentSet:
    def __init__(self, documents=None, base_dir: Optional[str] = None, base: Optional[str] = None,
                 allow_network: bool = True, timeout: float = 30.0):
        self.docs: Dict[str, Document] = {}
        self.base_dir = base_dir
        self.base = base
        self.allow_network = allow_network
        self.timeout = timeout
        self._fetched: Dict[str, Optional[Document]] = {}
        if documents:
            if isinstance(documents, dict):
                for uri, spec in documents.items():
                    self.add(uri, spec)
            else:
                for spec in documents:
                    self.add(spec["uri"], spec)

    def add(self, uri: str, spec) -> Document:
        """``spec`` is a path, bytes/str content, a :class:`Document`, or a dict
        with ``path``/``data``/``mediatype`` keys (the conf.json form)."""
        if isinstance(spec, Document):
            doc = spec
        elif isinstance(spec, dict):
            path = spec.get("path")
            if path is not None and self.base_dir and not os.path.isabs(path):
                path = os.path.join(self.base_dir, path)
            doc = Document(uri, path=path, data=spec.get("data"), mediatype=spec.get("mediatype"))
        elif isinstance(spec, (bytes,)):
            doc = Document(uri, data=spec)
        elif isinstance(spec, str) and (os.path.exists(spec) or (self.base_dir and os.path.exists(os.path.join(self.base_dir, spec)))):
            p = spec if os.path.exists(spec) else os.path.join(self.base_dir, spec)
            doc = Document(uri, path=p)
        elif isinstance(spec, os.PathLike):
            doc = Document(uri, path=os.fspath(spec))
        else:
            doc = Document(uri, data=spec)
        self.docs[uri] = doc
        return doc

    def get(self, uri: str, accept: Optional[str] = None) -> Optional[Document]:
        doc = self.docs.get(uri)
        if doc is not None:
            return doc
        if uri in self._fetched:
            return self._fetched[uri]
        doc = self._locate(uri, accept)
        self._fetched[uri] = doc
        return doc

    def _locate(self, uri: str, accept: Optional[str]) -> Optional[Document]:
        parsed = urlparse(uri)
        if parsed.scheme == "file":
            p = parsed.path
            if os.path.exists(p):
                return Document(uri, path=p)
            return None
        # a document relative to the base, mapped onto the working directory
        if self.base and self.base_dir and uri.startswith(self.base):
            rel = uri[len(self.base):]
            p = os.path.join(self.base_dir, rel)
            if os.path.isfile(p):
                return Document(uri, path=p)
        if parsed.scheme in ("http", "https") and self.allow_network:
            try:
                from urllib.request import Request, urlopen  # imported on first fetch (slow import)

                req = Request(uri, headers={"Accept": accept or "*/*", "User-Agent": "pysparql-generate"})
                with urlopen(req, timeout=self.timeout) as resp:
                    data = resp.read()
                    ct = resp.headers.get("Content-Type")
                return Document(uri, data=data, mediatype=ct.split(";")[0].strip() if ct else None)
            except Exception:
                return None
        if not parsed.scheme and self.base_dir:
            p = os.path.join(self.base_dir, uri)
            if os.path.isfile(p):
                return Document(uri, path=p)
        return None
