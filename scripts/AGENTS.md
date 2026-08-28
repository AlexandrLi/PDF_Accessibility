# Accessibility Python environment

Use the repository's shared root `.venv` for every accessibility, migration, and
PDF sweep command. Do not create a temporary virtual environment or use
`scripts/.venv`.

Run Python through:

```sh
scripts/with-a11y-python.sh <python arguments>
```

For issue-map sweeps, use:

```sh
scripts/sweep-accessibility-issue-map.sh <sweep arguments>
```

The launcher validates the required PDF/AWS packages and installs
`scripts/requirements-migrate.txt` only when the shared environment is missing
them.
