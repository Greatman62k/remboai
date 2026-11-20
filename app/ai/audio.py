"""Safe wrapping for raw RemboAI Glance microphone PCM before existing ASR."""
from __future__ import annotations

import io
import wave

SAMPLE_RATE = 16_000
SAMPLE_WIDTH = 2
MAX_SECONDS = 40
MAX_PCM_BYTES = SAMPLE_RATE * SAMPLE_WIDTH * MAX_SECONDS


def wrap_pcm_s16le_mono_16khz(pcm: bytes) -> bytes:
    """Wrap signed 16-bit little-endian mono PCM in a WAV container.

    The returned bytes can be passed to the existing transcriber as ``audio/wav``.
    The input is never written to disk.
    """
    if not isinstance(pcm, (bytes, bytearray, memoryview)):
        raise TypeError("PCM audio must be bytes-like")
    data = bytes(pcm)
    if not data:
        raise ValueError("PCM audio is empty")
    if len(data) % SAMPLE_WIDTH:
        raise ValueError("16-bit PCM must contain an even number of bytes")
    if len(data) > MAX_PCM_BYTES:
        raise ValueError("PCM audio exceeds 40 seconds")
    output = io.BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(SAMPLE_WIDTH)
        wav.setframerate(SAMPLE_RATE)
        wav.writeframes(data)
    return output.getvalue()
