#!/usr/bin/env python3
"""
Pull Jotform submissions via the API, keep only the verified ones, and write
a flat CSV (address components exploded into real columns) plus a KML.

    JOTFORM_API_KEY=... python3 export.py --form 262182145100039

Nothing is exported unless it passes the verification gate. If the status
field can't be found the run FAILS rather than quietly publishing everything —
see --status-field / --allow-unverified.

Stdlib only. No dependencies to install.
"""

import argparse
import csv
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

import addresslib as A

# Overridable only so the test suite can point at a local stand-in.
API_BASE = os.environ.get("JF_API_BASE", "https://api.jotform.com/v1")
PAGE_SIZE = 1000          # the API's per-request maximum
MAX_PAGES = 100           # backstop against a pagination bug looping forever

# Submission-level states the API reports. Anything else is trashed/quota'd
# and must never reach the map.
LIVE_STATUSES = {"ACTIVE"}

# Column labels used for the KML placemark's own name / description / styling.
NAME_HINTS = ("title", "name", "spot")
DESC_HINTS = ("description", "desc", "notes")
CATEGORY_HINTS = ("category", "type")


def log(msg):
    print(msg, file=sys.stderr, flush=True)


# --------------------------------------------------------------------------
# API
# --------------------------------------------------------------------------

def api_get(path, api_key, params=None, retries=3):
    url = API_BASE + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    # The key goes in a header, never the query string, so it stays out of
    # proxy logs and CI console output.
    req = urllib.request.Request(url, headers={
        "apiKey": api_key,
        "User-Agent": "jotform-export/1.0",
        "Accept": "application/json",
    })

    last = None
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")[:400]
            if e.code in (401, 403):
                raise SystemExit(
                    f"Jotform rejected the API key ({e.code}). Check that "
                    f"JOTFORM_API_KEY is set and has read access.\n{body}")
            if e.code == 404:
                raise SystemExit(f"Not found: {path}\n{body}")
            # 429 and 5xx are worth retrying; back off so we don't make it worse.
            last = f"HTTP {e.code}: {body}"
        except (urllib.error.URLError, TimeoutError) as e:
            last = str(e)
        if attempt < retries - 1:
            time.sleep(2 ** attempt)
    raise SystemExit(f"Gave up on {path} after {retries} attempts.\n{last}")


def fetch_submissions(form_id, api_key):
    """Every submission, following offset pagination."""
    out, offset = [], 0
    for _ in range(MAX_PAGES):
        page = api_get(f"/form/{form_id}/submissions", api_key,
                       {"limit": PAGE_SIZE, "offset": offset})
        content = page.get("content") or []
        out.extend(content)
        if len(content) < PAGE_SIZE:
            return out
        offset += PAGE_SIZE
    log(f"WARNING: stopped at {MAX_PAGES} pages ({len(out)} submissions); "
        f"there may be more.")
    return out


# --------------------------------------------------------------------------
# Shaping
# --------------------------------------------------------------------------

def answer_text(ans):
    """Flatten one answer's value to a string.

    Composite fields (fullname, address) answer with a dict; the API also
    offers prettyFormat for some types, which reads better than the raw parts.
    """
    val = ans.get("answer")
    if val is None or val == "":
        val = ans.get("prettyFormat", "")
    if isinstance(val, dict):
        parts = [str(v).strip() for _, v in sorted(val.items())
                 if v not in (None, "") and not isinstance(v, (dict, list))]
        return " ".join(p for p in parts if p)
    if isinstance(val, list):
        return ", ".join(str(v) for v in val if v not in (None, ""))
    return str(val).strip()


def flatten(submissions):
    """API submissions -> (list of label->value dicts, ordered label list).

    Column order follows the form's own field order so the CSV reads like the
    form rather than like a hash table.
    """
    order = {}
    rows = []
    for sub in submissions:
        row = {"submission_id": sub.get("id", ""),
               "created_at": sub.get("created_at", "")}
        for qid, ans in (sub.get("answers") or {}).items():
            label = (ans.get("text") or f"field_{qid}").strip()
            # Two fields sharing a label would silently overwrite each other.
            if label in row and label not in ("submission_id", "created_at"):
                label = f"{label} ({qid})"
            row[label] = answer_text(ans)
            try:
                pos = int(ans.get("order", 9999))
            except (TypeError, ValueError):
                pos = 9999
            order.setdefault(label, pos)
        rows.append(row)

    labels = ["submission_id", "created_at"] + sorted(
        (k for k in order), key=lambda k: (order[k], k))
    return rows, labels


def find_exact(labels, name):
    for label in labels:
        if label.strip().lower() == name.strip().lower():
            return label
    return None


def find_label(labels, hints):
    """Locate a column by hint, case-insensitively: whole name, then substring."""
    for hint in hints:
        for label in labels:
            if label.strip().lower() == hint:
                return label
    for hint in hints:
        for label in labels:
            if hint in label.strip().lower():
                return label
    return None


def apply_verification_gate(rows, labels, args):
    """Drop everything that isn't verified. Fail loudly if we can't tell."""
    if args.allow_unverified:
        log("WARNING: --allow-unverified set. Exporting UNVERIFIED submissions.")
        return rows, None

    # An explicitly named field must exist exactly. Falling back to a guess
    # here would gate on the wrong column while looking like it worked.
    if args.status_field:
        status_label = find_exact(labels, args.status_field)
        looked_for = repr(args.status_field)
    else:
        status_label = find_label(labels, ("status", "approval", "flow status"))
        looked_for = "a field named like 'Status'"

    if not status_label:
        raise SystemExit(
            "Couldn't find the verification field.\n"
            f"  looked for: {looked_for}\n"
            f"  available:  {', '.join(labels)}\n\n"
            "Pass --status-field with the right column name. If you really do "
            "want to export unreviewed submissions, pass --allow-unverified.")

    wanted = {v.strip().lower() for v in args.status_value.split(",") if v.strip()}
    kept = [r for r in rows if (r.get(status_label) or "").strip().lower() in wanted]

    seen = sorted({(r.get(status_label) or "(blank)").strip() for r in rows})
    log(f"Verification gate: {status_label!r} in {sorted(wanted)} "
        f"-> kept {len(kept)}/{len(rows)}")
    log(f"  values present: {', '.join(seen)}")

    if rows and not kept:
        log("  NOTE: nothing matched. Check --status-value against the list above.")
    return kept, status_label


# --------------------------------------------------------------------------
# Row filtering
# --------------------------------------------------------------------------

# Every spelling that resolves to a component: the output name plus each of the
# source keys the widget might have used ("region" -> state, "zip" -> postal).
COMPONENT_ALIASES = {}
for _out, _keys in A.COMPONENTS.items():
    COMPONENT_ALIASES[_out.lower()] = _out
    for _k in _keys:
        COMPONENT_ALIASES.setdefault(_k.lower(), _out)

OPERATORS = ("!=", "~=", "=")     # longest first; "=" is a suffix of the others


def parse_filters(raw_list, labels):
    """['city=Portland', 'state=Oregon'] -> [(name, op, value, is_component)].

    Comma-separated pairs are accepted in one --filter; repeat the flag when a
    value itself contains a comma.
    """
    out = []
    for raw in raw_list or []:
        for clause in raw.split(","):
            clause = clause.strip()
            if not clause:
                continue
            for op in OPERATORS:
                if op in clause:
                    name, value = clause.split(op, 1)
                    break
            else:
                raise SystemExit(
                    f"--filter: can't read {clause!r}. Expected field=value "
                    f"(also != and ~= for 'not' and 'contains').")

            name, value = name.strip(), value.strip()
            label = find_exact(labels, name)
            component = COMPONENT_ALIASES.get(name.lower())
            if label is None and component is None:
                raise SystemExit(
                    f"--filter: no such field {name!r}\n"
                    f"  form fields: {', '.join(labels)}\n"
                    f"  components:  {', '.join(A.COMPONENTS)}")
            out.append((label or component, op, value, label is None))
    return out


def filter_value(row, name, is_component, address_label):
    if is_component:
        blob = A.parse_blob(row.get(address_label, "")) if address_label else None
        return A.component(blob, name)
    return row.get(name) or ""


def apply_filters(rows, filters, address_label):
    """AND across clauses. Reports the values present when nothing matches, so
    a near-miss like country=usa vs 'United States' is obvious immediately."""
    for name, op, want, is_component in filters:
        before = len(rows)
        w = want.lower()

        def keep(row, name=name, op=op, w=w, is_component=is_component):
            have = filter_value(row, name, is_component, address_label).lower()
            if op == "=":
                return have == w
            if op == "!=":
                return have != w
            return w in have          # ~=

        present = sorted({filter_value(r, name, is_component, address_label)
                          or "(blank)" for r in rows})
        rows = [r for r in rows if keep(r)]
        log(f"Filter {name}{op}{want!r} -> kept {len(rows)}/{before}")
        if before and not rows:
            log(f"  no match. values present: {', '.join(present)}")
    return rows


# --------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------

def resolve_selection(labels, address_label, args):
    """Work out the output columns from --fields / --omit.

    --fields is an allowlist over BOTH form fields and address components, and
    the order you give is the column order you get. --omit is the denylist
    counterpart, applied to the default set.

    Returns (out_columns, component_names, drop_set). Selection affects output
    only — the gate, the address parser and --split-by still see every column,
    so you can drop a field you're still filtering or grouping on.
    """
    components = list(A.COMPONENTS)

    # The default layout: form fields in form order, with the address
    # components slotted in where the widget column sat.
    default = []
    for label in labels:
        if label == address_label:
            if args.keep_json:
                default.append(label)
            default.extend(A.DEFAULT_FIELDS)
        elif label != address_label:
            default.append(label)
    if not address_label:
        default = list(labels)

    known = {c.lower(): c for c in components}
    known.update({label.lower(): label for label in labels})

    def parse(raw, flag):
        out, seen = [], set()
        for part in raw.split(","):
            name = part.strip()
            if not name:
                continue
            match = known.get(name.lower())
            if match is None:
                raise SystemExit(
                    f"{flag}: no such column {name!r}\n"
                    f"  form fields: {', '.join(labels)}\n"
                    f"  components:  {', '.join(components)}")
            if match.lower() not in seen:
                seen.add(match.lower())
                out.append(match)
        return out

    if args.fields and args.omit:
        raise SystemExit("Use --fields or --omit, not both. --fields already "
                         "lists exactly what you want.")

    if args.fields:
        out_columns = parse(args.fields, "--fields")
    elif args.omit:
        dropped = {c.lower() for c in parse(args.omit, "--omit")}
        out_columns = [c for c in default if c.lower() not in dropped]
    else:
        out_columns = default

    chosen = {c.lower() for c in out_columns}
    component_names = [c for c in components if c.lower() in chosen]
    drop = {k for k in known if k not in chosen}
    return out_columns, component_names, drop


def write_csv(path, rows, out_columns, address_label, component_names):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=out_columns, extrasaction="ignore")
        w.writeheader()
        for row in rows:
            blob = A.parse_blob(row.get(address_label, "")) if address_label else None
            out = dict(row)
            for name in component_names:
                out[name] = A.component(blob, name)
            w.writerow(out)
    return out_columns


# My Maps caps a map at 10 layers and a single import at 2,000 rows.
MYMAPS_MAX_LAYERS = 10
MYMAPS_MAX_ROWS = 2000


def slug(value):
    out = "".join(c.lower() if c.isalnum() else "-" for c in value)
    return "-".join(p for p in out.split("-") if p) or "uncategorized"


def write_split(rows, labels, address_label, out_columns, component_names, args,
                name_key=None, desc_key=None, category_key=None, drop=None):
    """One CSV + KML per distinct value — one file per My Maps layer."""
    group_label = find_exact(labels, args.split_by)
    if not group_label:
        raise SystemExit(
            f"Can't split by {args.split_by!r} — no such field.\n"
            f"  available: {', '.join(labels)}")

    groups = {}
    for row in rows:
        key = (row.get(group_label) or "").strip() or "Uncategorized"
        groups.setdefault(key, []).append(row)

    split_dir = os.path.join(args.out_dir, "layers")
    os.makedirs(split_dir, exist_ok=True)

    log(f"\nSplitting by {group_label!r} into {split_dir}/")
    for name in sorted(groups, key=str.lower):
        subset = groups[name]
        stem = os.path.join(split_dir, f"{args.basename}-{slug(name)}")
        write_csv(stem + ".csv", subset, out_columns, address_label,
                  component_names)
        dropped = []
        with open(stem + ".kml", "w", encoding="utf-8") as f:
            f.write(A.build_kml(subset, address_label, name,
                                name_key=name_key, desc_key=desc_key,
                                category_key=category_key, skipped=dropped,
                                drop=drop))
        placed = len(subset) - len(dropped)
        if len(subset) > MYMAPS_MAX_ROWS:
            flag = f"  <-- over My Maps' {MYMAPS_MAX_ROWS}-row limit"
        elif placed == 0:
            # My Maps rejects a KML with no placemarks; import the CSV instead.
            flag = "  <-- no mappable points; import the .csv for this one"
        else:
            flag = ""
        log(f"  {name:<24} {len(subset):>4} row(s), {placed:>4} placemark(s){flag}")

    if len(groups) > MYMAPS_MAX_LAYERS:
        log(f"\n  WARNING: {len(groups)} groups but My Maps allows only "
            f"{MYMAPS_MAX_LAYERS} layers per map. Combine some, or split "
            f"across two maps.")
    elif len(groups) == MYMAPS_MAX_LAYERS:
        log(f"\n  NOTE: {len(groups)} groups exactly fills My Maps' "
            f"{MYMAPS_MAX_LAYERS}-layer cap. A new category won't fit.")

    # Single-file alternative: folders instead of files. Import this one first —
    # if My Maps turns the folders into layers you can skip the per-file work.
    folders_path = os.path.join(args.out_dir, args.basename + "-layers.kml")
    with open(folders_path, "w", encoding="utf-8") as f:
        f.write(A.build_kml(rows, address_label,
                            args.doc_name or args.basename,
                            name_key=name_key, desc_key=desc_key,
                            category_key=category_key, group_key=group_label,
                            drop=drop))
    log(f"  also wrote {folders_path} (one <Folder> per {group_label})")


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--form", default=os.environ.get("JOTFORM_FORM_ID"),
                    help="form ID (or set JOTFORM_FORM_ID)")
    ap.add_argument("--out-dir", default=".", help="where to write the outputs")
    ap.add_argument("--basename", default="spots", help="output filename stem")
    ap.add_argument("--address-field", default=None,
                    help="label of the widget column (default: auto-detect)")
    ap.add_argument("--status-field", default=None,
                    help="exact label of the verification field "
                         "(default: auto-detect a field named like 'Status')")
    ap.add_argument("--status-value", default="Verified",
                    help="comma-separated value(s) that count as verified")
    ap.add_argument("--allow-unverified", action="store_true",
                    help="export everything, including unreviewed submissions")
    ap.add_argument("--fields", default=None, metavar="A,B",
                    help="export ONLY these columns, in this order. Accepts "
                         "form field labels and address components. "
                         "Default: every form field, with the standard "
                         "components in place of the widget column.")
    ap.add_argument("--keep-json", action="store_true",
                    help="keep the raw widget column alongside the components")
    ap.add_argument("--doc-name", default=None, help="<name> for the KML Document")
    ap.add_argument("--omit", default=None, metavar="A,B",
                    help="drop these columns from the default set "
                         "(the inverse of --fields; can't use both)")
    ap.add_argument("--filter", action="append", default=None, metavar="F=V",
                    help="keep only rows where field=value. Repeatable, and "
                         "accepts comma-separated pairs. Use != for 'not' and "
                         "~= for 'contains'. Matches form fields and address "
                         "components (incl. aliases: region->state, zip->postal).")
    ap.add_argument("--split-by", nargs="?", const="Category", default=None,
                    metavar="LABEL",
                    help="also write one CSV+KML per distinct value of LABEL "
                         "(default LABEL: Category) — one file per My Maps layer")
    args = ap.parse_args()

    api_key = os.environ.get("JOTFORM_API_KEY", "").strip()
    if not api_key:
        raise SystemExit("JOTFORM_API_KEY is not set.")
    if not args.form:
        raise SystemExit("No form ID. Pass --form or set JOTFORM_FORM_ID.")

    log(f"Fetching submissions for form {args.form}…")
    raw = fetch_submissions(args.form, api_key)
    log(f"  {len(raw)} submission(s) returned")

    live = [s for s in raw if (s.get("status") or "ACTIVE").upper() in LIVE_STATUSES]
    if len(live) != len(raw):
        log(f"  skipped {len(raw) - len(live)} non-active (trashed/quota) submission(s)")

    rows, labels = flatten(live)
    if not rows:
        log("Nothing to export.")

    rows, _ = apply_verification_gate(rows, labels, args)

    # Detect the widget column BEFORE filtering. A filter can legitimately
    # leave only rows with no address, and detection would then find nothing.
    address_label = args.address_field or A.detect_address_key(rows, labels)

    if args.filter:
        rows = apply_filters(rows, parse_filters(args.filter, labels),
                             address_label)
    if rows and not address_label:
        raise SystemExit(
            "Couldn't find the address widget column.\n"
            f"  available: {', '.join(labels)}\n"
            "Pass --address-field explicitly.")
    if address_label:
        log(f"Address field: {address_label!r}")

    os.makedirs(args.out_dir, exist_ok=True)
    csv_path = os.path.join(args.out_dir, args.basename + ".csv")
    kml_path = os.path.join(args.out_dir, args.basename + ".kml")

    out_columns, component_names, drop = resolve_selection(
        labels, address_label, args)
    if args.fields or args.omit:
        log(f"Columns: {', '.join(out_columns)}")

    # A column that's been dropped can't also title or describe a placemark.
    name_key = find_label(labels, NAME_HINTS)
    desc_key = find_label(labels, DESC_HINTS)
    category_key = find_label(labels, CATEGORY_HINTS)
    if name_key and name_key.lower() in drop:
        name_key = None
    if desc_key and desc_key.lower() in drop:
        desc_key = None

    write_csv(csv_path, rows, out_columns, address_label, component_names)
    log(f"Wrote {csv_path} ({len(rows)} row(s))")

    skipped = []
    kml = A.build_kml(
        rows, address_label, args.doc_name or args.basename,
        name_key=name_key, desc_key=desc_key, category_key=category_key,
        skipped=skipped, drop=drop)
    with open(kml_path, "w", encoding="utf-8") as f:
        f.write(kml)
    log(f"Wrote {kml_path} ({len(rows) - len(skipped)} placemark(s))")

    if skipped:
        log(f"  {len(skipped)} row(s) had no usable coordinates:")
        for i, label in skipped:
            log(f"    {label}")

    if args.split_by:
        write_split(rows, labels, address_label, out_columns, component_names, args,
                    name_key=name_key, desc_key=desc_key,
                    category_key=category_key, drop=drop)


if __name__ == "__main__":
    main()
