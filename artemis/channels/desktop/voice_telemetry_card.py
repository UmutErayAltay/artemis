"""Ayarlar > Ses > Dinleme (Giriş): ses gecikmesi ve kalite özeti kartı."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError
from PySide6.QtGui import QShowEvent
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from artemis.channels.desktop.form_kit import meta_label
from artemis.channels.voice.metrics import MIN_TARGET_SAMPLES, _distribution
from artemis.config.settings import load_settings
from artemis.i18n import t

_MAX_REPORT_BYTES = 10 * 1024 * 1024


@dataclass(frozen=True)
class VoiceTelemetrySummary:
    """`.artemis/voice-telemetry/` altındaki schema-v2 raporlarının toplu özeti."""

    status: str  # "missing" | "empty" | "corrupt" | "ok"
    file_count: int
    skipped_files: int
    total_utterances: int
    valid_samples: int
    invalid_timing: int
    aborted: int
    no_audio: int
    outcomes: dict[str, int]
    p50_ms: float | None
    p90_ms: float | None
    latency_samples: int
    barge_in_p90_ms: float | None
    latest_file: str
    latest_utterances: int
    latest_samples: int
    latest_p50_ms: float | None
    latest_p90_ms: float | None
    targets_met: bool | None


def _is_non_bool_int(value: object) -> bool:
    return type(value) is int and value >= 0


def _finite_number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    val = float(value)
    return val if math.isfinite(val) and val >= 0 else None


def _extract_report_latencies(
    data: dict[str, Any],
) -> tuple[list[float], list[float], float | None, float | None]:
    pcm_values: list[float] = []
    barge_values: list[float] = []
    utterances = data.get("utterances")
    if isinstance(utterances, list):
        for sample in utterances:
            if not isinstance(sample, dict):
                continue
            if sample.get("response_kind") != "task" or sample.get("invalid_timing"):
                continue
            timings = sample.get("timings")
            if not isinstance(timings, dict):
                continue
            speech_end = _finite_number(timings.get("speech_end"))
            first_pcm = _finite_number(timings.get("first_pcm"))
            if speech_end is not None and first_pcm is not None and first_pcm >= speech_end:
                pcm_values.append((first_pcm - speech_end) * 1000.0)
            stop_req = _finite_number(timings.get("stop_requested"))
            play_stop = _finite_number(timings.get("playback_stopped"))
            if stop_req is not None and play_stop is not None and play_stop >= stop_req:
                barge_values.append((play_stop - stop_req) * 1000.0)
    dist = data.get("speech_end_to_first_pcm_ms")
    rep_p50 = _finite_number(dist.get("p50")) if isinstance(dist, dict) else None
    rep_p90 = _finite_number(dist.get("p90")) if isinstance(dist, dict) else None
    return pcm_values, barge_values, rep_p50, rep_p90


def summarize_voice_telemetry(telemetry_dir: Path) -> VoiceTelemetrySummary:
    """`voice-telemetry/*.json` dosyalarını güvenli biçimde özetler; bozuk dosyaları atlar."""
    if not telemetry_dir.is_dir():
        return VoiceTelemetrySummary(
            status="missing",
            file_count=0,
            skipped_files=0,
            total_utterances=0,
            valid_samples=0,
            invalid_timing=0,
            aborted=0,
            no_audio=0,
            outcomes={},
            p50_ms=None,
            p90_ms=None,
            latency_samples=0,
            barge_in_p90_ms=None,
            latest_file="",
            latest_utterances=0,
            latest_samples=0,
            latest_p50_ms=None,
            latest_p90_ms=None,
            targets_met=None,
        )

    candidates = [p for p in telemetry_dir.glob("*.json") if p.is_file()]
    candidates.sort(key=lambda p: (p.stat().st_mtime_ns, p.name))
    if not candidates:
        return VoiceTelemetrySummary(
            status="empty",
            file_count=0,
            skipped_files=0,
            total_utterances=0,
            valid_samples=0,
            invalid_timing=0,
            aborted=0,
            no_audio=0,
            outcomes={},
            p50_ms=None,
            p90_ms=None,
            latency_samples=0,
            barge_in_p90_ms=None,
            latest_file="",
            latest_utterances=0,
            latest_samples=0,
            latest_p50_ms=None,
            latest_p90_ms=None,
            targets_met=None,
        )

    valid_files = 0
    skipped = 0
    total_utterances = 0
    valid_samples = 0
    invalid_timing = 0
    aborted = 0
    no_audio = 0
    outcomes: dict[str, int] = {}
    all_pcm: list[float] = []
    all_barge: list[float] = []
    latest_file = ""
    latest_utterances = 0
    latest_samples = 0
    latest_p50: float | None = None
    latest_p90: float | None = None
    latest_barge_fallback: float | None = None

    for path in candidates:
        try:
            if path.stat().st_size > _MAX_REPORT_BYTES:
                skipped += 1
                continue
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError, TypeError):
            skipped += 1
            continue
        if not isinstance(data, dict) or type(data.get("schema_version")) is not int:
            skipped += 1
            continue
        if data.get("schema_version") != 2 or not isinstance(data.get("outcomes"), dict):
            skipped += 1
            continue
        u_count = data.get("total_utterances", 0)
        s_count = data.get("samples", 0)
        inv_count = data.get("invalid_timing", 0)
        ab_count = data.get("aborted", 0)
        na_count = data.get("no_audio", 0)
        if not all(
            _is_non_bool_int(v) for v in (u_count, s_count, inv_count, ab_count, na_count)
        ):
            skipped += 1
            continue
        valid_files += 1
        total_utterances += u_count
        valid_samples += s_count
        invalid_timing += inv_count
        aborted += ab_count
        no_audio += na_count
        for key, raw_val in data["outcomes"].items():
            if isinstance(key, str) and _is_non_bool_int(raw_val):
                outcomes[key] = outcomes.get(key, 0) + raw_val
        pcm_vals, barge_vals, rep_p50, rep_p90 = _extract_report_latencies(data)
        all_pcm.extend(pcm_vals)
        all_barge.extend(barge_vals)
        latest_file = path.name
        latest_utterances = u_count
        latest_samples = s_count
        if pcm_vals:
            latest_dist = _distribution(pcm_vals, len(pcm_vals))
            latest_p50 = latest_dist["p50"]
            latest_p90 = latest_dist["p90"]
        else:
            latest_p50 = rep_p50
            latest_p90 = rep_p90
        latest_barge_fallback = _finite_number(data.get("barge_in_latency_ms"))

    if valid_files == 0:
        return VoiceTelemetrySummary(
            status="corrupt",
            file_count=0,
            skipped_files=skipped,
            total_utterances=0,
            valid_samples=0,
            invalid_timing=0,
            aborted=0,
            no_audio=0,
            outcomes={},
            p50_ms=None,
            p90_ms=None,
            latency_samples=0,
            barge_in_p90_ms=None,
            latest_file="",
            latest_utterances=0,
            latest_samples=0,
            latest_p50_ms=None,
            latest_p90_ms=None,
            targets_met=None,
        )

    if all_pcm:
        agg_dist = _distribution(all_pcm, len(all_pcm))
        p50_ms = agg_dist["p50"]
        p90_ms = agg_dist["p90"]
        latency_samples = len(all_pcm)
    else:
        p50_ms = latest_p50
        p90_ms = latest_p90
        latency_samples = valid_samples

    barge_p90 = (
        _distribution(all_barge, len(all_barge))["p90"] if all_barge else latest_barge_fallback
    )

    targets_met: bool | None = None
    if latency_samples >= MIN_TARGET_SAMPLES and p50_ms is not None and p90_ms is not None:
        targets_met = p50_ms < 1500.0 and p90_ms < 3000.0 and (
            barge_p90 is None or barge_p90 < 300.0
        )

    return VoiceTelemetrySummary(
        status="ok",
        file_count=valid_files,
        skipped_files=skipped,
        total_utterances=total_utterances,
        valid_samples=valid_samples,
        invalid_timing=invalid_timing,
        aborted=aborted,
        no_audio=no_audio,
        outcomes=outcomes,
        p50_ms=p50_ms,
        p90_ms=p90_ms,
        latency_samples=latency_samples,
        barge_in_p90_ms=barge_p90,
        latest_file=latest_file,
        latest_utterances=latest_utterances,
        latest_samples=latest_samples,
        latest_p50_ms=latest_p50,
        latest_p90_ms=latest_p90,
        targets_met=targets_met,
    )


def _fmt_ms(value: float | None) -> str:
    return f"{round(value)} ms" if value is not None else "—"


class VoiceTelemetryCard(QFrame):
    """Ses > Dinleme altında ses gecikme ve kalite metriklerini özetler."""

    def __init__(
        self,
        config_path: Path,
        parent: QWidget | None = None,
        *,
        telemetry_dir: Path | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("card")
        self._config_path = Path(config_path)
        self._telemetry_dir = telemetry_dir

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(8)

        header = QHBoxLayout()
        header.setSpacing(8)
        self.title_label = QLabel(t("Ses Gecikmesi ve Kalite"))
        self.title_label.setObjectName("sectionTitle")
        self.refresh_button = QPushButton(t("Yenile"))
        self.refresh_button.setAccessibleName(t("Ses ölçüm özetini yenile"))
        self.refresh_button.clicked.connect(self.refresh)
        header.addWidget(self.title_label, 1)
        header.addWidget(self.refresh_button)
        layout.addLayout(header)

        self.overview_label = meta_label("")
        self.outcomes_label = meta_label("")
        self.latency_label = meta_label("")
        self.quality_label = meta_label("")
        for label in (
            self.overview_label,
            self.outcomes_label,
            self.latency_label,
            self.quality_label,
        ):
            layout.addWidget(label)

        self.refresh()

    def _resolve_dir(self) -> Path | None:
        if self._telemetry_dir is not None:
            return self._telemetry_dir
        try:
            return load_settings(self._config_path).state_dir / "voice-telemetry"
        except (OSError, ValueError, ValidationError, yaml.YAMLError):
            return None

    def showEvent(self, event: QShowEvent) -> None:
        super().showEvent(event)
        self.refresh()

    def refresh(self) -> None:
        telemetry_dir = self._resolve_dir()
        if telemetry_dir is None:
            self.overview_label.setText(t("Ses ölçüm klasörü okunamadı."))
            self.outcomes_label.setText("")
            self.latency_label.setText("")
            self.quality_label.setText("")
            return

        summary = summarize_voice_telemetry(telemetry_dir)
        if summary.status == "missing":
            self.overview_label.setText(t("Henüz ses oturumu kaydı yok."))
            self.outcomes_label.setText(
                t("Ses açılıp kapatıldığında gecikme ve kalite özeti burada görünür.")
            )
            self.latency_label.setText("")
            self.quality_label.setText("")
            return
        if summary.status == "empty":
            self.overview_label.setText(t("Ses ölçüm klasöründe henüz rapor dosyası yok."))
            self.outcomes_label.setText("")
            self.latency_label.setText("")
            self.quality_label.setText("")
            return
        if summary.status == "corrupt":
            self.overview_label.setText(
                t("Geçerli ses ölçüm raporu bulunamadı ({skipped} dosya atlandı).").format(
                    skipped=summary.skipped_files
                )
            )
            self.outcomes_label.setText("")
            self.latency_label.setText("")
            self.quality_label.setText("")
            return

        self.overview_label.setText(
            t("Oturumlar: {files} · Toplam söz: {utterances} · Geçerli örnek: {samples}").format(
                files=summary.file_count,
                utterances=summary.total_utterances,
                samples=summary.valid_samples,
            )
        )
        completed = summary.outcomes.get("completed", 0)
        rejected = summary.outcomes.get("rejected", 0)
        self.outcomes_label.setText(
            t(
                "Tamamlanan: {completed} · Reddedilen: {rejected} · "
                "Yarım/Hata: {aborted} · Sessiz: {no_audio}"
            ).format(
                completed=completed,
                rejected=rejected,
                aborted=summary.aborted,
                no_audio=summary.no_audio,
            )
        )
        self.latency_label.setText(
            t(
                "İlk ses gecikmesi P50: {p50} · P90: {p90} "
                "(Son oturum P50: {lp50} · P90: {lp90})"
            ).format(
                p50=_fmt_ms(summary.p50_ms),
                p90=_fmt_ms(summary.p90_ms),
                lp50=_fmt_ms(summary.latest_p50_ms),
                lp90=_fmt_ms(summary.latest_p90_ms),
            )
        )
        if summary.targets_met is None:
            quality = t(
                "Hedef değerlendirmesi için en az {min_samples} örnek gerekir "
                "(şu an {count})."
            ).format(min_samples=MIN_TARGET_SAMPLES, count=summary.latency_samples)
        elif summary.targets_met:
            quality = t("Kalite hedefleri karşılandı (P50 < 1500 ms, P90 < 3000 ms).")
        else:
            quality = t("Kalite hedefi aşıldı (hedef P50 < 1500 ms, P90 < 3000 ms).")
        if summary.skipped_files:
            quality += " " + t("({skipped} bozuk/uyumsuz dosya atlandı)").format(
                skipped=summary.skipped_files
            )
        self.quality_label.setText(quality)
