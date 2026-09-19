# Mission 5 Engineering Report

## Architecture before and after

Mission 4 provided the agent runtime, tools, sessions, approvals, macOS actions,
and HTTP API. Mission 5 adds a presentation/client layer rather than another
runtime: explicit audio capture → STT provider → local HTTP API → existing agent
and approvals → text response → bounded TTS presentation.

The `voice` package separates audio, STT, TTS, and conversation state. The
`client` package separates HTTP transport and the interactive push-to-talk CLI.
All hardware, model, process, and API boundaries are injectable for offline tests.

## Voice providers

`SpeechToText` returns structured `TranscriptionResult` values. `FakeSTT` is
deterministic. `FasterWhisperSTT` is an optional local provider loaded lazily; it
uses a temporary WAV because Faster Whisper accepts paths and deletes that file
in `finally` without retaining audio.

`TextToSpeech` returns structured `SpeechResult` values. `MacOSSayTTS` invokes
`/usr/bin/say` using an argument array, passes text after `--`, and never uses a
shell. It tracks and can terminate only its own subprocess. `FakeTTS` records
spoken output deterministically.

## Audio capture

`SoundDeviceAudioCapture` loads optional NumPy/sounddevice dependencies only when
used. Each call opens one input stream after an explicit user gesture, records
16-bit mono audio into memory, and stops on bounded duration, post-speech silence,
or cancellation. It exposes missing dependency, device, permission, silence, and
capture failures without bypassing macOS privacy controls. No background listener
or persistent raw-audio store exists.

## Client, sessions, and approvals

`HTTPAgentClient` uses the existing chat/approval endpoints with timeouts and
strict JSON-object validation. `VoiceSession` retains the returned session ID,
supports reset, retries a lost server session once, handles STT timeouts, and
always preserves the full textual response.

Long answers remain fully printed while only a bounded prefix and display notice
are spoken. TTS failures are reported separately and never erase agent text.

When an approval response arrives, the client retains its exact ID, session,
summary, and risk prompt. Only explicit normalized yes/no terms resolve the
current ID. No pending ID means “yes” does nothing. Unrelated utterances are
blocked until the approval is resolved. Success, denial, expiry, invalidation,
and replay responses clear pending client state.

## Files added

- `voice/audio.py`, `voice/stt.py`, `voice/tts.py`, `voice/session.py`
- `client/api_client.py`, `client/voice_client.py`
- package initializers and `tests/test_mission5.py`

## Files modified

- Configuration and `.env.example` for bounded voice settings
- CLI routing in `core/cato.py`
- Package metadata and optional voice dependencies
- README usage, privacy, approval, and troubleshooting documentation

## Testing and security

Tests cover deterministic and empty STT, provider exceptions/timeouts, safe TTS
arguments, cancellation/failure, audio start/stop/cancellation/permission and
dependency errors, session reuse/reset/recovery, exact approval and denial,
no-pending assent, expiry, API failure/malformed responses, long-response policy,
health, and TTS failure preservation. Normal tests use no microphone or network.

Security choices include explicit recording only, no transcript/audio logging,
no raw-audio persistence, shell-free TTS, exact pending approval binding, bounded
capture/transcription/speech, and graceful optional-dependency failure.

## Manual verification

Verification was non-invasive: CLI import/build paths, capability reporting logic,
fake microphone/STT/TTS/API flows, and subprocess argument inspection. No real
microphone capture, model download, spoken output, desktop action, or approval
action was triggered during implementation.

## Limitations and Mission 6

Faster Whisper model initialization may be slow and downloads a model initially.
The sounddevice backend depends on a working PortAudio installation. STT timeout
cannot forcibly stop third-party model code already running in its worker, though
the client returns promptly and temporary cleanup remains provider-owned. Sessions
and approvals are still process-local. There is no wake word or background mode.

Mission 6 should add authenticated local clients, encrypted durable sessions,
native menu-bar push-to-talk and approval UI, streaming transcription/TTS,
provider lifecycle management, and auditable privacy-preserving event telemetry.

## Real-world reliability sprint

Real microphone testing exposed that `/health` succeeded while lazy `/chat`
initialization failed, and every HTTP error was mislabeled as API unavailability.
Port 8000 was verified as Cato Uvicorn, but that process had no Gemini key:
health returned 200 while chat returned 500. Health now reports service identity
and provider readiness, chat returns an actionable 503, and the client preserves
connection, HTTP, malformed, stale, and non-Cato failure categories.

`cato voice` now reuses a ready service or starts and owns a temporary loopback
API. It never kills an unowned process and can select a free loopback port when a
stale/non-Cato service occupies the configured one. `cato health` prints detailed
voice readiness.

Faster Whisper now defaults to CPU and queries CTranslate2-supported compute
types, preferring `int8` and safely falling back to `float32`. Model loading is
locked, happens once, and is announced before recording. The default STT timeout
is 120 seconds. The prior audio threshold of `0.01` was incorrect for int16
samples and treated ambient noise as speech; defaults are now threshold 500,
1024-frame blocks, 1.2 seconds of silence, and a 12-second hard bound.
