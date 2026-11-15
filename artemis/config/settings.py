"""Sırların YAML veya repr içine girmemesi için ayarlarda sır alanı bulunmaz."""

import os
import re
from datetime import time
from decimal import Decimal
from pathlib import Path, PureWindowsPath
from typing import Any, Literal, Self
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
    # "image": resim üretim modeli (pet üretici, ADR 0048 eki); sohbet modeli olarak kullanılamaz.
    kind: Literal["chat", "image"] = "chat"
    # Resim çağrısı başına bütçeden ayrılan sabit üst tutar (USD); token fiyatı resimde anlamsız.
    image_cost_usd: Decimal = Field(default=Decimal(0), ge=0)

    @model_validator(mode="after")
    def validate_endpoint_use(self) -> Self:
        if (self.provider == "openai_compatible") != (self.endpoint is not None):
            raise ValueError("endpoint yalnız ve her zaman openai_compatible modelde verilir")
        if self.kind == "image" and self.provider != "openai_compatible":
            raise ValueError("Resim modeli openai_compatible sağlayıcı ve uç gerektirir")
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
    # Konuşma algılayıcı (ADR 0067): `auto` paketle gelen Silero VAD'ı (sinir ağı, gürültüde daha az
    # yanlış tetik) kullanır, kurulamazsa WebRTC'ye döner; `webrtc` eski davranıştır.
    vad_backend: Literal["auto", "webrtc", "silero"] = "auto"
    # Silero konuşma olasılığı eşiği; yalnız `vad_aggressiveness` WebRTC'ye uygulanır.
    vad_threshold: float = Field(default=0.5, gt=0, lt=1)
    end_silence_ms: int = Field(default=600, ge=100, le=3000)
    # Anlamsal konuşma sonu (ADR 0068): sessizlik `end_silence_ms`e varınca Smart Turn modeli
    # cümlenin bitip bitmediğine bakar; bitmemişse en çok `turn_max_silence_ms`e kadar beklenir.
    # Yalnız sabır ekler, turu hiç erken bitirmez; kurulamazsa eski davranışla sürer.
    turn_detection: bool = True
    turn_max_silence_ms: int = Field(default=1600, ge=200, le=5000)
    # Smart Turn "tur tamam" olasılığı eşiği.
    turn_threshold: float = Field(default=0.5, gt=0, lt=1)
    # Eski config uyumluluğu; söz kesme artık nihai STT doğrulamasını bekler (ADR 0040).
    barge_in_ms: int = Field(default=180, ge=100, le=1000)
    require_wake_during_output: bool = True
    # ADR 0042: konuşurken uyandırmasız doğrulanmış söz yalnız susturur, komut çalıştırmaz.
    barge_in_without_wake: bool = True
    # ADR 0046 eki: False iken söz kesme ve yanıt sonu takip penceresi açmaz (Artemis demeden
    # dinleme yok); tek başına "Artemis" karşılığı takibi her zaman açar. True: eski davranış.
    followup_listening: bool = False
    # Uyandırma karşılığı; çok kısa metin EMA TTS'te bozuk çıkıyordu (ADR 0046 eki).
    wake_ack_text: str = Field(default="Seni dinliyorum.", min_length=1, max_length=40)
    # ADR 0043: model yazarken cümle cümle seslendir; sağlayıcı akmıyorsa tam yanıt beklenir.
    streaming: bool = True
    continuous_mode: bool = False
    # Ses kapanınca içeriksiz gecikme raporu state_dir/voice-telemetry/ altına yazılır (ADR 0041).
    # Varsayılan açık: rapor yalnız süre/sayı taşır (metin, ses yok); gerçek kullanımda gecikme
    # ölçülmeden "hızlı" denemez.
    telemetry_report: bool = True
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
    # Oyun modu: tam ekran uygulama öndeyken büyük STT modeli GPU'dan bırakılır, hafif model
    # devralır; boşsa yerelde kurulu faster-whisper `small` aranır.
    game_mode: bool = False
    game_mode_stt_model: Path | None = None
    stt_device: Literal["cuda", "cpu"] = "cuda"
    stt_compute_type: str = "float16"
    cuda_library_dirs: list[Path] = Field(default_factory=list)
    stt_language: Literal["tr", "tr_en", "auto"] = "tr"
    stt_beam_size: int = Field(default=5, ge=1, le=10)
    stt_partial_beam_size: int = Field(default=1, ge=1, le=10)
    stt_partial_enabled: bool = True
    # ADR 0071: bitiş sessizliği sürerken nihai STT sessizliğin `stt_speculative_after_ms`inde
    # önceden başlar; bölüt aynı sesle kapanırsa sonuç kullanılır, konuşma sürerse atılır.
    stt_speculative: bool = True
    stt_speculative_after_ms: int = Field(default=200, ge=60, le=2000)
    # Kapalı: Whisper'ın VAD süzgeci bant sınırlı seste baştaki uyandırma sözcüğünü kesiyor
    # (ADR 0045: telefon bandında kaçırma %40 -> %0). Canlı döngüde STT'ye zaten webrtc VAD'in
    # kestiği parça gider, boşta ek maliyet yoktur.
    stt_vad_filter: bool = False
    stt_min_logprob: float = -1.0
    stt_max_no_speech: float = Field(default=0.6, ge=0, le=1)
    stt_hotwords: tuple[str, ...] = Field(default=(), max_length=100)
    # Öğretilmiş uygulama takma adlarının hedef adları STT ipucuna eklenir (ADR 0044).
    # Varsayılan kapalı: geniş hotword listesi uyandırmada yan etki verdi (CPA_LOCAL_TEST).
    stt_alias_hotwords: bool = False
    # ADR 0046: hybrid'de uyandırma/yankı kararları yerel metinle verilir, yalnız komut olacak
    # sözün sesi `stt_cloud_endpoint` ucuna (üst düzey `endpoints` kimliği) gider.
    stt_mode: Literal["local", "hybrid"] = "local"
    stt_cloud_endpoint: str = ""
    stt_cloud_model: str = ""
    # ADR 0050: xAI'nin STT REST ucu OpenAI uyumlu değildir (/stt, model alanı yok).
    stt_cloud_protocol: Literal["openai", "xai"] = "openai"
    # ADR 0047: konuşurken akan YALNIZ GÖSTERİM metni (yerel, `pip install .[stream]`).
    # Model kimliği koda gömülmez; açıkken depo ve sabit revision config'ten gelir.
    live_transcript: bool = False
    live_transcript_model: str = ""
    live_transcript_revision: str = ""
    # ADR 0069: "kws" iken cihaz içi anahtar sözcük algısı (live_transcript_model/revision'ı
    # paylaşır) uyandırma sözü olmayan bölütleri STT'den ÖNCE eler; kararı yine transkript verir.
    wake_backend: Literal["transcript", "kws"] = "transcript"
    wake_kws_threshold: float = Field(default=0.2, gt=0, lt=1)
    tts_backend: Literal["piper", "edge", "ema"] = "piper"
    # ema: yerel Türkçe EMA Lightning (`pip install .[ema]`, torch CPU)
    tts_local_fallback: bool = True
    piper_model: Path | None = None
    piper_config: Path | None = None
    piper_voice: str = ""
    edge_voice: str = ""
    allow_cloud_tts_text: bool = False
    ffmpeg: str = "ffmpeg"

    @model_validator(mode="before")
    @classmethod
    def drop_legacy_cloud_consent(cls, data: Any) -> Any:
        # Eski config'lerdeki allow_cloud_stt_audio kaldırıldı (hybrid seçmek açık izindir);
        # yalnız bu anahtar yutulur, diğer bilinmeyen alanlar yine reddedilir.
        if isinstance(data, dict) and "allow_cloud_stt_audio" in data:
            data = {k: v for k, v in data.items() if k != "allow_cloud_stt_audio"}
        return data

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
    # Kullanıcının açıkça seçtiği vault'a göreli dosyalar (örn. Threads.md); yalnız
    # memory_open_work aracından, safe_join ile okunur. exclude'u aşar (açık tercih).
    companion_vault_files: tuple[str, ...] = ()


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
    # İki yönlü kanalı masaüstü içinde çalıştırır (ADR 0052); varsayılan kapalı.
    run_in_desktop: bool = False
    # Telegram'dan çalışan `computer_use` bitince son ekran karesini sohbete yollar (ADR 0053).
    # Kare buluta (Telegram) gider: gizli tur/geçmişte ve `perception.allow_cloud_screen`
    # kapalıyken hiç gönderilmez.
    send_computer_screenshot: bool = False
    # Telegram'dan ses, fotoğraf ve dosya girdisi (ADR 0064); üçü ayrı ve varsayılan kapalıdır.
    accept_voice: bool = False
    accept_photo: bool = False
    accept_files: bool = False
    # Kaydedilecek belgelerin SON uzantısı bu listede olmalıdır (x.pdf.exe -> exe, reddedilir).
    file_extensions: tuple[str, ...] = (
        "pdf", "txt", "md", "docx", "xlsx", "csv", "png", "jpg", "zip",
    )

    @field_validator("file_extensions")
    @classmethod
    def validate_file_extensions(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        cleaned = tuple(item.strip().lstrip(".").lower() for item in value)
        if not all(re.fullmatch(r"[a-z0-9]{1,10}", item) for item in cleaned):
            raise ValueError("file_extensions yalnız harf/rakamdan oluşan uzantılar içerebilir")
        return cleaned

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


ALIAS_RE = re.compile(r"[a-z0-9_-]{1,32}")
ENV_NAME_RE = re.compile(r"[A-Z][A-Z0-9_]{0,63}")
# Başlangıç klasörü takma adları: "proje" hedef reponun kendisidir (varsayılan), "ikinci-beyin"
# memory.vault_path verilince örtük vardır (ADR 0049).
OWN_REPO_ALIAS = "proje"
VAULT_ALIAS = "ikinci-beyin"
BUILTIN_AGENT = "claude"
# Yerleşik ajanlar (ADR 0049 eki): claude yeniden tanımlanamaz; codex kullanıcı profili
# tanımlamadıkça `AgentProfile(kind="codex")` sayılır (komut `locate_codex` ile bulunur).
BUILTIN_AGENTS = (BUILTIN_AGENT, "codex")
RunMode = Literal["background", "visible"]


class ConfigChoiceError(ValueError):
    """Bilinmeyen ajan/klasör takma adı; model yalnız yapılandırılmış adları seçebilir."""


class AgentProfile(BaseModel):
    """Kullanıcının tanımladığı kod ajanı profili (ADR 0049); sır değeri burada tutulmaz."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["claude", "codex", "generic"]
    command: str | None = Field(default=None, min_length=1)
    # Yalnız generic: {prompt_file} {prompt} {workdir} {repo} yer tutucuları.
    args: tuple[str, ...] = ()
    prompt_via: Literal["stdin", "file", "arg"] | None = None
    # Çocuğa ek geçirilecek ortam değişkeni ADLARI; değerler asla yazılmaz/loglanmaz.
    env: tuple[str, ...] = ()
    # Yalnız generic: görünür pencere kipinin argümanları (args ile aynı yer tutucular; `{prompt}`
    # kısa yönergedir, tam istem `{prompt_file}` dosyasındadır). Boşsa görünür kip desteklenmez.
    visible_args: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_profile(self) -> Self:
        for name in self.env:
            if not ENV_NAME_RE.fullmatch(name):
                raise ValueError(f"env adı büyük harfli ortam değişkeni adı olmalı: {name!r}")
        if self.kind == "generic":
            if self.command is None:
                raise ValueError("generic ajan için command gerekli")
            if self.prompt_via == "file" and not any("{prompt_file}" in a for a in self.args):
                raise ValueError("prompt_via=file için args içinde {prompt_file} gerekli")
            if self.prompt_via == "arg" and not any("{prompt}" in a for a in self.args):
                raise ValueError("prompt_via=arg için args içinde {prompt} gerekli")
            if self.visible_args and not any(
                "{prompt_file}" in a or "{prompt}" in a for a in self.visible_args
            ):
                raise ValueError("visible_args içinde {prompt_file} veya {prompt} gerekli")
        elif self.args or self.prompt_via or self.visible_args:
            raise ValueError("args, prompt_via ve visible_args yalnız generic ajanda kullanılır")
        return self


class ProjectEntry(BaseModel):
    """Proje girdisi: eski biçim yalnız yol, yeni biçim ajan/klasör varsayılanı da taşır."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    path: Path
    agent: str | None = None
    workdir: str | None = None
    mode: RunMode | None = None
    # Bağımsız kontroller (test/build/lint): her biri argv listesidir, kabuk yoktur. Ajan
    # bittikten sonra Artemis çalıştırır; ajanın "bitti" beyanı tek başına doğrulama sayılmaz.
    # Yalnız YAML'dan gelir; model/araç bu listeyi kuramaz ve değiştiremez.
    checks: tuple[tuple[str, ...], ...] = ()

    @field_validator("checks")
    @classmethod
    def validate_checks(cls, value: tuple[tuple[str, ...], ...]) -> tuple[tuple[str, ...], ...]:
        for argv in value:
            if not argv or any(not part.strip() for part in argv):
                raise ValueError("checks girdisi boş olmayan metinlerden oluşan argv olmalı")
        return value


class CodeAgentConfig(BaseModel):
    """Kod ajanı (ADR 0023, 0049): izinli proje takma adları; liste boşsa özellik kapalıdır."""

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    # YAML anahtarı `projects`dir; eski `alias: yol` ve yeni `alias: {path, agent, workdir}`
    # biçimleri kabul edilir. `.projects` özelliği eski alias->Path görünümünü korur.
    project_entries: dict[str, ProjectEntry] = Field(default_factory=dict, alias="projects")
    agents: dict[str, AgentProfile] = Field(default_factory=dict)
    default_agent: str = BUILTIN_AGENT
    default_mode: RunMode = "background"
    # Proje kendi seçmediyse ajanın başlangıç klasörü (takma ad). "ikinci-beyin": ajan vault'ta
    # başlar, kuralları/CLAUDE.md'yi yükler; değişiklik yine projenin deposunda yapılır (ADR 0049).
    default_workdir: str = Field(default=OWN_REPO_ALIAS, pattern=r"^[a-z0-9_-]{1,32}$")
    workdirs: dict[str, Path] = Field(default_factory=dict)
    command: str = Field(default="claude", min_length=1)
    max_minutes: float = Field(default=20.0, ge=1, le=120)
    # Her bağımsız kontrol komutu için üst süre (sn); aşılırsa kontrol başarısız sayılır.
    check_timeout_seconds: float = Field(default=600.0, ge=1, le=3600)
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

    @field_validator("project_entries", mode="before")
    @classmethod
    def normalise_projects(cls, values: Any) -> Any:
        if isinstance(values, dict):
            return {
                alias: {"path": value} if isinstance(value, str | Path) else value
                for alias, value in values.items()
            }
        return values

    @model_validator(mode="after")
    def validate_references(self) -> Self:
        if any(not ALIAS_RE.fullmatch(alias) for alias in self.project_entries):
            raise ValueError("Proje takma adı 1-32 küçük harf, rakam, _ veya - olmalı")
        if any(not ALIAS_RE.fullmatch(name) for name in self.agents):
            raise ValueError("Ajan adı 1-32 küçük harf, rakam, _ veya - olmalı")
        if BUILTIN_AGENT in self.agents:
            raise ValueError("'claude' yerleşik ajandır; yeniden tanımlanamaz")
        if any(not ALIAS_RE.fullmatch(alias) for alias in self.workdirs):
            raise ValueError("Klasör takma adı 1-32 küçük harf, rakam, _ veya - olmalı")
        if OWN_REPO_ALIAS in self.workdirs:
            raise ValueError(f"'{OWN_REPO_ALIAS}' ayrılmış takma addır (projenin kendi reposu)")
        agent_names = set(self.agent_names())
        if self.default_agent not in agent_names:
            raise ValueError(f"default_agent tanımsız ajan: {self.default_agent!r}")
        # Örtük ikinci-beyin burada doğrulanamaz (vault_path başka bölümde); çözümleme
        # anında hata verir.
        folders = {OWN_REPO_ALIAS, VAULT_ALIAS, *self.workdirs}
        if self.default_workdir not in folders:
            raise ValueError(f"default_workdir tanımsız klasör: {self.default_workdir!r}")
        for alias, entry in self.project_entries.items():
            if entry.agent is not None and entry.agent not in agent_names:
                raise ValueError(f"Proje '{alias}' tanımsız ajan kullanıyor: {entry.agent!r}")
            if entry.workdir is not None and entry.workdir not in folders:
                raise ValueError(f"Proje '{alias}' tanımsız klasör kullanıyor: {entry.workdir!r}")
        return self

    @property
    def projects(self) -> dict[str, Path]:
        """Eski görünüm: alias -> yol (arayüz ve eski çağıranlar bunu okur)."""
        return {alias: entry.path for alias, entry in self.project_entries.items()}

    def project_path(self, alias: str) -> Path | None:
        entry = self.project_entries.get(alias)
        return entry.path if entry else None

    def project_agent(self, alias: str) -> str:
        entry = self.project_entries.get(alias)
        return entry.agent if entry and entry.agent else self.default_agent

    def project_workdir(self, alias: str) -> str:
        entry = self.project_entries.get(alias)
        return entry.workdir if entry and entry.workdir else self.default_workdir

    def project_mode(self, alias: str) -> RunMode:
        entry = self.project_entries.get(alias)
        return entry.mode if entry and entry.mode else self.default_mode

    def agent_names(self) -> tuple[str, ...]:
        """Seçilebilir ajan adları: yerleşikler (claude, codex) sonra kullanıcı ajanları."""
        custom = sorted(name for name in self.agents if name not in BUILTIN_AGENTS)
        return (*BUILTIN_AGENTS, *custom)

    def resolve_agent(self, name: str | None) -> tuple[str, AgentProfile]:
        """Yerleşik claude her zaman vardır (komutu eski `command` alanından gelir). codex'i
        kullanıcı tanımladıysa onun profili (örn. açık mutlak yol), yoksa varsayılan profil."""
        name = name or self.default_agent
        if name == BUILTIN_AGENT:
            return name, AgentProfile(kind="claude", command=self.command)
        profile = self.agents.get(name)
        if profile is None and name == "codex":
            profile = AgentProfile(kind="codex")
        if profile is None:
            known = ", ".join(self.agent_names())
            raise ConfigChoiceError(f"Bilinmeyen ajan '{name}'. Tanımlı ajanlar: {known}.")
        return name, profile

    def workdir_choices(self, vault_path: Path | None) -> dict[str, Path | None]:
        """Seçilebilir klasörler; None = projenin kendi reposu (çözümleme repo'ya bağlı)."""
        choices: dict[str, Path | None] = {OWN_REPO_ALIAS: None}
        if vault_path is not None:
            choices[VAULT_ALIAS] = vault_path
        return {**choices, **self.workdirs}

    def resolve_workdir(self, name: str | None, repo: Path, vault_path: Path | None = None) -> Path:
        """Takma ad -> yol. Model yol veremez; yalnız bu tablodaki adlar çözülür."""
        name = name or OWN_REPO_ALIAS
        choices = self.workdir_choices(vault_path)
        if name not in choices:
            known = ", ".join(sorted(choices))
            raise ConfigChoiceError(f"Bilinmeyen başlangıç klasörü '{name}'. Tanımlılar: {known}.")
        chosen = choices[name]
        return repo if chosen is None else chosen


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
    step_delay_seconds: float = Field(default=0.3, ge=0, le=3)
    # ADR 0039 ek karar: "auto" oturum içi hassas adımları ve başlangıcı sormaz
    # (`computer_use`, `window_control`, `screen_read` `auto_mode_ok` ile lekeli turda da
    # onaylanmaz,
    # atlama `approval_skipped` olarak denetime yazılır); kodla zorlanan engeller,
    # sınırlar ve Esc iki kipte de aynıdır.
    mode: Literal["accept", "auto"] = "accept"
    # Kotaya takılan görsel model için sırayla denenecek diğer görüntü kabul eden modeller.
    fallback_models: tuple[str, ...] = ()


def same_hotkey(first: str, second: str) -> bool:
    """"Ctrl+Alt+D" ile "alt + ctrl + d" aynı kısayoldur; iki özellik aynı tuşu kaydedemez."""
    def normal(text: str) -> tuple[frozenset[str], str]:
        *modifiers, key = [part.strip().lower() for part in text.split("+")]
        return frozenset(modifiers), key

    return bool(first.strip() and second.strip()) and normal(first) == normal(second)


class DictationConfig(BaseModel):
    """Dikte kipi (ADR 0057): kısayola bas, konuş, tekrar bas; metin odaktaki uygulamaya yazılır.

    Komut yolu DEĞİLDİR: metin modele, geçmişe ya da hafızaya gitmez. Varsayılan kapalı.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    enabled: bool = False
    hotkey: str = Field(default="ctrl+alt+d", min_length=1, max_length=40)
    # Kayıt bu süreyi aşarsa kendiliğinden biter ve o ana kadarki ses yazılır.
    max_seconds: float = Field(default=120.0, ge=1, le=600)


class SelectionActionsConfig(BaseModel):
    """Seçili metin eylemleri (ADR 0062): kısayolla Çevir/Düzelt/Özetle/Açıkla. Varsayılan kapalı.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    enabled: bool = False
    hotkey: str = Field(default="ctrl+alt+s", min_length=1, max_length=40)
    # "Çevir" eyleminin hedef dili (ör. en, tr, de); modele düz metin olarak söylenir.
    translate_to: str = Field(default="en", min_length=1, max_length=40)


class RoutinesConfig(BaseModel):
    """Rutinler (ADR 0058): adlandırılmış sabit araç adımları. Varsayılan kapalı."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    enabled: bool = False


MAX_TRIGGERS = 10


class TriggerConfig(BaseModel):
    """Config'te tanımlı tetikleyici (ADR 0061): yalnız kullanıcı yazar, hiçbir araç yazamaz."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(pattern=r"^[A-Za-z0-9_-]{1,32}$")
    folder: str = Field(min_length=1, max_length=500)
    pattern: str = Field(default="*", min_length=1, max_length=100)
    # Yer tutucular: {name} (tetikleyici adı), {file} (yalnız dosya adı).
    message: str = Field(default="Yeni dosya geldi: {file}", min_length=1, max_length=300)
    # Kullanıcının yazdığı istem şablonu; {path} dosyanın tam yoludur. Sohbet kutusuna yazılır.
    suggest: str | None = Field(default=None, min_length=1, max_length=500)
    # ADR 0058 rutin adı; düğme normal, onaylı bir rutin çalıştırır.
    routine: str | None = Field(default=None, min_length=1, max_length=40)

    @field_validator("folder")
    @classmethod
    def validate_folder(cls, value: str) -> str:
        absolute = Path(value).is_absolute() or PureWindowsPath(value).is_absolute()
        if chr(0) in value or not absolute:
            raise ValueError("folder mutlak bir yol olmalı (ör. C:/Users/ben/Downloads)")
        return value

    @model_validator(mode="after")
    def one_button(self) -> Self:
        if self.suggest is not None and self.routine is not None:
            raise ValueError("Bir tetikleyici en çok bir düğme taşır: suggest ya da routine")
        return self


class TriggersConfig(BaseModel):
    """Tetikleyiciler (ADR 0061): klasöre yeni dosya gelince bildirim. Varsayılan kapalı."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    enabled: bool = False
    items: tuple[TriggerConfig, ...] = Field(default=(), max_length=MAX_TRIGGERS)

    @field_validator("items")
    @classmethod
    def unique_names(cls, values: tuple[TriggerConfig, ...]) -> tuple[TriggerConfig, ...]:
        names = [item.name.casefold() for item in values]
        if len(names) != len(set(names)):
            raise ValueError("Tetikleyici adları benzersiz olmalı")
        return values


class CalendarConfig(BaseModel):
    """Takvim, salt okunur (ADR 0059). ICS adresi sırdır: ortam/kasa, YAML'a yazılmaz."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    enabled: bool = False
    lookahead_days: int = Field(default=7, ge=1, le=60)
    # Takvim içeriği kişiseldir: bu açılmadıkça bulut modele ve Telegram'a gitmez.
    allow_cloud: bool = False


class MailConfig(BaseModel):
    """Posta, salt okunur IMAP/TLS (ADR 0059). Parola sırdır: ortam/kasa, YAML'a yazılmaz."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    enabled: bool = False
    host: str = Field(default="", max_length=255, pattern=r"^[A-Za-z0-9.-]*$")
    port: int = Field(default=993, ge=1, le=65535)
    username: str = Field(default="", max_length=255)
    folder: str = Field(default="INBOX", min_length=1, max_length=100)
    max_messages: int = Field(default=10, ge=1, le=50)
    allow_cloud: bool = False


class SkillsConfig(BaseModel):
    """Kullanıcı becerileri (ADR 0060): <klasör>/*.md. Varsayılan kapalı."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    enabled: bool = False
    # Boşsa güvenliyse mevcut state_dir/beceriler, yoksa kullanıcı veri klasörü (skills.skills_dir).
    # Seçilen klasör vault/proje deposu/çalışma alanı içindeyse beceriler yüklenmez.
    folder: Path | None = None

    @field_validator("folder")
    @classmethod
    def _absolute_folder(cls, value: Path | None) -> Path | None:
        if value is not None and not value.is_absolute():
            raise ValueError("skills.folder mutlak yol olmalı")
        return value


class ResearchConfig(BaseModel):
    """Arka plan araştırma işi (ADR 0063). Varsayılan kapalı."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    enabled: bool = False
    # Başlatma onayı tam bu kadar web araması için ön onaydır; sayaç bitince arama reddedilir.
    max_searches: int = Field(default=12, ge=1, le=50)
    max_seconds: float = Field(default=600.0, ge=1, le=3600)


class DailySummaryConfig(BaseModel):
    """Gün sonu özeti (ADR 0066): audit'ten LLM'siz, yerel Markdown. Varsayılan kapalı."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    enabled: bool = False
    # Yerel saat (proactive.timezone), "SS:DD".
    at: str = Field(default="22:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    # "Vault'a ekle" düğmesinin ekleyeceği not (vault köküne göreli); yalnız memory.vault_path
    # tanımlıysa düğme görünür ve ekleme her zaman onaylıdır.
    vault_note: str = Field(default="Artemis-Gunluk.md", min_length=1, max_length=200)


class FileSearchConfig(BaseModel):
    """Yerel dosya araması (ADR 0065). Varsayılan kapalı; kökleri yalnız kullanıcı yazar."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    enabled: bool = False
    roots: list[str] = Field(default_factory=list)  # mutlak klasörler; hiçbir araç değiştirmez
    max_file_mb: int = Field(default=5, ge=1, le=100)
    exclude_dirs: list[str] = Field(
        default_factory=lambda: [".git", "node_modules", ".venv", "__pycache__", "AppData"]
    )

    @field_validator("roots")
    @classmethod
    def _absolute_roots(cls, values: list[str]) -> list[str]:
        if any(not os.path.isabs(value) for value in values):
            raise ValueError("file_search.roots mutlak klasör yolları olmalı")
        return values


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    workspace: Path = Path("desktop")
    state_dir: Path = Path(".artemis")
    audit_path: Path = Path(".artemis/audit.jsonl")
    models: dict[str, ModelConfig] = Field(default_factory=dict)
    endpoints: dict[str, EndpointConfig] = Field(default_factory=dict)
    primary_model: str = ""
    fallback_model: str | None = None
    # Zor işler modeli (model kademesi): boşsa her tur ana modelle gider. Tur başında
    # brain/tiering.choose_model seçer; kullanılamazsa (ücret/bütçe/hata) ana modele düşülür.
    hard_model: str | None = None
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
    dictation: DictationConfig = Field(default_factory=DictationConfig)
    selection_actions: SelectionActionsConfig = Field(default_factory=SelectionActionsConfig)
    routines: RoutinesConfig = Field(default_factory=RoutinesConfig)
    triggers: TriggersConfig = Field(default_factory=TriggersConfig)
    calendar: CalendarConfig = Field(default_factory=CalendarConfig)
    mail: MailConfig = Field(default_factory=MailConfig)
    skills: SkillsConfig = Field(default_factory=SkillsConfig)
    research: ResearchConfig = Field(default_factory=ResearchConfig)
    file_search: FileSearchConfig = Field(default_factory=FileSearchConfig)
    daily_summary: DailySummaryConfig = Field(default_factory=DailySummaryConfig)
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
        voice = self.voice
        if same_hotkey(self.dictation.hotkey, voice.listening_hotkey):
            raise ValueError("Dikte kısayolu ses kısayoluyla aynı olamaz")
        selection = self.selection_actions.hotkey
        if same_hotkey(selection, voice.listening_hotkey) or same_hotkey(
            selection, self.dictation.hotkey
        ):
            raise ValueError("Seçili metin kısayolu ses ve dikte kısayolundan farklı olmalı")
        if voice.stt_mode == "hybrid" and (
            voice.stt_cloud_endpoint not in self.endpoints
            or (voice.stt_cloud_protocol == "openai" and not voice.stt_cloud_model.strip())
        ):
            raise ValueError(
                "Karma tanıma için stt_cloud_endpoint endpoints içinde tanımlı ve "
                "stt_cloud_model dolu olmalı (xai protokolünde model gerekmez)"
            )
        if any(name not in self.applications for name in self.close_without_confirmation):
            raise ValueError("Onaysız kapatılacak uygulama izin listesinde tanımlanmalı")
        if self.primary_model and self.primary_model not in self.models:
            raise ValueError("Birincil model models içinde tanımlanmalı")
        if self.hard_model and self.hard_model not in self.models:
            raise ValueError("Zor işler modeli models içinde tanımlanmalı")
        if any(
            self.models[name].kind == "image"
            for name in (
                self.primary_model, self.hard_model or "", self.computer_use.model,
                *self.computer_use.fallback_models,
            )
            if name in self.models
        ):
            raise ValueError(
                "Resim modeli birincil, zor işler ya da bilgisayar kullanımı modeli olamaz"
            )
        if self.fallback_model:
            # Arıza yedeği herhangi bir sohbet modeli olabilir (ADR 0055): bulut modeline giden
            # gizli içerik sağlayıcı sınırında zaten maskelenir. Yerel (ollama) yedek tam içeriği
            # alır ve tek araçla sınırlıdır (router.local_fallback).
            fallback = self.models.get(self.fallback_model)
            if fallback is None or fallback.kind != "chat":
                raise ValueError("Yedek model tanımlı bir sohbet modeli olmalı")
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
    for field in (
        "stt_model", "game_mode_stt_model", "piper_model", "piper_config", "noise_model_dir"
    ):
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
    if agent.project_entries:
        agent_updates["project_entries"] = {
            alias: entry
            if entry.path.is_absolute()
            else entry.model_copy(update={"path": (base / entry.path).resolve()})
            for alias, entry in agent.project_entries.items()
        }
    if agent.workdirs:
        agent_updates["workdirs"] = {
            alias: value if value.is_absolute() else (base / value).resolve()
            for alias, value in agent.workdirs.items()
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
