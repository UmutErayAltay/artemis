"""Ses modeli sağlık yöneticisi testleri (Qt'siz çekirdek + çift konumlu UI, çevrimdışı)."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import threading
from pathlib import Path

import pytest
import yaml

from artemis.channels.voice import denoise, silero
from artemis.channels.voice.model_health import (
    PIPER_VOICE_CATALOG,
    VoiceModelEntry,
    clean_voice_model,
    download_voice_model,
    inventory_voice_models,
    summarize_health,
    verify_voice_model,
)
from artemis.config.model_paths import DEFAULT_NOISE_MODEL_DIR
from artemis.config.settings import Settings, VoiceConfig

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


def _write_config(tmp_path: Path, voice_overrides: dict[str, object] | None = None) -> Path:
    cfg = tmp_path / "config.yaml"
    data: dict[str, object] = {}
    if voice_overrides:
        data["voice"] = voice_overrides
    cfg.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    return cfg


def _make_whisper_cache(
    cache_root: Path,
    name: str,
    *,
    bin_bytes: bytes = b"ct2-model",
    cfg_text: str = '{"vocab_size": 10}',
) -> Path:
    snap = cache_root / f"models--Systran--faster-whisper-{name}" / "snapshots" / "rev1"
    snap.mkdir(parents=True, exist_ok=True)
    (snap / "model.bin").write_bytes(bin_bytes)
    (snap / "config.json").write_text(cfg_text, encoding="utf-8")
    return snap


def _make_piper_pair(
    models_dir: Path,
    voice_id: str,
    *,
    onnx_bytes: bytes = b"piper-onnx-bytes",
    json_text: str = '{"audio": {"sample_rate": 22050}}',
) -> tuple[Path, Path]:
    models_dir.mkdir(parents=True, exist_ok=True)
    onnx = models_dir / f"{voice_id}.onnx"
    cfg = models_dir / f"{voice_id}.onnx.json"
    onnx.write_bytes(onnx_bytes)
    cfg.write_text(json_text, encoding="utf-8")
    return onnx, cfg


def _copy_bundled_silero_and_dfn3(tmp_path: Path) -> tuple[Path, Path]:
    silero_dir = tmp_path / "silero"
    silero_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(
        silero.SILERO_MODEL_DIR / silero.SILERO_MODEL_NAME,
        silero_dir / silero.SILERO_MODEL_NAME,
    )
    dfn3_dir = tmp_path / "dfn3"
    dfn3_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(
        DEFAULT_NOISE_MODEL_DIR / denoise.STEP_MODEL_NAME,
        dfn3_dir / denoise.STEP_MODEL_NAME,
    )
    return silero_dir, dfn3_dir


def test_inventory_covers_all_four_families_with_sizes_and_statuses(
    tmp_path: Path,
) -> None:
    cfg = _write_config(tmp_path)
    hf_cache = tmp_path / "hf_cache"
    small_snap = _make_whisper_cache(hf_cache, "small", bin_bytes=b"w" * 2048)
    piper_dir = tmp_path / "models"
    _make_piper_pair(piper_dir, "tr_TR-dfki-medium", onnx_bytes=b"p" * 1024)
    silero_dir, dfn3_dir = _copy_bundled_silero_and_dfn3(tmp_path)

    entries = inventory_voice_models(
        cfg,
        whisper_installed=lambda name: small_snap if name == "small" else None,
        piper_models_dir=piper_dir,
        silero_dir=silero_dir,
        dfn3_dir=dfn3_dir,
        hf_cache_roots=(hf_cache,),
    )
    by_id = {e.id: e for e in entries}

    assert {"whisper", "piper", "silero", "dfn3"} == {e.family for e in entries}
    assert by_id["silero:vad"].status == "ok"
    assert by_id["silero:vad"].bundled is True
    assert by_id["silero:vad"].can_clean is False
    assert by_id["silero:vad"].size_bytes > 2_000_000

    assert by_id["dfn3:step"].status == "ok"
    assert by_id["dfn3:step"].bundled is False  # özel dfn3_dir verildi
    assert by_id["dfn3:step"].can_clean is False
    assert by_id["dfn3:step"].size_bytes > 8_000_000

    assert by_id["piper:tr_TR-dfki-medium"].status == "ok"
    assert by_id["piper:tr_TR-dfki-medium"].can_clean is True
    assert by_id["piper:tr_TR-dfki-medium"].size_bytes > 1024
    assert by_id["piper:en_US-lessac-medium"].status == "missing"

    assert by_id["whisper:small"].status == "ok"
    assert by_id["whisper:small"].can_clean is True
    assert by_id["whisper:small"].size_bytes >= 2048
    assert by_id["whisper:tiny"].status == "missing"

    summary = summarize_health(entries)
    assert summary.ok_count == 4
    assert summary.corrupt_count == 0
    assert summary.missing_count >= 7


def test_corrupt_sha_and_missing_detection_across_families(tmp_path: Path) -> None:
    cfg = _write_config(tmp_path)
    hf_cache = tmp_path / "hf_cache"
    bad_whisper = _make_whisper_cache(hf_cache, "tiny", bin_bytes=b"actual", cfg_text="{bad")
    piper_dir = tmp_path / "models"
    _make_piper_pair(
        piper_dir,
        "tr_TR-dfki-medium",
        onnx_bytes=b"wrong-bytes",
        json_text='{"audio": {"sample_rate": 22050}}',
    )
    silero_dir = tmp_path / "silero"
    silero_dir.mkdir()
    (silero_dir / silero.SILERO_MODEL_NAME).write_bytes(b"corrupt-silero")
    dfn3_dir = tmp_path / "dfn3_missing"
    dfn3_dir.mkdir()

    entries = inventory_voice_models(
        cfg,
        whisper_installed=lambda name: bad_whisper if name == "tiny" else None,
        piper_models_dir=piper_dir,
        piper_pins={"tr_TR-dfki-medium": ("0" * 64, None)},
        silero_dir=silero_dir,
        dfn3_dir=dfn3_dir,
        hf_cache_roots=(hf_cache,),
    )
    by_id = {e.id: e for e in entries}

    assert by_id["whisper:tiny"].status == "corrupt"
    assert by_id["piper:tr_TR-dfki-medium"].status == "corrupt"
    assert by_id["piper:tr_TR-dfki-medium"].integrity == "sha256_mismatch"
    assert by_id["silero:vad"].status == "corrupt"
    assert by_id["silero:vad"].integrity == "sha256_mismatch"
    assert by_id["dfn3:step"].status == "missing"


def test_bundled_and_custom_models_protected_from_clean(tmp_path: Path) -> None:
    custom_whisper = tmp_path / "custom_whisper"
    custom_whisper.mkdir()
    (custom_whisper / "model.bin").write_bytes(b"custom-bin")
    (custom_whisper / "config.json").write_text("{}", encoding="utf-8")

    cfg = _write_config(tmp_path, {"stt_model": str(custom_whisper)})
    settings = Settings(voice=VoiceConfig(stt_model=custom_whisper))

    entries = inventory_voice_models(
        cfg,
        settings,
        whisper_installed=lambda _n: None,
        hf_cache_roots=(),
    )
    by_id = {e.id: e for e in entries}

    # Paket içi Silero VAD ve varsayılan DFN3 kesinlikle silinemez
    assert by_id["silero:vad"].bundled is True
    assert by_id["silero:vad"].can_clean is False
    assert by_id["dfn3:step"].bundled is True
    assert by_id["dfn3:step"].can_clean is False
    assert by_id["whisper:custom:stt_model"].status == "ok"
    assert by_id["whisper:custom:stt_model"].can_clean is False

    for protected_id in ("silero:vad", "dfn3:step", "whisper:custom:stt_model"):
        with pytest.raises(ValueError):
            clean_voice_model(
                protected_id,
                cfg,
                settings,
                whisper_installed=lambda _n: None,
                hf_cache_roots=(),
            )

    assert (silero.SILERO_MODEL_DIR / silero.SILERO_MODEL_NAME).is_file()
    assert (DEFAULT_NOISE_MODEL_DIR / denoise.STEP_MODEL_NAME).is_file()
    assert (custom_whisper / "model.bin").is_file()


def test_offline_download_verify_and_clean_piper_and_whisper(tmp_path: Path) -> None:
    cfg = _write_config(tmp_path)
    piper_dir = tmp_path / "models"
    hf_cache = tmp_path / "hf_cache"
    silero_dir, dfn3_dir = _copy_bundled_silero_and_dfn3(tmp_path)

    onnx_payload = b"fake-piper-onnx-payload"
    json_payload = json.dumps({"audio": {"sample_rate": 22050}}).encode("utf-8")
    onnx_sha = hashlib.sha256(onnx_payload).hexdigest()
    json_sha = hashlib.sha256(json_payload).hexdigest()
    spec = PIPER_VOICE_CATALOG["tr_TR-dfki-medium"]

    def fake_fetch(url: str) -> bytes:
        if url == spec.onnx_url:
            return onnx_payload
        if url == spec.json_url:
            return json_payload
        raise AssertionError(f"Beklenmeyen URL: {url}")

    # 1) SHA uyuşmazlığı indirilen yarım dosyayı bırakmaz
    with pytest.raises(ValueError, match="SHA-256"):
        download_voice_model(
            "piper:tr_TR-dfki-medium",
            cfg,
            whisper_installed=lambda _n: None,
            url_fetcher=fake_fetch,
            piper_models_dir=piper_dir,
            piper_pins={"tr_TR-dfki-medium": ("f" * 64, json_sha)},
            silero_dir=silero_dir,
            dfn3_dir=dfn3_dir,
            hf_cache_roots=(hf_cache,),
        )
    assert not (piper_dir / "tr_TR-dfki-medium.onnx").exists()
    assert not (piper_dir / "tr_TR-dfki-medium.onnx.part").exists()

    # 2) Doğru SHA ile Piper indir -> doğrula -> temizle
    downloaded_piper = download_voice_model(
        "piper:tr_TR-dfki-medium",
        cfg,
        whisper_installed=lambda _n: None,
        url_fetcher=fake_fetch,
        piper_models_dir=piper_dir,
        piper_pins={"tr_TR-dfki-medium": (onnx_sha, json_sha)},
        silero_dir=silero_dir,
        dfn3_dir=dfn3_dir,
        hf_cache_roots=(hf_cache,),
    )
    assert downloaded_piper.status == "ok"
    assert downloaded_piper.integrity == "sha256_ok"
    assert downloaded_piper.can_clean is True

    cleaned_piper = clean_voice_model(
        "piper:tr_TR-dfki-medium",
        cfg,
        whisper_installed=lambda _n: None,
        piper_models_dir=piper_dir,
        silero_dir=silero_dir,
        dfn3_dir=dfn3_dir,
        hf_cache_roots=(hf_cache,),
    )
    assert cleaned_piper.status == "missing"
    assert not (piper_dir / "tr_TR-dfki-medium.onnx").exists()
    assert not (piper_dir / "tr_TR-dfki-medium.onnx.json").exists()

    # 3) Whisper çevrimdışı indirme -> doğrulama -> önbellek temizleme
    installed_map: dict[str, Path] = {}

    def fake_whisper_download(name: str) -> Path:
        snap = _make_whisper_cache(hf_cache, name, bin_bytes=b"whisper-tiny-bin")
        installed_map[name] = snap
        return snap

    def fake_whisper_installed(name: str) -> Path | None:
        snap = installed_map.get(name)
        if snap is not None and (snap / "model.bin").is_file():
            return snap
        return None

    dl_whisper = download_voice_model(
        "whisper:tiny",
        cfg,
        whisper_installed=fake_whisper_installed,
        whisper_downloader=fake_whisper_download,
        piper_models_dir=piper_dir,
        silero_dir=silero_dir,
        dfn3_dir=dfn3_dir,
        hf_cache_roots=(hf_cache,),
    )
    assert dl_whisper.status == "ok"
    assert dl_whisper.can_clean is True

    verified_whisper = verify_voice_model(
        "whisper:tiny",
        cfg,
        whisper_installed=fake_whisper_installed,
        piper_models_dir=piper_dir,
        silero_dir=silero_dir,
        dfn3_dir=dfn3_dir,
        hf_cache_roots=(hf_cache,),
    )
    assert verified_whisper.status == "ok"
    assert verified_whisper.integrity == "sha256_ok"

    cleaned_whisper = clean_voice_model(
        "whisper:tiny",
        cfg,
        whisper_installed=fake_whisper_installed,
        piper_models_dir=piper_dir,
        silero_dir=silero_dir,
        dfn3_dir=dfn3_dir,
        hf_cache_roots=(hf_cache,),
    )
    assert cleaned_whisper.status == "missing"
    assert not (hf_cache / "models--Systran--faster-whisper-tiny").exists()


def test_widget_offline_operations_bundled_protection_and_demo_mode(
    tmp_path: Path,
) -> None:
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication

    from artemis.channels.desktop.voice_model_manager import VoiceModelManagerWidget

    app = QApplication.instance() or QApplication([])
    cfg = _write_config(tmp_path)
    piper_dir = tmp_path / "models"
    hf_cache = tmp_path / "hf_cache"
    silero_dir, dfn3_dir = _copy_bundled_silero_and_dfn3(tmp_path)

    spec = PIPER_VOICE_CATALOG["tr_TR-dfki-medium"]
    onnx_payload = b"piper-ui-bytes"
    json_payload = b'{"audio": {"sample_rate": 22050}}'

    def fake_fetch(url: str) -> bytes:
        return onnx_payload if url == spec.onnx_url else json_payload

    common = {
        "whisper_installed": lambda _n: None,
        "piper_models_dir": piper_dir,
        "silero_dir": silero_dir,
        "dfn3_dir": dfn3_dir,
        "hf_cache_roots": (hf_cache,),
    }
    widget = VoiceModelManagerWidget(
        cfg,
        inventory_fn=lambda p: inventory_voice_models(p, **common),
        verify_fn=lambda eid, p: verify_voice_model(eid, p, **common),
        download_fn=lambda eid, p: download_voice_model(
            eid, p, url_fetcher=fake_fetch, **common
        ),
        clean_fn=lambda eid, p: clean_voice_model(eid, p, **common),
    )
    try:
        # Silero VAD seçiliyse paket içi koruma gereği Temizle kapalı olmalı
        bundled_entry = VoiceModelEntry(
            id="silero:vad",
            family="silero",
            name="silero_vad.onnx",
            label="Silero VAD (v5.1.2)",
            path=silero_dir / silero.SILERO_MODEL_NAME,
            size_bytes=2_327_524,
            status="ok",
            integrity="sha256_ok",
            bundled=True,
            can_download=False,
            can_clean=False,
            active=True,
            detail="Paket içi",
        )
        widget.apply_entries([bundled_entry, *widget.entries()[1:]])
        assert widget.select_entry("silero:vad")
        assert not widget.clean_button.isEnabled()
        assert not widget.download_button.isEnabled()
        assert widget.verify_button.isEnabled()
        assert not widget.trigger_clean("silero:vad")

        # Piper indir -> arka plan iş parçacığında tamamlanır
        assert widget.select_entry("piper:tr_TR-dfki-medium")
        assert widget.download_button.isEnabled()
        assert widget.trigger_download("piper:tr_TR-dfki-medium")
        thread = widget._thread
        assert thread is not None
        assert thread.wait(3000)
        app.processEvents()
        entry = widget.selected_entry()
        assert entry is not None and entry.status == "ok" and entry.can_clean

        # Doğrula ve sonra temizle
        assert widget.trigger_verify("piper:tr_TR-dfki-medium")
        assert widget._thread is not None and widget._thread.wait(3000)
        app.processEvents()

        assert widget.trigger_clean("piper:tr_TR-dfki-medium")
        assert widget._thread is not None and widget._thread.wait(3000)
        app.processEvents()
        cleaned = widget.selected_entry()
        assert cleaned is not None and cleaned.status == "missing"

        # Demo modunda indirme ve temizleme devre dışı kalır
        widget.set_demo(True)
        assert not widget.download_button.isEnabled()
        assert not widget.clean_button.isEnabled()
        assert not widget.trigger_download("piper:tr_TR-dfki-medium")
        assert not widget.trigger_clean("piper:tr_TR-dfki-medium")
    finally:
        widget.finish()
        widget.deleteLater()
        app.processEvents()


def test_widget_finish_detaches_running_thread(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication

    from artemis.channels.desktop import voice_model_manager as mod

    app = QApplication.instance() or QApplication([])
    cfg = _write_config(tmp_path)
    silero_dir, dfn3_dir = _copy_bundled_silero_and_dfn3(tmp_path)
    entered = threading.Event()
    release = threading.Event()

    def base_inventory(path: Path) -> list[VoiceModelEntry]:
        return inventory_voice_models(
            path,
            whisper_installed=lambda _n: None,
            piper_models_dir=tmp_path / "models",
            silero_dir=silero_dir,
            dfn3_dir=dfn3_dir,
            hf_cache_roots=(),
        )

    def slow_verify(entry_id: str, path: Path) -> VoiceModelEntry:
        entered.set()
        release.wait(5)
        return verify_voice_model(
            entry_id,
            path,
            whisper_installed=lambda _n: None,
            piper_models_dir=tmp_path / "models",
            silero_dir=silero_dir,
            dfn3_dir=dfn3_dir,
            hf_cache_roots=(),
        )

    monkeypatch.setattr(mod, "_SHUTDOWN_WAIT_MS", 50)
    widget = mod.VoiceModelManagerWidget(
        cfg, inventory_fn=base_inventory, verify_fn=slow_verify
    )
    try:
        assert widget.trigger_verify("silero:vad")
        assert entered.wait(3)
        thread = widget._thread
        assert thread is not None and thread.isRunning() and thread.parent() is widget

        widget.finish()
        assert thread.parent() is None
        assert thread in mod._DETACHED
        widget.deleteLater()
        app.processEvents()

        release.set()
        assert thread.wait(3000)
        app.processEvents()
        assert thread not in mod._DETACHED
    finally:
        release.set()


def test_settings_panel_dual_mount_sync_9_pages_and_demo_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication

    from artemis.channels.desktop.settings_panel import SettingsPanel
    from artemis.channels.voice import stt_catalog
    from tests.test_settings_panel import config

    monkeypatch.setattr(stt_catalog, "installed_path", lambda _name: None)
    app = QApplication.instance() or QApplication([])
    cfg = config(tmp_path)

    panel = SettingsPanel(cfg)
    try:
        names = panel.section_names()
        assert len(names) == 9
        ses_page = panel.stack.widget(names.index("Ses"))
        about_page = panel.stack.widget(names.index("Hakkında ve Güncelleme"))
        assert ses_page is not None and ses_page.isAncestorOf(panel.voice_model_manager)
        assert about_page is not None and about_page.isAncestorOf(
            panel.about_voice_model_manager
        )

        # İki yöneticinin başlangıç envanteri eşit olmalı
        assert [e.id for e in panel.voice_model_manager.entries()] == [
            e.id for e in panel.about_voice_model_manager.entries()
        ]

        # Birindeki güncelleme diğerine otomatik yansımalı
        updated = [
            VoiceModelEntry(
                id="piper:tr_TR-dfki-medium",
                family="piper",
                name="tr_TR-dfki-medium",
                label="Piper · Türkçe · DFKI Medium",
                path=tmp_path / "models" / "tr_TR-dfki-medium.onnx",
                size_bytes=4096,
                status="ok",
                integrity="sha256_ok",
                bundled=False,
                can_download=False,
                can_clean=True,
                active=False,
                detail="Senkron test",
            )
            if e.id == "piper:tr_TR-dfki-medium"
            else e
            for e in panel.voice_model_manager.entries()
        ]
        panel.voice_model_manager.apply_entries(updated)
        panel.voice_model_manager.models_changed.emit(updated)
        app.processEvents()
        about_map = {e.id: e for e in panel.about_voice_model_manager.entries()}
        assert about_map["piper:tr_TR-dfki-medium"].status == "ok"
        assert about_map["piper:tr_TR-dfki-medium"].size_bytes == 4096
    finally:
        panel.finish_inventory()
        panel.deleteLater()
        app.processEvents()

    demo_panel = SettingsPanel(cfg, demo=True)
    try:
        assert not demo_panel.voice_model_manager.download_button.isEnabled()
        assert not demo_panel.voice_model_manager.clean_button.isEnabled()
        assert not demo_panel.about_voice_model_manager.download_button.isEnabled()
        assert not demo_panel.about_voice_model_manager.clean_button.isEnabled()
    finally:
        demo_panel.finish_inventory()
        demo_panel.deleteLater()
        app.processEvents()
