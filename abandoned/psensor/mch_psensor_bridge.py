#!/usr/bin/env python3
"""Expose NAT-MCH / MicroTCA sensors to psensor.

psensor reads only lm-sensors (hwmon), so it cannot see IPMI sensors by itself.
It *can* read a remote psensor-server over HTTP, and that protocol is plain
JSON, so this script impersonates a psensor-server:

    python3 scan_mch.py --config crate.json      # discover the crate first
    python3 mch_psensor_bridge.py --config crate.json
    psensor --url=http://127.0.0.1:3131

With `--config` the bridge sweeps exactly the targets the scan found (the MCH
plus each AMC's MMC) and serves exactly the sensors it listed, so it never
discovers anything on its own.  Without it, it falls back to a plain
`sdr elist` of the MCH.

API used by psensor 1.2.1 (src/rsensor.c, src/lib/psensor_json.c):

    GET /api/1.1/sensors            -> [ {id, name, type}, ... ]
    GET /api/1.1/sensors/<id>       -> { ..., "last_measure": {value, time} }

`type` is psensor's bitmask (src/lib/psensor.h): TEMP=0x1, RPM=0x2,
PERCENT=0x4.  psensor ORs in SENSOR_TYPE_REMOTE itself, so we send the bare
value type only.

`--interval` (seconds, default 60) is the sweep period and the only knob that
affects the crate's load: the MCH has one IPMB serialiser and reading all
sensors is many queued transactions, so do not push it.  The HTTP side psensor
talks to is served from cache and never triggers a sweep.

Because `psensor --url=...` replaces the local sensor list, this bridge also
relays the host's own lm-sensors values when `sensors` is installed, so you
still get CPU/NVMe temperatures in the same window.
"""
import argparse
import json
import os
import random
import re
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote

SENSOR_TYPE_TEMP = 0x1
SENSOR_TYPE_RPM = 0x2

RE_TEMP = re.compile(r"^(-?[\d.]+) degrees C$")
RE_RPM = re.compile(r"^(\d+) RPM$")

# Entity ID (first field of "193.108") -> prefix used in the display name.
ENTITY = {10: "PM", 30: "CU", 193: "AMC", 194: "MCH", 7: "Shelf"}

TRANSIT = "0x82"          # MCH's IPMB-L controller, as used by the AMC tools
TARGET_GAP = 0.5          # pause between targets inside one sweep

cache = {}
cache_lock = threading.Lock()
stats = {"ipmi_polls": 0, "ipmi_errors": 0, "last_poll": 0,
         "targets_swept": 0, "config": None, "effective_interval": None}


def run(cmd, timeout):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout).stdout


def parse_elist(out):
    """`name | sensor_id | status | entity | value` -> {(entity, sensor_id): row}.

    Keyed by the *pair*: on a 12-AMC crate the MCH mirrors each MMC's SDRs into
    its own repository and the record ids repeat between AMCs ("C8h" exists on
    several), so the id alone is ambiguous.
    """
    rows = {}
    for line in out.splitlines():
        parts = [p.strip() for p in line.split("|")]
        if len(parts) != 5:
            continue
        name, sid, status, entity, value = parts
        rows[(entity, sid)] = {"name": name, "sensor_id": sid, "status": status,
                               "entity": entity, "value": value}
    return rows


def numeric(value, kind):
    """Pull the float out of an `sdr elist` value for a known kind."""
    v = value.strip()
    if kind == "temperature":
        m = RE_TEMP.match(v)
    elif kind == "fan":
        m = RE_RPM.match(v)
    else:
        return None
    return float(m.group(1)) if m else None


def sweep(mch, target, sdr_cache=None):
    """One `sdr elist` against the MCH or a bridged AMC MMC.

    `-S` applies to the MCH only: a bridged AMC has its own SDR repository.
    """
    cmd = ["ipmitool", "-I", "lan", "-H", mch, "-A", "none"]
    if target["kind"] != "mch":
        cmd += ["-T", TRANSIT, "-B", "0", "-t", target["address"],
                "-b", str(target.get("bus", "0"))]
    elif sdr_cache:
        cmd += ["-S", sdr_cache]
    cmd += ["sdr", "elist"]
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=90)
    if p.returncode != 0:
        raise RuntimeError(" ".join((p.stderr or "rc=%d" % p.returncode).split()))
    return parse_elist(p.stdout)


def poll_ipmi(mch):
    """Legacy path: everything the MCH itself reports."""
    now = int(time.time())
    found = {}
    for row in sweep(mch, {"kind": "mch"}).values():
        sid = row["sensor_id"]
        kind, val = None, None
        m = RE_TEMP.match(row["value"].strip())
        if m:
            kind, val = SENSOR_TYPE_TEMP, float(m.group(1))
        else:
            m = RE_RPM.match(row["value"].strip())
            if m:
                kind, val = SENSOR_TYPE_RPM, float(m.group(1))
        if kind is None:
            continue          # volts/amps/discrete: psensor has no place for them
        try:
            ent = int(row["entity"].split(".")[0])
        except ValueError:
            continue
        label = ENTITY.get(ent, row["entity"])
        found["mch-%s-%s" % (row["entity"], sid)] = {
            "id": "mch-%s-%s" % (row["entity"], sid),
            "name": "%s %s" % (label, row["name"]),
            "type": kind, "value": val, "time": now,
        }
    return found


def poll_config(mch, cfg, sdr_cache=None):
    """Sweep every configured target and keep the configured sensors only."""
    now = int(time.time())
    by_target = {}
    for s in cfg["sensors"]:
        by_target.setdefault(s["target"], []).append(s)

    found = {}
    for i, target in enumerate(cfg["targets"]):
        if i:
            time.sleep(TARGET_GAP)      # never back-to-back on the IPMB bus
        try:
            rows = sweep(mch, target, sdr_cache)
            stats["targets_swept"] += 1
        except Exception as exc:
            stats["ipmi_errors"] += 1
            print("sweep of %s failed: %s" % (target["id"], exc),
                  file=sys.stderr, flush=True)
            continue
        for s in by_target.get(target["id"], []):
            if s["type"] is None:
                continue                # not representable in psensor
            row = rows.get((s["entity"], s["sensor_id"]))
            if row is None:
                continue                # sensor vanished; keep the last value
            val = numeric(row["value"], s["kind"])
            if val is None:
                continue                # unavailable reading (e.g. "nr")
            found[s["id"]] = {"id": s["id"], "name": s["name"], "type": s["type"],
                              "value": val, "time": now}
    return found


def poll_lmsensors():
    """Relay the host's own lm-sensors values, if lm-sensors is installed."""
    try:
        out = run(["sensors", "-u"], 20)  # -u = raw, stable field order
    except FileNotFoundError:
        return {}
    now = int(time.time())
    found, chip = {}, ""
    for line in out.splitlines():
        if not line.strip():
            chip = ""
            continue
        if not line.startswith((" ", "\t")) and ":" not in line:
            chip = line.strip()
            continue
        key, _, rest = line.partition(":")
        key = key.strip()
        if key.startswith(("temp", "fan")) and "_input" in key:
            try:
                val = float(rest.split()[0])
            except (ValueError, IndexError):
                continue
            kind = SENSOR_TYPE_TEMP if key.startswith("temp") else SENSOR_TYPE_RPM
            sid = "lmsensor %s %s" % (chip, key)
            found[sid] = {"id": sid, "name": "%s %s" % (chip, key), "type": kind,
                          "value": val, "time": now}
    return found


MIN_INTERVAL = 5.0   # polling faster than this is what wedges the MCH
DEFAULT_GAP = 5.0    # quiet time required after a sweep, before the next one


def poller(mch, cfg, interval, max_interval, sdr_cache=None, gap=DEFAULT_GAP):
    """Refresh the cache on a rate-limited schedule, with backoff.

    The MCH is a small MCU with one IPMB serialiser.  Every sensor read is a
    queued IPMI transaction on a shared bus, and `sdr elist` reads *all* of
    them; pushing the rate produces exactly the
    `ipmiMsgSender(...): REQ ... failed ... result -9` / `no ACK` lines in the
    console log and shows up as the Err column of `ipmi_stat`.  Therefore:

      * one bulk sweep per target per cycle, never per-sensor requests;
      * the next sweep starts no sooner than `sweep duration + gap`, so the
        bus always gets quiet time -- a big crate takes as long as it takes,
        and `interval` is only a lower bound that this overrides;
      * exponential backoff (up to `max_interval`) once the MCH starts
        complaining, so a stuck lane is not hammered further.

    Requests served to psensor never trigger a sweep: they read the cache, so
    psensor may refresh as often as it likes without touching the crate.
    """
    backoff = interval
    last_report = None
    while True:
        t0 = time.time()
        ok = True
        try:
            fresh = poll_config(mch, cfg, sdr_cache) if cfg else poll_ipmi(mch)
            stats["ipmi_polls"] += 1
        except Exception as exc:                # timeout, no answer, ...
            fresh, ok = {}, False
            stats["ipmi_errors"] += 1
            print("poll failed: %s" % exc, file=sys.stderr, flush=True)
        fresh.update(poll_lmsensors())
        with cache_lock:
            cache.update(fresh)                 # keep last value on a miss
            stats["last_poll"] = int(time.time())
        took = time.time() - t0

        if ok:
            backoff = interval
        else:
            backoff = min(backoff * 2, max_interval)

        # The real floor is what the crate needs, not what the user asked for:
        # a 12-AMC crate takes ~179 s to sweep, so a 60 s setting is a lie.
        floor = took + gap
        target = max(backoff, floor)
        if target > backoff + 0.5:
            # Say it once, and again only if the requirement really moved --
            # otherwise this would repeat every cycle.
            if last_report is None or abs(target - last_report) >= 5:
                print("interval raised %.0fs -> %.0fs (sweep took %.1fs + %gs gap)"
                      % (backoff, target, took, gap), file=sys.stderr, flush=True)
            last_report = target
        wait = max(target * random.uniform(0.9, 1.1), floor)  # jitter > gap
        print("poll #%d: %d/%d sensors in %.1fs, next in %.0fs"
              % (stats["ipmi_polls"], len(fresh), len(cache), took, wait),
              file=sys.stderr, flush=True)
        stats["effective_interval"] = round(target, 1)
        time.sleep(wait)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self):
        path = unquote(self.path.split("?")[0]).rstrip("/")
        with cache_lock:
            snapshot = dict(cache)
        if path == "/api/1.1/sensors":
            body = [{"id": s["id"], "name": s["name"], "type": s["type"]}
                    for s in snapshot.values()]
        elif path.startswith("/api/1.1/sensors/"):
            sid = path[len("/api/1.1/sensors/"):]
            s = snapshot.get(sid)
            if s is None:
                self.send_error(404)
                return
            body = {"id": s["id"], "name": s["name"], "type": s["type"],
                    "min": 0, "max": 0,
                    "last_measure": {"value": s["value"], "time": s["time"]}}
        elif path == "/api/1.1/sysinfo":
            body = {"hostname": "nat-mch", "version": "0.2", **stats}
        else:
            self.send_error(404)
            return
        data = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):
        pass


def load_config(path):
    with open(path) as fh:
        cfg = json.load(fh)
    if cfg.get("version") != 1:
        raise SystemExit("%s: unsupported config version %r" % (path, cfg.get("version")))
    for key in ("mch", "targets", "sensors"):
        if key not in cfg:
            raise SystemExit("%s: missing %r" % (path, key))
    return cfg


def main():
    # Line-buffer stdout so `nohup ... > log &` shows the "ready:" line as it
    # happens instead of never (a redirected stream is fully buffered).
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except AttributeError:
        pass

    ap = argparse.ArgumentParser()
    ap.add_argument("--mch", default=None,
                    help="MCH address (default: from config, else 192.168.1.41)")
    ap.add_argument("--config", metavar="FILE",
                    help="config written by scan_mch.py --config")
    ap.add_argument("--bind", default="127.0.0.1",
                    help="address to listen on (default: loopback only)")
    ap.add_argument("--port", type=int, default=3131)
    ap.add_argument("--interval", type=float, default=60.0, metavar="SEC",
                    help="shortest gap between sweeps (default: 60, floor: %g); "
                         "raised automatically to sweep duration + --gap"
                         % MIN_INTERVAL)
    ap.add_argument("--gap", type=float, default=DEFAULT_GAP, metavar="SEC",
                    help="quiet time required after a sweep finishes "
                         "(default: %g)" % DEFAULT_GAP)
    ap.add_argument("--max-interval", type=float, default=300.0, metavar="SEC",
                    help="cap for the failure backoff (default: 300 s)")
    ap.add_argument("--sdr-cache", metavar="FILE",
                    help="SDR repository cache (-S), overriding the config's "
                         "sdr_cache; avoids one round trip per record per sweep")
    args = ap.parse_args()

    cfg = load_config(args.config) if args.config else None
    mch = args.mch or (cfg["mch"] if cfg else "192.168.1.41")
    stats["config"] = args.config

    sdr_cache = args.sdr_cache or (cfg.get("sdr_cache") if cfg else None)
    if sdr_cache:
        if not os.path.isabs(sdr_cache) and args.config:
            sdr_cache = os.path.join(os.path.dirname(os.path.abspath(args.config)),
                                     sdr_cache)
        if not os.path.exists(sdr_cache):
            print("sdr cache %s not found: every sweep will re-walk the SDR "
                  "repository" % sdr_cache, file=sys.stderr)
            sdr_cache = None
    stats["sdr_cache"] = sdr_cache

    if args.interval < MIN_INTERVAL:
        print("refusing --interval=%g: the MCH IPMB bus cannot take a poll "
              "faster than %gs, using %g"
              % (args.interval, MIN_INTERVAL, MIN_INTERVAL), file=sys.stderr)
        args.interval = MIN_INTERVAL

    configured = exportable = 0
    if cfg:
        configured = len(cfg["sensors"])
        exportable = sum(1 for s in cfg["sensors"] if s.get("type") is not None)

    # Fail fast on a taken port, but do not start accepting yet. psensor reads
    # the sensor list exactly once and never retries, so a client that connects
    # during warm-up would stay blank forever. The port stays closed until the
    # first sweep has data; an early psensor then fails visibly instead of
    # hanging, and a later one works.
    probe = socket.socket()
    try:
        probe.bind((args.bind, args.port))
    except OSError as exc:
        print("cannot listen on %s:%d (%s) -- another instance still running? "
              "try --port" % (args.bind, args.port, exc), file=sys.stderr)
        return 1
    finally:
        probe.close()

    threading.Thread(target=poller,
                     args=(mch, cfg, args.interval, args.max_interval,
                           sdr_cache, args.gap),
                     daemon=True).start()

    # Wait for the first completed sweep so psensor gets a populated list.
    # A 12-AMC crate needs ~3 min per cycle, so allow for that.
    waited = 0
    while waited < 900:
        with cache_lock:
            if cache:
                break
        if waited and waited % 30 == 0:
            print("first sweep still running (%ds) ..." % waited, flush=True)
        time.sleep(1)
        waited += 1

    print("ready: serving %d sensors on http://%s:%d"
          % (len(cache), args.bind, args.port), flush=True)
    print("now start: psensor --url=http://127.0.0.1:%d  (start it only after "
          "this line)" % args.port, flush=True)
    print("IPMI sweep: at least every %gs, but never sooner than the sweep "
          "duration + %gs (min %g, backoff cap %g)"
          % (args.interval, args.gap, MIN_INTERVAL, args.max_interval), flush=True)
    print("SDR cache: %s" % (sdr_cache or "none (repository walked every sweep)"))
    if cfg:
        print("config %s: %d sensors configured, %d exportable, targets: %s"
              % (args.config, configured, exportable,
                 ", ".join(t["id"] for t in cfg["targets"])), flush=True)
        skipped = configured - exportable
        if skipped:
            print("  %d sensor(s) not representable in psensor (voltage/current/"
                  "state) are polled but not exported" % skipped, flush=True)
    try:
        httpd = ThreadingHTTPServer((args.bind, args.port), Handler)
    except OSError as exc:
        print("cannot listen on %s:%d (%s)" % (args.bind, args.port, exc),
              file=sys.stderr)
        return 1
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        httpd.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
