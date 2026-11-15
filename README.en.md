# Artemis

Artemis is a Turkish, single-user personal desktop assistant for Windows 11. The core is Python 3.12 and also runs on Linux. The UI is a PySide6 desktop app, with voice and a Telegram channel. Every tool and channel goes through one SafetyGate, with approvals (default answer: no) and taint tracking. The primary documentation language is Turkish.

**Status: technical preview.** The core (safety gate, tools, agent loop, UI, voice pipeline) is tested. Real Windows device acceptance is partial; see docs/RELIABILITY_STATUS.md. There is no prebuilt installer; install from source.

## Features

- **Desktop chat and approvals:** chat panel; every side-effecting step asks for approval showing the full tool name and validated arguments; default answer is no; voice approvals happen in the on-screen dialog, a spoken "yes" is not approval.
- **Voice pipeline:** wake word "artemis"; Silero VAD as default speech detector (ADR 0067); local faster-whisper STT, optional hybrid cloud STT (OpenAI-compatible or xAI protocol, ADR 0046, 0050); Smart Turn end-of-turn detection (ADR 0068); optional on-device wake-word pre-gate (ADR 0069); Piper TTS by default, Edge TTS only with a separate privacy setting; sentence-streamed speech (ADR 0043); optional echo cancellation (ADR 0038, WebRTC AEC3) and noise suppression (ADR 0037, DeepFilterNet3), both off by default.
- **Computer use:** sees the screen and works with mouse and keyboard; Esc stops it (ADR 0039).
- **Memory:** persistent self-memory in `state_dir/memory.sqlite3` (ADR 0025); optional Obsidian vault read/search tools; vault append is append-only and always approved (ADR 0015).
- **Telegram:** connected from the desktop panel; remote commands; approvals as buttons; default no (ADR 0016, 0052).
- **Code agent and GitHub:** hands coding work to a pluggable code-agent backend (ADR 0023, 0049).
- **Routines, triggers and user skills** (ADR 0058, 0061, 0060).
- **MCP client:** stdio servers listed in `mcp.servers`, none started when empty (ADR 0028). Artemis-owned browser automation MCP server (ADR 0032).
- **Read-only calendar and mail** (ADR 0059); local file search (ADR 0065).
- **Reminders and a morning briefing** (ADR 0027); screen reading with local OCR, images not stored (ADR 0029); window and system control (ADR 0031); start on Windows sign-in (ADR 0030).
- **Providers:** OpenRouter, Anthropic, Ollama, OpenAI-compatible endpoints (ADR 0033), Claude Code CLI subscription (ADR 0035).

## Install

Python 3.12. Windows PowerShell:

```powershell
$env:PYTHONUTF8 = '1'
py -3.12 -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
Copy-Item config/config.example.yaml config/config.yaml
New-Item -ItemType Directory -Force desktop
```

Linux:

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
cp config/config.example.yaml config/config.yaml
mkdir -p desktop
```

Relative paths in the YAML resolve against the config file's directory; set `workspace: ../desktop` for file tools. The example config deliberately has no model; add one as described in docs/PROVIDERS.md and set `primary_model`. Paid model use needs explicit permission (`allow_paid: true`); keep the default `false` otherwise.

## First run

```bash
python -m artemis chat --config config/config.yaml
```

Type `/exit` to quit and `/yeni` for a new chat. Desktop with voice:

```powershell
python -m artemis desktop --ses --config config/config.yaml
```

The microphone opens only with the Voice button or `--ses`. A first voice trial with headphones is recommended.

## Secrets

Secrets come only from environment variables or the Windows Credential Manager, never from YAML, argv, logs, tests or commits. Examples: `OPENROUTER_API_KEY`, `ANTHROPIC_API_KEY`, `ARTEMIS_<ID>_API_KEY` for OpenAI-compatible endpoints (or Credential Manager entry `Artemis/<id>`). The Telegram token comes from an environment variable whose name ends in `_TOKEN`. The project does not auto-load a `.env` file. Subprocesses receive only the allowed part of the environment.

## Tests and checks

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\ruff.exe check .
.\.venv\Scripts\mypy.exe
.\.venv\Scripts\python.exe -m bench.run --sahte
```

Default tests never touch the real machine; tests marked `disruptive` (visible side effects) are skipped by default and run only on a prepared machine with `pytest -m disruptive`. A passing fake bench is not proof of live model quality.

## Documentation

- [README.md](README.md) (Turkish, full user guide; the primary language)
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)
- [docs/adr/README.md](docs/adr/README.md) (architecture decision records index)
- [docs/RELIABILITY_STATUS.md](docs/RELIABILITY_STATUS.md)
- [CONTRIBUTING.md](CONTRIBUTING.md)
- [docs/TESTING.md](docs/TESTING.md)

The primary documentation language is Turkish.

## License

License: not yet chosen (pending). Until a license is added, all rights are reserved by the author.
