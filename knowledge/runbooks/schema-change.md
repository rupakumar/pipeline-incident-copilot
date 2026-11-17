---
title: Schema change on write
failure_class: schema_change
owner: data-platform-team
last_reviewed: 2026-10-03
---

# Schema change on write

A write to an existing Delta table was rejected because the incoming data's schema does
not match the table's schema.

## Symptoms

- The run fails at the stage that appends to an existing table (usually silver, on
  incremental loads).
- Full-refresh runs of the same job may still succeed, because overwrite can replace the schema.

## Typical errors

- `[DELTA_METADATA_MISMATCH] A metadata mismatch was detected when writing to the Delta table`
  with the sub-error "A schema mismatch detected when writing to the Delta table"
- The error prints both schemas: **Table schema** (existing) and **Data schema** (incoming).
- `To enable schema migration using DataFrameWriter or DataStreamWriter, please set: '.option("mergeSchema", "true")'`

## Diagnosis

1. Compare the "Table schema" and "Data schema" blocks in the error: which columns are new,
   missing, or have a different type?
2. Find what introduced the difference: a new enrichment column, an upstream format change,
   or a code change in the parsing step. Check recent commits and job parameter changes
   (`enrich_columns`, `level_encoding`, `silver_write_mode`).
3. Confirm whether the run was incremental (`append`) — the incremental path does not evolve
   the schema.
4. Check downstream consumers: will a new column or a type change break them?

## Resolution

- **Intended new column:** evolve the table first — `ALTER TABLE ... ADD COLUMN`, or enable
  `mergeSchema` on the incremental write after review — then rerun.
- **Unintended change:** revert the configuration or code change that added or changed the
  column and rerun.
- **Type change:** do not silently cast. Plan a migration: rewrite the table with the new type
  in a controlled full refresh, and update consumers.

## Escalation

Schema changes that affect gold or published tables need sign-off from the dashboard owners
before rollout.

## Prevention

- Treat enrichment columns as schema changes: ship the `ALTER TABLE` with the feature.
- Note that some type changes do not fail at all (for example integers appended into a string
  column are cast silently). Add schema assertions for critical columns.
