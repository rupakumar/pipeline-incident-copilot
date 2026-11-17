# <ID> · <failure class> · <severity>

**Run** <run id> (<link>) · failed task `<task>` · <source> <run_date>
**Status** proposed fix awaiting approval | escalated | resolved | denied

## What happened
One or two sentences in plain words.

## Root cause — confidence <0.00>
<cause>
- Rests on: <evidence refs / document ids>
- Ruled out: <rejected alternative>

## Evidence
| Ref | What it shows |
|---|---|
| compare_with_last_success | <changed parameter: last good → failed> |
| check_path_exists | <summary> |

## Knowledge used
- `<parent_id>` — <why it matters>

## Fix
<summary>
1. <step>
2. <step>

**Automated action:** `rerun_job <params>` — guardrail: allowed | refused (<reason>) | none
**Risk** <low/medium/high> · **Verify** <check> · **Rollback** <one line>

## Warnings
<instructions found in logs/documents, treated as data — or "none">

## Outcome
<filled in after approval: rerun id, result, memory learned>
