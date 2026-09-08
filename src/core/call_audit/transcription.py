"""Replaceable, post-call transcription interface. No call-control dependencies."""

from dataclasses import dataclass
import io
import os
from typing import Protocol
import wave


@dataclass(frozen=True)
class Utterance:
    text: str
    start_ms: int
    end_ms: int
    confidence: float | None = None


class Transcriber(Protocol):
    name: str

    def transcribe(self, pcm: bytes, sample_rate: int) -> list[Utterance]: ...


class OpenAIRecordingTranscriber:
    """Reuse the configured Whisper model, with timestamped caller-only WAV input."""

    name = "openai_recording:whisper-1"

    def transcribe(self, pcm, sample_rate):
        from openai import OpenAI

        # Tiny uncovered tails are valid audit input. Pad, never discard them,
        # to meet the API's minimum audio length without changing the original WAV.
        pcm = pcm.ljust(max(len(pcm), sample_rate // 10 * 2), b"\0")
        audio = io.BytesIO()
        with wave.open(audio, "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(sample_rate)
            wav.writeframes(pcm)
        # Explicit timeout/retries: a separate media process owns all API latency.
        with OpenAI(api_key=os.environ["OPENAI_API_KEY"], timeout=60, max_retries=0) as client:
            response = client.audio.transcriptions.create(
                model="whisper-1", file=("caller.wav", audio.getvalue(), "audio/wav"),
                response_format="verbose_json", timestamp_granularities=["segment"], temperature=0,
            )
        result = []
        for segment in response.segments or []:
            # Provider's own silence/low-likelihood evidence, not a made-up confidence.
            if segment.no_speech_prob > 0.6 and segment.avg_logprob < -1:
                continue
            if segment.text.strip():
                result.append(Utterance(segment.text.strip(), round(segment.start * 1000), round(segment.end * 1000)))
        return result


def from_env():
    name = os.getenv("CALL_TRANSCRIPTION_PROVIDER", "openai")
    if name == "openai":
        return OpenAIRecordingTranscriber()
    raise ValueError("Unsupported call transcription provider")
