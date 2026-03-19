# Deferred Items - Phase 05

## Pre-existing Test Failure

- **File:** tests/test_history_logger.py::TestTestCompletion::test_on_test_completed
- **Error:** TypeError: the JSON object must be str, bytes or bytearray, not dict
- **Root cause:** Test passes a dict where json.loads expects a string (signal marshalling mismatch)
- **Discovered during:** 05-01 Task 3 verification
- **Not caused by:** Phase 05 changes (confirmed via git stash test)
