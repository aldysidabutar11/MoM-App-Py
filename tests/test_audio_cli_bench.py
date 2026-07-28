"""Phase 2 CLI commands and the fake-backend benchmark / smoke runner.

The CLI normally builds a real PortAudio backend. These tests substitute the fake
one and suppress the Windows endpoint lookup, so nothing here needs a microphone
or depends on which devices the host machine happens to have.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from mom_igd.audio.bench import run_capture_benchmark, run_capture_smoke
from mom_igd.audio.fake_backend import CounterSource, FakeAudioBackend
from mom_igd.audio.service import RecordingService
from mom_igd.cli import EXIT_FAILURE, EXIT_OK, main
from mom_igd.config import AppConfig


@pytest.fixture
def fake_hardware(monkeypatch: pytest.MonkeyPatch) -> FakeAudioBackend:
    """Make every CLI-constructed service use the fake backend."""
    backend = FakeAudioBackend(blocksize=1_200, source=CounterSource())
    monkeypatch.setattr(
        RecordingService, "_default_backend", staticmethod(lambda: backend)
    )
    # Without this, the real Windows registry lookup would reject the fake device
    # names as "not a Windows capture endpoint" -- correct behaviour, wrong context.
    monkeypatch.setattr(
        "mom_igd.audio.devices.query_windows_capture_endpoints", lambda: []
    )
    return backend


@pytest.fixture
def runtime(tmp_path: Path) -> list[str]:
    return ["--data-dir", str(tmp_path / "runtime")]


@pytest.fixture
def initialised(runtime: list[str]) -> list[str]:
    assert main(["db", "init", *runtime]) == EXIT_OK
    return runtime


def _json_out(capsys: pytest.CaptureFixture[str]) -> dict:
    return json.loads(capsys.readouterr().out)


# ==================================================== bench / smoke runner


def test_capture_smoke_passes_end_to_end(config: AppConfig) -> None:
    result = run_capture_smoke(config)
    failures = [step for step in result["steps"] if not step["ok"]]
    assert failures == [], failures
    assert result["ok"] is True
    names = {step["name"] for step in result["steps"]}
    assert {
        "session_start",
        "capture_no_loss",
        "clean_shutdown",
        "manifest_verified",
        "audio_byte_exact",
        "tampering_detected",
        "recovery_rebuilt_partial",
        "recovery_idempotent",
        "recovered_audio_verifies",
    } == names


def test_capture_smoke_leaves_no_artefact_behind(config: AppConfig, paths) -> None:
    """It works in a throwaway directory, never in the configured data root."""
    run_capture_smoke(config)
    assert not any(paths.recordings_dir.rglob("*.wav"))


def test_benchmark_reports_measured_values_and_targets(config: AppConfig) -> None:
    result = run_capture_benchmark(config, audio_minutes=0.1, speed=200.0)

    measured = result["measured"]
    assert measured["frames_written"] == measured["frames_requested"]
    assert measured["frames_written"] == measured["frames_produced"]
    assert measured["dropped_frames"] == 0
    assert measured["xrun_callbacks"] == 0
    assert measured["checksum_mismatches"] == 0
    assert measured["corrupt_chunks"] == 0
    assert measured["leaked_writer_threads"] == 0
    assert measured["capture_drift_percent"] <= 0.1
    assert measured["queue_high_water_frames"] > 0
    assert measured["writer_mean_write_ms"] >= 0.0
    assert result["manifest_ok"] is True
    assert result["ok"] is True
    # A short run at this speed completes, so coverage must be reported complete.
    assert result["coverage_complete"] is True
    assert measured["requested_audio_delivered_percent"] >= 99.0
    assert "INCOMPLETE COVERAGE" not in result["note"]


def test_capture_drift_measures_the_writer_not_the_fake_generator() -> None:
    """A generator that runs out of wall clock must not read as capture drift.

    At a high speed multiplier the fake source cannot always synthesise every
    requested frame before the deadline. That is a property of the test machine, not
    of the capture path, and blaming the capture path for it made a healthy run
    report FAIL. Fidelity is written-vs-produced; coverage is produced-vs-requested.
    """
    from mom_igd.audio.bench import run_capture_benchmark as run

    import inspect

    source = inspect.getsource(run)
    assert "abs(result.frames_written - frames_produced)" in source, (
        "capture drift must compare what was written against what the device "
        "produced, never against what the harness planned to produce"
    )
    assert "requested_audio_delivered_percent" in source
    assert "INCOMPLETE COVERAGE" in source, "a short run must announce itself"


def test_benchmark_marks_hardware_targets_as_not_measured(config: AppConfig) -> None:
    """An accelerated fake run says nothing about real CPU or memory cost."""
    result = run_capture_benchmark(config, audio_minutes=0.1, speed=200.0)
    for key in (
        "capture_cpu_avg_le_5pct",
        "capture_cpu_p95_le_10pct",
        "capture_rss_le_250mb",
    ):
        assert result["targets"][key].startswith("NOT MEASURED"), key
    for key in ("dropped_frames", "xrun_callbacks", "capture_drift", "leaked_threads"):
        assert result["targets"][key].startswith("PASS"), key
    assert "NOT MEASURED" in result["note"]


# ================================================================ audio CLI


def test_audio_devices_lists_the_fake_devices(
    fake_hardware, runtime: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["audio", "devices", *runtime]) == EXIT_OK
    out = capsys.readouterr().out
    assert "Fake USB Conference Mic" in out
    assert "FINGERPRINT" in out
    assert "No USB conference microphone verified by Windows" in out


def test_audio_devices_json_and_rejections(
    fake_hardware, runtime: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["audio", "devices", "--json", *runtime]) == EXIT_OK
    payload = _json_out(capsys)
    assert len(payload["devices"]) == 3
    assert payload["verified_usb_available"] is False
    assert any("output-only" in entry["reason"] for entry in payload["rejected"])

    assert main(["audio", "devices", "--all", *runtime]) == EXIT_OK
    assert "Excluded devices:" in capsys.readouterr().out


def test_audio_devices_fails_when_nothing_is_available(
    monkeypatch: pytest.MonkeyPatch, runtime: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    empty = FakeAudioBackend(devices=[])
    monkeypatch.setattr(RecordingService, "_default_backend", staticmethod(lambda: empty))
    monkeypatch.setattr("mom_igd.audio.devices.query_windows_capture_endpoints", lambda: [])
    assert main(["audio", "devices", *runtime]) == EXIT_FAILURE
    assert "No capture devices found." in capsys.readouterr().out


def test_audio_probe_runs_preflight(
    fake_hardware, initialised: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    code = main(["audio", "probe", "--json", *initialised])
    payload = _json_out(capsys)
    assert "items" in payload
    assert {"device", "format", "disk_space", "database"} <= {
        item["key"] for item in payload["items"]
    }
    assert code in (EXIT_OK, EXIT_FAILURE)


def test_audio_probe_text_output(
    fake_hardware, initialised: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    main(["audio", "probe", "--minutes", "30", *initialised])
    out = capsys.readouterr().out
    assert "Preflight for a 30-minute meeting" in out
    assert "READY TO RECORD" in out or "NOT READY" in out


def test_audio_probe_open_test_engages_the_fake_device(
    fake_hardware, initialised: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    fake_hardware.realtime = True
    fake_hardware.speed = 80.0
    main(["audio", "probe", "--open-test", "--json", *initialised])
    payload = _json_out(capsys)
    assert payload["open_test"]["ok"] is True
    assert payload["open_test"]["frames"] > 0


def test_audio_calibrate_reports_levels(
    fake_hardware, initialised: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    from mom_igd.audio.fake_backend import SineSource

    fake_hardware.source = SineSource(level_dbfs=-18.0)
    fake_hardware.realtime = True
    fake_hardware.speed = 80.0
    code = main(["audio", "calibrate", "--seconds", "0.4", "--json", *initialised])
    payload = _json_out(capsys)
    assert code == EXIT_OK
    assert payload["verdict"] == "GOOD"
    assert payload["audio_saved"] is False
    assert payload["levels"]["channels"]


def test_audio_calibrate_text_output_and_failure_exit(
    fake_hardware, initialised: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    from mom_igd.audio.fake_backend import SilenceSource

    fake_hardware.source = SilenceSource()
    fake_hardware.realtime = True
    fake_hardware.speed = 80.0
    code = main(["audio", "calibrate", "--seconds", "0.3", *initialised])
    out = capsys.readouterr().out
    assert code == EXIT_FAILURE, "silence is not an acceptable calibration"
    assert "Verdict" in out
    assert "NO_SIGNAL" in out
    assert "Noise floor" in out


def test_audio_verify_with_no_recordings(
    fake_hardware, initialised: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["audio", "verify", *initialised]) == EXIT_OK
    assert "No recordings found." in capsys.readouterr().out


def test_audio_verify_after_a_recording(
    fake_hardware, initialised: list[str], capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    from mom_igd.audio.manifest import ManifestWriter, write_manifest_summary
    from mom_igd.audio.writer import ChunkWriter
    from mom_igd.config import load_config

    config = load_config(data_root=tmp_path / "runtime", use_local_file=False)
    directory = config.runtime_paths().recordings_dir / "m-uuid" / "r-uuid"
    directory.mkdir(parents=True)
    profile = config.audio.capture_profile(sample_rate=8_000, channels=1)
    manifest = ManifestWriter(directory)
    writer = ChunkWriter(directory, profile, on_finalised=lambda f: manifest.append_chunk(f.record))
    writer.write(CounterSource().read(0, 2_000, profile))
    writer.close()
    write_manifest_summary(
        directory, recording_uuid="r-uuid", meeting_uuid="m-uuid", profile=profile,
        records=writer.finalised,
    )

    assert main(["audio", "verify", "--json", *initialised]) == EXIT_OK
    payload = _json_out(capsys)
    assert payload["recordings"] == 1
    assert payload["reports"][0]["ok"] is True
    assert payload["reports"][0]["recording"] == "m-uuid/r-uuid"

    # Corrupt it and the same command must fail.
    victim = directory / "chunk_000000.wav"
    data = bytearray(victim.read_bytes())
    data[60] ^= 0xFF
    victim.write_bytes(bytes(data))
    assert main(["audio", "verify", *initialised]) == EXIT_FAILURE
    assert "checksum mismatch" in capsys.readouterr().out


def test_audio_recover_reports_nothing_to_do(
    fake_hardware, initialised: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["audio", "recover", "--json", *initialised]) == EXIT_OK
    payload = _json_out(capsys)
    assert payload["scanned"] == 0
    assert payload["recovered_chunks"] == 0


def test_audio_recover_rebuilds_a_partial(
    fake_hardware, initialised: list[str], capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    from mom_igd.audio.writer import partial_path, write_partial_meta
    from mom_igd.config import load_config

    config = load_config(data_root=tmp_path / "runtime", use_local_file=False)
    directory = config.runtime_paths().recordings_dir / "m-uuid" / "r-uuid"
    directory.mkdir(parents=True)
    profile = config.audio.capture_profile(sample_rate=8_000, channels=1)
    write_partial_meta(
        directory, 0, profile, start_frame=0, utc_start="x", monotonic_start_ns=0
    )
    partial_path(directory, 0).write_bytes(CounterSource().read(0, 1_500, profile) + b"\x01")

    assert main(["audio", "recover", *initialised]) == EXIT_OK
    out = capsys.readouterr().out
    assert "Chunks recovered   : 1" in out
    assert "1500 frames" in out
    assert (directory / "chunk_000000.wav").is_file()


def test_audio_smoke_command_passes(
    runtime: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["audio", "smoke", *runtime]) == EXIT_OK
    out = capsys.readouterr().out
    assert "Audio capture smoke: PASS" in out
    assert "audio_byte_exact" in out


def test_audio_smoke_json(runtime: list[str], capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["audio", "smoke", "--json", *runtime]) == EXIT_OK
    payload = _json_out(capsys)
    assert payload["ok"] is True
    assert payload["passed"] == payload["total"] == 9


def test_audio_bench_command(runtime: list[str], capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["audio", "bench", "--minutes", "0.1", "--speed", "200", *runtime]) == EXIT_OK
    out = capsys.readouterr().out
    assert "dropped_frames" in out
    assert "NOT MEASURED" in out


def test_audio_bench_json(runtime: list[str], capsys: pytest.CaptureFixture[str]) -> None:
    assert main(
        ["audio", "bench", "--minutes", "0.1", "--speed", "200", "--json", *runtime]
    ) == EXIT_OK
    payload = _json_out(capsys)
    assert payload["measured"]["dropped_frames"] == 0
    assert payload["targets"]["capture_rss_le_250mb"].startswith("NOT MEASURED")


def test_audio_group_without_a_subcommand_is_refused(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["audio"]) == EXIT_FAILURE
    assert "requires a subcommand" in capsys.readouterr().err


def test_audio_help_lists_every_subcommand() -> None:
    from mom_igd.cli import build_parser

    help_text = build_parser().format_help()
    assert "audio" in help_text
    audio_help = None
    for action in build_parser()._subparsers._group_actions:  # noqa: SLF001
        audio_help = action.choices.get("audio")
        if audio_help is not None:
            break
    assert audio_help is not None
    text = audio_help.format_help()
    for command in ("devices", "probe", "calibrate", "verify", "recover", "smoke", "bench"):
        assert command in text, command


def test_doctor_production_flag_is_wired(
    fake_hardware, runtime: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    code = main(["doctor", "--production", "--json", *runtime])
    payload = _json_out(capsys)
    assert payload["production_gate"] is True
    keys = {result["key"] for result in payload["results"]}
    assert "audio_calibration_evidence" in keys
    assert code == EXIT_FAILURE, "no USB microphone and no calibration -> gate fails"


def test_doctor_development_does_not_apply_the_production_gate(
    fake_hardware, runtime: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    main(["doctor", "--json", *runtime])
    payload = _json_out(capsys)
    assert payload["production_gate"] is False
    keys = {result["key"] for result in payload["results"]}
    assert "audio_calibration_evidence" not in keys
    usb = next(r for r in payload["results"] if r["key"] == "usb_conference_microphone")
    assert usb["status"] == "WARN"
