import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock


def _load_main():
    path = Path(__file__).resolve().parents[1] / "main.py"
    spec = importlib.util.spec_from_file_location("sregym_clock_evidence_main_for_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_clock_skew_evidence_is_saved_outside_agent_artifacts(tmp_path, monkeypatch):
    main = _load_main()
    evidence = {
        "profile": "clock-skew",
        "preflight": {"status": "passed", "recommendation_status_before": 200},
        "target": {"pod_uid": "uid-123", "changed_before_cleanup": True},
        "cleanup": {"status": "confirmed"},
    }
    monkeypatch.setattr(
        main, "get_noise_manager", lambda: SimpleNamespace(evidence_snapshot=Mock(return_value=evidence))
    )

    path = main._write_clock_skew_evidence(tmp_path, "codex", "search_rate_retry_collapse_hotel_reservation", 2)

    assert path == tmp_path / "codex" / "search_rate_retry_collapse_hotel_reservation" / "noise_evidence_attempt2.json"
    assert json.loads(path.read_text()) == evidence
    assert path.stat().st_mode & 0o077 == 0
