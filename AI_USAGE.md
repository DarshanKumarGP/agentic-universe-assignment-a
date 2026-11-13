# AI Usage Disclosure

## Tools used
- Claude (Anthropic), via claude.ai chat, with its code-execution sandbox.

## What I used them for
- Comparing the two assignments and choosing A.
- Claude wrote the first complete implementation: the `ledger/` package, all tests (unit, pipeline, and the differential oracle), `SUBMISSION.md`, and this file. I gave it the assignment text, the schemas, the data generator and the requirement to deliver a complete, tested, submission-ready project.
- Claude generated the large dataset and ran the pipeline, the test suite, the timing runs and the `kill -9` experiment in its sandbox; the numbers in `SUBMISSION.md` come from those runs.

## What I wrote or decided myself
- Choosing Assignment A over B, and asking for a complete, requirement-oriented solution.
- _<Add your own bullets here before submitting: for example which design decisions in SUBMISSION.md you reviewed and agree with, anything you changed after reading the code, and the commands you ran yourself. Keep this honest: reviewers will ask you to explain and change the code live.>_

## Where the AI was wrong or unhelpful
- **CLI options:** the first run used `--db` *before* the sub-command (`ledger --db x ingest ...`), which my argparse layout rejects; the options are defined per sub-command. Caught on the first smoke run, so the documented commands put options after the sub-command.
- **A wrong test:** a phone test asserted `9198765432` (10 digits starting "91") is invalid. The code was right: it is a valid 10-digit number starting with 9. The test, not the implementation, was wrong. Caught because the suite failed on first run.
- **Default schema path:** the default `--schemas` path assumes the package sits at `<repo>/solution/ledger`; in the sandbox it was laid out differently, so smoke tests used `$LEDGER_SCHEMAS`. I confirmed it with a clean-clone layout test before packaging.

## How I verified AI-generated code
- 89 automated tests, including a differential test against an independent in-memory reference written from the spec, run on three random seeds.
- Idempotency checks on the large dataset (re-ingest changes 0 output files; incremental equals all-at-once, byte-identical).
- Three `kill -9` interruptions during a large ingest, then resume, with reports compared to a clean run.
- Reading the code myself: _<tick this off honestly after you have actually read through every module>_.
