# AI Usage Disclosure

## Tools used
- Claude (Anthropic), through the claude.ai chat, including its code-execution sandbox.

## What I used them for
- Comparing the two assignments (A: data pipeline, B: workflow dispatcher) and deciding which one I could build and explain best.
- Claude wrote the implementation: the `ledger/` package, all tests (unit, pipeline, and an independent reference implementation used for differential testing), and the first drafts of `SUBMISSION.md` and this file. I gave it the assignment text, the schemas, the data generator and the requirement for a complete, tested, submission-ready project.
- In its own sandbox, Claude generated the large dataset and ran the timing runs and the `kill -9` experiment; the performance numbers in `SUBMISSION.md` come from those runs.

## What I wrote or decided myself
- I chose Assignment A over B after comparing the two, mainly because A is deterministic and easier to test and explain.
- I set the requirements for the build: a complete solution covering the full core scope, with tests, idempotent re-runs and PII protection.
- I set up the project from a clean clone on my own Windows machine (Python 3.13), installed the requirements, generated the data with seed 42, ran `ingest` for both drops and `report`, and ran the full test suite myself: 89 passed. I also checked what would be committed so that generated data, the database and the key file stay out of the repo.
- I filled in the candidate details and the time spent in `SUBMISSION.md`, and I decided to disclose the AI use in full.
- Most of the design (report-time linking, per-file transactions, the keyed phone hash) was proposed by Claude. I went through it before submitting so I can explain it and change it live.

## Where the AI was wrong or unhelpful
- **CLI options:** Claude's first run put `--db` before the sub-command (`ledger --db x ingest ...`), which the argparse layout rejects, because the options are defined on each sub-command. It showed up on the first smoke run, so the documented commands put options after the sub-command.
- **A wrong test:** a phone test asserted that `9198765432` (10 digits starting with "91") is invalid. The code was right, since it is a valid 10-digit number starting with 9, and the test was wrong. The suite failed on its first run, which is how it was found.
- **Setup instructions:** Claude's first instructions assumed the zip contained the whole repo, but it only contained the solution files. My `generate_data.py` command failed with "file not found" until I copied them into a clone of the original repo.

## How I verified AI-generated code
- 89 automated tests, including a differential test against an independent in-memory reference written from the spec, run on three random seeds (in Claude's sandbox and again on my machine).
- On my machine: ran both drops and the report end to end, and ran `pytest` in a fresh virtual environment.
- In Claude's sandbox: idempotency checks on the large dataset (re-ingest changed 0 output files; ingesting drop by drop equalled ingesting everything at once), and three `kill -9` interruptions during a large ingest followed by a resume, with the reports compared to a clean run.
- Before submitting I read through the code module by module (`reports.py`, `leads.py`, `events.py`, `db.py`, `crypto.py`) so I can explain it.
