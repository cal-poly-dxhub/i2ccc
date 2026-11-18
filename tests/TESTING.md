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

## Page scraper tests

Run from the repo root (`pytest.ini` points at `tests/scraper`); no credentials needed:

```bash
pip install -r requirements.txt -r requirements-dev.txt
pytest
```

With [AWS Emulator](https://docs.aws.amazon.com/emulator/latest/userguide/) running, the S3 store and the Lambda handlers are tested against it as well (otherwise those tests are skipped):

```bash
( . ~/.aws/emulator/9297.env && pytest )
```

The emulator suite covers incremental discovery/download reporting, shared children and
folder cycles, simultaneous submissions, retry ordering, interrupted finalization, pagination,
and 30-day retention. It creates uniquely named tables with the production key/index schema
and TTL setting, then removes them. The fixture refuses non-loopback endpoints.

`tests/scraper/test_cdk_scraper.py` synthesizes the scraper construct and the existing
`RagChatbotStack` in `us-east-1`. The full-stack test substitutes unbundled Lambda source
assets for assertions only, so it needs no Docker builds, AWS credentials, or network access;
the actual deployment's bundling settings remain unchanged. It checks that the scraper
uses the existing ingestion resources and that chat roles receive no scraper permissions.

Python CI runs the scraper/CDK and collector tests. Emulator tests skip when a local endpoint
is not configured; live end-to-end tests remain opt-in.

See [src/scraper/README.md](../src/scraper/README.md).

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

## Test coverage

To check coverage run:

```bash
pytest tests --cov
coverage report --include="ingest_utils/*" # Scope report to certain modules
```

The golden (`tests/e2e/golden/confluence_e2e.json`) and `tests/e2e/last_run.json` are gitignored because they contain real data. Record these yourself.
