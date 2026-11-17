---
title: Missing input data
failure_class: missing_input
owner: data-platform-team
last_reviewed: 2026-10-03
---

# Missing input data

The job could not find the file or folder it was asked to read, usually at the first
(bronze) stage.

## Symptoms

- The run fails within the first minute, before any table is written.
- Bronze row counts for the run are missing; downstream tables keep yesterday's data.

## Typical errors

- `[PATH_NOT_FOUND] Path does not exist: dbfs:/Volumes/...`
- `[UC_VOLUME_NOT_FOUND] Volume ... does not exist`
- `FileNotFoundException` or `No such file or directory`

## Diagnosis

1. Read the path in the error message exactly — catalog, schema, volume, folder and file name.
2. List the parent folder in the volume. Is the file there under a different name
   (compressed `.gz`, different case, different date)?
3. If the path contains a date partition, check whether the upstream delivery for that date
   has arrived. Compare with the last successful run's input path.
4. If the volume itself is reported missing, check for a typo or a renamed volume in the
   job's input configuration.
5. Check recent changes to the job parameters (`input_path`) and to the deploy configuration.

## Resolution

- **File not delivered yet:** confirm with the upstream owner when it will land; rerun after
  it arrives. Do not point the job at a different date to "make it pass".
- **Wrong path or file name:** correct `input_path` (or remove the override to use the
  default landing file) and rerun.
- **Wrong volume:** restore the volume name in the configuration; never create an empty volume
  just to silence the error.

## Escalation

If the delivery is more than 2 hours late against the 06:00 UTC SLA, page the upstream owner
and notify dashboard consumers that today's numbers are delayed.

## Prevention

- Add a file-arrival check before the job starts.
- Validate `input_path` overrides against the volume layout at deploy time.
