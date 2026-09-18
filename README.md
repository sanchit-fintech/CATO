# Cato

Cato is currently a local-first, command-line personal-agent prototype for macOS.
It uses a model provider to understand a request, selects a registered tool, runs
that tool inside a configured safety boundary, and summarizes the result.

## Current capabilities

- Search for files and directories by name inside approved roots.
- Read non-sensitive text files inside approved roots.
- Use Gemini for request classification and response generation.
- Use a fake model provider for deterministic, offline tests.
- Reject traversal, symlink escapes, invalid tool calls, and obvious sensitive files.

Cato cannot yet modify files, control applications, run shell commands, retain
memory, accept voice input, execute multi-step plans, or communicate with mobile
devices.

## Architecture

`core.cato.Cato` coordinates a `ModelProvider` and the `ToolRegistry`. Gemini is
implemented in `core/llm/gemini.py`; tests inject `FakeModelProvider`. Registered
tools contain metadata and argument contracts and return structured `ToolResult`
objects. Filesystem tools canonicalize every requested path against configured
approved roots.

## Setup

Cato requires Python 3.11 or newer.

```bash
cd /path/to/Cato
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e '.[dev]'
cp .env.example .env
```

Edit `.env` and set your own `GEMINI_API_KEY`. Never commit that file.

## Configuration

- `GEMINI_API_KEY`: required for the real Gemini provider.
- `GEMINI_MODEL`: model name; defaults to `gemini-3.6-flash`.
- `CATO_APPROVED_ROOTS`: approved paths separated by `:` on macOS/Linux. The
  safe default is the directory from which Cato starts.
- `LOG_LEVEL`: Python logging level; defaults to `INFO`.

Relative approved roots are resolved at startup. Prefer narrow roots, for example:

```dotenv
CATO_APPROVED_ROOTS=/Users/you/Projects:/Users/you/Documents/CatoInbox
```

## Run

From the repository root with the virtual environment active:

```bash
python -m core.cato
```

After editable installation, `cato` is an equivalent command. Directly running
`python core/cato.py` is not supported because Cato is a Python package.

## Test and lint

Tests are offline and use temporary directories:

```bash
pytest
ruff check .
```

## Security model

File tools are limited to canonical paths below `CATO_APPROVED_ROOTS`. Parent-path
and symlink escapes are denied. Searches are bounded and hide obvious sensitive
filenames. Reads of `.env`, private keys, credential files, and token/secret files
return an approval-required result; their contents are not sent to the model.

This is an early baseline, not a complete sandbox. Run Cato as a normal user,
configure narrow approved roots, review those roots carefully, and do not grant
the process unnecessary macOS permissions. The approval UI and comprehensive
sensitive-data classification are future work.

Logs record startup, selected tool names, execution, and failures. They do not log
commands, API keys, file contents, or tool arguments.

## Short roadmap

1. Expand typed tool and permission policies.
2. Add carefully approved Mac and file actions.
3. Add voice input/output and local memory.
4. Add bounded multi-step task execution and coding assistance.
5. Add authenticated integrations and Apple-device communication.
