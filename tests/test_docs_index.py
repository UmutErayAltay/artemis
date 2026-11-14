import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LINK_RE = re.compile(r"\[[^\]]*\]\(([^)\s]+)\)")


def relative_link_targets(text):
    targets = []
    for raw in LINK_RE.findall(text):
        if raw.startswith(("http://", "https://", "mailto:", "#")):
            continue
        target = raw.split("#", 1)[0].strip()
        if target:
            targets.append(target)
    return targets


def test_every_adr_is_linked_from_index():
    adr_dir = ROOT / "docs" / "adr"
    index_text = (adr_dir / "README.md").read_text(encoding="utf-8")
    linked = {Path(target).name for target in relative_link_targets(index_text)}
    missing = sorted(
        path.name
        for path in adr_dir.glob("*.md")
        if path.name != "README.md" and path.name not in linked
    )
    assert not missing, f"ADR files not linked from docs/adr/README.md: {missing}"


def test_index_links_exist():
    adr_dir = ROOT / "docs" / "adr"
    index_text = (adr_dir / "README.md").read_text(encoding="utf-8")
    missing = sorted(
        target for target in relative_link_targets(index_text)
        if not (adr_dir / target).exists()
    )
    assert not missing, f"docs/adr/README.md links to missing files: {missing}"


def test_readme_en_and_contributing_exist_and_small():
    readme_en = ROOT / "README.en.md"
    contributing = ROOT / "CONTRIBUTING.md"
    assert readme_en.is_file(), "README.en.md is missing at the repo root"
    assert contributing.is_file(), "CONTRIBUTING.md is missing at the repo root"
    text = readme_en.read_text(encoding="utf-8")
    assert len(text.splitlines()) <= 150, "README.en.md exceeds 150 lines"


def test_relative_links_resolve():
    missing = []
    for name in ("README.en.md", "CONTRIBUTING.md"):
        text = (ROOT / name).read_text(encoding="utf-8")
        for target in relative_link_targets(text):
            if not (ROOT / target).exists():
                missing.append(f"{name} -> {target}")
    assert not missing, f"Relative links to missing files: {missing}"
