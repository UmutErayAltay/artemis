"""Rutin deposu ve doğrulaması (ADR 0058).

Rutin, adlandırılmış SABİT araç çağrıları dizisidir: argümanlar düz değerdir, istem ya da
model yoktur. Tek dosya `state_dir/routines.json`, atomik yazılır ve yüklenirken şemayla
doğrulanır. Bozuk dosya sessizce sıfırlanmaz: rutinler kapanır, kullanıcıya dürüstçe söylenir
ve dosya kullanıcı elle düzeltene dek olduğu gibi kalır.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from artemis.text import turkish_fold
from artemis.tools.registry import ToolRegistry

MAX_STEPS = 12
MAX_ROUTINES = 30
# Rutin içinden rutin çağrılamaz: sonsuz özyineleme ve "kayıtlı rutin başka rutini gizler" yolu yok.
FORBIDDEN_PREFIX = "routine_"


class RoutinesError(RuntimeError):
    """Dosya bozuk ya da yazılamıyor; mesaj kullanıcıya olduğu gibi gösterilir."""


def name_key(name: str) -> str:
    """Ad karşılaştırması: boşluk sayısı ve büyük/küçük harf fark etmez.

    Türkçe İ/I önce çevrilir: düz `casefold` "İ"yi "i" + birleşik nokta yapar ve "İşe başla" ile
    "işe başla" ayrı rutin sayılırdı.
    """
    return turkish_fold(" ".join(name.split()))


class RoutineStep(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tool: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,64}$")
    arguments: dict[str, Any] = Field(default_factory=dict)


class RoutineSchedule(BaseModel):
    model_config = ConfigDict(extra="forbid")

    at: str = Field(pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    # 0 = Pazartesi ... 6 = Pazar (datetime.weekday()).
    days: tuple[int, ...] = (0, 1, 2, 3, 4, 5, 6)

    @field_validator("days")
    @classmethod
    def validate_days(cls, values: tuple[int, ...]) -> tuple[int, ...]:
        if not values or any(day < 0 or day > 6 for day in values):
            raise ValueError("days 0 (Pazartesi) ile 6 (Pazar) arasında en az bir gün olmalı")
        return tuple(sorted(set(values)))


class Routine(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(pattern=r"^\w[\w -]{0,39}$")
    steps: list[RoutineStep] = Field(min_length=1, max_length=MAX_STEPS)
    schedule: RoutineSchedule | None = None


class RoutineFile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    routines: list[Routine] = Field(default_factory=list, max_length=MAX_ROUTINES)

    @model_validator(mode="after")
    def unique_names(self) -> RoutineFile:
        keys = [name_key(routine.name) for routine in self.routines]
        if len(keys) != len(set(keys)):
            raise ValueError("Rutin adları benzersiz olmalı")
        return self


# Bazı vekil sunucular (ör. Antigravity önündeki CLIProxyAPI) araç adlarını modelden gizler:
# `open_application` modele `mcp__organ_enhance__clip_open_application` diye görünür. Araç
# çağrısının adını vekil geri çevirir, ama rutin adımı gibi ARGÜMAN içinde yazılan adı çeviremez.
_FOREIGN_PREFIX = re.compile(r"^mcp__[A-Za-z0-9_-]+?__")


def resolve_tool_name(name: str, registry: ToolRegistry) -> str | None:
    """Kayıtlı araç adı; gizlenmiş adda önce `mcp__x__` öneki, sonra tek ön sözcük atılır.

    Yalnız kayıtlı bir araca çıkan ad kabul edilir; tahmin yoktur. Artemis'in kendi MCP
    araçları tek alt çizgiyle (`mcp_<sunucu>_<araç>`) adlanır, çift alt çizgili önek yabancıdır."""
    base = _FOREIGN_PREFIX.sub("", name)
    candidates = [name, base]
    if base != name and "_" in base:
        candidates.append(base.split("_", 1)[1])
    return next((item for item in candidates if registry.get(item) is not None), None)


def validate_steps(steps: list[RoutineStep], registry: ToolRegistry) -> list[RoutineStep]:
    """Her adımı aracın kendi `args_model`'ine karşı doğrular; normalleşmiş kopya döner.

    Kayıtta ve çalıştırmada aynı fonksiyon çağrılır: dosya elle bozulduysa ya da araç sonradan
    kaldırıldıysa hiçbir adım çalışmadan, rutin baştan reddedilir. Hata metni ham argümanı
    taşımaz.
    """
    checked: list[RoutineStep] = []
    for number, step in enumerate(steps, start=1):
        resolved = resolve_tool_name(step.tool, registry)
        # Yasak önek ÇÖZÜLMÜŞ adda denetlenir: "x_routine_run" yasağı aşamasın.
        if resolved is not None and resolved.startswith(FORBIDDEN_PREFIX) or (
            step.tool.startswith(FORBIDDEN_PREFIX)
        ):
            raise ValueError(f"Adım {number}: rutin içinden rutin aracı çağrılamaz.")
        if resolved is None:
            raise ValueError(f"Adım {number}: böyle bir araç yok ({step.tool}).")
        tool = registry.get(resolved)
        assert tool is not None
        try:
            normalized = tool.args_model.model_validate(step.arguments).model_dump(mode="json")
        except ValidationError as error:
            raise ValueError(f"Adım {number}: {resolved} argümanları şemaya uymuyor.") from error
        checked.append(RoutineStep(tool=resolved, arguments=normalized))
    return checked


class RoutineStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> list[Routine]:
        try:
            text = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return []
        except OSError as error:
            raise RoutinesError(f"Rutin dosyası okunamadı ({type(error).__name__}).") from error
        except UnicodeDecodeError as error:
            # Not Defteri ile UTF-16/ANSI kaydedilmiş dosya da "bozuk"tur; zamanlayıcıyı düşürmez.
            raise RoutinesError(
                "Rutin dosyası bozuk (UTF-8 değil), rutinler kapalı. Dosyayı UTF-8 olarak "
                f"kaydedin ya da silin: {self.path}"
            ) from error
        try:
            return RoutineFile.model_validate(json.loads(text)).routines
        except (ValueError, ValidationError) as error:
            # ValidationError de ValueError'dır; hata metni içerik taşıyabilir, yazılmaz.
            raise RoutinesError(
                "Rutin dosyası bozuk, rutinler kapalı. Dosyayı elle düzeltin ya da silin: "
                f"{self.path}"
            ) from error

    def save(self, routines: list[Routine]) -> None:
        data = RoutineFile(routines=routines).model_dump(mode="json")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(
            prefix=".routines-", suffix=".json", dir=self.path.parent
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(data, stream, ensure_ascii=False, indent=2)
            os.replace(temporary, self.path)
        except OSError as error:
            raise RoutinesError(f"Rutin dosyası yazılamadı ({type(error).__name__}).") from error
        finally:
            Path(temporary).unlink(missing_ok=True)

    def find(self, name: str) -> Routine | None:
        key = name_key(name)
        return next((item for item in self.load() if name_key(item.name) == key), None)
