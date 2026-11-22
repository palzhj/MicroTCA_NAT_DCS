#!/usr/bin/env python3
"""Prometheus exporter for MicroTCA crates, driven by an `scan_mch.py` config.

Why this exists instead of ipmi_exporter: the NAT-MCH here only answers
`ipmitool -I lan -A none`. FreeIPMI (`ipmi_exporter`'s engine) fails to open a
session against it -- "session timeout" for every driver/auth/privilege
combination tried, while TCP and ipmitool work fine. So the sweep path proven
against these crates is reused and the result is rendered as Prometheus text.

    python3 mch_exporter.py --config crate.json --port 9290
    curl -s http://127.0.0.1:9290/metrics | head

Same pacing rules as the bridge it replaced (abandoned/psensor): one bulk
sweep per target per cycle,
a measured `sweep + --gap` floor, exponential backoff, and an SDR cache so the
repository is not re-walked on every scrape.

Metrics:

    mch_sensor_value{id,name,orig,display,kind,target,entity,amc}
                                                    reading (temperatures C,
                                                    volts, amps, RPM, watts);
                                                    name = alias, orig = the
                                                    MCH's own sensor name,
                                                    amc="0" = not an AMC
    mch_sensor_status{id,name,kind,target,entity}   1 = ok, 0 = any other state
    mch_sweep_success                               1 = last sweep worked
    mch_sweep_duration_seconds                      wall clock of the last sweep
    mch_target_sweep_duration_seconds{target}
    mch_sensors_reported                            sensors in the last sweep
"""
import argparse
import collections
import json
import os
import random
import re
import socket
import subprocess
import sys
import threading
import time

try:
    from mch_names import resolve as resolve_mch, describe as describe_mch
except ImportError:
    def resolve_mch(name):
        return name

    def describe_mch(name):
        return name
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MIN_INTERVAL = 5.0
DEFAULT_GAP = 5.0
TRANSIT = "0x82"
TARGET_TIMEOUT = 60      # seconds per target; one dead MMC must not stall a sweep
TARGET_GAP = 0.5

RE_TEMP = re.compile(r"^(-?[\d.]+) degrees C$")
RE_RPM = re.compile(r"^(\d+) RPM$")
RE_VOLT = re.compile(r"^(-?[\d.]+) Volts$")
RE_AMP = re.compile(r"^(-?[\d.]+) Amps$")
RE_WATT = re.compile(r"^(-?[\d.]+) Watts$")

UNIT_KIND = [
    (RE_TEMP, "temperature"), (RE_RPM, "fan"), (RE_VOLT, "voltage"),
    (RE_AMP, "current"), (RE_WATT, "power"),
]

# Sum sensors can be mis-scaled (TVT PM2 reads 80.8 A while its own channels
# add up to 21.4 A). When that happens the channel total is the truth: replace
# the value and remember it so /metrics can expose the mismatch.
sum_mismatch = {}

lock = threading.Lock()
readings = {}       # id -> dict
seen_ids = set()    # stable ids already reported once
stats = {"success": 0.0, "duration": 0.0, "reported": 0, "poll_errors": 0,
         "last_poll": 0, "effective_interval": None, "config": None}
target_durations = {}


# PM channel -> what it feeds. ch01..ch04 are the shelf's own FRUs, ch05..ch16
# are the twelve AMC slots, so a legend can say "AMC1" instead of "Ch05".
PM_CHANNELS = {1: "MCH1", 2: "MCH2", 3: "CU1", 4: "CU2"}
for _slot in range(1, 13):
    PM_CHANNELS[4 + _slot] = "AMC%d" % _slot
RE_PM_CHANNEL = re.compile(r"^Ch(\d+) Current$")


def channel_load(orig):
    """'Ch05 Current' -> 'AMC1'; None when it is not a PM channel."""
    m = RE_PM_CHANNEL.match(orig)
    return PM_CHANNELS.get(int(m.group(1))) if m else None


def pretty(text):
    """Presentation form of a sensor name: no "+", no doubled spaces.
    "DDR +1.2VB" -> "DDR 1.2VB". `orig` keeps the vendor spelling."""
    return " ".join(str(text).replace("+", "").split())


def esc(value):
    return str(value).replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


def parse_elist(out):
    rows = {}
    for line in out.splitlines():
        parts = [p.strip() for p in line.split("|")]
        if len(parts) != 5:
            continue
        name, sid, status, entity, value = parts
        rows[(entity, sid)] = {"name": name, "sensor_id": sid,
                               "status": status, "entity": entity, "value": value}
    return rows


def numeric(value):
    for regex, kind in UNIT_KIND:
        m = regex.match(value.strip())
        if m:
            return kind, float(m.group(1))
    return None, None


def sweep_cmd(mch, target, sdr_cache):
    cmd = ["ipmitool", "-I", "lan", "-H", mch, "-A", "none"]
    if target["kind"] != "mch":
        cmd += ["-T", TRANSIT, "-B", "0", "-t", target["address"],
                "-b", str(target.get("bus", "0"))]
    elif sdr_cache:
        cmd += ["-S", sdr_cache]
    return cmd + ["sdr", "elist"]


def sweep(mch, cfg, sdr_cache, crate_name=None, fec_lookup=None):
    """Sweep every target; return {stable_id: reading}, success flag, duration.

    The sensor set comes from the sweep, not from the config: a hot-swapped AMC
    renumbers the SDR ids, so anything cached by id would silently point at the
    wrong sensor.  Identity is therefore (entity, the MMC's own sensor name),
    which survives a swap; the config only supplies nicer names and the AMC
    number.
    """
    started = time.time()
    ok = True
    found = {}
    # keyed by target as well: every AMC MMC uses the same entity (192.0),
    # so (entity, name) alone would keep only the last module's hint
    hints = {(s["target"], s["entity"], s["sensor_name"]): s
             for s in cfg["sensors"]}
    # The MCH mirrors AMC sensors, so a shelf row can match an AMC hint. Keep a
    # target-less index for that case: it gives the mirrored row its real AMC
    # number instead of the guess below.
    any_hint = {}
    for s in cfg["sensors"]:
        any_hint.setdefault((s["entity"], s["sensor_name"]), s)
    # The MCH mirrors an AMC's rails and the AMC's own MMC reports them again,
    # so the same physical sensor arrives twice. Key by what the rail *is*
    # (amc, pair, kind) and let the MMC copy win -- it carries the nicer alias.
    rail_index = {}
    # AMC slots we bridge into: their MMC is authoritative, so the MCH's
    # mirrored copy of the same rails is skipped rather than kept-then-dropped
    bridged = {t.get("slot") for t in cfg["targets"] if t.get("kind") == "amc"}
    taken_display = {}
    # Every AMC MMC shares entity 192.0, so "last one wins" would label a shelf
    # row as the highest slot. Drop any entity that maps to more than one AMC.
    entity_amc, ambiguous = {}, set()
    for s in cfg["sensors"]:
        amc_of = s.get("amc")
        if not amc_of:
            continue
        ent = s["entity"]
        if ent in entity_amc and entity_amc[ent] != amc_of:
            ambiguous.add(ent)
        else:
            entity_amc[ent] = amc_of
    for ent in ambiguous:
        entity_amc.pop(ent, None)

    for i, target in enumerate(cfg["targets"]):
        if i:
            time.sleep(TARGET_GAP)
        t0 = time.time()
        try:
            p = subprocess.run(sweep_cmd(mch, target, sdr_cache),
                               capture_output=True, text=True, timeout=TARGET_TIMEOUT)
            if p.returncode != 0:
                raise RuntimeError(" ".join((p.stderr or p.stdout).split())[:200])
            rows = parse_elist(p.stdout)
        except Exception as exc:
            ok = False
            with lock:
                stats["poll_errors"] += 1
            print("sweep of %s failed: %s" % (target["id"], exc),
                  file=sys.stderr, flush=True)
            continue
        target_durations[target["id"]] = time.time() - t0

        for (entity, sensor_id), row in rows.items():
            kind, value = numeric(row["value"])
            state_text = ""
            if kind is None:
                # discrete/state sensor (HOTSWAP AMC, HS <slot>, ...): no numeric
                # value, but the text is the module state -- "Module Handle
                # Closed" is the AMC's M-state bit, so keep it.
                kind, value = "state", None
                state_text = row["value"]
            orig = row["name"]
            hint = hints.get((target["id"], entity, orig))
            if hint is None and target["id"] == "shelf":
                hint = any_hint.get((entity, orig))     # mirrored AMC sensor
            alias = hint["name"] if hint else orig
            amc = (hint.get("amc") if hint else None)
            if amc is None:
                amc = entity_amc.get(entity, 0) if target["id"] == "shelf" else 0
            amc = int(amc)
            # An AMC MMC reports all its sensors under one entity (192.0), so
            # the target must be part of the identity or twelve modules collapse
            # into a single set. The shelf view keeps its old ids (no churn).
            if target["id"] == "shelf":
                stable_id = "%s/%s" % (entity, orig)
            else:
                stable_id = "%s/%s/%s" % (target["id"], entity, orig)
            # Rails come in pairs: a voltage "IO +3.3V" and its current
            # "IO +3.3V Curr". Expose the common part so a dashboard can plot
            # both without regex gymnastics.
            pair = orig[:-5] if kind == "current" and orig.endswith(" Curr") else orig
            # rail naming: no "+" in the name ...
            pair = " ".join(pair.replace("+", "").split())
            # ... and the +12 V rail is split across two sensors: its voltage
            # is "AMC +12V", its current "RTM +12V Curr". One rail, one name.
            if pair == "AMC 12V":
                pair = "RTM 12V"

            # Drop the MCH's mirrored copy BEFORE naming: it is a duplicate of
            # the MMC row, and letting it reserve the display would push the
            # real one into the "[entity]" disambiguation. State sensors are
            # exempt: "HS 015 AMC11" carries the FRU M-state (Transition to
            # M4), which the AMC's MMC does not report at all.
            if target["id"] == "shelf" and amc in bridged and kind != "state":
                continue

            # Only AMC rails can arrive twice (MCH mirror + the AMC's MMC).
            # Shelf sensors must NOT be collapsed this way: every PM reports a
            # sensor called "Current(Sum)", and dedupe by rail would keep one.
            if amc:
                rail_key = (amc, pair, kind)
                prev = rail_index.get(rail_key)
                if prev is not None:
                    if found[prev]["target"] == "shelf" and target["id"] != "shelf":
                        del found[prev]      # MMC supersedes the MCH mirror
                    else:
                        continue             # already have the better copy
                rail_index[rail_key] = stable_id

            # PM channels carry the load they feed, not just their number.
            # PM2 and PM4 are a hot-backup pair feeding the same rails, so the
            # name reads "<board> (PMn CHmm) Current":
            #   FEC23/CD1-AMC12 (PM2 CH16) Current
            #   CU1 (PM2 CH03) Current
            load = channel_load(orig) if amc == 0 else None
            display_override = None
            if load:
                pm = alias.split()[0]                     # PM2 / PM4
                ch = int(RE_PM_CHANNEL.match(orig).group(1))
                slot_of_ch = ch - 4
                if slot_of_ch >= 1:
                    fec_of_ch = ((fec_lookup(crate_name, slot_of_ch)
                                  or "AMC%d" % slot_of_ch)
                                 if fec_lookup else "AMC%d" % slot_of_ch)
                    alias = "%s/%s-AMC%d (%s CH%02d) Current" % (
                        fec_of_ch, crate_name or "?", slot_of_ch, pm, ch)
                else:
                    alias = "%s (%s CH%02d) Current" % (load, pm, ch)
                display_override = alias

            # One naming system on every page: an AMC's alias starts with its
            # slot ("AMC12 DDR +1.2VB"), the dashboards show the installation
            # name instead ("FEC23 DDR 1.2VB").
            fec_val = ((fec_lookup(crate_name, amc) or "AMC%d" % amc)
                       if fec_lookup and amc else "")
            if fec_val and alias.startswith("AMC%d" % amc):
                alias = fec_val + alias[len("AMC%d" % amc):]

            # The alias is usually "<FRU> <sensor>", so appending the sensor
            # name again just repeats it: "CU2 LM75 Temp (CD1) LM75 Temp".
            # Only add the tail when the alias does not already carry it.
            shown = pretty(alias)
            core = shown if not crate_name else "%s (%s)" % (shown, crate_name)
            if display_override:
                display = display_override          # crate is already in the name
            elif not orig or orig.lower() in alias.lower():
                display = core
            else:
                display = "%s %s" % (core, pretty(orig))
            # Two MCH modules report sensors with the same name, which would
            # put two different series under one legend row. Qualify the loser.
            if display in taken_display and taken_display[display] != stable_id:
                display = "%s [%s]" % (display, entity)
            taken_display[display] = stable_id

            if stable_id not in seen_ids:
                seen_ids.add(stable_id)
                if hint is None:
                    print("new sensor (%s): %s -- run scan_mch.py to give it a "
                          "friendly name" % (entity, orig), file=sys.stderr, flush=True)

            found[stable_id] = {
                "id": stable_id, "name": shown, "orig": orig, "pair": pair,
                "fec": fec_val,
                "display": display,
                "kind": kind, "target": target["id"], "entity": entity,
                "amc": amc, "sensor_id": sensor_id, "value": value,
                "state": state_text,
                "ok": 1.0 if row["status"] == "ok" else 0.0,
                "status": row["status"],
            }
    # Reconcile "Current(Sum)" against the sum of that PM's channels. The
    # channels are per-load measurements and agree with each other; the sum
    # record is the one that can be scaled wrongly.
    sums, chan = {}, collections.defaultdict(float)
    for r in found.values():
        if r["kind"] != "current" or r["amc"]:
            continue
        if r["orig"] == "Current(Sum)":
            sums[r["name"].split()[0]] = r
        else:
            m = re.search(r"\((PM[0-9]) CH", r["name"])
            if m:
                chan[m.group(1)] += r["value"] or 0.0
    fresh = {}
    for pm, row in sums.items():
        ch = chan.get(pm, 0.0)
        val = row["value"] or 0.0
        if abs(ch - val) > max(2.0, 0.2 * max(val, ch)):
            print("%s %s Current(Sum) reads %.2f A but its channels add up to "
                  "%.2f A -- using the channel total" % (crate_name, pm, val, ch),
                  file=sys.stderr, flush=True)
            row["value"] = round(ch, 2)
            fresh[pm] = (val, ch)
    with lock:
        sum_mismatch.clear()
        sum_mismatch.update(fresh)

    return found, ok, time.time() - started


def load_amc_aliases(path):
    """{"table": {crate: {slot: "FECn"}}, "fallback": {...}} -> lookup fn."""
    try:
        with open(path) as fh:
            cfg = json.load(fh)
    except (OSError, ValueError) as exc:
        print("no AMC aliases (%s): %s" % (path, exc), file=sys.stderr)
        return ((lambda crate, amc: ""), (lambda crate, amc: False),
                (lambda crate, amc: False))
    table = cfg.get("table", {})
    fb = cfg.get("fallback", {})
    prefix = fb.get("prefix", "FEC")
    per_crate = int(fb.get("per_crate", 11))

    def norm(value):
        # "NONE" (any case), "-" and "" all mean: this slot has no FEC alias.
        text = "" if value is None else str(value).strip()
        return "" if text.lower() in ("none", "n/a", "-", "") else text

    def lookup(crate, amc):
        if not crate or not amc:
            return ""
        explicit = norm(table.get(crate, {}).get(str(amc)))
        if not explicit and str(amc) in table.get(crate, {}):
            return ""            # explicitly NONE: no derivation, no warning
        if explicit:
            return explicit
        # only for slots the table does not cover, so nothing is ever blank
        digits = re.search(r"(\d+)$", crate)
        if not digits:
            return ""
        return "%s%d" % (prefix, per_crate * int(digits.group(1)) + int(amc))

    def absent(crate, amc):
        per = table.get(crate, {})
        return str(amc) in per and norm(per[str(amc)]) == ""

    return lookup, (lambda crate, amc: str(amc) in table.get(crate, {})), absent


def live_sdr_count(mch):
    """Record count from the crate, or None (one request, ~1 s)."""
    try:
        p = subprocess.run(["ipmitool", "-I", "lan", "-H", mch, "-A", "none",
                            "sdr", "info"], capture_output=True, text=True,
                           timeout=90)
        for line in p.stdout.splitlines():
            if line.startswith("Record Count"):
                return int(line.split(":")[1])
    except Exception:
        pass
    return None


def refresh_cache(mch, path):
    print("SDR repository changed -- re-dumping %s (about a minute); the next "
          "sweep will use the new layout" % path, file=sys.stderr, flush=True)
    try:
        p = subprocess.run(["ipmitool", "-I", "lan", "-H", mch, "-A", "none",
                            "sdr", "dump", path], capture_output=True, text=True,
                           timeout=900)
        return p.returncode == 0
    except Exception as exc:
        print("re-dump failed: %s" % exc, file=sys.stderr, flush=True)
        return False


def poller(mch, cfg, interval, max_interval, sdr_cache, gap,
           crate_name=None, fec_lookup=None):
    backoff = interval
    fails = 0
    last_report = None
    # A hot-swapped AMC changes the SDR repository and renumbers its ids, so a
    # cache with the old layout would map ids to the wrong sensors. Check the
    # size every 10th cycle (one ~1 s request) and re-dump when it moved.
    expected = cfg.get("sdr_count") if cfg else None
    cycles = 0
    while True:
        found, ok, took = sweep(mch, cfg, sdr_cache, crate_name, fec_lookup)
        cycles += 1
        if expected and sdr_cache and cycles % 10 == 0:
            live = live_sdr_count(mch)
            if live and live != expected:
                print("SDR repository is %d records, configured for %d"
                      % (live, expected), file=sys.stderr, flush=True)
                if refresh_cache(mch, sdr_cache):
                    expected = live
                    with lock:
                        stats["sdr_count"] = live
        with lock:
            if ok:
                # a complete sweep is authoritative: sensors that vanished are
                # really gone (hot-swap out), so replace the whole set
                readings.clear()
                readings.update(found)
            else:
                # partial sweep: keep the last good value for whatever this
                # cycle could not reach, rather than blanking the dashboard
                readings.update(found)
            stats["success"] = 1.0 if ok else 0.0
            stats["duration"] = took
            stats["reported"] = len(readings)
            stats["last_poll"] = int(time.time())

        if ok:
            backoff = interval
            fails = 0
        else:
            # A refused session usually means the MCH's table is momentarily
            # full; it ages entries out after ~60 s, so retry on that scale
            # for the first few failures before backing off properly.
            fails += 1
            backoff = 60 if fails <= 3 else min(backoff * 2, max_interval)
        floor = took + gap
        target_wait = max(backoff, floor)
        if target_wait > backoff + 0.5:
            if last_report is None or abs(target_wait - last_report) >= 5:
                print("interval raised %.0fs -> %.0fs (sweep took %.1fs + %gs gap)"
                      % (backoff, target_wait, took, gap), file=sys.stderr, flush=True)
            last_report = target_wait
        wait = max(target_wait * random.uniform(0.9, 1.1), floor)
        with lock:
            stats["effective_interval"] = round(target_wait, 1)
        print("sweep: %d sensors in %.1fs, next in %.0fs"
              % (len(found), took, wait), file=sys.stderr, flush=True)
        time.sleep(wait)


def render():
    with lock:
        snapshot = list(readings.values())
        st = dict(stats)
        durations = dict(target_durations)
    out = [
        "# HELP mch_sensor_value IPMI sensor reading in its native unit.",
        "# TYPE mch_sensor_value gauge",
    ]
    for r in snapshot:
        if r["value"] is None:
            continue
        out.append('mch_sensor_value{id="%s",name="%s",orig="%s",pair="%s",'
                   'display="%s",kind="%s",target="%s",entity="%s",amc="%s",'
                   'fec="%s"} %g'
                   % (esc(r["id"]), esc(r["name"]), esc(r["orig"]),
                      esc(r["pair"]), esc(r["display"]), esc(r["kind"]),
                      esc(r["target"]), esc(r["entity"]), esc(r["amc"]),
                      esc(r["fec"]), r["value"]))
    out += ["# HELP mch_sensor_status 1 when the sensor reports ok.",
            "# TYPE mch_sensor_status gauge"]
    for r in snapshot:
        out.append('mch_sensor_status{id="%s",name="%s",target="%s",status="%s"} %g'
                   % (esc(r["id"]), esc(r["name"]), esc(r["target"]),
                      esc(r["status"]), r["ok"]))
    out += ["# HELP mch_sensor_state_info 1 with the discrete state text of a "
            "state sensor (HOTSWAP AMC -> Module Handle Closed/Open).",
            "# TYPE mch_sensor_state_info gauge"]
    for r in snapshot:
        if r["kind"] == "state" and r["state"]:
            out.append('mch_sensor_state_info{id="%s",name="%s",target="%s",'
                       'state="%s"} 1'
                       % (esc(r["id"]), esc(r["name"]), esc(r["target"]),
                          esc(r["state"])))

    # Which signals does each rail actually report? Some rails are voltage
    # only, some current only -- a dashboard that assumes pairs shows an empty
    # chart for those.
    rails = {}
    for r in snapshot:
        key = (r["pair"], r["amc"])
        if r["kind"] == "voltage":
            rails[key] = rails.get(key, 0) | 1
        elif r["kind"] == "current":
            rails[key] = rails.get(key, 0) | 2
    out += ["# HELP mch_rail_signals Signals a rail reports: 1=voltage, 2=current, 3=both.",
            "# TYPE mch_rail_signals gauge"]
    for (pair, amc), mask in sorted(rails.items(), key=lambda kv: (kv[0][0], str(kv[0][1]))):
        out.append('mch_rail_signals{pair="%s",amc="%s"} %d'
                   % (esc(pair), esc(amc), mask))

    with lock:
        mismatched = dict(sum_mismatch)
    if mismatched:
        out += ["# HELP mch_sum_mismatch 1 when a PM's Current(Sum) disagreed "
                "with its channels and was replaced by the channel total.",
                "# TYPE mch_sum_mismatch gauge"]
        for pm, (reported, ch) in sorted(mismatched.items()):
            out.append('mch_sum_mismatch{pm="%s",reported="%.2f",channels="%.2f"} 1'
                       % (esc(pm), reported, ch))

    out += ["# HELP mch_sweep_success 1 when the last sweep completed.",
            "# TYPE mch_sweep_success gauge",
            "mch_sweep_success %g" % st["success"],
            "# HELP mch_sweep_duration_seconds Wall clock of the last sweep.",
            "# TYPE mch_sweep_duration_seconds gauge",
            "mch_sweep_duration_seconds %.3f" % st["duration"],
            "# HELP mch_target_sweep_duration_seconds Per-target sweep time.",
            "# TYPE mch_target_sweep_duration_seconds gauge"]
    for name, value in sorted(durations.items()):
        out.append('mch_target_sweep_duration_seconds{target="%s"} %.3f'
                   % (esc(name), value))
    out += ["# HELP mch_sensors_reported Sensors seen in the last sweep.",
            "# TYPE mch_sensors_reported gauge",
            "mch_sensors_reported %d" % st["reported"],
            "# HELP mch_sweep_errors_total Failed sweeps since start.",
            "# TYPE mch_sweep_errors_total counter",
            "mch_sweep_errors_total %d" % st["poll_errors"]]
    return "\n".join(out) + "\n"


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self):
        if self.path.rstrip("/") not in ("/metrics", ""):
            self.send_error(404)
            return
        data = render().encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; version=0.0.4")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):
        pass


def main():
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except AttributeError:
        pass

    ap = argparse.ArgumentParser(description="MicroTCA IPMI exporter (Prometheus)")
    ap.add_argument("--config", required=True, metavar="FILE")
    ap.add_argument("--mch", default=None)
    ap.add_argument("--bind", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=9290)
    ap.add_argument("--interval", type=float, default=60.0, metavar="SEC")
    ap.add_argument("--gap", type=float, default=DEFAULT_GAP, metavar="SEC")
    ap.add_argument("--max-interval", type=float, default=900.0, metavar="SEC")
    ap.add_argument("--sdr-cache", metavar="FILE")
    ap.add_argument("--amc-aliases", metavar="FILE",
                    help="FEC name table (default: crates/amc-aliases.json)")
    args = ap.parse_args()

    with open(args.config) as fh:
        cfg = json.load(fh)
    crate_name = args.mch or cfg["mch"]
    mch = resolve_mch(crate_name)
    stats["config"] = args.config

    sdr_cache = args.sdr_cache or cfg.get("sdr_cache")
    if sdr_cache:
        if not os.path.isabs(sdr_cache):
            sdr_cache = os.path.join(os.path.dirname(os.path.abspath(args.config)),
                                     sdr_cache)
        if not os.path.exists(sdr_cache):
            print("sdr cache %s missing" % sdr_cache, file=sys.stderr)
            sdr_cache = None

    if args.interval < MIN_INTERVAL:
        print("refusing --interval=%g, using %g" % (args.interval, MIN_INTERVAL),
              file=sys.stderr)
        args.interval = MIN_INTERVAL

    probe = socket.socket()
    probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        probe.bind((args.bind, args.port))
    except OSError as exc:
        print("cannot listen on %s:%d (%s)" % (args.bind, args.port, exc),
              file=sys.stderr)
        return 1
    finally:
        probe.close()

    # A stale SDR cache gives plausible but WRONG values (ids resolve to
    # different sensors). Compare the recorded repository size with the crate.
    expected = cfg.get("sdr_count") if cfg else None
    if expected:
        try:
            out = subprocess.run(
                ["ipmitool", "-I", "lan", "-H", mch, "-A", "none", "sdr", "info"],
                capture_output=True, text=True, timeout=60).stdout
            live = next((int(l.split(":")[1]) for l in out.splitlines()
                         if l.startswith("Record Count")), None)
            if live and live != expected:
                print("WARNING: SDR repository is %d records but the config was "
                      "built from %d -- the SDR cache is stale and the values "
                      "below may be wrong. Re-run: scan_mch.py --config %s"
                      % (live, expected, args.config), file=sys.stderr, flush=True)
        except Exception as exc:
            print("could not check the SDR repository size: %s" % exc,
                  file=sys.stderr, flush=True)

    alias_path = args.amc_aliases or os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(args.config))),
        "amc-aliases.json")
    fec_lookup, fec_explicit, slot_absent = load_amc_aliases(alias_path)

    # NONE in amc-aliases.json means "no board in that slot": do not bridge
    # into it -- an empty slot answers nothing and costs a timeout per cycle.
    if cfg:
        kept, skipped = [], []
        for t in cfg["targets"]:
            if t.get("kind") == "amc" and slot_absent(crate_name, t.get("slot")):
                skipped.append(t["id"])
            else:
                kept.append(t)
        if skipped:
            print("empty slots (no board): %s -- not swept" % ", ".join(skipped),
                  file=sys.stderr, flush=True)
        cfg["targets"] = kept
    derived = [a for a in range(1, 13)
               if fec_lookup(crate_name, a) and not fec_explicit(crate_name, a)]
    if derived:
        print("FEC names for %s AMC%s follow the fallback rule "
              "(%s * crate + slot); add explicit entries to %s only where the "
              "cabling differs" % (crate_name, ",AMC".join(map(str, derived)),
                                   "11", os.path.basename(alias_path)),
              file=sys.stderr, flush=True)

    threading.Thread(target=poller, daemon=True,
                     args=(mch, cfg, args.interval, args.max_interval,
                           sdr_cache, args.gap, crate_name,
                           fec_lookup)).start()

    # Serve immediately: Prometheus can scrape an empty cache, and a hung AMC
    # MMC must never take the endpoint down with it.
    print("crate: %s" % describe_mch(crate_name), flush=True)
    print("serving /metrics on http://%s:%d (%d targets; the poller logs when "
          "the first sweep lands)" % (args.bind, args.port,
                                      len(cfg["targets"])), flush=True)
    httpd = ThreadingHTTPServer((args.bind, args.port), Handler)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        httpd.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
