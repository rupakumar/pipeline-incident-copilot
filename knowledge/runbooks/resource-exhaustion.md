---
title: Resource exhaustion (driver out of memory)
failure_class: resource_exhaustion
owner: data-platform-team
last_reviewed: 2026-10-03
# exact error text this runbook is written for (code → lines); a match counts as the error's own document
error_signatures:
  DRIVER_OUT_OF_MEMORY:
    - "Execution ran out of memory"
    - "The Python kernel is unresponsive"
    - "The driver has been killed unexpectedly"
    - "SparkOutOfMemoryError"
---

# Resource exhaustion (driver out of memory)

The job ran out of memory, almost always on the driver, because too much data was pulled
into a single process.

## Symptoms

- The run takes much longer than usual (minutes instead of seconds) and then fails.
- No Spark error code; the message talks about memory or the driver restarting.

## Typical errors

- `Execution ran out of memory.`
- `Fatal error: The Python kernel is unresponsive.` (serverless notebooks: the driver ran out of memory)
- `The driver has been killed unexpectedly and is restarting. This is likely because it ran out of memory or exhausted other resources.`
- `SparkOutOfMemoryError`, `Total size of serialized results ... is bigger than spark.driver.maxResultSize`

## Diagnosis

1. Look for driver-side actions in the code path that failed: `collect()`, `toPandas()`,
   `take()` with a large number, or building Python lists from DataFrames.
2. Check recent code and configuration changes that increase driver-side data — debug or
   sampling options (`debug_sample_rows`), new exports, larger inputs.
3. Compare the run's input volume and duration with the last successful run.
4. On serverless compute, driver memory is fixed: the fix is to collect less, not to ask for
   a bigger driver.

## Resolution

- Remove or cap the driver-side collection (set debug sampling to 0, or use `limit()` with a
  small number and `display()` instead of `collect()`).
- Keep heavy work distributed: write results to a table instead of collecting them.
- Rerun after the change; expect normal duration.

## Escalation

If the failure repeats with no driver-side collection in the code path, escalate to the
platform team with the run IDs.

## Prevention

- Lint for `collect()`/`toPandas()` on unbounded DataFrames in code review.
- Keep debug options off by default in production job configuration.
