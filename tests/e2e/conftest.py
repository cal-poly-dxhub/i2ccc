# The e2e test runs real AWS and Drive calls and is run directly
# (python tests/e2e/confluence_e2e_test.py), so plain `pytest` skips it.
collect_ignore = ["confluence_e2e_test.py"]
