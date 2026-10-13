"""Çalışma köküyle sınırlı dosya işlemleri, sonuçları diskten doğrular."""

from pathlib import Path
from typing import Literal, cast

from pydantic import BaseModel, ConfigDict, Field

from jarvis.safety.paths import safe_join
from jarvis.safety.undo import UndoJournal
from jarvis.tools.base import BaseTool, Risk, ToolContext, ToolResult
from jarvis.tools.registry import register_tool


class Arguments(BaseModel):
    """Fazladan argümanları reddetmek, model hatalarını görünür kılar."""

    model_config = ConfigDict(extra="forbid")


class ListArguments(Arguments):
    path: str | None = None


class PathArguments(Arguments):
    path: str = Field(min_length=1)


class CreateArguments(PathArguments):
    content: str = Field(max_length=1_000_000)


class MoveArguments(Arguments):
    source: str = Field(min_length=1)
    destination: str = Field(min_length=1)


class RenameArguments(PathArguments):
    new_name: str = Field(min_length=1)


def failure(error: OSError | ValueError | UnicodeError) -> ToolResult:
    """Ham istisna metnindeki kullanıcı yolu veya sırları dışarı çıkarmaz."""
    if isinstance(error, FileNotFoundError):
        detail = "hedef bulunamadı."
    elif isinstance(error, PermissionError):
        detail = "erişim reddedildi."
    elif isinstance(error, UnicodeError):
        detail = "dosya geçerli UTF-8 metni değil."
    elif isinstance(error, ValueError):
        detail = "yol veya işlem geçersiz."
    else:
        detail = "işletim sistemi işlemi başarısız oldu."
    return ToolResult(status="error", message=f"Dosya işlemi başarısız: {detail}")


def move_verified(source: Path, destination: Path) -> ToolResult:
    """Hedefin üstüne yazmaz; taşıma sonrası iki konumu da denetler."""
    if not source.is_file():
        raise ValueError("Kaynak normal bir dosya olmalı.")
    if destination.exists() or destination.is_symlink():
        raise ValueError("Hedef zaten var; üzerine yazılmadı.")
    source.rename(destination)
    if source.exists() or not destination.is_file():
        raise OSError("Taşıma sonucu doğrulanamadı.")
    return ToolResult(success=True, status="ok", message="Dosya taşındı.")


@register_tool
class ListFiles(BaseTool):
    name = "list_files"
    label = "Dosyaları listeleme"
    done_label = "Dosyalar listelendi"
    description = "Çalışma kökündeki veya göreli klasördeki dosyaları boyutlarıyla listeler."
    args_model = ListArguments
    risk = Risk.READ_ONLY
    external = True

    async def execute(self, arguments: BaseModel, context: ToolContext) -> ToolResult:
        args = cast(ListArguments, arguments)
        try:
            directory = (
                context.workspace.resolve()
                if args.path is None
                else safe_join(context.workspace, args.path)
            )
            entries = []
            for item in sorted(directory.iterdir(), key=lambda path: path.name.casefold()):
                relative = item.relative_to(context.workspace.resolve()).as_posix()
                # Bağlantı hedefini de denetlemek kök dışı metadata sızıntısını önler.
                checked = safe_join(context.workspace, relative)
                info = checked.stat()
                entries.append(
                    {
                        "name": item.name,
                        "path": relative,
                        "size": info.st_size,
                        "is_dir": checked.is_dir(),
                    }
                )
            return ToolResult(
                success=True,
                status="ok",
                message=f"{len(entries)} öğe listelendi.",
                data={"entries": entries},
                external=True,
            )
        except (OSError, ValueError) as error:
            return failure(error)


@register_tool
class ReadFile(BaseTool):
    name = "read_file"
    label = "Dosya okuma"
    done_label = "Dosya okundu"
    description = "Göreli UTF-8 metin dosyasını okur; büyük içerik sınırlandırılır."
    args_model = PathArguments
    risk = Risk.READ_ONLY
    external = True
    content_kind = "file"

    async def execute(self, arguments: BaseModel, context: ToolContext) -> ToolResult:
        args = cast(PathArguments, arguments)
        try:
            path = safe_join(context.workspace, args.path)
            if not path.is_file():
                raise FileNotFoundError("Yalnız normal dosyalar okunabilir.")
            with path.open("r", encoding="utf-8") as stream:
                content = stream.read(262_145)
            truncated = len(content) > 262_144
            return ToolResult(
                success=True,
                status="ok",
                message="Dosya okundu.",
                data={"content": content[:262_144], "truncated": truncated},
                external=True,
                content_kind="file",
            )
        except (OSError, ValueError, UnicodeError) as error:
            return failure(error)


@register_tool
class CreateFile(BaseTool):
    name = "create_file"
    label = "Dosya oluşturma"
    done_label = "Dosya oluşturuldu"
    description = "Göreli konumda yeni UTF-8 dosya oluşturur; mevcut dosyaya yazmaz."
    args_model = CreateArguments
    risk = Risk.SIDE_EFFECT

    async def execute(self, arguments: BaseModel, context: ToolContext) -> ToolResult:
        args = cast(CreateArguments, arguments)
        try:
            path = safe_join(context.workspace, args.path, allow_symlinks=False)
            with path.open("x", encoding="utf-8", newline="") as stream:
                stream.write(args.content)
            if path.read_bytes() != args.content.encode("utf-8"):
                raise OSError("Yazılan içerik doğrulanamadı.")
            return ToolResult(success=True, status="ok", message="Dosya oluşturuldu.")
        except (OSError, ValueError, UnicodeError) as error:
            return failure(error)


@register_tool
class MoveFile(BaseTool):
    name = "move_file"
    label = "Dosya taşıma"
    done_label = "Dosya taşındı"
    description = "Dosyayı çalışma kökü içinde taşır; hedef zaten varsa reddeder."
    args_model = MoveArguments
    risk = Risk.SIDE_EFFECT

    async def execute(self, arguments: BaseModel, context: ToolContext) -> ToolResult:
        args = cast(MoveArguments, arguments)
        try:
            return move_verified(
                safe_join(context.workspace, args.source, allow_symlinks=False),
                safe_join(context.workspace, args.destination, allow_symlinks=False),
            )
        except (OSError, ValueError) as error:
            return failure(error)


@register_tool
class RenameFile(BaseTool):
    name = "rename_file"
    label = "Dosyayı yeniden adlandırma"
    done_label = "Dosya yeniden adlandırıldı"
    description = "Dosyanın aynı klasördeki adını değiştirir; new_name yalnız dosya adıdır."
    args_model = RenameArguments
    risk = Risk.SIDE_EFFECT

    async def execute(self, arguments: BaseModel, context: ToolContext) -> ToolResult:
        args = cast(RenameArguments, arguments)
        try:
            source = safe_join(context.workspace, args.path, allow_symlinks=False)
            # Her iki ayırıcı Linux testlerinde de Windows kurallarını korur.
            if "/" in args.new_name or "\\" in args.new_name:
                raise ValueError("Yeni ad tek bir dosya adı olmalı.")
            destination = safe_join(source.parent, args.new_name, allow_symlinks=False)
            return move_verified(source, destination)
        except (OSError, ValueError) as error:
            return failure(error)


@register_tool
class DeleteFile(BaseTool):
    name = "delete_file"
    label = "Dosya silme"
    done_label = "Dosya çöp kutusuna taşındı"
    description = "Dosyayı işletim sisteminin geri dönüşüm kutusuna taşır; kalıcı silmez."
    args_model = PathArguments
    risk = Risk.CONFIRM_REQUIRED

    async def execute(self, arguments: BaseModel, context: ToolContext) -> ToolResult:
        args = cast(PathArguments, arguments)
        try:
            path = safe_join(context.workspace, args.path, allow_symlinks=False)
            if not path.is_file():
                raise ValueError("Silinecek hedef normal bir dosya olmalı.")
            root = context.workspace.resolve()
            journal = context.services.get("undo_journal")
            if journal is None:
                journal = UndoJournal(context.settings.state_dir)
            record = journal.prepare(
                turn_id=context.turn_id,
                root=root,
                relative_path=path.relative_to(root).as_posix(),
                size=path.stat().st_size,
            )
            trash = context.services.get("trash")
            try:
                if trash is None:
                    # Test adaptörü varsa OS çöp kutusu bağımlılığı yüklenmez.
                    from send2trash import send2trash

                    send2trash(str(path))
                else:
                    trash(path)
            except (OSError, ValueError):
                try:
                    removed = not path.exists()
                    terminal_status: Literal["uncertain", "failed"] = (
                        "uncertain" if removed else "failed"
                    )
                    journal.set_status(record.record_id, terminal_status)
                except (OSError, ValueError):
                    return ToolResult(
                        status="error",
                        message="Dosya işlemi sonuçlandı; günlük kaydı hazırlanmış durumda kaldı.",
                        data={"undo_record_id": record.record_id, "undo_status": "prepared"},
                    )
                if removed:
                    return ToolResult(
                        status="error",
                        message="Dosya taşınmış olabilir; işlem sonucu belirsiz.",
                        data={"undo_record_id": record.record_id, "undo_status": "uncertain"},
                    )
                result = failure(OSError("Dosya geri dönüşüm kutusuna taşınamadı."))
                result.data.update(
                    {"undo_record_id": record.record_id, "undo_status": "failed"}
                )
                return result
            if path.exists():
                try:
                    journal.set_status(record.record_id, "failed")
                except (OSError, ValueError):
                    return ToolResult(
                        status="error",
                        message="Dosya taşınmadı; geri alma kaydı hazırlanmış durumda kaldı.",
                        data={"undo_record_id": record.record_id, "undo_status": "prepared"},
                    )
                return ToolResult(
                    status="error",
                    message="Dosya geri dönüşüm kutusuna taşınmadı.",
                    data={"undo_record_id": record.record_id, "undo_status": "failed"},
                )
            try:
                journal.set_status(record.record_id, "completed")
            except (OSError, ValueError):
                return ToolResult(
                    status="error",
                    message="Dosya taşındı; günlük kaydı hazırlanmış durumda kaldı.",
                    data={"undo_record_id": record.record_id, "undo_status": "prepared"},
                )
            return ToolResult(
                success=True,
                status="ok",
                message="Dosya geri dönüşüm kutusuna taşındı.",
                data={"undo_record_id": record.record_id, "undo_status": "completed"},
            )
        except (OSError, ValueError) as error:
            return failure(error)


@register_tool
class CreateDirectory(BaseTool):
    name = "create_directory"
    label = "Klasör oluşturma"
    done_label = "Klasör oluşturuldu"
    description = "Çalışma kökünde yeni bir klasör oluşturur; üst klasör mevcut olmalı."
    args_model = PathArguments
    risk = Risk.SIDE_EFFECT

    async def execute(self, arguments: BaseModel, context: ToolContext) -> ToolResult:
        args = cast(PathArguments, arguments)
        try:
            path = safe_join(context.workspace, args.path, allow_symlinks=False)
            path.mkdir()
            if not path.is_dir():
                raise OSError("Klasör oluşturma doğrulanamadı.")
            return ToolResult(success=True, status="ok", message="Klasör oluşturuldu.")
        except (OSError, ValueError) as error:
            return failure(error)
