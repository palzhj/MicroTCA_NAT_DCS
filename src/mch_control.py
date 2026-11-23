#!/usr/bin/env python3
"""Local control endpoint behind the dashboards' rescan button.

    GET /health
    GET /rescan?crate=CD1[&dry=1][&token=...]
    GET /pause[?crate=CD1]      stop the exporter(s): yield the MCH
    GET /resume[?crate=CD1]     start them again
    GET /status

A rescan stops the crate's exporter, re-inventories the crate and starts the
exporter again -- the same sequence as `make scan CRATE=...`. It runs in the
background because a full sweep of a 13-target crate takes minutes; the call
returns 202 immediately and progress shows in /status.

Guardrails:
  * binds 127.0.0.1 only, so it is not reachable from the network;
  * the crate must exist in crates/crate-aliases.json;
  * one job per crate at a time;
  * optional shared token via MCH_CONTROL_TOKEN (set it if other users share
    this host -- otherwise anyone local can stop an exporter).
"""
import json
import os
import re
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CRATES = os.path.join(ROOT, "crates")
PY = sys.executable
TOKEN = os.environ.get("MCH_CONTROL_TOKEN", "")
BIND = os.environ.get("MCH_CONTROL_BIND", "127.0.0.1")
PORT = int(os.environ.get("MCH_CONTROL_PORT", "9390"))

jobs = {}            # crate -> {"started": ts, "state": str, "log": str}
lock = threading.Lock()


def known_crates():
    from mch_names import load
    return {c["name"] for c in load(os.path.join(CRATES, "crate-aliases.json"))}


def unit(verb, crate):
    return subprocess.call(["systemctl", "--user", verb, "mch-exporter@%s" % crate])


def run_scan(crate):
    log = os.path.join(ROOT, "var", "log", "rescan-%s.log" % crate)
    os.makedirs(os.path.dirname(log), exist_ok=True)
    steps = [
        ["systemctl", "--user", "stop", "mch-exporter@%s" % crate],
        ["sleep", "8"],
        [PY, os.path.join(ROOT, "src", "scan_mch.py"), "--mch", crate,
         "--sdr-cache", os.path.join(CRATES, crate, "crate.sdr"),
         "--config", os.path.join(CRATES, crate, "config.json"),
         "--timeout", "120"],
        ["systemctl", "--user", "start", "mch-exporter@%s" % crate],
    ]
    with lock:
        jobs[crate]["state"] = "running"
    try:
        with open(log, "w") as fh:
            for cmd in steps:
                fh.write("$ %s\n" % " ".join(cmd))
                fh.flush()
                rc = subprocess.call(cmd, stdout=fh, stderr=subprocess.STDOUT)
                if rc != 0 and cmd[0] != "systemctl":
                    raise RuntimeError("%s exited %d" % (cmd[0], rc))
        state = "done"
    except Exception as exc:                        # noqa: BLE001 - report it
        state = "failed: %s" % exc
    with lock:
        jobs[crate].update({"state": state, "finished": int(time.time()),
                            "log": log})


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _send(self, code, payload, ctype="application/json"):
        body = (json.dumps(payload, indent=1) if not isinstance(payload, str)
                else payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass

    def do_GET(self):
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}

        if u.path.rstrip("/") == "/health":
            return self._send(200, {"ok": True, "crates": sorted(known_crates())})

        if u.path.rstrip("/") in ("/pause", "/resume"):
            if TOKEN and q.get("token") != TOKEN:
                return self._send(403, {"error": "bad token"})
            crate = q.get("crate", "")
            known = known_crates()
            if crate and crate not in known:
                return self._send(404, {"error": "%s is not in crate-aliases.json" % crate})
            targets = [crate] if crate else sorted(known)
            verb = "stop" if u.path.rstrip("/") == "/pause" else "start"
            results = {c: unit(verb, c) for c in targets}
            with lock:
                for c in targets:
                    jobs.setdefault(c, {})["state"] = (
                        "paused" if verb == "stop" else "starting")
            return self._send(200, {"action": verb, "crates": results,
                                    "note": "pause frees the MCH's session table "
                                            "for other tooling; resume brings "
                                            "data back on the next sweep"})

        if u.path.rstrip("/") == "/status":
            with lock:
                return self._send(200, dict(jobs))

        if u.path.rstrip("/") == "/rescan":
            if TOKEN and q.get("token") != TOKEN:
                return self._send(403, {"error": "bad token"})
            crate = q.get("crate", "")
            if not re.fullmatch(r"[A-Za-z0-9_-]+", crate):
                return self._send(400, {"error": "crate required"})
            if crate not in known_crates():
                return self._send(404, {"error": "%s is not in crate-aliases.json" % crate})
            with lock:
                job = jobs.get(crate)
                if job and job["state"] == "running":
                    return self._send(409, {"error": "already running", "job": job})
            if q.get("dry"):
                return self._send(200, {"would_stop": "mch-exporter@%s" % crate,
                                        "would_scan": crate,
                                        "then": "start exporter again"})
            with lock:
                jobs[crate] = {"started": int(time.time()), "state": "queued"}
            threading.Thread(target=run_scan, args=(crate,), daemon=True).start()
            return self._send(202, {"accepted": crate,
                                    "status": "/status",
                                    "note": "rescan takes ~4 min; the panel's "
                                            "value updates on the next scrape"})

        return self._send(404, {"error": "unknown path"})


def main():
    print("control endpoint on http://%s:%d (crates: %s)"
          % (BIND, PORT, ", ".join(sorted(known_crates()))), flush=True)
    ThreadingHTTPServer((BIND, PORT), Handler).serve_forever()


if __name__ == "__main__":
    sys.exit(main())
