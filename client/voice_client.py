"""Interactive push-to-talk and typed fallback client."""

from __future__ import annotations

from client.api_client import HTTPAgentClient
from client.service import LocalAPIManager
from core.config import Settings
from voice.audio import SoundDeviceAudioCapture
from voice.session import VoiceSession
from voice.stt import FasterWhisperSTT
from voice.tts import MacOSSayTTS


def run_voice_client(settings: Settings) -> None:
    client = HTTPAgentClient(settings.voice_client_api_url)
    manager = LocalAPIManager(client)
    status = manager.ensure(can_start=bool(settings.gemini_api_key))
    if not status.ready:
        manager.stop()
        print(f"Cato voice could not connect: {status.message}")
        print(f"API URL: {client.base_url}")
        if not settings.gemini_api_key:
            print("Configure GEMINI_API_KEY in .env before starting Cato voice.")
        return
    try:
        voice = build_voice_session(settings, client=client)
    except ValueError as error:
        manager.stop()
        print(f"Cato voice could not start: {error}")
        return
    try:
        print("Preparing the local speech model. First use may download it...")
        prepared = voice.stt.prepare()
        if prepared.success:
            compute = prepared.metadata.get("compute_type", "configured default")
            print(f"Speech model ready ({compute}).")
        else:
            print(f"Speech model unavailable: {prepared.error}")
            print("Typed fallback remains available with /type TEXT.")
        _interactive_loop(voice)
    finally:
        voice.cancel()
        manager.stop()


def build_voice_session(
    settings: Settings, *, client: HTTPAgentClient | None = None
) -> VoiceSession:
    if settings.stt_provider != "faster-whisper":
        raise ValueError(f"Unsupported STT provider: {settings.stt_provider}")
    if settings.tts_provider != "macos-say":
        raise ValueError(f"Unsupported TTS provider: {settings.tts_provider}")
    capture = SoundDeviceAudioCapture(
        timeout_seconds=settings.recording_timeout_seconds,
        silence_timeout_seconds=settings.silence_timeout_seconds,
        device=settings.microphone_device,
        silence_threshold=settings.silence_threshold,
        block_size=settings.audio_block_size,
    )
    stt = FasterWhisperSTT(
        model=settings.stt_model,
        device=settings.stt_device,
        compute_type=settings.stt_compute_type,
    )
    tts = MacOSSayTTS(voice=settings.tts_voice, rate=settings.tts_rate)
    return VoiceSession(
        client=client or HTTPAgentClient(settings.voice_client_api_url),
        capture=capture,
        stt=stt,
        tts=tts,
        stt_timeout_seconds=settings.stt_timeout_seconds,
        max_spoken_characters=settings.max_spoken_characters,
    )


def _interactive_loop(voice: VoiceSession) -> None:
    print("Cato Voice")
    print("ENTER: speak | /type TEXT | /reset | /health | /cancel | /quit")
    while True:
        try:
            command = input("voice> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nVoice client stopped.")
            return
        if command == "/quit":
            return
        if command == "/reset":
            voice.reset()
            print("Started a new conversation session.")
            continue
        if command == "/health":
            print(voice.health())
            continue
        if command == "/cancel":
            voice.cancel()
            print("Cancelled active recording or speech.")
            continue
        if command.startswith("/type "):
            typed = command.removeprefix("/type ").strip()
            print(f"You: {typed}")
            turn = voice.handle_text(typed)
        elif command:
            print("Use /type TEXT for typed input, or press ENTER to record.")
            continue
        else:
            print("Listening...")
            turn = voice.listen_once()
            if turn.transcript:
                print(f"You: {turn.transcript}")
        print(f"Cato: {turn.text}")
        if turn.tts_error:
            print(f"Voice output unavailable: {turn.tts_error}")
