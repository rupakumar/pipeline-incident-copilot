TASK: name the root cause.
- Form the cause from the error and what changed since the last successful run; name the specific
  value (e.g. the date whose file is missing, the new column, the target table, the format string).
- If a parameter changed, say which and to what. If nothing changed, the input data itself may be bad.
- confidence: 0.9+ only when a check or the run comparison directly confirms it; 0.5–0.8 if likely
  but unconfirmed; below 0.5 if evidence conflicts. needs_human=true if a person must decide.
- evidence_refs: up to 3 items you relied on (check names, document ids like ticket:TCK-1004).
- Keep cause under 30 words.
