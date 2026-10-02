"""Release publication fails closed before exposing unverified downloads."""
import importlib.util
import json
from pathlib import Path
import subprocess

import pytest


COMMIT = "a" * 40
SUBMISSION = "01234567-89ab-cdef-0123-456789abcdef"
VERSION = "0.1.0"
TAG = "v" + VERSION
RELEASE_ID = 401560592
DRAFT_TAG = "untagged-ab9043a45ed932b9fad7"
PRIVATE = "synthetic-authentication-secret"


@pytest.fixture
def publisher(monkeypatch, tmp_path):
    path = Path(__file__).resolve().parents[1] / ".github/scripts/publish_release.py"
    spec = importlib.util.spec_from_file_location("release_publication", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    source = tmp_path / "source"
    source.mkdir()
    (source / "pyproject.toml").write_text('[project]\nversion = "0.1.0"\n')
    monkeypatch.setattr(module, "ROOT", source)
    monkeypatch.setenv("GITHUB_REPOSITORY", module.REPOSITORY)
    monkeypatch.setenv("GITHUB_SHA", COMMIT)
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    return module


@pytest.fixture
def assets(publisher, tmp_path):
    directory = tmp_path / "dist"
    directory.mkdir()
    app = {
        "signing": "developer_id", "hardened_runtime": True,
        "secure_timestamp": True, "notarization": "accepted",
        "notarization_ticket_stapled": True, "notary_submission_id": SUBMISSION,
        "source_commit": COMMIT, "source_tree_dirty": False,
    }
    binary = directory / f"mcpme-{VERSION}-arm64.dmg"
    binary.write_bytes(b"synthetic signed dmg")
    receipt = {
        "schema_version": 1, "distribution": "notarized_drag_install_dmg",
        "notarization": "accepted", "notary_submission_id": SUBMISSION,
        "version": VERSION, "build": "1", "minimum_macos": "26.0", "architecture": "arm64",
        "app_build": app, "checks": dict.fromkeys(publisher.DMG_CHECKS, True),
        "sha256": publisher.digest(binary), "bytes": binary.stat().st_size,
        "disk_image_signed": True, "notarization_ticket_stapled": True,
    }
    binary.with_name(binary.name + ".build.json").write_text(json.dumps(receipt))
    return directory


def change_receipt(directory, change):
    path = directory / f"mcpme-{VERSION}-arm64.dmg.build.json"
    value = json.loads(path.read_text())
    change(value)
    path.write_text(json.dumps(value))


def http_result(status, body=None, *, returncode=None):
    return subprocess.CompletedProcess(
        ["gh"], (0 if 200 <= status < 300 else 1) if returncode is None else returncode,
        stdout=f"HTTP/2.0 {status} Status\nContent-Type: application/json\n\n{json.dumps(body if body is not None else {})}",
        stderr=PRIVATE,
    )


def install_github(monkeypatch, publisher, directory, *, remote_change=None, tag_change=None,
                   existing_draft=False, version=VERSION, pull_requests=None,
                   notes_assertion=None):
    calls = []
    drafted = existing_draft
    public = False
    tagged = existing_draft
    draft_readback = False
    tag = "v" + version

    def run(*arguments):
        nonlocal drafted, public, tagged, draft_readback
        calls.append(arguments)
        if arguments[0] == "api":
            endpoint = next(value for value in arguments if value.startswith("repos/"))
            # Release-note discovery is read-only and occurs before tag creation.
            if "/releases?" in endpoint:
                return http_result(200, [])
            if "/pulls?" in endpoint:
                return http_result(200, pull_requests or [])
            if "/commits?" in endpoint:
                return http_result(200, [{"sha": COMMIT}])
            if "POST" in arguments:
                assert not tagged
                assert "ref=refs/tags/" + tag in arguments and "sha=" + COMMIT in arguments
                tagged = True
                return http_result(201)
            if "PATCH" in arguments:
                assert endpoint == f"repos/{publisher.REPOSITORY}/releases/{RELEASE_ID}"
                assert drafted and not public and draft_readback
                # gh's typed field emits a JSON boolean rather than string "false".
                assert "draft=false" in arguments
                assert ("--field" in arguments or "-F" in arguments)
                public = True
                return http_result(200)
            if "git/ref/tags/" in endpoint and tagged:
                value = {"ref": "refs/tags/" + tag, "object": {"type": "commit", "sha": COMMIT}}
                if drafted and tag_change:
                    tag_change(value)
                return http_result(200, value)
            if not drafted:
                return http_result(404)
            if endpoint == publisher.release_endpoint(tag) and not public:
                # GitHub's tag endpoint cannot resolve an unpublished release.
                return http_result(404)
            assert endpoint == f"repos/{publisher.REPOSITORY}/releases/{RELEASE_ID}"
            remote = {
                "id": RELEASE_ID,
                "tag_name": tag, "target_commitish": COMMIT,
                "draft": not public, "prerelease": False,
                "html_url": f"https://github.com/{publisher.REPOSITORY}/releases/tag/{tag if public else DRAFT_TAG}",
                "assets": [
                    {"name": path.name, "size": path.stat().st_size, "state": "uploaded",
                     "digest": "sha256:" + publisher.digest(path)}
                    for path in directory.iterdir()
                ],
            }
            if remote_change:
                remote_change(remote)
            if not public:
                draft_readback = True
            return http_result(200, remote)
        assert arguments[0] == "release"
        if arguments[1] == "create":
            assert not drafted
            assert "--draft" in arguments
            assert "--verify-tag" in arguments
            assert arguments[arguments.index("--target") + 1] == COMMIT
            assert arguments[arguments.index("--title") + 1] == "mcpme " + version
            notes = Path(arguments[arguments.index("--notes-file") + 1]).read_text()
            assert "DMG:" in notes and "ZIP" not in notes and COMMIT in notes
            assert "SHA-256 hashes for the DMG and its build receipt" in notes
            if notes_assertion:
                notes_assertion(notes)
            drafted = True
        elif arguments[1] == "upload":
            assert drafted and not public
            assert "--clobber" not in arguments
            names = {Path(argument).name for argument in arguments[5:]}
            assert names == {path.name for path in directory.iterdir()}
        elif arguments[1] == "view":
            assert drafted
            assert arguments[2] == tag
            assert arguments[arguments.index("--repo") + 1] == publisher.GH_REPOSITORY
            assert arguments[arguments.index("--json") + 1] == "databaseId"
            return subprocess.CompletedProcess(["gh"], 0, json.dumps({"databaseId": RELEASE_ID}), PRIVATE)
        else:
            pytest.fail("unexpected mutation")
        return subprocess.CompletedProcess(["gh"], 0, PRIVATE, PRIVATE)

    monkeypatch.setattr(publisher, "command", run)
    return calls


def stable_release(version):
    return {"tag_name": "v" + version, "draft": False, "prerelease": False}


def test_previous_release_tag_uses_latest_published_at_not_version_order(publisher, monkeypatch):
    releases = [
        {**stable_release("9.9.9"), "published_at": "2026-01-01T00:00:00Z"},
        {**stable_release("1.2.3"), "published_at": "2026-02-01T00:00:00Z"},
        {**stable_release("2.0.0"), "published_at": "2026-03-01T00:00:00Z"},
    ]
    monkeypatch.setattr(publisher, "api_response", lambda endpoint, **kwargs: releases)
    assert publisher.previous_release_tag("v3.0.0") == "v2.0.0"


def test_previous_release_tag_reads_multiple_release_pages(publisher, monkeypatch):
    first = [{**stable_release(f"1.0.{index}"), "published_at": "2026-01-01T00:00:00Z"}
             for index in range(100)]
    second = [{**stable_release("2.0.0"), "published_at": "2026-02-01T00:00:00Z"}]
    calls = []

    def read(endpoint, **kwargs):
        calls.append(endpoint)
        return first if endpoint.endswith("page=1") else second

    monkeypatch.setattr(publisher, "api_response", read)
    assert publisher.previous_release_tag("v3.0.0") == "v2.0.0"
    assert len(calls) == 2


def test_release_commits_compare_paginates_and_checks_complete_range(publisher, monkeypatch):
    values = [{"sha": f"{index:040x}"} for index in range(100)]
    values.append({"sha": COMMIT})
    base = "b" * 40
    responses = [
        {"status": "ahead", "total_commits": 101,
         "base_commit": {"sha": base}, "merge_base_commit": {"sha": base},
         "commits": values[:100]},
        {"status": "ahead", "total_commits": 101,
         "base_commit": {"sha": base}, "merge_base_commit": {"sha": base},
         "commits": values[100:]},
    ]
    monkeypatch.setattr(publisher, "api", lambda endpoint, **kwargs: responses.pop(0))
    result = publisher.release_commits("v0.1.0", COMMIT)
    assert len(result) == 101 and COMMIT in result


@pytest.mark.parametrize("status", ["behind", "diverged"])
def test_release_commits_rejects_non_ancestor_comparison(publisher, monkeypatch, status):
    monkeypatch.setattr(publisher, "api", lambda endpoint, **kwargs: {
        "status": status, "total_commits": 0, "base_commit": {"sha": "b" * 40},
        "merge_base_commit": {"sha": "b" * 40}, "commits": [],
    })
    with pytest.raises(publisher.PublicationError, match="release_notes_range_invalid"):
        publisher.release_commits("v0.1.0", COMMIT)


@pytest.mark.parametrize("commits", [[{"sha": COMMIT}], [{"sha": COMMIT}, {"sha": COMMIT}]])
def test_release_commits_rejects_truncated_or_duplicate_compare_results(publisher, monkeypatch, commits):
    base = "b" * 40
    monkeypatch.setattr(publisher, "api", lambda endpoint, **kwargs: {
        "status": "ahead", "total_commits": 2, "base_commit": {"sha": base},
        "merge_base_commit": {"sha": base}, "commits": commits,
    })
    with pytest.raises(publisher.PublicationError, match="release_notes_commit_list_invalid"):
        publisher.release_commits("v0.1.0", COMMIT)


def test_first_release_commit_history_paginates_and_identical_range_is_empty(publisher, monkeypatch):
    pages = [[{"sha": f"{index:040x}"} for index in range(100)], [{"sha": COMMIT}]]
    monkeypatch.setattr(publisher, "api_response", lambda endpoint, **kwargs: pages.pop(0))
    result = publisher.release_commits(None, COMMIT)
    assert len(result) == 101 and COMMIT in result
    monkeypatch.setattr(publisher, "api", lambda endpoint, **kwargs: {
        "status": "identical", "total_commits": 0,
        "base_commit": {"sha": COMMIT}, "merge_base_commit": {"sha": COMMIT},
        "commits": [],
    })
    assert publisher.release_commits("v0.1.0", COMMIT) == set()


def test_pull_request_notes_reads_second_all_pr_page_in_created_order(publisher, monkeypatch):
    target = "b" * 40
    monkeypatch.setattr(publisher, "release_commits", lambda previous, current: {target})
    first = [{"number": index + 10, "title": "other", "merged_at": None,
              "merge_commit_sha": target} for index in range(100)]
    second = [{"number": 2, "title": "included", "merged_at": "2026-01-01T00:00:00Z",
               "merge_commit_sha": target, "body": "description"}]
    calls = []

    def read(endpoint, **kwargs):
        calls.append(endpoint)
        return first if endpoint.endswith("page=1") else second

    monkeypatch.setattr(publisher, "api_response", read)
    notes = publisher.pull_request_notes("v0.1.0", target)
    assert "included (#2)" in notes and "description" in notes
    assert all("state=all" in endpoint and "sort=created" in endpoint
               and "direction=asc" in endpoint for endpoint in calls)


def test_pull_request_notes_rejects_invalid_selected_merge_timestamp(publisher, monkeypatch):
    target = "b" * 40
    monkeypatch.setattr(publisher, "release_commits", lambda previous, current: {target})
    monkeypatch.setattr(publisher, "api_response", lambda endpoint, **kwargs: [{
        "number": 3, "title": "Bad timestamp", "merged_at": "not-a-timestamp",
        "merge_commit_sha": target, "body": "body",
    }])
    with pytest.raises(publisher.PublicationError, match="release_notes_pull_request_list_invalid"):
        publisher.pull_request_notes("v0.1.0", target)


def test_pull_request_notes_filters_range_and_preserves_ordered_bodies(publisher, monkeypatch):
    commit = "b" * 40
    release_commits = {commit, "c" * 40}
    prs = [
        {"number": 7, "title": "Later", "html_url": "https://github.com/p/7",
         "merged_at": "2026-02-02T00:00:00Z", "merge_commit_sha": commit,
         "body": "line 1\n\nline 2"},
        {"number": 5, "title": "Earlier", "html_url": "https://github.com/p/5",
         "merged_at": "2026-02-01T00:00:00Z", "merge_commit_sha": "c" * 40,
         "body": None},
        {"number": 9, "title": "Excluded", "html_url": "https://github.com/p/9",
         "merged_at": "2026-02-03T00:00:00Z", "merge_commit_sha": "d" * 40,
         "body": "outside"},
        {"number": 10, "title": "Unmerged", "html_url": "https://github.com/p/10",
         "merged_at": None, "merge_commit_sha": commit, "body": "outside"},
    ]
    monkeypatch.setattr(publisher, "release_commits", lambda previous, current: release_commits)
    monkeypatch.setattr(publisher, "api_response", lambda endpoint, **kwargs: prs)
    notes = publisher.pull_request_notes("v0.1.0", commit)
    assert notes.index("#5") < notes.index("#7")
    assert "No description was provided." in notes
    assert "> line 1" in notes and "> line 2" in notes
    assert "#9" not in notes and "#10" not in notes


def test_first_release_with_no_matching_pull_requests_has_explicit_message(publisher, monkeypatch):
    monkeypatch.setattr(publisher, "release_commits", lambda previous, current: {COMMIT})
    monkeypatch.setattr(publisher, "api_response", lambda endpoint, **kwargs: [])
    assert "No pull requests were merged in this commit range." in (
        publisher.pull_request_notes(None, COMMIT)
    )


def test_publish_fails_before_tag_creation_when_release_notes_lookup_fails(
        publisher, assets, monkeypatch):
    calls = []

    def run(*arguments):
        calls.append(arguments)
        endpoint = next((value for value in arguments if value.startswith("repos/")), "")
        if "/releases?" in endpoint:
            return http_result(500)
        return http_result(404)

    monkeypatch.setattr(publisher, "command", run)
    with pytest.raises(publisher.PublicationError):
        publisher.publish(assets)
    assert not any("POST" in call for call in calls)


@pytest.mark.parametrize("bump,expected", [
    ("major", "1.0.0"), ("minor", "0.2.0"), ("patch", "0.1.1"),
])
def test_prepare_bumps_published_version_without_changing_source(
        publisher, monkeypatch, tmp_path, capsys, bump, expected):
    source = publisher.ROOT / "pyproject.toml"
    original = source.read_bytes()
    output = tmp_path / "github-output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    calls = []

    def run(*arguments):
        calls.append(arguments)
        if "/releases?" in arguments[-1]:
            return http_result(200, [stable_release(VERSION)])
        assert arguments[-1].endswith("v" + expected)
        return http_result(404)

    monkeypatch.setattr(publisher, "command", run)
    publisher.prepare(bump)
    assert output.read_text() == f"version={expected}\ntag=v{expected}\n"
    assert source.read_bytes() == original
    assert all("GET" in call for call in calls)
    assert capsys.readouterr() == ("release_preflight_ready\n", "")


@pytest.mark.parametrize("baseline,published,bump,expected", [
    ("0.1.0", [], "minor", "0.2.0"),
    ("3.4.5", ["1.2.3"], "patch", "3.4.6"),
    ("0.1.0", ["2.9.9", "2.10.2", "1.99.99"], "minor", "2.11.0"),
    ("9.9.9", [], "major", "10.0.0"),
    ("9.9.9", [], "minor", "9.10.0"),
    ("9.9.9", [], "patch", "9.9.10"),
])
def test_bump_uses_highest_numeric_source_or_published_version(
        publisher, monkeypatch, baseline, published, bump, expected):
    monkeypatch.setattr(publisher, "command", lambda *args: http_result(
        200, [stable_release(version) for version in published],
    ))
    assert publisher.bumped_version(baseline, bump) == expected


def test_release_selection_reads_all_pages_and_ignores_non_stable_tags(publisher, monkeypatch):
    first = [stable_release(f"0.0.{patch}") for patch in range(100)]
    second = [stable_release("12.3.4"),
              {**stable_release("99.0.0"), "draft": True},
              {**stable_release("99.0.0"), "prerelease": True},
              stable_release("99.0.0-alpha"), stable_release("01.0.0"),
              {"tag_name": "unrelated-tag", "draft": False, "prerelease": False}]
    calls = []

    def run(*arguments):
        calls.append(arguments)
        endpoint = arguments[-1]
        if endpoint.endswith("page=1"):
            return http_result(200, first)
        assert endpoint.endswith("page=2")
        return http_result(200, second)

    monkeypatch.setattr(publisher, "command", run)
    assert publisher.bumped_version(VERSION, "minor") == "12.4.0"
    assert len(calls) == 2


@pytest.mark.parametrize("bump", ["", "Major", "other", "minor\nunsafe"])
def test_invalid_bump_fails_before_github_or_output_writes(publisher, monkeypatch, tmp_path, bump):
    output = tmp_path / "github-output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    monkeypatch.setattr(publisher, "command", lambda *args: pytest.fail("unexpected GitHub access"))
    with pytest.raises(publisher.PublicationError, match="release_bump_invalid"):
        publisher.prepare(bump)
    assert not output.exists()


@pytest.mark.parametrize("response", [{}, [None], [{"tag_name": "v0.2.0"}],
                                      [{**stable_release("0.2.0"), "draft": "false"}]])
def test_invalid_release_list_fails_closed(publisher, monkeypatch, response):
    monkeypatch.setattr(publisher, "command", lambda *args: http_result(200, response))
    with pytest.raises(publisher.PublicationError, match="release_github_release_list_invalid"):
        publisher.prepare("minor")


@pytest.mark.parametrize("status", [401, 403, 429, 500])
def test_release_selection_does_not_treat_api_failures_as_no_releases(publisher, monkeypatch, status):
    monkeypatch.setattr(publisher, "command", lambda *args: http_result(status))
    with pytest.raises(publisher.PublicationError, match="release_github_api_failed"):
        publisher.prepare("minor")


@pytest.mark.parametrize("existing", ["release", "tag"])
def test_selected_version_collision_prevents_output(publisher, monkeypatch, tmp_path, existing):
    output = tmp_path / "github-output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))

    def run(*arguments):
        if "/releases?" in arguments[-1]:
            return http_result(200, [stable_release(VERSION)])
        assert arguments[-1].endswith("v0.2.0")
        present = existing == "release" or "/git/ref/" in arguments[-1]
        return http_result(200, {"existing": True}) if present else http_result(404)

    monkeypatch.setattr(publisher, "command", run)
    with pytest.raises(publisher.PublicationError, match="already_exists"):
        publisher.prepare("minor")
    assert not output.exists()


@pytest.mark.parametrize("version", ["0.2", "v0.2.0", "0.2.0-alpha", "00.2.0", "0.2.0\nunsafe"])
def test_invalid_explicit_release_version_blocks_before_github(publisher, assets, monkeypatch, version):
    monkeypatch.setattr(publisher, "command", lambda *args: pytest.fail("unexpected GitHub access"))
    with pytest.raises(publisher.PublicationError, match="release_version_invalid"):
        publisher.publish(assets, version)


@pytest.mark.parametrize("operation", ["publish", "resume"])
def test_explicit_version_is_used_for_asset_and_release_verification(
        publisher, assets, monkeypatch, operation):
    selected = "0.2.0"
    binary = assets / f"mcpme-{VERSION}-arm64.dmg"
    receipt = binary.with_name(binary.name + ".build.json")
    value = json.loads(receipt.read_text())
    value["version"] = selected
    destination = assets / f"mcpme-{selected}-arm64.dmg"
    binary.rename(destination)
    receipt.unlink()
    destination.with_name(destination.name + ".build.json").write_text(json.dumps(value))
    calls = install_github(monkeypatch, publisher, assets, version=selected,
                           existing_draft=operation == "resume")
    if operation == "publish":
        result = publisher.publish(assets, selected)
    else:
        result = publisher.resume(assets, RELEASE_ID, selected)
    assert result.endswith("/v" + selected)
    assert (publisher.ROOT / "pyproject.toml").read_text() == '[project]\nversion = "0.1.0"\n'
    assert sum("PATCH" in call for call in calls) == 1


def test_racing_tag_creation_cannot_publish(publisher, assets, monkeypatch):
    calls = []

    def run(*arguments):
        calls.append(arguments)
        endpoint = next((value for value in arguments if value.startswith("repos/")), "")
        if "/releases?" in endpoint or "/pulls?" in endpoint:
            return http_result(200, [])
        if "/commits?" in endpoint:
            return http_result(200, [{"sha": COMMIT}])
        return http_result(422 if "POST" in arguments else 404)

    monkeypatch.setattr(publisher, "command", run)
    with pytest.raises(publisher.PublicationError, match="release_github_command_failed"):
        publisher.publish(assets)
    assert sum("POST" in call for call in calls) == 1
    assert not any(call[0] == "release" or "PATCH" in call for call in calls)


def test_prepare_outputs_canonical_version_only_after_preflight(publisher, monkeypatch, tmp_path, capsys):
    calls = []
    monkeypatch.setattr(publisher, "command", lambda *args: calls.append(args) or http_result(404))
    output = tmp_path / "github-output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    publisher.prepare()
    assert output.read_text() == "version=0.1.0\ntag=v0.1.0\n"
    assert len(calls) == 2 and all(call[0] == "api" for call in calls)
    assert capsys.readouterr() == ("release_preflight_ready\n", "")


@pytest.mark.parametrize("version", ["01.0.0", "0.1", "0.1.0-alpha", "v0.1.0", "0.1.0\nunsafe"])
def test_invalid_version_has_no_github_or_output_writes(publisher, monkeypatch, tmp_path, version):
    (publisher.ROOT / "pyproject.toml").write_text('[project]\nversion = ' + json.dumps(version) + '\n')
    output = tmp_path / "github-output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    monkeypatch.setattr(publisher, "command", lambda *args: pytest.fail("unexpected GitHub access"))
    with pytest.raises(publisher.PublicationError, match="release_project_version_invalid"):
        publisher.prepare()
    assert not output.exists()


@pytest.mark.parametrize("variable,value", [
    ("GITHUB_REPOSITORY", "someone-else/mcpme"), ("GITHUB_SHA", "main"),
    ("GITHUB_SHA", "A" * 40), ("GITHUB_SHA", "a" * 39),
])
def test_invalid_source_context_is_blocked(publisher, monkeypatch, variable, value):
    monkeypatch.setenv(variable, value)
    monkeypatch.setattr(publisher, "command", lambda *args: pytest.fail("unexpected GitHub access"))
    with pytest.raises(publisher.PublicationError):
        publisher.prepare()


@pytest.mark.parametrize("existing", ["release", "tag"])
def test_existing_version_never_overwritten(publisher, monkeypatch, existing):
    calls = []

    def run(*arguments):
        calls.append(arguments)
        present = existing == "release" or "git/ref/tags/" in arguments[-1]
        return http_result(200, {"existing": True}) if present else http_result(404)

    monkeypatch.setattr(publisher, "command", run)
    with pytest.raises(publisher.PublicationError, match="already_exists"):
        publisher.prepare()
    assert all(call[0] == "api" for call in calls)


@pytest.mark.parametrize("status", [401, 403, 429, 500])
def test_http_failures_are_not_treated_as_absent(publisher, monkeypatch, status, capsys):
    monkeypatch.setattr(publisher, "command", lambda *args: http_result(status))
    with pytest.raises(publisher.PublicationError, match="release_github_api_failed"):
        publisher.prepare()
    assert capsys.readouterr() == ("", "")


def test_transport_failure_without_http_response_fails_closed(publisher, monkeypatch):
    monkeypatch.setattr(publisher, "command", lambda *args: subprocess.CompletedProcess(["gh"], 1, "", PRIVATE))
    with pytest.raises(publisher.PublicationError, match="release_github_api_failed"):
        publisher.prepare()


@pytest.mark.parametrize("change", [
    lambda receipt: receipt.update(notarization="rejected"),
    lambda receipt: receipt["app_build"].update(notarization="not_requested"),
    lambda receipt: receipt["app_build"].update(signing="ad_hoc"),
    lambda receipt: receipt["app_build"].update(source_commit="b" * 40),
    lambda receipt: receipt["app_build"].update(source_tree_dirty=True),
    lambda receipt: receipt["app_build"].update(notarization_ticket_stapled=False),
    lambda receipt: receipt["app_build"].update(hardened_runtime=False),
    lambda receipt: receipt["app_build"].update(secure_timestamp=False),
    lambda receipt: receipt["app_build"].update(version="9.9.9"),
    lambda receipt: receipt["checks"].update(copied_app_gatekeeper_assessment=False),
    lambda receipt: receipt["checks"].update(copied_app_signature=False),
    lambda receipt: receipt.update(disk_image_signed=False),
    lambda receipt: receipt.update(notarization_ticket_stapled=False),
])
def test_invalid_receipts_block_before_any_remote_write(publisher, assets, monkeypatch, change):
    change_receipt(assets, change)
    monkeypatch.setattr(publisher, "command", lambda *args: pytest.fail("unexpected GitHub access"))
    with pytest.raises(publisher.PublicationError, match="release_asset_receipt_invalid"):
        publisher.publish(assets)
    assert not (assets / "SHA256SUMS.txt").exists()


@pytest.mark.parametrize("field,value", [("sha256", "0" * 64), ("bytes", 1), ("version", "0.2.0")])
def test_receipt_hash_size_and_version_must_match(publisher, assets, monkeypatch, field, value):
    change_receipt(assets, lambda receipt: receipt.update({field: value}))
    monkeypatch.setattr(publisher, "command", lambda *args: pytest.fail("unexpected GitHub access"))
    with pytest.raises(publisher.PublicationError, match="release_asset_receipt"):
        publisher.publish(assets)


@pytest.mark.parametrize("unexpected", [
    "other.dmg", "debug.log", f"mcpme-{VERSION}-arm64.zip",
    f"mcpme-{VERSION}-arm64.zip.build.json",
])
def test_unrelated_files_cannot_be_published(publisher, assets, unexpected):
    (assets / unexpected).write_text("unrelated")
    with pytest.raises(publisher.PublicationError, match="release_asset_set_invalid"):
        publisher.validated_assets(assets, VERSION, COMMIT)


@pytest.mark.parametrize("suffix", ["dmg", "dmg.build.json"])
def test_missing_or_symlinked_assets_are_rejected(publisher, assets, tmp_path, suffix):
    binary = assets / f"mcpme-{VERSION}-arm64.{suffix}"
    target = tmp_path / f"outside.{suffix}"
    binary.rename(target)
    with pytest.raises(publisher.PublicationError, match="release_asset_set_invalid"):
        publisher.validated_assets(assets, VERSION, COMMIT)
    binary.symlink_to(target)
    with pytest.raises(publisher.PublicationError, match="release_asset_set_invalid"):
        publisher.validated_assets(assets, VERSION, COMMIT)


def test_checksum_manifest_covers_dmg_and_receipt(publisher, assets):
    paths = publisher.validated_assets(assets, VERSION, COMMIT)
    manifest = (assets / "SHA256SUMS.txt").read_text().splitlines()
    assert {path.name for path in paths} == {
        f"mcpme-{VERSION}-arm64.dmg", f"mcpme-{VERSION}-arm64.dmg.build.json", "SHA256SUMS.txt",
    }
    assert len(manifest) == 2
    for line in manifest:
        checksum, filename = line.split("  ")
        assert checksum == publisher.digest(assets / filename)
    assert publisher.validated_assets(assets, VERSION, COMMIT) == paths


def test_existing_checksum_manifest_cannot_be_overwritten(publisher, assets):
    sums = assets / "SHA256SUMS.txt"
    sums.write_text("existing invalid checksum file")
    with pytest.raises(publisher.PublicationError, match="release_checksum_manifest_invalid"):
        publisher.validated_assets(assets, VERSION, COMMIT)
    assert sums.read_text() == "existing invalid checksum file"


def test_publish_is_draft_until_exact_assets_and_hashes_are_verified(publisher, assets, monkeypatch, capsys):
    calls = install_github(monkeypatch, publisher, assets)
    assert publisher.publish(assets) == f"https://github.com/{publisher.REPOSITORY}/releases/tag/{TAG}"
    mutation = next(index for index, call in enumerate(calls) if "PATCH" in call)
    assert any(call[:2] == ("release", "view") for call in calls[:mutation])
    assert any(f"repos/{publisher.REPOSITORY}/releases/{RELEASE_ID}" in call
               for call in calls[:mutation])
    assert any(f"repos/{publisher.REPOSITORY}/releases/{RELEASE_ID}" in call
               for call in calls[mutation + 1:])
    assert not any(call[:2] == ("release", "edit") for call in calls)
    assert capsys.readouterr() == ("", "")


def test_publish_places_merged_pr_body_in_release_notes_file(publisher, assets, monkeypatch):
    pull = {"number": 42, "title": "Installer improvements", "merged_at": "2026-01-01T00:00:00Z",
            "merge_commit_sha": COMMIT, "body": "* graceful restart\n* preserves settings"}
    calls = install_github(
        monkeypatch, publisher, assets, pull_requests=[pull],
        notes_assertion=lambda notes: (
            assert_in_notes(notes, "Installer improvements (#42)"),
            assert_in_notes(notes, "> * graceful restart"),
        ),
    )
    assert publisher.publish(assets) == f"https://github.com/{publisher.REPOSITORY}/releases/tag/{TAG}"
    assert any(call[:2] == ("release", "create") for call in calls)


def assert_in_notes(notes, expected):
    assert expected in notes


@pytest.mark.parametrize("change,diagnostic", [
    (lambda release: release["assets"][0].update(digest="sha256:" + "0" * 64), "digest_mismatch"),
    (lambda release: release["assets"][0].update(size=0), "digest_mismatch"),
    (lambda release: release["assets"].pop(), "asset_set_invalid"),
    (lambda release: release["assets"][0].update(name=f"mcpme-{VERSION}-arm64.zip"), "asset_set_invalid"),
    (lambda release: release["assets"].append(dict(release["assets"][0],
                                                name=f"mcpme-{VERSION}-arm64.zip")), "asset_set_invalid"),
    (lambda release: release.update(target_commitish="main"), "metadata_invalid"),
    (lambda release: release.update(draft=False), "metadata_invalid"),
])
def test_failed_remote_verification_keeps_draft_unpublished(publisher, assets, monkeypatch, change, diagnostic):
    calls = install_github(monkeypatch, publisher, assets, remote_change=change)
    with pytest.raises(publisher.PublicationError, match=diagnostic):
        publisher.publish(assets)
    assert any(call[:2] == ("release", "create") for call in calls)
    assert not any("PATCH" in call or call[:2] == ("release", "edit") for call in calls)
    assert not any("--clobber" in call or "delete" in call for call in calls)


def test_changed_tag_commit_keeps_draft_unpublished(publisher, assets, monkeypatch):
    calls = install_github(monkeypatch, publisher, assets,
                           tag_change=lambda value: value["object"].update(sha="b" * 40))
    with pytest.raises(publisher.PublicationError, match="release_tag_commit_mismatch"):
        publisher.publish(assets)
    assert any(call[:2] == ("release", "create") for call in calls)
    assert not any("PATCH" in call or call[:2] == ("release", "edit") for call in calls)


def test_draft_can_have_an_untagged_url_and_unresolvable_tag_endpoint(publisher, assets, monkeypatch):
    calls = install_github(monkeypatch, publisher, assets, existing_draft=True)
    assert publisher.api(publisher.release_endpoint(TAG), missing_allowed=True) is None
    assert publisher.resolve_release_id(TAG) == RELEASE_ID
    paths = publisher.validated_assets(assets, VERSION, COMMIT)
    assert publisher.verified_release(TAG, COMMIT, paths, draft=True, release_id=RELEASE_ID) == (
        f"https://github.com/{publisher.REPOSITORY}/releases/tag/{TAG}"
    )
    assert all("POST" not in call and "PATCH" not in call for call in calls)


@pytest.mark.parametrize("database_id", [None, 0, -1, True, "401560592"])
def test_resolved_release_id_must_be_a_positive_integer(publisher, monkeypatch, database_id):
    monkeypatch.setattr(publisher, "command", lambda *args: subprocess.CompletedProcess(
        ["gh"], 0, json.dumps({"databaseId": database_id}), PRIVATE,
    ))
    with pytest.raises(publisher.PublicationError):
        publisher.resolve_release_id(TAG)


@pytest.mark.parametrize("change", [
    lambda value: value.update(id=RELEASE_ID + 1),
    lambda value: value.update(html_url=f"https://github.com/someone-else/mcpme/releases/tag/{DRAFT_TAG}"),
    lambda value: value.update(html_url="https://github.com/jshahbazi/mcpme/releases/tag/untagged-not-hex"),
])
def test_wrong_release_id_or_untrusted_draft_url_blocks_publication(publisher, assets, monkeypatch, change):
    calls = install_github(monkeypatch, publisher, assets, remote_change=change)
    with pytest.raises(publisher.PublicationError, match="metadata_invalid"):
        publisher.publish(assets)
    assert not any("PATCH" in call for call in calls)


def test_resume_validated_draft_publishes_by_id_without_creating_or_uploading(publisher, assets, monkeypatch):
    calls = install_github(monkeypatch, publisher, assets, existing_draft=True)
    assert publisher.resume(assets, RELEASE_ID) == f"https://github.com/{publisher.REPOSITORY}/releases/tag/{TAG}"
    assert sum("PATCH" in call for call in calls) == 1
    assert not any("POST" in call or call[:2] in (("release", "create"), ("release", "upload")) for call in calls)


@pytest.mark.parametrize("change,diagnostic", [
    (lambda value: value["assets"].pop(), "asset_set_invalid"),
    (lambda value: value["assets"][0].update(digest="sha256:" + "0" * 64), "digest_mismatch"),
    (lambda value: value.update(id=RELEASE_ID + 1), "metadata_invalid"),
    (lambda value: value.update(draft=False), "metadata_invalid"),
])
def test_resume_cannot_publish_incomplete_or_mismatched_draft(publisher, assets, monkeypatch, change, diagnostic):
    calls = install_github(monkeypatch, publisher, assets, existing_draft=True, remote_change=change)
    with pytest.raises(publisher.PublicationError, match=diagnostic):
        publisher.resume(assets, RELEASE_ID)
    assert not any("PATCH" in call or "POST" in call or call[0] == "release" for call in calls)


def test_resume_rejects_local_checksum_mismatch_before_github_access(publisher, assets, monkeypatch):
    (assets / "SHA256SUMS.txt").write_text("invalid downloaded checksum manifest")
    monkeypatch.setattr(publisher, "command", lambda *args: pytest.fail("unexpected GitHub access"))
    with pytest.raises(publisher.PublicationError, match="release_checksum_manifest_invalid"):
        publisher.resume(assets, RELEASE_ID)


def test_resume_rejects_wrong_tag_commit_without_mutating_draft(publisher, assets, monkeypatch):
    calls = install_github(monkeypatch, publisher, assets, existing_draft=True,
                           tag_change=lambda value: value["object"].update(sha="b" * 40))
    with pytest.raises(publisher.PublicationError, match="release_tag_commit_mismatch"):
        publisher.resume(assets, RELEASE_ID)
    assert not any("PATCH" in call or "POST" in call for call in calls)


def test_github_command_capture_never_emits_private_output(publisher, monkeypatch, capsys):
    def run(arguments, **kwargs):
        assert arguments == ["gh", "release", "create", TAG]
        assert kwargs["stdout"] == subprocess.PIPE and kwargs["stderr"] == subprocess.PIPE
        assert kwargs["stdin"] == subprocess.DEVNULL and kwargs["check"] is False
        assert kwargs["env"]["GH_HOST"] == "github.com"
        return subprocess.CompletedProcess(arguments, 1, PRIVATE, PRIVATE)

    monkeypatch.setattr(publisher.subprocess, "run", run)
    with pytest.raises(publisher.PublicationError, match="release_github_command_failed") as raised:
        publisher.checked_command("release", "create", TAG)
    assert PRIVATE not in repr(raised.value)
    assert capsys.readouterr() == ("", "")


def test_github_timeout_does_not_retain_command_output(publisher, monkeypatch):
    def run(*args, **kwargs):
        raise subprocess.TimeoutExpired(["gh", PRIVATE], 120, output=PRIVATE, stderr=PRIVATE)

    monkeypatch.setattr(publisher.subprocess, "run", run)
    with pytest.raises(publisher.PublicationError) as raised:
        publisher.command("release", "create", TAG)
    assert PRIVATE not in repr(raised.value)
    assert raised.value.__cause__ is None
