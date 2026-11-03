"""XMP sidecar model for the LRT-lite workflow.

The on-disk contract was recovered from the LRTimelapse 7 package
(`lrtimelapse/resources/xmp-templates/{empty,raw_m_1,raw_m_2}.xmp`):

* a Lightroom Camera Raw XMP packet (`<x:xmpmeta>` -> `<rdf:RDF>` -> `<rdf:Description>`)
* keyframes are marked with **star ratings** (`xmp:Rating`), not custom fields:
  4 stars = default keyframe, 2 stars = left Holy Grail keyframe,
  3 stars = right Holy Grail keyframe, 5 stars = crop keyframe.
* per-frame adjustments live in the standard `crs:` namespace.

This module keeps the rest of the document byte-for-byte intact while editing the
properties the workflow needs.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

NSMAP = {
    "x": "adobe:ns:meta/",
    "rdf": "http://www.w3.org/1999/02/22-rdf-syntax-ns#",
    "crs": "http://ns.adobe.com/camera-raw-settings/1.0/",
    "crd": "http://ns.adobe.com/camera-raw-defaults/1.0/",
    "xmp": "http://ns.adobe.com/xap/1.0/",
    "xmpMM": "http://ns.adobe.com/xap/1.0/mm/",
    "stEvt": "http://ns.adobe.com/xap/1.0/sType/ResourceEvent#",
    "dc": "http://purl.org/dc/elements/1.1/",
    "photoshop": "http://ns.adobe.com/photoshop/1.0/",
    "tiff": "http://ns.adobe.com/tiff/1.0/",
    "exif": "http://ns.adobe.com/exif/1.0/",
    "exifEX": "http://cipa.jp/exif/1.0/",
    "aux": "http://ns.adobe.com/exif/1.0/aux/",
    "lrt": "http://lrtimelapse.com/",
}

for _prefix, _uri in NSMAP.items():
    try:
        ET.register_namespace(_prefix, _uri)
    except ValueError:  # pragma: no cover - reserved prefixes
        pass

TEMPLATE = """<x:xmpmeta xmlns:x="adobe:ns:meta/" x:xmptk="Adobe XMP Core 7.0-c000 1.000000, 0000/00/00-00:00:00        ">
  <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">
    <rdf:Description rdf:about=""
        xmlns:aux="http://ns.adobe.com/exif/1.0/aux/"
        xmlns:crd="http://ns.adobe.com/camera-raw-defaults/1.0/"
        xmlns:dc="http://purl.org/dc/elements/1.1/"
        xmlns:exif="http://ns.adobe.com/exif/1.0/"
        xmlns:lrt="http://lrtimelapse.com/"
        xmlns:photoshop="http://ns.adobe.com/photoshop/1.0/"
        xmlns:tiff="http://ns.adobe.com/tiff/1.0/"
        xmlns:xmp="http://ns.adobe.com/xap/1.0/"
        xmlns:xmpMM="http://ns.adobe.com/xap/1.0/mm/"
        xmlns:stEvt="http://ns.adobe.com/xap/1.0/sType/ResourceEvent#"
        xmlns:crs="http://ns.adobe.com/camera-raw-settings/1.0/">
      <crs:Version>14</crs:Version>
      <crs:ProcessVersion>11.0</crs:ProcessVersion>
      <crs:HasSettings>true</crs:HasSettings>
      <crs:WhiteBalance>As Shot</crs:WhiteBalance>
      <crs:Exposure2012>0.00</crs:Exposure2012>
      <crs:CropTop>0.0</crs:CropTop>
      <crs:CropLeft>0.0</crs:CropLeft>
      <crs:CropBottom>1.0</crs:CropBottom>
      <crs:CropRight>1.0</crs:CropRight>
      <crs:CropAngle>0.0</crs:CropAngle>
      <crs:HasCrop>false</crs:HasCrop>
      <crs:Look>
        <rdf:Description
            crs:Name=""
            crs:Amount="1.0">
        </rdf:Description>
      </crs:Look>
    </rdf:Description>
  </rdf:RDF>
</x:xmpmeta>
<?xpacket end="w"?>"""

#: star rating that marks a keyframe in the LRT workflow
RATING_KEYFRAME = 4
RATING_HG_LEFT = 2
RATING_HG_RIGHT = 3
RATING_CROP = 5
KEYFRAME_RATINGS = (RATING_HG_LEFT, RATING_HG_RIGHT, RATING_KEYFRAME, RATING_CROP)

_XPACKET_BEGIN = '<?xpacket begin="\ufeff" id="W5M0MpCehiHzreSzNTczkc9d"?>\n'
_XPACKET_END = '\n<?xpacket end="w"?>'


class XmpError(RuntimeError):
    """Raised when a sidecar cannot be parsed or is not an XMP packet."""


def _split_uri(qname: str) -> tuple[str, str]:
    try:
        prefix, local = qname.split(":", 1)
    except ValueError as exc:  # pragma: no cover - caller error
        raise XmpError(f"property name must look like 'crs:Exposure2012': {qname!r}") from exc
    if prefix not in NSMAP:
        raise XmpError(f"unknown XMP namespace prefix: {prefix!r}")
    return NSMAP[prefix], local


@dataclass
class XmpSidecar:
    """A single Lightroom sidecar that can be edited in place."""

    tree: ET.ElementTree
    description: ET.Element

    # -- construction ------------------------------------------------------
    @classmethod
    def new(cls) -> "XmpSidecar":
        return cls.parse(TEMPLATE)

    @classmethod
    def parse(cls, text: str) -> "XmpSidecar":
        body = _strip_packet(text)
        # Register prefixes that the document declares but we do not know, so
        # serialisation never rewrites them to ns0:/ns1:.
        for prefix, uri in re.findall(r'xmlns:([A-Za-z_][\w.-]*)="([^"]+)"', body):
            if prefix not in NSMAP:
                try:
                    ET.register_namespace(prefix, uri)
                except ValueError:
                    pass
        try:
            root = ET.fromstring(body)
        except ET.ParseError as exc:
            raise XmpError(f"sidecar is not valid XML: {exc}") from exc
        if root.tag != f"{{{NSMAP['x']}}}xmpmeta":
            raise XmpError(f"not an XMP packet: root element {root.tag!r}")
        rdf = root.find(f"{{{NSMAP['rdf']}}}RDF")
        if rdf is None:
            raise XmpError("XMP packet has no rdf:RDF element")
        desc = rdf.find(f"{{{NSMAP['rdf']}}}Description")
        if desc is None:
            raise XmpError("XMP packet has no rdf:Description element")
        return cls(ET.ElementTree(root), desc)

    @classmethod
    def load(cls, path: str | Path) -> "XmpSidecar":
        raw = Path(path).read_bytes()
        for encoding in ("utf-8-sig", "utf-8", "latin-1"):
            try:
                text = raw.decode(encoding)
                break
            except UnicodeDecodeError:
                continue
        else:  # pragma: no cover - latin-1 never fails
            raise XmpError(f"cannot decode sidecar {path}")
        doc = cls.parse(text)
        doc.path = Path(path)  # type: ignore[attr-defined]
        return doc

    @classmethod
    def load_or_new(cls, path: str | Path) -> "XmpSidecar":
        p = Path(path)
        if p.exists() and p.stat().st_size > 0:
            try:
                return cls.load(p)
            except XmpError:
                pass
        doc = cls.new()
        doc.path = p  # type: ignore[attr-defined]
        return doc

    # -- property access ---------------------------------------------------
    def _attr(self, uri: str, local: str) -> str | None:
        return self.description.get(f"{{{uri}}}{local}")

    def _child(self, uri: str, local: str) -> ET.Element | None:
        return self.description.find(f"{{{uri}}}{local}")

    def get(self, qname: str, default: str | None = None) -> str | None:
        uri, local = _split_uri(qname)
        attr = self._attr(uri, local)
        if attr is not None:
            return attr
        el = self._child(uri, local)
        if el is not None:
            return (el.text or "").strip()
        return default

    def get_float(self, qname: str, default: float = 0.0) -> float:
        raw = self.get(qname)
        if raw is None or raw == "":
            return default
        try:
            return float(raw)
        except ValueError:
            return default

    def get_int(self, qname: str, default: int = 0) -> int:
        return int(round(self.get_float(qname, float(default))))

    def set(self, qname: str, value: str | int | float) -> None:
        uri, local = _split_uri(qname)
        text = _format_value(value)
        # 1) existing attribute on rdf:Description
        if self.description.get(f"{{{uri}}}{local}") is not None:
            self.description.set(f"{{{uri}}}{local}", text)
            return
        # 2) existing child element
        el = self._child(uri, local)
        if el is not None:
            el.text = text
            return
        # 3) create a child element in a stable position
        el = ET.SubElement(self.description, f"{{{uri}}}{local}")
        el.text = text

    def remove(self, qname: str) -> None:
        uri, local = _split_uri(qname)
        if self.description.get(f"{{{uri}}}{local}") is not None:
            del self.description.attrib[f"{{{uri}}}{local}"]
        el = self._child(uri, local)
        if el is not None:
            self.description.remove(el)

    # -- workflow helpers --------------------------------------------------
    @property
    def rating(self) -> int:
        return self.get_int("xmp:Rating", 0)

    @rating.setter
    def rating(self, value: int) -> None:
        if value <= 0:
            self.set("xmp:Rating", 0)
        else:
            self.set("xmp:Rating", int(value))

    @property
    def is_keyframe(self) -> bool:
        return self.rating in KEYFRAME_RATINGS

    def exposure(self) -> float:
        return self.get_float("crs:Exposure2012", 0.0)

    def set_exposure(self, value: float) -> None:
        self.set("crs:Exposure2012", f"{value:.2f}")

    # -- persistence -------------------------------------------------------
    def to_bytes(self) -> bytes:
        body = ET.tostring(self.tree.getroot(), encoding="unicode")
        text = _XPACKET_BEGIN + body + _XPACKET_END + "\n"
        return text.encode("utf-8")

    def save(self, path: str | Path | None = None) -> Path:
        target = Path(path) if path is not None else getattr(self, "path", None)
        if target is None:
            raise XmpError("no destination path for sidecar")
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(target.suffix + ".tmp")
        tmp.write_bytes(self.to_bytes())
        tmp.replace(target)
        return target


def _strip_packet(text: str) -> str:
    """Drop the xpacket wrapper, BOM and XML declaration; keep the xmpmeta root."""
    text = text.lstrip("\ufeff \t\r\n")
    text = re.sub(r"<\?xpacket[^>]*\?>", "", text)
    text = re.sub(r"<\?xml[^>]*\?>", "", text)
    start = text.find("<x:xmpmeta")
    if start < 0:
        # tolerate a bare rdf:RDF document
        start = text.find("<rdf:RDF")
        if start < 0:
            raise XmpError("no x:xmpmeta or rdf:RDF element found")
    return text[start:].strip()


def _format_value(value: str | int | float) -> str:
    if isinstance(value, bool):  # pragma: no cover - defensive
        return "True" if value else "False"
    if isinstance(value, float):
        return f"{value:.6g}"
    return str(value)
