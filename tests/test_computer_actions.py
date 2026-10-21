"""Bilgisayar eylemi doğrulama kurallarının testleri."""

import json

import pytest
from pydantic import ValidationError

from jarvis.computer.actions import (
    BLOCKED_PROCESSES,
    MODIFIERS,
    NAMED_KEYS,
    SYSTEM_PROMPT,
    Action,
    is_sensitive,
    key_block_reason,
    parse_action,
    parse_keys,
    process_block_reason,
    scale_point,
)


@pytest.mark.parametrize(
    "payload",
    [
        {"type": "click", "x": 0, "y": 0, "reason": "Düğmeye tıkla"},
        {"type": "type", "text": "Merhaba", "reason": "Metni yaz"},
        {"type": "key", "keys": "ctrl+l", "reason": "Adres çubuğunu aç"},
        {"type": "scroll", "x": 10, "y": 20, "dy": -5, "reason": "Aşağı kaydır"},
        {"type": "wait", "seconds": 0.5, "reason": "Kısa bekle"},
        {"type": "done", "text": "Görev tamamlandı", "reason": "Hedefe ulaşıldı"},
        {"type": "ask_user", "text": "Devam edeyim mi?", "reason": "Onay iste"},
    ],
)
def test_valid_action_for_each_type(payload: dict[str, object]) -> None:
    action = Action.model_validate(payload)

    assert action.type == payload["type"]


@pytest.mark.parametrize(
    "payload",
    [
        {"type": "click", "y": 1, "reason": "Eksik x"},
        {"type": "click", "x": 1, "reason": "Eksik y"},
        {"type": "click", "x": -1, "y": 1, "reason": "Negatif x"},
        {"type": "click", "x": 1, "y": -1, "reason": "Negatif y"},
        {"type": "type", "reason": "Eksik metin"},
        {"type": "type", "text": "", "reason": "Boş metin"},
        {"type": "type", "text": "x" * 501, "reason": "Uzun metin"},
        {"type": "type", "text": "satır\nsonu", "reason": "Satır sonu"},
        {"type": "type", "text": "sat\rsonu", "reason": "Taşıma sonu"},
        {"type": "key", "reason": "Eksik tuş"},
        {"type": "key", "keys": "ctrl+", "reason": "Hatalı tuş"},
        {"type": "scroll", "y": 1, "dy": 1, "reason": "Eksik x"},
        {"type": "scroll", "x": 1, "dy": 1, "reason": "Eksik y"},
        {"type": "scroll", "x": 1, "y": 1, "reason": "Eksik dy"},
        {"type": "scroll", "x": -1, "y": 1, "dy": 1, "reason": "Negatif x"},
        {"type": "scroll", "x": 1, "y": -1, "dy": 1, "reason": "Negatif y"},
        {"type": "scroll", "x": 1, "y": 1, "dy": 0, "reason": "Sıfır kaydırma"},
        {"type": "scroll", "x": 1, "y": 1, "dy": 21, "reason": "Çok büyük kaydırma"},
        {"type": "scroll", "x": 1, "y": 1, "dy": -21, "reason": "Çok küçük kaydırma"},
        {"type": "wait", "reason": "Eksik süre"},
        {"type": "wait", "seconds": 0, "reason": "Sıfır süre"},
        {"type": "wait", "seconds": -1, "reason": "Negatif süre"},
        {"type": "wait", "seconds": 5.1, "reason": "Uzun süre"},
        {"type": "done", "reason": "Eksik özet"},
        {"type": "done", "text": "", "reason": "Boş özet"},
        {"type": "done", "text": "x" * 1001, "reason": "Uzun özet"},
        {"type": "ask_user", "reason": "Eksik soru"},
        {"type": "ask_user", "text": "", "reason": "Boş soru"},
        {"type": "ask_user", "text": "x" * 1001, "reason": "Uzun soru"},
        {"type": "wait", "seconds": 1, "reason": ""},
        {"type": "wait", "seconds": 1, "reason": "x" * 301},
        {"type": "wait", "seconds": 1},
        {"type": "unknown", "reason": "Bilinmeyen tür"},
    ],
)
def test_invalid_actions_are_rejected(payload: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        Action.model_validate(payload)


def test_parse_action_accepts_json_fences_and_prose() -> None:
    raw = """
Model output:
```json
{"type": "click", "x": 12, "y": 34, "reason": "Düğmeye tıkla"}
```
The action is ready.
"""

    action = parse_action(raw)

    assert action.type == "click"
    assert action.x == 12
    assert action.y == 34


def _assert_parse_error(raw: str) -> None:
    with pytest.raises(ValueError) as exc_info:
        parse_action(raw)

    message = str(exc_info.value)
    prefix = "Model geçerli bir eylem döndürmedi: "
    assert message.startswith(prefix)
    assert len(message) - len(prefix) <= 200


def test_parse_action_rejects_invalid_json() -> None:
    _assert_parse_error("```json\n{invalid json}\n```")


def test_parse_action_rejects_array_instead_of_object() -> None:
    _assert_parse_error("[]")


def test_parse_action_rejects_extra_field() -> None:
    raw = json.dumps(
        {
            "type": "wait",
            "seconds": 1,
            "reason": "Bekle",
            "extra": True,
        }
    )

    _assert_parse_error(raw)


def test_parse_action_rejects_missing_braces() -> None:
    _assert_parse_error("Model bir eylem döndürmedi.")


def test_parse_action_rejects_invalid_action() -> None:
    _assert_parse_error('{"type": "click", "reason": "Koordinat eksik"}')


def test_key_vocabulary_is_exact() -> None:
    expected_named = {
        "enter",
        "escape",
        "tab",
        "backspace",
        "delete",
        "space",
        "up",
        "down",
        "left",
        "right",
        "home",
        "end",
        "pageup",
        "pagedown",
        "insert",
    } | {f"f{number}" for number in range(1, 25)}

    assert MODIFIERS == ("ctrl", "alt", "shift", "win")
    assert frozenset(expected_named) == NAMED_KEYS


@pytest.mark.parametrize(
    ("combo", "expected"),
    [
        ("control+l", ("ctrl", "l")),
        ("esc", ("escape",)),
        ("return", ("enter",)),
        ("del", ("delete",)),
        ("windows+r", ("win", "r")),
        ("cmd+r", ("win", "r")),
        ("super+r", ("win", "r")),
        ("meta+r", ("win", "r")),
        ("pgup", ("pageup",)),
        ("pgdn", ("pagedown",)),
    ],
)
def test_parse_keys_applies_aliases(combo: str, expected: tuple[str, ...]) -> None:
    assert parse_keys(combo) == expected


def test_parse_keys_preserves_modifier_order() -> None:
    assert parse_keys("shift+ctrl+a") == ("shift", "ctrl", "a")


@pytest.mark.parametrize("key", ["a", "z", "0", "9", "enter", "f24"])
def test_parse_keys_accepts_single_keys(key: str) -> None:
    assert parse_keys(key) == (key,)


def test_parse_keys_accepts_lone_modifier() -> None:
    assert parse_keys("win") == ("win",)


@pytest.mark.parametrize(
    "combo",
    [
        "",
        "ctrl+",
        "ctrl+ctrl+a",
        "a+ctrl",
        "hyper+a",
        "ctrl+alt+shift+win+a",
        "f25",
    ],
)
def test_parse_keys_rejects_invalid_combinations(combo: str) -> None:
    with pytest.raises(ValueError):
        parse_keys(combo)


@pytest.mark.parametrize(
    "combo",
    [
        "win+r",
        "win",
        "ctrl+alt+delete",
        "ctrl+alt+del",
        "ctrl+shift+esc",
    ],
)
def test_key_block_reason_blocks_dangerous_combinations(combo: str) -> None:
    assert key_block_reason(parse_keys(combo)) is not None


def test_key_block_reason_allows_normal_combination() -> None:
    assert key_block_reason(parse_keys("ctrl+l")) is None


def test_is_sensitive_for_alt_f4() -> None:
    action = Action.model_validate(
        {"type": "key", "keys": "alt+f4", "reason": "Pencereyi kapat"}
    )

    assert is_sensitive(action)


def test_is_sensitive_for_explicit_flag() -> None:
    action = Action.model_validate(
        {
            "type": "click",
            "x": 1,
            "y": 1,
            "reason": "Gönder",
            "sensitive": True,
        }
    )

    assert is_sensitive(action)


def test_is_sensitive_false_for_normal_action() -> None:
    action = Action.model_validate(
        {"type": "key", "keys": "ctrl+l", "reason": "Adres çubuğunu aç"}
    )

    assert not is_sensitive(action)


def test_scale_point_scales_coordinates() -> None:
    assert scale_point(50, 100, (100, 200), (200, 400)) == (100, 200)


def test_scale_point_clamps_to_screen() -> None:
    assert scale_point(199, 199, (200, 200), (100, 100)) == (99, 99)


@pytest.mark.parametrize(
    ("x", "y"),
    [
        (-1, 0),
        (100, 0),
        (0, -1),
        (0, 200),
    ],
)
def test_scale_point_rejects_out_of_range_coordinates(x: int, y: int) -> None:
    with pytest.raises(ValueError, match="Koordinat"):
        scale_point(x, y, (100, 200), (1920, 1080))


def test_blocked_processes_cover_shells_and_password_managers() -> None:
    # Liste büyüyebilir (ADR 0039); bu çekirdek küme hiçbir zaman düşmemeli.
    assert frozenset(
        {
            "cmd.exe",
            "powershell.exe",
            "pwsh.exe",
            "windowsterminal.exe",
            "wt.exe",
            "conhost.exe",
            "openconsole.exe",
            "regedit.exe",
            "mmc.exe",
            "taskmgr.exe",
            "1password.exe",
            "bitwarden.exe",
            "keepass.exe",
            "keepassxc.exe",
        }
    ) <= BLOCKED_PROCESSES


def test_process_block_reason_blocks_own_pid() -> None:
    assert (
        process_block_reason("brave.exe", 1234, 1234)
        == "Jarvis'in kendi penceresi"
    )


def test_process_block_reason_handles_windows_path() -> None:
    reason = process_block_reason(r"C:\Windows\System32\cmd.exe", 1234, 5678)

    assert reason is not None
    assert "cmd.exe" in reason


def test_process_block_reason_is_case_insensitive() -> None:
    assert process_block_reason("C:/Windows/System32/CMD.EXE", 1234, 5678) is not None


def test_process_block_reason_allows_other_process() -> None:
    assert process_block_reason("brave.exe", 1234, 5678) is None


@pytest.mark.parametrize(
    "action_type",
    ["click", "type", "key", "scroll", "wait", "done", "ask_user"],
)
def test_system_prompt_mentions_every_action_type(action_type: str) -> None:
    assert f'"{action_type}"' in SYSTEM_PROMPT


@pytest.mark.parametrize("combo", ["ctrl+esc", "ctrl+escape", "ctrl+shift+esc", "ctrl+alt+escape"])
def test_start_menu_and_task_manager_combos_are_blocked(combo: str) -> None:
    assert key_block_reason(parse_keys(combo)) is not None


@pytest.mark.parametrize("combo", ["enter", "ctrl+enter", "shift+enter", "alt+f4"])
def test_enter_and_alt_f4_are_sensitive_regardless_of_model_flag(combo: str) -> None:
    action = Action(type="key", keys=combo, reason="deneme", sensitive=False)
    assert is_sensitive(action)


def test_plain_keys_stay_non_sensitive() -> None:
    assert not is_sensitive(Action(type="key", keys="ctrl+l", reason="adres çubuğu"))
    assert not is_sensitive(Action(type="key", keys="tab", reason="sonraki alan"))


@pytest.mark.parametrize(
    "exe",
    [
        r"C:\Windows\explorer.exe",
        r"C:\Windows\SystemApps\MicrosoftWindows.Client.CBS_cw5n1h2txyewy\SearchHost.exe",
        "StartMenuExperienceHost.exe",
        "mshta.exe",
        "rundll32.exe",
        r"C:\Program Files\Git\usr\bin\mintty.exe",
        "wsl.exe",
        r"C:\Python312\python.exe",
        r"C:\Users\x\AppData\Local\Programs\Microsoft VS Code\Code.exe",
    ],
)
def test_launchers_and_terminal_editors_are_blocked(exe: str) -> None:
    assert process_block_reason(exe, 10, 99) is not None
