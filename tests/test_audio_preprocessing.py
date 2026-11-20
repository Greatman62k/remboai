import io
import wave

import pytest

from app.ai.audio import MAX_PCM_BYTES, SAMPLE_RATE, SAMPLE_WIDTH, wrap_pcm_s16le_mono_16khz


def test_pcm_is_wrapped_as_mono_16khz_signed_16_bit_wav_without_changing_frames():
    pcm = b"\x01\x00\xff\xff" * (SAMPLE_RATE // 2)
    result = wrap_pcm_s16le_mono_16khz(pcm)
    with wave.open(io.BytesIO(result), "rb") as wav:
        assert wav.getnchannels() == 1
        assert wav.getsampwidth() == SAMPLE_WIDTH == 2
        assert wav.getframerate() == SAMPLE_RATE == 16_000
        assert wav.getnframes() == len(pcm) // SAMPLE_WIDTH
        assert wav.readframes(wav.getnframes()) == pcm


def test_pcm_accepts_exactly_40_seconds_and_rejects_oversize_payload():
    pcm = bytes(MAX_PCM_BYTES)
    result = wrap_pcm_s16le_mono_16khz(pcm)
    with wave.open(io.BytesIO(result), "rb") as wav:
        assert wav.getnframes() == SAMPLE_RATE * 40
    with pytest.raises(ValueError, match="40 seconds"):
        wrap_pcm_s16le_mono_16khz(pcm + b"\x00\x00")


@pytest.mark.parametrize(("pcm", "message"), [
    (b"", "empty"),
    (b"\x00", "even number"),
])
def test_pcm_rejects_empty_or_truncated_samples(pcm, message):
    with pytest.raises(ValueError, match=message):
        wrap_pcm_s16le_mono_16khz(pcm)


def test_pcm_rejects_non_bytes_input():
    with pytest.raises(TypeError, match="bytes-like"):
        wrap_pcm_s16le_mono_16khz("not PCM")
