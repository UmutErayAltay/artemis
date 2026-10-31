"""Pet üretici, Qt'siz kısım (ADR 0048 madde 8): istem, yanıttan SVG çıkarma, SVG temizleme.

Güven sınırı `sanitize_svg`'dir: model çıktısı güvenilmezdir. Beyaz liste dışındaki her öğe
ve öznitelik atılır (olay işleyicileri, href, style, script, foreignObject, image, use…); DOCTYPE/
ENTITY/işlem yönergesi ve 64 KB üstü girdi baştan reddedilir. Temizlenmiş ağaç yeniden
serileştirilir; çizimi `pet_animator` yapar.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET

from artemis.channels.desktop.pet_sprite import ID_RE

MAX_SVG_BYTES = 64 * 1024
MAX_ELEMENTS = 2000
MAX_DEPTH = 32
SVG_NS = "http://www.w3.org/2000/svg"
VIEWBOX = "0 0 192 208"

SYSTEM_PROMPT = (
    "Sen bir masaüstü maskot ressamısın. Kullanıcının isteğine göre TEK bir <svg> öğesi döndür.\n"
    f'Kurallar: viewBox="{VIEWBOX}"; ortada tek, sevimli, tam boy bir maskot karakter; ayaklar '
    "y≈200 civarında; saydam zemin (arka plan çizme); düz renkler ya da basit gradyanlar; "
    "metin, <image>, <style>, script ve dış bağlantı yok; gözleri <g id=\"eyes\"> içine, "
    "varsa ağzı <g id=\"mouth\"> içine koy; toplam 64 KB'tan küçük.\n"
    "Kullanıcı düzeltme isterse önceki SVG'yi o isteğe göre değiştirip tam SVG'yi yeniden ver. "
    "Yanıtta SVG dışında en çok bir kısa cümle olsun."
)

_ELEMENTS = frozenset(
    (
    "svg", "g", "path", "circle", "ellipse", "rect", "line", "polyline", "polygon", "defs",
    "linearGradient", "radialGradient", "stop", "clipPath", "title", "desc",
    )
)
_PLAIN = frozenset(
    (
    "d", "points", "transform", "gradientTransform", "gradientUnits", "viewBox", "x", "y",
    "x1", "y1", "x2", "y2", "cx", "cy", "r", "rx", "ry", "fx", "fy", "width", "height",
    "offset", "opacity", "fill-opacity", "stroke-opacity", "stop-opacity", "fill-rule",
    "clip-rule", "stroke-width", "stroke-linecap", "stroke-linejoin", "stroke-miterlimit",
    "stroke-dasharray", "stroke-dashoffset",
    )
)
_PAINT = frozenset({"fill", "stroke", "stop-color", "clip-path"})
_PLAIN_VALUE = re.compile(r"[\w\s.,+\-()%#]*")
_LOCAL_URL = re.compile(r"url\(\s*#[A-Za-z_][\w.\-]*\s*\)")
_XML_DECL = re.compile(r"\A\s*<\?xml\s[^>]*\?>", re.IGNORECASE)
_FORBIDDEN = re.compile(r"<!\s*(doctype|entity)|<\?", re.IGNORECASE)
_SVG_BLOCK = re.compile(r"<svg\b.*</svg\s*>", re.IGNORECASE | re.DOTALL)


class SvgError(ValueError):
    """SVG kullanılamaz; mesaj kullanıcıya gösterilebilir (Türkçe)."""


def extract_svg(reply: str) -> str | None:
    """Yanıttan ilk `<svg` ile son `</svg>` arasını alır (kod çitleri ve ek yazı tolere edilir)."""
    match = _SVG_BLOCK.search(reply)
    return match.group(0) if match else None


def slugify(name: str) -> str:
    """Görünen addan `^[a-z0-9][a-z0-9-]{0,63}$` kimliği; boş kalırsa 'pet'."""
    table = str.maketrans("çğıöşüÇĞİÖŞÜ", "cgiosucgiosu")
    text = re.sub(r"[^a-z0-9]+", "-", name.translate(table).lower()).strip("-")[:64].strip("-")
    slug = text or "pet"
    assert ID_RE.fullmatch(slug)
    return slug


def _clean_paint(name: str, value: str) -> str | None:
    value = value.strip()
    if "url" in value.lower():
        local = name in {"fill", "stroke", "clip-path"} and _LOCAL_URL.fullmatch(value)
        return value if local else None
    if name == "clip-path":
        return value if value == "none" else None
    return value if _PLAIN_VALUE.fullmatch(value) else None


def _clean(element: ET.Element, depth: int, budget: list[int]) -> ET.Element | None:
    name = element.tag.rsplit("}", 1)[-1] if isinstance(element.tag, str) else ""
    if name not in _ELEMENTS or depth > MAX_DEPTH:
        return None
    budget[0] -= 1
    if budget[0] < 0:
        raise SvgError("SVG çok karmaşık (öğe sayısı sınırı aşıldı).")
    clean = ET.Element(f"{{{SVG_NS}}}{name}")
    for key, value in element.attrib.items():
        if key == "id":
            if re.fullmatch(r"[A-Za-z_][\w.\-]*", value):
                clean.set("id", value)
        elif key in _PAINT:
            kept = _clean_paint(key, value)
            if kept is not None:
                clean.set(key, kept)
        elif key in _PLAIN and _PLAIN_VALUE.fullmatch(value):
            clean.set(key, value)
    # Metin gerekmez (çizilmez); boş bırakmak title/desc içinden de veri taşımayı keser.
    for child in element:
        kept_child = _clean(child, depth + 1, budget)
        if kept_child is not None:
            clean.append(kept_child)
    return clean


def sanitize_svg(svg: str) -> str:
    """Beyaz listeyle temizlenmiş SVG metni; kullanılamıyorsa SvgError."""
    if len(svg.encode("utf-8")) > MAX_SVG_BYTES:
        raise SvgError("SVG 64 KB'tan büyük.")
    body = _XML_DECL.sub("", svg, count=1)
    if _FORBIDDEN.search(body):
        raise SvgError("SVG'de DOCTYPE, ENTITY ya da işlem yönergesi var; reddedildi.")
    try:
        root = ET.fromstring(body.strip())
    except ET.ParseError as error:
        raise SvgError(f"SVG ayrıştırılamadı: {error}") from error
    if root.tag.rsplit("}", 1)[-1] != "svg":
        raise SvgError("Kök öğe <svg> olmalı.")
    clean = _clean(root, 0, [MAX_ELEMENTS])
    assert clean is not None  # kök svg beyaz listede
    clean.attrib.pop("width", None)  # boyutu çizici belirler
    clean.attrib.pop("height", None)
    clean.set("viewBox", clean.get("viewBox") or VIEWBOX)
    if len(clean) == 0:
        raise SvgError("SVG boş: çizilebilir öğe kalmadı.")
    ET.register_namespace("", SVG_NS)
    return ET.tostring(clean, encoding="unicode")


def without_eyes(svg: str) -> str:
    """Temizlenmiş SVG'den `id="eyes"` grubunu çıkarır (göz kırpma karesi); yoksa aynısı."""
    root = ET.fromstring(svg)
    for parent in root.iter():
        for child in list(parent):
            if child.get("id") == "eyes":
                parent.remove(child)
    ET.register_namespace("", SVG_NS)
    return ET.tostring(root, encoding="unicode")
