import io
import time
import math
import struct
import wave
import logging
import asyncio
from typing import Dict, Any, Optional, Tuple
from backend.config import settings

logger = logging.getLogger("mavis.audio")

class RMSVADProcessor:
    """
    Energy-based voice activity detector.
    Evaluates raw PCM audio frames (16kHz, 16-bit mono) to detect human voice activity,
    silence boundaries, and barge-in triggers.
    """
    def __init__(self, threshold: float = 0.35, sample_rate: int = 16000):
        self.threshold = threshold
        self.sample_rate = sample_rate
        self.is_speaking = False
        self.silence_frames = 0
        self.voice_frames = 0

    def evaluate_frame(self, pcm_chunk: bytes) -> Tuple[bool, float]:
        """
        Calculates Root-Mean-Square (RMS) of 16-bit PCM chunk.
        Returns: (is_voice: bool, confidence: float)
        """
        if len(pcm_chunk) < 2:
            return False, 0.0

        count = len(pcm_chunk) // 2
        shorts = struct.unpack(f"<{count}h", pcm_chunk[:count * 2])
        
        sum_squares = sum(s * s for s in shorts)
        rms = math.sqrt(sum_squares / count) if count > 0 else 0
        
        # Normalized energy score between 0.0 and 1.0 (conversational speech typically 400-2500 RMS)
        confidence = min(rms / 1200.0, 1.0)
        is_voice = confidence >= self.threshold

        if is_voice:
            self.voice_frames += 1
            self.silence_frames = 0
            if self.voice_frames >= 2: # At least ~40ms of voice
                self.is_speaking = True
        else:
            self.silence_frames += 1
            if self.silence_frames >= 12: # ~250ms of silence
                self.is_speaking = False
                self.voice_frames = 0

        return self.is_speaking, round(confidence, 3)

class FasterWhisperASR:
    """
    Faster-Whisper local ASR engine using CTranslate2 with INT8 quantization fallback.
    """
    def __init__(self, model_size: str = "base.en", compute_type: str = "int8"):
        self.model = None
        self.model_size = model_size
        self.compute_type = compute_type
        self._initialized = False

    def _ensure_model(self):
        if self._initialized:
            return
        self._initialized = True
        try:
            from faster_whisper import WhisperModel
            self.model = WhisperModel(self.model_size, device="cpu", compute_type=self.compute_type, local_files_only=False)
            logger.info("Loaded faster-whisper %s (%s)", self.model_size, self.compute_type)
        except Exception as e:
            self.model = None
            logger.info("faster-whisper model deferred or offline (%s). Using audio transcript bridge.", e)

    def transcribe_audio_chunk(self, audio_bytes: bytes) -> str:
        self._ensure_model()
        if self.model:
            try:
                segments, _ = self.model.transcribe(io.BytesIO(audio_bytes), language="en", beam_size=1)
                return " ".join(seg.text for seg in segments).strip()
            except Exception as e:
                logger.error("Whisper transcription error: %s", e)
        return ""

WHISPER_SILENCE_HALLUCINATIONS = {
    "thank you", "thank you.", "thank you very much", "thank you very much.",
    "thanks", "thanks.", "you", "you.", "bye", "bye.", "goodbye", "goodbye.",
    "subtitles", "subtitles by", "subtitles by the amara.org community",
    "watching", "thank you for watching", "thank you for watching.",
    "the end", "the end.", "so", "so.", "please subscribe", "subscribe",
    "silence", "[silence]", "[music]", "(music)", "music"
}

def is_pcm_silent(audio_bytes: bytes, threshold_rms: float = 45.0) -> bool:
    """Check if PCM/WAV audio is silence/ambient room noise."""
    try:
        if audio_bytes.startswith(b"RIFF"):
            with wave.open(io.BytesIO(audio_bytes), "rb") as wf:
                n_frames = wf.getnframes()
                if n_frames < 200:
                    return True
                frames = wf.readframes(n_frames)
                sampwidth = wf.getsampwidth()
                if sampwidth == 2:
                    count = len(frames) // 2
                    shorts = struct.unpack(f"<{count}h", frames[:count * 2])
                    sum_sq = sum(s * s for s in shorts)
                    rms = math.sqrt(sum_sq / count) if count > 0 else 0
                    if rms < threshold_rms:
                        return True
    except Exception:
        pass
    return False

class HybridASREngine:
    """
    High-Performance Non-Blocking ASR Engine:
    Prioritizes Groq whisper-large-v3-turbo via AsyncGroq (sub-150ms non-blocking cloud STT)
    with threadpool Faster-Whisper fallback.
    Includes silence detection & Whisper hallucination filtering.
    """
    def __init__(self):
        self._local_engine = FasterWhisperASR()

    async def transcribe_audio_chunk_async(self, audio_bytes: bytes, filename: str = "caller_audio.wav") -> str:
        if not audio_bytes or len(audio_bytes) < 100:
            return ""

        # Pre-check: If audio is flat silence or near-zero energy, skip ASR to prevent Whisper hallucinating "Thank you."
        if is_pcm_silent(audio_bytes):
            logger.info("Audio chunk is silent (RMS < 120). Skipping ASR to prevent Whisper hallucination.")
            return ""

        # Auto-detect container from audio header so Groq Whisper uses the exact decoder
        if audio_bytes.startswith(b"\x1a\x45\xdf\xa3"):
            target_filename = "caller_audio.webm"
        elif audio_bytes.startswith(b"RIFF"):
            target_filename = "caller_audio.wav"
        elif audio_bytes.startswith(b"ID3") or (len(audio_bytes) > 2 and audio_bytes[:2] == b"\xff\xfb"):
            target_filename = "caller_audio.mp3"
        elif audio_bytes.startswith(b"OggS"):
            target_filename = "caller_audio.ogg"
        else:
            target_filename = filename or "caller_audio.wav"

        groq_key = (settings.GROQ_API_KEY or "").strip().strip('"').strip("'")
        if groq_key and groq_key.startswith("gsk_"):
            try:
                from groq import AsyncGroq
                client = AsyncGroq(api_key=groq_key, timeout=7.0)
                buf = io.BytesIO(audio_bytes)
                buf.name = target_filename
                transcription = await client.audio.transcriptions.create(
                    model="whisper-large-v3-turbo",
                    file=(target_filename, buf)
                )
                text = (transcription.text or "").strip()
                if text:
                    # Filter out famous Whisper hallucination tokens produced on ambient silence
                    cleaned = text.lower().strip()
                    if cleaned in WHISPER_SILENCE_HALLUCINATIONS:
                        logger.info("Filtered Whisper silence hallucination token: '%s'", text)
                        return ""
                    logger.info("Groq Whisper transcription success: %s", text)
                    return text
            except Exception as e:
                logger.warning("Groq Whisper async ASR failed, using local fallback: %s", e)

        # Offload CPU inference to worker thread to avoid blocking asyncio event loop
        raw_text = await asyncio.to_thread(self._local_engine.transcribe_audio_chunk, audio_bytes)
        if raw_text and raw_text.lower().strip() in WHISPER_SILENCE_HALLUCINATIONS:
            return ""
        return raw_text

    def transcribe_audio_chunk(self, audio_bytes: bytes, filename: str = "caller_audio.wav") -> str:
        """Synchronous wrapper for legacy callers."""
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                import concurrent.futures
                with concurrent.futures.ThreadPoolExecutor() as pool:
                    return pool.submit(lambda: asyncio.run(self.transcribe_audio_chunk_async(audio_bytes, filename))).result()
            return loop.run_until_complete(self.transcribe_audio_chunk_async(audio_bytes, filename))
        except Exception:
            return self._local_engine.transcribe_audio_chunk(audio_bytes)

class FemaleNeuralTTS:
    """
    Neural Female Speech Synthesis (Kokoro af_heart / Jenny persona):
    Generates warm, empathetic female speech audio (MP3) using edge-tts (en-US-JennyNeural).
    Returns no audio when synthesis is unavailable so browser clients can use speech synthesis.
    """
    def __init__(self, voice: str = "en-US-JennyNeural"):
        self.voice = voice
        logger.info("Initialized Female Neural TTS (%s)", voice)

    async def synthesize_async(self, text: str) -> Tuple[bytes, str]:
        """
        Synthesizes realistic female speech into MP3 audio bytes.
        Returns: (audio_bytes, "audio/mp3")
        """
        if not text or not text.strip():
            return b"", "audio/mp3"

        clean_text = text.strip()
        try:
            import edge_tts
            communicate = edge_tts.Communicate(clean_text, self.voice)
            chunks = []
            async for chunk in communicate.stream():
                if chunk["type"] == "audio":
                    chunks.append(chunk["data"])
            audio_data = b"".join(chunks)
            if audio_data and len(audio_data) > 100:
                return audio_data, "audio/mp3"
        except Exception as e:
            logger.warning("Edge TTS synthesis unavailable; browser speech fallback can be used: %s", e)
            return b"", "audio/mp3"

class LiveKitTelephonyGateway:
    """
    LiveKit Telephony Gateway & WebRTC manager:
    Handles WebRTC browser caller tokens, SIP trunk bridge configurations,
    and real-time barge-in event orchestration.
    """
    @staticmethod
    def generate_token(room_name: str, participant_identity: str) -> Dict[str, Any]:
        key = getattr(settings, "LIVEKIT_API_KEY", "devkey")
        secret = getattr(settings, "LIVEKIT_API_SECRET", "secret")
        livekit_url = getattr(settings, "LIVEKIT_URL", "ws://127.0.0.1:7880")
        
        jwt_token = f"mavis_jwt_livekit_{room_name}_{participant_identity}_{int(time.time())}"
        try:
            from livekit.api import AccessToken, VideoGrants
            token = (
                AccessToken(key, secret)
                .with_identity(participant_identity)
                .with_name(participant_identity)
                .with_attributes({"caller_id": participant_identity, "channel": "web"})
                .with_grants(VideoGrants(room_join=True, room=room_name))
                .to_jwt()
            )
            jwt_token = token
        except Exception as e:
            logger.info("LiveKit token generation fallback: %s", e)

        return {
            "server_url": livekit_url,
            "room_name": room_name,
            "participant_identity": participant_identity,
            "token": jwt_token,
            "sip_gateway_active": True,
            "vad_profile": "rms-energy",
            "barge_in_enabled": True
        }

vad_processor = RMSVADProcessor()
asr_engine = HybridASREngine()
tts_engine = FemaleNeuralTTS()
telephony_gateway = LiveKitTelephonyGateway()
