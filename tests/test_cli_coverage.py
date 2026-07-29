from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
import typer

import reharness.cli as cli
from reharness.services import HarnessError


class ExplodingHarness:
    def __getattr__(self, name: str):
        def fail(*args, **kwargs):
            raise HarnessError(f"{name} failed")
        return fail


class SuccessHarness:
    def complete_task(self, task_id, succeeded, summary, result_type=None, failure_reason=None):
        return SimpleNamespace(
            id=task_id,
            status="succeeded" if succeeded else "failed",
            result_type=result_type,
            failure_reason=failure_reason,
        )

    def run_command(self, *args, **kwargs):
        return {"stdout": "out\n", "stderr": "warning\n", "evidence_id": "EVD-1", "exit_code": 130}

    def refute_conclusion(self, conclusion_id, evidence, reason):
        return SimpleNamespace(id=conclusion_id, status="refuted")

    def verify_all_evidence(self):
        return [{"id": "EVD-1", "valid": False}]

    def doctor(self):
        return [{"severity": "error", "code": "BROKEN", "entity": "EVD-1", "message": "broken evidence"}]

    def run_test(self, *args, **kwargs):
        return SimpleNamespace(id="TRUN-1", status="failed", passed_count=0, total_count=1, evidence_id="EVD-1")

    def import_junit(self, *args, **kwargs):
        return SimpleNamespace(id="TRUN-2", status="failed")

    def test_run_data(self, run_id):
        return {"id": run_id, "status": "failed"}


def test_cli_helpers_cover_file_and_output_branches(tmp_path: Path, capsys) -> None:
    text = tmp_path / "text.md"
    text.write_text("hello", encoding="utf-8")
    items = tmp_path / "items.yaml"
    items.write_text("- one\n- two\n", encoding="utf-8")
    invalid = tmp_path / "invalid.txt"
    invalid.write_text("not a mapping", encoding="utf-8")

    assert cli._read_text(None, text, "text") == "hello"
    assert cli._read_list(["zero"], items) == ["zero", "one", "two"]
    with pytest.raises(HarnessError):
        cli._read_text(None, None, "text")
    with pytest.raises(HarnessError):
        cli._read_mapping(invalid)

    cli._emit("line")
    cli._emit([1, 2])
    assert "line" in capsys.readouterr().out
    with pytest.raises(typer.Exit):
        cli._handle_error(HarnessError("boom"))


def test_cli_success_exit_paths(monkeypatch, tmp_path: Path, capsys) -> None:
    fake = SuccessHarness()
    monkeypatch.setattr(cli, "_harness", lambda: fake)

    cli.task_fail("TASK-1", "failed summary", None, "environment", False)
    with pytest.raises(typer.Exit) as exc:
        cli.run(SimpleNamespace(args=["cmd"]), "TASK-1", [], None, None, None, None, None, None, False)
    assert exc.value.exit_code == 1
    assert "warning" in capsys.readouterr().err

    cli.conclusion_refute("CON-1", ["EVD-1"], "counterexample", None, False)
    with pytest.raises(typer.Exit):
        cli.evidence_verify_all(False)
    with pytest.raises(typer.Exit):
        cli.doctor(False)
    with pytest.raises(typer.Exit):
        cli.test_run_command("TEST-1", None, None, None, None, False)

    junit = tmp_path / "junit.xml"
    junit.write_text('<testsuite tests="1" failures="1"/>', encoding="utf-8")
    with pytest.raises(typer.Exit):
        cli.test_import("TEST-1", junit, None, None, False)


def test_each_cli_command_translates_harness_errors(monkeypatch, tmp_path: Path) -> None:
    fake = ExplodingHarness()
    monkeypatch.setattr(cli, "_harness", lambda: fake)

    text = tmp_path / "text.md"
    text.write_text("content", encoding="utf-8")
    mapping = tmp_path / "mapping.json"
    mapping.write_text('{"key": "value"}', encoding="utf-8")
    artifact = tmp_path / "artifact.bin"
    artifact.write_bytes(b"artifact")
    junit = tmp_path / "junit.xml"
    junit.write_text('<testsuite tests="1"/>', encoding="utf-8")

    calls = [
        lambda: cli.project_show(False), lambda: cli.brief("normal"), lambda: cli.context("topic", 1000),
        cli.render, lambda: cli.doctor(False), lambda: cli.summary(False),
        lambda: cli.task_start("research", "goal", None, [], None, [], None, [], False),
        lambda: cli.task_step("TASK-1", "observation_recorded", "step", None, None, False),
        lambda: cli.task_revise_plan("TASK-1", text, "reason"),
        lambda: cli.task_succeed("TASK-1", "done", None, None, False),
        lambda: cli.task_fail("TASK-1", "failed", None, "reason", False),
        lambda: cli.run(SimpleNamespace(args=["cmd"]), "TASK-1", [], None, None, None, None, None, None, False),
        lambda: cli.evidence_capture(artifact, "experiment_result", None, None, False),
        lambda: cli.evidence_show("EVD-1", False), lambda: cli.evidence_verify("EVD-1", False),
        lambda: cli.evidence_verify_all(False),
        lambda: cli.conclusion_create("claim", None, None, "", None, None, None, False),
        lambda: cli.conclusion_support("CON-1", ["EVD-1"], "", None, False),
        lambda: cli.conclusion_refute("CON-1", ["EVD-1"], "", None, False),
        lambda: cli.conclusion_supersede("CON-1", "CON-2", "", None, False),
        lambda: cli.requirement_create("description", None, [], None, [], None, "medium", False),
        lambda: cli.requirement_accept("REQ-1"), lambda: cli.requirement_start("REQ-1"),
        lambda: cli.requirement_implemented("REQ-1"),
        lambda: cli.requirement_plan("REQ-1", text, None, False),
        lambda: cli.requirement_verify("REQ-1", "TRUN-1", False),
        lambda: cli.change_capture("base", "head", None, [], None, False),
        lambda: cli.build_capture(artifact, "succeeded", None, None, False),
        lambda: cli.test_define("test", "unit", ["true"], [], mapping, mapping, mapping, False),
        lambda: cli.test_run_command("TEST-1", None, None, None, None, False),
        lambda: cli.project_update(None, "active", None, None, False),
        lambda: cli.task_show("TASK-1", False), lambda: cli.task_list(None, False),
        lambda: cli.conclusion_show("CON-1", False), lambda: cli.conclusion_list(None, False),
        lambda: cli.requirement_show("REQ-1", False), lambda: cli.requirement_list(None, False),
        lambda: cli.test_import("TEST-1", junit, None, None, False), lambda: cli.test_show("TRUN-1", False),
    ]

    for call in calls:
        with pytest.raises(typer.Exit) as exc:
            call()
        assert exc.value.exit_code == 2


def test_init_translates_initialization_error(monkeypatch, tmp_path: Path) -> None:
    def fail(*args, **kwargs):
        raise HarnessError("init failed")

    monkeypatch.setattr(cli.Harness, "initialize", fail)
    with pytest.raises(typer.Exit) as exc:
        cli.init("name", "", tmp_path, None, False)
    assert exc.value.exit_code == 2
