# Project automation

Cato discovers Python, Node, Rust, and Go projects and can inspect safe Git state.
For Python projects it can run two bounded commands without a shell:

- the active Python interpreter with `-m pytest -q`
- the active Python interpreter with `-m ruff check .`

Both require explicit approval because repository configuration and tests execute
project-controlled code. Working directories must resolve inside an approved
root. Test targets cannot be absolute or contain `..`. Runtime and captured output
are capped, and pytest result counts are extracted into structured data.

Cato does not install dependencies, run arbitrary package scripts, format files,
or modify source automatically.
