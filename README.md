# EDDN Validator

Listens to the EDDN relay, stores the traffic in PostgreSQL, and scores EDDN
sender applications against the reference senders (EDMC, EDDiscovery,
EDDLite, EDO Materials Helper). See [docs/DESIGN.md](docs/DESIGN.md) for the design.

## Services (docker compose)

| Service | What it does | Port |
|---|---|---|
| `listener` | Stores every EDDN relay message | – |
| `worker` | Partitions, release poll, retention, pairing (5 min), reference profiles (hourly), scorecards (15 min) | – |
| `web` | Public scorecard site, read-only (`eddn_ro`) | 8027 (`WEB_PORT`) |
| `migrate` | Applies migrations, then exits | – |

For internet exposure, put `web` behind a reverse proxy: see `deploy/nginx-example.conf`.
The public site is currently at https://eddnvalid.weslocke.com/.

The public site has these pages:

| Page | Shows |
|---|---|
| `/` | Picker, plus the applications first seen in the last 30 days, A–Z |
| `/apps` | Every application A–Z in three columns, with its letter grade and score |
| `/card/{app_id}?v={version}` | A version's scorecard, over all its messages on file (reference apps: last 7 days), with every version of the app listed at the bottom. Hover any underlined field for an explanation and breakdown. Old `/card/{app_id}/{days}` links redirect here. |
| `/about`, `/about/{layer}` | How scoring works, and a plain-English list of what each layer checks (built from `config/`, so it can't drift) |

Logo and favicons are in `src/eddn_validator/static/`; `logo.svg` is the source,
and the PNGs were rendered from it.
Configuration lives in `config/`:

- `rules.yaml`: documented EDDN rules
- `scoring.yaml`: weights, thresholds and value checks
- `reference_apps.yaml`: the reference apps
- `retention.yaml`: retention limits

Changing a config file needs a rebuild (`compose up -d --build`).

## Setup

1. Copy `.env.example` to `.env` and fill it in.
2. Create a Docker context for the host, once:
   `docker context create dockerhost --docker host=ssh://user@your-docker-host`
3. Create the roles and database, once:
   `docker --context dockerhost compose run --rm --build migrate bootstrap`
4. Start the stack:
   `docker --context dockerhost compose up -d --build`

To deploy changes, run step 4 again. Only containers whose image or
configuration changed are restarted.

Logs: `docker --context dockerhost compose logs -f listener worker`

## Commands

`eddn-validator` (the image entrypoint) has these subcommands:

| Command | What it does |
|---|---|
| `bootstrap` | Creates the `eddn_app` and `eddn_ro` roles and the database using the admin login, then migrates |
| `migrate` | Applies pending migrations and creates upcoming daily partitions |
| `listener [--dry-run] [--seconds N]` | Subscribes to the relay and stores messages |
| `worker [--once JOB]` | Runs the scheduled jobs: `partitions`, `releases`, `retention`, `pairing`, `profiles`, `scorecards` |
| `import-archive [--since D] [--until D] [--workers N] [--reference]` | Imports history from the EDDN archive at edgalaxydata.space (default: last 180 days to yesterday). Resumable, and only fills gaps in the data already held. |
| `import-sqlite PATH` | Imports the earlier listener's `eddn.sqlite` (resumable; progress kept in `sqlite_imports`) |
| `web [--port N]` | Serves the public scorecard site |
| `scorecard APP VERSION` | Scores one app version now and prints a text summary |
| `pair-backlog` | Pairs every unpaired message now, without the worker's per-run time limit. Waits for the worker's pairing lock instead of exiting. |
| `backfill-station-systems` | Fills in which star system each known station is in, from all stored reference-app docking and location messages |
| `create-indexes` | Builds the indexes added after launch, one partition at a time, without blocking the listener. Safe to re-run. |
| `analyze-partitions` | Planner statistics on every `messages` partition, then `ANALYZE` each, one at a time (the worker re-analyzes recent days daily) |
| `import-missing-facts --since D --until D [--workers N]` | Reads older archive days and stores reference facts only for places other apps report and no reference fact covers (live game only); then re-pairs, pins, rebuilds other apps' cards and measures the pairing gap (DESIGN §24) |
| `pin-facts` | Pins every fact that other apps' messages on file refer to, so retention keeps it |
| `followups [--from STEP]` | The one-off backfill follow-up job (DESIGN §21–25); `--from` resumes at a step: wait, backfill, import-13, repair, rebuild, measure, import-14, final |
| `rules-report [--hours N] [--app TEXT]` | Documented-rule findings per app over recent traffic |

Any command can run outside Docker with `--env-file .env`, e.g.
`.venv/bin/eddn-validator --env-file .env scorecard "EDDI" 5.0.5`.

## Importing EDDN history

`import-archive` streams the daily files from https://edgalaxydata.space/EDDN/:

- **Non-reference apps:** messages are stored like live traffic.
- **Reference apps:** only their system, body and station facts are kept, for pairing. Retention would delete their messages straight away.
- **Re-running:** finished files are skipped and only data that's missing is added, so it's safe to run again, e.g. the next day for the newest file.
- **Pairing:** paused while an import runs.

A second pass, `import-archive --reference --until <yesterday or earlier>`, stores
the reference apps' own messages for those days as well. It replaces whatever
partial reference-app data is held for them, and can be resumed. Afterwards,
`pair-backlog` pairs all the imported history in one go.

Run it on the Docker host as a one-off container:

    docker --context dockerhost compose run -d --no-deps --name eddn-validator-archive migrate import-archive --workers 8
    docker --context dockerhost logs -f eddn-validator-archive
    docker --context dockerhost rm eddn-validator-archive      # when finished

### Changing the `messages` table

`messages` is partitioned by day and written to constantly. Never run DDL on the
parent table (`ALTER TABLE messages`, `CREATE INDEX … ON messages`) while the
listener or an import is running: it queues every insert behind it. Add indexes
per partition, as `create-indexes` does (`src/eddn_validator/indexes.py`); new
partitions get them automatically when the worker creates them.

## Development

```
uv venv --python 3.11
uv pip install -e '.[dev]'
uv run pytest
uv run eddn-validator listener --dry-run --seconds 30
```

## Related

The validator grew out of checking the EDDN uploads of [ED Outrider](https://github.com/weslocke/ED-Outrider),
a companion app for Elite Dangerous.

## Licence

GNU General Public License v3.0 or later: see [LICENSE](LICENSE).

The sample EDDN messages in `tests/fixtures/eddn/` are copied from the
[EDDN project](https://github.com/EDCD/EDDN)'s test messages and remain under
EDDN's BSD 3-Clause License (see the `LICENSE` file in that folder). Thanks to
EDCD for EDDN itself, and to the developers of E:D Market Connector, EDDiscovery,
EDDLite and EDO Materials Helper, whose apps serve as the reference.
