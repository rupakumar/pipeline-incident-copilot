---
title: Data-quality gate breach
failure_class: data_quality
owner: data-platform-team
last_reviewed: 2026-10-03
# exact error text this runbook is written for (code → lines); a match counts as the error's own document
error_signatures:
  DATA_QUALITY_FAILED:
    - "DATA_QUALITY_FAILED"
---

# Data-quality gate breach

The data-quality gate stopped the run because too many parsed rows are invalid, to keep bad
data out of gold tables.

## Symptoms

- Bronze and silver were written; the run fails before gold.
- The error reports the share of rows that failed the check.

## Typical errors

- `AssertionError: DATA_QUALITY_FAILED: <N>% of rows in ..._logs_silver have null event_time (threshold 5%)`
- Related Spark parsing errors when parsing is strict: `[CANNOT_PARSE_TIMESTAMP]`

## Diagnosis

1. Read the failure rate — it tells you which kind of problem you have:
   - **close to 100%:** the parser cannot read *any* timestamps → a format/configuration
     problem (wrong `timestamp_format`, wrong source, format change upstream).
   - **a partial share (for example 10–50%):** most lines parse, some do not → bad or
     corrupted upstream data in this delivery.
2. Compare the configured `timestamp_format` with a few raw lines in bronze.
3. For partial failures, sample bronze rows whose silver `event_time` is null and look at the
   raw text: truncated lines, garbled timestamps, mixed formats.
4. Check recent configuration changes and which upstream delivery (date partition) was read.

## Resolution

- **Wrong format configuration:** remove the `timestamp_format` override or set it to the
  source's native format; rerun.
- **Corrupted delivery:** quarantine the file, ask the upstream owner to resend it, and rerun
  on the clean file. Do not lower the 5% threshold to get the run through.

## Escalation

Corrupted deliveries go to the upstream data owner; repeated corruption needs a contract
check on the export.

## Prevention

- Validate timestamp format overrides against a sample before deploy.
- Add a lightweight schema/format check on arrival for upstream files.
