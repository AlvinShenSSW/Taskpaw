"""Keep public design specs free of machine-local paths (#213)."""

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
ALLOWED_USERS = frozenset(
    {"youruser", "example", "you", "USER", "<user>", "hubert", "Tester"}
)
PATTERNS = (
    ("Unix home", re.compile(r"/(?:Users|home)/(?P<user>[^/\\\r\n]+)/")),
    (
        "Windows home",
        re.compile(r"(?i:[a-z]:[\\/]users[\\/])(?P<user>[^/\\\r\n]+)[\\/]"),
    ),
    ("AFK run", re.compile(r"\.afk[/]runs[/]")),
    ("Claude plugin cache", re.compile(r"\.claude[/]plugins[/]cache")),
)


def _validate_specs(root: Path) -> None:
    paths = sorted((root / "docs" / "specs").glob("*.md"))
    assert paths, "No docs/specs/*.md files found"
    violations = []
    for path in paths:
        for number, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), start=1
        ):
            for category, pattern in PATTERNS:
                for match in pattern.finditer(line):
                    if (
                        "user" in pattern.groupindex
                        and match.group("user") in ALLOWED_USERS
                    ):
                        continue
                    violations.append(
                        f"{path.relative_to(root).as_posix()}:{number}: {category}"
                    )
    if violations:
        raise AssertionError(
            "Machine-local paths in design specs:\n" + "\n".join(violations)
        )


def _write_spec(root: Path, name: str, text: str) -> None:
    path = root / "docs" / "specs" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))


def test_specs_have_no_machine_local_paths():
    _validate_specs(REPO_ROOT)


@pytest.mark.parametrize(
    ("text", "category"),
    [
        ("/Users/synthetic/docs", "Unix home"),
        ("/home/synthetic/docs", "Unix home"),
        ("/Users/Synthetic Person/docs", "Unix home"),
        (r"C:\Users\synthetic\docs", "Windows home"),
        ("C:/Users/synthetic/docs", "Windows home"),
        ("C:/Users/304/docs", "Windows home"),
        (r"c:\users\synthetic\docs", "Windows home"),
        (r"C:\Users\Synthetic Person\docs", "Windows home"),
        (".afk/runs/example/ledger.md", "AFK run"),
        ("~/.afk/runs/example/ledger.md", "AFK run"),
        (".claude/plugins/cache/example/skill", "Claude plugin cache"),
        ("~/.claude/plugins/cache/example/skill", "Claude plugin cache"),
        (".claude/plugins/cache", "Claude plugin cache"),
        ("/Users/you/.afk/runs/example/ledger.md", "AFK run"),
        ("/home/example/.claude/plugins/cache", "Claude plugin cache"),
        ("C:/Users/Tester/.afk/runs/example/ledger.md", "AFK run"),
        ("C:/Users/USER/.claude/plugins/cache", "Claude plugin cache"),
    ],
)
def test_rejected_paths_report_filename_and_line(tmp_path, text, category):
    _write_spec(tmp_path, "bad.md", f"# Fixture\n{text}\n")
    with pytest.raises(AssertionError) as error:
        _validate_specs(tmp_path)
    assert f"docs/specs/bad.md:2: {category}" in str(error.value)


@pytest.mark.parametrize("user", sorted(ALLOWED_USERS))
@pytest.mark.parametrize(
    "template",
    ["/Users/{}/docs", "/home/{}/docs", r"C:\Users\{}\docs", "c:/users/{}/docs"],
)
def test_exact_placeholders_are_allowed(tmp_path, user, template):
    _write_spec(tmp_path, "allowed.md", template.format(user))
    _validate_specs(tmp_path)


@pytest.mark.parametrize("user", ["you-extra", "prefix-you", "tester", "You"])
@pytest.mark.parametrize(
    "template",
    ["/Users/{}/docs", "/home/{}/docs", r"C:\Users\{}\docs", "C:/Users/{}/docs"],
)
def test_placeholder_near_matches_are_rejected(tmp_path, user, template):
    _write_spec(tmp_path, "bad.md", template.format(user))
    with pytest.raises(AssertionError, match=r"docs/specs/bad\.md:1:"):
        _validate_specs(tmp_path)


@pytest.mark.parametrize(
    "text",
    [
        "/Users/you/docs /Users/synthetic/docs",
        "/Users/synthetic/docs /Users/you/docs",
        "/home/example/docs /home/synthetic/docs",
        r"C:\Users\Tester\docs C:\Users\synthetic\docs",
        "C:/Users/USER/docs C:/Users/synthetic/docs",
        "/Users/you/docs .afk/runs/example/ledger.md",
        "/Users/you/docs .claude/plugins/cache",
    ],
)
def test_allowed_path_never_masks_forbidden_neighbor(tmp_path, text):
    _write_spec(tmp_path, "mixed.md", text)
    with pytest.raises(AssertionError, match=r"docs/specs/mixed\.md:1:"):
        _validate_specs(tmp_path)


def test_multiple_allowed_paths_and_repo_references_pass(tmp_path):
    _write_spec(
        tmp_path,
        "good.md",
        "/Users/you/docs /home/example/docs C:/Users/Tester/docs\n"
        "docs/specs/design.md taskpaw_v3/tests/test_version.py\n"
        ".github/workflows/ci.yml .afk/config.md\n",
    )
    _validate_specs(tmp_path)


def test_violations_are_aggregated_in_sorted_file_and_line_order(tmp_path):
    _write_spec(tmp_path, "z.md", "# Fixture\n.claude/plugins/cache\n")
    _write_spec(
        tmp_path,
        "a.md",
        "# Fixture\n/home/synthetic/docs\n.afk/runs/example/ledger.md\n",
    )
    with pytest.raises(AssertionError) as error:
        _validate_specs(tmp_path)
    assert str(error.value) == (
        "Machine-local paths in design specs:\n"
        "docs/specs/a.md:2: Unix home\n"
        "docs/specs/a.md:3: AFK run\n"
        "docs/specs/z.md:2: Claude plugin cache"
    )


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_fenced_paths_are_rejected_with_one_based_line_numbers(tmp_path, newline):
    _write_spec(
        tmp_path, "fenced.md", newline.join(["```text", "/home/synthetic/docs", "```"])
    )
    with pytest.raises(AssertionError) as error:
        _validate_specs(tmp_path)
    assert str(error.value) == (
        "Machine-local paths in design specs:\ndocs/specs/fenced.md:2: Unix home"
    )


def test_only_direct_markdown_specs_are_scanned(tmp_path):
    _write_spec(tmp_path, "good.md", "docs/constitution.md")
    _write_spec(tmp_path, "nested/bad.md", "/home/synthetic/docs")
    _write_spec(tmp_path, "notes.txt", "/home/synthetic/docs")
    _validate_specs(tmp_path)


def test_empty_spec_set_fails(tmp_path):
    with pytest.raises(AssertionError, match=r"No docs/specs/\*\.md files found"):
        _validate_specs(tmp_path)
