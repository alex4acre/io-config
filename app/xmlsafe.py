# SPDX-License-Identifier: MIT

"""XML parsing for files users upload (ESI) or the master hands over (ENI).

ESI and ENI files never need a DOCTYPE, so documents with one are refused: that rules out entity
expansion ("billion laughs") and external entities without an extra library. Namespaces are dropped
from the tags, so code can look for "Device" whatever prefix a vendor's tool wrote.
"""

import re
import xml.etree.ElementTree as ET

_DOCTYPE = re.compile(rb"<!DOCTYPE|<!ENTITY", re.IGNORECASE)


class XmlError(ValueError):
    pass


def check(data: bytes):
    if _DOCTYPE.search(data):
        raise XmlError("XML with a DOCTYPE/ENTITY is not accepted")


def _strip_namespaces(root):
    for el in root.iter():
        if isinstance(el.tag, str) and "}" in el.tag:
            el.tag = el.tag.split("}", 1)[1]
    return root


def parse_xml(data) -> ET.Element:
    check(data.encode("utf-8", "replace") if isinstance(data, str) else data)
    try:
        return _strip_namespaces(ET.fromstring(data))
    except ET.ParseError as e:
        raise XmlError(f"not valid XML: {e}") from e


def root_tag(path: str) -> str:
    """The root element's name, without building the whole tree (for sorting uploads quickly)."""
    with open(path, "rb") as f:
        check(f.read())
        f.seek(0)
        try:
            for _event, el in ET.iterparse(f, events=("start",)):
                return el.tag.split("}", 1)[-1]
        except ET.ParseError as e:
            raise XmlError(f"not valid XML: {e}") from e
    return ""
