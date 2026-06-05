# Deployment Guide — `erpera_driver_app`

Quick reference for keeping a Frappe site running the latest backend code.
Pick the path that matches your environment.

---

## TL;DR

| You want to… | Run |
|---|---|
| First-time install on a site | `make install SITE=<site>` |
| Pull latest code + migrate + restart | `make update SITE=<site>` |
| Just reload Python modules after edits | `make restart` |
| Smoke-test the live `auth.login` endpoint | `make smoke-auth HOST=<host>` |
| One-shot full deploy with safety checks | `./scripts/deploy.sh <site>` |
| See what's installed on a site | `make list-apps SITE=<site>` |

`SITE` defaults to `dev.localhost`. Override per command or `export SITE=…`.

---

## Path A — Frappe Cloud Dashboard (recommended for prod)

Frappe Cloud handles the bench operations behind a UI.

1. Open **https://frappecloud.com/dashboard** → click your site (e.g. `cowberry.frappe.cloud`).
2. **Apps** tab → find `erpera_driver_app`.
   - **Not installed yet?** Click **Install App** → "Install from GitHub" → paste `https://github.com/reformiqo/erpera_driver_app` → Install.
   - **Already installed?** Click **Update** (or **Fetch Updates** → **Deploy**). Frappe Cloud pulls the latest commit, runs `migrate`, and restarts workers automatically.
3. Wait for the deploy job to finish (status badge turns green).
4. Smoke-test in a terminal:
   ```bash
   curl https://<site>/api/method/erpera_driver_app.api.auth.app_version
   # → {"message":{"success":true,"data":{"app":"erpera_driver_app","version":"..."}}}
   ```

---

## Path B — bench SSH (self-hosted / dev)

Run from the bench root (`~/frappe-bench` typically).

### First-time install

```bash
bench get-app https://github.com/reformiqo/erpera_driver_app
bench --site <site> install-app erpera_driver_app
bench --site <site> migrate
bench restart
```

Or use the shortcut:

```bash
make install SITE=<site>
```

### Subsequent updates

```bash
cd ~/frappe-bench/apps/erpera_driver_app && git pull origin main
bench --site <site> migrate
bench restart
```

Or:

```bash
make update SITE=<site>
```

### One-shot deploy with built-in checks

`scripts/deploy.sh` runs the full sequence with idempotent guards (skips
already-done steps) and a smoke test at the end:

```bash
./scripts/deploy.sh cowberry.frappe.cloud ~/frappe-bench
```

Environment knobs:
- `APP_NAME=…` — override the package name (default `erpera_driver_app`)
- `BRANCH=staging` — deploy a non-main branch
- `SKIP_PULL=1` — skip `git pull` (useful in CI where the code is already pinned)

---

## Troubleshooting

### `App erpera_driver_app is not installed` (HTTP 417)

The app code is on disk but the site doesn't have it registered. Run:

```bash
bench --site <site> install-app erpera_driver_app
# or via Frappe Cloud Dashboard → Apps → Install App
```

Verify it's now in the site's installed list:

```bash
bench --site <site> list-apps
```

### `module 'erpera_driver_app.api.auth' has no attribute 'login'` (HTTP 417)

The app **is** installed but Python workers are still holding an older
module object. The fix is a worker restart:

```bash
bench restart
# or via Frappe Cloud Dashboard → Site → "Restart Bench"
```

This is also the symptom you get if the code on disk is older than the
deployed branch — pull first, then restart:

```bash
cd ~/frappe-bench/apps/erpera_driver_app && git pull origin main
bench --site <site> migrate
bench restart
```

### Custom fields not appearing on Delivery Note / Customer / etc

`fixtures/custom_field.json` ships ~30 Custom Fields the API code reads
and writes. They install on the first `bench migrate` after `install-app`.
If they're missing on an already-installed site, re-run:

```bash
bench --site <site> migrate
```

### Cleanly remove the app (caution — drops the custom DocType tables)

```bash
bench --site <site> uninstall-app erpera_driver_app
# or: make uninstall SITE=<site>
```

---

## CI / automated deploys

There's no GitHub Actions workflow committed yet — if you want one, the
minimum useful pipeline is:

```yaml
# .github/workflows/deploy.yml (sketch)
on:
  push:
    branches: [main]

jobs:
  deploy:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - name: Trigger Frappe Cloud deploy
        run: |
          curl -X POST \
            "https://frappecloud.com/api/method/press.api.bench.deploy" \
            -H "Authorization: token ${{ secrets.FRAPPE_CLOUD_API_TOKEN }}" \
            -d '{"site":"cowberry.frappe.cloud","apps":["erpera_driver_app"]}'
```

Or for self-hosted bench:

```yaml
- name: SSH deploy
  uses: appleboy/ssh-action@v1
  with:
    host: ${{ secrets.BENCH_HOST }}
    username: frappe
    key: ${{ secrets.SSH_KEY }}
    script: |
      cd ~/frappe-bench
      ./apps/erpera_driver_app/scripts/deploy.sh cowberry.frappe.cloud
```

---

## Reference: what the deploy actually does

1. **Pull code** — `git pull` in `apps/erpera_driver_app/` so the
   Python files on disk match `origin/main`.
2. **Install app on site** (first time only) — adds the row to
   `tabInstalled Application` so Frappe's `get_attr()` will dispatch
   to its modules.
3. **Migrate** — applies any new DocType JSON, new Custom Fields from
   the fixture, and unregistered patches.
4. **Restart workers** — reloads Python modules. **Without this step**,
   workers keep using the old code from before the pull, which is the
   most common cause of "I deployed but nothing changed" reports.
