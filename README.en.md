# Artemis

Artemis is a Turkish, single-user personal desktop assistant for Windows 11. The core is Python 3.12 and also runs on Linux. The UI is a PySide6 desktop app, with voice and a Telegram channel. Every tool and channel goes through one SafetyGate, with approvals (default answer: no) and taint tracking. The primary documentation language is Turkish.

**Status: technical preview.** The core (safety gate, tools, agent loop, UI, voice pipeline) is tested. 3300+ tests on Windows and Ubuntu CI. Real Windows device acceptance is partial; see docs/RELIABILITY_STATUS.md. A Windows installer is available on the [Releases](https://github.com/UmutErayAltay/artemis/releases) page or can be built from source (see [Windows package](#windows-package-and-first-run)). License: [Apache-2.0](LICENSE); for bundled third-party components (including GPL-3.0 Piper) see [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

## Features

- **Desktop chat and approvals:** chat panel; risky steps (delete, close) and every side effect after external content was read ask for approval, showing the full tool name and validated arguments; default answer is no; voice approvals happen in the on-screen dialog, a spoken "yes" is not approval.
- **Voice pipeline:** wake word "artemis"; Silero VAD as default speech detector (ADR 0067); local faster-whisper STT, optional hybrid cloud STT (OpenAI-compatible or xAI protocol, ADR 0079, 0050); Smart Turn end-of-turn detection (ADR 0068); optional on-device wake-word pre-gate (ADR 0069); Piper TTS by default, Edge TTS only with a separate privacy setting; cloud TTS through any OpenAI-compatible `/audio/speech` model (e.g. OpenRouter speech models) or Gemini TTS, with optional emotion tags such as `[laugh]` for models that support them (ADR 0074); game mode releases the large STT model in full-screen apps; a light local model (Small) takes over, or with `voice.game_mode_target: cloud` Small listens on the CPU for the wake word only and the command goes to the cloud (ADR 0072); sentence-streamed speech (ADR 0043); optional echo cancellation (ADR 0038, WebRTC AEC3) and noise suppression (ADR 0037, DeepFilterNet3), both off by default.
- **Computer use:** sees the screen and works with mouse and keyboard; Esc stops it (ADR 0039). UI Automation is the default observation (`computer_use.observation: uia`): it reads the window's controls by name so text-only models work too (ADR 0073). `hybrid` mode adds a screenshot when the control list is empty; if a window exposes fewer than 5 interactive controls, local Windows OCR text is appended as `VisualText` elements. Clicking an unrequested destructive control (Delete, Sign out, Reset, Pay, Format…), Enter/Space on such a focused control and the Delete key are blocked in code, also in `auto` mode (ADR 0078). Media keys and ratio clicks on sliders/seek bars (`click_element` + `ratio`).
- **Memory:** persistent self-memory in `state_dir/memory.sqlite3` (ADR 0025), optionally encrypted at rest with Windows DPAPI (on for new installs, ADR 0076); optional Obsidian vault read/search tools; vault append is append-only and always approved (ADR 0015).
- **Telegram:** connected from the desktop panel; remote commands; approvals as buttons; default no (ADR 0016, 0052).
- **Code agent and GitHub:** hands coding work to a pluggable code-agent backend (ADR 0023, 0049).
- **Routines, triggers and user skills** (ADR 0058, 0061, 0060).
- **MCP client:** stdio servers listed in `mcp.servers`, none started when empty (ADR 0028). Artemis-owned browser automation MCP server (ADR 0032).
- **Read-only calendar and mail** (ADR 0059); local file search (ADR 0065).
- **Reminders and a morning briefing** (ADR 0027); screen reading with local OCR, images not stored (ADR 0029); window and system control (ADR 0031); start on Windows sign-in (ADR 0030).
- **Providers:** OpenRouter, Anthropic, Ollama, OpenAI-compatible endpoints (ADR 0033), Claude Code CLI subscription (ADR 0035).
- **Interface language:** Turkish or English (Settings > Appearance); with English selected, replies, tool messages and the wake acknowledgement are English and speech recognition can be set to English (ADR 0082).
- **In-app updates:** Settings > About checks GitHub releases and can download the installer, verify its SHA-256 against the release metadata and start it (ADR 0081).

## Videos

Made with Remotion from real screenshots of the demo UI; English narration.

**Product tour (≈110 s)**

https://github.com/user-attachments/assets/a1c41f39-473a-4953-ab1b-045b1ec48f74

**Setup guide (≈35 s)**

https://github.com/user-attachments/assets/d5ebb21b-0178-4df0-acb5-9b4f231e83d5

## Windows package and first run

Download `Artemis-Setup-<version>.exe` (or the portable `Artemis-<version>-windows.zip`) from [Releases](https://github.com/UmutErayAltay/artemis/releases), or build it from source (PyInstaller + Inno Setup, ADR 0075, details in docs/PACKAGING.md):

```powershell
powershell -ExecutionPolicy Bypass -File scripts\build_windows.ps1
# output: dist\Artemis\Artemis.exe; with Inno Setup (ISCC.exe) installed also dist\installer\Artemis-Setup-<version>.exe
```

The installer needs no admin rights. Settings and memory live per user in `%APPDATA%\Artemis`; the example config is copied on first start and uninstalling never touches user data. The installer is not code-signed yet, so SmartScreen may warn.

On first start the **"connect Artemis to a brain"** wizard offers four cards: OpenRouter sign-in without seeing a key (OAuth PKCE; quota and cost stay on the user's own account), Gemini with a Google account (opens the AI Studio key page and detects the copied key), a Claude subscription (`claude` CLI), and an API key, local model or proxy (Ollama, LM Studio, Groq). The wizard looks for OpenAI-compatible services running on this machine (Ollama, LM Studio, LiteLLM, local proxies such as CLIProxyAPI), only on `127.0.0.1`, and connects one with a click; Artemis never installs or runs a proxy itself. Keys are stored only in Windows Credential Manager. Signing in with another app's identity (e.g. Gemini CLI) is deliberately not supported. Settings > About offers a manual update check (version query only).

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

Copyright 2026 Umut Eray Altay. Licensed under the [Apache License, Version 2.0](LICENSE). The Windows installer also bundles third-party components under their own licenses (Piper TTS: GPL-3.0-or-later; PySide6 and edge-tts: LGPL-3.0; certifi: MPL-2.0), so the binary package as a whole is distributed under GPL-3.0 terms; see [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
