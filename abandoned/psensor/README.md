# Abandoned: psensor bridge

`mch_psensor_bridge.py` made the crate's sensors visible in
[psensor](https://wpitchoune.net/psensor/) on the desktop. It is kept here for
reference only — the Grafana stack in `deploy/` replaced it.

## What it did

psensor reads only `libsensors` (i.e. `/sys/class/hwmon/*`), and a MicroTCA
crate exposes its sensors as **IPMI over LAN**. There is no hwmon device for
them and no mainline IPMI→hwmon driver, so psensor cannot discover them.

psensor *can* read a remote `psensor-server` over a plain JSON API
(`psensor --url=...`). The bridge impersonated that server, sourcing values
from `ipmitool` instead of `libsensors`:

```
ipmitool sdr elist ──► mch_psensor_bridge.py ──HTTP/JSON──► psensor --url
        (60 s)                 cache                    (any rate, free)
```

```sh
python3 mch_psensor_bridge.py --config ../crate.json --port 3131
psensor --url=http://127.0.0.1:3131
```

Options: `--config`, `--mch`, `--bind` (default `127.0.0.1`), `--port` (3131),
`--interval` (60 s, floor 5), `--gap` (5 s), `--max-interval` (300 s),
`--sdr-cache`.

## Why it was abandoned

1. **psensor reads the sensor list once and never retries.** If it starts
   before the bridge has data it stays blank forever, which cost us a whole
   debugging session. The bridge worked around it by keeping the port closed
   until the first sweep landed, but the failure mode remained.
2. **`psensor --url` replaces the local sensor list**, so CPU/NVMe temperatures
   disappeared while watching the crate — we had to relay `sensors -u` to
   compensate.
3. **It cannot show voltages, currents or states.** psensor has types for
   temperature, RPM and percent only; 481 of the 544 sensors on the big crate
   had nowhere to go.
4. **No history, no thresholds, no alerts, no multi-crate.** A desktop widget,
   not monitoring.
5. On the big crate a full sweep takes ~180 s (or ~24 s shelf-only), which does
   not fit a "temperature display" refresh model.

Grafana over `mch_exporter.py` has none of these limits: it stores history,
charts every kind, keeps the host's own sensors, and separates scrape rate from
crate load.

## Kept because

The protocol details are documented here and were verified against psensor
1.2.1 source, in case a lightweight desktop readout is ever wanted again:

```
GET /api/1.1/sensors        -> [ {id, name, type}, ... ]
GET /api/1.1/sensors/<id>   -> { ..., "last_measure": {value, time} }
```

`type` is psensor's bitmask (`src/lib/psensor.h`): `TEMP=0x1`, `RPM=0x2`,
`PERCENT=0x4`; psensor ORs in `SENSOR_TYPE_REMOTE` itself, so the server sends
the bare value type. Query parameters and pacing are inherited from
`mch_exporter.py` — the two share the config format and the sweep rules.
