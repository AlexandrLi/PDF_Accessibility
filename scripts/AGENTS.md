# Accessibility Python environment

Use the repository's shared root `.venv` for every accessibility, migration, and
PDF sweep command. Do not create a temporary virtual environment or use
`scripts/.venv`.

Run Python through:

```sh
scripts/with-a11y-python.sh <python arguments>
```

For the complete issue-map workflow, use:

```sh
scripts/accessibility-course-workflow.sh prepare <course-id>
scripts/accessibility-course-workflow.sh plan <compact-manifest>
```

After the user manually approves the generated PDFs in Adobe, compute the
manifest SHA-256 and publish that exact manifest:

```sh
scripts/accessibility-course-workflow.sh publish <compact-manifest> \
  --approved-sha <sha256>
```

If Adobe manually passes a topic that the local checker marked residual or
unverifiable, add that exact topic explicitly with
`--approved-exception <topic-id>`. Never apply a course-wide exception.

If a repaired PDF intentionally renders differently from the original (for
example an Adobe Auto-Tag or OCR rebuild accepted with
`--allow-render-change`), the manifest must record `renderIdentical: false`
honestly. Publishing such a topic requires the user to visually review it and
pass that exact topic with `--approved-render-exception <topic-id>`. Never
set `renderIdentical: true` for a file whose rendered pages changed, and never
apply a course-wide render exception.

Never publish before explicit user approval. The publish command is
fail-closed: it preflights every object, verifies recovery for every object,
then replaces and reads back each PDF. Use its recorded report for rollback:

```sh
scripts/accessibility-course-workflow.sh rollback <replacement-report>
```

The launcher validates the required PDF/AWS packages and installs
`scripts/requirements-migrate.txt` only when the shared environment is missing
them.
