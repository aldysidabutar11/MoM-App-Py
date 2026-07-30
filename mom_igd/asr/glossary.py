"""Terminology normalisation: fix the spelling of technical terms, change nothing else.

Indonesian meetings in this domain are full of English technical vocabulary, and Whisper
spells what it hears -- "deploy" comes back as "deploi", "database" as "data base". Those
are reasonable phonetic transcriptions and useless terms: a reader searching the minutes
for "deploy" will not find "deploi".

**This is a spelling normaliser and nothing more.** It replaces whole words with a
canonical form from a reviewed list. It does not translate, paraphrase, expand
abbreviations, summarise, or make any decision about meaning. Anything cleverer would be
editing the record.

**Whole words only, and that is enforced.** Matching is on word boundaries, so "api" never
fires inside "apik" and "lab" never fires inside "laboratorium". A variant shorter than
three characters is refused at load time, because a two-letter variant will eventually
collide with an ordinary Indonesian word.

**The original is kept.** Every rewrite is counted and the untouched text is stored beside
the normalised one, because a transformation of evidence that cannot be undone is not a
transformation, it is a loss. A reviewer can always see what the model actually said.

**Two uses, one list.** The same glossary supplies the decoder's initial prompt -- which
influences the model before it decides anything, and is bounded because a long prompt
evicts the audio context it is meant to help -- and the post-decode normaliser. Keeping
them on one list means the terms the prompt teaches are the terms the normaliser expects.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, Iterable, Mapping, Sequence

from mom_igd.logging_setup import get_logger

__all__ = [
    "GLOSSARY_FILENAME",
    "MIN_VARIANT_LENGTH",
    "Glossary",
    "GlossaryError",
    "Term",
    "load_glossary",
]

_LOG = get_logger("asr.glossary")

GLOSSARY_FILENAME: Final[str] = "glossary.id-en.toml"

#: A variant shorter than this is refused. Two-letter tokens are common Indonesian
#: words ("di", "ke", "ya") and a rule on one of those would corrupt ordinary prose.
MIN_VARIANT_LENGTH: Final[int] = 3

#: Word characters for boundary detection. Deliberately not ``\b``: the variants include
#: multi-word phrases ("data base"), and a hand-rolled boundary check makes the rule the
#: same for one word and for three.
_WORD_CHARS: Final[re.Pattern[str]] = re.compile(r"\w", re.UNICODE)


class GlossaryError(RuntimeError):
    """The glossary is malformed. Loaded strictly: a bad rule silently corrupts text."""


@dataclass(frozen=True, slots=True)
class Term:
    """One canonical spelling and the misspellings that map onto it."""

    canonical: str
    variants: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {"canonical": self.canonical, "variants": list(self.variants)}


@dataclass(slots=True)
class Glossary:
    """A loaded, validated term list with its provenance."""

    version: str
    terms: tuple[Term, ...]
    sha256: str
    source_name: str
    #: Compiled (pattern, canonical) pairs, longest variant first so a multi-word
    #: variant is matched before one of its own words.
    _rules: tuple[tuple[re.Pattern[str], str], ...] = field(default_factory=tuple)

    @property
    def canonical_terms(self) -> tuple[str, ...]:
        return tuple(term.canonical for term in self.terms)

    def initial_prompt(self, *, max_chars: int) -> str:
        """A bounded, comma-separated term list for the decoder's initial prompt.

        Terms are taken in file order and the list is truncated at a term boundary, so a
        prompt is never a half-written word. Order is the file's rather than sorted, so an
        author can put the terms that matter most first and know they survive truncation.
        """
        if max_chars <= 0 or not self.terms:
            return ""
        parts: list[str] = []
        length = 0
        for term in self.terms:
            candidate = term.canonical
            addition = len(candidate) + (2 if parts else 0)
            if length + addition > max_chars:
                break
            parts.append(candidate)
            length += addition
        return ", ".join(parts)

    def normalise(self, text: str) -> tuple[str, int]:
        """Return the corrected text and how many words were actually changed.

        A match that already reads correctly is **not** counted. That matters because
        matching is case-insensitive, so a case-only rule (``bpjs`` -> ``BPJS``) matches
        its own output: counting every match would make the reported number grow on every
        pass and make normalisation look non-idempotent when it is not.
        """
        if not text:
            return text, 0
        result = text
        changed = 0
        for pattern, canonical in self._rules:

            def _replace(match: re.Match[str], canonical: str = canonical) -> str:
                nonlocal changed
                found = match.group(0)
                replacement = _target(canonical, found)
                if replacement != found:
                    changed += 1
                return replacement

            result = pattern.sub(_replace, result)
        return result, changed

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "sha256": self.sha256,
            "source_name": self.source_name,
            "term_count": len(self.terms),
            "variant_count": sum(len(term.variants) for term in self.terms),
        }


def _target(canonical: str, found: str) -> str:
    """What a matched variant should become, keeping deliberate capitalisation.

    An all-caps canonical form ("API", "BPJS") is always written as declared -- that *is*
    the correct spelling. Otherwise, a variant that appeared capitalised (start of a
    sentence) keeps its capital, because lower-casing it would break the sentence.
    """
    if canonical.isupper():
        return canonical
    if found[:1].isupper():
        return canonical[:1].upper() + canonical[1:]
    return canonical


def _boundary_pattern(variant: str) -> re.Pattern[str]:
    """Compile a whole-word(s) matcher for one variant.

    ``(?<!\\w)`` / ``(?!\\w)`` rather than ``\\b`` so a multi-word variant behaves the
    same as a single word, and so a variant that begins or ends with a non-word character
    does not silently match everywhere.
    """
    escaped = r"\s+".join(re.escape(part) for part in variant.split())
    return re.compile(rf"(?<!\w){escaped}(?!\w)", re.IGNORECASE | re.UNICODE)


def _validate(raw: Mapping[str, Any], source_name: str) -> tuple[str, tuple[Term, ...]]:
    version = str(raw.get("version") or "").strip()
    if not version:
        raise GlossaryError(
            f"{source_name} has no `version`. The version is recorded on every "
            "transcript it normalises, so a transcript produced under an older term "
            "list stays identifiable."
        )
    entries = raw.get("terms")
    if not isinstance(entries, list) or not entries:
        raise GlossaryError(
            f"{source_name} lists no terms. Use `[[terms]]` tables with `canonical` "
            "and `variants`, or turn the normaliser off with "
            "`[asr].glossary_enabled = false`."
        )

    terms: list[Term] = []
    seen_variants: dict[str, str] = {}
    canonicals: set[str] = set()
    for index, entry in enumerate(entries):
        if not isinstance(entry, Mapping):
            raise GlossaryError(f"{source_name} term #{index} is not a table")
        canonical = str(entry.get("canonical") or "").strip()
        if not canonical:
            raise GlossaryError(f"{source_name} term #{index} has no `canonical`")
        if not _WORD_CHARS.search(canonical):
            raise GlossaryError(
                f"{source_name} term #{index} canonical {canonical!r} contains no word "
                "characters"
            )
        folded_canonical = canonical.casefold()
        if folded_canonical in canonicals:
            raise GlossaryError(
                f"{source_name} declares {canonical!r} twice. Merge the two entries -- "
                "two rules for one term make the outcome depend on file order."
            )
        canonicals.add(folded_canonical)

        raw_variants = entry.get("variants") or ()
        if not isinstance(raw_variants, Sequence) or isinstance(raw_variants, str):
            raise GlossaryError(
                f"{source_name} term {canonical!r} has `variants` that is not a list"
            )
        variants: list[str] = []
        for variant in raw_variants:
            text = str(variant).strip()
            if len(text) < MIN_VARIANT_LENGTH:
                raise GlossaryError(
                    f"{source_name} term {canonical!r} has variant {text!r}, which is "
                    f"shorter than {MIN_VARIANT_LENGTH} characters. A very short "
                    "variant will collide with an ordinary Indonesian word and rewrite "
                    "prose that was already correct."
                )
            if text == canonical:
                raise GlossaryError(
                    f"{source_name} term {canonical!r} lists itself, character for "
                    "character, as a variant. That rule can only ever replace text with "
                    "itself."
                )
            folded = text.casefold()
            if folded in seen_variants:
                raise GlossaryError(
                    f"{source_name} maps variant {text!r} to both "
                    f"{seen_variants[folded]!r} and {canonical!r}. One variant cannot "
                    "have two corrections -- the winner would depend on file order."
                )
            seen_variants[folded] = canonical
            variants.append(text)
        if not variants:
            raise GlossaryError(
                f"{source_name} term {canonical!r} has no variants, so it can never "
                "fire. Remove it, or give it the misspellings it is meant to correct."
            )
        terms.append(Term(canonical=canonical, variants=tuple(variants)))

    # A variant that is another term's canonical form would rewrite a correct spelling
    # into a different term. Checked across the whole file, after every term is known.
    for folded, owner in seen_variants.items():
        if folded in canonicals and folded != owner.casefold():
            raise GlossaryError(
                f"{source_name} lists {folded!r} as a variant of {owner!r}, but it is "
                "also another term's canonical spelling. That rule would rewrite text "
                "that was already correct."
            )
    return version, tuple(terms)


def load_glossary(path: str | Path) -> Glossary:
    """Load, validate and compile a glossary from disk.

    Strict on purpose. A malformed rule does not fail loudly at runtime -- it silently
    rewrites the wrong words in somebody's minutes, and nobody notices until a reader
    disputes a sentence the model never produced.
    """
    from mom_igd.asr.manifest import sha256_file

    source = Path(path)
    if not source.is_file():
        raise GlossaryError(
            f"the glossary {source.name} does not exist at {source.parent}. Either "
            "restore it or set `[asr].glossary_enabled = false`."
        )
    try:
        raw = tomllib.loads(source.read_text(encoding="utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        raise GlossaryError(f"{source.name} is not valid UTF-8 TOML: {exc}") from None

    version, terms = _validate(raw, source.name)
    # Longest variant first, so "data base" is corrected before a rule for "base" could
    # take half of it. Length then alphabetical, so the order is fully determined.
    rules = sorted(
        ((variant, term.canonical) for term in terms for variant in term.variants),
        key=lambda pair: (-len(pair[0]), pair[0]),
    )
    glossary = Glossary(
        version=version,
        terms=terms,
        sha256=sha256_file(source),
        source_name=source.name,
        _rules=tuple((_boundary_pattern(variant), canonical) for variant, canonical in rules),
    )
    _LOG.info(
        "asr.glossary.loaded",
        extra={
            "version": version,
            "terms": len(terms),
            "variants": sum(len(term.variants) for term in terms),
        },
    )
    return glossary


def normalise_segments(
    segments: Iterable[Mapping[str, Any]], glossary: Glossary | None
) -> tuple[tuple[dict[str, Any], ...], int]:
    """Apply the glossary to a segment list, keeping the raw text beside the corrected.

    With no glossary the segments still gain ``text_raw``, equal to ``text``. That keeps
    every reader on one shape instead of two, and makes "normalisation was off" visible
    as zero replacements rather than as a missing field.
    """
    out: list[dict[str, Any]] = []
    total = 0
    for segment in segments:
        row = dict(segment)
        original = str(row.get("text") or "")
        row.setdefault("text_raw", original)
        if glossary is None:
            row["glossary_replacements"] = 0
            out.append(row)
            continue
        corrected, count = glossary.normalise(original)
        row["text"] = corrected
        row["text_raw"] = original
        row["glossary_replacements"] = count
        total += count
        out.append(row)
    return tuple(out), total
