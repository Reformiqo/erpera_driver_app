# erpera_driver_app — deployment automation
#
# Shorthand for bench operations on a Frappe site running this app.
# Pass the target site via SITE, e.g.:
#
#     make update SITE=cowberry.frappe.cloud
#     make install SITE=dev.localhost
#
# Defaults SITE to dev.localhost so local development stays a one-word
# command. Override BENCH (path to frappe-bench) if you don't run from
# the bench root.

SITE  ?= dev.localhost
BENCH ?= .
APP   := erpera_driver_app
REPO  := https://github.com/reformiqo/erpera_driver_app

# ── Targets ──────────────────────────────────────────────────────────

.PHONY: help install update migrate restart status list-apps logs \
        smoke-auth export-fixtures uninstall

help:  ## Show this help.
	@awk 'BEGIN {FS = ":.*?## "} /^[a-zA-Z_-]+:.*?## / { printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2 }' $(MAKEFILE_LIST)
	@echo ""
	@echo "Variables: SITE=$(SITE)  BENCH=$(BENCH)  APP=$(APP)"

install:  ## First-time install: fetch + install + migrate + restart.
	cd $(BENCH) && bench get-app $(REPO) || true
	cd $(BENCH) && bench --site $(SITE) install-app $(APP)
	cd $(BENCH) && bench --site $(SITE) migrate
	cd $(BENCH) && bench restart
	@echo "✓ $(APP) installed on $(SITE)."

update:  ## Update an existing install: pull + migrate + restart.
	cd $(BENCH)/apps/$(APP) && git pull origin main
	cd $(BENCH) && bench --site $(SITE) migrate
	cd $(BENCH) && bench restart
	@echo "✓ $(APP) updated on $(SITE)."

migrate:  ## Apply pending doctype JSON / patches without pulling code.
	cd $(BENCH) && bench --site $(SITE) migrate
	cd $(BENCH) && bench restart

restart:  ## Restart bench workers (reloads Python modules).
	cd $(BENCH) && bench restart

status:  ## Show site status + installed app version.
	@echo "── Bench-level installed apps ──"
	cd $(BENCH) && bench list-apps
	@echo ""
	@echo "── App $(APP) version on $(SITE) ──"
	cd $(BENCH) && bench --site $(SITE) version | grep -E "(^$(APP)|^$$)" || \
		echo "$(APP) is NOT installed on $(SITE)"

list-apps:  ## List apps installed specifically on $(SITE).
	cd $(BENCH) && bench --site $(SITE) list-apps

logs:  ## Tail the bench error log (Ctrl-C to stop).
	cd $(BENCH) && tail -f logs/web.error.log

smoke-auth:  ## Probe the live auth.login endpoint (set HOST=cowberry.frappe.cloud).
	@HOST=$${HOST:-$(SITE)}; \
	echo "→ POST https://$$HOST/api/method/$(APP).api.auth.login"; \
	curl -sS -o /tmp/login_resp.json -w "HTTP %{http_code}\n" \
		-X POST "https://$$HOST/api/method/$(APP).api.auth.login" \
		-H "Content-Type: application/json" \
		-d '{"user":"smoke@test","password":"smoke"}'; \
	echo "── Response body ──"; \
	cat /tmp/login_resp.json | head -c 500; echo ""

export-fixtures:  ## Re-export Custom Field fixtures from the live site.
	cd $(BENCH) && bench --site $(SITE) export-fixtures --app $(APP)

uninstall:  ## Remove the app from $(SITE) (keeps app code on disk).
	cd $(BENCH) && bench --site $(SITE) uninstall-app $(APP) --yes
