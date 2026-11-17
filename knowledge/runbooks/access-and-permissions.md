---
title: Access and permissions errors
failure_class: permissions
owner: data-platform-team
last_reviewed: 2026-10-03
---

# Access and permissions errors

The job identity is not allowed to read from or write to a catalog, schema or table.

## Symptoms

- The run fails when it reaches a stage that touches a new object, often the final write.
- Earlier stages succeed; the error names the object and the missing privilege.

## Typical errors

- `[UNAUTHORIZED_ACCESS] Unauthorized access: PERMISSION_DENIED: User does not have CREATE TABLE on Schema '...'`
- `PERMISSION_DENIED: User does not have USE SCHEMA / SELECT / MODIFY on ...`
- `[INSUFFICIENT_PERMISSIONS]`

## Diagnosis

1. Read which privilege is missing (`CREATE TABLE`, `USE SCHEMA`, `SELECT`, `MODIFY`) and on
   which object.
2. Check whether the target is where the pipeline is supposed to write. A target outside
   the pipeline's own schema (for example a read-only shared catalog) usually means a
   configuration mistake, not a missing grant.
3. Check recent changes to output table configuration (`gold_table`) and ownership or grants
   on the target schema.
4. Confirm which identity the job runs as.

## Resolution

- **Wrong target:** point the output back to the pipeline's own schema
  (`workspace.pipeline_demo`) and rerun.
- **Legitimate new target:** request `USE CATALOG`, `USE SCHEMA` and `CREATE TABLE` (or
  `MODIFY`) on the target from its owner; rerun after the grant.
- Never write to read-only shared catalogs such as `samples`.

## Escalation

Grant requests go to the owner of the target catalog. Automated remediation must never
change grants.

## Prevention

- Validate output table names against an allow-list of schemas at deploy time.
- Keep grants in code (reviewed), not ad hoc.
