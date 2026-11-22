# MicroTCA crate monitoring -- routine operations.
#
# Everything runs from the project root; the exporter keeps that as its
# WorkingDirectory so `crates/<name>/...` paths resolve.

CRATE ?= CD1
PY    ?= python3

.PHONY: help check scan restart status logs render up down install-units

help:
	@echo "make check              compile everything, show the crate table"
	@echo "make scan   CRATE=CD1   re-inventory a crate (stops its exporter first)"
	@echo "make restart CRATE=CD1  restart that crate's exporter"
	@echo "make status             exporter + container state"
	@echo "make logs   CRATE=CD1   follow the exporter log"
	@echo "make render             re-render both dashboards to docs/screenshots/"
	@echo "make up / make down     Prometheus + Grafana"

check:
	$(PY) -m py_compile src/*.py
	@rm -rf src/__pycache__
	@$(PY) src/mch_names.py

scan:
	systemctl --user stop mch-exporter@$(CRATE) || true
	sleep 8
	$(PY) src/scan_mch.py --mch $(CRATE) --sdr-cache crates/$(CRATE)/crate.sdr \
	    --config crates/$(CRATE)/config.json --timeout 180
	systemctl --user start mch-exporter@$(CRATE)

restart:
	systemctl --user restart mch-exporter@$(CRATE)
	@sleep 3; systemctl --user status mch-exporter@$(CRATE) --no-pager | head -4

status:
	@systemctl --user list-units 'mch-exporter@*' --no-pager
	@cd deploy && docker compose ps

logs:
	journalctl --user -u mch-exporter@$(CRATE) -f

render:
	@mkdir -p docs/screenshots
	curl -s -u "$${GF_USER:-admin}:$${GF_PASS:?set GF_PASS}" -o docs/screenshots/summary.png \
	  "http://127.0.0.1:3000/render/d/mtca-overview?width=1920&height=1450&kiosk&var-crate=$(CRATE)&var-amc=All"
	curl -s -u "$${GF_USER:-admin}:$${GF_PASS:?set GF_PASS}" -o docs/screenshots/amc.png \
	  "http://127.0.0.1:3000/render/d/mtca-amc?width=1920&height=1100&kiosk&var-crate=$(CRATE)"
	@ls -l docs/screenshots/*.png

up:
	cd deploy && docker compose up -d

down:
	cd deploy && docker compose down

install-units:
	@for f in deploy/systemd/*.service; do \
	    sed "s|@ROOT@|$(CURDIR)|g" $$f > ~/.config/systemd/user/$$(basename $$f); \
	    echo "installed $$(basename $$f)"; done
	@systemctl --user daemon-reload
