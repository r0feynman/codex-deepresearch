# PRD Review Rubric

Score out of 10. A PRD is implementation-ready only if it scores at least 9.0 and has no blocking findings.

## Scorecard

| Area | Weight | What Good Looks Like |
| --- | ---: | --- |
| Product direction | 1.0 | Final product surface and primary workflow are unmistakable. |
| Implementable execution contract | 1.5 | Commands, APIs, handoffs, states, or interfaces are specific enough to build. |
| Mode/provider/interface separation | 1.0 | Distinct modes are not mixed together. Helper tools are not confused with the product. |
| Data/schema specificity | 1.5 | Required fields, enums, validation rules, and invalid states are defined. |
| MVP work breakdown | 1.0 | MVP is split into independently testable tickets with dependencies. |
| Acceptance and validation quality | 1.0 | Acceptance criteria are measurable and tied to tests or smoke checks. |
| Risk, policy, and cost handling | 1.0 | High-risk issues are addressed in MVP, not deferred without justification. |
| Roadmap beyond MVP | 1.0 | Later phases show a path to product quality without bloating MVP. |
| Goal alignment | 1.0 | The PRD does not drift away from the user's original goal. |

## Severity Definitions

`blocking`:

- Prevents implementation.
- Contradicts product direction.
- Makes acceptance impossible to verify.
- Hides a required API, schema, or workflow behind vague language.

`major`:

- Implementation is possible but likely to diverge.
- Important edge cases, states, or test cases are missing.
- Work breakdown is too large or too coupled.

`minor`:

- Improves clarity or maintainability.
- Can be deferred without changing the MVP contract.

## Reviewer Checklist

Ask these questions:

- What would two engineers interpret differently?
- What would an implementation agent have to invent?
- Which workflow step lacks input/output definition?
- Which claim cannot be tested?
- Which "MVP" item is actually post-MVP?
- Which post-MVP item is actually required for basic usefulness?
- Where can cost, security, privacy, or policy failure appear?
- Is the final report, artifact, API response, UI, or command output defined?
- Are there clear blocked, failed, partial, and retry states?
- Is there a first issue small enough to implement now?

## Finding Format

Use this format:

```text
Severity: blocking|major|minor
Section: <section or file>
Finding: <problem>
Why it matters: <impact>
Required change: <concrete edit>
```

## Pass Conditions

Pass when all are true:

- Score is at least 9.0.
- No blocking findings remain.
- The MVP backlog can be copied into issues without additional decomposition.
- The first implementation issue is clear.
- The PRD has an explicit self-review summary.
