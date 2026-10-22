"""Retain static SVG token shapes while removing active and external content."""
from __future__ import annotations

import math
import re
from xml.etree import ElementTree as ET


SVG = "http://www.w3.org/2000/svg"
TAGS = {"svg", "g", "path", "rect", "circle", "ellipse", "line", "polygon", "polyline",
        "defs", "linearGradient", "radialGradient", "stop", "clipPath", "mask", "use", "title", "desc"}
NUMBERS = {"x", "y", "x1", "y1", "x2", "y2", "cx", "cy", "r", "rx", "ry", "width", "height",
           "fx", "fy", "offset", "opacity", "fill-opacity", "stroke-opacity", "stop-opacity",
           "stroke-width", "stroke-miterlimit", "stroke-dashoffset"}
PAINT = {"fill", "stroke", "stop-color"}
ENUMS = {"fill-rule": {"nonzero", "evenodd"}, "clip-rule": {"nonzero", "evenodd"},
         "stroke-linecap": {"butt", "round", "square"}, "stroke-linejoin": {"miter", "round", "bevel"},
         "gradientUnits": {"objectBoundingBox", "userSpaceOnUse"},
         "clipPathUnits": {"objectBoundingBox", "userSpaceOnUse"},
         "maskUnits": {"objectBoundingBox", "userSpaceOnUse"}, "maskContentUnits": {"objectBoundingBox", "userSpaceOnUse"}}
NUMBER = r"[-+]?(?:\d*\.\d+|\d+\.?\d*)(?:[eE][-+]?\d+)?"
IDENTIFIER = r"[A-Za-z_][A-Za-z0-9_.-]{0,127}"


def _finite_coordinates(value: str) -> bool:
    return all(math.isfinite(float(number)) and abs(float(number)) <= 1_000_000
               for number in re.findall(NUMBER, value))


def _attribute(name: str, value: str) -> str | None:
    value = value.strip()
    if len(value) > 250_000 or not value:
        return None
    if name in NUMBERS:
        return value if re.fullmatch(NUMBER + r"(?:px|%)?", value) and _finite_coordinates(value) else None
    if name in PAINT:
        return value if (re.fullmatch(r"#[0-9A-Fa-f]{3,8}|[A-Za-z]{1,24}|(?:rgb|rgba|hsl|hsla)\([\d\s.,%+\-]+\)", value)
                         or re.fullmatch(r"url\(#" + IDENTIFIER + r"\)", value)) else None
    if name in {"clip-path", "mask"}:
        return value if value == "none" or re.fullmatch(r"url\(#" + IDENTIFIER + r"\)", value) else None
    if name == "id":
        return value if re.fullmatch(IDENTIFIER, value) else None
    if name == "href":
        return value if re.fullmatch("#" + IDENTIFIER, value) else None
    if name in ENUMS:
        return value if value in ENUMS[name] else None
    if name in {"transform", "gradientTransform"}:
        return value if re.fullmatch(r"(?:\s*(?:matrix|translate|scale|rotate|skewX|skewY)\([-+\d.eE,\s]+\)\s*)+", value) and _finite_coordinates(value) else None
    if name == "d":
        return value if re.fullmatch(r"[MmZzLlHhVvCcSsQqTtAa\d.eE,+\-\s]+", value) and _finite_coordinates(value) else None
    if name in {"points", "stroke-dasharray", "viewBox"}:
        return value if re.fullmatch(r"[-+\d.eE,\s]+", value) and _finite_coordinates(value) else None
    return None


def sanitize_token_svg(body: bytes) -> bytes:
    if len(body) > 1_000_000 or b"\x00" in body:
        raise ValueError("Token SVG is too large or has an unsupported encoding")
    try:
        text = body.decode("utf-8-sig")
        if re.search(r"<!\s*(?:DOCTYPE|ENTITY)", text, re.I):
            raise ValueError("Token SVG declarations are unsupported")
        original = ET.fromstring(text)
    except (UnicodeError, ET.ParseError):
        raise ValueError("Token SVG is invalid") from None
    if original.tag not in ("svg", f"{{{SVG}}}svg") or sum(1 for _ in original.iter()) > 5000:
        raise ValueError("Token SVG root or shape count is invalid")

    def copy(node: ET.Element, depth: int = 0) -> ET.Element | None:
        if depth > 40:
            return None
        if node.tag.startswith("{"):
            namespace, name = node.tag[1:].split("}", 1)
            if namespace != SVG:
                return None
        else:
            name = node.tag
        if name not in TAGS:
            return None
        clean = ET.Element(f"{{{SVG}}}{name}")
        for raw_name, value in node.attrib.items():
            attr = "href" if raw_name == "{http://www.w3.org/1999/xlink}href" else raw_name
            if raw_name == "style":
                for declaration in value.split(";"):
                    key, _, val = declaration.partition(":")
                    key = key.strip()
                    if key in PAINT | NUMBERS | {"fill-rule", "clip-rule", "stroke-linecap", "stroke-linejoin"} and (safe := _attribute(key, val)) is not None:
                        clean.set(key, safe)
            elif (safe := _attribute(attr, value)) is not None:
                clean.set(attr, safe)
        if name in {"title", "desc"}:
            clean.text = (node.text or "")[:200]
        for child in node:
            retained = copy(child, depth + 1)
            if retained is not None:
                clean.append(retained)
        return clean

    result = copy(original)
    assert result is not None
    box = result.get("viewBox", "").replace(",", " ").split()
    if len(box) != 4 or float(box[2]) <= 0 or float(box[3]) <= 0:
        result.set("viewBox", "0 0 64 64")
    result.set("width", "64")
    result.set("height", "64")
    if not any(node.tag.rsplit("}", 1)[-1] in {"path", "rect", "circle", "ellipse", "line", "polygon", "polyline", "use"} for node in result.iter()):
        raise ValueError("Token SVG contains no supported static shapes")
    return ET.tostring(result, encoding="utf-8", xml_declaration=True)
