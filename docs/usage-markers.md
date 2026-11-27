# Usage markers

[`usage-markers.json`](usage-markers.json) dates every **deliberate
intervention** aimed at how the cohort's agents use the index, so each usage
report reads against it (telemetry design, § Markers; #35).
`scripts/usage_report.py` splits each host's and repo's periods at the
entries that reached it.

## What is a marker

- A change to the Code Exploration Policy block in a repo's `AGENTS.md`.
- A change to the SocratiCode reminder or health hook.
- A skill change made for this purpose.
- Collection starting on a host.

**Not markers:**

- the routine daily skills bump, even when it carries a skill change: the
  marker is the change, and the bump is its reach;
- a fix to the collector or its keep rules. A data-shape boundary belongs to
  the report's code. For example, MCP calls are identifiable only from #54's
  install, so the report counts an `mcp_tool` record without
  `tool_parameters` as unidentified.

**Whoever ships the intervention adds the entry,** in the same change when it
ships from this repo, or else on the day it merges upstream.

## Format

```json
{"id": "policy-negative-rule-v2", "date": "2026-12-01T18:00:00Z",
 "what": "One line: what changed.", "where": "policy",
 "source": "gregoryfoster/skills#NNN",
 "reach": [{"repo": "notifier", "at": "2026-12-02T04:10:00Z"},
           {"host": "co-index", "at": "2026-12-01T18:00:00Z"}]}
```

| Field | Holds |
|---|---|
| `id` | lower-case slug, unique |
| `date` | when the change itself landed, UTC, ending `Z` |
| `where` | `collection`, `skill`, `policy` (an `AGENTS.md` block) or `hook` |
| `source` | the issue, PR or commit, for the record |
| `reach` | one entry per repo (from `deploy/cohort.json`) or host, with when it took effect there, never before `date` |

Entries stay in date order. `tests/report/test_usage_markers.py` holds the
shape.

## Recording reach

Reach is recorded **by hand when the change is seen to land**, which is not
the merge date:

- **A skill change** reaches a repo at that repo's next daily vendor bump,
  which is the commit that moves `skills-vendor/` past the merge.
- **A policy block** reaches a repo when `init-socraticode` re-runs there.
- **Public repos** are read anonymously, from their commit history.
- **Private repos** are confirmed by the operator.

Add each repo's reach as it lands, by editing the entry. Until a repo's reach
is recorded, the report leaves that repo out of the before-and-after split
rather than guessing.
