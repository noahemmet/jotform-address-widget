"""
Shared parsing for the Jotform address-map widget's stored value.

The widget can only occupy one Jotform column, so it packs every address
component into a single value. Everything here is about getting those
components back out — used by both the CSV and the KML writers.

Pure stdlib, no network. Import it; don't run it.
"""

import html
import json
from xml.sax.saxutils import escape

# Output name -> keys to look for in the blob, in priority order. The widget's
# `json` format abbreviates (lat/lng); its `lines` format spells things out
# (Latitude/Longitude) and gets camel-cased on the way in.
COMPONENTS = {
    "neighborhood":        ("neighborhood", "neighbourhood"),
    "street":              ("street",),
    "city":                ("city",),
    "county":              ("county",),
    "state":               ("state", "region"),
    "postal":              ("postal", "zip", "postcode"),
    "country":             ("country",),
    "country_code":        ("countryCode",),
    "lat":                 ("lat", "latitude"),
    "lng":                 ("lng", "lon", "long", "longitude"),
    "full_address":        ("address", "fullAddress"),
    "neighborhood_source": ("neighborhoodSource",),
    "provider":            ("provider",),
}

DEFAULT_FIELDS = ["neighborhood", "street", "city", "county", "state",
                  "postal", "country", "country_code", "lat", "lng"]


def parse_blob(raw):
    """Accept the widget's `json` OR `lines` output. Returns a dict or None."""
    raw = html.unescape(raw or "").strip()
    if not raw:
        return None

    if raw.startswith("{"):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return None
        return parsed if isinstance(parsed, dict) else None

    if ": " in raw:
        out = {}
        for line in raw.splitlines():
            if ": " not in line:
                continue
            k, _, v = line.partition(": ")
            parts = k.strip().split()
            if not parts:
                continue
            # "Country Code" -> countryCode
            key = parts[0].lower() + "".join(p.capitalize() for p in parts[1:])
            out[key] = v.strip()
        return out or None

    return None


def component(blob, name):
    """One named component out of a parsed blob, or "" if absent."""
    if not blob:
        return ""
    for key in COMPONENTS[name]:
        val = blob.get(key)
        if val not in (None, ""):
            return str(val)
    return ""


def coords(blob):
    """(lng, lat) floats, or None when the geocode didn't produce a usable point."""
    lat, lng = component(blob, "lat"), component(blob, "lng")
    try:
        lat, lng = float(lat), float(lng)
    except (TypeError, ValueError):
        return None
    if not (-90 <= lat <= 90) or not (-180 <= lng <= 180):
        return None
    # Null island means a failed geocode, never a real spot.
    if lat == 0 and lng == 0:
        return None
    return lng, lat


def looks_like_address(value):
    """True when a value parses and carries at least two known components.

    The two-component floor keeps a Long Text field that happens to contain a
    brace from being mistaken for the address column.
    """
    blob = parse_blob(value)
    if not blob:
        return False
    return sum(1 for f in DEFAULT_FIELDS if component(blob, f)) >= 2


def detect_address_key(rows, keys):
    """Pick the column/key most often holding a real address blob."""
    best, best_hits = None, 0
    for key in keys:
        hits = sum(1 for r in rows if looks_like_address(r.get(key, "")))
        if hits > best_hits:
            best, best_hits = key, hits
    return best


# --------------------------------------------------------------------------
# KML
# --------------------------------------------------------------------------

ICON_BASE = "http://maps.google.com/mapfiles/kml/paddle/"
CATEGORY_ICONS = {
    "art installation":     "red-circle.png",
    "vending machines":     "blu-circle.png",
    "free art exchanges":   "purple-circle.png",
    "plant and seed swaps": "grn-circle.png",
    "yard installations":   "ylw-circle.png",
    "trinkets":             "orange-circle.png",
    "free library":         "ltblu-circle.png",
    "mini galleries":       "pink-circle.png",
    "wishing trees":        "wht-circle.png",
    "one-of-a-kinds":       "grn-stars.png",
}
DEFAULT_ICON = "red-circle.png"

EXTENDED_COMPONENTS = ("neighborhood", "street", "city", "county", "state",
                       "postal", "country", "country_code")


def _style_id(category):
    key = (category or "").strip().lower()
    return "cat-" + (key.replace(" ", "-") if key in CATEGORY_ICONS else "default")


def build_kml(rows, address_key, doc_name,
              name_key=None, desc_key=None, category_key=None, skipped=None,
              group_key=None, drop=None):
    """Rows are plain dicts of label -> value. Returns the KML document text.

    With group_key, placemarks are wrapped in one <Folder> per distinct value.
    Google Earth always shows those as a tree; My Maps *may* turn them into
    separate layers, which is worth trying before splitting into files.
    """
    if skipped is None:
        skipped = []
    # Names of columns/components to keep out of the file entirely. Compared
    # case-insensitively so "Your Name" and "your name" both match.
    drop = {d.strip().lower() for d in (drop or [])}

    out = ['<?xml version="1.0" encoding="UTF-8"?>',
           '<kml xmlns="http://www.opengis.net/kml/2.2">',
           "<Document>",
           f"  <name>{escape(doc_name)}</name>"]

    for label, icon in list(CATEGORY_ICONS.items()) + [("default", DEFAULT_ICON)]:
        sid = "cat-" + (label.replace(" ", "-") if label != "default" else "default")
        out += [f'  <Style id="{sid}">',
                "    <IconStyle>",
                f"      <Icon><href>{ICON_BASE}{icon}</href></Icon>",
                "    </IconStyle>",
                "  </Style>"]

    if group_key:
        # Stable order: named groups alphabetically, unlabelled last.
        groups, order = {}, []
        for row in rows:
            g = (row.get(group_key) or "").strip() or "Uncategorized"
            if g not in groups:
                groups[g] = []
                order.append(g)
            groups[g].append(row)
        for g in sorted(order, key=lambda s: (s == "Uncategorized", s.lower())):
            body = build_kml(groups[g], address_key, doc_name,
                             name_key=name_key, desc_key=desc_key,
                             category_key=category_key, skipped=skipped,
                             drop=drop)
            inner = body.split("</Style>")[-1].rsplit("</Document>", 1)[0].strip()
            if not inner:
                continue
            out.append("  <Folder>")
            out.append(f"    <name>{escape(g)}</name>")
            out.append(inner)
            out.append("  </Folder>")
        out += ["</Document>", "</kml>"]
        return "\n".join(out) + "\n"

    for i, row in enumerate(rows, start=1):
        blob = parse_blob(row.get(address_key, ""))
        point = coords(blob)
        label = (row.get(name_key) or "").strip() if name_key else ""
        if not point:
            skipped.append((i, label or f"row {i}"))
            continue
        lng, lat = point

        name = label or component(blob, "neighborhood") or f"Spot {i}"
        desc = (row.get(desc_key) or "").strip() if desc_key else ""
        category = (row.get(category_key) or "").strip() if category_key else ""

        out.append("  <Placemark>")
        out.append(f"    <name>{escape(name)}</name>")
        if desc:
            # CDATA keeps punctuation and apostrophes intact in the balloon.
            out.append(f"    <description><![CDATA[{desc.replace(']]>', ']] >')}]]></description>")
        out.append(f"    <styleUrl>#{_style_id(category)}</styleUrl>")

        out.append("    <ExtendedData>")
        for key in EXTENDED_COMPONENTS:
            if key.lower() in drop:
                continue
            val = component(blob, key)
            if val:
                out.append(f'      <Data name="{key}">'
                           f"<value>{escape(val)}</value></Data>")
        for col, val in row.items():
            if not col or col == address_key:
                continue
            if col in (name_key, desc_key):
                continue
            if col.strip().lower() in drop:
                continue
            val = ("" if val is None else str(val)).strip()
            if not val:
                continue
            out.append(f'      <Data name="{escape(col)}">'
                       f"<value>{escape(val)}</value></Data>")
        out.append("    </ExtendedData>")

        out.append(f"    <Point><coordinates>{lng},{lat},0</coordinates></Point>")
        out.append("  </Placemark>")

    out += ["</Document>", "</kml>"]
    return "\n".join(out) + "\n"
