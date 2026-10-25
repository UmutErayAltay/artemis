"""Aşama 2 değerlendirme corpus'ları: ağsız, TTS'siz, gerçek WAV dosyalarıyla (ADR 0045)."""

import asyncio
import json
import wave
from pathlib import Path
from typing import Any

import numpy as np
import pytest
import yaml

from bench import corpus_common_voice as cv
from bench import corpus_synthetic as synthetic
from bench import eval_audio
from bench.speech_accuracy import load_corpus, measure, resolve_target


def test_output_must_stay_outside_the_repository(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="depo"):
        eval_audio.ensure_outside_repo(eval_audio.REPO_ROOT / "reports" / "ses")
    with pytest.raises(ValueError, match="depo"):
        eval_audio.ensure_outside_repo(eval_audio.REPO_ROOT)
    assert eval_audio.ensure_outside_repo(tmp_path / "x") == (tmp_path / "x").resolve()


def test_resample_and_conditions_are_deterministic_and_bounded() -> None:
    tone = np.sin(2 * np.pi * 440 * np.arange(22_050) / 22_050).astype(np.float32)
    resampled = eval_audio.resample(tone, 22_050)
    assert len(resampled) == 16_000
    spectrum = np.abs(np.fft.rfft(resampled))
    assert abs(int(np.argmax(spectrum)) - 440) <= 1  # 1 sn: bin = Hz
    speech = eval_audio.resample(tone, 22_050)
    for name in eval_audio.CONDITIONS:
        first = eval_audio.apply_condition(name, speech, 7)
        assert np.array_equal(first, eval_audio.apply_condition(name, speech, 7)), name
        assert np.all(np.isfinite(first)) and len(first) > 0
    noisy = eval_audio.add_noise(speech, eval_audio.pink_noise(len(speech), 1), 5)
    noise = noisy - speech
    snr = 20 * np.log10(np.sqrt(np.mean(speech**2)) / np.sqrt(np.mean(noise**2)))
    assert abs(snr - 5) < 0.01
    band = np.abs(np.fft.rfft(eval_audio.telephone_band(speech)))
    assert band[:300].max() < 1e-6 * band[440]
    assert len(eval_audio.change_speed(speech, 1.25)) == round(16_000 / 1.25)
    with pytest.raises(ValueError):
        eval_audio.apply_condition("bilinmeyen", speech, 0)


def _clip(client: str, sentence: str, down: int = 0) -> cv.Clip:
    return cv.Clip(client, sentence, b"mp3", down)


def test_common_voice_selection_is_speaker_disjoint_and_balanced() -> None:
    clips = [_clip(f"spk{s}", f"Cümle {s}-{i}.") for s in range(12) for i in range(8)]
    clips += [_clip("kotu", "Reddedilen cümle.", down=2)]
    chosen = cv.select(clips, samples=40, per_speaker=3)
    assert len(chosen) == 36  # 12 konuşmacı × 3; reddedilen klip alınmaz
    per_speaker: dict[str, int] = {}
    splits: dict[str, set[str]] = {}
    for clip, split in chosen:
        per_speaker[clip.client_id] = per_speaker.get(clip.client_id, 0) + 1
        splits.setdefault(clip.client_id, set()).add(split)
    assert set(per_speaker.values()) == {3} and "kotu" not in per_speaker
    assert all(len(names) == 1 for names in splits.values())
    assert {name for names in splits.values() for name in names} == {"dev", "test"}
    # Ayrım kimlikten türetilir: aynı konuşmacı her çalıştırmada aynı tarafa düşer.
    assert cv.speaker_split("spk3") == cv.speaker_split("spk3")
    assert cv.speaker_key("spk3").startswith("cv_") and "spk3" not in cv.speaker_key("spk3")


def _pcm(seconds: float = 0.5) -> bytes:
    samples = np.sin(np.arange(int(16_000 * seconds)) / 5).astype(np.float32) * 0.3
    return eval_audio.to_pcm(samples)


def test_common_voice_build_writes_wavs_note_and_loadable_corpus(tmp_path: Path) -> None:
    clips = [(_clip("a", "Merhaba dünya."), "dev"), (_clip("b", "Bozuk."), "test"),
             (_clip("c", "İyi akşamlar."), "test")]

    def decode(data: bytes) -> bytes:
        if data == b"mp3" and decode.calls == 1:
            decode.calls += 1
            raise ValueError("bozuk")
        decode.calls += 1
        return _pcm()

    decode.calls = 0  # type: ignore[attr-defined]
    note = cv.build(clips, tmp_path / "cv", decode, sources=["tr/test #0"])
    assert note["items"] == 2 and note["skipped"] == 1 and note["license"].startswith("CC0")
    corpus, root = load_corpus(tmp_path / "cv" / "corpus.yaml")
    assert {item.source for item in corpus.items} == {"common_voice"}
    with wave.open(str(root / corpus.items[0].audio)) as audio:
        assert (audio.getframerate(), audio.getnchannels()) == (16_000, 1)
    assert json.loads((tmp_path / "cv" / "DATASET_NOTE.json").read_text("utf-8"))["speakers"] == 2


def test_common_voice_download_uses_cache_and_rejects_bad_shard(tmp_path: Path) -> None:
    import httpx

    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, content=b"parquet-bytes")

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        first = cv.download(3, tmp_path, client)
        second = cv.download(3, tmp_path, client)
        with pytest.raises(ValueError):
            cv.download(8, tmp_path, client)
    assert first == second and first.read_bytes() == b"parquet-bytes"
    assert len(calls) == 1 and "test-00003-of-00008.parquet" in calls[0]
    assert not list(tmp_path.glob("*.part"))


def test_common_voice_reads_real_parquet(tmp_path: Path) -> None:
    pyarrow = pytest.importorskip("pyarrow")
    parquet = pytest.importorskip("pyarrow.parquet")
    table = pyarrow.table({
        "client_id": ["x", "y"], "sentence": ["Bir.", "İki."], "down_votes": [0, 1],
        "audio": [{"bytes": b"a", "path": "a.mp3"}, {"bytes": b"b", "path": "b.mp3"}],
    })
    parquet.write_table(table, tmp_path / "s.parquet")
    clips = cv.read_shard(tmp_path / "s.parquet")
    assert [(c.client_id, c.sentence, c.audio, c.down_votes) for c in clips] == [
        ("x", "Bir.", b"a", 0), ("y", "İki.", b"b", 1),
    ]


def test_synthetic_templates_keep_dev_and_test_apps_disjoint(tmp_path: Path) -> None:
    templates = synthetic.load_templates()
    items = synthetic.utterances(templates)
    dev_apps = {a.name for a in templates.apps["dev"]}
    test_apps = {a.name for a in templates.apps["test"]}
    for item in items:
        apps = set(item.labels["expected_apps"])
        assert apps <= (dev_apps if item.split == "dev" else test_apps), item.id
        assert templates.wake_word in item.transcript or item.labels["wake"] is False
    correction = next(i for i in items if i.id == "test_correction_0")
    assert correction.labels["target_app"] == "Obsidian" and correction.labels["correction"]
    assert correction.transcript == "Artemis, Telegram değil, Obsidian uygulamasını aç."
    negation = next(i for i in items if i.id == "dev_negation_0")
    assert negation.labels["target_app"] is None and negation.labels["negation"]
    overlapping = tmp_path / "t.yaml"
    raw = yaml.safe_load(synthetic.TEMPLATES_PATH.read_text(encoding="utf-8"))
    raw["apps"]["test"].append(raw["apps"]["dev"][0])
    overlapping.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")
    with pytest.raises(ValueError, match="ayrık"):
        synthetic.load_templates(overlapping)
    assert synthetic.assign_splits(["b", "a", "c"]) == {"a": "dev", "b": "test", "c": "dev"}
    with pytest.raises(ValueError):
        synthetic.assign_splits(["tek"])


def test_synthetic_build_and_measure_end_to_end(tmp_path: Path) -> None:
    async def fake_tts(text: str) -> tuple[Any, int]:
        return eval_audio.to_float(_pcm(0.3)), 22_050

    voices = [
        synthetic.Voice("ses_a", "dev", fake_tts), synthetic.Voice("ses_b", "test", fake_tts),
    ]
    note = asyncio.run(synthetic.build(synthetic.load_templates(), voices, tmp_path / "syn"))
    assert note["items"] > 0 and not note["failures"]
    corpus, root = load_corpus(tmp_path / "syn" / "corpus.yaml")
    conditions = {item.condition for item in corpus.items}
    assert "clean" in conditions and len(conditions) > 2
    assert {item.speaker_id for item in corpus.items if item.split == "test"} == {"ses_b"}
    # Ses yalnız kendi ayrımının cümlelerini okur: test sesi dev uygulamalarını hiç görmez.
    assert all(f"{item.speaker_id}_{item.split}_" in item.id for item in corpus.items)

    class Echo:
        def __init__(self) -> None:
            self.items = iter(corpus.items)

        async def transcribe(self, _pcm: bytes) -> str:
            return next(self.items).transcript

    report = asyncio.run(measure(corpus, root, Echo(), mode="sahte", model_metadata={}))
    totals = report["totals"]
    assert totals["wer"] == 0 and totals["missed_wake_rate"] == 0
    # "artmış" eskiden referans metinde bile uyandırıyordu; eşleştirici düzeltildi (ADR 0045).
    assert any("near_wake_4" in item.id for item in corpus.items)
    assert totals["false_wake"] == 0 and totals["target_accuracy"] == 1.0
    assert totals["wrong_target"] == 0
    assert set(report["source_breakdown"]) == {"synthetic"}
    plan = report["plan_reports"]["synthetic"]
    assert plan["mode"] == "fake" and plan["metrics"]["intent_accuracy"] is None
    assert "synthetic" in plan["notes"][0]


def test_speaker_leak_between_dev_and_test_is_rejected(tmp_path: Path) -> None:
    corpus = {"items": [
        {"id": "a", "category": "c", "speaker_id": "s1", "split": "dev",
         "audio": "a.wav", "transcript": "bir"},
        {"id": "b", "category": "c", "speaker_id": "s1", "split": "test",
         "audio": "b.wav", "transcript": "iki"},
    ]}
    path = tmp_path / "corpus.yaml"
    path.write_text(yaml.safe_dump(corpus), encoding="utf-8")
    with pytest.raises(ValueError, match="sızıntı"):
        load_corpus(path)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Artemis, breyv uygulamasını aç.", "Brave"),
        ("Artemis Brave'i aç", "Brave"),
        ("Artemiz, Zoom değil, Obsidyan uygulamasını aç.", "Obsidian"),
        ("Artemis, yarın hava nasıl olacak?", None),
        ("Artemis, uygulamasını aç.", None),
    ],
)
def test_resolve_target_uses_the_spoken_name_resolver(text: str, expected: str | None) -> None:
    apps = ["Brave", "Zoom", "Obsidian", "Spotify"]
    assert resolve_target(text, apps, "Artemis") == expected


def test_wake_and_target_metrics_count_errors(tmp_path: Path) -> None:
    eval_audio.write_wav(tmp_path / "a.wav", eval_audio.to_float(_pcm(1.0)))
    rows = [
        # (wake etiketi, hedef, tahmin)
        (True, "Brave", "Artemis Spotify uygulamasını aç."),  # yanlış hedef
        (True, "Brave", "Bir şey aç."),  # uyandırma kaçtı, hedef yok
        (False, None, "Artemis fiyatları artırmış."),  # yanlış uyanma
        (False, None, "Fiyatlar yükselmiş."),
    ]
    corpus = load_corpus_from(tmp_path, [
        {"id": f"i{index}", "category": "c", "speaker_id": "s", "split": "test",
         "audio": "a.wav", "transcript": "x", "wake": wake, "target_app": target,
         "source": "synthetic"}
        for index, (wake, target, _) in enumerate(rows)
    ])

    class Fixed:
        def __init__(self) -> None:
            self.items = iter(prediction for *_, prediction in rows)

        async def transcribe(self, _pcm: bytes) -> str:
            return next(self.items)

    report = asyncio.run(measure(corpus, tmp_path, Fixed(), mode="sahte", model_metadata={}))
    totals = report["totals"]
    assert totals["missed_wake_rate"] == 0.5 and totals["false_wake"] == 1
    assert totals["target_accuracy"] == 0.0 and totals["wrong_target"] == 1
    assert totals["false_wake_per_hour"] == pytest.approx(1 / (2 / 3600))


def load_corpus_from(root: Path, items: list[dict[str, Any]]) -> Any:
    path = root / "corpus.yaml"
    path.write_text(
        yaml.safe_dump({"apps": ["Brave", "Spotify"], "items": items}, allow_unicode=True),
        encoding="utf-8",
    )
    return load_corpus(path)[0]


def test_xtts_requires_explicit_license_and_cloud_tts_requires_consent(tmp_path: Path) -> None:
    out = str(tmp_path / "x")
    with pytest.raises(SystemExit):
        synthetic.main(["--cikti", out, "--xtts", "Ana Florence"])
    with pytest.raises(SystemExit):
        synthetic.main(["--cikti", out, "--edge", "tr-TR-EmelNeural"])
    assert synthetic.voice_slug("Ana Florence") == "ana_florence"
    assert not (tmp_path / "x").exists()


def test_babble_mixes_pool_clips_and_requires_a_pool() -> None:
    pool = [eval_audio.pink_noise(8000, seed) for seed in range(4)]
    mixed = eval_audio.babble(16_000, pool, 3)
    assert len(mixed) == 16_000 and np.any(mixed)
    assert np.array_equal(mixed, eval_audio.babble(16_000, pool, 3))
    with pytest.raises(ValueError):
        eval_audio.babble(100, [], 0)


def test_noise_corpus_keeps_labels_and_needs_babble_source(tmp_path: Path) -> None:
    from bench import corpus_noise

    eval_audio.write_wav(tmp_path / "src" / "a.wav", eval_audio.to_float(_pcm(0.5)))
    items = [
        {"id": f"v_{kind}_clean", "category": "c", "speaker_id": "v", "split": "test",
         "audio": "a.wav", "transcript": "Artemis aç.", "source": "synthetic",
         "condition": condition, "wake": wake}
        for kind, wake, condition in (("w", True, "clean"), ("n", False, "clean"),
                                      ("x", True, "pink_snr5"), ("y", None, "clean"))
    ]
    (tmp_path / "src" / "corpus.yaml").write_text(
        yaml.safe_dump({"items": items}, allow_unicode=True), encoding="utf-8"
    )
    with pytest.raises(ValueError, match="babble"):
        corpus_noise.build(tmp_path / "src" / "corpus.yaml", tmp_path / "out",
                           babble_corpus=None, per_class=5)
    note = corpus_noise.build(
        tmp_path / "src" / "corpus.yaml", tmp_path / "out2", babble_corpus=None,
        per_class=5, conditions=("pink_snr10", "telephone"),
    )
    corpus, _ = load_corpus(tmp_path / "out2" / "corpus.yaml")
    # Yalnız temiz ve uyandırma etiketli örnekler, her koşulda bir kez.
    assert note["items"] == 4 and {i.condition for i in corpus.items} == {"pink_snr10", "telephone"}
    assert sorted(i.wake for i in corpus.items) == [False, False, True, True]
    assert all(i.source == "synthetic" and i.transcript == "Artemis aç." for i in corpus.items)


def test_denoised_recognizer_keeps_length_and_alignment() -> None:
    pytest.importorskip("onnxruntime")
    from bench.speech_accuracy import DenoisedRecognizer

    class Capture:
        pcm = b""

        async def transcribe(self, pcm: bytes) -> str:
            Capture.pcm = pcm
            return "tamam"

    pcm = _pcm(0.73)
    recognizer = DenoisedRecognizer(Capture(), "orta")
    assert asyncio.run(recognizer.transcribe(pcm)) == "tamam"
    assert len(Capture.pcm) == len(pcm) and Capture.pcm != pcm


def test_hypothesis_text_only_for_synthetic_items(tmp_path: Path) -> None:
    eval_audio.write_wav(tmp_path / "a.wav", eval_audio.to_float(_pcm(0.2)))
    corpus = load_corpus_from(tmp_path, [
        {"id": "s", "category": "c", "speaker_id": "a", "split": "test", "audio": "a.wav",
         "transcript": "x", "source": "synthetic"},
        {"id": "r", "category": "c", "speaker_id": "b", "split": "test", "audio": "a.wav",
         "transcript": "x", "source": "real"},
    ])

    class Same:
        async def transcribe(self, _pcm: bytes) -> str:
            return "gizli olabilir"

    report = asyncio.run(measure(corpus, tmp_path, Same(), mode="sahte", model_metadata={},
                                 include_synthetic_text=True))
    rows = {row["id"]: row for row in report["items"]}
    assert rows["s"]["hypothesis"] == "gizli olabilir" and "hypothesis" not in rows["r"]
    plain = asyncio.run(measure(corpus, tmp_path, Same(), mode="sahte", model_metadata={}))
    assert all("hypothesis" not in row for row in plain["items"])
