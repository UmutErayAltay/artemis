"""ADR 0043: akan yanıtın seslendirilmesi, iddia süzgeci ve tek iptal akışı."""

import asyncio
from time import monotonic

import pytest

from artemis.brain.agent_loop import AgentLoop
from artemis.brain.fake import ScriptedProvider
from artemis.brain.models import Completion, ToolCall
from artemis.channels.runner import CommandRunner
from artemis.channels.voice.core import AudioSegment, PCMChunk, VoiceController
from artemis.channels.voice.metrics import timing_report
from artemis.channels.voice.streaming import SpeechStream, claims_success
from artemis.config.settings import Settings, VoiceConfig
from artemis.safety.audit import AuditLog
from artemis.safety.gate import SafetyGate
from artemis.tools.base import ToolContext
from artemis.tools.registry import ToolRegistry
from bench.voice_latency import BenchClockTool
from tests.test_voice_core import FakeMic, FakeRecognizer, FakeVAD


def _drain(speech: SpeechStream) -> list[str]:
    items = []
    while not speech._queue.empty():
        item = speech._queue.get_nowait()
        if item is not None:
            items.append(item)
    return items


@pytest.mark.parametrize("sentence", [
    "Brave'i kapattım.", "Dosya silindi.", "Mesajı gönderdim.", "İşlem tamamlandı.",
    "Hepsini hallettik.", "I closed the window.", "Done.",
    "Kapattım, başka ne yapayım?", "Brave'i kapattım ve Chrome'u açıyorum.",
])
def test_success_claims_are_detected(sentence):
    assert claims_success(sentence)


@pytest.mark.parametrize("sentence", [
    "Bakıyorum.", "Hava bugün güneşli.", "Brave'i kapatıyorum.", "Saat 10.30.",
    "Uzun bir anlatım başlıyor.", "Yardım edebilirim.", "Adım adım anlatayım.",
])
def test_neutral_or_future_sentences_are_not_claims(sentence):
    assert not claims_success(sentence)


def test_sentences_flow_before_step_ends_and_numbers_are_not_split():
    speech = SpeechStream()
    speech.step_start()
    for piece in ["Sıcaklık 3.5 ", "derece. Yarın ", "yağmur var"]:
        speech.text(piece)
    assert _drain(speech) == ["Sıcaklık 3.5 derece."]
    speech.step_end(False)
    assert _drain(speech) == ["Yarın yağmur var"]


def test_claim_waits_for_step_end_and_keeps_order():
    speech = SpeechStream()
    speech.step_start()
    speech.text("Tamamlandı. Başka bir şey ister misin? ")
    # İddia bekletilir; arkasındaki cümle de sırayı bozmamak için bekler.
    assert _drain(speech) == []
    speech.step_end(False)
    assert _drain(speech) == ["Tamamlandı.", "Başka bir şey ister misin?"]


def test_claim_before_tool_call_is_dropped_and_rest_of_step_is_silent():
    speech = SpeechStream()
    speech.step_start()
    speech.text("Bakıyorum. Brave'i kapattım. ")
    speech.tool_call()
    # İddia içermeyen cümle: yalnız "araç başladı" kuralı onu susturmalı.
    speech.text("Bu da araçtan sonra gelen bir açıklama. ")
    speech.step_end(True)
    assert _drain(speech) == ["Bakıyorum."]
    speech.step_start()
    speech.text("Brave kapatıldı.")
    speech.step_end(False)
    speech.finish("Brave kapatıldı.")
    assert _drain(speech) == ["Brave kapatıldı."]


@pytest.mark.parametrize(("streamed", "final", "expected"), [
    ("Saat on.", "Saat on.", ["Saat on."]),  # akarken bir kez; sonda tekrar yok
    ("Uzun yanıt", "Uzun yanıt\n\n…(kesildi)", ["Uzun yanıt", "…(kesildi)"]),
    ("", "Model yanıtı akmadı.", ["Model yanıtı akmadı."]),
    ("Bir.\nİki.", "Bir.\n\nİki.", ["Bir.", "İki."]),  # yalnız boşluk farkı: tekrar yok
])
def test_finish_speaks_only_what_did_not_stream(streamed, final, expected):
    speech = SpeechStream()
    speech.step_start()
    if streamed:
        speech.text(streamed)
    speech.step_end(False)
    speech.finish(final)
    assert _drain(speech) == expected


def test_loop_message_after_tool_step_is_spoken_whole():
    speech = SpeechStream()
    speech.step_start()
    speech.text("Kontrol ediyorum. ")
    speech.step_end(True)
    speech.finish("Adım sınırı doldu; işlem durduruldu.")
    assert _drain(speech) == ["Kontrol ediyorum.", "Adım sınırı doldu; işlem durduruldu."]


def test_closed_stream_ignores_late_text():
    speech = SpeechStream()
    speech.step_start()
    speech.close()
    speech.text("Geç gelen. ")
    speech.step_end(False)
    assert _drain(speech) == []


class RecordingTTS:
    def __init__(self):
        self.sentences = []
        self.closed = False

    async def stream(self, text, cancelled):
        self.sentences.append(text)
        if not cancelled.is_set():
            yield PCMChunk(text.encode())

    async def close(self):
        self.closed = True


class RecordingSink:
    def __init__(self):
        self.pcm = []
        self.closed = False

    async def play(self, chunks, cancelled, on_first_pcm):
        async for chunk in chunks:
            if cancelled.is_set():
                return
            self.pcm.append(chunk.pcm.decode())
            on_first_pcm()

    async def close(self):
        self.closed = True


class GatedProvider:
    """İlk cümleyi akıtır, sonra test izin verene dek bekler: ilk ses model bitmeden çıkmalı."""

    def __init__(self, completions):
        self.completions = list(completions)
        self.release = asyncio.Event()
        self.cancelled = False

    async def complete(self, messages, tools, *, timeout, stream=None):
        completion = self.completions.pop(0)
        sentences = completion.content.split(" | ")
        try:
            if stream is not None:
                stream.text(sentences[0] + " ")
                await self.release.wait()
                for sentence in sentences[1:]:
                    stream.text(sentence + " ")
                if completion.tool_calls:
                    stream.tool_call()
            else:
                await self.release.wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        return completion.model_copy(update={"content": " ".join(sentences)})


def _controller(tmp_path, provider, texts, *, streaming=True):
    registry = ToolRegistry()
    registry.register(BenchClockTool)
    context = ToolContext(
        workspace=tmp_path, settings=Settings(), audit=AuditLog(tmp_path / "audit.jsonl"),
    )
    runner = CommandRunner(AgentLoop(provider, SafetyGate(registry), context, lambda *_: False))
    events = []
    tts, sink = RecordingTTS(), RecordingSink()
    controller = VoiceController(
        runner, VoiceConfig(), FakeMic(), FakeVAD([]), FakeRecognizer(texts), tts, sink,
        stream_responses=streaming, on_event=events.append,
    )
    return controller, tts, sink, events


async def _until(condition, timeout=1.0):
    async def wait():
        while not condition():
            await asyncio.sleep(0.001)
    await asyncio.wait_for(wait(), timeout)


def _segment():
    return AudioSegment(b"a", monotonic(), monotonic())


async def test_first_sentence_is_heard_before_the_model_finishes(tmp_path):
    provider = GatedProvider([Completion(content="Merhaba Umut. | Bugün hava güzel.")])
    controller, tts, sink, events = _controller(tmp_path, provider, ["artemis selam"])
    try:
        await controller._handle_utterance(_segment())
        await _until(lambda: sink.pcm)
        assert sink.pcm == ["Merhaba Umut."]
        assert not any(event.stage == "model_complete" for event in events)
        provider.release.set()
        await controller._settle_active()
    finally:
        await controller.close()
    # Akan cümle bir kez söylenir; tur sonunda tekrar edilmez.
    assert sink.pcm == ["Merhaba Umut.", "Bugün hava güzel."]
    report = timing_report(events)
    sample = report["utterances"][0]
    assert sample["streamed"] and not sample["invalid_timing"]
    assert sample["timings"]["first_pcm"] < sample["timings"]["model_complete"]
    assert report["model_first_token_ms"]["samples"] == 1


async def test_tool_turn_never_speaks_pre_tool_claim(tmp_path):
    provider = ScriptedProvider([
        Completion(content="Bakıyorum. Saat 10.00 oldu.",
                   tool_calls=[ToolCall(id="1", name="bench_clock")]),
        Completion(content="Saat 10.00."),
    ])
    controller, tts, sink, _events = _controller(tmp_path, provider, ["artemis saat kaç"])
    try:
        await controller._handle_utterance(_segment())
        await controller._settle_active()
    finally:
        await controller.close()
    assert sink.pcm == ["Bakıyorum.", "Saat 10.00."]
    assert not any("oldu" in sentence for sentence in tts.sentences)


async def test_barge_in_stops_provider_tts_and_sink_with_one_cancel(tmp_path):
    provider = GatedProvider([
        Completion(content="Uzun bir anlatım başlıyor. | Bu cümle hiç söylenmemeli."),
        Completion(content="Tamam, durdum."),
    ])
    controller, tts, sink, events = _controller(
        tmp_path, provider, ["artemis anlat", "artemis dur"],
    )
    try:
        await controller._handle_utterance(_segment())
        await _until(lambda: sink.pcm)
        await controller._handle_utterance(_segment())
        assert provider.cancelled
        provider.release.set()
        await _until(lambda: "Tamam, durdum." in sink.pcm)
        await controller._settle_active()
    finally:
        await controller.close()
    # Eski turun bekleyen cümlesi yeni tura sızmadı.
    assert "Bu cümle hiç söylenmemeli." not in sink.pcm
    assert sink.pcm == ["Uzun bir anlatım başlıyor.", "Tamam, durdum."]
    stops = [event.stage for event in events if event.utterance_id == 2
             and event.stage in {"stop_requested", "playback_stopped"}]
    assert stops == ["stop_requested", "playback_stopped"]


async def test_speech_while_streaming_model_thinks_is_not_barge_in(tmp_path):
    class SecondSilent(GatedProvider):
        async def complete(self, messages, tools, *, timeout, stream=None):
            if len(self.completions) == 1:
                await self.release.wait()  # ikinci tur: hiç metin akıtmadan düşünür
            completion = self.completions.pop(0)
            if stream is not None:
                stream.text(completion.content)
            return completion

    provider = SecondSilent([Completion(content="Merhaba."), Completion(content="Hazır.")])
    controller, _tts, sink, events = _controller(
        tmp_path, provider, ["artemis selam", "artemis düşün", "bekle"],
    )
    try:
        # Önceki tur konuştu: yankı penceresi ve "konuşurken wake iste" kuralı canlı.
        await controller._handle_utterance(_segment())
        await controller._settle_active()
        await controller._handle_utterance(_segment())
        await asyncio.sleep(0.01)
        active = controller._active
        start = monotonic()
        # barge_in_ms'i aşan gerçekçi söz: yalnız "hoparlör yazmıyor" kuralı kesmeyi önlemeli.
        await controller._handle_utterance(AudioSegment(b"a", start, start + 0.5))
        # Hoparlöre henüz yazılmadı: uyandırmasız söz turu iptal etmez (ADR 0042).
        assert active is not None and not active.done()
        provider.release.set()
        await controller._settle_active()
    finally:
        await controller.close()
    assert sink.pcm == ["Merhaba.", "Hazır."]
    assert not any(event.outcome == "interrupted" for event in events)


async def test_non_streaming_provider_still_speaks_full_answer_once(tmp_path):
    class Legacy:
        async def complete(self, messages, tools, *, timeout, stream=None):
            return Completion(content="Tam yanıt. İkinci cümle.")

    controller, _tts, sink, events = _controller(tmp_path, Legacy(), ["artemis selam"])
    try:
        await controller._handle_utterance(_segment())
        await controller._settle_active()
    finally:
        await controller.close()
    assert sink.pcm == ["Tam yanıt. İkinci cümle."]
    assert not timing_report(events)["utterances"][0]["streamed"]


async def test_streaming_disabled_keeps_old_path(tmp_path):
    provider = ScriptedProvider([Completion(content="Bir. İki.")])
    controller, _tts, sink, _ = _controller(tmp_path, provider, ["artemis selam"],
                                            streaming=False)
    try:
        await controller._handle_utterance(_segment())
        await controller._settle_active()
    finally:
        await controller.close()
    assert sink.pcm == ["Bir. İki."]
