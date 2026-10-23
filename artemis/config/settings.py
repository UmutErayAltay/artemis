"""Sırların YAML veya repr içine girmemesi için ayarlarda sır alanı bulunmaz."""

import re
from datetime import time
from decimal import Decimal
from pathlib import Path
from typing import Literal, Self
from urllib.parse import urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from artemis.config.model_paths import DEFAULT_NOISE_MODEL_DIR

GITHUB_OWNER_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})")
ENDPOINT_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,40}")


def _validate_base_url(value: str) -> str:
    parsed = urlparse(value)
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("Sağlayıcı adresinde sır, query veya fragment bulunamaz")
    if parsed.scheme not in {"https", "http"} or not parsed.hostname:
        raise ValueError("Geçerli HTTP(S) sağlayıcı adresi gerekli")
    if parsed.scheme == "http" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise ValueError("Şifresiz HTTP yalnız yerel sunucu için kullanılabilir")
    return value.rstrip("/")


class EndpointConfig(BaseModel):
    """Kullanıcının tanımladığı OpenAI uyumlu uç (ADR 0033); anahtar burada tutulmaz."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    base_url: str
    # False: anahtarsız yerel sunucu; Authorization başlığı hiç gönderilmez.
    api_key: bool = True

    @field_validator("base_url")
    @classmethod
    def validate_base_url(cls, value: str) -> str:
        return _validate_base_url(value)


class ModelConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    provider: Literal["openrouter", "anthropic", "ollama", "openai_compatible", "claude_cli"]
    model: str = Field(min_length=1)
    input_per_million: Decimal = Field(ge=0)
    output_per_million: Decimal = Field(ge=0)
    context_window: int = Field(default=32768, ge=1024)
    endpoint: str | None = None

    @model_validator(mode="after")
    def validate_endpoint_use(self) -> Self:
        if (self.provider == "openai_compatible") != (self.endpoint is not None):
            raise ValueError("endpoint yalnız ve her zaman openai_compatible modelde verilir")
        return self


class VoiceConfig(BaseModel):
    """Önceden kurulmuş yerel ses modellerinin ve ses akışının ayarları."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    wake_word: str = Field(default="artemis", min_length=1, max_length=64)
    input_device: int | str | None = None
    followup_seconds: float = Field(default=8.0, ge=0, le=120)
    sample_rate: Literal[16000] = 16000
    frame_ms: Literal[20, 30] = 20
    vad_aggressiveness: int = Field(default=2, ge=0, le=3)
    end_silence_ms: int = Field(default=600, ge=100, le=3000)
    barge_in_ms: int = Field(default=180, ge=100, le=1000)
    require_wake_during_output: bool = True
    continuous_mode: bool = False
    # Masaüstünde sesi açıp kapatan Windows genel kısayolu ("Ses" düğmesiyle aynı yol);
    # tek elle basılabilsin diye ctrl+space. Boş dize kısayolu kapatır.
    listening_hotkey: str = Field(default="ctrl+space", max_length=40)
    # ADR 0037: DeepFilterNet3 gürültü engelleme (CPU). Düzey bastırma sınırını (dB) belirler:
    # hafif 12, orta 24, güçlü sınırsız; güçlü bastırma Whisper doğruluğunu düşürebilir.
    noise_suppression: bool = False
    noise_suppression_level: Literal["hafif", "orta", "guclu"] = "orta"
    # false: temizlenmiş ses yalnız algılamaya (VAD, bitiş, barge-in, seviye) gider; STT aynı
    # aralığın HAM sesini alır (ölçüm: Whisper temizlenmiş sesle çoğunlukla kötüleşir, ADR 0037).
    noise_suppression_stt: bool = False
    # ADR 0038: WebRTC AEC3 (livekit) ile yalnız yerelde yankı engelleme;
    # hoparlörle kullanımda Artemis'in kendi sesini mikrofondan siler.
    echo_cancellation: bool = False
    echo_delay_ms: int = Field(default=50, ge=0, le=500)
    # Verilmezse pakete dahil durumlu adım modeli (artemis/channels/voice/models/dfn3).
    noise_model_dir: Path | None = None
    pre_roll_ms: int = Field(default=300, ge=0, le=1000)
    max_utterance_seconds: float = Field(default=15.0, gt=0, le=120)
    queue_frames: int = Field(default=32, ge=2, le=256)
    stt_model: Path | None = None
    stt_device: Literal["cuda", "cpu"] = "cuda"
    stt_compute_type: str = "float16"
    cuda_library_dirs: list[Path] = Field(default_factory=list)
    stt_language: Literal["tr", "tr_en", "auto"] = "tr"
    stt_beam_size: int = Field(default=5, ge=1, le=10)
    stt_partial_beam_size: int = Field(default=1, ge=1, le=10)
    stt_partial_enabled: bool = True
    stt_vad_filter: bool = True
    stt_min_logprob: float = -1.0
    stt_max_no_speech: float = Field(default=0.6, ge=0, le=1)
    stt_hotwords: tuple[str, ...] = Field(default=(), max_length=100)
    tts_backend: Literal["piper", "edge", "ema"] = "piper"
    # ema: yerel Türkçe EMA Lightning (`pip install .[ema]`, torch CPU)
    tts_local_fallback: bool = True
    piper_model: Path | None = None
    piper_config: Path | None = None
    piper_voice: str = ""
    edge_voice: str = ""
    allow_cloud_tts_text: bool = False
    ffmpeg: str = "ffmpeg"

    @field_validator("stt_hotwords")
    @classmethod
    def validate_stt_hotwords(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        total_chars = 0
        for word in values:
            if not word.strip() or len(word) > 64:
                raise ValueError("STT hotword boş olamaz ve 64 karakteri aşamaz")
            if any(ord(char) < 32 or ord(char) == 127 for char in word):
                raise ValueError("STT hotword kontrol karakteri içeremez")
            total_chars += len(word)
        if total_chars > 2048:
            raise ValueError("STT hotword toplam uzunluğu 2048 karakteri aşamaz")
        return values

    @field_validator("wake_word")
    @classmethod
    def validate_wake_word(cls, value: str) -> str:
        if not value.strip() or any(ord(char) < 32 or ord(char) == 127 for char in value):
            raise ValueError("Wake word boşluk veya kontrol karakteri içeremez")
        return value.strip()


class MemoryConfig(BaseModel):
    """Obsidian vault hafızası (ADR 0015); `vault_path` verilmedikçe tamamen kapalıdır."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    vault_path: Path | None = None
    # "/" ile biten girdi o klasör bölümünü yolun başında VEYA içinde eşler (companion
    # klasörünün adı "🔮 850-Companion/" gibi önek taşıyabilir); diğerleri tam yoldur.
    exclude: tuple[str, ...] = (
        "daily/",
        "receipts/",
        "knowledge/index.md",
        "knowledge/log.md",
        "850-Companion/",
    )
    max_results: int = Field(default=5, ge=1, le=20)
    max_note_chars: int = Field(default=20000, ge=1000, le=200000)
    embedding_model: str | None = None
    embedding_cache_dir: Path | None = None


class TelegramConfig(BaseModel):
    """Telegram kanalı ve bildirimi (ADR 0016); `chat_id` verilmedikçe tamamen kapalıdır.

    Bilerek token alanı YOKTUR: `extra="forbid"` sayesinde YAML'a yazılan bir `token:`
    doğrulamada reddedilir; sır yalnız `token_env` adlı ortam değişkeninden okunur.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    chat_id: int | None = None
    token_env: str = "ARTEMIS_TELEGRAM_BOT_TOKEN"
    api_base: str = "https://api.telegram.org"
    request_timeout_seconds: float = Field(default=10.0, gt=0, le=60)
    poll_timeout_seconds: int = Field(default=25, ge=0, le=50)
    approval_timeout_seconds: float = Field(default=120.0, gt=0, le=900)
    max_message_age_seconds: int = Field(default=120, ge=1, le=3600)

    @field_validator("token_env")
    @classmethod
    def validate_token_env(cls, value: str) -> str:
        # `_TOKEN` son eki zorunludur: denetim temizleyicisi adında "token" geçen
        # ortam değişkenlerinin DEĞERİNİ her kayıttan siler. Başka bir ad seçilirse
        # token denetim dosyasına düz metin sızabilirdi.
        if not re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", value) or not value.endswith("_TOKEN"):
            raise ValueError(
                "token_env büyük harfli bir ortam değişkeni adı olmalı ve _TOKEN ile bitmeli"
            )
        return value

    @field_validator("api_base")
    @classmethod
    def validate_api_base(cls, value: str) -> str:
        # Sağlayıcı adresleriyle aynı kurallar: sır/query yok, HTTP yalnız yerel sunucu.
        return Settings.validate_endpoint(value)


class CodeAgentConfig(BaseModel):
    """Kod ajanı (ADR 0023): izinli proje takma adları; liste boşsa özellik kapalıdır."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    projects: dict[str, Path] = Field(default_factory=dict)
    command: str = Field(default="claude", min_length=1)
    max_minutes: float = Field(default=20.0, ge=1, le=120)
    model: str | None = None  # Eski yönetici modeli ayarı; manager_model kazanır.
    manager_model: str | None = Field(default=None, min_length=1, max_length=128)
    coder_model: str | None = Field(default=None, min_length=1, max_length=128)
    tester_model: str | None = Field(default=None, min_length=1, max_length=128)
    # Kendi GitHub repoları (ADR 0023): sahip + klasör verilirse klasördeki repolar
    # çalışma anında projeler listesine eklenir. Kimlik doğrulamayı `gh` yönetir.
    github_owner: str | None = None
    repos_dir: Path | None = None
    auto_sync: bool = True

    @field_validator("github_owner")
    @classmethod
    def validate_github_owner(cls, value: str | None) -> str | None:
        if value is not None and not GITHUB_OWNER_RE.fullmatch(value):
            raise ValueError("GitHub kullanıcı adı harf, rakam veya - içermeli (en çok 39)")
        return value

    @field_validator("projects")
    @classmethod
    def validate_projects(cls, values: dict[str, Path]) -> dict[str, Path]:
        if any(not re.fullmatch(r"[a-z0-9_-]{1,32}", alias) for alias in values):
            raise ValueError("Proje takma adı 1-32 küçük harf, rakam, _ veya - olmalı")
        return values


class BriefingConfig(BaseModel):
    """Config'te tanımlı brifing (ADR 0027): istemi model kuramaz ve değiştiremez."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(pattern=r"^[a-z0-9_-]{1,32}$")
    at: time
    # 0 = Pazartesi ... 6 = Pazar (datetime.weekday()).
    days: tuple[int, ...] = (0, 1, 2, 3, 4, 5, 6)
    prompt: str = Field(min_length=1, max_length=2000)

    @field_validator("days")
    @classmethod
    def validate_days(cls, values: tuple[int, ...]) -> tuple[int, ...]:
        if not values or any(day < 0 or day > 6 for day in values):
            raise ValueError("days 0 (Pazartesi) ile 6 (Pazar) arasında en az bir gün olmalı")
        return tuple(sorted(set(values)))

    @field_validator("at")
    @classmethod
    def validate_at(cls, value: time) -> time:
        # Saat dilimi ProactiveConfig.timezone'dan gelir; saniye hassasiyeti gerekmez.
        if value.tzinfo is not None:
            raise ValueError("Brifing saati saat dilimi taşımaz; proactive.timezone kullanılır")
        # Not: YAML'da tırnaksız 9:30 altmışlık sayıya dönüşür ve burada reddedilir;
        # örnek config saati tırnakla ("08:30") yazar.
        return value.replace(second=0, microsecond=0)


class ProactiveConfig(BaseModel):
    """Hatırlatma ve brifing (ADR 0027); `enabled` verilmedikçe tamamen kapalıdır."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    enabled: bool = False
    timezone: str = "Europe/Istanbul"
    briefings: tuple[BriefingConfig, ...] = ()
    reminder_grace_hours: float = Field(default=12.0, ge=0, le=168)
    briefing_grace_hours: float = Field(default=2.0, ge=0, le=24)
    max_events_per_day: int = Field(default=20, ge=1, le=500)
    poll_seconds: float = Field(default=30.0, ge=1, le=300)
    # Kullanıcının kendi Telegram sohbetine de gönder (telegram.chat_id gerekir).
    telegram: bool = True

    @field_validator("timezone")
    @classmethod
    def validate_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as error:
            message = "timezone geçerli bir IANA adı olmalı (ör. Europe/Istanbul)"
            raise ValueError(message) from error
        return value

    @field_validator("briefings")
    @classmethod
    def validate_briefings(
        cls, values: tuple[BriefingConfig, ...]
    ) -> tuple[BriefingConfig, ...]:
        names = [briefing.name for briefing in values]
        if len(names) != len(set(names)):
            raise ValueError("Brifing adları benzersiz olmalı")
        return values


ENV_NAME_RE = re.compile(r"[A-Z][A-Z0-9_]{0,63}")


class MCPServerConfig(BaseModel):
    """Tek bir stdio MCP sunucusu (ADR 0028). Sır alanı yoktur; yalnız ortam adları verilir."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(pattern=r"^[a-z0-9_]{1,24}$")
    command: str = Field(min_length=1)
    args: tuple[str, ...] = ()
    cwd: Path | None = None
    # Zorunlu ve varsayılansız: "file" sonuçları buluta/Telegram'a gitmez (CLAUDE.md 12).
    content: Literal["file", "text"]
    timeout_seconds: float = Field(default=15.0, ge=1, le=300)
    # Riski yalnız kullanıcı düşürür; sunucunun readOnlyHint'i dikkate alınmaz.
    read_only_tools: tuple[str, ...] = ()
    confirm_tools: tuple[str, ...] = ()
    # Değer değil AD: değer çalışma anında ortamdan okunur, yalnız bu sunucuya verilir.
    env_passthrough: tuple[str, ...] = ()

    @field_validator("env_passthrough")
    @classmethod
    def validate_env_names(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if any(not ENV_NAME_RE.fullmatch(value) for value in values):
            raise ValueError("env_passthrough büyük harfli ortam değişkeni adları içermeli")
        return values

    @model_validator(mode="after")
    def validate_tool_lists(self) -> Self:
        if set(self.read_only_tools) & set(self.confirm_tools):
            raise ValueError("Bir araç hem read_only_tools hem confirm_tools içinde olamaz")
        return self


class MCPConfig(BaseModel):
    """MCP istemcisi (ADR 0028); `servers` boşken hiçbir süreç başlatılmaz."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    servers: tuple[MCPServerConfig, ...] = ()

    @field_validator("servers")
    @classmethod
    def validate_servers(
        cls, values: tuple[MCPServerConfig, ...]
    ) -> tuple[MCPServerConfig, ...]:
        names = [server.name for server in values]
        if len(names) != len(set(names)):
            raise ValueError("MCP sunucu adları benzersiz olmalı")
        return values


class SystemControlConfig(BaseModel):
    """Pencere ve sistem kontrolü (ADR 0031)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    # Kapatma/yeniden başlatma bu kadar sonra olur; arada `cancel_shutdown` ile iptal edilir.
    shutdown_delay_seconds: int = Field(default=60, ge=30, le=600)
    timeout_seconds: float = Field(default=15.0, ge=1, le=120)


class PerceptionConfig(BaseModel):
    """Ekran algısı (ADR 0029); varsayılan kapalıdır, açıkken her okuma onay ister."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    screen_enabled: bool = False
    # Ekran metni varsayılan olarak dosya/pano gibi gizlidir: bulut modele ve Telegram'a gitmez.
    allow_cloud_screen: bool = False
    timeout_seconds: float = Field(default=20.0, ge=1, le=120)


class ComputerUseConfig(BaseModel):
    """Bilgisayar kullanımı (ADR 0039); varsayılan kapalı, görsel model açıkça seçilir."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    enabled: bool = False
    # `Settings.models` anahtarı; ekran görüntüsünü kabul eden bir model olmalı.
    # Boşsa Artemis'in o anki ana modeli (Settings.primary_model) kullanılır.
    model: str = ""
    max_steps: int = Field(default=25, ge=1, le=100)
    max_minutes: float = Field(default=5.0, ge=0.5, le=30)
    screenshot_width: int = Field(default=1280, ge=640, le=2560)
    step_delay_seconds: float = Field(default=0.6, ge=0, le=3)
    # ADR 0039 ek karar: "auto" oturum içi hassas adımları ve başlangıcı sormaz
    # (`computer_use`, `window_control`, `screen_read` `auto_mode_ok` ile lekeli turda da
    # onaylanmaz,
    # atlama `approval_skipped` olarak denetime yazılır); kodla zorlanan engeller,
    # sınırlar ve Esc iki kipte de aynıdır.
    mode: Literal["accept", "auto"] = "accept"
    # Kotaya takılan görsel model için sırayla denenecek diğer görüntü kabul eden modeller.
    fallback_models: tuple[str, ...] = ()


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    workspace: Path = Path("desktop")
    state_dir: Path = Path(".artemis")
    audit_path: Path = Path(".artemis/audit.jsonl")
    models: dict[str, ModelConfig] = Field(default_factory=dict)
    endpoints: dict[str, EndpointConfig] = Field(default_factory=dict)
    primary_model: str = ""
    fallback_model: str | None = None
    max_steps: int = Field(default=8, ge=1, le=100)
    max_seconds: float = Field(default=60.0, gt=0, le=3600)
    provider_timeout_seconds: float = Field(default=15.0, gt=0, le=3600)
    max_output_tokens: int = Field(default=1024, ge=1)
    # Konuşma bağlamı (ADR 0012): 0 tur bağlamı kapatır; bütçe model penceresinin
    # yarısıyla runtime'da ayrıca sınırlanır.
    context_turns: int = Field(default=6, ge=0, le=50)
    context_token_budget: int = Field(default=6000, ge=256, le=200_000)
    # Kalıcı hafıza (ADR 0025): False iken diske hiçbir şey yazılmaz (test/demo).
    persist_memory: bool = True
    daily_budget_usd: Decimal = Field(default=Decimal("1"), ge=0)
    allow_paid: bool = False
    allow_cloud_file_content: bool = False
    allow_cloud_clipboard: bool = False
    fallback_seconds: float = Field(default=60.0, ge=0)
    applications: dict[str, str] = Field(default_factory=dict)
    allow_all_applications: bool = False
    # Temiz turda kapatma onayını atlayan, kullanıcının seçtiği takma adlar.
    close_without_confirmation: list[str] = Field(default_factory=list)
    voice: VoiceConfig = Field(default_factory=VoiceConfig)
    memory: MemoryConfig = Field(default_factory=MemoryConfig)
    telegram: TelegramConfig = Field(default_factory=TelegramConfig)
    code_agent: CodeAgentConfig = Field(default_factory=CodeAgentConfig)
    proactive: ProactiveConfig = Field(default_factory=ProactiveConfig)
    mcp: MCPConfig = Field(default_factory=MCPConfig)
    perception: PerceptionConfig = Field(default_factory=PerceptionConfig)
    system_control: SystemControlConfig = Field(default_factory=SystemControlConfig)
    computer_use: ComputerUseConfig = Field(default_factory=ComputerUseConfig)
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    anthropic_base_url: str = "https://api.anthropic.com/v1"
    ollama_base_url: str = "http://127.0.0.1:11434"

    @field_validator("openrouter_base_url", "anthropic_base_url", "ollama_base_url")
    @classmethod
    def validate_endpoint(cls, value: str) -> str:
        return _validate_base_url(value)

    @field_validator("endpoints")
    @classmethod
    def validate_endpoint_ids(cls, values: dict[str, EndpointConfig]) -> dict[str, EndpointConfig]:
        if any(not ENDPOINT_ID_RE.fullmatch(name) for name in values):
            raise ValueError("Uç kimliği yalnız [A-Za-z0-9_-] ve 1-40 karakter olabilir")
        return values

    @model_validator(mode="after")
    def validate_models(self) -> Self:
        if any(
            m.endpoint is not None and m.endpoint not in self.endpoints
            for m in self.models.values()
        ):
            raise ValueError("Modelin endpoint değeri endpoints içinde tanımlanmalı")
        if any(name not in self.applications for name in self.close_without_confirmation):
            raise ValueError("Onaysız kapatılacak uygulama izin listesinde tanımlanmalı")
        if self.primary_model and self.primary_model not in self.models:
            raise ValueError("Birincil model models içinde tanımlanmalı")
        if self.fallback_model:
            fallback = self.models.get(self.fallback_model)
            if fallback is None or fallback.provider != "ollama":
                raise ValueError("Yedek model tanımlı bir Ollama modeli olmalı")
        # Boş model "ana beyni kullan" demektir; o zaman ana modelin kendisi doğrulanır.
        vision_model = self.computer_use.model or self.primary_model
        if self.computer_use.enabled and vision_model not in self.models:
            raise ValueError("computer_use.model models içinde tanımlanmalı")
        for name in self.computer_use.fallback_models:
            if name not in self.models:
                raise ValueError("computer_use.fallback_models models içinde tanımlanmalı")
        return self


def load_settings(path: Path) -> Settings:
    """Göreli yolları config dosyasına göre çözerek çalışma dizini sürprizini önler."""
    with path.open(encoding="utf-8") as stream:
        raw = yaml.safe_load(stream)
    if not isinstance(raw, dict):
        raise ValueError("Config bir YAML eşlemesi olmalı")
    settings = Settings.model_validate(raw)
    base = path.resolve().parent
    updates: dict[str, object] = {}
    for field in ("workspace", "state_dir", "audit_path"):
        value = getattr(settings, field)
        updates[field] = value if value.is_absolute() else (base / value).resolve()
    voice_updates: dict[str, Path] = {}
    for field in ("stt_model", "piper_model", "piper_config", "noise_model_dir"):
        value = getattr(settings.voice, field)
        if value is not None:
            path_value = value
            voice_updates[field] = (
                path_value if path_value.is_absolute() else (base / path_value).resolve()
            )
    if settings.voice.noise_model_dir is None:
        # Pakete dahil model dizini; cwd ve config konumundan bağımsız.
        voice_updates["noise_model_dir"] = DEFAULT_NOISE_MODEL_DIR
    if voice_updates:
        updates["voice"] = settings.voice.model_copy(update=voice_updates)
    memory_updates: dict[str, Path] = {}
    for field in ("vault_path", "embedding_cache_dir"):
        value = getattr(settings.memory, field)
        if value is not None:
            memory_updates[field] = value if value.is_absolute() else (base / value).resolve()
    if memory_updates:
        updates["memory"] = settings.memory.model_copy(update=memory_updates)
    agent = settings.code_agent
    agent_updates: dict[str, object] = {}
    if agent.repos_dir is not None:
        repos = agent.repos_dir
        agent_updates["repos_dir"] = repos if repos.is_absolute() else (base / repos).resolve()
    if agent.projects:
        agent_updates["projects"] = {
            alias: value if value.is_absolute() else (base / value).resolve()
            for alias, value in agent.projects.items()
        }
    if agent_updates:
        updates["code_agent"] = agent.model_copy(update=agent_updates)
    if any(server.cwd is not None for server in settings.mcp.servers):
        servers = tuple(
            server
            if server.cwd is None or server.cwd.is_absolute()
            else server.model_copy(update={"cwd": (base / server.cwd).resolve()})
            for server in settings.mcp.servers
        )
        updates["mcp"] = settings.mcp.model_copy(update={"servers": servers})
    return settings.model_copy(update=updates)
