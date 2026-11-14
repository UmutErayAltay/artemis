import pytest

from artemis.memory import vault
from artemis.text import search_fold, turkish_fold


@pytest.mark.parametrize(
    ("raw", "folded"),
    [("ISPARTA", "ısparta"), ("İşe Başla", "işe başla"), ("Artemiz", "artemiz"), ("ışık", "ışık")],
)
def test_turkish_fold_maps_dotted_and_dotless_i(raw: str, folded: str) -> None:
    assert turkish_fold(raw) == folded


def test_search_fold_equates_every_i() -> None:
    assert search_fold("IŞIK") == search_fold("ışık") == search_fold("işik") == "işik"


def test_vault_reexports_shared_fold() -> None:
    assert vault.turkish_fold is turkish_fold
