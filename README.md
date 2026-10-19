# Award-to-Lane

Every business day USDA's Agricultural Marketing Service (AMS) awards contracts for food bought for
schools and food banks. Each award is published as a "PCA" (Purchase Contract Award) PDF listing the
winning company, its plant, every destination city, the quantity, the price and the delivery window.
The contracts are FOB destination, so the winner pays the freight. Award-to-Lane finds new awards
every morning, parses them, explodes them into plant-to-destination lanes, scores them and produces a
call sheet for freight-broker sales reps.

## How it finds awards

The award PDFs are **not** on `ams.usda.gov/selling-food/solicitations` (that page only links master
solicitations and schedules, and it blocks non-browser user agents). They live behind the WBSCM
"Public Procurements" portal, which is a React app fed by an unauthenticated JSON endpoint:

```
GET https://portal.wbscm.usda.gov/ppp/search?docCat=Solicitation&latest=true&active=false&awdDt=YYYY-MM-DD
```

Each record carries the solicitation number, the commodity subcategory and a `docLinks.items` list; the
award PDF is the item whose `docType` is `PCA`. Discovery is one polite request per day plus one
download per new PDF (about 25 a month). No HTML scraping, no Playwright.

## Setup

Python 3.11 or newer.

**Windows (PowerShell or cmd)**

```bat
py -3.12 -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env
```

**macOS / Linux**

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Edit `config.yaml`: at minimum put your real website and e-mail in `http.user_agent`. USDA asks for an
identifiable agent, and OpenStreetMap's Nominatim (the geocoding fallback for towns missing from the
bundled gazetteer) refuses placeholder agents with HTTP 403. API keys for optional plug-ins go in `.env`,
never in `config.yaml`.

## Running

```bash
python run_daily.py
```

The first run backfills `sources.usda_pca.backfill_days` (365) days of awards so the rolling call sheet
starts complete; at three seconds between requests that takes roughly 20 minutes for about 300 PDFs.
Later runs query the last `lookback_days` (30) days and download only PDFs they have not seen. Every run
ends with a one-screen terminal summary (also saved to `output/<date>/summary.txt`).

| Flag | Effect |
|---|---|
| `--backfill` | query the full backfill window again |
| `--reparse` | re-parse every downloaded PDF (after changing the parser) |
| `--skip-discover` | rebuild outputs from the database without contacting the portal |
| `--no-download` | record new award PDFs but do not download them |
| `--date YYYY-MM-DD` | run as of another date (affects NEW flags, days-until-open and the output folder) |
| `--verbose` | debug logging on the console (the log file always has debug detail) |
| `status [award_key contract_no status] [--note ...] [--rep ...]` | list the statuses reps have recorded, or set one (see "Working the sheet") |

Logs: `logs/award_to_lane.log`. Database: `data/award_to_lane.sqlite`. PDFs: `data/pdfs/<award>/`.
A PDF that fails to download or parse is logged, listed in the summary and never stops the run.

### Windows Task Scheduler

Run `scripts\register_task.cmd` once. It registers a per-user task named "Award-to-Lane daily" that
runs `scripts\run_daily.bat` every weekday at 06:30. To do it by hand instead: Task Scheduler >
Create Basic Task > Daily (weekdays) > Start a program > browse to `scripts\run_daily.bat`, and set
"Start in" to the project folder. Make sure the task runs whether or not you are logged on and that the
machine is awake at that hour.

## Outputs (`output/YYYY-MM-DD/`)

| File | Contents |
|---|---|
| `call_sheet.html` | one row per lead (company + plant within an award): NEW flag, status (what a rep did with it, note on hover), score, commodity, equipment, destinations, total pounds, estimated truckloads, delivery window, days until it opens, bidder count, fleet status (FMCSA plug-in), flags, PDF link, and an expandable lane list with drops, pounds, truckloads and approximate miles. Filter box, "hide lost/skipped" toggle, expand-all and a print layout. |
| `call_sheet.csv` | the same lead rows, flat, including `status`, `status_note`, `status_rep` and `status_updated_at` |
| `lanes.csv` | one row per lane (origin, destination, drops, quantity, pounds, truckloads, equipment, miles, rate reference) |
| `apollo_import.csv` | unique companies with domain, fleet status, USDOT number and power units where known, for bulk import |
| `emails/` | one drafted outreach email per NEW lead that no rep has touched yet, with the contract number, commodity, lane count, delivery window and a `[QUOTE ...]` placeholder; rewritten every run, so a lead marked lost or quoted loses its draft |
| `summary.txt` | the terminal summary |

**Rolling window.** The sheet contains every award whose delivery window has not closed. Booked and
quoted leads sort first, then NEW leads (first seen this run and posted within
`call_sheet.new_window_days`) by score; everything else sorts by days until the window opens, with a
contacted lead after the untouched leads that open on the same day; lost and skipped leads sink to the
bottom, greyed out.

**Lead vs lane.** A lead is one contract (one company shipping from one plant) inside an award. A lane
is one unique plant-to-destination pair inside that contract, with its drop count rolled up.
Truckloads are counted per drop: each drop is at least one load, and anything heavier than
`lanes.pounds_per_truckload` (40,000 lb) splits into more. Cases are converted to pounds from the pack
size in the material text ("CTN-30 LB", "PKG 8/5 LB", "CAN-6/10"); converted weights are marked `~`.
Miles are gazetteer-centroid great-circle distance times `lanes.road_distance_factor`.

**Scoring** is one function, `score_lead` in `src/award_to_lane/score.py`: a weighted blend of award
value (log scale), truckloads (log scale) and urgency (windows opening within two weeks score highest,
already-open windows next), multiplied by penalties for blanket awards, parse problems, international
awards, unknown windows and (with the FMCSA plug-in on) a confirmed private fleet. Weights and penalties
are in `config.yaml` under `scoring`.

## Working the sheet

Reps record what happened to each lead so the next morning's sheet reflects it: a lead keeps its
status on every later sheet until the award's delivery window closes and it drops off. Statuses:

| Status | Meaning | On the sheet |
|---|---|---|
| `new` | nobody has worked it yet (the default; also resets a lead) | plain row; NEW leads get an email draft |
| `contacted` | called or e-mailed, waiting to hear back | grey tag; sorts after untouched leads opening the same day |
| `quoted` | rates sent | blue tag; stays at the top with NEW |
| `booked` | freight won | green tag; stays at the top with NEW |
| `lost` | went elsewhere | red tag; row greyed out at the bottom |
| `skip` | not worth a call (private fleet, too small, wrong region...) | struck-through tag; row greyed out at the bottom |

Anything other than `new` suppresses the drafted email for that lead. Lost and skipped leads stay
visible (greyed out) so you can change your mind; the "hide lost/skipped" box in the toolbar hides
them, and the filter box matches status words too. Hover a status tag for the note, rep and date.

**Two ways to set a status.** The `award_key` and `contract_no` are in the company cell of the sheet
and in `call_sheet.csv`.

```bash
python run_daily.py status                                                  # list everything recorded
python run_daily.py status 12-3J14-26-B-0410                                # ...for one award
python run_daily.py status 12-3J14-26-B-0410 12-3J14-26-C-0001 contacted --note "left vm w/ traffic mgr"
python run_daily.py status 12-3J14-26-B-0410 12-3J14-26-C-0001 quoted --rep pat
python run_daily.py status 12-3J14-26-B-0410 12-3J14-26-C-0001 new          # un-skip / reset
```

`--rep` defaults to your login name; a `--note` that is not given keeps the previous note (`--note ""`
clears it). Use `-` as the contract number for the rare lead that has none. The CLI confirms the
company name, or warns when the key is not on file (a typo, or an award not parsed yet; the status is
stored either way and attaches when the lead appears).

Or edit **`data/lead_status.csv`** (`paths.lead_status`) in Excel or any editor, no tooling needed:

```
award_key,contract_no,status,note,rep,updated_at
12-3J14-26-B-0410,12-3J14-26-C-0001,quoted,sent 4 lanes,pat,2026-10-19
12-3J14-26-B-0433-0001,12-3J14-26-C-0007,skip,private fleet,,
```

`award_key` and `status` are required; `contract_no`, `note` and `rep` may be blank; `updated_at` is
an ISO date or date-time and may be blank. Status words are case-insensitive (`skipped` is accepted
for `skip`). The file is read at the start of every run and merged into the `lead_status` table in
SQLite: the newest `updated_at` wins, and on a tie, or when the file row has no `updated_at`, the file
wins. A key listed twice in the file resolves the same way (the later row on a tie). The CLI writes
both the table and the file (one row per lead, sorted), so the file is always a complete record you
can back up or hand to another rep; if the file is locked open in Excel the CLI saves to the database
and the next merge keeps the newer database row. Bad rows (unknown status, no key) are logged and
skipped, never fatal.

## Configuration (`config.yaml`)

| Key | Meaning |
|---|---|
| `http.user_agent`, `http.min_seconds_between_requests` | politeness; keep the agent identifiable |
| `sources.usda_pca.backfill_days` / `lookback_days` | first-run and daily query windows |
| `sources.usda_pca.include_international` | export awards (port-code destinations, no window in the PDF); off by default |
| `sources.usda_pca.include_blanket_awards` | fiscal-year blanket awards (plant known, destinations not listed); on, flagged |
| `lanes.pounds_per_truckload`, `lanes.road_distance_factor`, `lanes.default_case_weight_lb` | lane math |
| `lanes.geocoder.*` | bundled gazetteer path and the optional Nominatim fallback |
| `equipment.rules` | ordered keyword rules; first hit on the material text, then on the commodity subcategory, wins |
| `call_sheet.new_window_days` | how recent a posting must be to count as NEW |
| `paths.lead_status` | the rep-maintained status file merged into the database every run (`data/lead_status.csv`) |
| `scoring.*` | weights and penalties |
| `plugins.*` | SAM.gov, Apollo, FMCSA fleet and pricing switches (below) |

## Plug-ins (off by default)

**SAM.gov entity data** (`plugins.sam_gov.enabled: true`, key in `.env` as `SAM_GOV_API_KEY`). Free
alternative to Apollo. Every USDA contractor is registered in SAM.gov, and the public Entity Management
API returns the registration's legal name, UEI, CAGE, physical address, website and points of contact.
For each company on the sheet without a cached result the plug-in sends one request (legal name plus
state), picks the entity whose core legal name and state match, and fills the website into
`apollo_import.csv` and the call sheet, and the government-business and electronic-business POCs into
the contacts line and the email drafts. The public API gives POC **names and titles only**: e-mail and
phone are "FOUO" fields served only to federal system accounts, so a SAM.gov contact is someone to look
up or call the switchboard for, not an address to mail. Looser matches (a truncated award name, a DBA
name, or no state to check against) are still shown but flagged `sam_possible_match`; every contact
records its source (`sam_gov`, `sam_gov~` for possible matches, `apollo`). Get the key by signing in to
SAM.gov and revealing "Public API Key" under Account Details (it is not an api.data.gov key, and it is
reissued roughly every 90 days). A personal key on an account without a SAM.gov role is limited to
**10 requests per day**, which is why `max_companies_per_run` defaults to 10 and misses are cached too
(retried after `miss_retry_days`); the cache clears a few companies every morning until the sheet is
covered. Details of what the API returns are in the docstring of `src/award_to_lane/contacts/sam_gov.py`.

**Apollo contacts** (`plugins.apollo.enabled: true`, key in `.env` as `APOLLO_API_KEY`). For each company
on the sheet without a cached result, looks up the domain and up to `max_contacts_per_company` people
with the configured logistics / transportation / supply-chain titles. Results are cached in SQLite, so a
company costs credits once. Domains flow into `apollo_import.csv`, contacts into the call sheet and the
email drafts. Customers who already have Apollo leave this off and use `apollo_import.csv`. When both
plug-ins are on, SAM.gov runs first and Apollo only looks up companies that still have no website;
Apollo contacts are appended to any SAM.gov contacts rather than replacing them.

**FMCSA private-fleet check** (`plugins.fmcsa.enabled: true`, no key). The award never says whether the
winner runs its own trucks, which decides whether it needs a broker at all. FMCSA's Motor Carrier Census
does: every company operating commercial vehicles holds a USDOT number, and its MCS-150 filing records
the operation classification and the power-unit count. The plug-in queries the census file FMCSA
publishes on data.transportation.gov (dataset `az4n-8mr2`, refreshed daily) with one request per
company, filtered to active registrations, and shows the result as a **Fleet** column on the call sheet
and in `call_sheet.csv` / `apollo_import.csv`: `private fleet` ("PRIVATE PROPERTY" classification with
power units), `for-hire carrier` (carrier authority, no private classification), `registered, no trucks`
(a USDOT record with no power units, such as a brokerage, or a passenger / government classification),
`unverified` (a plausible record whose name or
state does not match exactly: a truncated award name, a DBA, the same name in another state, or a legal
name that merely contains the award name's words; the candidate's details are shown so the rep can
judge) and `no fleet on record` (no active record under that name: the normal case for a winner that
ships purely through carriers, though large companies also register one USDOT number per division under
slightly different names). Only a
confirmed `private fleet` adds the `private_fleet` flag and the `scoring.penalties.private_fleet`
multiplier (0.8). The USDOT number, power units and a one-line note (legal name, city, classification,
MC number, date of the last MCS-150) go to the CSVs and the column's tooltip. Results and misses are
cached in SQLite (`company_fleet`) and re-checked after `refresh_days` / `miss_retry_days`;
`max_companies_per_run` (25) caps the requests per morning, NEW leads first. Socrata serves anonymous
requests from a shared per-IP allowance; a free app token (data.transportation.gov > profile > developer
settings) in `.env` as `SOCRATA_APP_TOKEN` raises it to about 1,000 requests an hour. Source choice,
field meanings and the matching rules are in the docstring of `src/award_to_lane/contacts/fmcsa.py`.

**Pricing** (`plugins.pricing.provider`): `stub` returns nothing; `usda_market_news` pulls the USDA
Market News truck-rate report through the free MARS API (key in `.env` as `USDA_MARS_API_KEY`) and
attaches a regional reefer rate range to matching lanes as a reference. To add SONAR, DAT or a
customer's own rate table, subclass `RateProvider` in `src/award_to_lane/pricing/`, register it in
`get_provider`, and name it in the config.

## Adding another lead source

Recalls (CPSC/FSIS), rail embargoes, WARN notices and USAspending awards all fit the same shape: find
documents or records, turn them into `Award`/`Contract`/`LineItem` objects (or directly into `Lead`s),
and let the shared lane builder, scorer and reports do the rest.

1. Create `src/award_to_lane/sources/<name>/source.py` with a class that subclasses `LeadSource`
   (`sources/base.py`) and implements `discover`, `parse_pending` and `build_leads`. Store what you
   fetch in the `documents` table (or your own table) so re-runs are idempotent.
2. Register it in `all_sources()` in `sources/base.py` and add `sources.<name>.enabled: true` plus any
   settings to `config.yaml`.
3. Put sample inputs under `samples/` and tests under `tests/`.

The USDA source (`sources/usda_pca/`) is the reference implementation: `discover.py` polls the portal,
`parse.py` dispatches by PDF layout family, `parse_details.py` is the coordinate-based table parser,
`parse_narrative.py` holds the regexes, `parse_fallback.py` handles the rare layouts.

## Data notes and known limits

- `data/us_places.csv` is derived from the US Census Bureau 2023 Gazetteer (public domain): 32k
  incorporated places and CDPs plus 31k county subdivisions (New Jersey, Pennsylvania, New England and
  Michigan townships), with centroids. Lookups ignore spacing and punctuation, expand Ft/St/Mt, and
  tolerate small typos. Unincorporated warehouse towns that are still missing fall back to OpenStreetMap
  Nominatim (one request per second, cached in SQLite; needs a real contact in the User-Agent). Set
  `lanes.geocoder.nominatim_fallback: false` to stay fully offline; such lanes then show no miles.
- About 95% of award PDFs are one SAP report layout; the parser reconciles every row against the
  report's own subtotals and totals and flags any mismatch instead of dropping rows. The rest:
  fiscal-year blanket awards (no destinations; kept, flagged, scored low), hand-made Word PDFs (best
  effort, flagged `needs_review`), scanned images (no text layer; flagged, no OCR), cancellation
  notices (detected, excluded), and international export awards (port codes, metric tons, no window;
  excluded unless enabled).
- Pounds for case-packed goods and miles are estimates and are labelled as such on the sheet.

## Tests

```bash
pytest
```

The suite parses every PDF in `samples/` and checks narrative variants, wrapped destinations, totals
reconciliation, geocoding, lane math, scoring, rolling-window rules, discovery bookkeeping, outputs and
the plug-in seams (with the network mocked, including the FMCSA fleet matcher and the scoring penalty), and
lead status tracking (CSV merge rules, sort order, email suppression, the Status column, CLI round trip).

## Layout

```
run_daily.py                 entry point
config.yaml  .env.example    configuration and secrets template
src/award_to_lane/
  config.py db.py http.py    settings, SQLite schema, polite HTTP session
  models.py                  Award / Contract / LineItem / Lane / Lead
  geo.py commodity.py        geocoding; equipment rules and pack-size weights
  lanes.py score.py          lane explosion, lead roll-up, scoring
  lead_status.py             rep statuses: CSV <-> SQLite merge, attach to leads, the `status` CLI
  pipeline.py                the daily run
  sources/                   LeadSource interface + usda_pca implementation
  report/                    call sheet HTML/CSV, lanes CSV, emails, Apollo import, summary
  contacts/sam_gov.py        optional SAM.gov plug-in (free public registration data)
  contacts/apollo.py         optional Apollo plug-in
  contacts/fmcsa.py          optional FMCSA plug-in (private fleet? free Motor Carrier Census data)
  pricing/                   RateProvider interface, stub, USDA Market News
data/us_places.csv           bundled gazetteer
data/lead_status.csv         rep-maintained lead statuses (see "Working the sheet")
samples/                     real award PDFs used as test fixtures (+ raw text dumps)
tests/                       pytest suite
scripts/                     Windows batch files for Task Scheduler
```
