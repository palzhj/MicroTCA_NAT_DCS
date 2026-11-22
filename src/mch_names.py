#!/usr/bin/env python3
"""Resolve a crate name (CD1) to its MCH IP from crates/crate-aliases.json.

One file to edit when a crate moves or is added:

    {"crates": [{"name": "CD1", "ip": "192.0.2.32", ...}, ...]}

Used by scan_mch.py and mch_exporter.py, so `--mch CD1` works without touching
/etc/hosts. Lookup is case-insensitive. Override the file with MCH_INVENTORY.

    python3 mch_names.py            # print the table
    python3 mch_names.py CD1        # resolve one name
"""
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # src/..
DEFAULT = os.path.join(ROOT, "crates", "crate-aliases.json")
IPV4 = re.compile(r"^\d{1,3}(?:\.\d{1,3}){3}$")


def inventory_path():
    return os.environ.get("MCH_INVENTORY") or DEFAULT


def strip_comments(text):
    """Allow // and /* */ in the inventory.

    JSON has no comments, but disabling a crate by commenting it out is
    convenient -- so strip them here instead of insisting on strict JSON.
    Strings are skipped, so a "//" inside a value survives.
    """
    out, i, n = [], 0, len(text)
    in_str = escape = False
    while i < n:
        ch = text[i]
        if in_str:
            out.append(ch)
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
            i += 1
        elif ch == '"':
            in_str = True
            out.append(ch)
            i += 1
        elif text.startswith("//", i):
            j = text.find("\n", i)
            i = n if j < 0 else j
        elif text.startswith("/*", i):
            j = text.find("*/", i + 2)
            i = n if j < 0 else j + 2
        else:
            out.append(ch)
            i += 1
    return "".join(out)


BASE_PORT = int(os.environ.get("MCH_PORT_BASE", "9290"))


def assign_ports(crates):
    """The exporter's HTTP port is derived, not configured: 9290 + the trailing
    number in the crate name (CD1 -> 9291, TVT -> 9290). Collisions are bumped
    in file order, so the same list always maps to the same ports."""
    used = set()
    for crate in crates:
        name = str(crate.get("name", ""))
        digits = re.search(r"(\d+)$", name)
        port = BASE_PORT + int(digits.group(1)) if digits else BASE_PORT
        while port in used or port > 65535:
            port += 1
        used.add(port)
        crate["port"] = port
    return crates


def load(path=None):
    p = path or inventory_path()
    try:
        with open(p) as fh:
            return assign_ports(json.loads(strip_comments(fh.read()))["crates"])
    except OSError as exc:
        if not p.endswith("crate-aliases.json"):
            print("mch_names: cannot read %s (%s)" % (p, exc), file=sys.stderr)
        else:
            print("mch_names: %s is missing -- copy the example and edit it:\n"
                  "    cp crates/crate-aliases.example.json crates/crate-aliases.json"
                  % p, file=sys.stderr)
        return []
    except (KeyError, ValueError) as exc:
        print("mch_names: %s is not valid (%s)" % (p, exc), file=sys.stderr)
        return []


def resolve(name):
    """CD1 -> 192.0.2.32. Anything that is not a known name is passed through,
    so a literal IP (or a name the OS can resolve) still works."""
    if not name or IPV4.match(name):
        return name
    for crate in load():
        if str(crate.get("name", "")).lower() == name.lower():
            return crate["ip"]
    return name


def describe(name):
    """Human-readable 'CD1 (192.0.2.32)' for startup lines."""
    ip = resolve(name)
    return name if ip == name else "%s (%s)" % (name, ip)


def main():
    if len(sys.argv) > 1:
        print(resolve(sys.argv[1]))
        return 0
    crates = load()
    if not crates:
        print("no crates in %s" % inventory_path())
        return 1
    print("%-8s %-16s %-8s %s" % ("name", "ip", "port", "site"))
    for c in crates:
        print("%-8s %-16s %-8s %s" % (c["name"], c["ip"],
                                      c.get("port", ""), c.get("site", "")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
