TASK: propose the fix for the root cause.
- steps: 2–3 short, concrete steps a person follows, following the matching runbook and how past
  tickets were resolved, adapted to this incident.
- action: the single automated rerun that fixes it, if one exists within the allowed actions;
  otherwise null — e.g. when an upstream file is missing or corrupted, a table schema must be
  altered, or a permission granted first. Resetting a wrongly changed parameter to its default ("")
  is usually right when the run comparison shows that parameter changed. Never rerun with the same
  parameters that failed, and never invent a new value: only resets or the last working values are
  allowed (anything else is refused and goes to a person).
- If a "Candidate fix" is listed below and it undoes the change that caused the failure, use it as
  the action; reject it (action null) only if the failure needs a person first.
- risk: low / medium / high. verify: one check that confirms the fix worked.
