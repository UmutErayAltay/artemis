"""Gün sonu özeti (ADR 0066): günün denetim kayıtlarından LLM'siz, deterministik Markdown.

Model çağrılmaz, hiçbir şey makineden çıkmaz. Girdi yalnız denetim kaydının (zaten sırdan
arındırılmış) sayılabilir alanlarıdır: konuşma metni, model yanıtı ve araç sonuç gövdesi
OKUNMAZ, çıktıya girmez. Denetim dosyası büyük olabilir ve bozuk/yarım satır ya da bilinmeyen
şekilli olay içerebilir: dosya sondan sınırlı okunur, bozuk satır atlanır, hiçbir şey fırlatılmaz.
Dosya yolları ve araç adları dış metin gibi ele alınır (denetim/yön karakterleri temizlenir).
"""

from __future__ import annotations

import json
import os
import re
from collections import Counter
from collections.abc import Iterator
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from artemis.proactive.triggers import sanitize_name

# Bir günlük özet için en çok bu kadar bayt ve satır taranır (denetim dosyası 20 MB'a kadar
# büyür, döndürülmüş dosyalar da vardır); tek satır sınırı bozuk, bitmeyen satırı keser.
MAX_SCAN_BYTES = 64 * 1024 * 1024
MAX_SCAN_LINES = 400_000
MAX_LINE_BYTES = 512 * 1024
MAX_LISTED_FILES = 25
MAX_LISTED_TOOLS = 15
MAX_LISTED_JOBS = 15
VAULT_TEXT_LIMIT = 4000  # vault_append metin sınırı
_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_FILE_TOOLS = {
    "create_file": "oluşturuldu",
    "create_directory": "klasör oluşturuldu",
    "move_file": "taşındı",
    "rename_file": "adı değişti",
    "delete_file": "silindi",
}


def parse_day(value: str) -> date | None:
    """Yalnız katı `YYYY-MM-DD`; dosya adı ayrıştırılan tarihten türetilir, girdiden değil."""
    if not _DAY.fullmatch(value):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def summary_path(state_dir: Path, day: date) -> Path:
    return state_dir / "gunluk" / f"{day.isoformat()}.md"


def _candidate_files(audit_path: Path, start: datetime) -> list[Path]:
    """Güncel dosya ve gün başından sonra yazılmış döndürülmüş dosyalar, yeniden eskiye."""
    found: list[tuple[float, Path]] = []
    try:
        siblings = list(audit_path.parent.glob(f"{audit_path.stem}-*.jsonl"))
    except OSError:
        siblings = []
    for path in [audit_path, *siblings]:
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        if mtime >= start.timestamp():
            found.append((mtime, path))
    return [path for _, path in sorted(found, reverse=True)]


def _tail_lines(path: Path, budget: int, truncated: list[bool]) -> Iterator[bytes]:
    """Dosyanın son `budget` baytından tam satırlar (ilk yarım satır atılır).

    Bir şey atlanırsa (dosyanın başı, dev satır) `truncated` dolar: özet bunu söylemek zorunda.
    """
    try:
        with path.open("rb") as stream:
            size = stream.seek(0, os.SEEK_END)
            offset = max(0, size - budget)
            stream.seek(offset)
            if offset:
                truncated.append(True)
                stream.readline(MAX_LINE_BYTES)  # kesilmiş ilk satır
            while True:
                line = stream.readline(MAX_LINE_BYTES)
                if not line:
                    return
                if not line.endswith(b"\n") and len(line) >= MAX_LINE_BYTES:
                    truncated.append(True)
                    while stream.readline(MAX_LINE_BYTES).endswith(b"\n") is False:
                        pass  # bitmeyen dev satırın kalanı atılır
                    continue
                yield line
    except OSError:
        return


def _events(
    audit_path: Path, start: datetime, end: datetime, truncated: list[bool]
) -> Iterator[dict[str, Any]]:
    """`[start, end)` içindeki olaylar, eskiden yeniye; sınırlı, bozuk satırı atlar.

    Tarama bayt ya da satır sınırıyla kesilirse `truncated` dolar (kural 5: eksik özet tam gibi
    sunulmaz).
    """
    budget, lines = MAX_SCAN_BYTES, MAX_SCAN_LINES
    chunks: list[list[dict[str, Any]]] = []
    for path in _candidate_files(audit_path, start):
        if budget <= 0 or lines <= 0:
            truncated.append(True)
            break
        allowed = budget
        try:
            budget -= min(path.stat().st_size, budget)
        except OSError:
            continue
        chunk: list[dict[str, Any]] = []
        for raw in _tail_lines(path, allowed, truncated):
            lines -= 1
            if lines <= 0:
                truncated.append(True)
                break
            try:
                event = json.loads(raw)
                stamp = datetime.fromisoformat(event["timestamp"])
                if not isinstance(event, dict) or stamp.tzinfo is None:
                    continue
                # Uç tarihli satır (yıl 1 / 9999 + ofset) astimezone'da OverflowError verir;
                # tek bozuk satır zamanlayıcıyı düşürmemeli.
                inside = start <= stamp.astimezone(UTC) < end
            except (ValueError, KeyError, TypeError, RecursionError, OverflowError):
                continue
            if inside:
                chunk.append(event)
        chunks.append(chunk)
    for chunk in reversed(chunks):  # en eski dosya önce
        yield from chunk


def _text(value: object, limit: int = 200) -> str:
    return sanitize_name(value, limit) if isinstance(value, str) else ""


def _decimal(value: object) -> Decimal:
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return Decimal(0)
    return amount if amount.is_finite() and amount >= 0 else Decimal(0)


def build_summary(
    audit_path: Path,
    day: date,
    start: datetime,
    end: datetime,
    *,
    reminders: dict[str, int] | None = None,
    late_at: str = "",
) -> str | None:
    """Günün özeti (Markdown) ya da gün boşsa `None`. `reminders`: durum -> sayı (depodan)."""
    turns = Counter[str]()
    tools_ok, tools_failed = Counter[str](), Counter[str]()
    files: list[str] = []
    files_total = 0
    jobs: list[str] = []
    routines: list[str] = []
    denied: Counter[str] = Counter()
    other = Counter[str]()
    cost = Decimal(0)
    pending: dict[str, list[tuple[str, dict[str, Any]]]] = {}
    truncated: list[bool] = []
    for event in _events(audit_path, start, end, truncated):
        name = event.get("event")
        turn_id = event.get("turn_id")
        key = turn_id if isinstance(turn_id, str) else ""
        if name == "turn_start":
            if event.get("channel") == "typed_tool":
                turns["panel"] += 1
            elif event.get("unattended") is True:
                turns["gözetimsiz"] += 1
            else:
                turns["sohbet"] += 1
        elif name == "turn_end":
            usage = event.get("usage")
            if isinstance(usage, dict):
                cost += _decimal(usage.get("cost_usd"))
            if event.get("stop_reason") not in (None, "completed"):
                turns["tamamlanmayan"] += 1
        elif name == "tool_requested":
            tool, args = event.get("tool"), event.get("arguments")
            if isinstance(tool, str):
                pending.setdefault(key, []).append((tool, args if isinstance(args, dict) else {}))
        elif name == "tool_result":
            tool, result = event.get("tool"), event.get("result")
            if not isinstance(tool, str) or not isinstance(result, dict):
                continue
            stack = pending.get(key, [])
            args = {}
            for index in range(len(stack) - 1, -1, -1):  # iç içe (rutin) adımlarda en yakın eş
                if stack[index][0] == tool:
                    args = stack.pop(index)[1]
                    break
            label = _text(tool, 64)
            if result.get("success") is True:
                tools_ok[label] += 1
                verb = _FILE_TOOLS.get(tool)
                if verb:
                    files_total += 1
                    paths = [args.get(n) for n in ("source", "destination", "path", "new_name")]
                    shown = " → ".join(_text(p, 160) for p in paths if isinstance(p, str))
                    if len(files) < MAX_LISTED_FILES:
                        files.append(f"{shown} ({verb})")
            elif result.get("status") != "denied":
                tools_failed[label] += 1
        elif name == "approval" and event.get("approved") is False:
            denied[_text(event.get("tool"), 64) or "?"] += 1
        elif name == "code_agent_done":
            if len(jobs) < MAX_LISTED_JOBS:
                jobs.append(
                    f"Kod işi {_text(event.get('project'), 60) or '?'}: "
                    f"{_text(event.get('status'), 30) or '?'}"
                )
            else:
                other["kod işi (listelenmeyen)"] += 1
        elif name == "research_end":
            if len(jobs) < MAX_LISTED_JOBS:
                jobs.append(f"Araştırma: {_text(event.get('status'), 30) or '?'}")
            else:
                other["araştırma (listelenmeyen)"] += 1
        elif name == "routine_run_end":
            state = "tamamlandı" if event.get("completed") is True else "yarıda kaldı"
            if len(routines) < MAX_LISTED_JOBS:
                routines.append(f"{_text(event.get('name'), 60) or '?'}: {state}")
        elif name in _OTHER_LABELS:
            other[_OTHER_LABELS[name]] += 1
    reminders = {k: v for k, v in (reminders or {}).items() if v}
    if not (turns or tools_ok or tools_failed or files_total or jobs or routines or denied
            or other or reminders):
        return None
    lines = [f"# Gün özeti · {day.isoformat()}"]
    if late_at:
        lines.append(f"_gecikmeli: bu özet {_text(late_at, 40)} civarı üretildi_")
    if truncated:
        lines.append("_Denetim kaydı tarama sınırını aştı; bu özet eksik olabilir._")
    if turns:
        lines += ["", "## Konuşma", ", ".join(f"{k} {v}" for k, v in turns.items())]
    used = tools_ok + tools_failed
    if used:
        lines += ["", "## Araçlar"]
        for tool, count in used.most_common(MAX_LISTED_TOOLS):
            failed = tools_failed[tool]
            lines.append(f"- {tool}: {count}" + (f" ({failed} başarısız)" if failed else ""))
        if len(used) > MAX_LISTED_TOOLS:
            lines.append(f"- ... ve {len(used) - MAX_LISTED_TOOLS} araç daha")
    if files:
        lines += ["", "## Dosyalar"] + [f"- {item}" for item in files]
        if files_total > len(files):
            lines.append(f"- ... ve {files_total - len(files)} işlem daha")
    if jobs:
        lines += ["", "## Arka plan işleri"] + [f"- {item}" for item in jobs]
    if routines:
        lines += ["", "## Rutinler"] + [f"- {item}" for item in routines]
    if reminders:
        names = {"delivered": "iletildi", "missed": "kaçırıldı", "failed": "iletilemedi"}
        lines += ["", "## Hatırlatmalar", ", ".join(
            f"{names.get(k, k)} {v}" for k, v in sorted(reminders.items())
        )]
    if denied:
        lines += ["", "## Reddedilen onaylar", ", ".join(f"{k} {v}" for k, v in denied.items())]
    if other:
        lines += ["", "## Diğer", ", ".join(f"{k} {v}" for k, v in other.items())]
    if cost > 0:
        lines += ["", f"Model harcaması: ${cost:.4f}"]
    return "\n".join(lines) + "\n"


# ADR 0057-0065 olayları: yalnız sayılır, içerikleri okunmaz.
_OTHER_LABELS = {
    "dictation": "dikte",
    "selection_action": "seçili metin eylemi",
    "telegram_download": "Telegram'dan indirme",
}


def write_summary(path: Path, text: str) -> None:
    """Aynı günün dosyası geçici dosya + `os.replace` ile atomik yeniden yazılır."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        temp.write_text(text, encoding="utf-8", newline="\n")
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)
