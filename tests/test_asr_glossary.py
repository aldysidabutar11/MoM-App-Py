"""Terminology normalisation: a spelling corrector that must never edit meaning.

This is the one stage that rewrites what the model said, so it is loaded strictly and
matched narrowly. A malformed rule does not fail loudly at runtime -- it silently rewrites
the wrong words in somebody's minutes, and nobody notices until a reader disputes a
sentence the model never produced.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from mom_igd.asr.glossary import (
    MIN_VARIANT_LENGTH,
    GlossaryError,
    load_glossary,
    normalise_segments,
)

SHIPPED = Path(__file__).resolve().parent.parent / "config" / "glossary.id-en.toml"


def _write(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "glossary.toml"
    path.write_text(body, encoding="utf-8")
    return path


# ===========================================================================
# Loading: strict, and refusing rules that would corrupt text
# ===========================================================================


def test_the_shipped_glossary_loads() -> None:
    glossary = load_glossary(SHIPPED)
    assert glossary.version
    assert len(glossary.terms) > 20
    assert len(glossary.sha256) == 64


def test_a_missing_file_names_the_way_out(tmp_path: Path) -> None:
    with pytest.raises(GlossaryError, match="glossary_enabled"):
        load_glossary(tmp_path / "absent.toml")


def test_invalid_toml_is_refused(tmp_path: Path) -> None:
    with pytest.raises(GlossaryError, match="not valid UTF-8 TOML"):
        load_glossary(_write(tmp_path, "version = "))


def test_a_glossary_without_a_version_is_refused(tmp_path: Path) -> None:
    """The version is recorded on every transcript it touches."""
    body = '[[terms]]\ncanonical = "deploy"\nvariants = ["deploi"]\n'
    with pytest.raises(GlossaryError, match="no `version`"):
        load_glossary(_write(tmp_path, body))


@pytest.mark.parametrize("body", ['version = "1"\n', 'version = "1"\nterms = []\n'])
def test_a_glossary_with_no_terms_is_refused(tmp_path: Path, body: str) -> None:
    with pytest.raises(GlossaryError, match="lists no terms"):
        load_glossary(_write(tmp_path, body))


def test_a_term_without_a_canonical_form_is_refused(tmp_path: Path) -> None:
    body = 'version = "1"\n[[terms]]\nvariants = ["deploi"]\n'
    with pytest.raises(GlossaryError, match="no `canonical`"):
        load_glossary(_write(tmp_path, body))


def test_a_term_with_no_variants_is_refused(tmp_path: Path) -> None:
    """It could never fire, so its presence is a false sense of coverage."""
    body = 'version = "1"\n[[terms]]\ncanonical = "deploy"\nvariants = []\n'
    with pytest.raises(GlossaryError, match="no variants"):
        load_glossary(_write(tmp_path, body))


def test_a_variant_shorter_than_the_minimum_is_refused(tmp_path: Path) -> None:
    """A two-letter variant will collide with an ordinary Indonesian word."""
    body = 'version = "1"\n[[terms]]\ncanonical = "deploy"\nvariants = ["di"]\n'
    with pytest.raises(GlossaryError, match=str(MIN_VARIANT_LENGTH)):
        load_glossary(_write(tmp_path, body))


def test_a_variant_identical_to_its_canonical_form_is_refused(tmp_path: Path) -> None:
    """That rule can only ever replace text with itself."""
    body = 'version = "1"\n[[terms]]\ncanonical = "deploy"\nvariants = ["deploy"]\n'
    with pytest.raises(GlossaryError, match="character for"):
        load_glossary(_write(tmp_path, body))


def test_a_case_only_variant_is_allowed_and_corrects_the_case(tmp_path: Path) -> None:
    """`bpjs` -> `BPJS` is a real correction, and the commonest one for an acronym."""
    body = 'version = "1"\n[[terms]]\ncanonical = "BPJS"\nvariants = ["bpjs"]\n'
    glossary = load_glossary(_write(tmp_path, body))
    assert glossary.normalise("klaim bpjs") == ("klaim BPJS", 1)


def test_a_case_only_rule_does_not_re_fire_on_its_own_output(tmp_path: Path) -> None:
    """Matching is case-insensitive, so it matches what it just wrote. It must not count."""
    body = 'version = "1"\n[[terms]]\ncanonical = "BPJS"\nvariants = ["bpjs"]\n'
    glossary = load_glossary(_write(tmp_path, body))
    once, first = glossary.normalise("klaim bpjs")
    twice, second = glossary.normalise(once)
    assert (twice, second) == (once, 0)
    assert first == 1


def test_one_variant_mapping_to_two_terms_is_refused(tmp_path: Path) -> None:
    """The winner would depend on file order."""
    body = (
        'version = "1"\n'
        '[[terms]]\ncanonical = "deploy"\nvariants = ["deploi"]\n'
        '[[terms]]\ncanonical = "deployment"\nvariants = ["deploi"]\n'
    )
    with pytest.raises(GlossaryError, match="cannot have two corrections"):
        load_glossary(_write(tmp_path, body))


def test_a_duplicated_canonical_term_is_refused(tmp_path: Path) -> None:
    body = (
        'version = "1"\n'
        '[[terms]]\ncanonical = "deploy"\nvariants = ["deploi"]\n'
        '[[terms]]\ncanonical = "Deploy"\nvariants = ["diploy"]\n'
    )
    with pytest.raises(GlossaryError, match="twice"):
        load_glossary(_write(tmp_path, body))


def test_a_variant_that_is_another_terms_canonical_form_is_refused(tmp_path: Path) -> None:
    """That rule would rewrite correct text into a different term."""
    body = (
        'version = "1"\n'
        '[[terms]]\ncanonical = "server"\nvariants = ["serper"]\n'
        '[[terms]]\ncanonical = "backend"\nvariants = ["server"]\n'
    )
    with pytest.raises(GlossaryError, match="already correct"):
        load_glossary(_write(tmp_path, body))


def test_the_shipped_glossary_has_no_person_or_client_name() -> None:
    """It is committed. A rule list is not a place for anybody's name."""
    text = SHIPPED.read_text(encoding="utf-8").lower()
    for forbidden in ("aldy", "pangsor", "sidabutar", "@", "password", "token"):
        assert forbidden not in text, forbidden


def test_no_shipped_variant_is_a_common_indonesian_word() -> None:
    """Rewriting ordinary prose is the one failure mode that matters most here."""
    glossary = load_glossary(SHIPPED)
    common = {
        "kode", "data", "jalan", "kerja", "hasil", "waktu", "orang", "rapat",
        "sudah", "belum", "dengan", "untuk", "yang", "akan", "bisa", "harus",
        "tidak", "lebih", "juga", "dari", "pada", "atau", "saja", "baru",
    }
    for term in glossary.terms:
        for variant in term.variants:
            assert variant.casefold() not in common, (
                f"{variant!r} is ordinary Indonesian and would corrupt prose"
            )


# ===========================================================================
# Matching: whole words only
# ===========================================================================


def test_a_variant_is_corrected(tmp_path: Path) -> None:
    glossary = load_glossary(
        _write(tmp_path, 'version = "1"\n[[terms]]\ncanonical = "deploy"\nvariants = ["deploi"]\n')
    )
    assert glossary.normalise("kita deploi besok") == ("kita deploy besok", 1)


def test_a_variant_inside_a_longer_word_is_not_corrected(tmp_path: Path) -> None:
    """A variant must match on word boundaries, never as a substring."""
    glossary = load_glossary(
        _write(tmp_path, 'version = "1"\n[[terms]]\ncanonical = "API"\nvariants = ["epi ai"]\n')
    )
    assert glossary.normalise("kerjanya epi aime") == ("kerjanya epi aime", 0)
    assert glossary.normalise("dokumen epi ai siap") == ("dokumen API siap", 1)


def test_an_ordinary_indonesian_word_is_left_alone_by_the_shipped_glossary() -> None:
    """`api` means "fire". Rewriting it to the acronym would corrupt the sentence."""
    glossary = load_glossary(SHIPPED)
    assert glossary.normalise("kebakaran api besar") == ("kebakaran api besar", 0)


def test_a_multi_word_variant_matches_only_as_a_phrase(tmp_path: Path) -> None:
    glossary = load_glossary(
        _write(
            tmp_path,
            'version = "1"\n[[terms]]\ncanonical = "database"\nvariants = ["data base"]\n',
        )
    )
    assert glossary.normalise("data base baru")[1] == 1
    assert glossary.normalise("data dan base terpisah")[1] == 0


def test_extra_whitespace_inside_a_phrase_still_matches(tmp_path: Path) -> None:
    glossary = load_glossary(
        _write(
            tmp_path,
            'version = "1"\n[[terms]]\ncanonical = "database"\nvariants = ["data base"]\n',
        )
    )
    assert glossary.normalise("data   base")[1] == 1


def test_matching_ignores_case(tmp_path: Path) -> None:
    glossary = load_glossary(
        _write(tmp_path, 'version = "1"\n[[terms]]\ncanonical = "deploy"\nvariants = ["deploi"]\n')
    )
    assert glossary.normalise("DEPLOI")[1] == 1
    assert glossary.normalise("Deploi")[1] == 1


def test_a_capitalised_variant_keeps_its_capital(tmp_path: Path) -> None:
    """Lower-casing it would break the sentence it starts."""
    glossary = load_glossary(
        _write(tmp_path, 'version = "1"\n[[terms]]\ncanonical = "deploy"\nvariants = ["deploi"]\n')
    )
    assert glossary.normalise("Deploi besok.") == ("Deploy besok.", 1)


def test_an_all_caps_canonical_form_is_always_written_as_declared(tmp_path: Path) -> None:
    glossary = load_glossary(
        _write(tmp_path, 'version = "1"\n[[terms]]\ncanonical = "BPJS"\nvariants = ["bejeis"]\n')
    )
    assert glossary.normalise("klaim bejeis")[0] == "klaim BPJS"
    assert glossary.normalise("Bejeis sudah")[0] == "BPJS sudah"


def test_a_longer_variant_wins_over_a_shorter_one(tmp_path: Path) -> None:
    """Otherwise a rule for "base" would take half of "data base"."""
    glossary = load_glossary(
        _write(
            tmp_path,
            'version = "1"\n'
            '[[terms]]\ncanonical = "database"\nvariants = ["data base"]\n'
            '[[terms]]\ncanonical = "basis"\nvariants = ["base"]\n',
        )
    )
    assert glossary.normalise("data base siap")[0] == "database siap"


def test_normalising_is_idempotent(tmp_path: Path) -> None:
    """Running it twice must not keep rewriting."""
    glossary = load_glossary(SHIPPED)
    once, first_count = glossary.normalise("Kita deploi ke serper")
    twice, second_count = glossary.normalise(once)
    assert twice == once
    assert first_count > 0
    assert second_count == 0


def test_empty_text_is_returned_unchanged(tmp_path: Path) -> None:
    glossary = load_glossary(SHIPPED)
    assert glossary.normalise("") == ("", 0)


def test_the_count_is_the_number_of_replacements_not_terms(tmp_path: Path) -> None:
    glossary = load_glossary(
        _write(tmp_path, 'version = "1"\n[[terms]]\ncanonical = "deploy"\nvariants = ["deploi"]\n')
    )
    assert glossary.normalise("deploi deploi deploi")[1] == 3


# ===========================================================================
# The initial prompt
# ===========================================================================


def test_the_prompt_is_bounded(tmp_path: Path) -> None:
    """A long prompt evicts the audio context it is meant to help."""
    glossary = load_glossary(SHIPPED)
    prompt = glossary.initial_prompt(max_chars=100)
    assert len(prompt) <= 100
    assert prompt


def test_the_prompt_is_truncated_at_a_term_boundary() -> None:
    glossary = load_glossary(SHIPPED)
    prompt = glossary.initial_prompt(max_chars=40)
    assert not prompt.endswith(",")
    for term in prompt.split(", "):
        assert term in glossary.canonical_terms, f"{term!r} is a fragment"


def test_a_zero_budget_produces_no_prompt() -> None:
    assert load_glossary(SHIPPED).initial_prompt(max_chars=0) == ""


def test_the_prompt_keeps_file_order_so_priority_survives_truncation() -> None:
    glossary = load_glossary(SHIPPED)
    prompt = glossary.initial_prompt(max_chars=60)
    assert prompt.split(", ") == list(glossary.canonical_terms[: len(prompt.split(", "))])


def test_the_prompt_teaches_no_misspelling() -> None:
    """Putting a variant in the prompt would be the opposite of the intent.

    Compared case-sensitively on purpose: a case-only variant ("bpjs" for "BPJS") is a
    correction, not a misspelling, and its canonical form belongs in the prompt.
    """
    glossary = load_glossary(SHIPPED)
    prompt = glossary.initial_prompt(max_chars=4000)
    misspellings = {
        variant
        for term in glossary.terms
        for variant in term.variants
        if variant.casefold() != term.canonical.casefold()
    }
    for entry in prompt.split(", "):
        assert entry not in misspellings, entry
    assert set(prompt.split(", ")) <= set(glossary.canonical_terms)


# ===========================================================================
# Applying it to a segment list
# ===========================================================================


def test_the_original_text_is_kept_beside_the_corrected_one() -> None:
    """A transformation of evidence that cannot be undone is a loss."""
    glossary = load_glossary(SHIPPED)
    segments, total = normalise_segments([{"text": "kita deploi"}], glossary)
    assert segments[0]["text"] == "kita deploy"
    assert segments[0]["text_raw"] == "kita deploi"
    assert segments[0]["glossary_replacements"] == 1
    assert total == 1


def test_with_no_glossary_the_shape_is_the_same_and_the_count_is_zero() -> None:
    """"Normalisation was off" must be visible, not a missing field."""
    segments, total = normalise_segments([{"text": "kita deploi"}], None)
    assert segments[0]["text"] == "kita deploi"
    assert segments[0]["text_raw"] == "kita deploi"
    assert segments[0]["glossary_replacements"] == 0
    assert total == 0


def test_the_total_is_summed_across_segments() -> None:
    glossary = load_glossary(SHIPPED)
    _segments, total = normalise_segments(
        [{"text": "deploi"}, {"text": "serper dan deploi"}], glossary
    )
    assert total == 3


def test_the_input_segments_are_not_mutated() -> None:
    glossary = load_glossary(SHIPPED)
    original = {"text": "kita deploi"}
    normalise_segments([original], glossary)
    assert original == {"text": "kita deploi"}


def test_a_segment_with_no_text_is_handled() -> None:
    glossary = load_glossary(SHIPPED)
    segments, total = normalise_segments([{"start_ms": 0}], glossary)
    assert segments[0]["text"] == ""
    assert total == 0


def test_the_provenance_block_carries_the_digest_and_counts() -> None:
    payload = load_glossary(SHIPPED).to_dict()
    assert set(payload) == {
        "version",
        "sha256",
        "source_name",
        "term_count",
        "variant_count",
    }
    assert payload["variant_count"] >= payload["term_count"]
