"""Çalışma köküyle sınırlı dosya işlemleri, sonuçları diskten doğrular."""

import fnmatch
import shutil
from pathlib import Path
from typing import Annotated, Any, Literal, Self, cast

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from artemis.i18n import t
from artemis.safety.paths import safe_join
from artemis.safety.undo import UndoJournal
from artemis.tools.base import BaseTool, Risk, ToolContext, ToolResult
from artemis.tools.registry import register_tool


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


def _plain_name(value: str) -> str:
    """Toplu seçimde ad yalnız dosya adıdır: yol ayırıcı ve `..` yok."""
    if "/" in value or "\\" in value or ".." in value or "\x00" in value:
        raise ValueError("Ad yol ayırıcı veya '..' içeremez.")
    return value


class CopyArguments(MoveArguments):
    pass


class MoveFilesArguments(Arguments):
    source_dir: str = ""
    destination_dir: str = ""
    names: list[Annotated[str, Field(min_length=1, max_length=255)]] | None = Field(
        default=None, min_length=1, max_length=50
    )
    pattern: str | None = Field(default=None, min_length=1, max_length=64)

    @field_validator("names")
    @classmethod
    def _names_plain(cls, value: list[str] | None) -> list[str] | None:
        for name in value or []:
            _plain_name(name)
        return value

    @field_validator("pattern")
    @classmethod
    def _pattern_plain(cls, value: str | None) -> str | None:
        return None if value is None else _plain_name(value)

    @model_validator(mode="after")
    def _exactly_one(self) -> Self:
        if (self.names is None) == (self.pattern is None):
            raise ValueError("names ya da pattern'den tam biri verilmeli.")
        return self


PATH_RULE = (
    "Yollar çalışma köküne (masaüstü) göredir; kökü listelemek için yol boş bırakılır; "
    "C:\\... gibi mutlak yol kullanma."
)
PATH_HINT = (
    "Yol çalışma köküne göre göreli olmalı (örn. 'belgeler/not.txt'); kökü listelemek için "
    "list_files'ı yolsuz çağır; C:\\... gibi mutlak yol kullanma."
)
MAX_COPY_BYTES = 50 * 1024 * 1024


def failure(error: OSError | ValueError | UnicodeError) -> ToolResult:
    """Ham istisna metnindeki kullanıcı yolu veya sırları dışarı çıkarmaz."""
    if isinstance(error, FileNotFoundError):
        detail = t("hedef bulunamadı.")
    elif isinstance(error, PermissionError):
        detail = t("erişim reddedildi.")
    elif isinstance(error, UnicodeError):
        detail = t("dosya geçerli UTF-8 metni değil.")
    elif isinstance(error, ValueError):
        detail = t("yol veya işlem geçersiz.")
        if str(error).startswith(("Mutlak", "Boş", "Çalışma kökünün")):
            detail = f"{detail} {t(PATH_HINT)}"
    else:
        detail = t("işletim sistemi işlemi başarısız oldu.")
    return ToolResult(
        status="error",
        message=t("Dosya işlemi başarısız: {detail}").format(detail=detail),
    )


def move_verified(source: Path, destination: Path) -> ToolResult:
    """Hedefin üstüne yazmaz; taşıma sonrası iki konumu da denetler."""
    if not source.is_file():
        raise ValueError("Kaynak normal bir dosya olmalı.")
    if destination.exists() or destination.is_symlink():
        raise ValueError("Hedef zaten var; üzerine yazılmadı.")
    source.rename(destination)
    if source.exists() or not destination.is_file():
        raise OSError("Taşıma sonucu doğrulanamadı.")
    return ToolResult(success=True, status="ok", message=t("Dosya taşındı."))


@register_tool
class ListFiles(BaseTool):
    name = "list_files"
    label = "Dosyaları listeleme"
    done_label = "Dosyalar listelendi"
    description = (
        "Çalışma kökündeki veya göreli klasördeki dosyaları boyutlarıyla listeler."
        + " "
        + PATH_RULE
    )
    args_model = ListArguments
    risk = Risk.READ_ONLY
    external = True

    async def execute(self, arguments: BaseModel, context: ToolContext) -> ToolResult:
        args = cast(ListArguments, arguments)
        try:
            workspace_root = context.workspace.resolve()
            directory = (
                workspace_root
                if not args.path
                else safe_join(context.workspace, args.path)
            )
            entries = []
            for item in sorted(directory.iterdir(), key=lambda path: path.name.casefold()):
                try:
                    relative = item.relative_to(workspace_root).as_posix()
                    # Bağlantı hedefini de denetlemek kök dışı metadata sızıntısını önler.
                    checked = safe_join(context.workspace, relative)
                    info = checked.stat()
                    is_dir = checked.is_dir()
                except (OSError, ValueError):
                    # Kök dışına çıkan veya kırık olan tekil girdi dışarı sızdırılmadan
                    # atlanır; klasördeki diğer öğeler listelenmeye devam eder.
                    continue
                entries.append(
                    {
                        "name": item.name,
                        "path": relative,
                        "size": info.st_size,
                        "is_dir": is_dir,
                    }
                )
            return ToolResult(
                success=True,
                status="ok",
                message=t("{count} öğe listelendi.").format(count=len(entries)),
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
    description = (
        "Göreli UTF-8 metin dosyasını okur; büyük içerik sınırlandırılır."
        + " "
        + PATH_RULE
    )
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
                message=t("Dosya okundu."),
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
    description = (
        "Göreli konumda yeni UTF-8 dosya oluşturur; mevcut dosyaya yazmaz."
        + " "
        + PATH_RULE
    )
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
            return ToolResult(success=True, status="ok", message=t("Dosya oluşturuldu."))
        except (OSError, ValueError, UnicodeError) as error:
            return failure(error)


@register_tool
class MoveFile(BaseTool):
    name = "move_file"
    label = "Dosya taşıma"
    done_label = "Dosya taşındı"
    description = "Dosyayı çalışma kökü içinde taşır; hedef zaten varsa reddeder." + " " + PATH_RULE
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
class CopyFile(BaseTool):
    name = "copy_file"
    label = "Dosya kopyalama"
    done_label = "Dosya kopyalandı"
    description = (
        "Normal bir dosyayı çalışma kökü içinde kopyalar; hedef zaten varsa reddeder, hedef "
        "klasör önce create_directory ile oluşturulmalı. " + PATH_RULE
    )
    args_model = CopyArguments
    risk = Risk.SIDE_EFFECT

    async def execute(self, arguments: BaseModel, context: ToolContext) -> ToolResult:
        args = cast(CopyArguments, arguments)
        try:
            source = safe_join(context.workspace, args.source, allow_symlinks=False)
            destination = safe_join(context.workspace, args.destination, allow_symlinks=False)
            if not source.is_file():
                raise ValueError("Kaynak normal bir dosya olmalı.")
            if not destination.parent.is_dir():
                return ToolResult(
                    message=t(
                        "Hedef klasör yok; önce create_directory ile oluştur, sonra tekrar kopyala."
                    )
                )
            if destination.exists() or destination.is_symlink():
                return ToolResult(message=t("Hedef zaten var; üzerine yazılmadı."))
            size = source.stat().st_size
            if size > MAX_COPY_BYTES:
                return ToolResult(message=t("Dosya kopyalamak için çok büyük (en çok 50 MiB)."))
            shutil.copyfile(source, destination)
            if not destination.is_file() or destination.stat().st_size != size:
                raise OSError("Kopya doğrulanamadı.")
            return ToolResult(success=True, status="ok", message=t("Dosya kopyalandı."))
        except (OSError, ValueError) as error:
            return failure(error)


@register_tool
class MoveFiles(BaseTool):
    name = "move_files"
    label = "Toplu dosya taşıma"
    done_label = "Dosyalar taşındı"
    description = (
        "Bir klasördeki birden çok dosyayı başka klasöre taşır (alt klasöre inmez, üzerine "
        "yazmaz). Ya names (en çok 50 dosya adı) ya da pattern (yalnız ad üzerinde, örn. "
        "*.txt) ver. Hedef klasör mevcut olmalı. " + PATH_RULE
    )
    args_model = MoveFilesArguments
    risk = Risk.SIDE_EFFECT

    async def execute(self, arguments: BaseModel, context: ToolContext) -> ToolResult:
        args = cast(MoveFilesArguments, arguments)
        try:
            source_dir = _dir_of(context, args.source_dir)
            destination_dir = _dir_of(context, args.destination_dir)
            if not source_dir.is_dir():
                raise ValueError("Kaynak klasör yok.")
            if not destination_dir.is_dir():
                return ToolResult(
                    message=t(
                        "Hedef klasör yok; önce create_directory ile oluştur, sonra tekrar dene."
                    )
                )
            if args.names is not None:
                names = list(dict.fromkeys(args.names))
            else:
                pattern = cast(str, args.pattern)
                names = sorted(
                    item.name
                    for item in source_dir.iterdir()
                    if fnmatch.fnmatch(item.name, pattern) and item.is_file()
                )
        except (OSError, ValueError) as error:
            return failure(error)
        if not names:
            return ToolResult(message=t("Eşleşen dosya yok; hiçbir dosya taşınmadı."))
        rel_src = _rel(args.source_dir)
        rel_dst = _rel(args.destination_dir)
        rows: list[dict[str, Any]] = []
        for name in names:
            try:
                source = safe_join(context.workspace, rel_src + name, allow_symlinks=False)
                target = safe_join(context.workspace, rel_dst + name, allow_symlinks=False)
                if target.exists() or target.is_symlink():
                    rows.append(_row(name, "skipped_exists", "Hedefte aynı ad var; atlandı."))
                    continue
                move_verified(source, target)
                rows.append(_row(name, "moved", "Taşındı."))
            except (OSError, ValueError) as error:
                rows.append(_row(name, "failed", failure(error).message))
        moved = sum(row["status"] == "moved" for row in rows)
        data = {"files": rows}
        if moved == len(rows):
            return ToolResult(
                success=True,
                status="ok",
                message=t("{count} dosya taşındı.").format(count=moved),
                data=data,
            )
        return ToolResult(
            status="partial" if moved else "error",
            message=t("{moved}/{total} dosya taşındı; kalanlar için 'files' listesine bak.").format(
                moved=moved, total=len(rows)
            ),
            data=data,
        )


def _rel(directory: str) -> str:
    """Klasör önekini ('' ya da 'a/b/') dosya adına eklemek için."""
    directory = directory.strip().replace("\\", "/").strip("/")
    return f"{directory}/" if directory else ""


def _dir_of(context: ToolContext, directory: str) -> Path:
    rel = _rel(directory)
    if not rel:
        return context.workspace.resolve()
    return safe_join(context.workspace, rel.rstrip("/"), allow_symlinks=False)


def _row(name: str, status: str, message: str) -> dict[str, Any]:
    return {"name": name, "status": status, "message": t(message)}


@register_tool
class RenameFile(BaseTool):
    name = "rename_file"
    label = "Dosyayı yeniden adlandırma"
    done_label = "Dosya yeniden adlandırıldı"
    description = (
        "Dosyanın aynı klasördeki adını değiştirir; new_name yalnız dosya adıdır."
        + " "
        + PATH_RULE
    )
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
    description = (
        "Dosyayı işletim sisteminin geri dönüşüm kutusuna taşır; kalıcı silmez."
        + " "
        + PATH_RULE
    )
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
                        message=t(
                            "Dosya işlemi sonuçlandı; günlük kaydı hazırlanmış durumda kaldı."
                        ),
                        data={"undo_record_id": record.record_id, "undo_status": "prepared"},
                    )
                if removed:
                    return ToolResult(
                        status="error",
                        message=t("Dosya taşınmış olabilir; işlem sonucu belirsiz."),
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
                        message=t(
                            "Dosya taşınmadı; geri alma kaydı hazırlanmış durumda kaldı."
                        ),
                        data={"undo_record_id": record.record_id, "undo_status": "prepared"},
                    )
                return ToolResult(
                    status="error",
                    message=t("Dosya geri dönüşüm kutusuna taşınmadı."),
                    data={"undo_record_id": record.record_id, "undo_status": "failed"},
                )
            try:
                journal.set_status(record.record_id, "completed")
            except (OSError, ValueError):
                return ToolResult(
                    status="error",
                    message=t("Dosya taşındı; günlük kaydı hazırlanmış durumda kaldı."),
                    data={"undo_record_id": record.record_id, "undo_status": "prepared"},
                )
            return ToolResult(
                success=True,
                status="ok",
                message=t("Dosya geri dönüşüm kutusuna taşındı."),
                data={"undo_record_id": record.record_id, "undo_status": "completed"},
            )
        except (OSError, ValueError) as error:
            return failure(error)


@register_tool
class CreateDirectory(BaseTool):
    name = "create_directory"
    label = "Klasör oluşturma"
    done_label = "Klasör oluşturuldu"
    description = (
        "Çalışma kökünde yeni bir klasör oluşturur; üst klasör mevcut olmalı."
        + " "
        + PATH_RULE
    )
    args_model = PathArguments
    risk = Risk.SIDE_EFFECT

    async def execute(self, arguments: BaseModel, context: ToolContext) -> ToolResult:
        args = cast(PathArguments, arguments)
        try:
            path = safe_join(context.workspace, args.path, allow_symlinks=False)
            path.mkdir()
            if not path.is_dir():
                raise OSError("Klasör oluşturma doğrulanamadı.")
            return ToolResult(success=True, status="ok", message=t("Klasör oluşturuldu."))
        except (OSError, ValueError) as error:
            return failure(error)
