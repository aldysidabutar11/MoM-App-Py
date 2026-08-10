"""Naming the switch that is wrong, instead of listing three that might be.

A muted microphone and a silent room are byte-identical at every user-mode audio API:
the stream opens, the callback fires on time, and every sample is zero. So a `NO_SIGNAL`
verdict on its own can only offer a list, and the operator has to debug it.

This happened on the development machine. Privacy was allowed at all three levels, the
device was enabled, PortAudio delivered 25 357 frames in 0.66 s -- and `mute` was `True`.
The raw kernel-streaming endpoint for the same array measured -60.9 dBFS of ordinary
speech while every mixer-side endpoint measured -96.7 dBFS, because mute lives in the
mixer and WDM-KS runs underneath it.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from mom_igd.audio.endpoint_state import EndpointState, read_default_capture_endpoint

MODULE = Path(__file__).resolve().parents[1] / "mom_igd" / "audio" / "endpoint_state.py"


# ===========================================================================
# It must never write
# ===========================================================================


def _code_without_prose(path: Path) -> str:
    """The module's executable text, with docstrings and comments removed.

    Necessary rather than fastidious: the module *documents* that the setters are
    deliberately absent, so a plain substring search over the file finds the very words
    it is meant to forbid and fails against its own explanation.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            if (
                node.body
                and isinstance(node.body[0], ast.Expr)
                and isinstance(node.body[0].value, ast.Constant)
                and isinstance(node.body[0].value.value, str)
            ):
                node.body.pop(0)
    return ast.unparse(tree)


def test_no_setter_is_bound_anywhere_in_the_module() -> None:
    """CLAUDE.md rule 12. An app that can unmute a microphone can start a recording in a
    room the operator believed was private, so the setters are absent, not merely unused."""
    code = _code_without_prose(MODULE)
    for banned in (
        "SetMute",
        "SetMasterVolumeLevel",
        "SetMasterVolumeLevelScalar",
        "SetChannelVolumeLevel",
        "RegSetValue",
        "SetValue",
    ):
        assert banned not in code, f"{banned} must not appear in a read-only module"


def test_only_the_two_getter_vtable_slots_are_used() -> None:
    """Slot 9 is GetMasterVolumeLevelScalar and slot 15 is GetMute.

    Pinned because a wrong index would silently call a *different* method on the same
    interface -- and several of the neighbours are setters.
    """
    source = MODULE.read_text(encoding="utf-8")
    assert "_method(volume, 9, ctypes.HRESULT, POINTER(c_float))" in source
    assert "_method(volume, 15, ctypes.HRESULT, POINTER(c_bool))" in source


def test_the_module_imports_nothing_heavy() -> None:
    tree = ast.parse(MODULE.read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    assert "sounddevice" not in imported, "a diagnostic must not need the audio backend"
    for banned in ("numpy", "comtypes", "pycaw", "win32api"):
        assert banned not in imported


# ===========================================================================
# What it concludes
# ===========================================================================


def test_a_muted_endpoint_explains_silence_and_names_the_setting() -> None:
    state = EndpointState(muted=True, volume_percent=88.0)
    assert state.known
    assert state.explains_silence
    advice = state.advice or ""
    assert "MUTED" in advice
    assert "Sound" in advice, "the advice must name where to go"


def test_a_zero_volume_endpoint_explains_silence_too() -> None:
    state = EndpointState(muted=False, volume_percent=0.0)
    assert state.explains_silence
    assert "volume" in (state.advice or "")


def test_a_healthy_endpoint_offers_no_advice() -> None:
    """Otherwise every quiet room would be blamed on a setting that is fine."""
    state = EndpointState(muted=False, volume_percent=75.0)
    assert not state.explains_silence
    assert state.advice is None


def test_an_unknown_endpoint_falls_back_rather_than_guessing() -> None:
    state = EndpointState()
    assert not state.known
    assert not state.explains_silence
    assert state.advice is None


def test_reading_the_endpoint_never_raises() -> None:
    """A diagnostic that fails must not fail the thing it is diagnosing."""
    state = read_default_capture_endpoint()
    assert isinstance(state, EndpointState)
    assert state.muted is None or isinstance(state.muted, bool)


def test_the_state_serialises_for_the_api_and_the_shell() -> None:
    payload = EndpointState(muted=True, volume_percent=88.06).to_dict()
    assert payload == {
        "muted": True,
        "volume_percent": 88.1,
        "known": True,
        "explains_silence": True,
    }


# ===========================================================================
# The snapshot prefers the specific diagnosis
# ===========================================================================


def _snapshot_of(pcm: bytes):
    """Run PCM through the real meter and return its cumulative snapshot."""
    from mom_igd.audio.backend import CaptureProfile
    from mom_igd.audio.quality import QualityMeter

    meter = QualityMeter(CaptureProfile(sample_rate=16_000, channels=1))
    meter.add(pcm)
    return meter.cumulative_snapshot()


def test_a_no_signal_snapshot_reports_the_mute_when_windows_knows(monkeypatch) -> None:
    from mom_igd.audio import quality as quality_module

    monkeypatch.setattr(
        "mom_igd.audio.endpoint_state.read_default_capture_endpoint",
        lambda: EndpointState(muted=True, volume_percent=90.0),
    )
    snapshot = _snapshot_of(b"\x00\x00" * 4000)
    assert snapshot.verdict is quality_module.LevelVerdict.NO_SIGNAL
    assert "MUTED" in snapshot.diagnosis
    assert snapshot.to_dict()["advice"] == snapshot.diagnosis
    assert snapshot.to_dict()["endpoint"]["muted"] is True


def test_a_no_signal_snapshot_falls_back_when_windows_cannot_say(monkeypatch) -> None:
    from mom_igd.audio import quality as quality_module

    monkeypatch.setattr(
        "mom_igd.audio.endpoint_state.read_default_capture_endpoint",
        lambda: EndpointState(),
    )
    snapshot = _snapshot_of(b"\x00\x00" * 4000)
    assert snapshot.diagnosis == quality_module.LevelVerdict.NO_SIGNAL.advice


def test_a_healthy_snapshot_asks_windows_nothing(monkeypatch) -> None:
    """An ordinary calibration must not pay for COM calls it does not need."""
    from mom_igd.audio import quality as quality_module

    calls: list[int] = []

    def spy():
        calls.append(1)
        return EndpointState()

    monkeypatch.setattr(
        "mom_igd.audio.endpoint_state.read_default_capture_endpoint", spy
    )
    import math
    import struct

    pcm = b"".join(
        struct.pack("<h", int(6000 * math.sin(index / 8.0))) for index in range(4000)
    )
    snapshot = _snapshot_of(pcm)
    assert snapshot.verdict is quality_module.LevelVerdict.GOOD
    assert snapshot.diagnosis
    assert calls == [], "a good level must not trigger an endpoint read"
