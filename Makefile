# Teal Deal — common tasks. `make help` lists them.
#
# Thin wrapper: the build lives in run.sh, the server in collector.py.

HOST    ?= teal-deal.test
CADDY   ?= teal-deal-caddy
BROWSER ?= chromium
DUCKDB  ?= duckdb
QUACK   ?= quack:localhost:8789
TOKEN   ?= teal-deal-demo-token

.DEFAULT_GOAL := help
.PHONY: help build run serve public browser check stress stop

help:  ## list the targets
	@grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  make %-9s %s\n", $$1, $$2}'

# Rebuild only if the data is missing; `make build` always rebuilds.
teal_deal_catalog.ducklake:
	./run.sh --build-only

build:  ## rebuild all data from scratch, don't serve
	./run.sh --build-only

run:  ## rebuild, then serve locally on :8787 (same as ./run.sh)
	./run.sh

serve: teal_deal_catalog.ducklake  ## serve what's already built (no rebuild)
	uv run collector.py

public: teal_deal_catalog.ducklake  ## serve via TLS proxy, quack read-only (needs docker, Linux)
	-@docker rm -f $(CADDY) >/dev/null 2>&1
	docker run -d --name $(CADDY) --network host \
	  -v $(CURDIR)/deploy/Caddyfile.local:/etc/caddy/Caddyfile:ro caddy:2 >/dev/null
	@echo "site  https://$(HOST):8443   dashboard https://$(HOST):8443/dashboard.html"
	@echo "open it with: make browser   (or see deploy/Caddyfile.local)"
	trap 'docker rm -f $(CADDY) >/dev/null 2>&1' EXIT; \
	TEAL_DEAL_PUBLIC=1 TEAL_DEAL_QUACK_URL=quack:$(HOST):8444 uv run collector.py

browser:  ## open the public-mode dashboard in a throwaway browser profile
	$(BROWSER) --user-data-dir=/tmp/teal-deal-browser \
	  --host-resolver-rules="MAP $(HOST) 127.0.0.1" --ignore-certificate-errors \
	  https://$(HOST):8443/dashboard.html

check:  ## confirm the quack endpoint refuses writes (run alongside `make public`)
	@$(DUCKDB) -c "INSTALL quack; LOAD quack; ATTACH '$(QUACK)' AS r (TOKEN '$(TOKEN)')" \
	  -c "SELECT count(*) AS events FROM r.events"
	@out=$$($(DUCKDB) -c "INSTALL quack; LOAD quack; ATTACH '$(QUACK)' AS r (TOKEN '$(TOKEN)')" \
	  -c "INSERT INTO r.events SELECT * FROM r.events LIMIT 0" 2>&1); \
	if echo "$$out" | grep -q "Authorization failed"; then echo "OK: writes refused"; \
	else echo "FAIL: write was not refused — is the collector running via 'make public'?"; echo "$$out"; exit 1; fi

stress:  ## concurrent-writer test (plain mode only; public mode refuses writes)
	uv run 07_stress.py --workers 8 --duration 10

stop:  ## stop collector, proxy and throwaway browser
	-@pkill -INT -f "[c]ollector.py"
	-@docker rm -f $(CADDY) >/dev/null 2>&1
	-@pkill -f "[t]eal-deal-browser"
	@echo stopped
