"""Run-loop tests on the offline fixture market and paper broker.

The connectome needs the prepared MaleCNS dataset, so a stand-in controller
proposes BUY; market, guard, paper broker and ledger are the real ones.
"""

import json

import pytest

from stonkfly import cli
from stonkfly.config import Settings
from stonkfly.ledger import Ledger


class Brain:
    circuit = {"report": {}}
    visual_report = {}


class Controller:
    def __init__(self, settings):
        self.brain = Brain()

    def observe(self, rgb, reinforcement):
        return {
            "side": "BUY",
            "stimulus": reinforcement,
            "memory": {"changed_edges": 0},
        }

    def save(self, path):
        path.write_bytes(b"checkpoint")

    def restore(self, path):
        pass


@pytest.fixture
def run(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("stonkfly.data.verify", lambda: {"release": "test"})
    monkeypatch.setattr("stonkfly.neural.controller.FlyController", Controller)

    def invoke(*argv):
        monkeypatch.setattr("sys.argv", ["stonkfly", *argv])
        cli.main()

    return invoke


def test_missing_dataset_stops_before_creating_a_run(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("stonkfly.data.DATA", tmp_path / "data")
    monkeypatch.setattr("stonkfly.data.GRAPH", tmp_path / "data/graph.npz")
    monkeypatch.setattr("sys.argv", ["stonkfly", "run", "--fixture", "--fast"])
    with pytest.raises(SystemExit, match="stonkfly prepare"):
        cli.main()
    # No ledger exists, so a later run with prepared data is not halted.
    assert not (tmp_path / "runs").exists()


@pytest.mark.parametrize(
    "reason,resumable",
    [
        ("FileNotFoundError", True),
        ("Loss stop reached; holdings remain exposed", False),
    ],
)
def test_halted_run_says_why_instead_of_exiting_silently(
    run, tmp_path, reason, resumable
):
    ledger = Ledger(tmp_path / "r/ledger.sqlite", Settings(), "paper")
    ledger.halt(reason)
    ledger.close()
    with pytest.raises(SystemExit) as e:
        run("run", "--fixture", "--fast", "--steps", "1", "--out", "r")
    message = str(e.value)
    assert reason in message
    # Financial stops cannot be cleared, so that flag must not be suggested.
    assert ("--resume-reviewed" in message) == resumable


def test_stop_file_is_reported_instead_of_exiting_silently(run, tmp_path, capsys):
    (tmp_path / "r").mkdir()
    (tmp_path / "r/STOP").touch()
    run("run", "--fixture", "--fast", "--steps", "1", "--out", "r")
    assert "r/STOP" in capsys.readouterr().out
    assert not (tmp_path / "r/events.jsonl").exists()


def test_fixture_execution_sees_the_observed_price(run, tmp_path):
    run("run", "--fixture", "--fast", "--steps", "1", "--out", "r")
    event = json.loads((tmp_path / "r/events.jsonl").read_text())
    # Re-reading the book for execution must not advance the synthetic market.
    assert event["execution"]["status"] == "FILLED"


def test_stop_reason_is_printed_not_only_filed(run, tmp_path, capsys):
    run("run", "--fixture", "--fast", "--steps", "1", "--out", "r")
    ledger = Ledger(tmp_path / "r/ledger.sqlite", Settings(), "paper")
    ledger.put("provenance_sha256", "recorded by other source code")
    ledger.close()
    with pytest.raises(SystemExit):
        run("run", "--fixture", "--fast", "--steps", "1", "--out", "r")
    assert "Run source/protocol changed" in capsys.readouterr().err


def test_changed_settings_are_refused_without_a_traceback(run):
    run("run", "--fixture", "--fast", "--steps", "1", "--out", "r")
    with pytest.raises(SystemExit, match="separate paper run directory"):
        run("run", "--fixture", "--fast", "--steps", "1", "--out", "r", "--frozen")


def test_monitor_without_runs_says_where_it_looked(run):
    with pytest.raises(SystemExit, match="under runs/"):
        run("monitor", "--no-browser")
    with pytest.raises(SystemExit, match="nowhere"):
        run("monitor", "--out", "nowhere", "--no-browser")


def test_status_of_a_missing_run_names_the_directory(run):
    with pytest.raises(SystemExit, match="nowhere"):
        run("status", "--out", "nowhere")
