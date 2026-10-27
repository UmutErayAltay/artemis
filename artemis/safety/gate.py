"""Her kanaldan gelen çağrı için tek onay ve denetim noktası."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from time import monotonic
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit
from uuid import uuid4

from pydantic import ValidationError

from artemis.tools.base import Risk, ToolContext, ToolResult, normalize_risk
from artemis.tools.registry import ToolRegistry

if TYPE_CHECKING:
    from artemis.brain.models import ToolCall

ApprovalCallback = Callable[[str, dict[str, Any]], bool]
TAINT_NOTICE = "Bu adım dış bir içerik okunduktan sonra istendi"
# Taint bu turda değil, bağlamdaki önceki bir turdan geliyorsa kullanıcıya bu söylenir.
HISTORY_TAINT_NOTICE = TAINT_NOTICE + " (önceki bir turda)"
# Gözetimsiz (proaktif) turda onay verecek kimse yoktur; varsayılan cevap hayırdır (ADR 0027).
UNATTENDED_NOTICE = "Gözetimsiz turda yan etkili işlem yapılmaz"
SITE_NOTICE = "Bu adım bir sitenin içeriğini okur"
_TRUSTED_STEP_ACTIONS = frozenset({"click", "read_text", "wait_for"})


def trusted_sites_path(state_dir: Path) -> Path:
    return state_dir / "trusted_sites.json"


def load_trusted_sites(path: Path) -> set[str]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set()
    return {str(host) for host in data} if isinstance(data, list) else set()


def remember_site(path: Path, host: str) -> None:
    """Kullanıcının "bu site için bir daha sorma" seçimini kalıcı yazar (ADR 0036)."""
    sites = load_trusted_sites(path) | {host}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(sorted(sites), ensure_ascii=False, indent=2), encoding="utf-8")


def site_of(external: bool, arguments: dict[str, Any]) -> str | None:
    """Dış içerik okuyan ve `url` alan araçta okunan sitenin host'u (ör. tarayıcı MCP)."""
    url = arguments.get("url")
    if not external or not isinstance(url, str):
        return None
    parts = urlsplit(url)
    if "\\" in url or "@" in parts.netloc or any(ord(ch) <= 0x20 or ord(ch) == 0x7F for ch in url):
        # Python ile tarayıcı (WHATWG) bu adresleri farklı host'a çözer:
        # `https://kotu.com\@iyi.com` Python'da `iyi.com`, Chromium'da `kotu.com`'dur. Ham netloc
        # döner: onayda gerçek belirsizlik görünür, güvenilen listedeki temiz adla asla eşleşmez.
        return parts.netloc.lower() or url
    host = (parts.hostname or "").rstrip(".").lower()
    return host or None


@dataclass
class TurnState:
    turn_id: str = field(default_factory=lambda: str(uuid4()))
    tainted: bool = False
    # Modele verilen geçmişte dış içerikli bir tur var (ADR 0013). `tainted` gibi tur
    # içinde değişmez; yalnız `outbound` araçlar için onay nedenidir.
    history_external: bool = False
    deadline: float | None = None
    # Turu kullanıcı başlatmadı (zamanlanmış brifing). READ_ONLY dışındaki her çağrı onay
    # sorulmadan reddedilir; kanal yalnız bu bayrağı verir, politika burada kalır (ADR 0027).
    unattended: bool = False


class SafetyGate:
    def __init__(self, registry: ToolRegistry) -> None:
        self.registry = registry
        self.pending_notice = ""
        # Onay penceresindeki "bu site için bir daha sorma" seçeneği için okunan host.
        self.pending_site = ""

    async def dispatch(
        self,
        call: ToolCall,
        context: ToolContext,
        state: TurnState,
        approve: ApprovalCallback,
    ) -> ToolResult:
        started = monotonic()
        context.turn_id = state.turn_id
        context.deadline = state.deadline
        # Kayıt bayrağı bağlamda kalan dış içeriği de kapsar (ADR 0013): önceki turda
        # okunan metinden kurulan hatırlatma da "dış içerikten" sayılır. Onay kuralı değişmez.
        context.turn_tainted = state.tainted or state.history_external
        self.pending_notice = ""
        self.pending_site = ""
        context.audit.emit(
            "tool_requested", turn_id=state.turn_id, tool=call.name, arguments=call.arguments
        )
        tool = self.registry.get(call.name)
        if tool is None:
            return self._finish(
                ToolResult(message=f"Kayıtlı olmayan araç çalıştırılmadı: {call.name}"),
                call,
                context,
                state,
                started,
            )
        if context.cancelled.is_set():
            return self._finish(
                ToolResult(message="İşlem iptal edildi."), call, context, state, started
            )
        if state.deadline is not None and monotonic() >= state.deadline:
            return self._finish(
                ToolResult(message="Süre sınırı dolduğu için işlem yapılmadı."),
                call,
                context,
                state,
                started,
            )
        try:
            arguments = tool.args_model.model_validate(deepcopy(call.arguments))
        except ValidationError:
            # Pydantic ham girdiyi hata metnine ekleyebilir; sırları taşımıyoruz.
            return self._finish(
                ToolResult(message="Araç argümanları şemaya uymuyor."),
                call,
                context,
                state,
                started,
            )
        risk = normalize_risk(tool.risk_for(arguments, context))
        normalized = arguments.model_dump(mode="json")
        if state.unattended and risk is not Risk.READ_ONLY:
            # approve çağrılmaz: karşıda kimse yokken açık kalan onay, saatler sonra eski
            # bağlamla yan etki üretebilirdi. Red yine onay olayı olarak denetime yazılır.
            self.pending_notice = f"{UNATTENDED_NOTICE}. {tool.description}"
            context.audit.emit(
                "approval",
                turn_id=state.turn_id,
                tool=call.name,
                arguments=normalized,
                approved=False,
                unattended=True,
                notice=self.pending_notice,
            )
            return self._finish(
                ToolResult(status="denied", message=f"{UNATTENDED_NOTICE}; işlem çalıştırılmadı."),
                call,
                context,
                state,
                started,
            )
        history_outbound = state.history_external and tool.outbound
        # Site okuma her zaman sorulur; kullanıcının güvendiği site hiç sorulmaz (ADR 0036).
        # Güven yalnız veri taşımayan adımları kapsar (izin listesi; `fill` dışarıda kalır).
        # Dış içerik bağlamdayken sorgu/parça taşıyan adres de sorulur: güvenilen sitedeki
        # açık yönlendirme (ör. google.com/url?q=...) veriyi başka siteye taşıyabilir.
        site = site_of(tool.external, normalized)
        steps = normalized.get("steps")
        fills = isinstance(steps, list) and not all(
            isinstance(step, dict) and step.get("action") in _TRUSTED_STEP_ACTIONS
            for step in steps
        )
        url = normalized.get("url")
        parts = urlsplit(url) if isinstance(url, str) else None
        carries_data = parts is not None and bool(parts.query or parts.fragment)
        trusted_site = (
            site is not None
            and risk is not Risk.CONFIRM_REQUIRED
            and not fills
            and not ((state.tainted or state.history_external) and carries_data)
            and site in load_trusted_sites(trusted_sites_path(context.settings.state_dir))
        )
        # Otomatik kipte yalnız `auto_mode_ok` aracın onayı atlanır (ADR 0039 ek karar):
        # ekran adımları her tur yeni dış içerik ürettiği için aksi hâlde kip hiç işe
        # yaramazdı. Diğer her araç kural 4'e aynen uyar.
        auto_skipped = (
            tool.auto_mode_ok
            and context.settings.computer_use.mode == "auto"
            and site is None
        )
        would_require_approval = not trusted_site and (
            site is not None
            or risk is Risk.CONFIRM_REQUIRED
            or (state.tainted and risk is not Risk.READ_ONLY)
            or (history_outbound and risk is not Risk.READ_ONLY)
        )
        requires_approval = not auto_skipped and would_require_approval
        approved: bool | None = None
        # Yalnız onay gerçekten istenecekken atlandıysa yazılır: aksi hâlde SIDE_EFFECT temiz
        # tur gibi hiç sorulmayacak çağrılar da "atlandı" diye denetimi kirletirdi.
        if auto_skipped and would_require_approval:
            # Onay sorulmadı; "evet" diye kaydetmek uydurma olurdu.
            context.audit.emit(
                "approval_skipped",
                turn_id=state.turn_id,
                tool=call.name,
                arguments=normalized,
                auto_mode=True,
                tainted=state.tainted,
                history_external=state.history_external,
            )
        if requires_approval:
            if site is not None and not fills and risk is not Risk.CONFIRM_REQUIRED:
                self.pending_site = site
            if state.tainted:
                notice = TAINT_NOTICE
            elif history_outbound:
                notice = HISTORY_TAINT_NOTICE
            elif site is not None:
                notice = f"{SITE_NOTICE} ({site})"
            else:
                notice = "Bu işlem onay gerektirir."
            # Onay ekranı tool açıklamasını da taşır; kullanıcı etkilenecek kapsamı
            # (ör. uygulamanın tüm süreçlerinin zorla kapatılmasını) görebilir.
            self.pending_notice = f"{notice}. {tool.description}"
            approved = approve(call.name, deepcopy(normalized)) is True
            context.audit.emit(
                "approval",
                turn_id=state.turn_id,
                tool=call.name,
                arguments=normalized,
                approved=approved,
                notice=self.pending_notice,
            )
            if not approved:
                return self._finish(
                    ToolResult(status="denied", message="Kullanıcı işlemi onaylamadı."),
                    call,
                    context,
                    state,
                    started,
                )
        # Onay sırasında iptal geldiyse araç çalıştırılmaz.
        if context.cancelled.is_set():
            return self._finish(
                ToolResult(message="İşlem iptal edildi."), call, context, state, started
            )
        if state.deadline is not None and monotonic() >= state.deadline:
            return self._finish(
                ToolResult(message="Süre sınırı dolduğu için işlem yapılmadı."),
                call,
                context,
                state,
                started,
            )
        context.audit.emit(
            "tool_dispatch",
            turn_id=state.turn_id,
            tool=call.name,
            arguments=normalized,
            risk=risk.value,
            confirmation_required=requires_approval,
            approved=approved,
            tainted=state.tainted,
            outbound=tool.outbound,
            history_external=state.history_external,
            trusted_site=trusted_site,
        )
        try:
            result = await tool.execute(arguments, context)
        except asyncio.CancelledError:
            context.audit.emit(
                "tool_interrupted",
                turn_id=state.turn_id,
                tool=call.name,
                completion="unknown",
                reason="Araç görevi iptal edildi; alt işlemin tamamlanması doğrulanamadı.",
            )
            self._finish(
                ToolResult(message="Araç iptal edildi; işlemin tamamlanması doğrulanamadı."),
                call,
                context,
                state,
                started,
            )
            raise
        except (OSError, ValueError, RuntimeError, TypeError) as error:
            context.audit.emit(
                "tool_exception",
                turn_id=state.turn_id,
                tool=call.name,
                error_type=type(error).__name__,
                reason="Araç yürütmesi başarısız oldu.",
            )
            result = ToolResult(message=f"Araç işlemi başarısız oldu ({type(error).__name__}).")
        if tool.external or result.external:
            state.tainted = True
            result = result.model_copy(update={"external": True})
        # Başarısız sonuç dosya içeriği taşımaz; etiketlenirse "kapalı/bulunamadı" gibi
        # hata mesajı da maskelenir ve model nedenini hiç göremez.
        if tool.content_kind != "text" and result.success:
            result = result.model_copy(update={"content_kind": tool.content_kind})
        return self._finish(result, call, context, state, started)

    @staticmethod
    def _finish(
        result: ToolResult,
        call: ToolCall,
        context: ToolContext,
        state: TurnState,
        started: float,
    ) -> ToolResult:
        context.audit.emit(
            "tool_result",
            turn_id=state.turn_id,
            tool=call.name,
            result=result.model_dump(mode="json"),
            tainted=state.tainted,
            elapsed_seconds=monotonic() - started,
        )
        return result
