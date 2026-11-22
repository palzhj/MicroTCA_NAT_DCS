# MicroTCA crate monitoring (NAT-MCH)

Two tools that watch a MicroTCA crate through its NAT-MCH carrier hub over
IPMI/LAN, plus a Grafana stack to display them:

| Tool | Purpose |
|---|---|
| [`scan_mch.py`](src/scan_mch.py) | inventory every monitoring item — the shelf's shared FRUs **and each AMC's own MMC** — and write the config |
| [`mch_exporter.py`](src/mch_exporter.py) | sweep that config and expose every sensor as Prometheus metrics |
| [`deploy/`](deploy/docker-compose.yml) | Prometheus + Grafana, dashboards provisioned |

```
src/scan_mch.py ──crates/<name>/config.json──► src/mch_exporter.py ──/metrics──► Prometheus ──► Grafana
 (discovery)                 (sweep + cache)              (180 s scrape)  (panels)
```

Both tools are read-only and speak `ipmitool` over RMCP; neither touches the
telnet CLI, so they can run while someone is logged in.

The old psensor bridge now lives in [`abandoned/psensor/`](abandoned/psensor/README.md).

## Target crate

| Property | Value |
|---|---|
| MCH | N.A.T. **NAT-MCH-PHYS M4**, FW `V2.23.6`, IPMI 1.5 |
| Shelf | Schroff MicroTCA 11850-027 |
| SDR records | **573** |
| Monitoring items | **955** (544 from the shelf, 411 from the 12 AMC MMCs) |
| AMCs | **12** (u4FCP-v2, every slot) |
| RTT | ~46 ms |

Seven crates are monitored, one exporter each:

| Crate | Items | AMCs | Sweep | `/metrics` |
|---|---|---|---|---|
| CD1 | 955 | 12 | ~183 s | 9291 |
| CD2 | 955 | 12 | ~183 s | 9292 |
| CD3 | 884 | 11 | ~169 s | 9293 |
| CD4 | 884 | 11 | ~163 s | 9294 |
| CD5 | 955 | 12 | ~185 s | 9295 |
| CD6 | 955 | 12 | ~185 s | 9296 |
| TVT | 458 | 5 | ~78 s | 9290 |

A crate at `192.168.1.41` is parked: its MCH's IPMI session layer is wedged and
needs a reboot.

## Crate table is local

`crates/crate-aliases.json` carries your site's addresses and is **gitignored**.
The tracked template is [`crates/crate-aliases.example.json`](crates/crate-aliases.example.json),
which uses the NAT-MCH factory address (`192.168.1.41`) so a fresh clone points
somewhere harmless:

```sh
cp crates/crate-aliases.example.json crates/crate-aliases.json
$EDITOR crates/crate-aliases.json      # real names + IPs
python3 src/mch_inventory.py           # regenerate env files + crates.yml
```

## Ports are derived

`crates/crate-aliases.json` has no port field. The exporter's HTTP port is
computed from the crate name -- `9290 + the trailing number`, so `CD1 -> 9291`,
`CD2 -> 9292`, `TVT -> 9290` -- and written into `crates/<name>/env` and
`crates.yml` by `src/mch_inventory.py`. Override the base with `MCH_PORT_BASE`
if 9290 is taken.

## Layout

```
dcs/
├── src/                    the code (stdlib only)
│   ├── scan_mch.py         inventory a crate -> config.json
│   ├── mch_exporter.py     sweep + /metrics (one process per crate)
│   ├── mch_names.py        crate name -> IP   (reads crates/crate-aliases.json)
│   └── mch_inventory.py    regenerate crates.yml / hosts / env from that file
├── crates/
│   ├── crate-aliases.example.json  tracked template (factory IPs)
│   ├── crate-aliases.json          YOUR crates -- gitignored
│   ├── amc-aliases.json    (crate, AMC slot) -> FEC name; NONE = empty slot
│   ├── <name>/             config.json, config-shelf.json, crate.sdr, env
│   └── hosts.snippet       generated, optional /etc/hosts
├── deploy/
│   ├── docker-compose.yml  Prometheus + Grafana (+ image renderer)
│   ├── prometheus/         prometheus.yml + generated crates.yml
│   ├── grafana/            provisioning + dashboard JSON
│   └── systemd/            mch-exporter@.service (template)
├── docs/screenshots/       rendered dashboards
├── abandoned/psensor/      the retired psensor bridge
└── Makefile                make check / scan / restart / status / render
```

## Credentials

Nothing secret is tracked:

| What | Where |
|---|---|
| Grafana admin user/password | `deploy/.env` (gitignored); copy `deploy/.env.example` |
| Control-endpoint token (optional) | `crates/control.env`, referenced by the unit |
| Crate addresses | `crates/crate-aliases.json` (gitignored); template is `crate-aliases.example.json` |

## Requirements

- `python3` (standard library only, no packages)
- `ipmitool` ≥ 1.8.11 — the NAT-MCH manual's minimum
- Docker, for the dashboard stack only

## Scanning the crate — `scan_mch.py`

```sh
# shelf-only inventory (the MCH already mirrors each AMC's sensors): ~31 s
python3 src/scan_mch.py --mch 192.0.2.32 --no-amc \
        --sdr-cache crates/CD1/crate.sdr \
        --config crates/CD1/config-shelf.json

# full inventory, bridging into each AMC's MMC: ~3 min 43 s
python3 src/scan_mch.py --mch 192.0.2.32 \
        --sdr-cache crates/CD1/crate.sdr \
        --config crates/CD1/config.json --timeout 180
```

| Option | Default | Meaning |
|---|---|---|
| `--mch` | `192.168.1.41` | MCH address |
| `--config FILE` | — | write the config consumed by the exporter |
| `--sdr-cache FILE` | — | cache the SDR repository (`-S`); created with one `sdr dump` if missing |
| `--no-amc` | off | shelf only: trust the MCH's mirror of the AMC sensors |
| `--all-slots` | off | probe AMC1–12 even when hot-swap says the slot is empty |
| `--delay SEC` | `1.0` | pause between IPMI calls (floor: 0.5) |
| `--timeout SEC` | `90` | per-IPMI-call timeout |
| `--json` / `--out FILE` | off | structured report |

Why `--sdr-cache` matters: a sweep costs roughly one round trip per SDR record.
On this crate that is 573 records × 2 trips ≈ **87 s**, so without the cache
every single call pays it again. With it, a full sweep is **24 s**.

`--no-amc` is the cheap mode for dashboards: the MCH mirrors 324 AMC sensors
(27 per AMC) into its own repository, so bridging into the 12 MMCs only adds
RTM/FMC2/FMC3 detail — and costs 156 s of a 180 s cycle.

## Exporting — `mch_exporter.py`

```sh
python3 src/mch_exporter.py --config crates/CD1/config.json --port 9291 --interval 300
curl -s http://127.0.0.1:9290/metrics | head
```

| Option | Default | Meaning |
|---|---|---|
| `--config FILE` | required | config from `scan_mch.py --config` |
| `--mch` | from config | MCH address |
| `--port` | `9290` | `/metrics` port |
| `--bind` | `0.0.0.0` | listen address |
| `--interval SEC` | `60` | shortest gap between sweeps (floor: 5) |
| `--gap SEC` | `5` | quiet time required after a sweep |
| `--max-interval SEC` | `900` | cap for the failure backoff |
| `--sdr-cache FILE` | from config | overrides the config's `sdr_cache` |

Metrics:

```
mch_sensor_value{id,name,kind,target,entity}    temperature / voltage / current / fan / power
mch_sensor_status{id,name,target,status}        1 = ok
mch_sweep_success                              1 = last sweep completed
mch_sweep_duration_seconds                     wall clock of the last sweep
mch_target_sweep_duration_seconds{target}
mch_sensors_reported
mch_sweep_errors_total
```

The endpoint opens immediately — a scrape during warm-up gets an empty cache
rather than a connection error — and the poller reports when the first sweep
lands:

```
serving /metrics on http://0.0.0.0:9291 (13 targets; the poller logs when the first sweep lands)
sweep: 486 sensors in 183.2s, next in 297s
```

## Mis-scaled sum sensors

Some PM modules publish a `Current(Sum)` that disagrees with their own sixteen
channel sensors. The exporter reconciles every sweep: if the two differ by more
than `max(2 A, 20 %)`, it **exports the channel total** and records the mismatch
in a metric, because the channels are per-load measurements that agree with each
other while the sum record is the one that can be scaled wrongly.

```
TVT  PM2 Current(Sum) sensor : 80.80 A
TVT  Σ channels             : 21.40 A     <- exported
mch_sum_mismatch{pm="PM2",reported="80.80",channels="21.40"} 1
```

The warning appears in the journal once per sweep, and the alert rule above
keeps it on the record until the module is fixed.

## Partial sweeps

A sweep can fail on one target (a dead MMC, a transient session refusal). The
exporter then keeps the **last good value** for everything it could not reach
and reports `mch_sweep_success 0`, rather than blanking the panel:

```python
readings.update(found)   # ok=False: merge, keep the rest
readings.clear(); readings.update(found)   # ok=True: replace, removals are real
```

A complete sweep is authoritative — a sensor that vanished really is gone
(hot-swap out). Only a partial one merges.

## Sensor names and labels

Every exported series carries the same label set, so the dashboards never have
to guess:

| Label | Meaning | Example |
|---|---|---|
| `name` | friendly alias, `+` removed | `PM2 Ch04 Current`, `DDR 1.2VB` |
| `orig` | the MMC's own sensor name, verbatim | `DDR +1.2VB` |
| `pair` | the rail, `+` removed, V/I merged | `IO 3.3V`, `RTM 12V` |
| `display` | what the legend shows | `PM2 Ch04 Current (CD1)` |
| `fec` | installation alias of the AMC | `FEC13` |
| `amc` | slot number, `0` for shelf sensors | `2` |
| `entity` | IPMI entity / instance | `193.98` |
| `kind` | `temperature` `voltage` `current` `fan` `power` `state` | `current` |

`display` is `<alias> (<crate>)` and only grows a trailing original name when
the alias does not already contain it — `CU2 LM75 Temp (CD1)`, not
`CU2 LM75 Temp (CD1) LM75 Temp`. If two different sensors would produce the
same display, the second gets `[entity]` appended, so a legend row is never
ambiguous.

`name` and `display` are the *presentation* form: the vendor's `+` is removed
(`DDR +1.2VB` -> `DDR 1.2VB`) so they match the rail names in `pair`, and an
AMC's slot prefix is replaced by its FEC name (`AMC12 DDR +1.2VB` ->
`FEC23 DDR 1.2VB (CD1)`), so both dashboards use one naming system. Only
`orig` keeps the verbatim `ipmitool` spelling, which is what you want when
cross-checking the crate by hand.

The `+12 V` rail is measured in two places: its voltage is the AMC sensor
`AMC +12V`, its current `RTM +12V Curr`. Both carry `pair="RTM 12V"`, so one
dropdown entry plots both.

### FEC aliases — `crates/amc-aliases.json`

The FEC name of each board is assigned per installation, so it is a table, not
a formula:

```json
{
  "table": {
    "TVT": {"1": "FEC2", "8": "FEC110", "2": "NONE"},
    "CD1": {"1": "FEC3", "2": "FEC13"}
  },
  "fallback": {"prefix": "FEC", "per_crate": 11}
}
```

* an entry wins for that slot;
* **`NONE` means "no board in that slot"** — the exporter then skips it
  entirely instead of paying a 60 s timeout per cycle;
* a slot with no entry gets `FEC<per_crate * crate_number + slot>`, which is
  the correct assignment for this installation — no entry is needed unless the
  cabling differs. The exporter reports which slots used the rule at startup.

An **empty slot** needs no target: `scan_mch.py` derives the populated slots
from the hot-swap sensors, so CD3/CD4 (no board in slot 7) have 11 AMC targets
and TVT has 5. The `NONE` entries are a second line of defence for a config
that does include such a slot.

Restart the crate's exporter after editing; the change needs no rescan.

## Config format

JSON, versioned, generated — edit it to add or remove monitoring items:

```json
{
  "version": 1,
  "mch": "192.0.2.32",
  "sdr_cache": "crate.sdr",
  "targets": [
    {"id": "shelf", "kind": "mch", "label": "shelf"}
  ],
  "sensors": [
    {"id": "mch-193.108-D9h", "name": "AMC HOTSWAP AMC", "kind": "state",
     "type": null, "target": "shelf", "sensor_id": "D9h",
     "sensor_name": "HOTSWAP AMC", "entity": "193.108"}
  ]
}
```

- `targets` — what gets swept: `kind: "mch"` (unbridged) or `kind: "amc"`
  (bridged to `address`/`bus`).
- `sensors[].entity` + `sensor_id` — the lookup key. Both are required: record
  ids repeat between AMCs ("C8h" exists on several), so the id alone is
  ambiguous.
- `sensors[].kind` — drives the metric name suffix.
- `sensors[].type` — legacy, for the abandoned psensor bridge; the exporter
  ignores it.

**Editing rules of thumb:**

| you edit | data | crate load |
|---|---|---|
| `sensors[]` | fewer/more metrics | **unchanged** — a sweep reads every sensor on the target anyway |
| `targets[]` | adds a whole FRU | **large** — one more `sdr elist` per cycle (an AMC ≈ 13 s) |

Restart the exporter after any edit. Deleting a target without deleting its
sensors leaves those sensors frozen at their last value.

## Rate budget — only one leg is expensive

The path is `crate → exporter → Prometheus → Grafana`, but **only the first leg
speaks IPMI**. Everything downstream is free to poll as often as it likes.

| Leg | Protocol | Cost per unit | Current setting | Who throttles it |
|---|---|---|---|---|
| crate → exporter | IPMI/RMCP over UDP, 1 session per `ipmitool` call | shelf elist ~24 s, each bridged AMC ~13 s → **~183 s per full sweep, 13 sessions** (TVT: 6) | one sweep, then `sweep + 5 s` | the exporter itself |
| exporter → Prometheus | HTTP `GET /metrics`, served from cache | 0 IPMI, 0 sessions | **180 s** for the CD crates, **90 s** for TVT (own job) | nothing needed |
| Prometheus → Grafana | HTTP query API | 0 IPMI | 30 s–1 m | nothing needed |

So the crate sees roughly **13 sessions per 3 minutes** (~2.6/min) no matter how
often Grafana refreshes or how short the dashboard time range is. A 10 s scrape
or 1 m Grafana refresh changes nothing on the IPMB bus.

**The `--interval` flag cannot make it faster than the hardware allows.** The
wait is `max(--interval, sweep + 5 s)`, and a full 13-target sweep takes ~183 s
— so `--interval 5` still ends up at ~188 s. That is deliberate: the setting is
a *lower bound*, and the measured sweep time wins.

If you want fresher data, the lever is the **sweep scope**, not the interval:

| Config | Sweep | Rails per AMC | Interval floor |
|---|---|---|---|
| shelf-only (`config-shelf.json`) | ~24 s | 15 | ~29 s |
| shelf + 12 AMC MMCs (`config.json`) | ~183 s | 22 | ~188 s |

Splitting the work across two exporters would double the session rate on one
MCH, which is exactly what wedges its session table — one consumer per crate.

## Rate policy — read this before changing `--interval`

The MCH is a small MCU with **one IPMB serialiser**. `sdr elist` reads the SDR
repository *and every sensor value*, so a sweep is already dozens of queued
transactions on a bus shared with the shelf manager's own housekeeping. Polling
too fast produces:

```
ipmiMsgSender(51): REQ(I2C=0xc4) failed on bus 13, result -9
ipmiMsgSender(61): REQ(I2C=0x16) failed on bus 17 - no ACK
```

and can leave a lane wedged. Hence:

- **`--interval` is a lower bound**: the wait is `max(interval, sweep + gap)`,
  so a 486-sensor crate paces itself (~188 s) whatever you ask for;
- **one bulk sweep per target per cycle**, never per-sensor requests;
- **exponential backoff** on failure, with a **60 s retry** for the first three
  failures — a refused session usually means the MCH's session table is
  momentarily full and ages out in about that time;
- Prometheus scraping is decoupled: it reads the cache and costs the crate
  nothing, so the scrape period is chosen to match the sweep (180 s) rather
  than to be faster than the data.

**One consumer per crate.** Every `ipmitool` call opens an IPMI session, and
this MCH's session table is small. A second poller (a psensor bridge, an ad-hoc
loop) causes:

```
Get Session Challenge command failed
Error: Unable to establish LAN session
```

### Verifying that you are not overloading the MCH

`ipmi_stat` (telnet CLI, firmware ≥ 2.22) is the canary — check that the `Err`
column and the queueing counters do not move:

```
   Dev        TxReq     TxACK        Rx   || Err
 IPMB-L01  :  19717       -          -    ||  78820
 IPMB-EXP  : 5987119       3    5987122   ||     54

Traffic loading:
 queueing(2)   :  -
 queueing(>=3) :  -
```

If `queueing(2)` or `queueing(>=3)` stops showing `-`, requests are piling up.

## Dashboard stack (Prometheus + Grafana)

The exporters run on the host (they shell out to `ipmitool`); Prometheus and
Grafana run in Docker.

### Crate names — one file to edit

[`crates/crate-aliases.json`](crates/crate-aliases.json) maps a short name to each MCH:

```json
{"crates": [
  {"name": "CD1", "ip": "192.0.2.32", "site": "LAB"},   # documentation IP
  {"name": "TVT", "ip": "192.0.2.31", "site": "TAO"}
]}
```

Ports are not configured -- see [Ports are derived](#ports-are-derived).

Comment a block out with `//` to disable a crate. Then regenerate the derived
files — nothing else needs editing:

```sh
python3 src/mch_inventory.py     # crates/hosts.snippet, prometheus/crates.yml, crates/*/env
python3 src/mch_names.py         # show the table
```

`mch_names.py` resolves names at run time, so `scan_mch.py --mch CD1` and
`"mch": "CD1"` in a config work **without** `/etc/hosts`. Adding the snippet to
`/etc/hosts` is optional, only for tools that do not go through the resolver.

### One service per crate, one template

```sh
cp deploy/systemd/mch-exporter@.service ~/.config/systemd/user/
systemctl --user daemon-reload
loginctl enable-linger "$USER"           # keep running without a login

systemctl --user enable --now mch-exporter@CD1     # port comes from crates/CD1/env
```

`%i` is the crate name; the port is read from `crates/%i/env`, which
`mch_inventory.py` generates. Onboarding crate 2 is therefore:

```sh
mkdir -p crates/CD2
systemctl --user stop  mch-exporter@CD2 2>/dev/null || true
python3 src/scan_mch.py --mch CD2 --no-amc \
        --sdr-cache crates/CD2/crate.sdr --config crates/CD2/config.json
systemctl --user enable --now mch-exporter@CD2
```

The scan and the exporter must not run at the same time — one consumer per MCH.

### The UI

```sh
cd deploy && docker compose up -d
#    Grafana    http://127.0.0.1:3000   credentials in deploy/.env
#    Prometheus http://127.0.0.1:9090
```

Two provisioned dashboards, cross-linked:

| Dashboard | Content |
|---|---|
| **MicroTCA crates - summary** (`/d/mtca-overview`) | `Sweep OK` strip (all crates, 3 grid units), `Current (A) - PM total`, `Current (A) - PM channels`, `Temperatures (C)`, `Cooling unit fans (RPM)` -- 2 x 2, currents on top |
| **MicroTCA crates - AMC details** (`/d/mtca-amc`) | one rail across every FEC: voltage on the left, current on the right, plus a rails table (V only / I only / both) and an `AMC slot -> FEC` table |

Variables: `Crate` (both pages) and `Rail` (AMC page, populated from
`mch_rail_signals`, filtered to drop pre-normalisation `+` names).

Charts use a table legend with `Last / Max / Mean / Min`, sorted by Last, and a
single-mode tooltip.

| Component | Pacing | Why |
|---|---|---|
| exporter sweep | 300 s | the only thing that talks to the crate |
| Prometheus scrape | 180 s (`mtca`) / 90 s (`mtca-fast`) | matched to each crate's sweep: one stored point per real measurement |

Prometheus sets `scrape_interval` per **job**, not per target, so a crate with a
small sweep asks for its own job with `"fast": true` in
`crates/crate-aliases.json`. `mch_inventory.py` then writes it to
`crates-fast.yml` instead of `crates.yml`. TVT (78 s sweep) uses this; the CD
crates (183 s sweep) stay in the slow job.

### Stale series after a rename

Changing a label creates **new time series**; the old ones live on for the
retention period and show up as duplicate legend rows (`FMC2 +12V` next to
`FMC2 12V`, or `mch_sensor_value {__name__=…}` when a series predates the
`display` label). They are historical, not a collection fault.

Check for them with:

```promql
count by (crate) (mch_sensor_value)      # unexpected crates?
count(mch_sensor_value{display=""})      # series with no name?
```

To remove them, enable the admin API temporarily
(`--web.enable-admin-api` in `deploy/docker-compose.yml`), then:

```sh
curl -X POST 'http://127.0.0.1:9090/api/v1/admin/tsdb/delete_series' \
     --data-urlencode 'match[]={__name__="mch_sensor_value", display=""}'
curl -X POST 'http://127.0.0.1:9090/api/v1/admin/tsdb/clean_tombstones'
```

Always include `__name__`: a matcher on a label the metric does not have
(`display!~…`) also matches every series *without* that label and will delete
`up`, `mch_sweep_success` and friends. Deleted samples cannot be recovered, and
history before the rename is lost with them.

### Alerting

Four provisioned rules (`deploy/grafana/provisioning/alerting/`), evaluated every
minute:

| Rule | Expression | For | Means |
|---|---|---|---|
| **MTCA exporter down** | `up{job=~"mtca.*"} < 1` | 5 m | a crate's `/metrics` stopped answering |
| **MTCA sweep failed** | `mch_sweep_success < 1` | 15 m | the exporter runs but the crate does not complete sweeps |
| **AMC not in M4** | `mch_sensor_state_info{name=~".*HS 0.*AMC.*", state!~"Transition to M4"} > 0` | 10 m | a module is not active |
| **AMC exposes no readable sensor** | joined count `< 1` (see the slot table) | 15 m | the module is active but publishes no measurement |
| **PM sum disagrees with its channels** | `mch_sum_mismatch > 0` | 10 m | a `Current(Sum)` sensor is mis-scaled |

The last two are the ones that matter: an unreadable module is invisible to a
plain `up` or `sweep_success` check — the crate is healthy, one board is blind.

Delivery uses the `mtca-ops` contact point, which is **email and needs SMTP**
under `[smtp]` in `grafana.ini`. Until that is configured the rules still
evaluate and their state is visible in *Alerting → Alert rules*.

### Why not `ipmi_exporter`

Because it cannot talk to these MCHs. `ipmi_exporter` wraps FreeIPMI, and
FreeIPMI fails to open a session against the NAT-MCH while `ipmitool` succeeds:

```
# ipmitool -I lan -A none            -> works (every call this session)
# ipmi-sensors -D LAN   -a none     -> ipmi_ctx_open_outofband: session timeout
# ipmi-sensors -D LAN_2_0 -a none   -> ipmi_ctx_open_outofband_2_0: session timeout
# (also with -l ADMIN / -l OPERATOR, and with --network host)
```

So `mch_exporter.py` reuses the `ipmitool` path that is known to work.

## Sensor mapping

A series is identified by `(entity, the MMC's own sensor name)` — never by the
SDR id, which the MCH renumbers whenever the repository changes:

```
id="193.108/AMC +12V"          shelf view
id="amc12/192.0/FMC2 VADJ"     bridged view: every MMC uses entity 192.0,
                               so the target has to be part of the key
```

Display prefixes come from the entity:

| Entity | Prefix |
|---|---|
| 7 | `Shelf` |
| 10 | `PM` — power module |
| 30 | `CU` — cooling unit |
| 193 | `AMC` — AdvancedMC |
| 194 | `MCH` — MCH/hub/clock modules |

An AMC's rails arrive twice (the MCH mirrors 27 of the 35 sensors, the MMC
reports all of them). The bridged copy wins, and the mirror is skipped — see
the `if amc:` block in `sweep()`.

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `Get Session Challenge command failed` | session table full — stop other consumers, wait ~60 s, the exporter retries |
| `sweep of shelf failed: ... timeout` | crate busy or SDR cache missing; check `ipmi_stat`, regenerate the cache |
| `ready: serving /metrics (0 sensors)` never appears | the first sweep has not completed; watch `journalctl --user -u mch-exporter@CD1 -f` |
| Prometheus target `down` | exporter not running, or `mch-exporter:9290` unreachable — verify with `docker compose exec prometheus wget -qO- http://mch-exporter:9290/metrics` |
| Grafana panels empty | datasource/targets first: `http://127.0.0.1:9090/targets` |
| Scan takes forever | missing `--sdr-cache`; without it every call re-walks 573 records |
| Legend shows `mch_sensor_value {__name__=…}` or a name twice | stale series from before a rename — see [Stale series after a rename](#stale-series-after-a-rename) |
| `FEC names … are DERIVED` at startup | slots missing from `crates/amc-aliases.json`; the name is computed, verify it |
| An AMC panel is empty for one rail | that rail reports one signal only (`FMC0 3.3V` is current-only); the rails table says which |
| `numeric sensors exposed = 0` for a slot | the board answers but every sensor reads `Disabled`/`Not Available`. Check `ipmitool … -t 0xSS sdr get "AMC +12V"` and `fru print`: `Device not present` means the MMC is up but its FRU/SDR are unprovisioned — reseat/power-cycle, then replace |

## Known limitations

* **`mch_sensor_status` lacks `amc`/`fec`**, so an alert cannot say "HOTSWAP AMC
  changed on FEC13" — only `mch_sensor_value` carries those labels. Adding them
  renames every status series.

## Abandoned

[`abandoned/psensor/`](abandoned/psensor/README.md) — the psensor bridge, kept
for reference. Superseded by Grafana: psensor reads its sensor list once, cannot
show voltages/currents, and replaces the host's own sensors.
