"""Açık/yarım iş sorusu için Artemis'in kendi dosyaları + seçilmiş vault dosyaları (ADR 0054).

Kendi dosyaları Artemis'in belleğidir (dış içerik değil). Kullanıcının açıkça seçtiği
vault dosyaları ise dış içeriktir: biri okunduysa sonuç `external=True` olur ve kapı
turu `vault_read_note` ile aynı biçimde lekeler.
"""

import sqlite3
from datetime import datetime
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from artemis.memory.companion import (
    CompanionConflictError,
    content_private,
    content_tainted,
    list_changes,
    read_companion,
    undo_change,
    write_companion,
)
from artemis.safety.paths import safe_join
from artemis.tools.base import BaseTool, Risk, ToolContext, ToolResult
from artemis.tools.code_agent_jobs import FINISHED, CodeAgentJobs
from artemis.tools.registry import register_tool

_OWN_CHARS = 1000
_VAULT_CHARS = 1500


class OpenWorkArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")


@register_tool
class MemoryOpenWork(BaseTool):
    name = "memory_open_work"
    label = "Açık işlere bakma"
    done_label = "Açık işlere bakıldı"
    description = (
        "Kullanıcı açık/yarım işleri sorduğunda kullan: \"nerede kalmıştık\", \"açık işimiz "
        "var mı\". Artemis'in kendi açık iş ve son oturum notlarını, ayarlanmışsa seçili "
        "vault dosyalarını (örn. Threads, Last-Session) döndürür."
    )
    args_model = OpenWorkArguments
    risk = Risk.READ_ONLY

    async def execute(self, arguments: BaseModel, context: ToolContext) -> ToolResult:
        settings = context.settings
        sections: dict[str, str] = {}
        own_tainted = own_private = False
        for key in ("isler", "son_oturum"):
            text = read_companion(settings.state_dir, key, _OWN_CHARS).strip()
            if text:
                sections[key] = text
                own_tainted = own_tainted or content_tainted(settings.state_dir, key)
                own_private = own_private or content_private(settings.state_dir, key)
        vault = settings.memory.vault_path
        vault_read: list[str] = []
        if vault is not None and vault.is_dir():
            for rel in settings.memory.companion_vault_files:
                try:
                    # Açık kullanıcı seçimi exclude'u aşar ama yol kaçışı yine reddedilir.
                    path = safe_join(vault, rel)
                    if not path.is_file():
                        continue
                    with path.open("r", encoding="utf-8") as stream:
                        sections[rel] = stream.read(_VAULT_CHARS)
                except (OSError, ValueError, UnicodeError):
                    continue
                vault_read.append(rel)
        if not sections:
            return ToolResult(message="Okunabilir açık iş kaydı yok (dosyalar boş veya eksik).")
        # Lekeli turda yazılmış kendi dosyası da dış içerik gibi işlenir (kalıcı enjeksiyon yolu).
        external = bool(vault_read) or own_tainted
        return ToolResult(
            success=True,
            status="ok",
            message=f"{len(sections)} kaynak okundu.",
            data={"sources": list(sections), "vault_sources": vault_read, "content": sections},
            external=external,
            content_kind="file" if external else "text",
            private=own_private,
        )


# --- Faz B: yazma yolu. Her yazma günlüğe girer ve geri alınabilir (ADR 0054). ---

_RULES_HEADING = "## Kurallar"
_JOBS_HEADING = "## Tamamlanan kod işleri"
_KEEP_JOBS = 20


def _add_to_section(text: str, heading: str, line: str, keep: int | None = None) -> str:
    """`line`ı başlığın bölümünün sonuna ekler (başlık yoksa açar); `keep`: son N madde kalır."""
    lines = text.rstrip("\n").split("\n") if text.strip() else []
    if heading not in lines:
        lines += ["", heading]
    start = lines.index(heading) + 1
    end = next((i for i in range(start, len(lines)) if lines[i].startswith("## ")), len(lines))
    body = [item for item in lines[start:end] if item.strip()]
    body.append(line)
    if keep is not None:
        body = body[-keep:]
    tail = ["", *lines[end:]] if end < len(lines) else []
    return "\n".join([*lines[:start], *body, *tail]).rstrip("\n") + "\n"


def _section(text: str, heading: str) -> str:
    """Başlıkla birlikte bölüm metni; yoksa boş."""
    lines = text.split("\n")
    if heading not in lines:
        return ""
    start = lines.index(heading)
    end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("## ")), len(lines))
    return "\n".join(lines[start:end]).strip("\n")


def _fail(message: str) -> ToolResult:
    return ToolResult(message=message)


def _write(
    context: ToolContext, key: str, text: str, source: str, *, tainted: bool | None = None,
    private: bool | None = None,
) -> ToolResult | int:
    """Yazar; hata varsa dürüst ToolResult, başarıda değişiklik kimliği döner.

    Gizlilik kökeni lekeden ayrı saklanır (ADR 0056): yerel hafızaya yazma onayı buluta
    paylaşım izni değildir. Gizli turda yazılan metin sonraki turlarda buluta maskeli gider.
    """
    try:
        return write_companion(
            context.settings.state_dir, key, text, source=source, turn_id=context.turn_id,
            tainted=context.turn_tainted if tainted is None else tainted,
            private=context.turn_private if private is None else private,
        )
    except ValueError as error:
        return _fail(f"Yazılmadı: {error}")
    except (OSError, sqlite3.Error) as error:
        return _fail(f"Hafıza dosyası yazılamadı ({type(error).__name__}).")


class SessionUpdateArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")
    son_oturum: str | None = Field(default=None, max_length=3000)
    isler: str | None = Field(default=None, max_length=6000)

    @model_validator(mode="after")
    def need_one(self) -> Self:
        if self.son_oturum is None and self.isler is None:
            raise ValueError("son_oturum veya isler verilmeli.")
        return self


@register_tool
class MemorySessionUpdate(BaseTool):
    name = "memory_session_update"
    label = "Oturum notunu güncelleme"
    done_label = "Oturum notu güncellendi"
    description = (
        "Anlamlı bir iş bitince veya kullanıcı neyin kaldığını söyleyince çağır; her yanıtta "
        "değil. son_oturum: bu oturumun 3-6 kısa satırlık özeti. isler: AÇIK İŞLERİN TAM "
        "güncel listesi (kapatmadığın eski maddeleri koru)."
    )
    args_model = SessionUpdateArguments
    risk = Risk.SIDE_EFFECT

    async def execute(self, arguments: BaseModel, context: ToolContext) -> ToolResult:
        args = SessionUpdateArguments.model_validate(arguments)
        ids: list[int] = []
        for key, text in (("son_oturum", args.son_oturum), ("isler", args.isler)):
            if text is None:
                continue
            text = text.rstrip("\n") + "\n"
            if key == "isler":
                # Makbuz bölümü model metninden değil iş kaydından gelir; ezilmesin.
                old = _section(
                    read_companion(context.settings.state_dir, "isler", 10**6), _JOBS_HEADING
                )
                if old and _JOBS_HEADING not in text:
                    text += "\n" + old + "\n"
            outcome = _write(context, key, text, "session_update")
            if isinstance(outcome, ToolResult):
                return outcome
            ids.append(outcome)
        return ToolResult(
            success=True, status="ok", message="Oturum notu güncellendi.", data={"changes": ids}
        )


class RuleAddArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=1, max_length=300)


@register_tool
class MemoryRuleAdd(BaseTool):
    name = "memory_rule_add"
    label = "Kural ekleme"
    done_label = "Kural eklendi"
    description = (
        "Kullanıcı seni düzelttiğinde veya \"bundan sonra şöyle yap\" dediğinde kalıcı kural "
        "ekler (her zaman onay ister). Kullanıcı hakkındaki bilgi/tercih için memory_remember."
    )
    args_model = RuleAddArguments
    risk = Risk.CONFIRM_REQUIRED

    async def execute(self, arguments: BaseModel, context: ToolContext) -> ToolResult:
        args = RuleAddArguments.model_validate(arguments)
        # Yalnız bu turda okunan dış içerik engeller. Önceki turdan kalan dış içerik engellemez:
        # kural metni onay penceresinde harfiyen gösterilir ve kullanıcı onaylar; aksi halde
        # oturumda bir kez vault okununca kural hiç eklenemiyordu (2026-11-07 gerçek deneme).
        if context.turn_read_external:
            return _fail(
                "Kural eklenmedi: bu turda dış içerik okundu; kurallar yalnız temiz turda eklenir."
            )
        rule = " ".join(args.text.split())
        current = read_companion(context.settings.state_dir, "kimlik", 10**6)
        # Onaylı tam metin kullanıcınındır: geçmişteki dış içerik yüzünden Kimlik lekeli
        # işaretlenmez (yoksa yapışkan leke sonraki her turu dış içerikli yapardı).
        # Gizlilik ise düşürülmez (ADR 0056): onay yerel yazmanın onayıdır, bulut izni değildir.
        outcome = _write(
            context, "kimlik", _add_to_section(current, _RULES_HEADING, f"- {rule}"), "rule_add",
            tainted=False,
        )
        if isinstance(outcome, ToolResult):
            return outcome
        return ToolResult(
            success=True, status="ok", message="Kural eklendi.", data={"change_id": outcome}
        )


class ChangesArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")
    limit: int = Field(default=10, ge=1, le=50)


def _preview(text: str) -> str:
    return text if len(text) <= 160 else text[:160] + "…"


@register_tool
class MemoryChanges(BaseTool):
    name = "memory_changes"
    label = "Hafıza değişiklikleri"
    done_label = "Hafıza değişiklikleri okundu"
    description = "Artemis'in kendi hafıza dosyalarında yaptığı son değişiklikleri listeler."
    args_model = ChangesArguments
    risk = Risk.READ_ONLY

    async def execute(self, arguments: BaseModel, context: ToolContext) -> ToolResult:
        args = ChangesArguments.model_validate(arguments)
        try:
            rows = list_changes(context.settings.state_dir, args.limit)
        except (OSError, sqlite3.Error) as error:
            return _fail(f"Değişiklik günlüğü okunamadı ({type(error).__name__}).")
        items = [
            {
                "id": row["id"], "at": row["at"], "file": row["file"], "source": row["source"],
                "before": _preview(row["before"]), "after": _preview(row["after"]),
                "tainted": row["tainted"] or row["before_tainted"],
            }
            for row in rows
        ]
        # Panel geçmişi değil şu anki kural listesini gösterir: kural çoğalınca hangisinin
        # yürürlükte olduğu adım adım değişikliklerden okunmasın (Umut, 2026-11-07).
        state_dir = context.settings.state_dir
        rules = _rules(read_companion(state_dir, "kimlik", 10**6))
        # Arşivdeki her anlık görüntü kendi kökenini taşır (ADR 0056): güncel dosya geri
        # alınıp temizlenmiş olsa da dönen `before`/`after` metni dış ya da gizli olabilir.
        # Sonuç dış içerikse kapı turu lekeler; gizliyse buluta maskeli gider.
        external = any(row["tainted"] or row["before_tainted"] for row in rows) or (
            bool(rules) and content_tainted(state_dir, "kimlik")
        )
        private = any(row["private"] or row["before_private"] for row in rows) or (
            bool(rules) and content_private(state_dir, "kimlik")
        )
        return ToolResult(
            success=True, status="ok",
            message=f"{len(rules)} kural, {len(items)} değişiklik.",
            data={"changes": items, "rules": rules},
            external=external,
            content_kind="file" if external else "text",
            private=private,
        )


def _rules(kimlik: str) -> list[str]:
    body = _section(kimlik, _RULES_HEADING).split("\n")[1:]
    return [line[2:] for line in body if line.startswith("- ")]


class RuleRemoveArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=1, max_length=300)


@register_tool
class MemoryRuleRemove(BaseTool):
    name = "memory_rule_remove"
    label = "Kural kaldırma"
    done_label = "Kural kaldırıldı"
    description = (
        "Kimlik'teki bir kuralı tam metniyle kaldırır (kullanıcı isterse; her zaman onay ister)."
    )
    args_model = RuleRemoveArguments
    risk = Risk.CONFIRM_REQUIRED

    async def execute(self, arguments: BaseModel, context: ToolContext) -> ToolResult:
        args = RuleRemoveArguments.model_validate(arguments)
        current = read_companion(context.settings.state_dir, "kimlik", 10**6)
        section = _section(current, _RULES_HEADING)
        target = f"- {args.text}"
        if target not in section.split("\n")[1:]:
            return _fail("Bu kural Kimlik'te yok; liste yenilenip tekrar denenmeli.")
        lines = section.split("\n")
        lines.remove(target)  # aynı metin iki kez varsa yalnız ilki
        # Silme yeni metin eklemez: yapışkan leke/gizlilik dışında Kimlik'i lekelemez.
        outcome = _write(
            context, "kimlik", current.replace(section, "\n".join(lines), 1), "rule_remove",
            tainted=False, private=False,
        )
        if isinstance(outcome, ToolResult):
            return outcome
        return ToolResult(
            success=True, status="ok", message="Kural kaldırıldı.", data={"change_id": outcome}
        )


class UndoArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")
    change_id: int = Field(ge=1)


@register_tool
class MemoryChangeUndo(BaseTool):
    name = "memory_change_undo"
    label = "Hafıza değişikliğini geri alma"
    done_label = "Hafıza değişikliği geri alındı"
    description = (
        "memory_changes'teki bir değişikliği geri alır (kullanıcı isterse). Dosya o değişiklikten "
        "sonra yeniden değiştiyse reddeder."
    )
    args_model = UndoArguments
    # Kalıcı kuralı/kartı geri sarar; model tek başına yapamasın (panelde de onay sorulur).
    risk = Risk.CONFIRM_REQUIRED

    async def execute(self, arguments: BaseModel, context: ToolContext) -> ToolResult:
        args = UndoArguments.model_validate(arguments)
        try:
            new_id = undo_change(
                context.settings.state_dir, args.change_id,
                turn_id=context.turn_id, tainted=context.turn_tainted,
                private=context.turn_private,
            )
        except (LookupError, CompanionConflictError) as error:
            return _fail(str(error))
        except (OSError, sqlite3.Error) as error:
            return _fail(f"Geri alınamadı ({type(error).__name__}).")
        return ToolResult(
            success=True, status="ok", message="Değişiklik geri alındı.",
            data={"change_id": new_id},
        )


class JobReceiptArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")
    job_id: str = Field(min_length=1, max_length=32)


@register_tool
class MemoryJobReceipt(BaseTool):
    name = "memory_job_receipt"
    label = "Kod işi makbuzu"
    done_label = "Kod işi makbuzu yazıldı"
    description = (
        "Kullanıcı bitmiş bir kod işinin makbuzunu isteyince çağır (\"bu işin makbuzunu yaz\"). "
        "Metin yalnız iş kaydından üretilir; job_id code_agent_status'tan alınır."
    )
    args_model = JobReceiptArguments
    risk = Risk.CONFIRM_REQUIRED

    async def execute(self, arguments: BaseModel, context: ToolContext) -> ToolResult:
        args = JobReceiptArguments.model_validate(arguments)
        jobs = context.services.get("code_agent_jobs")
        if not isinstance(jobs, CodeAgentJobs):
            return _fail("Kod ajanı iş deposu kullanılamıyor.")
        job = next((item for item in jobs.status() if item["job_id"] == args.job_id), None)
        if job is None:
            return _fail(f"{args.job_id} kimlikli iş bulunamadı.")
        if job["status"] not in FINISHED:
            return _fail(f"İş henüz bitmedi (durum: {job['status']}); makbuz yazılmadı.")
        day = datetime.fromtimestamp(float(job["created_at"])).strftime("%Y-%m-%d")
        summary = " ".join(str(job.get("summary") or "").split())[:160] or "özet yok"
        # Commit satırı "<sha> <konu>" olabilir; makbuza yalnız kısa kimlik girer.
        commits = [str(item) for item in job.get("commits") or [] if str(item).strip()]
        shas = ", ".join(item.split()[0][:10] for item in commits) or "yok"
        line = f"- {day} {job['project']}: {job['status']}, {summary}, commit {shas}"
        current = read_companion(context.settings.state_dir, "isler", 10**6)
        if line in current.split("\n"):
            return ToolResult(success=True, status="ok", message="Bu makbuz zaten kayıtlı.")
        outcome = _write(
            context, "isler", _add_to_section(current, _JOBS_HEADING, line, _KEEP_JOBS),
            "job_receipt",
        )
        if isinstance(outcome, ToolResult):
            return outcome
        return ToolResult(
            success=True, status="ok", message="Makbuz Isler.md'ye yazıldı.",
            data={"change_id": outcome, "line": line},
        )
