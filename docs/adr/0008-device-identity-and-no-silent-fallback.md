# ADR-0008 — Device identity, verified transport, and no silent fallback

* **Status:** Accepted
* **Phase:** 2

## Context

Three questions had to be settled before a device could be chosen reliably: how to
remember *which* microphone the operator picked, what to do when it is gone, and how
to tell a USB conference microphone from a laptop array — the distinction the Phase 2
production gate rests on.

Phase 0 established that the built-in Intel Smart Sound array applies beamforming and
noise suppression that suppress speakers who are not facing the laptop. For nine
people around a table that loses voices outright and makes voiceprints inconsistent
for Phase 6. So "is this actually a USB conference microphone?" has to be a fact, not
an impression.

## Decision

### A PortAudio index is not an identity

Indices shift when a device is plugged in, unplugged, or after a reboot. Persisting
one and reusing it later can point at a completely different microphone.

Devices are identified by a **fingerprint**: SHA-256 over host API, normalised name
and input-channel count, truncated to 32 hex characters. The index is looked up fresh
every time and kept only as a diagnostic hint. The same physical microphone exposed
through two host APIs yields two fingerprints, which is correct — they are different
capture paths with different capabilities.

`audio.preferred_device_fingerprint` is **empty in the tracked `default.toml`**: a
fingerprint identifies one microphone on one machine, so committing one would make
every other machine start with a selection it cannot resolve.

### Never fall back silently

If the selected device is absent, `resolve_selection` raises, naming the device that
was expected and listing what is present instead. It does not substitute another
microphone, and the operator decides what to do.

This is the rule the whole module exists to protect. A silent fallback produces a
recording of a nine-person meeting through the wrong microphone — discovered only
afterwards, with nothing left to re-record.

The same reasoning rejects **virtual aggregate endpoints**: MME's "Microsoft Sound
Mapper" and DirectSound's "Primary Sound Capture Driver" follow whatever Windows
currently considers the default, so the actual input could change *mid-recording*
with no record of it. They are refused with that reason.

### Never guess the transport

`USB` is reported only when the operating system says so. On Windows the audio stack
records each endpoint's bus in
`HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\MMDevices\Audio\Capture\*\Properties`
under `PKEY_Device_EnumeratorName` (`USB`, `HDAUDIO`, `INTELAUDIO`, `BTHENUM`, …),
read read-only with the standard-library `winreg`. No WMI, no subprocess, no write.

When it cannot be verified the answer is `UNKNOWN` and the operator is asked to
confirm. A device named "USB Audio Device" may not be one, and a false "this is a USB
conference microphone" would make the production gate meaningless.

Two details were learned the hard way, by probing real hardware:

* **Matching must be exact, not substring.** PortAudio composes Windows names as
  `"<endpoint description> (<adapter name>)"`. Substring matching let a Bluetooth
  endpoint described simply as `"Microphone"` match `"Microphone Array (Intel Smart
  Sound…)"`, which produced two different buses and reported `UNKNOWN` for the
  built-in array. Matching now requires the description to equal the head of the
  PortAudio name.
* **Active endpoints must win.** Windows keeps a stale record for every device ever
  attached; this machine had 30 capture endpoints, of which one was active. Taking
  the first match rejected the only working microphone as "disabled".

Rejecting on a name *is* allowed where it is conservative: "Stereo Mix" and
"loopback" record what the computer plays rather than the room, and a false positive
costs one unusable entry in a list. Asserting `USB` on a name is not conservative,
and is never done.

### Windows evidence is authoritative for usability

PortAudio's WDM-KS host API exposes output nodes such as "PC Speaker" with a non-zero
input-channel count, so the channel count alone cannot distinguish a microphone from
a loudspeaker. When registry evidence is available, a device that is not a registered
capture endpoint is not a microphone, whatever it claims. Without that evidence (any
non-Windows host, or an unreadable registry) the check falls back to channel count
and the loopback/virtual name rules, because rejecting everything would be worse.

On the development machine this correctly reduced 21 enumerated devices to 3 usable
entries and excluded 17 with a specific reason each.

### The seam is late-bound

`DeviceDiscoveryService(endpoint_provider=None)` resolves the registry function at
call time rather than binding it as a default argument. A default captured at
definition time cannot be substituted, which makes the seam useless to tests and to
any non-Windows caller — a testability defect found while writing the CLI tests.

### Nothing on the system is changed

No gain, no AGC, no microphone enhancement, no permission, no registry write, no
power plan. When the level is wrong the operator is told what to adjust, in words
that name the setting.

## Consequences

**Good.** A replug or reboot does not break a saved selection: the fingerprint holds
while the index moves. The production gate has real evidence behind it. Every
exclusion carries a reason the operator can act on, rather than a device silently
missing from a list.

**Bad / accepted.** Transport is `UNKNOWN` more often than a name-based guess would
claim — including for devices that genuinely are USB but whose Windows description
does not match PortAudio's name. That is the intended trade: an honest `UNKNOWN`
requiring one manual confirmation beats a confident wrong answer. Two microphones
with an identical name, host API and channel count would share a fingerprint; no
conference setup in scope has that, and the alternative (persisting an index) is
worse. On a non-Windows host the transport is always `UNKNOWN`, which is accurate.
