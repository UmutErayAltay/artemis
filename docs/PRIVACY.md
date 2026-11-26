# Privacy Policy

Artemis is a local-first personal desktop assistant for Windows (with core support on Linux). All data, configuration, conversation history, and voice processing stay on your device unless you explicitly configure or invoke an external service.

## Summary of Defaults

Out of the box, optional cloud and external integrations are **disabled**:

- **Telemetry:** None collected or sent anywhere.
- **Work profile (`work_profile`):** `"work"` by default; switching to `"private"` blocks all outbound cloud traffic.
- **Paid cloud models (`allow_paid`):** `false` by default.
- **Telegram (`telegram.chat_id`):** `null` (disabled) by default.
- **Speech-to-Text (`voice.stt_mode`):** `"local"` by default (Faster-Whisper, local files only).
- **Text-to-Speech (`voice.tts_backend`):** `"piper"` by default (local Piper TTS; `voice.allow_cloud_tts_text: false`).
- **Cloud file, clipboard, and screen sharing:** `allow_cloud_file_content: false`, `allow_cloud_clipboard: false`, `perception.screen_enabled: false`, `perception.allow_cloud_screen: false`.
- **Direct audio & multimodal screen context:** `voice.direct_audio_input: false`, `voice.multimodal_screen_context: false`.
- **Calendar, Mail, Research, MCP, Computer Use, Selection Actions, Dictation:** all `false` or empty by default.

## Data Processing Details

### 1. LLM Providers and Work Profiles

- **Cloud LLM providers (`openrouter`, `anthropic`, `openai_compatible`, `claude_cli`; the `openai_compatible` presets in the UI include OpenAI, Google Gemini, DeepSeek, Z.ai GLM, Groq, xAI, and Mistral):** Contacted only when you configure a model/provider and supply your own API key or credential. API keys are never stored in YAML configuration files; they are read only from environment variables or Windows Credential Manager.
- **Local models:** Local Ollama (`http://127.0.0.1:11434` by default) and local `openai_compatible` servers on `127.0.0.1`, `localhost`, or `::1` run entirely on your machine. Plain HTTP is rejected for non-loopback hosts.
- **Paid usage guard:** `allow_paid` defaults to `false`, blocking models with non-zero token/image pricing unless explicitly enabled. `daily_budget_usd` defaults to `1`.
- **`"private"` work profile:** Setting `work_profile: "private"` enforces strict local-only operation. It blocks all cloud LLM requests (falling back to a configured local `ollama` model or refusing the request if none is available), and blocks cloud screen, clipboard, and file sharing, remote TTS, multimodal screen context, and Telegram notifications/channels.

### 2. Telegram Channel

- Disabled by default (`telegram.chat_id: null`).
- Activated only if you configure `telegram.chat_id` and provide the bot token via the `ARTEMIS_TELEGRAM_BOT_TOKEN` environment variable.
- Inbound Telegram voice, photo, and file acceptance (`accept_voice`, `accept_photo`, `accept_files`), desktop hosting (`run_in_desktop`), and computer-use screenshots (`send_computer_screenshot`) all default to `false`. Content marked private (or produced under `"private"` profile) is never sent to Telegram.

### 3. Voice Pipeline (STT & TTS) and Model Downloads

- **STT (`voice.stt_mode: "local"`):** Uses local `faster-whisper`. Cache lookups check local files only (`local_files_only=True`). Hybrid cloud STT (`voice.stt_mode: "hybrid"`) is opt-in and sends command speech audio only to the user-configured `voice.stt_cloud_endpoint`.
- **TTS (`voice.tts_backend: "piper"`):** Uses local Piper TTS by default. Using Edge TTS (`edge`) or a cloud speech endpoint (`cloud`) requires explicitly setting `voice.allow_cloud_tts_text: true` (default `false`); paid cloud speech models additionally require `voice.tts_cloud_allow_paid: true` (default `false`).
- **Multimodal voice options:** `voice.direct_audio_input` (default `false`) and `voice.multimodal_screen_context` (default `false`) are opt-in. Multimodal screen context is further gated by `perception.screen_enabled`, `perception.allow_cloud_screen`, and is blocked in the `"private"` profile.
- **Model downloads:** Speech/noise models are downloaded only when you explicitly click download in the in-app Model Health Manager. Exception: when you enable live transcript (`voice.live_transcript`) or the keyword-spotting gate (KWS wake model), their pinned-revision model files are downloaded automatically from Hugging Face on first start (no audio is sent).

### 4. Screen, Clipboard, Files, and Local Storage

- `allow_cloud_file_content` (`false`), `allow_cloud_clipboard` (`false`), `perception.screen_enabled` (`false`), `perception.allow_cloud_screen` (`false`), `computer_use.enabled` (`false`), `selection_actions.enabled` (`false`), and `dictation.enabled` (`false`) are all disabled by default.
- Raw microphone audio and screenshots are kept in memory only and are not persisted to disk.
- Local state (`state_dir`, `audit.jsonl`, `memory.sqlite3`) is stored on your device. On Windows, optional SQLite conversation memory encryption at rest is available via Windows DPAPI (`memory.encrypt_at_rest`: the code default is `false`; `config/config.example.yaml` and the packaged install set it to `true`). Setting `persist_memory: false` disables writing conversation memory to disk.

### 5. Calendar, Mail, Research, MCP, Web Search, and Updates

- **Calendar & Mail:** `calendar.enabled: false` and `mail.enabled: false` by default. Even when enabled, `allow_cloud`, `calendar.allow_write`, and `mail.allow_send` default to `false` (writes/sends require explicit on-screen approval).
- **Background Research & Automations:** `research.enabled`, `routines.enabled`, `proactive.enabled`, `skills.enabled`, `file_search.enabled`, and `daily_summary.enabled` are all `false` by default.
- **MCP & Code Agents:** `mcp.servers` and `code_agent.projects` are empty by default, so no external MCP servers or code-agent subprocesses are launched unless configured.
- **Web Search (`web_search`):** Sends search queries to DuckDuckGo (`https://html.duckduckgo.com/html/`) only when the `web_search` tool is explicitly invoked during a turn.
- **Update Check:** Checks the GitHub Releases API (`https://api.github.com/repos/UmutErayAltay/artemis/releases/latest`) with a read-only GET request only when you manually click check for updates in Settings. Downloaded installers are verified against their SHA-256 digest before use.

### 6. Telemetry

- Artemis collects and transmits **zero remote telemetry**.
- `voice.telemetry_report` (default `true`) writes **local-only, content-free** latency and stage timing JSON reports (durations, utterance/generation IDs, outcome labels — no text, transcripts, or audio) under `<state_dir>/voice-telemetry/`. These files never leave your machine.

## Running 100% Locally

To guarantee zero outbound network traffic from Artemis:

1. Switch the work profile to **Private** (`work_profile: "private"` in config or from the desktop profile selector).
2. Configure a local `ollama` model (or a local `127.0.0.1` `openai_compatible` server) as `primary_model`.
3. Keep `voice.stt_mode: "local"` and `voice.tts_backend: "piper"` (with `voice.allow_cloud_tts_text: false`, `voice.direct_audio_input: false`, and `voice.multimodal_screen_context: false`).
4. Leave `telegram.chat_id: null`, `calendar.enabled: false`, `mail.enabled: false`, `research.enabled: false`, and `mcp.servers: []`.
5. Avoid invoking the `web_search` tool or manual update/model-download buttons when offline.
6. Optionally set `voice.telemetry_report: false` to stop writing local latency JSON files, and `persist_memory: false` to disable local conversation persistence.
