"""Documentation that can go stale silently.

Prose drifts from code for free -- nothing fails, and the next reader trusts it.
These checks cover the parts that are mechanically checkable, which is exactly
the set worth automating: commands that must exist, paths that must resolve, and
the shipped risk numbers, which are quoted in the README and frozen in the config
for the trial.

Deliberately NOT checked: test counts, line counts, coverage percentages. Pinning
those would mean editing a doc on every commit, and a check everyone learns to
silence is worse than no check.
"""

from __future__ import annotations

import pathlib
import re

import pytest

from trading_bot.cli import build_parser
from trading_bot.core.policy import Policy

ROOT = pathlib.Path(__file__).resolve().parent.parent
DOCS = ["README.md", "PLAN.md", "deploy/README.md", "records/README.md"]

# Referenced on purpose while absent. `config/earnings.json` is the unwired
# earnings calendar (README section 17); the scripts are recorded in PLAN's repo
# audit as things that were deleted.
EXPECTED_ABSENT = {"config/earnings.json", "scripts/seed_demo_data.py"}


def read(doc: str) -> str:
    return (ROOT / doc).read_text(encoding="utf-8")


@pytest.mark.parametrize("doc", DOCS)
def test_the_doc_exists(doc: str) -> None:
    assert (ROOT / doc).is_file()


@pytest.mark.parametrize("doc", DOCS)
def test_every_documented_command_is_real(doc: str) -> None:
    """A command that was renamed leaves instructions that fail at the prompt --
    and in the crontab, silently, at 18:00."""
    action = next(a for a in build_parser()._actions if getattr(a, "choices", None))
    known = set(action.choices)

    documented = set(re.findall(r"trading_bot\s+([a-z_]+)", read(doc)))
    unknown = documented - known
    assert not unknown, f"{doc} documents commands that do not exist: {sorted(unknown)}"


@pytest.mark.parametrize("doc", DOCS)
def test_every_referenced_repo_path_resolves(doc: str) -> None:
    text = read(doc)
    referenced = set(
        re.findall(r"`((?:config|trading_bot|tests|scripts|deploy|records)/[\w./-]+)`", text)
    )

    missing = {
        r for r in referenced - EXPECTED_ABSENT if not (ROOT / r).exists()
    }
    assert not missing, f"{doc} references paths that do not exist: {sorted(missing)}"


@pytest.mark.parametrize("doc", DOCS)
def test_internal_links_resolve(doc: str) -> None:
    base = (ROOT / doc).parent
    broken = []
    for _label, target in re.findall(r"\[([^\]]+)\]\(([^)#]+)(?:#[^)]*)?\)", read(doc)):
        if target.startswith(("http://", "https://", "mailto:")):
            continue
        if not (base / target).exists():
            broken.append(target)
    assert not broken, f"{doc} has broken links: {broken}"


def test_the_readme_quotes_the_shipped_risk_numbers_correctly() -> None:
    """The README states these in section 4 to distinguish them from the library
    defaults. If the config moves and the prose does not, the document is
    actively misleading about how much risk is being taken."""
    policy = Policy.from_yaml(str(ROOT / "config" / "policy.yaml"))
    # Prose wraps, and this passage is a blockquote -- so the continuation lines
    # carry a "> " that lands mid-phrase once the text is joined.
    readme = " ".join(
        re.sub(r"^\s*>\s?", "", line) for line in read("README.md").splitlines()
    )
    readme = " ".join(readme.split())

    for label, value in [
        ("per trade", policy.max_risk_per_trade),
        ("heat", policy.max_portfolio_heat),
        ("per position", policy.max_position_pct),
    ]:
        shown = f"{value * 100:g}%"
        assert f"{shown} {label}" in readme, (
            f"README does not state {shown} for {label!r}; config says {value}"
        )


def test_the_readme_names_the_policy_version_that_ships() -> None:
    policy = Policy.from_yaml(str(ROOT / "config" / "policy.yaml"))
    assert policy.version in read("README.md")
    assert policy.version in read("PLAN.md")


def test_the_frozen_config_is_still_marked_frozen() -> None:
    """The freeze is a comment in a file anyone can edit, so it gets a test."""
    text = (ROOT / "config" / "policy.yaml").read_text(encoding="utf-8")
    assert "frozen" in text.lower() or "FROZEN" in text
