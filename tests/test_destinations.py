import os
import sqlite3

import pytest

from jev_runtime.cli import main
from jev_runtime.config import load
from jev_runtime.destinations import MISSING_DESTINATION
from jev_runtime.judgment import FakeJudge
from jev_runtime.runtime import Runtime
from jev_runtime.sandbox import Sandbox
from jev_runtime.store import Store


@pytest.mark.parametrize("arguments", [[], ["setup"], ["onboard"], ["status"]])
def test_missing_destination_fails_before_prompts_or_writes(
    tmp_path, monkeypatch, capsys, arguments
):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("builtins.input", lambda _: pytest.fail("No prompt without a destination"))
    assert main(arguments) == 2
    assert capsys.readouterr().err == MISSING_DESTINATION + "\n"
    assert list(tmp_path.iterdir()) == []


def test_one_folder_deferred_onboarding_resumes_exact_intent(tmp_path, monkeypatch, capsys):
    work = tmp_path / "my work"
    work.mkdir()
    elsewhere = tmp_path / "unrelated"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    monkeypatch.setattr(
        "jev_runtime.cli.credential", lambda *a, **k: pytest.fail("No credentials needed")
    )
    answers = iter(["  Build my app, preserving my exact requirements.  ", "/done"])
    monkeypatch.setattr("builtins.input", lambda _: next(answers))
    assert main(["--output", str(work)]) == 0
    path = work / ".jev/ledger.sqlite"
    store = Store(path, readonly=True)
    session = store.records("onboarding_session")[0]
    assert session[1]["workspace"] == str(work / "project")
    assert (
        store.records("human_intent")[0][1]["text"]
        == "  Build my app, preserving my exact requirements.  "
    )
    assert not store.records("judgment")
    store.close()
    monkeypatch.setattr("builtins.input", lambda _: "/done")
    assert main(["--output", str(work)]) == 0
    store = Store(path, readonly=True)
    assert len(store.records("onboarding_session")) == 1
    assert len(store.records("human_intent")) == 1
    store.close()
    assert list(elsewhere.iterdir()) == []
    assert not (work / ".jev/providers.toml").exists()
    assert "--output" in capsys.readouterr().out


def test_setup_preserves_old_settings_without_model_calls(tmp_path, monkeypatch):
    settings = tmp_path / ".jev/providers.toml"
    settings.parent.mkdir()
    original = b'[onboarding]\nprovider = "unconfigured"\n'
    settings.write_bytes(original)
    monkeypatch.setattr("builtins.input", lambda _: "")
    assert main(["--output", str(tmp_path), "setup"]) == 0
    assert load(settings)["onboarding"]["provider"] == "unconfigured"
    assert load(settings)["agent"]["provider"] == "unconfigured"
    backups = list(settings.parent.glob("providers.toml.backup-*"))
    assert len(backups) == 1 and backups[0].read_bytes() == original
    assert not (settings.parent / "ledger.sqlite").exists()


@pytest.mark.parametrize("command", ["status", "inspect", "complete"])
def test_read_only_inspection_never_creates_missing_history(tmp_path, capsys, command):
    arguments = ["--output", str(tmp_path), command]
    if command == "complete":
        arguments.append("missing")
    assert main(arguments) == 2
    assert "No saved history" in capsys.readouterr().err
    assert list(tmp_path.iterdir()) == []


def test_existing_legacy_history_is_opened_without_permission_or_byte_changes(tmp_path, capsys):
    path = tmp_path / "old history.sqlite"
    store = Store(path)
    identifier = store.record_intent("Original recorded instruction")
    store.close()
    path.chmod(0o400)
    before = path.read_bytes(), path.stat().st_mode, path.stat().st_mtime_ns
    assert main(["--state", str(path), "inspect", "--kind", "human_intent"]) == 0
    assert identifier in capsys.readouterr().out
    assert (path.read_bytes(), path.stat().st_mode, path.stat().st_mtime_ns) == before
    store = Store(path, readonly=True)
    with pytest.raises(sqlite3.OperationalError):
        store.record_intent("Cannot append via a read-only connection")
    store.close()


def test_read_only_history_observes_new_writer_events(tmp_path):
    path = tmp_path / "history.sqlite"
    writer = Store(path)
    reader = Store(path, readonly=True)
    identifier = writer.record_intent("New committed instruction")
    assert reader.get(identifier, "human_intent")["text"] == "New committed instruction"
    reader.close()
    writer.close()


def test_folder_must_exist_and_cannot_escape_through_metadata_link(tmp_path, capsys):
    assert main(["--output", str(tmp_path / "missing"), "setup"]) == 2
    assert not (tmp_path / "missing").exists()
    outside = tmp_path / "outside"
    outside.mkdir()
    work = tmp_path / "work"
    work.mkdir()
    (work / ".jev").symlink_to(outside, target_is_directory=True)
    assert main(["--output", str(work), "setup"]) == 2
    assert "outside the selected folder" in capsys.readouterr().err
    assert list(outside.iterdir()) == []


def test_conflicting_paths_and_configuration_only_do_not_create_history(tmp_path, capsys):
    config = tmp_path / "providers.toml"
    config.write_text('[onboarding]\nprovider = "unconfigured"\n')
    assert main(["--config", str(config), "onboard"]) == 2
    assert MISSING_DESTINATION in capsys.readouterr().err
    assert main(["--output", str(tmp_path), "--state", str(tmp_path / "state"), "status"]) == 2
    assert not (tmp_path / "state").exists()
    assert not (tmp_path / "ledger.sqlite").exists()


def test_shared_directory_is_not_assumed_to_be_personal_output(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("jev_runtime.destinations.os.getuid", lambda: os.stat(tmp_path).st_uid + 1)
    assert main(["--output", str(tmp_path), "setup"]) == 2
    assert "owned by your calling account" in capsys.readouterr().err
    assert list(tmp_path.iterdir()) == []


def test_folder_layout_runs_real_worker_without_workspace_overlap(tmp_path, monkeypatch, capsys):
    availability = Sandbox().check()
    if not availability["available"]:
        pytest.skip(availability["reason"])
    project = tmp_path / "project"
    project.mkdir()
    path = tmp_path / ".jev/ledger.sqlite.demo"
    store = Store(path)
    runtime = Runtime(store, FakeJudge())
    source = store.record_intent("Write report.txt in my selected project.")
    proposal = runtime.propose(
        {
            "intent_id": source,
            "workspace": str(project),
            "actions": ["read", "write", "execute"],
            "shell_commands": [["python3"]],
            "required_evidence": ["report"],
            "checks": [{"requirement": "report", "kind": "file", "path": "report.txt"}],
        }
    )
    runtime.approve(proposal)
    store.close()
    monkeypatch.setattr("jev_runtime.cli.confirm_operation", lambda _: False)
    assert (
        main(
            [
                "--output",
                str(tmp_path),
                "--demo",
                "run",
                "--proposal",
                proposal,
                "--no-provider",
                "--",
                "python3",
                "-c",
                "from pathlib import Path; Path('report.txt').write_text('verified')",
            ]
        )
        == 0
    )
    store = Store(path, readonly=True)
    assert store.records("runtime_result")[-1][1]["success"] is True
    run = store.records("runtime_session")[-1][1]
    assert str(tmp_path / ".jev/workers") in run["directory"]
    assert not (project / "report.txt").exists()
    store.close()
    assert "--output" in capsys.readouterr().out


@pytest.mark.parametrize(
    "layout", ["folder_demo", "legacy_demo", "legacy_project", "legacy_workers"]
)
def test_all_effective_write_destinations_are_validated(tmp_path, layout):
    work = tmp_path / "work"
    work.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    external = outside / "preserved.sqlite"
    store = Store(external)
    store.record_intent("Must remain unchanged")
    store.close()
    before = external.read_bytes(), external.stat().st_mode
    if layout == "folder_demo":
        (work / ".jev").mkdir()
        (work / ".jev/ledger.sqlite.demo").symlink_to(external)
        argv = ["--output", str(work), "--demo", "onboard"]
    elif layout == "legacy_demo":
        (work / "ledger.sqlite.demo").symlink_to(external)
        argv = ["--state", str(work / "ledger.sqlite"), "--demo", "onboard"]
    else:
        (work / layout.removeprefix("legacy_")).symlink_to(outside, target_is_directory=True)
        argv = ["--state", str(work / "ledger.sqlite"), "onboard"]
    assert main(argv) == 2
    assert (external.read_bytes(), external.stat().st_mode) == before
    assert list(outside.iterdir()) == [external]


def test_reopening_folder_preserves_explicit_project_session(tmp_path, monkeypatch):
    work, project = tmp_path / "work", tmp_path / "existing-project"
    work.mkdir()
    project.mkdir()
    monkeypatch.setattr("builtins.input", lambda _: "/done")
    assert main(["--output", str(work), "onboard", "--workspace", str(project)]) == 0
    assert main(["--output", str(work)]) == 0
    store = Store(work / ".jev/ledger.sqlite", readonly=True)
    sessions = store.records("onboarding_session")
    assert len(sessions) == 1 and sessions[0][1]["workspace"] == str(project)
    assert not (work / "project").exists()
    store.close()


def test_auto_setup_validates_explicit_config_write_parent(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("jev_runtime.cli.sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("jev_runtime.cli.setup", lambda *a: pytest.fail("Unvalidated destination"))
    assert (
        main(
            [
                "--state",
                str(tmp_path / "history.sqlite"),
                "--config",
                str(tmp_path / "absent/settings.toml"),
                "onboard",
            ]
        )
        == 2
    )
    assert "Create the destination folder" in capsys.readouterr().err
    assert not (tmp_path / "absent").exists()


def test_export_validates_output_even_with_read_only_ledger(tmp_path, monkeypatch, capsys):
    path = tmp_path / "history.sqlite"
    Store(path).close()
    monkeypatch.setattr("jev_runtime.destinations.os.getuid", lambda: os.stat(tmp_path).st_uid + 1)
    assert (
        main(
            [
                "--state",
                str(path),
                "constitution",
                "export",
                "proposal",
                str(tmp_path / "export.json"),
            ]
        )
        == 2
    )
    assert "owned by your calling account" in capsys.readouterr().err
    assert not (tmp_path / "export.json").exists()


def test_legacy_staged_run_reuses_its_recorded_worker_directory(tmp_path, monkeypatch):
    from jev_runtime.supervisor import Supervisor

    availability = Sandbox().check()
    if not availability["available"]:
        pytest.skip(availability["reason"])
    project = tmp_path / "project"
    project.mkdir()
    state = tmp_path / "ledger.sqlite"
    store = Store(str(state) + ".demo")
    runtime = Runtime(store, FakeJudge())
    source = store.record_intent("Create a verified report.")
    proposal = runtime.propose(
        {
            "intent_id": source,
            "workspace": str(project),
            "actions": ["read", "write", "execute"],
            "shell_commands": [["python3"]],
            "required_evidence": ["report"],
            "checks": [
                {
                    "kind": "file",
                    "path": "report.txt",
                    "requirement": "report",
                    "contains": "verified",
                }
            ],
        }
    )
    runtime.approve(proposal)
    runner = Supervisor(runtime, tmp_path / "previous-worker-directory", confirm=lambda _: True)
    run = runner.run(
        proposal,
        ["python3", "-c", "from pathlib import Path; Path('report.txt').write_text('verified')"],
    )
    store.close()
    monkeypatch.setattr("jev_runtime.cli.confirm_operation", lambda _: True)
    assert main(["--state", str(state), "--demo", "apply", run]) == 0
    assert (project / "report.txt").read_text() == "verified"
    assert not (tmp_path / "workers").exists()


def test_setup_allows_separate_explicit_config_and_history_directories(tmp_path, monkeypatch):
    settings = tmp_path / "settings"
    settings.mkdir()
    history = tmp_path / "job/history.sqlite"
    monkeypatch.setattr("builtins.input", lambda _: "")
    assert (
        main(["--state", str(history), "--config", str(settings / "providers.toml"), "setup"]) == 0
    )
    assert (settings / "providers.toml").exists()
    assert not history.parent.exists()


def test_explicit_workspace_cannot_retarget_a_recorded_interview(tmp_path, monkeypatch, capsys):
    work, first, second = (tmp_path / name for name in ("work", "first", "second"))
    for path in (work, first, second):
        path.mkdir()
    monkeypatch.setattr("builtins.input", lambda _: "/done")
    assert main(["--output", str(work), "onboard", "--workspace", str(first)]) == 0
    store = Store(work / ".jev/ledger.sqlite", readonly=True)
    session = store.records("onboarding_session")[0][0]
    store.close()
    monkeypatch.setattr("builtins.input", lambda _: pytest.fail("Mismatched workspace prompted"))
    assert (
        main(["--output", str(work), "onboard", "--session", session, "--workspace", str(second)])
        == 2
    )
    assert "--workspace differs" in capsys.readouterr().err


def _folder_proposal(tmp_path, project=None):
    project = project or tmp_path / "project"
    project.mkdir()
    store = Store(tmp_path / ".jev/ledger.sqlite.demo")
    runtime = Runtime(store, FakeJudge())
    source = store.record_intent("Create report.txt in the selected project.")
    proposal = runtime.propose(
        {
            "intent_id": source,
            "workspace": str(project),
            "actions": ["read", "write", "execute"],
            "shell_commands": [["python3"]],
            "required_evidence": ["report"],
            "checks": [{"kind": "file", "path": "report.txt", "requirement": "report"}],
        }
    )
    runtime.approve(proposal)
    return store, runtime, proposal, project


def _change_observed_owner(monkeypatch, project):
    from pathlib import Path

    original = Path.stat

    def stat(path, *args, **kwargs):
        result = original(path, *args, **kwargs)
        if path == project:
            fields = list(result)
            fields[4] += 1
            return os.stat_result(fields)
        return result

    monkeypatch.setattr(Path, "stat", stat)


def test_selected_policy_missing_workspace_checked_before_worker(tmp_path, monkeypatch, capsys):
    work = tmp_path / "work"
    work.mkdir()
    store, _, proposal, project = _folder_proposal(work, tmp_path / "recorded-project")
    store.close()
    project.rmdir()
    monkeypatch.setattr("jev_runtime.cli.supervisor", lambda *a: pytest.fail("Worker initialized"))
    assert (
        main(
            [
                "--output",
                str(work),
                "--demo",
                "run",
                "--proposal",
                proposal,
                "--no-provider",
                "--",
                "python3",
                "-c",
                "pass",
            ]
        )
        == 2
    )
    assert "Create the destination folder" in capsys.readouterr().err


def test_selected_policy_rejects_conflicting_explicit_workspace(tmp_path, monkeypatch, capsys):
    store, _, proposal, _ = _folder_proposal(tmp_path)
    store.close()
    second = tmp_path / "second"
    second.mkdir()
    monkeypatch.setattr("jev_runtime.cli.supervisor", lambda *a: pytest.fail("Worker initialized"))
    assert (
        main(
            [
                "--output",
                str(tmp_path),
                "--demo",
                "run",
                "--proposal",
                proposal,
                "--workspace",
                str(second),
                "--no-provider",
                "--",
                "python3",
                "-c",
                "pass",
            ]
        )
        == 2
    )
    assert "--workspace differs" in capsys.readouterr().err


def test_publication_rechecks_actual_project_owner_after_review(tmp_path, monkeypatch):
    from jev_runtime.destinations import DestinationError
    from jev_runtime.supervisor import Supervisor

    availability = Sandbox().check()
    if not availability["available"]:
        pytest.skip(availability["reason"])
    store, runtime, proposal, project = _folder_proposal(tmp_path)
    runner = Supervisor(runtime, tmp_path / ".jev/workers", confirm=lambda _: True)
    run = runner.run(
        proposal,
        ["python3", "-c", "from pathlib import Path; Path('report.txt').write_text('verified')"],
    )
    original = runner._authorize

    def authorize(proposal, action, payload, **kwargs):
        result = original(proposal, action, payload, **kwargs)
        if action == "write":
            _change_observed_owner(monkeypatch, project)
        return result

    monkeypatch.setattr(runner, "_authorize", authorize)
    with pytest.raises(DestinationError, match="owned by your calling account"):
        runner.publish(run)
    assert not (project / "report.txt").exists()
    assert store.records("publication") == []
    assert store.records("publication_failed")[-1][1]["phase"] == "preflight"
    store.close()


def test_read_only_research_can_stage_verify_and_recover_without_project_write_access(tmp_path):
    from jev_runtime.supervisor import Supervisor

    availability = Sandbox().check()
    if not availability["available"]:
        pytest.skip(availability["reason"])
    project = tmp_path / "reference-project"
    project.mkdir()
    (project / "reference.txt").write_text("Read-only research material")
    project.chmod(0o500)
    store = Store(tmp_path / "records/ledger.sqlite")
    runtime = Runtime(store, FakeJudge())
    source = store.record_intent("Read the research material; do not change the project.")
    proposal = runtime.propose(
        {
            "intent_id": source,
            "workspace": str(project),
            "actions": ["read", "execute"],
            "shell_commands": [["python3"]],
            "required_evidence": ["read material"],
            "checks": [
                {
                    "kind": "file",
                    "requirement": "read material",
                    "path": "reference.txt",
                    "contains": "research",
                }
            ],
        }
    )
    runtime.approve(proposal)
    runner = Supervisor(runtime, tmp_path / "records/workers", confirm=lambda _: True)
    try:
        run = runner.run(
            proposal,
            ["python3", "-c", "from pathlib import Path; print(Path('reference.txt').read_text())"],
        )
        assert runner.publish(run) == []
        assert runtime.complete(proposal) is True
        runtime.failure(proposal, "Restrict for a fresh read-only recovery", quarantine=True)
        runner.recover(proposal)
        assert runtime.autonomy(proposal)["state"] == "NORMAL"
        assert (project / "reference.txt").read_text() == "Read-only research material"
        assert project.stat().st_mode & 0o777 == 0o500
    finally:
        store.close()
        project.chmod(0o700)
