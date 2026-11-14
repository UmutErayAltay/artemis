"""Kod düzeni ratchet'i: dosya <= 1500 satır, fonksiyon <= 200 satır.

Büyük dosya/fonksiyonlar gözden geçirmeyi ve bölmeyi zorlaştırır. Sınırı aşan
mevcut yerler ALLOW içinde kayıtlıdır; kayıtlar yalnız küçülebilir (liste
kısalır, değerler azalır). Yeni ihlal testi düşürür.
"""

from __future__ import annotations

import ast
from pathlib import Path

MAX_FILE = 1500
MAX_FUNC = 200

ROOT = Path(__file__).parents[1]

ALLOW: dict[str, int] = {
    # güvenlik çekirdeği; yalnız boyut için bölünmez (ADR gerekir)
    "artemis/safety/gate.py::SafetyGate.dispatch": 224,
    "artemis/brain/agent_loop.py::AgentLoop.run": 337,
}


def _walk(node: ast.AST, scope: tuple[str, ...], rel: str, out: dict[str, int]) -> None:
    for child in ast.iter_child_nodes(node):
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names = (*scope, child.name)
            if not isinstance(child, ast.ClassDef):
                size = (child.end_lineno or child.lineno) - child.lineno + 1
                out[f"{rel}::{'.'.join(names)}"] = size
            _walk(child, names, rel, out)
        else:
            _walk(child, scope, rel, out)


def _sizes() -> dict[str, int]:
    out: dict[str, int] = {}
    for path in sorted((ROOT / "artemis").rglob("*.py")):
        rel = path.relative_to(ROOT).as_posix()
        text = path.read_text(encoding="utf-8")
        out[rel] = len(text.splitlines())
        _walk(ast.parse(text), (), rel, out)
    return out


def _limit(key: str) -> int:
    return MAX_FILE if "::" not in key else MAX_FUNC


def test_limits() -> None:
    bad = [
        f"{k}: {v} > {ALLOW.get(k, _limit(k))}"
        for k, v in _sizes().items()
        if v > ALLOW.get(k, _limit(k))
    ]
    assert not bad, "\n".join(bad)


def test_allowlist_only_shrinks() -> None:
    sizes = _sizes()
    stale = [
        k
        for k, cap in ALLOW.items()
        if k not in sizes or not (_limit(k) < sizes[k] <= cap)
    ]
    assert not stale, f"allowlist'ten çıkarılmalı veya düşürülmeli: {stale}"
