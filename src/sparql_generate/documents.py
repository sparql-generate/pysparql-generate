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
            with open(self.path, "rb") as f:
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
        """A file path for engines that read files directly, or None."""
        if self.path is not None and self.data is None:
            return self.path
        return None


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
