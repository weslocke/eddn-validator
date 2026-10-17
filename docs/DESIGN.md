# EDDN Validator: design

Status: **built and deployed**, 2026-10-13; last updated 2026-10-14. All six phases are done. §21 replaces the per-range scorecards described below with one card per version. §14–§16 list where the build differs from the plan below, decided while calibrating against real traffic. §17–§20 cover the data-integrity checks, the indexes added for the history import, the code-review fixes and the later site pages.

Research inputs, all in `docs/research/` (working notes, kept out of the public repository):

- `eddn-schemas.md` covers the EDDN schemas, the README rules, gateway and relay behaviour, and the rule IDs `GEN-*`, `COM-*` and so on, which this document refers to.
- `edmc.md` covers E:D Market Connector 6.1.x.
- `eddiscovery-eddlite.md` covers EDDiscovery 19.1.x and EDDLite 3.3.0.

---

## 1. Goal

When a developer of a new EDDN sender (or anyone else) picks an application, a version and a time range on a web page, the page shows a scorecard. The scorecard says how closely that application's EDDN output matches what the reference applications send. It also lists the documented EDDN rules the application breaks and how the application behaves on the network.

**Reference applications** ("the big four", originally the big three). Each one is a separate reference pool:

| Reference app | `softwareName` values | Version format | Notes |
|---|---|---|---|
| EDMC | `E:D Market Connector [Windows]`, `E:D Market Connector [Linux]` | SemVer, e.g. `6.1.2` (pre-release: `6.1.3-beta3`) | Windows and Linux are stored separately and pooled for comparison |
| EDDiscovery | `EDDiscovery` | 4-part, e.g. `19.1.11.0` | Has CAPI. Has per-docking dedup. |
| EDDLite | `EDDLite` | 4-part, e.g. `3.3.0.0` | No CAPI. Last release was 2025-05. |
| EDO Materials Helper | `EDO Materials Helper` | SemVer, e.g. `3.15.6` | Added 2026-10-13 as a mature, high-volume sender. Releases often, sometimes several a week. Research: `docs/research/edo-materials-helper.md`. |

Constraints:

- The web page is read-only. Its only inputs are the application, the version and one of the fixed ranges.
- The database account the web side uses has SELECT permission only.
- Scorecards can be shared by URL, and their results are cached.

---

## 2. What the research changed

These findings shape the rest of the design:

1. **Almost everything on the relay has already passed the JSON schema.** The value is in the README-only rules: about 60 machine-checkable rules are listed in `eddn-schemas.md` §23. It is also in matching the behaviour of the reference apps.
   - *Correction, 2026-10-14:* the live gateway now accepts arrays with repeated items, which the schemas forbid ("accepted temporarily", eddn.edcd.io). Rule GEN-040 checks for them.
   - The deployed gateway is ahead of every public branch, so the public `Settings.py` is no longer a reliable list of accepted schemas. See `docs/research/edcd-github-updates.md`.
2. **The reference apps disagree with each other a lot.**
   - EDD and EDDLite use a field **allowlist**, so they drop `Taxi`, `Multicrew`, `WasFootfalled` and any new Frontier field. EDMC uses a **blocklist** and passes those through.
   - Ship names differ in case: EDMC sends `cobramkiii`, EDD sends `CobraMkIII`.
   - Commodity `statusFlags` differ, and so does sort order.
   
   "Expected" (meaning all of them send it) therefore covers a fairly small core. Everything else is reported as a frequency comparison, and the comparison has to treat case and order as normalisation questions rather than errors.
3. **The reference apps break documented rules too.** For example:
   - EDD and EDDLite do not remove `Int_PlanetApproachSuite` (OUT-001).
   - EDMC sends empty `commodities` lists.
   - EDD sends `stationType: "Megaship"` rather than `MegaShip`.
   
   Documented rules are scored **absolutely**: a rule is a rule. The scorecard shows the reference apps' own failure rate next to each rule for context. A side benefit is that the scorecards for the big three themselves become a list of bugs that can be reported upstream.
4. **EDMC re-sends late.** Its persistent queue re-sends failed messages at the next start-up, possibly days later. Because of a bug it also retries HTTP 400 rejections indefinitely. Shipyard dedup is broken as well, so the same shipyard is re-sent on every `Shipyard` event. Stale and duplicate checks therefore have to be **relative to a reference baseline**, not absolute.
5. **The relay changes the `uploaderID` hash every 3 minutes.** Nothing can be tracked per uploader. All behaviour checks key on `softwareName`, version and content.
6. **The relay drops messages for slow subscribers** (`SNDHWM 500`). The listener must never block on the database.
7. **Releases:**
   - Pre-release status is not in the EDD or EDDLite version string. Only GitHub's `prerelease` flag shows it.
   - EDD 19.1.7 was withdrawn: it has a tag but no release.
   - EDDLite 3.1.0 installs may report `3.0.0.0`.
   
   So the reference versions come from the GitHub **releases** API, not from tags.
8. **Game branch:** `gameversion` 4.x and `CAPI-Live-*` mean Live, and 3.x and `CAPI-Legacy-*` mean Legacy. An empty or missing `gameversion` is classed as Unknown. EDMC does not filter Legacy data at all.

---

## 3. Architecture

```
                        tcp://eddn.edcd.io:9500 (ZeroMQ PUB, zlib JSON)
                                       │
                         ┌─────────────▼─────────────┐
                         │  listener (systemd)       │  recv thread → bounded queue
                         │  decompress, classify,    │  → writer thread: COPY batches
                         │  per-message analysis     │    (~1 s or 500 rows)
                         └─────────────┬─────────────┘
                                       │ rw role
┌──────────────────────────────────────▼──────────────────────────────────────┐
│ PostgreSQL 15                                                               │
│  messages (partitioned) · apps · app_versions · ref_versions · facts_*      │
│  pair_results · ref_profiles · scorecards                                   │
└──────▲───────────────────────────────────────────────────────────▲──────────┘
       │ rw role                                                   │ SELECT-only role
┌──────┴──────────────────────────────┐              ┌─────────────┴───────────┐
│ worker (systemd), scheduled jobs:   │              │ web (FastAPI, uvicorn)  │
│  retention (hourly)                 │              │ reads scorecards + app  │
│  github release poll (6-hourly)     │              │ list only; no compute   │
│  facts + pairing (every 5 min)      │              └─────────────▲───────────┘
│  reference profiles (hourly)        │                            │
│  scorecards (tiered schedule)       │                   reverse proxy (TLS,
└─────────────────────────────────────┘                   rate limit, cache)
```

There are three processes in one Python package, `eddn_validator`.

- **The web process only reads finished scorecards.** It does no scoring, so it does almost no work for each request and has no expensive endpoint that could be abused.
- **All scoring happens in the worker.** The worker precomputes a scorecard for every active app version and range.

Stack:

- Python 3.11 or later (pinned to 3.11 with `uv`)
- `pyzmq`
- `psycopg` 3 (for `COPY` and pooling)
- FastAPI with Jinja2 templates (autoescape on)
- `PyYAML`
- `httpx` (for GitHub)
- `pytest`
- Plain numbered SQL migrations with a small runner. There is no ORM.

---

## 4. Ingest (listener)

1. **Receive.** A SUB socket subscribes to `''`. Each frame is decompressed with zlib, then parsed as JSON. Parsing errors are counted, never fatal. TCP keepalive is on. If no message arrives for 5 minutes, the listener reconnects.
2. **Classify.** Every message gets these fields:
   - `schema_name` (e.g. `journal/1`) and `is_test` (the `/test` suffix)
   - `event`
   - `event_type`: `journal/1:FSDJump`, `commodity/3`, `codexentry/1` and so on
   - `branch`: `live`, `legacy` or `unknown`, from `gameversion` (see §2.8)
   - `source`: `journal` or `capi`, from a `CAPI-*` `gameversion`. This applies to commodity, outfitting and shipyard.
   - `app_id`, from `softwareName`
   - `is_big3`: true if `softwareName` is one of the four reference names
   - `msg_ts` (`message.timestamp`) and `gw_ts` (`header.gatewayTimestamp`)
   - `subtype` (§6.2)
   - `content_hash`: sha256 of the canonical JSON of `$schemaRef` and `message`, after dropping fields that legitimately vary between re-sends. This is used for the duplicate check.
3. **Per-message analysis** (field paths and documented-rule findings) is **not** stored. The scorer derives both from `payload` when it builds a card. This keeps rows small, and means changing a rule never requires a backfill.
   - Field paths are normalised: arrays become `[]`, and numeric object keys become `{n}`. Examples: `Factions[].ActiveStates[].State:string` and `StarPos[]:number`.
   - The per-message rules are the ones in `eddn-schemas.md` §23.
4. **Write.** A bounded in-memory queue feeds a writer thread that uses `COPY` in batches. If the queue fills because the database is slow, rows are dropped, the drop is logged and counted, and the receive thread keeps draining the socket. `apps` and `app_versions` (first seen, last seen, count) are upserted once per batch.

Strings from EDDN (`softwareName`, `softwareVersion` and every message field) are **untrusted input**:

- They are stored as-is.
- They are length-capped in the indexed columns: 128 characters for the name, 64 for the version.
- They are only ever rendered with escaping (§9).

---

## 5. Storage

### 5.1 Tables

| Table | Purpose | Key columns |
|---|---|---|
| `messages` | Every relayed message. Partitioned **by day** on `gw_ts`. | `id`, `gw_ts`, `received_at`, `msg_ts`, `app_id`, `software_version`, `schema_ref`, `schema_name`, `is_test`, `event`, `event_type`, `subtype`, `branch`, `source`, `game_version`, `game_build`, `is_big3`, `content_hash`, `payload jsonb` (the full envelope, as relayed) |
| `apps` | One row per distinct `softwareName` | `id`, `software_name`, `ref_group` (`EDMC`/`EDDiscovery`/`EDDLite`/null), `first_seen`, `last_seen` |
| `app_versions` | One row per app and version | `app_id`, `version`, `first_seen`, `last_seen`, `msg_count`, `is_reference` (maintained by the GitHub poll) |
| `ref_versions` | GitHub releases of the reference apps | `ref_group`, `tag`, `version` (mapped to header format), `published_at`, `prerelease` |
| `facts_system` | Static system facts from reference apps | `system_address` PK, `star_system`, `star_pos`, `obs_count`, `conflict` bool, `last_seen` |
| `facts_body` | Static body facts from reference `Scan` messages | (`system_address`, `body_id`) PK, `body_name`, `static jsonb`, `obs_count`, `conflict`, `last_seen` |
| `facts_station` | Station facts | `market_id` PK, `station_name`, `station_type`, `star_system`, `system_address`, `system_seen`, `last_seen` (system columns added in migration 0008, §17) |
| `facts_market` | Recent market snapshots from reference apps (kept only for the pairing window) | `market_id`, `observed_at`, `source`, `commodities jsonb` |
| `pair_results` | One row per paired comparison | `message_id`, `gw_ts`, `pair_kind`, `fields_compared`, `mismatches jsonb` |
| `ref_profiles` | Reference statistics per bucket | (`event_type`, `subtype`, `branch`, `ref_group`) → `profile jsonb` |
| `scorecards` | Finished scorecards | (`app_id`, `version`, `range_days`) → `computed_at`, `card jsonb` |

### 5.2 Retention (hourly job)

| Data | Rule |
|---|---|
| Reference-version messages from the big three | Keep the newest **3000 per (`softwareName`, `event_type`, `branch`)**, using the current reference-version list |
| Big-three messages from **non-reference** versions (old releases, betas, dev builds) | Keep the newest **5000 per (`softwareName`, version, `event_type`, `branch`)** |
| Messages from all other apps | Kept for 6 months. Whole daily partitions are dropped, which is cheap and avoids table bloat. |
| `facts_system`, `facts_body`, `facts_station`, `pair_results` | Rows with `last_seen` older than 6 months are deleted, on the same cycle |
| `facts_market` | Kept for 2 hours (it only serves the pairing window) |
| `scorecards` | Deleted when the app version has had no messages in 180 days |

Disk use is measured during Phase 1 (§11) before it's needed. The `/stats` view in the worker log reports bytes per day per table.

---

## 6. Analysis model

### 6.1 Layer 1: documented rules

The rules are kept in `config/rules.yaml`. Each one has an ID, the schemas it applies to, a severity (`error`/`warning`/`info`), a source citation and a check. There are three kinds of check:

- **Per-message** checks are evaluated by the scorer from `payload`. Examples:
  - GEN-020: a recursive check for `_Localised` keys, which catches `Genuses[].Genus_Localised`, the gateway's gap in journal/1.
  - GEN-001 and GEN-002: `gameversion` or `gamebuild` missing.
  - GEN-004: the CAPI endpoint does not match the schema.
  - GEN-025 and GEN-026: `odyssey` sent with a Legacy version, or `odyssey:true` with `horizons:false`.
  - COM-001, COM-002, COM-003, COM-007, OUT-001, CDX-001, CDX-002, FSD-001, APS-001, FCC-001 to FCC-004, and others.
  - GEN-030, `StarPos` inside the `SystemAddress` id64 cube (§6.5).
  - JNL-001, CodexEntry sent on journal/1.
- **Reference-fact** checks are evaluated by the pairing job (§6.4). GEN-031 is one: name, address and position disagree with known system facts.
- **Behavioural** checks are evaluated by the scorer over the whole sample (§6.6).

Rules with no reliable check (for example the CAPI lag check, or OUT-002, which needs a curated SKU list) are documented in the YAML but disabled until a check exists.

### 6.1a Schemas the design doesn't know about

EDDN keeps adding schemas. For example, `scanorganic/1` and `outfitting/3` have been accepted since July/August 2026, though no public branch registers `outfitting/3`. The listener stores every schema it receives, and every schema is evaluated:

- **Sent by any reference app:** the schema is taken as valid. It becomes a normal event type with reference profiles, and is scored like the rest. Only the generic per-message rules apply until it has its own documented rules (GEN-020 `_Localised`, GEN-030 id64, timestamps, `gameversion`, and so on).
- **Seen only from non-reference apps:** the schema is flagged as **"Unrecognised schema: not sent by any reference app"**, a warning shown on the scorecard. The messages are still checked against the generic rules, but there's no reference to compare structure or values against.
- **Seen from no one until now:** the scorecard and the worker log say so, as a prompt to add documented rules for it to `config/rules.yaml`.

### 6.2 Buckets and subtypes

Comparisons are made within a **bucket**, which is (`event_type`, `subtype`, `branch`). Subtypes are defined in `config/subtypes.yaml` as simple predicates. The initial set:

| event_type | Subtypes |
|---|---|
| `journal/1:Scan` | body kind (`star` = has `StarType`, `planet` = has `PlanetClass`, `belt` = neither) × scan class (`Detailed`, `AutoScan`, `NavBeacon*`, `Basic`) |
| `journal/1:FSDJump`, `Location`, `CarrierJump` | `populated` (has `Factions`) / `unpopulated`. For Location, also `docked` / `not docked`. |
| `journal/1:Docked` | `fleetcarrier`, `planetary` (surface/crater/settlement types), `orbital`, `other` |
| `commodity/3`, `outfitting/2`, `shipyard/2` | `journal` / `capi` (from `gameversion`) |
| everything else | `default` |

If a bucket has too little reference data, the comparison falls back to the parent bucket (`subtype = *`), and the scorecard notes that it did.

### 6.3 Layers 2 and 3: structure and values against reference profiles

The hourly **reference profile** job builds statistics per bucket from reference-version messages. The figures are kept per reference app, so EDMC (both platforms pooled), EDD and EDDLite each have their own.

**Structure: field presence.** For each normalised path, it records the fraction of the app's messages in which the path appears.

- **Expected**: every reference app that has enough data in this bucket sends the path in at least 95% of its messages. A reference app with no data in the bucket (EDDLite for CAPI data, for instance) is not counted.
- **Common**: sent by at least one reference app in at least 50% of messages.
- **Rare**: seen in reference data, but below 50% in every reference app.
- **Unseen**: never seen in reference data.

How each class is scored against the app under test:

| Class | Scoring |
|---|---|
| Expected path missing | Penalised in proportion to the app's miss rate |
| Common or rare path | Not penalised. Shown as a side-by-side frequency table (app % vs EDMC % / EDD % / EDDLite %). |
| Unseen path, allowed by the schema | Info only (it may be a new Frontier field that EDMC hasn't seen yet) |
| Unseen path that the schema would reject | Not possible, because the gateway already rejects it |
| Unseen path under journal/1 `additionalProperties:true` | Warning. A personal-data denylist match (GEN-034) is an error. |

**Values.** Each path also gets a value profile:

- **Type distribution.** `int` and `float` are both treated as `number`, because EDD writes integral doubles as `0.0`. The exception is where the schema says `integer`.
- **Strings:** if a path has at most about 200 distinct values across at least 500 samples, it is treated as an **enum**, for example `StarType`, `PlanetClass`, `StationType` and economy symbols. Otherwise a **shape signature** is used (letters, digits, symbols and the `$…_name;` form), keeping the top N shapes.
- **Numbers:** min, max, p1 and p99, whether values are integral, and decimal precision.
- **Arrays:** length distribution.

Checks on the app's values:

| Check | Severity | Notes |
|---|---|---|
| Type not seen in reference data | error | For example, text in a numeric field |
| Value outside an established enum | warning | Matched case-insensitively first. A case-only difference is reported as **info**, naming which reference app uses which case (`Megaship` vs `MegaShip`, `cobramkiii` vs `CobraMkIII`). |
| Number outside the reference range by more than a margin | warning | |
| String shape outside the reference shapes | info | For example, a `$…_name;` symbol left in a commodity name |

### 6.4 Layer 4: same-object pairing

Big-three messages roll out of retention within hours, so pairing cannot wait until scorecard time.

- **Facts.** The worker runs every 5 minutes. It updates the `facts_*` tables from new reference-app messages, and keeps observation counts. A fact on which two reference apps disagree is marked `conflict` and not used.
- **Pairing.** In the same run, the worker pairs non-reference messages that are 10 to 70 minutes old with the facts. Waiting 10 minutes means the reference side has had time to arrive, even if the new app reported first. Results go to `pair_results`.

The fields compared are configured in `config/pairing.yaml`. **Only static fields are compared**:

| Pair kind | Key | Compared |
|---|---|---|
| system | `SystemAddress` | `StarSystem`/`System`/`SystemName`, and `StarPos` (exact on the 1/32 ly grid) |
| body | (`SystemAddress`, `BodyID`) | `BodyName`, `StarType`/`PlanetClass`, `MassEM`/`StellarMass`, `Radius`, `SurfaceGravity`, `Atmosphere*`, `Composition`, `Materials`, `Rings`, `RotationPeriod`, `AxialTilt`, `SemiMajorAxis`, `Eccentricity`, `OrbitalInclination`, `Periapsis`, `OrbitalPeriod`, `TidalLock`, `Landable`, `Volcanism`, `TerraformState`. Floats use a relative tolerance of 1e-9. **Excluded, because they change over time:** `MeanAnomaly`, `DistanceFromArrivalLS`, `WasDiscovered`, `WasMapped`, `WasFootfalled`. |
| station | `MarketID` | `StationName`, `StationType` (case-insensitive), and the star system it is in, for stations that don't move (§17) |
| market | `marketId` within ±5 min, same `source` | commodity name set (case-insensitive) and buy/sell prices. These are **soft**: they're reported, but weighted lower, because a market can tick between two players' snapshots. |
| navroute hop | each `Route[]` `SystemAddress` | name, `StarPos`, `StarClass` against `facts_system` and the star facts |

Faction and BGS pairing (influence and states within the same tick) is deferred: tick detection is unreliable. See §12.

### 6.5 id64 cross-check (GEN-030)

`SystemAddress` encodes the system's sector and boxel. Decoding it gives a cube that `StarPos` must fall inside. The check needs no reference data at all, so it works for every message, including Legacy and rarely visited systems.

Before this check is turned on, the decoder is **validated against `facts_system`**: every reference-app (address, position) pair must pass. If it doesn't, the check stays off.

### 6.6 Layer 5: behaviour

All behaviour figures are computed for the app's sample and for the reference apps over the same window. **Only the excess over the worst reference app's rate is penalised.**

**EDO Materials Helper is left out of the stale and duplicate baselines** (decided 2026-10-13). It re-sends the newest journal with no age limit on every start-up, so its stale rate would make the baseline far too lenient. It still counts as a reference for structure, values, schemas and pairing.

| Check | Measure |
|---|---|
| Duplicates | The share of messages whose `content_hash` repeats within 24 h for the same app and version |
| Stale | `gw_ts − msg_ts` > 1 h (with distribution buckets: 5 min / 1 h / 24 h / older) |
| Clock skew / fake `Z` | `msg_ts > gw_ts + 5 min`. Offsets close to a whole number of hours are flagged separately as a probable local time labelled UTC (GEN-022). |
| Bursts | More than N messages of one event_type in one second from the same app and version (a sign of journal replay) |
| Unbatched FSS signals | FSD-002: runs of one-signal `fsssignaldiscovered` messages for the same system within a second |
| Legacy and beta leakage | The game-version panel (§7) counts and shows Legacy data, beta builds on live schemas (GEN-014), and missing `gameversion` |
| Out-of-order location context | Low weight. Messages that claim a system the same app's preceding FSDJump (within seconds) didn't report. This is loose, because uploaders cannot be told apart. |

### 6.7 Sampling and confidence

- **Scoring sample:** the newest **5000** messages per (`event_type`, `subtype`) for the selected app version, within the range. Counts and behaviour rates always use the **full** range.
- **Confidence:**
  - **fewer than 5** messages: **Insufficient data**. Counts are shown, but there is no score and no weight in the grade.
  - **5–29** messages: **Low confidence**. The bucket is scored and flagged, and gets half weight.
  - **30 or more:** scored normally.
- **Reference side:** a bucket needs at least 50 reference messages before it defines "expected" fields or enums. Otherwise only layers 1 and 5 apply.
- **Exception:** documented-rule failures are always listed, however small the sample.

### 6.8 Scoring

**Bucket score** (0–100) is a weighted mean of the layers that have data. Weights are configurable in `config/scoring.yaml`, and when a layer has no data the remaining weights are rescaled:

| Layer | Default weight |
|---|---|
| 1. Documented rules | 35 |
| 2. Structure (expected fields) | 25 |
| 3. Values | 20 |
| 4. Pairing | 10 |
| 5. Behaviour | 10 |

Within a layer, penalties come from failure rates, weighted by severity: an error counts in full, a warning counts half, and an info counts nothing (it is shown, not scored).

**Event-type score** is the message-weighted mean of its subtype buckets.

**Overall grade** is the mean of the event-type scores, with each event type weighted equally. Low-confidence types get half weight. It maps to a letter: A ≥ 90, B ≥ 80, C ≥ 70, D ≥ 60, F otherwise.

**Coverage** (event types the app never sent) is **not** counted in the grade. A specialised tool shouldn't be penalised for sending only what it is built for. It is still reported prominently, so that a missing type that *ought* to be there gets noticed. For each event type the reference apps sent in the range but this app didn't, the scorecard shows:

- how many reference messages there were;
- which reference apps send it (some are CAPI-only or EDMC-only, for example).

This lets a developer see straight away whether the gap is deliberate or a bug.

---

## 7. Scorecard contents

```
┌──────────────────────────────────────────────────────────────────────────────┐
│ MyNewTool 0.4.2            Range: 7 days   Computed: 2026-10-13 14:05 UTC     │
│ Overall: B (84.1)    12,406 messages · 9 event types · 1 low-confidence       │
├──────────────────────────────────────────────────────────────────────────────┤
│ Game versions: Live 4.4.1.0 97.8% · Legacy 3.8 1.9% · "" 0.3%                │
│ Schemas: live 99.1% · /test 0.9%      Branch mix flag: ⚠ sends Legacy data    │
├──────────────────────────────────────────────────────────────────────────────┤
│ Event type             Msgs   Score  Rules Struct Values Paired Behav  Conf   │
│ journal/1:FSDJump      4,210   91     100    95    88     97     70    ok     │
│ journal/1:Scan         6,005   78      92    71    80     94     —     ok     │
│ commodity/3 (journal)    611   88     ...                                     │
│ fssbodysignals/1          12   —      ...                            low      │
│ navroute/1                 3   —                                    insuff.   │
├──────────────────────────────────────────────────────────────────────────────┤
│ Not sent (seen from reference apps in range): codexentry/1, approachsettle... │
└──────────────────────────────────────────────────────────────────────────────┘
```

Each event type expands to show:

- **Rule failures:** rule ID, description and citation, the app's failure rate, and each reference app's own rate for comparison.
- **Field table:** each path's class, the app's frequency, and each reference app's frequency. Missing expected fields and unseen fields are highlighted.
- **Value findings:** type mismatches, values outside an enum (with case notes), and out-of-range values.
- **Pairing:** the number of objects paired, the agreement rate, and the fields that disagree most.
- **Behaviour:** duplicate, stale, skew and burst rates against the reference baseline.
- **Examples:** up to 3 offending messages per finding, as escaped JSON. `uploaderID` is already a hash from the relay. See Q3.

Versions are never mixed. The version dropdown lists the app's versions newest-first by `last_seen`, with message counts.

---

## 8. Web

- **Pages:**
  - `GET /` shows the picker.
  - `GET /card/{app_id}/{days}?v={version}` shows a scorecard. `days` must be one of 1, 7, 14, 30, 90 or 180, and anything else returns 404.
  - `GET /apps` and `GET /about/{layer}` were added later (§20).
  - `GET /about` explains the method.
  - Scorecards are shareable through these URLs.
- **Picker:** the full app and version list is **embedded in the page as JSON**, a few hundred entries at most. A small inline script filters it as the user types. Nothing typed is sent to the server, and **the server never sees free text**.
- **Unknown values:** a URL whose `app_id` or `version` isn't in `apps` or `app_versions` returns 404.
- **Database access:** only `scorecards`, `apps` and `app_versions`, through `eddn_ro`, which has SELECT permission only. The web container is never given the `eddn_app` credentials.
- **Caching:** an in-process TTL cache of 60 s, plus `Cache-Control: public, max-age=300` for the reverse proxy. Scorecards change only when the worker recomputes them.
- **Recompute schedule (worker):**
  - `1d` cards every 15 minutes;
  - `7d` and `14d` hourly;
  - `30d` and longer every 6 hours.
  
  A card is recomputed only if its app version has new messages since the last run.
- **Security:**
  - Jinja autoescape everywhere, and JSON embedded with `|tojson`.
  - A strict CSP: `default-src 'none'`, the inline script and style allowed by a per-request nonce, `img-src 'self' data:`, and no third-party assets.
  - Security headers: HSTS through the proxy, `X-Content-Type-Options` and frame-ancestors none.
  - The reverse proxy (nginx or Caddy) handles TLS and rate limiting per IP.

---

## 9. GitHub release polling

A worker job runs every 6 hours. It uses the unauthenticated releases API, or a token if one is configured.

| Ref group | Repo | Tag → header version | Take |
|---|---|---|---|
| EDMC | `EDCD/EDMarketConnector` | `Release/6.1.2` → `6.1.2` | latest **3** with `prerelease=false`, `draft=false` |
| EDDiscovery | `EDDiscovery/EDDiscovery` | `Release_19.1.11` → `19.1.11.0` | latest 3 non-pre-release |
| EDDLite | `EDDiscovery/EDDLite` | `Release_3.3.0` → `3.3.0.0` | latest 3 non-pre-release, plus a known-alias map (`3.1.0` → also `3.0.0.0`) |
| EDOMH | `jixxed/ed-odyssey-materials-helper` | `3.15.6` → `3.15.6` | latest 3 non-pre-release |

Releases API objects only. A tag without a release, such as the withdrawn EDD 19.1.7, is ignored. The mappings and aliases live in `config/reference_apps.yaml`.

**Residual risk:** anyone can send messages claiming to be `EDDiscovery 19.1.11.0`. The defences are:

- Reference profiles use robust statistics (percentiles, 95% presence thresholds).
- Facts need agreement, and conflicting facts are dropped.
- Neither defence is proof against a determined forger. This is accepted for now.

---

## 10. Deployment

**Target: Docker Compose on a home server** (Ubuntu 22.04, Docker 29.5 and Compose v5).

- **One image, several services.** The same image runs `listener`, `worker` and `web`, each with a different command. A one-shot `migrate` service applies SQL migrations before the others start. The worker runs its own scheduler loop, so there's no cron.
- **Database:** the server's existing `postgres` container (pgvector/pg15), whose data is on a separate data drive. It gets a dedicated `eddn_validator` database with its own roles.
- **Configuration:** `config/*.yaml` is **baked into the image**. Bind mounts would point at the server's filesystem, not this checkout, because the deploy goes through a remote Docker context. Changing the configuration only rebuilds the last image layer, which takes seconds.
- **Deploying from the dev machine:**
  - Use a Docker context over SSH: `docker context create dockerhost --docker host=ssh://user@your-docker-host`.
  - Then `docker --context dockerhost compose up -d --build`. This sends the local source to the server, builds there, and restarts only the services whose image or configuration changed.
  - A web or worker deploy never interrupts the listener.
  - Restarting the listener loses a few seconds of relay traffic, because the relay doesn't replay.
- **Root disk:** the server's root filesystem is nearly full, and Docker images and logs live there. So:
  - containers use `json-file` logging with `max-size`/`max-file`;
  - old images are pruned after deploys;
  - nothing large is written inside containers.
- The systemd notes below apply only if Docker is ever dropped.
- **Configuration:**
  - `.env` holds the DSNs (`DATABASE_URL_RW` and `DATABASE_URL_RO`), the relay URL and an optional `GITHUB_TOKEN`. `.env.example` is committed, and `.env` is git-ignored.
  - `config/*.yaml` holds the rules, subtypes, pairing, scoring and reference apps.
- **Database roles:**
  - `eddn_app` owns the database. The migrations, listener and worker use it. A separate read-write role would add nothing, because the worker needs DDL anyway for partition maintenance.
  - `eddn_ro` is the web role, with SELECT on the scorecard and app tables only.
  - A one-off `bootstrap` command creates both roles and the database, using admin credentials that only the `migrate` service receives.
- **Python:**
  - `requires-python = ">=3.11"`.
  - Development uses `uv` with 3.11 pinned.
  - The code also runs on 3.14.
  - The test host has system Python 3.10.12, which reached end of life in October 2026. That interpreter is not touched. `uv python install 3.11` puts a managed interpreter in user space, and `uv sync` builds the venv from it. The systemd units point at the venv's Python, so no system Python upgrade is needed.

Planned layout:

```
pyproject.toml  .env.example  .gitignore
config/   rules.yaml subtypes.yaml pairing.yaml scoring.yaml reference_apps.yaml
migrations/   0001_init.sql …
src/eddn_validator/
    config.py db.py
    listener/   relay.py classify.py writer.py
    analysis/   paths.py rules.py id64.py profiles.py values.py
    worker/     scheduler.py retention.py github.py facts.py pairing.py scorer.py
    web/        app.py templates/ static/
tests/          (fixtures: EDDN repo test messages + captured relay samples)
deploy/         systemd units, nginx example
docs/
```

---

## 11. Build phases

| Phase | Deliverable | Why this order |
|---|---|---|
| 1 | Schema and migrations, the listener, retention, and `apps`/`app_versions`. Run it continuously. | Reference profiles need data, and the less common event types (dockingdenied, fcmaterials and so on) take days to build up. Also measures volume and disk use. |
| 2 | The GitHub poll and the rules engine (per-message `findings` at ingest), with unit tests against the EDDN repo fixtures | Pure functions, easy to test |
| 3 | Reference profiles, facts and pairing, and id64 validation | Needs Phase 1 data |
| 4 | The scorer and `scorecards`, plus a CLI to print a scorecard | Lets the scoring be tuned before any UI exists |
| 5 | The web UI | |
| 6 | Hardening: proxy config, CSP, load test, and the systemd units for the internet-facing host | |

---

## 12. Deferred / later ideas

- Faction and BGS value pairing (influence, states) once a tick detector exists.
- Trend view: a version's score over time, or between versions.
- Self-test: a sender submits to `/test` and gets a card within minutes. This mostly works already, since `/test` traffic is stored and scored.
- A curated SKU and module list for OUT-002, and a ship list for SHP-001.
- Watching for `outfitting/3` going live (it is currently master-only in the EDDN repo).
- **Older reference facts.** Pairing can only check places a reference app has reported within the six-month window. If that gap turns out to matter, import only the reference apps' system, body and station facts (no messages) from older edgalaxydata.space archives. Measure first: how many other-app reports go unpaired because no reference fact exists.
- **External reference data was considered and dropped (2026-10-14).** Spansh is built from the same EDDN feed with looser filtering than the reference-app rule used here, so it would add little and could be circular, checking an app against its own earlier reports. Generated system names are already covered by the system-ID checks (§17). EDSM is never used: it imports EDDN data without filtering.

---

## 13. Open questions

- **Q1: Coverage in the grade?** *Decided:* not counted in the grade, but shown prominently with reference counts (§6.8).
- **Q2: Old and beta versions of the big three.** *Decided:* keep the newest 5000 per (app, version, `event_type`, `branch`).
- **Q3: Example messages on the scorecard.** *Decided:* yes, up to 3 per finding.
- **Q4: Where the listener runs.** *Decided:* Docker on the home server, the same host as Postgres.

---

## 14. Calibration on real traffic (2026-10-13)

These changes to the plan above came from scoring real apps and checking every surprising finding.

**Rules**
- **CDX-002 is disabled.** The journal `CodexEntry` event now carries `BodyID` itself, so the README rule (BodyID only alongside BodyName) is out of date. Every reference app was "failing" it.
- **Squadron carriers** have 4-character IDs (FCJ-003).
- **GEN-026** (odyssey true with horizons false) skips CAPI data. There, horizons is worked out from the station, as the commodity README says.
- **GEN-030 (StarPos inside the boxel its SystemAddress encodes)** is an **error**-level per-message rule. The decoder put all 14,791 positions the reference apps reported inside the right boxel, with zero tolerance.

**Pairing**
- Station types are normalised (EDDiscovery writes `Bernal` for `Ocellus`, and `Unknown` for types it doesn't know).
- Station names that are localisation symbols (`$Operations_…`, mission megaships) are skipped.
- Pairing waits until the facts have caught up, so it never compares against half-built facts.

**Values**
- **Open-ended fields are never checked against a value set or range:** names, timestamps and IDs. Navigation-beacon scans repeat one timestamp and system for dozens of bodies, which made those look like closed sets.
- **Closed-set values are accumulated** (`ref_values`) from every reference message over six months, so rare but real values become known.
- **An unfamiliar value is a warning only if its format is new.** A localised name or free text where references send `$symbol;` or CamelCase counts. A new member of a known form (`NaturalDisaster`, star type `H`) is just noted.
- **Numeric warnings are only for units-scale or sign errors** (beyond 100 spans). Physical and economic values have long tails.

**Behaviour**
- **Baselines are per event type.** For example, about 15% of `fssallbodiesfound` messages are duplicates for every app.
- **EDO Materials Helper is left out of the stale and duplicate baselines.**

**Scoring**
- **Pairing weight raised from 10 to 20** (rules 30, fields 20, values 20, pairing 20, behaviour 10). On real data it's the strongest signal, and it's what EDDN consumers care about.
- **The overall grade weights event types by message count** (low confidence counts half). With equal weights, Elite Warboard got an A even though 28% of its largest event type had wrong positions.

**Not stored**
- Per-message paths and findings are computed from `payload` by the scorer. They aren't stored at ingest (§4.3).

**Running**
- Each worker run scores the app versions with new messages and refreshes every card at least every 6 hours.
- The first full pass scored 167 app versions (899 cards) in about 6 minutes.

---

## 15. Keeping the reference apps' history (2026-10-14)

**What changed:** the reference apps' messages are no longer cut to the newest 3,000 per event type. Every message is kept for 183 days.

**Why:** facts, profiles and rules can then be rebuilt from history whenever their logic changes. Estimated total at the full window: about 645 GB (the owner's limit is 700 GB).

**Size guard:**
- If the database passes 650 GB (`config/retention.yaml`), the hourly retention job removes the reference apps' oldest days, oldest first, and compacts each of those day partitions. *Changed 2026-10-14:* the trigger is now free space on the database drive (below `min_free_gb`, 60 GB; the owner keeps at least 50 GB free), seen through a read-only mount in the worker; the 650 GB cap is gone (`size_cap_gb` is optional), and the newest 7 days are never trimmed.
- Other apps' messages are never touched.
- The guard is skipped while an archive import is running.

**Keeping the jobs fast at this volume:**
- **Profiles** read each (reference app, event type, branch) bucket newest-first through `messages_big3_idx`, within 30 days, and only the current reference versions.
- **Behaviour baselines** use the last 2 days.
- **The scorer** samples per event type through `messages_app_idx`.
- **Scorecards for the reference apps themselves** cover 1 and 7 days only.
- **The facts job** skips archive-imported rows (`received_at = gw_ts`), whose facts were folded in during the first import pass.

**Archive history** is filled in two passes:
1. **`import-archive`:** other apps' messages, plus the reference apps' facts.
2. **`import-archive --reference --until <before today>`:** the reference apps' own messages. It first removes the partial reference-app data held for those days. Rows it has already imported are kept, so it can resume. Progress is recorded in `archive_reference_imports`.

Pairing pauses only during pass 1. `pair-backlog` pairs the imported history without the worker's per-run time limit.

---

## 16. Rules added from EDCD GitHub research (2026-10-14)

Source: `docs/research/edcd-github-updates.md`.

| Rule | Severity | What it checks |
|---|---|---|
| GEN-040 | error | No repeated items in arrays the schemas mark unique. On real traffic it fires for most busy senders, the reference apps included. |
| GEN-014B | warning | Known beta builds on live schemas. Frontier's betas no longer say "beta". The build Frontier reused for a live release (r330116) is excluded. |
| GEN-041 | info | MarketID 2⁶⁴ − 1 (the game's no-market value). |
| OUT-003 | warning | Repeated module `id`s in `outfitting/3`. Names legitimately repeat. |
| SCO-001…005 | | `scanorganic/1`: `Body` not renamed, empty `BodyName`, latitude/longitude out of range (also codexentry and approachsettlement), body outside the system (info), Variant not of the Species (info). |

**Behaviour:** identical messages re-sent 25–150 s apart are shown as "retrying sooner than EDDN's 1-minute minimum". They're noted, not scored. Most of EDDI's duplicate docking and location messages are of this kind.

---

## 17. Data-integrity checks (2026-10-14)

EDDN consumers care most about data put in the wrong place: a system name with another system's address, or a body or station filed under the wrong system. These checks target that.

**Checks that need no other app** (per-message rules in `rules.py`, decoding in `id64.py`):

| Rule | Severity | What it checks |
|---|---|---|
| GEN-030 | error | `StarPos` falls inside the boxel its `SystemAddress` encodes (§6.5). Positions too large to be real (garbage or overflowed values) fail instead of crashing the check. |
| GEN-033 | error | A generated (procedural) system name matches its `SystemAddress`. The name decodes to sector, mass code and index (index = a + b·26 + c·26² + n1·26³, 128 boxels per axis, N2 as the high part). Hand-made names (" Sector", " Dark Region", ICZ and named systems) are skipped. Validated on 4.7 million systems with no false failures. |
| GEN-035 | warning | A body's name starts with the system it is reported in, for generated system names. Catches a stale location after a jump. |
| GEN-032 | warning | `StarPos` [0,0,0] for any system other than Sol. |

**MarketID ranges.** These follow EDDiscovery's and Spansh's classification:

| Range | Meaning | Handling |
|---|---|---|
| below 127,000,000 | No real station | GEN-043, warning: test data on a live schema, or an ID damaged in a 32-bit field |
| 127,000,000–127,999,999 | Shared placeholder for instanced Operations stations | GEN-042, info; never used as a station fact |
| 3.4×10⁹ | Rescue ships | Moving station: name and type paired, system not |
| 3.6×10⁹–3.94×10⁹ | Megaships and fleet carriers | Moving station: name and type paired, system not |
| 3.95×10⁹ and above | Colonisation; from 4.2×10⁹, colonised stations, many beyond 32 bits | Normal stations |
| 2⁶⁴ − 1 | The game's "no market" value | GEN-041, info; filtered out before any database write (`facts.db_int`), since it doesn't fit a signed bigint |

An ID beyond 32 bits is real data, not a JavaScript precision bug, so it is never flagged by itself.

**Station system pairing.** `facts_station` records which system each station is in (`star_system`, `system_address`, `system_seen`), learned from reference-app `Docked`, `Location` and settlement messages. When another app reports a station that doesn't move, the system it gives is compared with that fact. Facts learned before migration 0008 are filled in by `backfill-station-systems`.

---

## 18. Indexes for the history import (2026-10-14)

With six months of reference-app history, pairing slowed to a crawl: finding the next unpaired other-app message meant walking every reference row in id order. Two partial indexes fix that:

| Index | Columns | Predicate | Used by |
|---|---|---|---|
| `…_other_id_idx` | `(id)` | `NOT is_big3` | Pairing: the next other-app messages after the watermark |
| `…_live_ref_id_idx` | `(id)` | `is_big3 AND NOT is_test AND received_at <> gw_ts` | Facts job: the next live reference messages, skipping archive rows |

Plus `last_seen` indexes on the three facts tables and `gw_ts` on `pair_results`, for the hourly retention deletes.

**These exist on each partition only, never on the parent table.**
- A parent-level `CREATE INDEX` waits for a lock on all of `messages` and queues every insert behind it. One attempt blocked the listener for 7 minutes.
- `CONCURRENTLY` waits for every open transaction before each phase, which during an import means hours.
- The planner uses each partition's own indexes when it scans that partition, so queries on `messages` get the same benefit.

`create-indexes` (`indexes.py`) builds them one partition at a time with `SET LOCAL lock_timeout '10s'`, retrying a busy partition later instead of queueing. `partitions.ensure_partitions` adds them to each new partition as it is created, while it is still empty.

**Rule for future changes:** no parent-level DDL on `messages` while anything writes to it.

---

## 19. Code-review fixes (2026-10-14)

A multi-agent review found 18 bugs, all fixed with tests (175 tests pass):

- **Bad input that could crash a job:** timestamps outside Python's range (`OverflowError` in `parse_ts`); an unhashable `BodyID`; integer and float overflow in `facts.same`, `valid_pos` and the value accumulator; NUL characters in a version in the URL (now a 404).
- **Listener:** any unexpected error is logged and counted instead of stopping the receive loop. Header strings with lone surrogates or NUL characters are cleaned before they reach a column.
- **Storage:** `register_apps` no longer burns `apps` id sequence values on every batch.
- **Pairing and facts:** an advisory lock stops two pairing runs from overlapping; `pair-backlog` waits for it rather than exiting.
- **Behaviour:** the reference burst count covered all stored history while the other baseline rates covered 2 days, which inflated the burst allowance; it now uses the same 2 days.
- **Archive import:** a line split across a bz2 block boundary is no longer lost, and imports run newest-first, so the most useful days arrive first.
- **Scheduling:** reference apps' own versions are included when choosing which cards are due. An archive import's heartbeat older than 3 hours is treated as a crashed import, so pairing and the size guard can't stay paused forever.
- **Partitions:** creation uses `IF NOT EXISTS` and a lock timeout, so a busy table can't stall the worker.
- **Web:** the last inline `style` attribute was removed so the nonce CSP holds.

Problems found in operation and fixed along the way:
- psycopg connections are autocommit by default with explicit `transaction()` blocks. Before that, nested savepoints held a lock on `messages` for 6 minutes during an import.
- Server-side TCP keepalives are set through libpq options, so a killed container can't leave a COPY session holding locks.
- The reference import pass registers apps on its own connection, so it doesn't hold `apps` row locks for a whole file.
- Facts upserts are sorted by key, which removed deadlocks between parallel import workers.

---

## 20. Site additions (2026-10-14)

- **Versions on each scorecard:** the bottom of every card lists every version of the app seen on EDDN, most recently active first, with grade, score, message count and first and last seen dates. Each links to its own card.
- **Current version beside each name** on the All applications page and in the front page's new-applications list.

- **Front page:** applications first seen in the last 30 days, A–Z, each linking to its shortest card of at least 7 days.
- **`/apps`:** every application A–Z in three columns, with its letter grade and numeric score. Both lists come from the database on each request (through the 60 s cache), so new applications appear without a deploy.
- **Hover explanations:** every underlined field on a scorecard explains itself and, where it helps, breaks the number down (texts in `web_tips.py`). They work on hover, keyboard focus and tap, in a fixed-position box so scrolling tables can't clip them. Event type and message count have none, by request.
- **`/about/{layer}`:** one plain-English page per layer (rules, fields, values, pairing, behaviour). The lists are built from `config/rules.yaml`, `config/scoring.yaml` and the compared body fields (`about.py`), so they can't drift from what is checked. Only the wording is hand-written.
- **Logo and favicon:** an original orange hexagon with a tick, in the style of the game's interface but not Frontier's trademark. `static/logo.svg` is the source; the 32, 180 and 512 px PNGs were rendered from it. It appears in the header of every page; `/favicon.ico` serves the 32 px PNG.

---

## 21. One scorecard per version (2026-10-14)

**What changed:** the six time ranges (1, 7, 14, 30, 90 and 180 days) are gone. Each app version has one scorecard, over every message from it still on file.

**Why:** most versions are short-lived, so several ranges held the same messages and showed identical cards; picking a range looked like it did nothing. A version is the unit a developer cares about, and one card per version makes versions directly comparable.

**How:**
- The scorer's window for other apps is `ALL_DAYS` (3650 days). Retention keeps about six months, so that means "everything on file"; a version older than that is scored on its most recent six months.
- **Reference apps keep a 7-day window.** A reference version can have tens of millions of messages (EDMC 6.1.2: 58 million), and counting duplicates, bursts and re-sends across all of them hourly isn't worth the load. Their cards say "last 7 days".
- `scorecards` keeps its `range_days` column, which now holds 3650 or 7. Scoring a version deletes any cards it has for other windows, which cleared the old per-range cards as each version was rescored.
- The card shows the date of the oldest message it covers ("all messages on file (since …)").
- **Addresses:** `/card/{app_id}?v={version}`. The old `/card/{app_id}/{days}?v=…` addresses return a 301 redirect to it, so shared links keep working.
- The `scorecard` command lost its `--range` option.

This supersedes the per-range parts of §6.7 (sampling within the range), §7 (the mock-up's range), §8 (the card URL and recompute tiers) and §15 (reference cards covering 1 and 7 days).


---

## 22. Second review fixes (2026-10-14)

A second multi-agent review (six areas, each with an independent verifier) confirmed 31 findings. All are fixed.

**Archive import**
- **Cut-off downloads are retried, not recorded as done.** `http.client` returns a short body silently when the server closes early, so `stream_lines` now checks the byte count against `Content-Length` and that the bzip2 stream reached its end. Either failing raises `IncompleteDownload`, and the file is retried.
- **Reference pass:** the file list is fetched (with retries) before any reference-app rows are deleted; `--reference` refuses `--category` and `--no-test`, which would delete whole days but re-import only part of them.
- **Message counts** (`app_versions.msg_count`) are added only after a file commits, and the live rows a reference pass replaces are subtracted, so neither retries nor the reference pass double-count.
- One unparseable line no longer fails a whole file; a killed worker process now fails the run (`ProcessPoolExecutor`) instead of hanging it; the first pass streams its messages in chunks instead of holding a whole file in memory.
- `followups.py` retries every step (24 times, 5 minutes apart) and treats files that still failed as a reason to retry the day.

**Robustness against hostile messages**
- Digit runs in system names and game versions are bounded before `int()` (Python refuses more than 4,300 digits), and `rules.evaluate` skips a check that raises, so one message can no longer abort a scorecard or the hourly profiles job.

**Scoring** (these change grades)
- **Duplicates** mean the same thing for the app and the reference baseline: an identical message within `duplicate_window_hours` (24) of its previous copy. Before, the app was measured over everything on file and the baseline over 2 days.
- **Known fields accumulate** (`ref_paths`, migration 0009), like known values, so a rare but real field (a station's damage state) no longer flickers between "known" and "no reference app sends this".
- **Behaviour baseline:** a reference app that never sends an event type no longer lends it its overall rates; overall rates are used only when no reference app sends enough of that type.
- The baseline uses the scorer's thresholds from `scoring.yaml` instead of its own copies.
- A field the reference apps only fill with fractions (0..1) holding a value above 2 is a warning (a percentage, a units error the span test missed).
- A Legacy branch that the newest-messages sample missed gets its own sample, instead of a row with "ok" confidence and no score.
- Cards of versions whose newest messages are over 30 days old say they are compared with today's reference apps.
- Reference-app versions that stop sending lose their 7-day card instead of keeping a frozen one.

**Facts and pairing**
- Body facts come from the live game only, and Legacy scans aren't paired against them (Legacy planets differ).
- A disputed system is cleared when a later batch's observations (3 or more) all agree, and a `[0,0,0]` placeholder position no longer disputes a real one.

**Rules:** GEN-035 needs a word boundary (`… d13-1` is not a prefix match for `… d13-10 2`).

**Web:** version sorting recognises pre-release words only as whole words ("March" isn't "rc"), ranks alpha < beta < rc, and lets other suffixes tie with the bare number (the later-seen one ranks higher); cards and version lists have their own cache, so unknown addresses can't evict the list pages; malformed card addresses get the 404 page; an empty version string isn't treated as "no version".

**Operations**
- The listener reconnects after any socket error and exits (so Docker restarts it) if the receiver thread dies; ingest statistics for minutes when the database was unreachable are kept and written later under their own minute.
- The size guard never trims the last 7 days (today's partition is being written to; the week is what reference cards and baselines read).

**`messages_default` repair (manual).** If the worker was down for longer than `partitions_ahead_days`, rows for a day with no partition land in `messages_default`, and that day's partition can no longer be created. `ensure_partitions` now logs this and carries on with the other days, retention expires old rows from `messages_default`, and the worker warns while it holds rows. To move a day's rows into their own partition (briefly locks `messages_default` only):

```sql
BEGIN;
SET LOCAL lock_timeout = '10s';
CREATE TABLE messages_p20261020 (LIKE messages INCLUDING DEFAULTS);
WITH m AS (DELETE FROM messages_default
            WHERE gw_ts >= '2026-10-20 00:00+00' AND gw_ts < '2026-10-21 00:00+00' RETURNING *)
INSERT INTO messages_p20261020 SELECT * FROM m;
ALTER TABLE messages ATTACH PARTITION messages_p20261020
      FOR VALUES FROM ('2026-10-20 00:00+00') TO ('2026-10-21 00:00+00');
COMMIT;
```

Then run `eddn-validator create-indexes` for the partition's own indexes.

---

## 23. Database performance pass (2026-10-14)

Full analysis: `docs/research/db-performance-2026-10-14.md` (item numbers below refer to it).

**Done (no restart, nothing running was interrupted)**
- **A1:** `scorer._counts` carries the literal widest window, so a reference app's 7-day card prunes to 8 day partitions instead of scanning all ~190.
- **A2:** per-database settings (`ALTER DATABASE eddn_validator SET`): `work_mem` 64MB, `effective_cache_size` 16GB, `random_page_cost` 1.5 (SSD), `effective_io_concurrency` 200, `maintenance_work_mem` 1GB, `jit` off, plus `track_io_timing`, `log_min_duration_statement` 10s, `log_temp_files` 64MB and `log_lock_waits` for measuring. The profiles job runs its three big scans (buckets, baseline, bursts) without parallel workers and with `work_mem` 512MB.
- **A3:** `pair_results`: a partial index for the scorer's mismatch queries, and a BRIN index on `gw_ts` replacing the 419 MB btree (built `CONCURRENTLY`; listed in `indexes.PLAIN`).
- **A4:** facts lookups go in slices of 10,000 keys (`facts.lookup`, `facts.lookup_bodies`); larger `= ANY` lists flipped the planner to a full scan.
- **A5:** the station-system backfill names the reference apps' ids, so it uses `messages_big3_idx`.
- **A10 (part):** autovacuum thresholds on the facts tables, `apps` and `app_versions`.
- **A12:** the size guard's `VACUUM FULL` waits at most 30 s for its lock.

**Server restart, 2026-10-14 08:26 UTC** (settings written with `ALTER SYSTEM`, so they live in `postgresql.auto.conf` in the data directory): `shared_buffers` 4GB, `max_locks_per_transaction` 256, `max_worker_processes` 16, `max_parallel_workers` 8, `max_parallel_maintenance_workers` 4, `max_wal_size` 8GB / `min_wal_size` 2GB, `checkpoint_timeout` 15min, `wal_compression` lz4, bgwriter 1000 pages / 50ms / ×4, autovacuum cost limit 2000 and `autovacuum_work_mem` 512MB, `shared_preload_libraries = 'pg_stat_statements, auto_explain'` (plans of statements over 30 s are logged). The `pg_stat_statements` extension is installed in eddn_validator. Not done: `shm_size` (it needs the hand-made postgres container recreated; the code avoids parallel sorts instead).

**After the follow-up job's busy steps (2026-10-14, all measured on a quiet database):**
- **A6 done.** `Scorer._counts_and_behaviour` replaces the separate count and behaviour queries with one pass, and cards are built before the short transaction that swaps them in. Checked against the old path in one REPEATABLE READ snapshot per version (GameGlass 8.1.4, EDDI 5.0.2, EDMC Linux 6.1.2, a version with /test messages): identical results.
- **A9 done.** Per-partition extended statistics (`app_id`, `software_version`, `event_type`) and larger samples; `analyze-partitions` ran over all 191 partitions in about 13 minutes; new partitions get the statistics when created, and the worker's daily `analyze` job re-analyzes yesterday's partition.
- **A10 done.** `apps` 976 kB → 16 kB and `app_versions` 4.4 MB → 88 kB (VACUUM FULL), `facts_body_pkey` 2,158 MB → 1,540 MB (REINDEX CONCURRENTLY, 4 minutes).
- **A7 and A8 on hold.** After the restart, EDDI 5.0.2's whole card takes about 76–80 s (220 s before the restart), and the queries those indexes target are only about 13 s of it; the rest is the per-event-type sample and rule evaluation in Python. Not worth 2.5 GB + 3 GB of indexes and the extra write work until `pg_stat_statements` shows otherwise. The A7 index definition is left commented out in `indexes.PARTITIONED`.

Measured (quiet database, EDDI 5.0.2, 2.85M messages): whole card 220 s before the restart → 76 s after it; count + behaviour queries 23 s → 13.5 s with A6. GameGlass 8.1.4 (113k messages): whole card about 11–12 s.

---

## 24. Pairing gap and older reference facts (2026-10-14)

**Baseline, before any older facts** (followups gap report, 15.5M other-app messages on file, Apr 16 to Oct 14):

| | Claims | With no reference fact | Distinct places unmatched |
|---|---|---|---|
| Systems | 16,516,443 | 4,950,936 (30.0%) | 1,255,035 (99.7% of these claims are generated names, covered by the system-ID checks) |
| Bodies | 6,038,062 | 2,359,462 (39.1%) | 2,171,476 of 4,565,433 distinct bodies (47.6%) |
| Stations | 2,409,204 | 133,211 (5.5%) | 14,509 |

A sample of four reference Journal.Scan days from 6–12 months back matched only about 1,100 unmatched bodies a day (exploration scans are mostly one-offs), so a full import of older facts was rejected. Instead, `import-missing-facts` reads the six months before the window (2025-10-16 to 2026-04-15) and stores reference facts only for places currently missing:
- systems and bodies, with `last_seen` set to the import time (static facts, kept by retention);
- for stations, only the system they are in (`star_system`, `system_address`, `system_seen` from the message), with no name or type, so pairing compares only the system.

After it: a full re-pair, a rebuild of other apps' cards, and the gap measurement again, to decide whether going further back is worth it.

**Result of the first pull (2025-10-16 to 2026-04-15, run 2026-10-14):** 1,274 files, 149.7M reference messages read, 476,040 fact rows stored (repeat sightings merged); then a full re-pair, the cards rebuilt, and the gap measured again:

| | Before | After | Change |
|---|---|---|---|
| Systems | 30.0% of claims (1,255,035 places) | 27.0% (1,214,039) | hand-named systems: 14,800 claims → 7,387 |
| Bodies | 39.1% of claims (2,171,476 places) | 36.4% (2,030,072) | about 141,000 bodies gained a fact (6.5% of the gap) |
| Stations | 5.5% of claims (14,509 places) | 4.1% (8,652) | 40% of the missing stations filled |

These facts have `last_seen` set to the import time, so retention removes any that no reference app reports again within six months.


**Pinned facts (2026-10-14, migration 0010).** `facts_system`, `facts_body` and `facts_station` keep one row per place, written only from the reference apps' messages. A `pinned` flag marks rows another app's report has been paired against: pairing sets it the first time a fact is used, and `pin-facts` (also run at the end of `import-missing-facts`) pins every fact the other apps' messages on file report. Retention never deletes pinned rows; unpinned ones still expire six months after a reference app last reported them. This keeps the facts other apps actually need, including everything the targeted pulls brought in, without keeping the tens of millions of bodies only reference users ever scan.

**Second pull, 2024-10-16 to 2025-10-15 (2026-10-14):** 2,555 files, 277.5M reference messages read, 313,089 fact rows stored, 0 failures; then re-pair, rebuild and gap measurement:

| Claims with no reference fact | Baseline | + 6 months | + 1½ years |
|---|---|---|---|
| Systems | 30.0% | 27.0% | 24.4% |
| Hand-named system claims | 14,800 | 7,387 | 3,959 |
| Bodies (distinct unmatched) | 39.1% (2,171,476) | 36.4% (2,030,072) | 34.0% (1,901,505) |
| Stations (distinct unmatched) | 5.5% (14,509) | 4.1% (8,652) | 3.1% (5,027) |

The owner chose to stop here: each further year would add about 2 points of body coverage and only a few thousand stations and hand-named systems. All of these facts were then pinned with `pin-facts`.


---

## 25. Second bug sweep fixes (2026-10-14)

A Fable sweep of the code changed since §22 (six areas, each with an adversarial verifier) confirmed 28 findings, about 20 distinct. All fixed:

- **Size guard** (`retention.py`): a compaction that cannot get its lock stops the run (deleting more days would free nothing); each run first compacts any trimmed day still waiting (seen from its dead-row statistics); it waits for a running archive import unless free space is below `hard_min_free_gb` (52).
- **Archive import:** a file's version counts commit in the file's own transaction, and nothing after the commit can send a file round the retry loop again (no re-import, no double count); the reference pass subtracts the counts of the rows it deletes in the same transaction as the DELETE.
- **Facts:** `facts.store` writes systems and bodies in key order, like stations and `pin()`, so concurrent writers cannot deadlock; a disputed system is cleared only when two reference apps agree or the batch confirms the value already held; a station stored without a name or type (targeted pull) gets them from any later report; `pin_in_slices` pins millions of keys in short retried transactions.
- **Pairing:** a long run pairs in rounds and refreshes the facts between them, so messages arriving during a backlog run are not paired against hours-old facts.
- **Targeted pull:** the re-pair is checked to have reached the end (an import or catching-up facts makes pairing return quietly) and retried otherwise; every step after the import is retried like the follow-up job's; route-hop systems are targets too; before Update 14 (no `gameversion` header) a message with `odyssey: true` counts as live.
- **Follow-up job:** a day is imported only when as many files are listed as the day before (or after six hours of a short listing), since the reference pass deletes the whole day first.
- **Scorer:** each card gets its own timestamp (long-lived scorers in the rebuild threads); the duplicate window reaches back before the range, as the reference baseline does; the `NOT is_big3` predicate is off until the A7 index exists (`OTHER_APP_INDEX`).
- **Smaller:**
  - migration 0010 uses `SET LOCAL`, and the runner resets `lock_timeout` and `statement_timeout` before each migration;
  - the daily `analyze` job re-checks the last three days against their own end;
  - "newest … not graded yet" only appears for a version with messages still on file;
  - one-letter pre-release forms (`b4`) count only right before a number.

---

## 26. Clearer scoring pages (2026-10-15)

A reader read a 100% score next to a list of "outside the reference range" findings as a contradiction, and could not tell profile-based checks from per-message pairing. Changes:

- `/about` was rewritten around a plain-English summary of the five layers, each with what it is compared with. It adds how the grade is worked out, "How to read a scorecard", and an FAQ (100% with findings, why out-of-range is not an error, what pairing compares with, why six messages show up as eleven notes).
- Each `/about/<layer>` page opens with a "Compared with:" line.
- On the scorecard:
  - each detail section starts with a line saying what it compares against, linked to its About page;
  - severities read serious / minor / note, and notes are dimmed and marked "not scored";
  - the layer column headings link to their pages.
- `score_values` returns a summary (messages checked, distinct messages with any finding, messages with a scored finding). The Values section shows it as "N fields flagged across M of K messages checked", since one message can appear in several field rows.

---

## 27. Rule corrections from the reference apps' own failure rates (2026-10-15)

A rule that one trusted app breaks constantly while the others never do usually means a check that is too literal. Reviewing every rule the reference apps break more than 1% of the time:

- **APS-001 (MarketID with settlement data) was a false positive.** It took any `Station…` field as evidence of a market. For Guardian ruins the game writes an empty `"StationFaction": {}` into ApproachSettlement; EDO Materials Helper passes it through while the other apps drop empty fields, so EDO alone failed 9% (all 966 cases in a week were empty factions, 918 of them `$Ancient` ruins). The check now ignores empty station fields. EDDN's own ruins sample predates the empty field, which is why the tests did not catch it.
- **COM-005 is switched off.** CAPI markets only sometimes carry `statusFlags` (commodity README), so the check could not tell a correct market without flags from flags dropped by the sender; EDMC and EDDiscovery "failed" it on about half their CAPI markets.
- **GEN-040 is now minor (warning), not serious.** The repeats are real (the game lists some modules and ships more than once, and the schemas say senders must remove them), but EDDN's gateway only warns, and EDMC and EDDLite both send them.
- Confirmed genuine and unchanged: OUT-001 (EDDiscovery, EDDLite), CDX-003 (EDDLite, a note), GEN-023 (EDO's late re-sends, a note), GEN-042 (game data, a note). COM-002 (EDMC) was listed here too, wrongly: see §28.

## 28. Corrections from a read of EDDN's documentation (2026-10-17)

After the ruins case (§27), every schema README and the developer guide were read again for edge cases the checks get wrong. Six were confirmed against the data:

- **COM-002 (empty `economies`/`prohibited`) applies to journal-sourced markets only.** The README's advice is for Market.json, which has neither list, so a journal-based sender should leave them out. CAPI data can carry a genuinely empty list, so CAPI markets (gameversion `CAPI-…`) are no longer checked. EDMC's "failures" were all CAPI.
- **Moving stations are decided by type first.** Odyssey settlements (IDs 3.8–3.94e9) and crater outposts and ports (3.7–3.8e9) share ID blocks with carriers. The station-system comparison skipped them as if they could move, so those mismatches were never caught. Now carriers and megaships move, and so does a `SurfaceStation` with an ID in the carrier range: these are PowerPlay "Stronghold Carrier"s. Any other known type is fixed. The ID ranges decide only when the type is unknown.
- **Legacy messages pair on systems only.** Body facts were already live-only. Station and market facts are now live-only too (the Legacy galaxy has no Odyssey settlements or Colonisation stations, and markets differ), so Legacy messages are no longer compared against them.
- **GEN-005 also catches a journal gameversion copied into gamebuild.** The developer guide allows mirroring gameversion into gamebuild only for CAPI values.
- **FSD-001 compares times, not text.** A signal timestamp with more sub-second digits than the top-level one is the same moment.
- **SBC-001 respects "if it's in the source data".** A ScanBaryCentre with none of the orbital elements is what the game wrote. Only a partial set, with some elements kept and others dropped, is flagged.

The 2022 facts pull (§24) was restarted with this code so its re-pair uses it. Cards are rebuilt on the normal cycle.

## 29. Rule review against EDDN's documentation, and rule details on the technical page (2026-10-17)

A multi-agent review compared every rule with the documentation it cites (`EDCD/EDDN` at 4ad669bb, plus `develop` for scanorganic). Each finding was then challenged by a second agent, and the survivors were counted in seven days of messages (8.8 million from the reference apps). No reference app is affected by any of them. Changes:

- **GEN-027J** fails a journal/1 message without `horizons`, including one that sends `odyssey` alone. Every client's LoadGame carries Horizons, and only Odyssey can be absent (3.8 clients). This affected Journal Limpet (11,011 messages in the week) and EDFM Companion (182).
- **JNL-001** is now a note: journal/1 still lists CodexEntry, and its README only points to codexentry/1.
- **GEN-004** is now minor: the endpoint word is a SHOULD (Dev:258-259); the MUST NOT beside it is GEN-008 and GEN-009B.
- **COM-006** applies to CAPI markets only: it is the README's fallback for a sender that cannot read LoadGame.
- **GEN-043** skips /test schemas and catches negative MarketIDs. **GEN-041** also catches the no-market value rounded by a 64-bit float. **COM-003** also catches `$drones_name;`. **DCK-001** treats a blank StationName as empty. **NAV-001** is cited as implied.
- New **GEN-009** (minor): a journal gameversion with an empty gamebuild, or the reverse. New **GEN-009B** (serious): CAPI market or shipyard data carrying a journal build as gamebuild.
- **OUT-001** is now minor. The README's "remove ... (for historical reasons)" is a plain instruction, not a MUST, and the listing is not wrong: the module is on sale. EDDiscovery and EDDLite still send it (EliteDangerousCore's `CreateEDDNOutfittingMessage` keeps every weapon, internal and armour item), in about 57% of their outfitting messages, so they are still flagged.
- FCC-003 stays as it is: the schema rejects `locName` in well-formed entries, but a whole market dumped into `Items` passes the gateway.

Of the review's own findings, only GEN-027J and JNL-001 matched a message that week (OUT-001 was lowered afterwards, on a separate question).

The technical page now opens each rule into what it checks exactly (exceptions included), a failing and a passing example, and how often each reference app fails it. The rates are read from the reference profiles, which migration 0011 lets the web role read. The examples live in `config/rule_details.yaml`, and `tests/test_rules.py` runs each one through the real checks. A failing example must raise the rule and a passing one must not, so the page cannot drift from the code. Every enabled rule must have an entry. The page also gained sections on what pairing compares and on what the gateway already rejects.

## 30. Severity standard made consistent (2026-10-17)

The stated standard is now: **serious** when the rule is a MUST, or breaking it leaves the data wrong for listeners, and the check is reliable. **Minor** when the check is a heuristic, or the rule is a SHOULD or a plain instruction. **Note** for advice, and for timing rules the behaviour layer already scores. Applied:

- **GEN-022** (future-dated) is now a note, like GEN-023 (late). The behaviour layer measures both with the same thresholds, so a future-dated message was being penalised twice.
- **GEN-034** (personal data), **COM-002** (empty lists, journal-sourced), **GEN-010** and **GEN-011** (softwareName, softwareVersion) are now serious. Each is a MUST, checked reliably. On the current cards they affect one app (GEN-034), EDDI and older EDMC cards (COM-002; EDMC's clear on rebuild, being CAPI), and nobody (GEN-010, GEN-011).
- **COM-001** and **FCJ-001** stay serious without a MUST behind them, because the data is wrong for listeners: `$gold_name;` does not match `gold`, and CAPI materials data has a different shape from FCMaterials.json. OUT-001, a plain instruction where the data is not wrong, stays minor.
- GEN-025's description now attributes its claim to the developer guide: whether the current Legacy client (3.8.0.2100) still leaves Odyssey out of LoadGame could not be confirmed.

## 31. Weak spots and a smooth confidence ramp (2026-10-17)

A review of the scoring maths (an outside opinion, checked against the code) raised two valid points. Its other points were already handled: the rules floor, structure weighting, missing layers, pairing with no pairs, and insufficient data. The two changes:

- **Weak spots.** The overall score is weighted by message count, so a broken low-volume event type barely moves it. For example, journal messages at 99 and 100 market messages at 20 still give 98.9. The grade stays as it is, since it measures what listeners receive per message. Instead, every event type with at least `low_below` (30) messages that scores below C is recorded as a weak spot, both in the card and in a new `scorecards.weak_spots` column (migration 0012), so the lists need not read whole cards. Weak spots show under the card's header, beside the grade on the All apps and New apps lists, and in the picker's version list. Existing cards were filled in once from their own rows: 77 of 989 have one.
- **Confidence ramp.** The low-confidence weight jumped from half to full at 30 messages. It is now `messages × min(1, messages / 30)`, a smooth ramp from 5 to 30 messages.

## 32. "Not seen in this version yet" (2026-10-17)

A card listed every event type the reference apps send but the version did not as "not sent by this version". For a new version with few messages, that read as if the app did not support event types its earlier versions sent; Outrider 2026.10.25 showed 16. Each card now records its event types with message counts in `scorecards.event_types` (migration 0013, filled once for existing cards from their rows). The card page splits the list when it is shown:

- **Not seen in this version yet:** an earlier version with messages on file sent it. The page shows the highest such version, linked, and when it was last seen.
- **Not sent by this application:** no version with messages on file has sent it. The "Not sent" tile counts only these, with the other count beside it.

Neither is scored. The reference column is now labelled "messages profiled", since it shows the profile sample (up to 2,000 newest per app), not messages on file. The card's Basis line now describes the confidence ramp (§31) correctly.

## 33. Wrong SystemAddresses the rules cannot see (2026-10-17)

A corrupted or stale SystemAddress was caught by GEN-030 (position outside the boxel the address encodes) and GEN-033 (a generated name that does not match it). Two cases got through:

- **Negative addresses.** Nothing decodes from them, so GEN-030 skipped them silently. New rule **GEN-036** (serious) flags any negative SystemAddress, in the message or a NavRoute hop. No upper limit is applied: none is documented, and the largest real id64 (7.3e15) is far from any boundary.
- **A wrong address in the same boxel, for a name that cannot confirm it** (hand-named systems, and hand-made sectors where GEN-033 returns None). Pairing now covers this. When a claim's address has no reference fact, it looks the system up by name *and* position (two systems can share a name, never a name and a position, so genuine duplicate names are never accused). If exactly one fact matches and its address differs, that is a system mismatch on `SystemAddress`. It is applied only where GEN-030 and GEN-033 are silent, so no fault is penalised twice. NavRoute hops are checked the same way. The lookup uses a new index, `facts_system_name_idx`.

This enforces the developer guide's MUST (Dev:444-456: cross-check the system name, StarPos and SystemAddress). A by-name search over all history beforehand (15.6M messages from other apps) found no bit-corrupted addresses. Of 824 addresses 1–2 bits off, 820 were stale locations (811 from Elite Warboard) and 3 were genuine duplicate names. All history was re-paired once so existing cards include the new check.
