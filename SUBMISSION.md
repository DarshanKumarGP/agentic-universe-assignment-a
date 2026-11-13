# Submission

## Candidate
- **Name:** Darshan Kumar GP
- **Assignment chosen:** A (Data): Interaction Ledger & Reporting Pipeline
- **Time spent (honest estimate):** _<fill in your own number>_

## How to run
```bash
# from a clean clone
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

python assignment-a-data/tools/generate_data.py --seed 42 --scale small --out data

cd solution                                   # the package lives here
python -m ledger ingest ../data/drop_1        # idempotent: safe to run twice
python -m ledger ingest ../data/drop_2
python -m ledger report --out ../reports      # writes the five files
# ...or everything at once:  python -m ledger ingest ../data

pytest                                        # 89 tests, ~3 s (run inside solution/)
```
- Options go **after** the sub-command: `--db PATH` (default `./ledger.db`, or `$LEDGER_DB`), `--schemas DIR`
  (default `assignment-a-data/schemas`), `-q`.
- From the repo root instead: `PYTHONPATH=solution python -m ledger ingest data/drop_1`.
- The PII secret is auto-generated into `<db>.key` (mode 0600) on first use; set `$LEDGER_SECRET` to supply your own.
  **Keep the key with the database**: without it phones cannot be matched (the tool detects this and says so).

## Design

**Shape.** Two stages, `ingest` (writes facts) and `report` (derives everything).
Ingest stores only *facts*: validated leads and raw events. It does not store *conclusions* such as
"this event belongs to lead 42" or "lead 42 is completed". Reports recompute those from the ledger on every run.
That is what makes three hard requirements almost free:
- **Event-before-lead:** linking happens at report time with a join on `(workflow_id, phone_hash)`, so an event that
  was an orphan after `drop_1` links itself once the lead arrives in `drop_2`. Nothing needs "re-linking".
- **Incremental == all-at-once:** the report is a pure function of the ledger contents, and the ledger is the same.
- **Overrides never edit history:** an override is just another row; the effective disposition is computed.

```
 leads/*.csv ──► validate ─► reject (rejections table)         events/*.jsonl ─► parse ─► malformed? (counted)
                    │ valid                                                          │
                    ▼                                                                ▼
          merge by (workflow, phone_hash)                         INSERT OR IGNORE (PK event_id) ─► duplicate? (counted)
                    ▼                                                                ▼
              leads (PII encrypted)                               events  (append-only, triggers block UPDATE/DELETE)
                    └───────────────┬──────────────────────────────────────────────┘
                                    ▼   report (every run, from scratch)
        LEFT JOIN events→leads on (workflow_id, phone_hash)  ─► orphan if no lead
        override → call_id → lead of that call's other events
        per call: connected? failed? effective disposition (override > set, latest by occurred_at)
        per lead: bucket;  per workflow-day: calls / connected / success leads
                                    ▼
          batch_summary.csv  daily_overview.csv  lead_report.csv  rejections.csv  data_quality.json
```

**Schema (SQLite, `ledger/db.py`).**
| table | purpose / key constraints |
|---|---|
| `ingested_files` | one row per file ever ingested: `UNIQUE(kind, workflow_id, file_name)`, sha256, and the per-file counters behind `data_quality.json`. This is the idempotency anchor. |
| `leads` | `UNIQUE(workflow_id, phone_hash)` = lead identity. `phone_hash` (HMAC), `phone_last4` (for masking), `external_ref`, `batch_id` (set once by the first valid row), `fields_json` (non-PII), `pii_blob` (encrypted). |
| `rejections` | `(workflow_id, batch_id, row_number, reason)`. No PII by construction. |
| `events` | **the ledger.** `event_id PRIMARY KEY`, `event_type`, `workflow_id`, `call_id`, `occurred_ts` (UTC epoch), `phone_hash` (nullable), `disposition_code`, `payload_json`. `BEFORE UPDATE` / `BEFORE DELETE` triggers abort: append-only is enforced by the database, not by convention. |
| `meta` | key fingerprint, so a wrong/missing key fails loudly instead of silently producing orphans. |

**PII.** Phone: HMAC-SHA256 with a secret key (a plain hash of a 10-digit number is brute-forceable in seconds), which still
allows exact-match linking. Raw phones from events are hashed in memory and never stored. Other PII fields (names, and
the phone itself for re-contact) are Fernet-encrypted. Only the last 4 digits are stored in clear, because the report
needs the masked form. Verified by a test that scans the raw DB bytes for names and numbers.
The key lives outside the DB (env var or a 0600 key file); in production it would come from a KMS/secret manager.

**Idempotency & crash safety.** Each input file is ingested in **one transaction** that also records the file in
`ingested_files`. Re-running skips files already recorded (same name + sha256). A `kill -9` mid-file rolls the whole
file back, so there are no half-ingested files and no double counting (the counters are committed with the data).
Event dedupe uses `PRIMARY KEY` + `INSERT OR IGNORE`, so it is correct across files and drops without a read-before-write.

## Decisions & assumptions
| Decision | Why | Alternative considered |
|---|---|---|
| Link events to leads **at report time**, not at ingest | Late-arriving leads just work; no re-link pass, no stale `lead_id`s | Store `lead_id` on the event and back-fill when leads arrive (more writes, more states to get wrong) |
| Reports recomputed from the ledger on every run | Cannot drift from the ledger; ~4.5 s for 140k leads / 630k events | Maintain incremental aggregates (faster, but a second source of truth) |
| Idempotency by file identity `(kind, workflow, file name)` + sha256; same name with **different** content is refused (exit code 2) | Re-runs are exact no-ops, and silently re-ingesting a changed file would break "append-only" | Row-level idempotency (can't make the duplicate/merge counters idempotent) |
| Per-file counters stored in `ingested_files`; orphan and unknown-disposition counts computed at report time | Counters that depend on *other* data (orphans) must reflect the final state, as the spec says | Count everything at ingest (orphans would be stale) |
| An override links via `call_id` only; a call's lead = the lead of its `call.initiated` event (else of its first linked event) | Spec: overrides have no phone | n/a |
| Events with an unparseable `occurred_at` or no `call_id` count as **orphans** (not malformed) | Spec defines malformed narrowly (bad JSON / no `event_id`); such events can't be placed on a call or in time | Count as malformed |
| Equal `occurred_at` ties broken by `event_id` | Result is deterministic and independent of file/line order | Arrival order |
| `batch_summary` has a row only for batches that own at least one lead | `batch_id` is a property of leads, and the spec defines it that way | Also emit all-zero rows for fully-rejected batches |
| `rejections_by_reason` lists every possible reason (zeros included) | Stable shape for diffing | Only observed reasons |
| Disposition codes matched exactly (case-sensitive, untrimmed) | "not in dispositions.csv" read literally; the generator's bad codes (`RENEWD`, `call_back`) are wrong either way | Normalise case |
| Duplicate-header columns: first wins; header normalisation follows the spec's literal order (lower → trim → collapse) | Deterministic | n/a |
| Dates accept 1-2 digit day/month; `Mon` is the 3-letter abbreviation; decimals round half-up and accept commas anywhere | Lenient on formatting, strict on validity (31/02, negatives rejected) | Strict 2-digit only |
| A non-phone *required* field that is present but invalid → `invalid_<field>` | Spec only defines `invalid_phone`; no shipped schema triggers this | Treat as missing |
| Phone-typed fields are always treated as PII even if a schema forgets `"pii": true` | Fail safe | Trust the schema |
| Unknown-workflow lead folders are skipped with a note, not fatal | One bad folder shouldn't block a day's drop | Hard fail |

## Failure modes considered
- **Bad input rows** (bad phone, missing fields, impossible dates, `N/A` amounts, wrong enum): rejected with a single reason or nulled with a warning, never a crash.
- **Malformed / truncated event lines, missing `event_id`, non-object JSON, bad UTF-8**: counted and skipped (`errors="replace"`).
- **Duplicates**: events (within a file, across files, across drops) and lead rows (merged, last non-null wins).
- **Re-runs / re-delivery:** exact no-op; a changed file under an old name is refused loudly.
- **Crash or `kill -9` mid-run:** per-file atomic commit; tested live (3 kills during a 630k-event ingest, then resume → reports byte-identical to a clean run, `PRAGMA integrity_check` ok) and by an injected-failure unit test.
- **Out-of-order / early events:** handled by report-time linking; override-before-set and set-after-override tested.
- **Wrong or lost key:** detected via a stored fingerprint, clear error instead of silent mass-orphaning.
- **Concurrent writers:** `BEGIN IMMEDIATE` serialises them (SQLite), so two ingests can't interleave inside a file.
- **Not handled (known):** key rotation; schema evolution/back-fills of *corrected* files (would need an explicit "supersede file" flow); lead CSVs are read fully into memory (fine for ~100k rows; events are streamed); SQLite is single-writer.

## Testing
- `pytest` (89 tests, ~3 s):
  - unit: phone (valid/invalid/prefix rules), dates (4 formats, day-first, impossible dates), decimals, enums, header normalisation, timestamps (Z / offset / naive=IST);
  - domain: effective disposition (latest by `occurred_at`, override beats set, tie-break) and bucket precedence;
  - pipeline on hand-made drops: exact counts for every `data_quality.json` key, row numbering with blank rows, merge semantics, defaults, event-before-lead, unknown dispositions, idempotent re-ingest, incremental == all-at-once, crash rollback, append-only triggers, **no plaintext PII in the DB file**, report format;
  - **differential test:** an independent in-memory oracle (no SQL, no hashing) written from the spec, compared with the pipeline's output on 3 generator seeds (1, 7, 2026) for all five files.
- Not tested: concurrent processes, Postgres (SQLite only), non-UTF-8 CSVs beyond not crashing.
- Caveat: the oracle reuses the value parsers/row validator, so an error in a *parser rule* would be shared; those rules have their own targeted tests, but I had no official expected output to diff against.

## Results
| | small (seed 42) | large (seed 42, 144 MB) |
|---|---|---|
| ingest drop_1 / drop_2 | 0.2 s each | 9.3 s / 9.4 s |
| report | <0.1 s | 4.5 s |
| **total** | <1 s | **≈ 23 s** (target < 2 min) |

- Large run: 143,990 lead rows (7,097 rejected, 3,990 merged), 632,602 event lines (631 malformed, 18,490 duplicates, 34,424 orphans, 1,005 unknown dispositions).
- Re-ingesting both drops changed 0 output files; `drop_1`+`drop_2` equals ingesting `data/` in one go (byte-identical).
- Surprising bits of the data: batch_003 renames/reorders columns (handled by alias matching); about 1% of `disposition.set` events carry codes not in the catalogue; a supervisor override can flip a *terminal* disposition back to non-terminal, which moves a lead out of `completed`.

## If I had another day
1. Postgres backend + `docker-compose` profile (same schema; `INSERT ... ON CONFLICT DO NOTHING`), and a `--verify` command that re-checks ledger invariants.
2. Move report aggregation fully into SQL (window functions) and add incremental materialised views keyed by "files since last report".
3. Key rotation (versioned keys on `pii_blob` and `phone_hash`) and a "supersede file" workflow for corrected uploads.
4. **At 100k leads/day:** a scheduler (Airflow/cron) runs `ingest` per drop on arrival and `report` after; idempotency makes retries free; alert on `malformed`/`orphan` rate jumps from `data_quality.json`; backfill = drop the files in the folder and re-run (order-independent thanks to report-time linking); partition `events` by month and keep the hot window indexed.
