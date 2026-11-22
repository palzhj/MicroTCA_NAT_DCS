#!/usr/bin/env python3
"""Scan the monitoring items of a MicroTCA crate: the NAT-MCH and every AMC.

Enumerates, in order:

  1. the MCH itself                (`ipmitool mc info`)
  2. the shelf's shared FRUs       (cooling units, power module, MCH modules)
  3. every populated AMC's MMC     (bridged to `0x70 + 2*slot` on bus 0)

and prints the sensors grouped by FRU, with a classification per value
(temperature / voltage / current / fan / power / discrete state).

Read-only: only `mc info`, `sdr elist` and `sdr list fru` are issued.

Pacing matters -- the MCH has one IPMB serialiser and a busy bus is what wedges
it (see the `ipmiMsgSender(...): ... result -9` / `no ACK` console messages).
So the scan is sequential, spaces calls by `--delay` (default 1 s), never
retries, and skips empty slots instead of probing all twelve.

usage:
    scan_mch.py                        # default MCH, human report
    scan_mch.py --json                  # machine-readable
    scan_mch.py --delay 2 --all-slots   # probe every AMC slot, slowly
    scan_mch.py --out crate-$(date +%F).json --json
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time

try:
    from mch_names import resolve as resolve_mch, describe as describe_mch
except ImportError:                     # tool kept standalone on purpose
    def resolve_mch(name):
        return name

    def describe_mch(name):
        return name

MCH_DEFAULT = "192.168.1.41"
TRANSIT = "0x82"          # MCH's IPMB-L controller, as used by the AMC tools
MIN_DELAY = 0.5           # seconds between IPMI calls

# "HS 016 AMC12" -> FRU 16, slot 12.  A slot is populated when its hot-swap
# sensor reports state M4 (activated); M0 means no module.
RE_HS_AMC = re.compile(r"^HS\s+(\d+)\s+AMC(\d+)$")
RE_HS_FRU = re.compile(r"^HS\s+\d+\s+(AMC\d+|CU\d+|PM\d+)$")
RE_HEX = re.compile(r"^0x[0-9a-fA-F]+$")

# Entity ID (first component of the "193.108" pair) -> what it is.
ENTITY = {
    7: "Shelf",
    10: "Power module",
    30: "Cooling unit",
    193: "AMC",
    194: "MCH module",
}

# Short prefixes for the bridge's display names.
SHORT = {7: "Shelf", 10: "PM", 30: "CU", 193: "AMC", 194: "MCH"}

# Legacy psensor type bits (see abandoned/psensor). The exporter ignores
# this field; it is kept so existing configs stay loadable.
PSENSOR_TYPE = {"temperature": 0x1, "fan": 0x2}

UNIT_KIND = [
    ("degrees C", "temperature"),
    ("Volts", "voltage"),
    ("Amps", "current"),
    ("RPM", "fan"),
    ("Watts", "power"),
    ("Hz", "frequency"),
]


def classify(value):
    """Map an `sdr elist` value string to (kind, numeric value or None)."""
    v = value.strip()
    for suffix, kind in UNIT_KIND:
        if v.endswith(suffix):
            num = v[: -len(suffix)].strip()
            try:
                return kind, float(num)
            except ValueError:
                return kind, None
    if RE_HEX.match(v):
        return "discrete", int(v, 16)
    try:
        return "discrete", float(v)
    except ValueError:
        return "state", None


def ipmi(mch, args, target=None, timeout=90, sdr_cache=None):
    """Run one ipmitool command. target = (address, bus) for a bridged AMC.

    `sdr_cache` (-S) is only applied to the MCH itself: a bridged AMC has its
    own SDR repository, so feeding it the MCH's cache would mislabel sensors.
    """
    cmd = ["ipmitool", "-I", "lan", "-H", mch, "-A", "none"]
    if target:
        cmd += ["-T", TRANSIT, "-B", "0", "-t", target[0], "-b", target[1]]
    elif sdr_cache:
        cmd += ["-S", sdr_cache]
    cmd += args
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout, p.stderr.strip()
    except subprocess.TimeoutExpired:
        return -1, "", "timeout after %ds" % timeout


def parse_elist(out):
    """`name | sensor_id | status | entity | value` -> list of sensor dicts."""
    sensors = []
    for line in out.splitlines():
        parts = [p.strip() for p in line.split("|")]
        if len(parts) != 5:
            continue
        name, sid, status, entity, value = parts
        kind, num = classify(value)
        try:
            ent = int(entity.split(".")[0])
        except ValueError:
            ent = None
        sensors.append({
            "name": name,
            "sensor_id": sid,
            "status": status,
            "entity": entity,
            "entity_id": ent,
            "value": value,
            "kind": kind,
            "numeric": num,
        })
    return sensors


def mc_info(out):
    """Pick the fields worth keeping out of `mc info`."""
    keep = {}
    for line in out.splitlines():
        k, _, v = line.partition(":")
        k, v = k.strip(), v.strip()
        if k in ("Manufacturer Name", "Product Name", "Firmware Revision",
                 "IPMI Version", "Device ID"):
            keep[k] = v
    return keep


def group(sensors):
    """Group sorted sensors by their entity, preserving discovery order."""
    groups = []
    index = {}
    for s in sensors:
        key = s["entity"]
        if key not in index:
            ent = s["entity_id"]
            base = ENTITY.get(ent, "Entity %s" % ent) if ent else "Unassigned"
            # Several FRUs share an entity ID (two CUs, three MCH modules),
            # so the entity pair is what tells them apart.
            index[key] = {"entity": key, "label": "%s (%s)" % (base, key),
                          "sensors": []}
            groups.append(index[key])
        index[key]["sensors"].append(s)
    return groups


def brief(value, limit=22):
    """Keep long state strings from pushing the columns out of alignment."""
    value = value.strip()
    return value if len(value) <= limit else value[: limit - 1] + "\u2026"


def scan(mch, delay, all_slots=False, sdr_cache=None, timeout=90,
         no_amc=False, expect_count=None):
    result = {"mch_address": mch, "scanned_at": int(time.time()),
              "mch": {}, "amc_slots": [], "errors": [], "sdr_count": None,
              "sdr_cache": sdr_cache}

    rc, out, err = ipmi(mch, ["mc", "info"], timeout=timeout)
    if rc != 0:
        result["errors"].append("MCH unreachable: %s" % (err or "rc=%d" % rc))
        return result
    result["mch"] = mc_info(out)
    time.sleep(delay)

    # Record count tells us how expensive a repository walk is here: on a
    # 474-record crate over a 46 ms link every walk costs ~74 s.
    rc, out, err = ipmi(mch, ["sdr", "info"], timeout=timeout)
    if rc == 0:
        for line in out.splitlines():
            if line.startswith("Record Count"):
                try:
                    result["sdr_count"] = int(line.split(":")[1].strip())
                except (IndexError, ValueError):
                    pass
    time.sleep(delay)

    # A cache dumped when the repository was smaller is worse than no cache:
    # ids still parse, but they resolve to different sensors. Refresh it.
    if (sdr_cache and os.path.exists(sdr_cache) and expect_count
            and result["sdr_count"] and expect_count != result["sdr_count"]):
        print("SDR repository changed (%s -> %s records): refreshing the cache"
              % (expect_count, result["sdr_count"]), file=sys.stderr, flush=True)
        os.remove(sdr_cache)

    if sdr_cache and not os.path.exists(sdr_cache):
        print("dumping the SDR repository to %s (one walk, may take minutes "
              "on a large crate) ..." % sdr_cache, file=sys.stderr, flush=True)
        rc, out, err = ipmi(mch, ["sdr", "dump", sdr_cache],
                            timeout=max(timeout, 600))
        if rc != 0 or not os.path.exists(sdr_cache):
            print("sdr dump failed: %s" % (err or "rc=%d" % rc), file=sys.stderr)
            sdr_cache = None
            result["sdr_cache"] = None
        time.sleep(delay)

    rc, out, err = ipmi(mch, ["sdr", "elist"], timeout=timeout,
                        sdr_cache=sdr_cache)
    shelf = parse_elist(out) if rc == 0 else []
    if rc != 0:
        result["errors"].append("MCH sdr elist failed: %s" % (err or "rc=%d" % rc))
    result["shelf_groups"] = group(shelf)
    result["shelf_sensor_count"] = len(shelf)

    # Which AMC slots are populated, from the hot-swap sensors.
    present = {}
    for s in shelf:
        m = RE_HS_AMC.match(s["name"])
        if m:
            present[int(m.group(2))] = {
                "fru_id": int(m.group(1)),
                "state": s["value"],
                "status": s["status"],
                "activated": "M4" in s["value"],
            }
    result["hotswap"] = present

    # --no-amc: the MCH already mirrors each AMC's sensors in its own SDR
    # repository (27 per AMC on the 12-slot crate), so a shelf-only sweep is
    # enough for a status view and costs a fraction of bridging into 12 MMCs.
    if no_amc:
        slots = []
    elif all_slots:
        slots = list(range(1, 13))
    else:
        slots = sorted(present)
    for slot in slots:
        time.sleep(delay)
        info = present.get(slot, {})
        entry = {
            "slot": slot,
            "fru_id": info.get("fru_id"),
            "address": "0x%02x" % (0x70 + 2 * slot),
            "hotswap_state": info.get("state"),
            "reachable": False,
            "mmc": {},
            "sensors": [],
        }
        if info and not info.get("activated"):
            entry["note"] = "not activated (%s)" % info.get("state")
            result["amc_slots"].append(entry)
            continue

        target = ("0x%02x" % (0x70 + 2 * slot), "0")
        rc, out, err = ipmi(mch, ["mc", "info"], target=target, timeout=60)
        if rc != 0:
            # 0xd3 "destination unavailable" is definitive: do not spend a
            # second request reading SDRs from a slot that has no MMC.
            entry["error"] = " ".join((err or "rc=%d" % rc).split())
            result["amc_slots"].append(entry)
            continue
        entry["mmc"] = mc_info(out)
        time.sleep(delay)
        rc, out, err = ipmi(mch, ["sdr", "elist"], target=target)
        if rc == 0:
            entry["reachable"] = True
            entry["sensors"] = parse_elist(out)
            # A device-SDR-only MMC reports its count here; 0/1 means it is
            # exposing almost nothing, which is worth flagging.
            time.sleep(delay)
            rc2, out2, _ = ipmi(mch, ["raw", "0x04", "0x20"], target=target, timeout=60)
            if rc2 == 0 and out2.strip():
                try:
                    entry["device_sdr_count"] = int(out2.split()[0], 16)
                except (ValueError, IndexError):
                    pass
        else:
            entry["error"] = " ".join((err or "rc=%d" % rc).split())
        result["amc_slots"].append(entry)

    return result


def build_config(result):
    """Turn a scan into the config consumed by mch_exporter.py.

    The config names every monitoring item *and* the target each one lives on,
    so the bridge no longer has to rediscover the crate: it sweeps exactly
    these targets and keeps exactly these sensors.

    Sensors are identified by `sensor_id` (the SDR id, e.g. "65h"), which is
    stable across sweeps, rather than by name -- names repeat between FRUs.
    """
    targets = [{"id": "shelf", "kind": "mch", "label": "shelf"}]
    sensors = []

    # Map an AMC's entity instance to its slot number.  The hot-swap sensor
    # name carries both ("HS 016 AMC12" -> entity 193.108, slot 12), which is
    # the only place the slot appears; the MCH's mirrored sensors are named
    # "TEMP UCD", "AMC +12V", ... without it.
    entity_slot = {}
    entity_fru = {}
    for g in result["shelf_groups"]:
        for s in g["sensors"]:
            m = RE_HS_AMC.match(s["name"])
            if m:
                entity_slot[s["entity"]] = int(m.group(2))
            # "HS 040 CU1" / "HS 051 PM2" name the FRU holding that entity;
            # without it both cooling units and both power modules produce
            # identical sensor names and the legend cannot tell them apart.
            fm = RE_HS_FRU.match(s["name"])
            if fm:
                entity_fru[s["entity"]] = fm.group(1)
    # Sensors reported by a reachable AMC MMC are authoritative; the MCH keeps
    # copies of a few of them (hot-swap, slot clock), so drop those duplicates.
    mirrored = set()
    for entry in result["amc_slots"]:
        if not entry["reachable"]:
            continue
        label = "AMC%d" % entry["slot"]
        targets.append({
            "id": "amc%d" % entry["slot"], "kind": "amc", "label": label,
            "slot": entry["slot"], "address": entry["address"], "bus": "0",
        })
        for s in entry["sensors"]:
            mirrored.add((s["entity"], s["name"]))
            sensors.append(_config_sensor(s, "amc%d" % entry["slot"],
                                          "%s %s" % (label, s["name"]),
                                          "amc%d-%s-%s" % (entry["slot"],
                                                           s["entity"], s["sensor_id"]),
                                          amc=entry["slot"]))

    for g in result["shelf_groups"]:
        for s in g["sensors"]:
            if (s["entity"], s["name"]) in mirrored:
                continue
            slot = entity_slot.get(s["entity"])
            fru = entity_fru.get(s["entity"])
            if slot is not None:
                # "AMC FPGA +1.2VA Curr" on AMC6 -> "AMC6 FPGA +1.2VA Curr":
                # the slot goes in front and the redundant prefix comes off,
                # so the legend needs no [AMC6] suffix and stays unique.
                rail = s["name"]
                if rail.upper().startswith("AMC "):
                    rail = rail[4:]
                name = "AMC%d %s" % (slot, rail)
            elif fru:
                rail = s["name"]
                if rail.upper().startswith(fru.upper() + " "):
                    rail = rail[len(fru) + 1:]
                name = "%s %s" % (fru, rail)
            else:
                prefix = SHORT.get(s["entity_id"], "")
                name = s["name"] if not prefix or s["name"].startswith(prefix) \
                    else "%s %s" % (prefix, s["name"])
            sensors.append(_config_sensor(
                s, "shelf", name, "mch-%s-%s" % (s["entity"], s["sensor_id"]),
                amc=entity_slot.get(s["entity"])))

    return {
        "version": 1,
        "mch": result["mch_address"],
        "generated_at": result["scanned_at"],
        "generated_from": "scan_mch.py",
        "sdr_cache": result.get("sdr_cache"),
        # the repository size the config was built against; the exporter
        # compares this with the crate on every start (a stale cache maps
        # sensor ids to the wrong sensors -- silently wrong values)
        "sdr_count": result.get("sdr_count"),
        "targets": targets,
        "sensors": sensors,
    }


def _config_sensor(sensor, target, display, sid, amc=None):
    psensor_type = PSENSOR_TYPE.get(sensor["kind"])
    entry = {
        "id": sid,
        "name": display,
        "kind": sensor["kind"],
        "type": psensor_type,
        "target": target,
        "sensor_id": sensor["sensor_id"],
        "sensor_name": sensor["name"],
        "entity": sensor["entity"],
    }
    if amc is not None:
        entry["amc"] = amc
    return entry


def render(result):
    lines = []
    lines.append("MicroTCA crate scan -- %s" % result["mch_address"])
    mch = result["mch"]
    if mch:
        lines.append("MCH  %s %s  IPMI %s" % (
            mch.get("Product Name", "?"), mch.get("Firmware Revision", "?"),
            mch.get("IPMI Version", "?")))
    lines.append("scanned %s" % time.strftime("%Y-%m-%d %H:%M:%S",
                                              time.localtime(result["scanned_at"])))
    if result["errors"]:
        for e in result["errors"]:
            lines.append("ERROR  %s" % e)
        return "\n".join(lines)

    lines.append("")
    lines.append("Shelf / shared FRUs -- %d sensors" % result["shelf_sensor_count"])
    for g in result["shelf_groups"]:
        lines.append("  [%s]" % g["label"])
        for s in g["sensors"]:
            flag = "" if s["status"] == "ok" else "  <- %s" % s["status"]
            lines.append(("    %-22s %-22s %-12s %s" % (
                s["name"], brief(s["value"]), s["kind"], flag)).rstrip())

    for entry in result["amc_slots"]:
        lines.append("")
        head = "AMC%d (slot %d, IPMB 0x%02x)" % (
            entry["slot"], entry["slot"], int(entry["address"], 16))
        if entry.get("note"):
            lines.append("%s -- %s" % (head, entry["note"]))
            continue
        if not entry["reachable"]:
            lines.append("%s -- UNREACHABLE: %s" % (head, entry.get("error", "")))
            continue
        mmc = entry["mmc"]
        lines.append("%s -- MMC fw %s, IPMI %s" % (
            head, mmc.get("Firmware Revision", "?"), mmc.get("IPMI Version", "?")))
        lines.append("  %d sensor(s)" % len(entry["sensors"]))
        for s in entry["sensors"]:
            flag = "" if s["status"] == "ok" else "  <- %s" % s["status"]
            lines.append(("    %-22s %-22s %-12s %s" % (
                s["name"], brief(s["value"]), s["kind"], flag)).rstrip())
        # A healthy MMC exports tens of sensors. Only two (or fewer) means the
        # device-SDR side is barely populated -- that is the case worth noting.
        if len(entry["sensors"]) <= 2:
            lines.append("  note: only %d sensor(s) exported (MMC advertises %s "
                         "device SDR(s)); its own rails may not be reachable"
                         % (len(entry["sensors"]),
                            entry.get("device_sdr_count", "?")))

    shelf_all = [s for g in result["shelf_groups"] for s in g["sensors"]]
    amc_all = [s for e in result["amc_slots"] for s in e["sensors"]]
    counts = {}
    for s in shelf_all + amc_all:
        counts[s["kind"]] = counts.get(s["kind"], 0) + 1
    order = ["temperature", "voltage", "current", "power", "fan", "frequency",
             "discrete", "state"]
    spread = ", ".join("%d %s" % (counts[k], k) for k in order if k in counts)
    reachable = sum(1 for e in result["amc_slots"] if e["reachable"])

    lines.append("")
    lines.append("Inventory -- %d monitoring items: %d from the shelf, %d from %d AMC(s)"
                 % (len(shelf_all) + len(amc_all), len(shelf_all),
                    len(amc_all), reachable))
    if spread:
        lines.append("  " + spread)
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description="Scan MicroTCA crate monitoring items")
    ap.add_argument("--mch", default=MCH_DEFAULT)
    ap.add_argument("--delay", type=float, default=1.0, metavar="SEC",
                    help="pause between IPMI calls (default: 1, floor: %g)" % MIN_DELAY)
    ap.add_argument("--no-amc", action="store_true",
                    help="shelf only: rely on the MCH's mirror of the AMC "
                         "sensors instead of bridging into each MMC")
    ap.add_argument("--all-slots", action="store_true",
                    help="probe AMC1..12 even when the hot-swap sensor says empty")
    ap.add_argument("--json", action="store_true", help="emit JSON instead of text")
    ap.add_argument("--out", metavar="FILE", help="also write the report to FILE")
    ap.add_argument("--config", metavar="FILE",
                    help="write the exporter config (input of mch_exporter.py)")
    ap.add_argument("--sdr-cache", metavar="FILE",
                    help="cache the MCH's SDR repository here (-S); created with "
                         "one `sdr dump` if missing. Use on large/slow crates.")
    ap.add_argument("--timeout", type=float, default=90.0, metavar="SEC",
                    help="per-IPMI-call timeout (default: 90)")
    args = ap.parse_args()

    if args.delay < MIN_DELAY:
        print("refusing --delay=%g: keep at least %gs between IPMI calls"
              % (args.delay, MIN_DELAY), file=sys.stderr)
        args.delay = MIN_DELAY

    crate_name = args.mch                 # keep the alias, resolve for I/O only
    args.mch = resolve_mch(crate_name)
    expect = None
    if args.config and os.path.exists(args.config):
        try:
            with open(args.config) as fh:
                expect = json.load(fh).get("sdr_count")
        except (OSError, ValueError):
            expect = None

    result = scan(args.mch, args.delay, args.all_slots,
                  sdr_cache=args.sdr_cache, timeout=int(args.timeout),
                  no_amc=args.no_amc, expect_count=expect)
    if result.get("sdr_count") and result["sdr_count"] > 200:
        print("note: this crate has %d SDR records; without --sdr-cache every "
              "walk costs one round trip per record" % result["sdr_count"],
              file=sys.stderr)
    text = json.dumps(result, indent=2) if args.json else render(result)
    print(text)
    if args.out:
        with open(args.out, "w") as fh:
            fh.write(text + "\n")
        print("written to %s" % args.out, file=sys.stderr)
    if args.config:
        if result["errors"]:
            print("not writing %s: the scan failed" % args.config, file=sys.stderr)
            return 1
        result["mch_address"] = crate_name   # config keeps "CD1", not the IP
        cfg = build_config(result)
        # a cache sitting next to the config is stored by name, so the config
        # stays valid if the directory moves
        if cfg.get("sdr_cache") and args.config:
            cache_abs = os.path.abspath(cfg["sdr_cache"])
            cfg_dir = os.path.dirname(os.path.abspath(args.config))
            if os.path.dirname(cache_abs) == cfg_dir:
                cfg["sdr_cache"] = os.path.basename(cache_abs)
        with open(args.config, "w") as fh:
            json.dump(cfg, fh, indent=2)
            fh.write("\n")
        exportable = sum(1 for s in cfg["sensors"] if s["type"] is not None)
        print("config written to %s: %d sensors on %d target(s), %d with a legacy psensor type"
              % (args.config, len(cfg["sensors"]), len(cfg["targets"]), exportable),
              file=sys.stderr)
    return 0 if not result["errors"] else 1


if __name__ == "__main__":
    sys.exit(main())
