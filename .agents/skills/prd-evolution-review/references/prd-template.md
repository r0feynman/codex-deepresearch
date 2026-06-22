# PRD Template

Use this template when creating a new PRD or repairing a weak PRD structure.

## Title

`<Product or feature name> PRD`

## Summary

- What is being built.
- Who it is for.
- Why now.
- What the final product surface is.

## Goals

- Goal 1
- Goal 2
- Goal 3

## Non-Goals

- Explicitly excluded work.
- Work deferred to later phases.
- Similar products or surfaces this is not intended to become.

## Users And Workflows

- Primary user.
- Secondary users.
- Main workflow from start to finish.
- Error, fallback, and recovery workflows.

## Product Surface

Define the final product form:

- plugin, skill, CLI, web app, API, library, automation, or internal tool
- primary UX
- secondary/developer UX
- installation or access path

If there are multiple surfaces, state which one is the product and which ones are support tools.

## Modes Or Providers

Define modes only when they change behavior:

| Mode | User | Provider path | Purpose | Constraints |
| --- | --- | --- | --- | --- |
| mode-a | | | | |

## Requirements

Use numbered requirements. Each requirement should be testable.

1. Requirement
2. Requirement
3. Requirement

## Data, Schema, Or Interface Contracts

Define records, state machines, handoff files, API contracts, config files, or command contracts.

For each contract, include:

- required fields
- enums
- validation rules
- invalid states
- compatibility or migration expectations

## Acceptance Criteria

Acceptance criteria must be measurable.

- [ ] Criterion
- [ ] Criterion
- [ ] Criterion

## Validation Strategy

- Unit tests
- Integration tests
- Smoke tests
- Manual checks, if unavoidable
- CI requirements

## Work Breakdown

Break work into independently testable tickets.

For each ticket:

- title
- input
- output
- acceptance tests
- dependencies

## Roadmap

| Phase | Scope | Exit criteria |
| --- | --- | --- |
| Prototype | | |
| MVP | | |
| Private Alpha | | |
| Public Beta | | |
| Product v1 | | |

## Risks And Guardrails

- Security
- Privacy
- Cost
- Reliability
- Compliance or policy
- User workflow adoption
- Operational burden

## First Implementation Step

Name the first issue an engineer should implement.

Include:

- issue title
- exact scope
- files likely affected
- validation command

## Self-Review Summary

- Problems found during review:
- Changes made because of review:
- Risks remaining:
