"""Pet üretici resim modeli yolu (ADR 0048 eki): istek biçimi, ücret kuralları, dilimleyici,
ortak ölçek, satır yedeği ve arayüz akışı. Ağ yok: httpx MockTransport / sahte üreteç."""

from __future__ import annotations

import base64
import json
import os
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from pydantic import ValidationError
from PySide6.QtCore import QBuffer, QIODevice, Qt
from PySide6.QtGui import QColor, QImage, QPainter
from PySide6.QtWidgets import QApplication

from artemis.brain import images
from artemis.brain.errors import BudgetExceeded, ProviderError
from artemis.channels.desktop import pet_creator_panel as panel_mod
from artemis.channels.desktop import pet_image_pipeline as pipe
from artemis.channels.desktop import pet_sprite as ps
from artemis.channels.desktop.pet_section import PetSection
from artemis.config.settings import EndpointConfig, ModelConfig, Settings
from artemis.safety.audit import AuditLog

KEY = "sk-test-not-a-real-key"
BASE = "http://127.0.0.1:8317/v1"


@pytest.fixture
def qapp() -> QApplication:
    current = QApplication.instance()
    return current if isinstance(current, QApplication) else QApplication([])


def png(image: QImage) -> bytes:
    buffer = QBuffer()
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    image.save(buffer, "PNG")  # type: ignore[call-overload]
    return bytes(buffer.data().data())


def tiny_png() -> bytes:
    image = QImage(8, 8, QImage.Format.Format_ARGB32)
    image.fill(0x80FF0000)
    return png(image)


def image_model(**kw: Any) -> ModelConfig:
    fields: dict[str, Any] = {
        "provider": "openai_compatible", "endpoint": "proxy", "model": "gpt-image-2.5",
        "kind": "image", "input_per_million": Decimal(0), "output_per_million": Decimal(0),
    }
    return ModelConfig(**{**fields, **kw})


def make_settings(tmp_path: Path, model: ModelConfig | None = None, **kw: Any) -> Settings:
    chat = ModelConfig(provider="ollama", model="m", input_per_million=Decimal(0),
                       output_per_million=Decimal(0))
    return Settings(
        workspace=tmp_path, state_dir=tmp_path / "s", audit_path=tmp_path / "a.jsonl",
        endpoints={"proxy": EndpointConfig(base_url=BASE)},
        models={"sohbet": chat, "resim": model or image_model()}, primary_model="sohbet", **kw,
    )


class Recorder:
    """MockTransport işleyicisi: istekleri saklar, sabit yanıt döner."""

    def __init__(self, response: httpx.Response | None = None) -> None:
        self.requests: list[httpx.Request] = []
        self.bodies: list[bytes] = []
        self.response = response

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        self.bodies.append(request.read())
        return self.response or httpx.Response(
            200, json={"data": [{"b64_json": base64.b64encode(tiny_png()).decode()}]}
        )

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self)


@pytest.fixture(autouse=True)
def key_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARTEMIS_PROXY_API_KEY", KEY)


def audit_events(tmp_path: Path) -> list[dict[str, Any]]:
    path = tmp_path / "a.jsonl"
    lines = path.read_text("utf-8").splitlines() if path.exists() else []
    return [json.loads(line) for line in lines]


# ------------------------------------------------------------------ images.py


def test_generations_request_shape(tmp_path: Path) -> None:
    rec = Recorder()
    settings = make_settings(tmp_path)
    out = images.generate_image(settings, "resim", "bir kedi", size="1024x1024",
                                audit=AuditLog(settings.audit_path), transport=rec.transport)
    assert out == tiny_png()
    request = rec.requests[0]
    assert str(request.url) == f"{BASE}/images/generations"
    assert request.headers["authorization"] == f"Bearer {KEY}"
    assert json.loads(rec.bodies[0]) == {
        "model": "gpt-image-2.5", "prompt": "bir kedi", "size": "1024x1024",
        "background": "transparent", "n": 1,
    }


def test_edits_multipart_with_reference(tmp_path: Path) -> None:
    rec = Recorder()
    settings = make_settings(tmp_path)
    reference = tiny_png()
    images.generate_image(settings, "resim", "şapka ekle", reference=reference, size="1536x1024",
                          audit=AuditLog(settings.audit_path), transport=rec.transport)
    request, body = rec.requests[0], rec.bodies[0]
    assert str(request.url) == f"{BASE}/images/edits"
    assert request.headers["content-type"].startswith("multipart/form-data")
    assert b'name="image"' in body and reference in body
    for field in (b'name="model"', b'name="prompt"', b'name="size"', b'name="n"'):
        assert field in body
    assert b"1536x1024" in body and b"transparent" in body


def test_errors_never_carry_key(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    for response in (httpx.Response(500, text=f"boom {KEY}"), httpx.Response(401, text=KEY),
                     httpx.Response(200, text=f"not json {KEY}")):
        with pytest.raises(ProviderError) as info:
            images.generate_image(settings, "resim", "x", audit=AuditLog(settings.audit_path),
                                  transport=Recorder(response).transport)
        assert KEY not in str(info.value)
    assert KEY not in (tmp_path / "a.jsonl").read_text("utf-8")


def test_redirect_is_not_followed(tmp_path: Path) -> None:
    # Yönlendirme takip edilseydi Authorization başlığı başka bir sunucuya taşınabilirdi.
    settings = make_settings(tmp_path)
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if "evil" in str(request.url):
            body = base64.b64encode(tiny_png()).decode()
            return httpx.Response(200, json={"data": [{"b64_json": body}]})
        return httpx.Response(307, headers={"location": "https://evil.example/steal"})

    with pytest.raises(ProviderError):
        images.generate_image(settings, "resim", "x", audit=AuditLog(settings.audit_path),
                              transport=httpx.MockTransport(handler))
    assert not any("evil" in url for url in seen)


def test_audit_has_no_bytes_or_key(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    images.generate_image(settings, "resim", "x", reference=tiny_png(),
                          audit=AuditLog(settings.audit_path), transport=Recorder().transport)
    events = audit_events(tmp_path)
    transfer = next(e for e in events if e["event"] == "cloud_transfer")
    assert transfer["content_kind"] == "pet_image_prompt" and transfer["has_reference"] is True
    assert next(e for e in events if e["event"] == "provider_call")["success"] is True
    raw = (tmp_path / "a.jsonl").read_text("utf-8")
    assert KEY not in raw and base64.b64encode(tiny_png()).decode() not in raw


def test_paid_requires_allow_paid(tmp_path: Path) -> None:
    rec = Recorder()
    settings = make_settings(tmp_path, image_model(output_per_million=Decimal(1),
                                                   image_cost_usd=Decimal("0.2")))
    with pytest.raises(BudgetExceeded):
        images.generate_image(settings, "resim", "x", audit=AuditLog(settings.audit_path),
                              transport=rec.transport)
    assert rec.requests == []


def test_priced_without_image_cost_is_misconfigured(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, image_model(output_per_million=Decimal(1)),
                             allow_paid=True)
    with pytest.raises(ProviderError, match="image_cost_usd"):
        images.generate_image(settings, "resim", "x", audit=AuditLog(settings.audit_path),
                              transport=Recorder().transport)


def test_priced_reserves_and_settles(tmp_path: Path) -> None:
    from artemis.brain.budget import BudgetLedger

    settings = make_settings(tmp_path, image_model(output_per_million=Decimal(1),
                                                   image_cost_usd=Decimal("0.3")),
                             allow_paid=True)
    ledger = BudgetLedger(settings.state_dir / "budget.sqlite3", settings.daily_budget_usd)
    images.generate_image(settings, "resim", "x", audit=AuditLog(settings.audit_path),
                          transport=Recorder().transport)
    assert ledger.totals() == (Decimal("0.3"), Decimal(0))
    # hata: sunucu işlemiş olabilir, rezervasyon tutulur (harcanmış sayılmaz ama serbest de değil)
    with pytest.raises(ProviderError):
        images.generate_image(settings, "resim", "x", audit=AuditLog(settings.audit_path),
                              transport=Recorder(httpx.Response(500)).transport)
    assert ledger.totals()[1] == Decimal("0.3")
    # günlük tavan (1 USD): 0.3 + 0.3 + 0.3 doluyken dördüncü çağrı reddedilir
    with pytest.raises(ProviderError):
        images.generate_image(settings, "resim", "x", audit=AuditLog(settings.audit_path),
                              transport=Recorder(httpx.Response(500)).transport)
    with pytest.raises(BudgetExceeded):
        images.generate_image(settings, "resim", "x", audit=AuditLog(settings.audit_path),
                              transport=Recorder().transport)


def test_response_cap_and_bad_payloads(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    cases = {
        "büyük": httpx.Response(200, content=b"x" * (images.MAX_RESPONSE_BYTES + 1)),
        "url": httpx.Response(200, json={"data": [{"url": "https://evil.example/x.png"}]}),
        "resim değil": httpx.Response(
            200, json={"data": [{"b64_json": base64.b64encode(b"hello world!!").decode()}]}),
        "base64": httpx.Response(200, json={"data": [{"b64_json": "###"}]}),
        "boş": httpx.Response(200, json={"data": []}),
    }
    for name, response in cases.items():
        rec = Recorder(response)
        with pytest.raises(ProviderError):
            images.generate_image(settings, "resim", "x", audit=AuditLog(settings.audit_path),
                                  transport=rec.transport)
        assert len(rec.requests) == 1, name


def test_rejects_non_image_model(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    with pytest.raises(ProviderError, match="resim modeli"):
        images.generate_image(settings, "sohbet", "x", audit=AuditLog(settings.audit_path),
                              transport=Recorder().transport)


def test_image_model_cannot_be_chat_model(tmp_path: Path) -> None:
    base = make_settings(tmp_path)
    with pytest.raises(ValidationError, match="Resim modeli"):
        base.model_validate({**base.model_dump(), "primary_model": "resim"})
    with pytest.raises(ValidationError):
        image_model(provider="ollama", endpoint=None)


# ------------------------------------------------------------------ dilimleyici


def make_strip(frames: list[tuple[int, int, int]], *, size: tuple[int, int] = (1536, 1024),
               gap: int = 40, bg: QColor | None = None, color: int = 0xFF3050C0) -> QImage:
    """(genişlik, yükseklik, alt boşluk) listesinden şerit; kareler zemine yakın hizalıdır."""
    strip = QImage(*size, QImage.Format.Format_ARGB32)
    strip.fill(bg if bg is not None else QColor(0, 0, 0, 0))
    painter = QPainter(strip)
    x = gap
    for w, h, lift in frames:
        painter.fillRect(x, size[1] - 50 - lift - h, w, h, QColor.fromRgba(color))
        x += w + gap
    painter.end()
    return strip


def test_slice_by_gaps_and_trim(qapp: QApplication) -> None:
    frames = pipe.slice_strip(make_strip([(100, 200, 0)] * 6), 6)
    assert len(frames) == 6 and all(f is not None for f in frames)
    assert all((f.image.width(), f.image.height()) == (100, 200) for f in frames if f)


def test_slice_merges_tiny_fragments(qapp: QApplication) -> None:
    strip = make_strip([(100, 200, 0)] * 4)
    painter = QPainter(strip)
    painter.fillRect(40 + 100 + 15, 100, 4, 4, QColor(255, 0, 0))  # kareler arası kıvılcım
    painter.end()
    frames = pipe.slice_strip(strip, 4)
    assert len(frames) == 4 and all(f is not None for f in frames)


def test_slice_falls_back_to_equal_width(qapp: QApplication) -> None:
    strip = QImage(1200, 400, QImage.Format.Format_ARGB32)
    strip.fill(QColor(0, 0, 0, 0))
    painter = QPainter(strip)
    painter.fillRect(0, 100, 1200, 200, QColor(10, 200, 10))  # boşluksuz tek blok
    painter.end()
    frames = pipe.slice_strip(strip, 6)
    assert len(frames) == 6
    assert all(f is not None and f.image.width() == 200 for f in frames)


def test_slice_removes_opaque_background(qapp: QApplication) -> None:
    strip = make_strip([(100, 200, 0)] * 5, bg=QColor(250, 250, 250))
    clean = pipe.remove_background(strip)
    assert QColor.fromRgba(clean.pixel(2, 2)).alpha() == 0
    assert QColor.fromRgba(clean.pixel(90, 1024 - 50 - 100)).alpha() == 255
    assert len(pipe.slice_strip(strip, 5)) == 5
    assert all(f is not None for f in pipe.slice_strip(strip, 5))


# ------------------------------------------------------- ölçek, zemin, ayna, yedek


def solid_row(
    row: int, height: int, lift: tuple[int, ...] | None = None, width: int = 60
) -> list[pipe.Frame | None]:
    count = ps.FRAMES[row]
    frames: list[pipe.Frame | None] = []
    for i in range(count):
        image = QImage(width, height, QImage.Format.Format_ARGB32)
        image.fill(QColor(200, 30, 30, 255))
        frames.append(pipe.Frame(image, 500 - height - (lift[i] if lift else 0)))
    return frames


def bbox_of(sheet: QImage, row: int, col: int) -> tuple[int, int, int, int]:
    cell = sheet.copy(col * 192, row * 208, 192, 208)
    mask = bytes(cell.constBits())[3::4]
    ys = [y for y in range(208) if any(mask[y * 192:(y + 1) * 192])]
    xs = [x for x in range(192) if any(mask[y * 192 + x] for y in ys)]
    return xs[0], ys[0], xs[-1] + 1, ys[-1] + 1


def test_rows_normalised_to_base_size_and_baseline(qapp: QApplication) -> None:
    base = QImage(100, 100, QImage.Format.Format_ARGB32)
    base.fill(QColor(0, 0, 255))
    # model her şeritte farklı boyutta çizer: aynı karakter 400, 200 ve 100 px
    rows = {r: solid_row(r, h, width=h) for r, h in
            ((ps.IDLE, 400), (ps.WAVING, 200), (ps.REVIEW, 100))}
    sheet, fallback = pipe.assemble(rows, base)
    assert (sheet.width(), sheet.height()) == (1536, 1872)
    boxes = [bbox_of(sheet, r, 0) for r in (ps.IDLE, ps.WAVING, ps.REVIEW)]
    assert all(b[3] == pipe.BASELINE for b in boxes)  # ortak zemin
    heights = [b[3] - b[1] for b in boxes]
    assert max(heights) - min(heights) <= 2  # satırlar arası aynı boyut
    assert heights[0] == pytest.approx(pipe.FILL * 208, abs=5)  # temel ~%92 (genişlik sınırı)
    assert boxes[0][0] + boxes[0][2] == pytest.approx(192, abs=2)  # yatay ortalı
    assert sorted(fallback) == [ps.RUN_RIGHT, ps.RUN_LEFT, ps.JUMPING, ps.FAILED, ps.WAITING,
                                ps.RUNNING]


def test_jump_keeps_arc_and_fits_cell(qapp: QApplication) -> None:
    base = QImage(100, 100, QImage.Format.Format_ARGB32)
    base.fill(QColor(0, 0, 255))
    lift = (0, 100, 300, 100, 0)  # zıplama yayı çok yüksek: ölçek hücreye sığacak kadar kısılır
    frames = solid_row(ps.JUMPING, 200, lift=lift, width=200)
    scale = pipe.row_scale(frames, base)
    sheet, _ = pipe.assemble({ps.JUMPING: frames}, base)
    boxes = [bbox_of(sheet, ps.JUMPING, c) for c in range(5)]
    assert boxes[0][3] == boxes[4][3] == pipe.BASELINE
    assert boxes[2][1] >= 0  # tepe kare hücreden taşmaz
    assert boxes[0][3] - boxes[2][3] == pytest.approx(300 * scale, abs=2)  # yay oranı korunur
    assert scale * (500) <= pipe.BASELINE


def test_slice_touching_frames_cut_at_thinnest_column(qapp: QApplication) -> None:
    strip = QImage(1000, 300, QImage.Format.Format_ARGB32)
    strip.fill(QColor(0, 0, 0, 0))
    painter = QPainter(strip)
    for i in range(5):  # kareler bitişik (boşluk yok), aralarında yalnız ince köprü
        painter.fillRect(20 + i * 190, 50, 170, 200, QColor(200, 80, 20))
        if i:
            painter.fillRect(20 + i * 190 - 20, 240, 20, 4, QColor(200, 80, 20))
    painter.end()
    frames = pipe.slice_strip(strip, 5)
    assert all(f is not None for f in frames)
    # düz eşit bölme gövdeleri keserdi; köprüden kesilince gövde (170) hiçbir karede bölünmez
    assert all(170 <= f.image.width() <= 190 and f.image.height() == 200 for f in frames if f)


def test_clean_frame_drops_edge_sliver_keeps_prop(qapp: QApplication) -> None:
    image = QImage(300, 340, QImage.Format.Format_ARGB32)
    image.fill(QColor(0, 0, 0, 0))
    painter = QPainter(image)
    painter.fillRect(60, 40, 160, 220, QColor(200, 80, 20))  # gövde
    painter.fillRect(230, 120, 40, 40, QColor(250, 200, 0))  # yakın aksesuar (asa)
    painter.fillRect(293, 100, 7, 60, QColor(200, 80, 20))  # sağ kenarda komşu artığı
    painter.fillRect(0, 100, 6, 60, QColor(200, 80, 20))  # sol kenarda komşu artığı
    painter.fillRect(150, 320, 6, 6, QColor(200, 80, 20))  # gövdeden uzak kıvılcım
    painter.end()
    out = pipe.clean_frame(image)
    alpha = lambda x, y: QColor.fromRgba(out.pixel(x, y)).alpha()  # noqa: E731
    assert alpha(100, 100) == 255 and alpha(250, 140) == 255  # gövde ve asa kalır
    assert alpha(296, 130) == 0 and alpha(2, 130) == 0 and alpha(152, 322) == 0


def test_slice_strip_removes_neighbour_sliver(qapp: QApplication) -> None:
    strip = make_strip([(100, 200, 0)] * 4, gap=30)
    painter = QPainter(strip)
    # karenin yanında 6 px lik komşu kare artığı (kareye katılır, temizlenmeli)
    painter.fillRect(136, 1024 - 50 - 200, 6, 200, QColor(0xC0, 0x30, 0x50))
    painter.end()
    frames = pipe.slice_strip(strip, 4)
    assert all(f is not None for f in frames)
    assert [f.image.width() for f in frames if f] == [100, 100, 100, 100]


def test_running_left_is_mirror(qapp: QApplication) -> None:
    base = QImage(100, 100, QImage.Format.Format_ARGB32)
    base.fill(QColor(0, 0, 255))
    frames = solid_row(ps.RUN_RIGHT, 300)
    for i, frame in enumerate(frames):  # asimetrik kare: soldan ince
        assert frame is not None
        painter = QPainter(frame.image)
        painter.fillRect(0, 0, 10 + i, 20, QColor(0, 255, 0))
        painter.end()
    sheet, fallback = pipe.assemble({ps.RUN_RIGHT: frames}, base)
    assert ps.RUN_LEFT not in fallback or ps.RUN_RIGHT in fallback  # sağ üretildi: ayna yedek değil
    for col in range(8):
        right = sheet.copy(col * 192, ps.RUN_RIGHT * 208, 192, 208)
        left = sheet.copy(col * 192, ps.RUN_LEFT * 208, 192, 208)
        assert left == right.flipped(Qt.Orientation.Horizontal)
    assert ps.RUN_LEFT not in fallback


class FakeModel:
    """Şerit üreteci: istemdeki kare sayısına göre şerit; belirli satırı bozabilir."""

    def __init__(self, break_row: str | None = None, exc: Exception | None = None) -> None:
        self.calls: list[tuple[str, bytes | None, str]] = []
        self.break_row = break_row
        self.exc = exc

    def __call__(self, prompt: str, reference: bytes | None, size: str) -> bytes:
        self.calls.append((prompt, reference, size))
        if self.break_row and self.break_row in prompt:
            if self.exc:
                raise self.exc
            raise ProviderError("kötü yanıt")
        if size == pipe.BASE_SIZE:
            image = QImage(1024, 1024, QImage.Format.Format_ARGB32)
            image.fill(QColor(0, 0, 0, 0))
            painter = QPainter(image)
            painter.fillRect(300, 200, 400, 700, QColor(30, 90, 200))
            painter.end()
            return png(image)
        count = int(prompt.split("strip of ")[1].split()[0])
        return png(make_strip([(90, 220, 0)] * count))


def base_pair(model: FakeModel) -> tuple[QImage, bytes]:
    return pipe.make_base("mavi bir robot", model)


def test_make_base_and_edit_use_reference(qapp: QApplication) -> None:
    model = FakeModel()
    base, base_png = base_pair(model)
    assert (base.width(), base.height()) == (400, 700)  # alfa sınırına kırpıldı
    assert model.calls[0][1] is None and "mavi bir robot" in model.calls[0][0]
    model.calls.clear()
    # düzeltme: mevcut temel referans olarak gider (edits)
    edited, _ = pipe.edit_base("şapka ekle", base_png, model)
    assert model.calls[0][1] == base_png and "şapka ekle" in model.calls[0][0]
    assert edited.width() == 400
    # oran zorlanmaz (proxy size'ı yok sayabilir); çözülemeyen veri reddedilir
    assert not pipe.decode(png(make_strip([(90, 220, 0)])), pipe.BASE_SIZE).isNull()
    with pytest.raises(pipe.PetImageError):
        pipe.decode(b"\x89PNG\r\n\x1a\nbozuk", pipe.BASE_SIZE)


def test_animate_all_rows(qapp: QApplication) -> None:
    model = FakeModel()
    base, base_png = base_pair(model)
    model.calls.clear()
    seen: list[int] = []
    progress: list[tuple[int, int, int]] = []
    sheet, fallback = pipe.animate(base_png, base, model, progress=lambda *a: progress.append(a),
                                   row_ready=lambda row, cells: seen.append(row))
    assert fallback == [] and len(model.calls) == 8  # sola koşma için çağrı yok
    assert [p[0] for p in progress] == list(range(8)) and seen == [r for r, _ in pipe.ROWS]
    for _, reference, size in model.calls:
        assert reference == base_png and size == "1536x1024"
    counts = [int(c[0].split("strip of ")[1].split()[0]) for c in model.calls]
    assert counts == [6, 8, 4, 5, 8, 6, 6, 6]
    for row, count in enumerate(ps.FRAMES):
        for col in range(count):
            assert bbox_of(sheet, row, col)[3] == pipe.BASELINE


def test_row_retry_then_procedural_fallback(qapp: QApplication) -> None:
    model = FakeModel(break_row="crouch")  # zıplama satırı hep bozuk
    base, base_png = base_pair(model)
    model.calls.clear()
    sheet, fallback = pipe.animate(base_png, base, model)
    assert fallback == [ps.JUMPING]
    assert sum("crouch" in c[0] for c in model.calls) == 2  # bir kez yeniden denendi
    assert len(model.calls) == 7 + 2
    assert any(bbox_of(sheet, ps.JUMPING, c) for c in range(5))


def test_budget_error_aborts_instead_of_fallback(qapp: QApplication) -> None:
    model = FakeModel(break_row="waving", exc=BudgetExceeded("limit"))
    base, base_png = base_pair(model)
    with pytest.raises(BudgetExceeded):
        pipe.animate(base_png, base, model)


def test_cancel_between_calls(qapp: QApplication) -> None:
    model = FakeModel()
    base, base_png = base_pair(model)
    model.calls.clear()
    with pytest.raises(pipe.Cancelled):
        pipe.animate(base_png, base, model, cancelled=lambda: len(model.calls) >= 2)
    assert len(model.calls) == 2


# ------------------------------------------------------------------ arayüz


class UiFake:
    """`generate_image` yerine: pipeline FakeModel'ine yönlendirir ve çağrıları kaydeder."""

    def __init__(self) -> None:
        self.model = FakeModel()
        self.aliases: list[str] = []

    def __call__(self, settings: Settings, alias: str, prompt: str, *, reference: bytes | None,
                 size: str, audit: Any) -> bytes:
        self.aliases.append(alias)
        return self.model(prompt, reference, size)


@pytest.fixture
def section(qapp: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
            request: pytest.FixtureRequest) -> tuple[PetSection, UiFake, Settings]:
    settings = make_settings(tmp_path)
    fake = UiFake()
    monkeypatch.setattr(panel_mod, "load_settings", lambda path: settings)
    monkeypatch.setattr(panel_mod, "generate_image", fake)
    sec = PetSection(tmp_path / "pets", None, config_path=tmp_path / "c.yaml")
    request.addfinalizer(sec.deleteLater)
    request.addfinalizer(lambda: sec.creator and sec.creator.shutdown())
    return sec, fake, settings


def wait_image(creator: panel_mod.CreatorPanel, qapp: QApplication) -> None:
    for _ in range(1500):
        qapp.processEvents()
        if creator._image_call is None:
            return
        creator._image_call.wait(20)
    raise AssertionError("resim işi bitmedi")


def test_ui_image_flow(section: tuple[PetSection, UiFake, Settings], qapp: QApplication) -> None:
    sec, fake, _ = section
    creator = sec.creator
    assert creator is not None
    # SVG yolunda yalnız sohbet modeli, resim yolunda yalnız resim modeli listelenir
    def aliases() -> list[Any]:
        return [creator.model_combo.itemData(i) for i in range(creator.model_combo.count())]

    assert aliases() == ["sohbet"]
    creator.method.setCurrentIndex(1)
    assert aliases() == ["resim"]
    assert "abonelik kotasından" in creator.warning.text() and "4-6 dk" in creator.warning.text()
    assert not creator.animate_button.isEnabled()  # temel görsel yok
    chosen: list[str] = []
    sec.skin_selected.connect(chosen.append)
    creator.name_edit.setText("Mavi Robot")
    creator.input.setText("mavi bir robot")
    creator.send()
    assert not creator.send_button.isEnabled()
    wait_image(creator, qapp)
    assert creator._base is not None and creator.animate_button.isEnabled()
    assert creator._sheet is None and not creator.save_button.isEnabled()
    first_png = creator._base_png
    creator.input.setText("şapka ekle")  # düzeltme: önceki temel referans olarak gider
    creator.send()
    wait_image(creator, qapp)
    assert fake.model.calls[-1][1] == first_png and "Hata" not in creator.chat_log.toPlainText()
    fake.model.calls.clear()
    creator.animate()
    assert creator.cancel_button.isEnabled()
    wait_image(creator, qapp)
    assert len(fake.model.calls) == 8 and creator._sheet is not None
    assert creator.save_button.isEnabled() and creator.status.text() == ""
    creator.save()
    assert chosen == ["mavi-robot"]
    labels = [sec.listing.item(i).text() for i in range(sec.listing.count())]
    assert any("Mavi Robot" in t for t in labels)
    assert set(fake.aliases) == {"resim"}


def test_ui_progress_text_and_cancel(section: tuple[PetSection, UiFake, Settings],
                                     qapp: QApplication) -> None:
    sec, fake, _ = section
    creator = sec.creator
    assert creator is not None
    creator.method.setCurrentIndex(1)
    creator.input.setText("robot")
    creator.send()
    wait_image(creator, qapp)
    creator._on_progress(2, 8, ps.JUMPING)
    assert creator.busy.text() == "3/8: zıplama…"
    fake.model.calls.clear()
    original = fake.model

    class Cancelling(FakeModel):
        def __call__(self, prompt: str, reference: bytes | None, size: str) -> bytes:
            creator.cancel()  # ikinci çağrıdan sonra iptal
            return original(prompt, reference, size)

    fake.model = Cancelling()
    creator.animate()
    wait_image(creator, qapp)
    assert "İptal edildi" in creator.chat_log.toPlainText() and creator._sheet is None
    assert len(original.calls) == 1  # ilk çağrı bitti, ikincisi başlamadı


def test_ui_priced_confirm(qapp: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                           request: pytest.FixtureRequest) -> None:
    settings = make_settings(
        tmp_path, image_model(output_per_million=Decimal(1), image_cost_usd=Decimal("0.2")),
        allow_paid=True)
    fake = UiFake()
    monkeypatch.setattr(panel_mod, "load_settings", lambda path: settings)
    monkeypatch.setattr(panel_mod, "generate_image", fake)
    sec = PetSection(tmp_path / "pets", None, config_path=tmp_path / "c.yaml")
    request.addfinalizer(sec.deleteLater)
    creator = sec.creator
    assert creator is not None
    request.addfinalizer(creator.shutdown)
    creator.method.setCurrentIndex(1)
    assert "Ücretli" in creator.warning.text() and "1.6" in creator.warning.text()
    asked: list[str] = []
    monkeypatch.setattr(creator, "_confirm", lambda text: asked.append(text) or False)
    creator.input.setText("robot")
    creator.send()
    assert creator._image_call is None and fake.aliases == []  # reddedilince çağrı yok
    assert "0.2 USD" in asked[0] and creator.input.text() == "robot"
    monkeypatch.setattr(creator, "_confirm", lambda text: True)
    creator.send()
    wait_image(creator, qapp)
    assert fake.aliases == ["resim"] and creator._base is not None


def test_ui_demo_disabled(qapp: QApplication, tmp_path: Path) -> None:
    sec = PetSection(tmp_path / "p", None, demo=True, config_path=tmp_path / "c.yaml")
    creator = sec.creator
    assert creator is not None
    creator.method.setCurrentIndex(1)
    assert not creator.send_button.isEnabled() and not creator.animate_button.isEnabled()
    creator.shutdown()
    sec.deleteLater()
