# Testing

Run from the repo root. Uses mocks so no credentials needed.

## Drive/confluence collector test

Test against prior ingest utils output:

```bash
pip install -r requirements-dev.txt
pytest tests/ingest_utils/test_collector_golden.py
```
This compares what will land in S3 against `tests/ingest_utils/golden/collector_fresh_bucket.json` or `tests/ingest_utils/golden/collector_fresh_bucket.json` with skip_existing enabled.

Reset the golden after an intentional behavior change:

```bash
UPDATE_GOLDEN=1 pytest tests/ingest_utils
git diff tests/ingest_utils/golden/
```

## Live end-to-end testing

To prevent regressions across the whole system, we provide e2e test using `i2ccc/tests/e2e/fixtures/calendar_rows.html` as a sample confluence page and check it against OpenSearch to ensure that the data is consistent across refactors. This calls AWS and Drive. `pytest` skips this by default.

Record the gold standard of OpenSearch output (do this once before any refactoring):
The only time this will be recorded is at the start of development, and whenever new functionality that has a different expected
output is implemented. Note that this output will depend on some config settings, 
such as chunk size and embedding model, so keep these consistent across runs.

```bash
python tests/e2e/confluence_e2e_test.py --record-golden
```

Compare the current deploy against it:

```bash
python tests/e2e/confluence_e2e_test.py
```

Remove leftovers from an interrupted run:

```bash
python tests/e2e/confluence_e2e_test.py --cleanup
```

The golden (`tests/e2e/golden/confluence_e2e.json`) and `tests/e2e/last_run.json` are gitignored because they contain real data. Record these yourself.
