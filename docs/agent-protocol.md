# LLM Agent Protocol

## Session start

1. Call `harness context --topic <work topic> --budget <context budget>`.
2. Read current supported, refuted, exploring, and superseded conclusions.
3. Read active requirements and related historical tasks.
4. Define a fixed goal that describes the work outcome, not the hoped-for hypothesis result.
5. Define success criteria, stop conditions, and constraints.
6. Create the task before changing code or running an experiment.

## During work

- Use `harness run` for commands that affect research or engineering state.
- Record externally meaningful observations with `harness task step`.
- Record plan changes with `harness task revise-plan`; do not rewrite earlier events.
- Capture logs, raw outputs, scripts, reports, data manifests, and build outputs as Evidence.
- Keep secrets out of commands, logs, metadata, and environment snapshots.
- Do not mark a task failed merely because a hypothesis was refuted.
- Do not mark a requirement verified because code was merged.

## Research completion

1. Confirm the execution itself is complete and evidence is accessible.
2. Mark the Task `succeeded` or `failed`.
3. Set a research result type: `positive`, `negative`, or `inconclusive` when useful.
4. Update a Conclusion only through evidence-backed commands.
5. Prefer a narrower replacement Conclusion over silently rewriting an old Claim.

## Engineering completion

1. Capture the code Change against explicit base and head commits.
2. Capture the Build artifact and digest.
3. Run or import a Test Specification whose pass criteria existed before the result.
4. Move a Requirement to `verified` only with a covered passing Test Run.

## Final consistency check

```bash
harness render
harness doctor
```

Resolve all error-level findings before presenting work as complete.
