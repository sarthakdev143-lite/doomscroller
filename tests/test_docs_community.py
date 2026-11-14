"""Contract tests for repository community health files and issue/PR templates."""
from __future__ import annotations

from pathlib import Path

from tests.test_repo_hygiene import scan_text_for_secrets

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def test_security_policy_and_changelog_exist_and_cover_core_guarantees():
    security = PROJECT_ROOT / "SECURITY.md"
    changelog = PROJECT_ROOT / "CHANGELOG.md"
    assert security.is_file(), "SECURITY.md must exist at repo root"
    assert changelog.is_file(), "CHANGELOG.md must exist at repo root"

    sec_text = security.read_text(encoding="utf-8")
    for token in ("REVIEW_TOKEN", "127.0.0.1:8000", "8600", "scrub_secrets", "security/advisories"):
        assert token in sec_text, f"SECURITY.md missing expected guidance: {token!r}"
    assert not scan_text_for_secrets(sec_text)

    chg_text = changelog.read_text(encoding="utf-8")
    for token in ("Keep a Changelog", "serve_slim.py", "Sarvam", "Earshot", "channels.py"):
        assert token in chg_text, f"CHANGELOG.md missing milestone entry: {token!r}"
    assert not scan_text_for_secrets(chg_text)


def test_github_pr_and_issue_templates_are_valid_and_secret_free():
    pr_tpl = PROJECT_ROOT / ".github" / "PULL_REQUEST_TEMPLATE.md"
    issue_dir = PROJECT_ROOT / ".github" / "ISSUE_TEMPLATE"
    bug_yml = issue_dir / "bug_report.yml"
    feat_yml = issue_dir / "feature_request.yml"
    cfg_yml = issue_dir / "config.yml"

    for path in (pr_tpl, bug_yml, feat_yml, cfg_yml):
        assert path.is_file(), f"Missing GitHub community file: {path.relative_to(PROJECT_ROOT)}"
        assert not scan_text_for_secrets(path.read_text(encoding="utf-8"))

    pr_text = pr_tpl.read_text(encoding="utf-8")
    for invariant in ('pytest -q -m "not slow"', "numpy", "_guard", "winproc"):
        assert invariant in pr_text, f"PR template missing invariant check: {invariant!r}"

    try:
        import yaml  # type: ignore[import-untyped]
    except ImportError:
        yaml = None

    if yaml is not None:
        for form_path in (bug_yml, feat_yml):
            data = yaml.safe_load(form_path.read_text(encoding="utf-8"))
            assert isinstance(data, dict)
            assert data.get("name") and data.get("description")
            body = data.get("body")
            assert isinstance(body, list) and len(body) >= 3
        cfg = yaml.safe_load(cfg_yml.read_text(encoding="utf-8"))
        assert isinstance(cfg, dict) and "contact_links" in cfg
