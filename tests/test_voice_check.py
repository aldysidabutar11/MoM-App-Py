"""The voice-to-text check: does what I said become the right words, before the meeting?

A level meter proves the microphone is delivering *sound*. It says nothing about whether
that sound becomes the right words, and somebody responsible for a minute needs to see a
sentence they just spoke appear correctly before they trust ninety minutes of it.

Two failure directions, and this panel has to be safe in both. Show nothing when speech
was captured, and the operator concludes the microphone is broken -- which already
happened once here, on a working microphone. Show invented text, and they conclude it
works when it does not. So the filters are tested as carefully as the plumbing, and so is
the counting that keeps a filtered window from looking like a dead one.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from mom_igd.asr.live import (
    MAX_NO_SPEECH_PROB,
    MIN_AVG_LOGPROB,
    LiveSegment,
    LiveTranscriptState,
    _drop_if_invented,
)

WEB = Path(__file__).resolve().parents[1] / "mom_igd" / "shell" / "web"


# ===========================================================================
# What is shown, and what is refused
# ===========================================================================


def _keep(text: str, *, no_speech: float = 0.1, logprob: float = -0.3, previous: str = "") -> str:
    return _drop_if_invented(text, no_speech=no_speech, logprob=logprob, previous=previous)


def test_a_real_sentence_is_shown() -> None:
    assert _keep("Kita sepakat menunda go-live ke 5 September") != ""


def test_the_decoder_s_own_doubt_wins() -> None:
    """`no_speech_prob` is the only signal that separates loud noise from speech."""
    assert _keep("Kita sepakat menunda", no_speech=MAX_NO_SPEECH_PROB + 0.1) == ""


def test_a_guessed_window_is_refused() -> None:
    assert _keep("Kita sepakat menunda", logprob=MIN_AVG_LOGPROB - 0.2) == ""


def test_the_observed_hallucination_is_refused() -> None:
    """Measured: a loud room with nobody speaking produced this five windows running."""
    for phrase in ("Terima kasih.", "terima kasih", "Thank you.", "Sampai jumpa"):
        assert _keep(phrase) == "", phrase


def test_the_same_words_inside_a_real_sentence_survive() -> None:
    """"Terima kasih" is a normal thing to say in a meeting. Only the bare phrase goes."""
    assert _keep("Terima kasih pak Andi atas laporannya") != ""
    assert _keep("Baik, terima kasih semua, rapat saya tutup") != ""


def test_a_window_repeating_the_previous_one_is_refused() -> None:
    """Whisper's failure mode on non-speech is a loop, four seconds apart."""
    assert _keep("Kita sepakat menunda", previous="kita sepakat menunda") == ""
    assert _keep("Kita sepakat menunda", previous="anggaran naik") != ""


def test_empty_text_stays_empty() -> None:
    assert _keep("") == ""
    assert _keep("   ") == ""


# ===========================================================================
# A filtered window must never look like a dead microphone
# ===========================================================================


def test_the_state_reports_what_was_filtered() -> None:
    state = LiveTranscriptState(decoded_windows=5, filtered_windows=5)
    payload = state.to_dict()
    assert payload["decoded_windows"] == 5
    assert payload["filtered_windows"] == 5, (
        "silently discarding text recreates the confusion the filters were added after"
    )
    assert payload["is_preview"] is True


def test_the_payload_always_declares_itself_a_preview() -> None:
    """So no interface can present it as the transcript by leaving a label off."""
    assert LiveTranscriptState().to_dict()["is_preview"] is True
    state = LiveTranscriptState(segments=[LiveSegment("halo", 0, 4000)])
    assert state.to_dict()["is_preview"] is True
    assert state.to_dict()["text"] == "halo"


# ===========================================================================
# The service: it measures, it transcribes, it stores nothing
# ===========================================================================


def test_a_voice_check_is_refused_while_a_recording_runs(config, paths, conn) -> None:
    """The microphone is already in use, and one recording at a time is the rule."""
    from mom_igd.audio.service import RecordingService, RecordingServiceError

    service = RecordingService(config, paths)
    service._active = object()  # noqa: SLF001 - the guard is what is under test
    with pytest.raises(RecordingServiceError, match="recording is in progress"):
        service.voice_check(seconds=5)


@pytest.mark.parametrize("seconds", [0.5, 2.0, 61.0, 600.0])
def test_an_unreasonable_duration_is_refused(config, paths, conn, seconds) -> None:
    """Too short proves nothing; too long is a recording wearing a different name."""
    from mom_igd.audio.service import RecordingService, RecordingServiceError

    service = RecordingService(config, paths)
    with pytest.raises(RecordingServiceError, match="between 3 and 60"):
        service.voice_check(seconds=seconds)


def test_the_service_never_writes_during_a_voice_check() -> None:
    """Read statically: the method must not reach the writer, manifest or database."""
    source = (
        Path(__file__).resolve().parents[1] / "mom_igd" / "audio" / "service.py"
    ).read_text(encoding="utf-8")
    tree = ast.parse(source)
    function = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "voice_check"
    )
    body = ast.unparse(function)
    for banned in ("ChunkWriter", "ManifestWriter", "record_event", "_store_setting",
                   "save_to=Path", "CaptureSession"):
        assert banned not in body, f"voice_check must not touch {banned}"
    assert "save_to=None" in body, "the calibration it runs must keep no audio"


def test_the_level_endpoint_reports_inactive_when_the_microphone_is_closed(
    config, paths, conn
) -> None:
    """A bar frozen on somebody's last word is indistinguishable from a working one."""
    from mom_igd.audio.service import RecordingService

    service = RecordingService(config, paths)
    assert service.live_level() == {"active": False}


# ===========================================================================
# The panel
# ===========================================================================


@pytest.fixture(scope="module")
def html() -> str:
    return (WEB / "index.html").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def block() -> str:
    """The voice module, to the end of the IIFE that holds it.

    This was a fixed 8000-character slice, and growing the module by one paragraph moved
    a line that had always been inside the window to just outside it -- a failure that
    said nothing about the code. The end of the enclosing IIFE is where the module
    actually ends, so that is the boundary read here.
    """
    script = (WEB / "app.js").read_text(encoding="utf-8")
    start = script.index("voice to text")
    end = script.index("\n})();", start)
    return script[start:end]


def test_the_panel_says_nothing_is_stored(html: str) -> None:
    card = html[html.index('id="voice-card"') : html.index('id="voice-card"') + 2000]
    assert "tidak ada yang disimpan" in card.lower() or "Tidak ada yang disimpan" in card


def test_the_panel_tells_the_operator_what_to_compare(html: str) -> None:
    """The whole point: read it back and check the meaning is right."""
    card = html[html.index('id="voice-card"') : html.index('id="voice-card"') + 2000]
    assert "artinya tepat" in card


def test_the_microphone_only_opens_on_a_button_press(html: str, block: str) -> None:
    card = html[html.index('id="voice-card"') : html.index('id="voice-card"') + 2000]
    assert "hanya dibuka saat tombol ditekan" in card
    assert "voice.start.addEventListener" in block


def test_an_empty_result_is_always_explained(block: str) -> None:
    """Three reasons, and the operator must be told which. Saying nothing is the bug."""
    assert "model_available" in block, "must distinguish 'no model installed'"
    assert "filtered_windows" in block, "must distinguish 'heard, but not clear enough'"
    assert "tidak bergerak" in block, "must distinguish 'nothing heard at all'"


def test_the_panel_reports_the_filtered_count(block: str) -> None:
    assert "Dibuang (tidak jelas)" in block


def test_the_panel_uses_no_repeating_timer(block: str) -> None:
    assert "setInterval" not in block
    assert "voicePoll" in block and "voiceStopPolling" in block


def test_transcript_text_is_written_as_text_never_as_markup(block: str) -> None:
    """Whatever was said in the room is untrusted input."""
    assert "innerHTML" not in block
    assert "createTextNode" in block


def test_the_panel_polls_both_the_level_and_the_words(block: str) -> None:
    assert "/audio/level" in block
    assert "/audio/live" in block


def test_every_element_the_panel_uses_exists(html: str, block: str) -> None:
    wanted = set(re.findall(r"getElementById\('(voice-[a-z0-9-]+)'\)", block))
    present = set(re.findall(r'id="(voice-[a-z0-9-]+)"', html))
    assert wanted - present == set()


# ===========================================================================
# The accurate pass
# ===========================================================================


def _final_pass_source() -> str:
    """Both halves of the accurate pass.

    It is split across two packages on purpose -- Phase 2 owns the captured bytes,
    `mom_igd.asr` owns the decoding -- so a reason code can be introduced in either one.
    Reading only the audio side would let a new silent empty result through.
    """
    root = Path(__file__).resolve().parents[1] / "mom_igd"
    wanted = {
        root / "audio" / "service.py": "_final_transcription",
        root / "asr" / "live.py": "decode_once",
    }
    chunks: list[str] = []
    for path, name in wanted.items():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        function = next(
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name == name
        )
        chunks.append(ast.unparse(function))
    return "\n".join(chunks)


def test_every_empty_final_result_carries_a_reason() -> None:
    """A blank box teaches the operator the feature is broken. Three causes, three notes.

    Read out of the source rather than exercised, because reaching two of these branches
    needs a loaded model and this suite must run without one.
    """
    # Quote style comes from `ast.unparse`, not from the file, so both are accepted.
    returns = re.findall(r"""return \(['"]{2},\s*([^)]+)\)""", _final_pass_source())
    assert returns, "the accurate pass must have empty-result branches to check"
    assert "None" not in {value.strip() for value in returns}, (
        "an empty accurate result must always say why; found a bare return"
    )


def test_the_panel_explains_each_reason(block: str) -> None:
    pattern = r"""return \(['"]{2},\s*['"]([A-Z_]+)['"]\)"""
    reasons = re.findall(pattern, _final_pass_source())
    assert reasons, "the accurate pass must name its empty-result reasons"
    for reason in reasons:
        assert reason in block, f"the panel has no wording for {reason}"


def test_the_panel_shows_the_accurate_text_not_only_the_stream(block: str, html: str) -> None:
    """The streaming lines are reassurance; the accurate pass is the answer."""
    assert "final_text" in block
    assert 'id="voice-final"' in html


def test_the_panel_says_the_verdict_in_the_language_it_is_written_in(block: str) -> None:
    """The service's advice is English, written for the CLI. This panel is not the CLI.

    Every verdict the quality module can return must have wording here, so a new one
    cannot reach a non-technical operator as untranslated English by default.
    """
    from mom_igd.audio.quality import LevelVerdict

    for verdict in LevelVerdict:
        assert verdict.value in block, f"the panel has no wording for {verdict.value}"


def test_every_verdict_that_needs_an_action_names_the_setting(block: str) -> None:
    """A warning that does not name a remedy is one the operator learns to ignore."""
    wording = block[block.index("var VOICE_VERDICT") :]
    wording = wording[: wording.index("};")]
    # Each entry runs from its own key to the next one, so a remedy written under
    # TOO_QUIET cannot be miscounted as covering TOO_LOUD.
    keys = ["NO_SIGNAL", "TOO_QUIET", "GOOD", "TOO_LOUD", "CLIPPING"]
    spans = sorted((wording.index(key), key) for key in keys)
    for position, (start, key) in enumerate(spans):
        if key == "GOOD":
            continue  # nothing to fix when the level is already right
        end = spans[position + 1][0] if position + 1 < len(spans) else len(wording)
        clause = wording[start:end]
        assert "Sound" in clause or "mikrofon" in clause, (
            f"{key} tells the operator something is wrong without naming what to change"
        )


# ===========================================================================
# The duplication: every preview line printed twice
# ===========================================================================


def _extract_function(source: str, name: str) -> str:
    """Pull one JavaScript function out of `app.js` by matching its braces.

    Crude, and adequate: the shell has no build step and no module system, so there is
    nothing to import. Braces inside string literals would break it, and `voiceAppend`
    has none.
    """
    start = source.index("function " + name)
    depth = 0
    for position in range(source.index("{", start), len(source)):
        if source[position] == "{":
            depth += 1
        elif source[position] == "}":
            depth -= 1
            if depth == 0:
                return source[start : position + 1]
    raise AssertionError(f"unbalanced braces around {name}")


def test_the_counter_of_rendered_lines_never_moves_backwards() -> None:
    """The bug on the operator's screen: all three preview lines appeared twice.

    `voiceAppend` ended with `voiceSeen = segments.length`, which reads as "remember how
    many there are" but is used as "remember how many are drawn". Those differ exactly
    once, and it is not a rare case: `/audio/live` answers with an empty list as soon as
    the transcriber is cleared, and that happens while the accurate pass is still
    running. The last poll in flight came back empty, reset the counter to zero, and the
    final response redrew the whole list under the copy already on screen.

    Run rather than read, because the reset was invisible to inspection -- the assignment
    looked correct on the line it was written on.
    """
    import json
    import shutil
    import subprocess

    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")

    source = (WEB / "app.js").read_text(encoding="utf-8")
    harness = """
      var rendered = [];
      var voiceSeen = 0;
      var document = {
        createElement: function () {
          return { appendChild: function () {}, className: '', textContent: '' };
        },
        createTextNode: function (t) { return { text: t }; }
      };
      var voice = {
        output: { appendChild: function (n) { rendered.push(n); }, scrollTop: 0,
                  scrollHeight: 0 },
        placeholder: {}
      };
      function voiceShow() {}
      __FN__
      var segs = [
        { text: 'satu', started_ms: 0 },
        { text: 'dua', started_ms: 5000 },
        { text: 'tiga', started_ms: 10000 }
      ];
      voiceAppend(segs);   // the polls, while the microphone is open
      voiceAppend([]);     // a poll in flight after the transcriber was cleared
      voiceAppend(segs);   // the final response, carrying the whole list again
      console.log(JSON.stringify({ lines: rendered.length, seen: voiceSeen }));
    """.replace("__FN__", _extract_function(source, "voiceAppend"))

    result = subprocess.run(  # noqa: S603 - fixed executable, generated script
        [node, "-e", harness], capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0, result.stderr
    outcome = json.loads(result.stdout)
    assert outcome["lines"] == 3, (
        f"each preview line was drawn {outcome['lines'] / 3:.0f} times; an empty poll "
        "must not let the final response redraw what is already on screen"
    )
    assert outcome["seen"] == 3


def test_the_statistics_do_not_call_a_block_of_text_one_sentence(block: str) -> None:
    """"Kalimat terbaca: 1" sat beside three visible lines and read like a fault."""
    assert "Kalimat terbaca" not in block
    assert "Baris pratinjau" in block
