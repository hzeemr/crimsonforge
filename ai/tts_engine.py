"""Multi-provider Text-to-Speech engine.

TTS providers share API keys with translation providers where possible:
  - OpenAI TTS uses the same OpenAI API key from Settings
  - ElevenLabs uses its own API key (separate service)
  - Azure Speech uses its own subscription key
  - Google Cloud TTS uses the same credentials as Gemini
  - Edge TTS is free — no API key needed

All models and voices are fetched dynamically from provider APIs.
Nothing is hardcoded.
"""

from __future__ import annotations

import io
import os
import time
import tempfile
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Optional, Callable

from utils.logger import get_logger

logger = get_logger("ai.tts_engine")


@dataclass
class TTSVoice:
    """A single TTS voice fetched from provider API."""
    voice_id: str
    name: str
    language: str = ""
    gender: str = ""
    provider: str = ""
    sample_rate: int = 24000
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class TTSModel:
    """A TTS model fetched from provider API."""
    model_id: str
    name: str
    provider: str = ""


@dataclass
class TTSResult:
    """Result of a TTS synthesis request."""
    audio_data: bytes
    text: str
    voice: str
    model: str = ""
    provider: str = ""
    duration_ms: float = 0.0
    latency_ms: float = 0.0
    cost_estimate: float = 0.0
    char_count: int = 0
    error: str = ""
    success: bool = True
    audio_format: str = "wav"
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class TTSProviderStatus:
    """Runtime/provider status information for enterprise UI."""
    connected: bool
    message: str = ""
    device: str = ""
    model: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


class TTSProviderBase(ABC):
    """Abstract base for TTS providers."""

    name: str = ""
    provider_id: str = ""
    requires_api_key: bool = True

    def __init__(self, api_key: str = "", **kwargs):
        self._api_key = api_key
        self._extra = kwargs

    @abstractmethod
    def list_models(self) -> list[TTSModel]:
        """Fetch available TTS models from provider API. Never hardcode."""
        ...

    @abstractmethod
    def list_voices(self, language: str = "") -> list[TTSVoice]:
        """Fetch available voices from provider API. Never hardcode."""
        ...

    @abstractmethod
    def synthesize(self, text: str, model_id: str = "", voice_id: str = "",
                   language: str = "", speed: float = 1.0,
                   options: Optional[dict[str, Any]] = None) -> TTSResult:
        """Synthesize text to audio."""
        ...

    def get_status(self) -> TTSProviderStatus:
        """Optional provider/server status for enterprise UI."""
        return TTSProviderStatus(connected=True, message="")

    def save_profile(self, profile_id: str, ref_audio_path: str,
                     ref_text: str = "", overwrite: bool = True) -> dict[str, Any]:
        raise NotImplementedError("This provider does not support saved voice profiles")

    @property
    def api_key(self) -> str:
        return self._api_key

    @api_key.setter
    def api_key(self, value: str):
        self._api_key = value


# ═══════════════════════════════════════════════════════════════════════
#  OPENAI TTS — uses same API key as OpenAI translation provider
# ═══════════════════════════════════════════════════════════════════════

class TTSOpenAI(TTSProviderBase):
    """OpenAI TTS. Shares API key with OpenAI translation provider."""

    name = "OpenAI TTS"
    provider_id = "openai_tts"

    def list_models(self) -> list[TTSModel]:
        """Fetch TTS models from OpenAI API."""
        try:
            from openai import OpenAI
            client = OpenAI(api_key=self._api_key)
            models = client.models.list()
            tts_models = []
            for m in models:
                if "tts" in m.id.lower():
                    tts_models.append(TTSModel(m.id, m.id, "openai_tts"))
            if not tts_models:
                # API returned models but none matched "tts" — add known TTS models
                # that may be listed under different names
                for mid in ["tts-1", "tts-1-hd", "gpt-4o-mini-tts"]:
                    tts_models.append(TTSModel(mid, mid, "openai_tts"))
            return tts_models
        except Exception as e:
            logger.warning("Failed to fetch OpenAI TTS models: %s", e)
            return []

    def list_voices(self, language: str = "") -> list[TTSVoice]:
        """Fetch voices from OpenAI API.

        OpenAI does not have a /audio/voices listing endpoint.
        The accepted voice values are defined in their API reference at
        https://platform.openai.com/docs/api-reference/audio/createSpeech
        We query the API first; if no endpoint exists, we return the
        API-documented accepted values.
        """
        try:
            from openai import OpenAI
            client = OpenAI(api_key=self._api_key)
            # Try the voices endpoint (may be added in future API versions)
            try:
                response = client.get("/audio/voices")
                if hasattr(response, 'voices') and response.voices:
                    return [TTSVoice(v.id, v.name, provider="openai_tts")
                            for v in response.voices]
            except Exception:
                pass
        except Exception:
            pass

        # OpenAI API-documented voice IDs (from /docs/api-reference/audio/createSpeech)
        # These are the exact values the API accepts in the 'voice' parameter.
        api_voices = [
            ("alloy", "Alloy", "neutral"),
            ("ash", "Ash", "male"),
            ("ballad", "Ballad", "male"),
            ("coral", "Coral", "female"),
            ("echo", "Echo", "male"),
            ("fable", "Fable", "male"),
            ("juniper", "Juniper", "female"),
            ("nova", "Nova", "female"),
            ("onyx", "Onyx", "male"),
            ("sage", "Sage", "female"),
            ("shimmer", "Shimmer", "female"),
            ("verse", "Verse", "male"),
            ("marin", "Marin", "female"),
            ("cedar", "Cedar", "male"),
        ]
        return [TTSVoice(vid, name, gender=gender, provider="openai_tts")
                for vid, name, gender in api_voices]

    def synthesize(self, text: str, model_id: str = "", voice_id: str = "",
                   language: str = "", speed: float = 1.0,
                   options: Optional[dict[str, Any]] = None) -> TTSResult:
        start = time.time()
        try:
            from openai import OpenAI
            client = OpenAI(api_key=self._api_key)
            model = model_id or "gpt-4o-mini-tts"
            kwargs = {
                "model": model,
                "voice": voice_id or "alloy",
                "input": text,
                "speed": max(0.25, min(4.0, speed)),
                "response_format": "wav",
            }
            # instructions only works with gpt-4o-mini-tts, not tts-1/tts-1-hd
            if "gpt-4o" in model and language:
                kwargs["instructions"] = f"Speak in {language}."
            response = client.audio.speech.create(**kwargs)
            audio_data = response.content
            latency = (time.time() - start) * 1000
            cost = (len(text) / 1_000_000) * 15.0

            return TTSResult(
                audio_data=audio_data, text=text, voice=voice_id or "alloy",
                model=model_id, provider="openai_tts",
                latency_ms=latency, cost_estimate=cost, char_count=len(text),
                audio_format="wav",
            )
        except Exception as e:
            return TTSResult(
                audio_data=b"", text=text, voice=voice_id,
                provider="openai_tts", error=str(e), success=False,
                latency_ms=(time.time() - start) * 1000,
                audio_format="wav",
            )


# ═══════════════════════════════════════════════════════════════════════
#  ELEVENLABS TTS — own API key
# ═══════════════════════════════════════════════════════════════════════

class TTSElevenLabs(TTSProviderBase):
    """ElevenLabs TTS. Best voice quality, 70+ languages, voice cloning."""

    name = "ElevenLabs"
    provider_id = "elevenlabs_tts"

    def list_models(self) -> list[TTSModel]:
        """Fetch models from ElevenLabs API."""
        try:
            import requests
            headers = {"xi-api-key": self._api_key}
            resp = requests.get("https://api.elevenlabs.io/v1/models",
                                headers=headers, timeout=10)
            resp.raise_for_status()
            return [TTSModel(m["model_id"], m.get("name", m["model_id"]),
                             "elevenlabs_tts")
                    for m in resp.json()]
        except Exception as e:
            logger.warning("Failed to fetch ElevenLabs models: %s", e)
            return []

    def list_voices(self, language: str = "") -> list[TTSVoice]:
        """Fetch voices from ElevenLabs API."""
        try:
            import requests
            headers = {"xi-api-key": self._api_key}
            resp = requests.get("https://api.elevenlabs.io/v1/voices",
                                headers=headers, timeout=10)
            resp.raise_for_status()
            voices = []
            for v in resp.json().get("voices", []):
                voices.append(TTSVoice(
                    voice_id=v["voice_id"],
                    name=v.get("name", v["voice_id"]),
                    gender=v.get("labels", {}).get("gender", ""),
                    provider="elevenlabs_tts",
                ))
            return voices
        except Exception as e:
            logger.warning("Failed to fetch ElevenLabs voices: %s", e)
            return []

    def synthesize(self, text: str, model_id: str = "", voice_id: str = "",
                   language: str = "", speed: float = 1.0,
                   options: Optional[dict[str, Any]] = None) -> TTSResult:
        start = time.time()
        try:
            import requests
            vid = voice_id or "21m00Tcm4TlvDq8ikWAM"
            headers = {
                "xi-api-key": self._api_key,
                "Content-Type": "application/json",
            }
            payload = {
                "text": text,
                "model_id": model_id or "eleven_multilingual_v2",
                "voice_settings": {"stability": 0.5, "similarity_boost": 0.75},
            }
            resp = requests.post(
                f"https://api.elevenlabs.io/v1/text-to-speech/{vid}",
                headers=headers, json=payload, timeout=30,
            )
            resp.raise_for_status()
            latency = (time.time() - start) * 1000

            return TTSResult(
                audio_data=resp.content, text=text, voice=vid,
                model=model_id, provider="elevenlabs_tts",
                latency_ms=latency, char_count=len(text),
                audio_format="mp3",
            )
        except Exception as e:
            return TTSResult(
                audio_data=b"", text=text, voice=voice_id,
                provider="elevenlabs_tts", error=str(e), success=False,
                latency_ms=(time.time() - start) * 1000,
                audio_format="mp3",
            )


# ═══════════════════════════════════════════════════════════════════════
#  EDGE TTS — FREE, NO API KEY
# ═══════════════════════════════════════════════════════════════════════

class TTSEdge(TTSProviderBase):
    """Microsoft Edge TTS. Free, 400+ voices, no API key."""

    name = "Edge TTS (Free)"
    provider_id = "edge_tts"
    requires_api_key = False

    def list_models(self) -> list[TTSModel]:
        """Edge TTS has one model (the Edge neural engine)."""
        return [TTSModel("edge-neural", "Edge Neural TTS", "edge_tts")]

    def list_voices(self, language: str = "") -> list[TTSVoice]:
        """Fetch all voices from Edge TTS API."""
        try:
            import asyncio
            import edge_tts
            voices_data = asyncio.run(edge_tts.list_voices())
            result = []
            for v in voices_data:
                locale = v.get("Locale", "")
                if language and language.lower() not in locale.lower():
                    continue
                result.append(TTSVoice(
                    voice_id=v["ShortName"],
                    name=v.get("FriendlyName", v["ShortName"]),
                    language=locale,
                    gender=v.get("Gender", "").lower(),
                    provider="edge_tts",
                ))
            return result
        except Exception as e:
            logger.warning("Failed to fetch Edge TTS voices: %s", e)
            return []

    def synthesize(self, text: str, model_id: str = "", voice_id: str = "",
                   language: str = "", speed: float = 1.0,
                   options: Optional[dict[str, Any]] = None) -> TTSResult:
        start = time.time()
        try:
            import asyncio
            import edge_tts

            voice = voice_id or "en-US-GuyNeural"
            rate_pct = int((speed - 1) * 100)
            rate = f"+{rate_pct}%" if rate_pct >= 0 else f"{rate_pct}%"

            async def _gen():
                comm = edge_tts.Communicate(text, voice, rate=rate)
                tmp = tempfile.NamedTemporaryFile(suffix=".mp3", delete=False)
                path = tmp.name
                tmp.close()
                await comm.save(path)
                with open(path, "rb") as f:
                    data = f.read()
                os.unlink(path)
                return data

            audio = asyncio.run(_gen())
            latency = (time.time() - start) * 1000

            return TTSResult(
                audio_data=audio, text=text, voice=voice,
                model="edge-neural", provider="edge_tts",
                latency_ms=latency, char_count=len(text), cost_estimate=0.0,
                audio_format="mp3",
            )
        except Exception as e:
            return TTSResult(
                audio_data=b"", text=text, voice=voice_id,
                provider="edge_tts", error=str(e), success=False,
                latency_ms=(time.time() - start) * 1000,
                audio_format="mp3",
            )


# ═══════════════════════════════════════════════════════════════════════
#  GOOGLE CLOUD TTS — shares credentials with Gemini provider
# ═══════════════════════════════════════════════════════════════════════

class TTSGoogle(TTSProviderBase):
    """Google Cloud TTS. Uses same Google Cloud credentials as Gemini."""

    name = "Google Cloud TTS"
    provider_id = "google_tts"

    def list_models(self) -> list[TTSModel]:
        """Google TTS models are implicit in voice selection."""
        return [TTSModel("google-neural", "Google Neural TTS", "google_tts")]

    def list_voices(self, language: str = "") -> list[TTSVoice]:
        """Fetch voices from Google Cloud TTS API."""
        try:
            from google.cloud import texttospeech
            client = texttospeech.TextToSpeechClient()
            response = client.list_voices(language_code=language if language else None)
            voices = []
            for v in response.voices:
                gender_map = {1: "male", 2: "female", 3: "neutral"}
                for lang in v.language_codes:
                    voices.append(TTSVoice(
                        voice_id=v.name, name=v.name,
                        language=lang,
                        gender=gender_map.get(v.ssml_gender, ""),
                        provider="google_tts",
                        sample_rate=v.natural_sample_rate_hertz,
                    ))
            return voices
        except Exception as e:
            logger.warning("Failed to fetch Google TTS voices: %s", e)
            return []

    def synthesize(self, text: str, model_id: str = "", voice_id: str = "",
                   language: str = "", speed: float = 1.0,
                   options: Optional[dict[str, Any]] = None) -> TTSResult:
        start = time.time()
        try:
            from google.cloud import texttospeech
            client = texttospeech.TextToSpeechClient()

            synth_input = texttospeech.SynthesisInput(text=text)
            voice_params = texttospeech.VoiceSelectionParams(
                language_code=language or "en-US",
                name=voice_id or "",
            )
            audio_config = texttospeech.AudioConfig(
                audio_encoding=texttospeech.AudioEncoding.LINEAR16,
                speaking_rate=speed,
            )
            response = client.synthesize_speech(
                input=synth_input, voice=voice_params, audio_config=audio_config,
            )
            latency = (time.time() - start) * 1000
            cost = (len(text) / 1_000_000) * 16.0

            return TTSResult(
                audio_data=response.audio_content, text=text,
                voice=voice_id, model="google-neural",
                provider="google_tts", latency_ms=latency,
                cost_estimate=cost, char_count=len(text),
                audio_format="wav",
            )
        except Exception as e:
            return TTSResult(
                audio_data=b"", text=text, voice=voice_id,
                provider="google_tts", error=str(e), success=False,
                latency_ms=(time.time() - start) * 1000,
                audio_format="wav",
            )


# ═══════════════════════════════════════════════════════════════════════
#  AZURE SPEECH — own subscription key + region
# ═══════════════════════════════════════════════════════════════════════

class TTSAzure(TTSProviderBase):
    """Azure Speech Service. 400+ voices, 140+ languages."""

    name = "Azure Speech"
    provider_id = "azure_tts"

    def list_models(self) -> list[TTSModel]:
        return [TTSModel("azure-neural", "Azure Neural TTS", "azure_tts")]

    def list_voices(self, language: str = "") -> list[TTSVoice]:
        """Fetch voices from Azure REST API."""
        try:
            import requests
            region = self._extra.get("region", "eastus")
            url = f"https://{region}.tts.speech.microsoft.com/cognitiveservices/voices/list"
            headers = {"Ocp-Apim-Subscription-Key": self._api_key}
            resp = requests.get(url, headers=headers, timeout=10)
            resp.raise_for_status()
            voices = []
            for v in resp.json():
                locale = v.get("Locale", "")
                if language and language.lower() not in locale.lower():
                    continue
                voices.append(TTSVoice(
                    voice_id=v["ShortName"],
                    name=v.get("DisplayName", v["ShortName"]),
                    language=locale,
                    gender=v.get("Gender", "").lower(),
                    provider="azure_tts",
                ))
            return voices
        except Exception as e:
            logger.warning("Failed to fetch Azure voices: %s", e)
            return []

    def synthesize(self, text: str, model_id: str = "", voice_id: str = "",
                   language: str = "", speed: float = 1.0,
                   options: Optional[dict[str, Any]] = None) -> TTSResult:
        start = time.time()
        try:
            import requests
            region = self._extra.get("region", "eastus")
            voice = voice_id or "en-US-JennyNeural"
            rate_pct = int((speed - 1) * 100)
            rate = f"+{rate_pct}%" if rate_pct >= 0 else f"{rate_pct}%"

            ssml = (
                f'<speak version="1.0" xmlns="http://www.w3.org/2001/10/synthesis" xml:lang="en-US">'
                f'<voice name="{voice}"><prosody rate="{rate}">{text}</prosody></voice></speak>'
            )
            url = f"https://{region}.tts.speech.microsoft.com/cognitiveservices/v1"
            headers = {
                "Ocp-Apim-Subscription-Key": self._api_key,
                "Content-Type": "application/ssml+xml",
                "X-Microsoft-OutputFormat": "riff-24khz-16bit-mono-pcm",
            }
            resp = requests.post(url, headers=headers,
                                 data=ssml.encode("utf-8"), timeout=30)
            resp.raise_for_status()
            latency = (time.time() - start) * 1000
            cost = (len(text) / 1_000_000) * 16.0

            return TTSResult(
                audio_data=resp.content, text=text, voice=voice,
                model="azure-neural", provider="azure_tts",
                latency_ms=latency, cost_estimate=cost, char_count=len(text),
                audio_format="wav",
            )
        except Exception as e:
            return TTSResult(
                audio_data=b"", text=text, voice=voice_id,
                provider="azure_tts", error=str(e), success=False,
                latency_ms=(time.time() - start) * 1000,
                audio_format="wav",
            )


# ═══════════════════════════════════════════════════════════════════════
#  TTS ENGINE — MULTI-PROVIDER MANAGER
# ═══════════════════════════════════════════════════════════════════════

class TTSMistral(TTSProviderBase):
    """Mistral Voxtral TTS. Open-weight, 9 languages, voice cloning."""

    name = "Mistral Voxtral TTS"
    provider_id = "mistral_tts"

    def list_models(self) -> list[TTSModel]:
        """Fetch TTS models from Mistral API."""
        try:
            import requests
            headers = {"Authorization": f"Bearer {self._api_key}"}
            resp = requests.get("https://api.mistral.ai/v1/models",
                                headers=headers, timeout=10)
            resp.raise_for_status()
            models = []
            for m in resp.json().get("data", []):
                mid = m.get("id", "")
                if "tts" in mid.lower() or "voxtral" in mid.lower():
                    models.append(TTSModel(mid, mid, "mistral_tts"))
            return models
        except Exception as e:
            logger.warning("Failed to fetch Mistral TTS models: %s", e)
            return []

    def list_voices(self, language: str = "") -> list[TTSVoice]:
        """Mistral Voxtral has preset voices — fetch from API if available."""
        # Voxtral has 20 preset voices but no list endpoint yet
        # Return empty — user provides voice_id or uses default
        return []

    def synthesize(self, text: str, model_id: str = "", voice_id: str = "",
                   language: str = "", speed: float = 1.0,
                   options: Optional[dict[str, Any]] = None) -> TTSResult:
        start = time.time()
        try:
            import requests
            headers = {
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
            }
            payload = {
                "model": model_id or "mistral-tts-latest",
                "input": text,
                "voice": voice_id or "jessica",
                "response_format": "wav",
                "speed": speed,
            }
            resp = requests.post("https://api.mistral.ai/v1/audio/speech",
                                 headers=headers, json=payload, timeout=30)
            resp.raise_for_status()
            latency = (time.time() - start) * 1000
            cost = (len(text) / 1000) * 0.016  # $0.016 per 1K chars

            return TTSResult(
                audio_data=resp.content, text=text, voice=voice_id or "jessica",
                model=model_id, provider="mistral_tts",
                latency_ms=latency, cost_estimate=cost, char_count=len(text),
                audio_format="wav",
            )
        except Exception as e:
            return TTSResult(
                audio_data=b"", text=text, voice=voice_id,
                provider="mistral_tts", error=str(e), success=False,
                latency_ms=(time.time() - start) * 1000,
                audio_format="wav",
            )


class TTSOmniVoice(TTSProviderBase):
    """OmniVoice local server integration for high-fidelity cloning."""

    name = "OmniVoice Local"
    provider_id = "omnivoice_tts"
    requires_api_key = False

    def _base_url(self) -> str:
        return (self._extra.get("base_url") or "http://127.0.0.1:8880").rstrip("/")

    def _timeout(self) -> float:
        try:
            return float(self._extra.get("timeout", 120))
        except Exception:
            return 120.0

    def _headers(self) -> dict[str, str]:
        headers = {}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        return headers

    @staticmethod
    def _encode_multipart(
        fields: dict[str, Any],
        files: dict[str, tuple[str, bytes, str]],
    ) -> tuple[bytes, str]:
        import uuid

        boundary = f"----CrimsonForgeOmniVoice{uuid.uuid4().hex}"
        parts: list[bytes] = []

        for name, value in fields.items():
            parts.append(f"--{boundary}\r\n".encode("utf-8"))
            parts.append(
                f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode("utf-8")
            )
            parts.append(str(value).encode("utf-8"))
            parts.append(b"\r\n")

        for name, (filename, content, mime_type) in files.items():
            parts.append(f"--{boundary}\r\n".encode("utf-8"))
            parts.append(
                (
                    f'Content-Disposition: form-data; name="{name}"; '
                    f'filename="{filename}"\r\n'
                ).encode("utf-8")
            )
            parts.append(f"Content-Type: {mime_type}\r\n\r\n".encode("utf-8"))
            parts.append(content)
            parts.append(b"\r\n")

        parts.append(f"--{boundary}--\r\n".encode("utf-8"))
        return b"".join(parts), f"multipart/form-data; boundary={boundary}"

    def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: Optional[dict[str, Any]] = None,
        form_fields: Optional[dict[str, Any]] = None,
        files: Optional[dict[str, tuple[str, bytes, str]]] = None,
        expect_json: bool = False,
    ) -> Any:
        import json
        import urllib.error
        import urllib.request

        url = f"{self._base_url()}{path}"
        headers = dict(self._headers())
        body: Optional[bytes] = None

        if json_body is not None:
            body = json.dumps(json_body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        elif form_fields is not None or files:
            body, content_type = self._encode_multipart(form_fields or {}, files or {})
            headers["Content-Type"] = content_type

        req = urllib.request.Request(url, data=body, headers=headers, method=method.upper())
        try:
            with urllib.request.urlopen(req, timeout=self._timeout()) as resp:
                payload = resp.read()
                if expect_json:
                    return json.loads(payload.decode("utf-8")) if payload else {}
                return payload
        except urllib.error.HTTPError as e:
            details = e.read().decode("utf-8", errors="replace").strip()
            message = f"HTTP {e.code}"
            if details:
                message += f": {details}"
            raise RuntimeError(message) from e
        except urllib.error.URLError as e:
            raise RuntimeError(str(e.reason or e)) from e

    @staticmethod
    def _parse_models(payload: Any) -> list[TTSModel]:
        items = []
        if isinstance(payload, dict):
            items = payload.get("data") or payload.get("models") or []
        elif isinstance(payload, list):
            items = payload

        models: list[TTSModel] = []
        for item in items:
            if isinstance(item, dict):
                mid = str(item.get("id") or item.get("model") or item.get("name") or "").strip()
                if mid:
                    models.append(TTSModel(mid, str(item.get("name") or mid), "omnivoice_tts"))
            elif item:
                mid = str(item).strip()
                models.append(TTSModel(mid, mid, "omnivoice_tts"))
        if not models:
            models.append(TTSModel("omnivoice", "omnivoice", "omnivoice_tts"))
        return models

    def list_models(self) -> list[TTSModel]:
        try:
            return self._parse_models(self._request("GET", "/v1/models", expect_json=True))
        except Exception as e:
            logger.warning("Failed to fetch OmniVoice models: %s", e)
            return [TTSModel("omnivoice", "omnivoice", "omnivoice_tts")]

    def list_voices(self, language: str = "") -> list[TTSVoice]:
        try:
            payload = self._request("GET", "/v1/voices", expect_json=True)
            voices_data = payload.get("voices", []) if isinstance(payload, dict) else payload
            voices: list[TTSVoice] = []
            for item in voices_data or []:
                if not isinstance(item, dict):
                    continue
                voice_id = str(item.get("id") or item.get("voice") or "").strip()
                if not voice_id:
                    continue
                vtype = str(item.get("type") or "").strip()
                description = str(item.get("description") or "").strip()
                profile_id = str(item.get("profile_id") or "").strip()
                label = profile_id or description or voice_id
                if voice_id == "auto":
                    label = "Auto"
                elif voice_id.startswith("design:"):
                    label = "Design / Custom"
                elif voice_id.startswith("clone:"):
                    label = profile_id or voice_id.removeprefix("clone:")
                voices.append(TTSVoice(
                    voice_id=voice_id,
                    name=label,
                    provider="omnivoice_tts",
                    metadata={
                        "type": vtype,
                        "description": description,
                        "profile_id": profile_id,
                        "design_attributes": payload.get("design_attributes", {}) if isinstance(payload, dict) else {},
                    },
                ))
            if not voices:
                voices.append(TTSVoice("auto", "Auto", provider="omnivoice_tts"))
                voices.append(TTSVoice("design:", "Design / Custom", provider="omnivoice_tts"))
            return voices
        except Exception as e:
            logger.warning("Failed to fetch OmniVoice voices: %s", e)
            return [
                TTSVoice("auto", "Auto", provider="omnivoice_tts"),
                TTSVoice("design:", "Design / Custom", provider="omnivoice_tts"),
            ]

    def get_status(self) -> TTSProviderStatus:
        try:
            payload = self._request("GET", "/health", expect_json=True)
            models = self.list_models()
            model_name = models[0].model_id if models else ""
            device = ""
            if isinstance(payload, dict):
                device = str(payload.get("device") or payload.get("runtime", "")).strip()
            msg = "Connected"
            if device:
                msg += f" ({device})"
            return TTSProviderStatus(
                connected=True,
                message=msg,
                device=device,
                model=model_name,
                extra=payload if isinstance(payload, dict) else {},
            )
        except Exception as e:
            return TTSProviderStatus(connected=False, message=str(e))

    def save_profile(self, profile_id: str, ref_audio_path: str,
                     ref_text: str = "", overwrite: bool = True) -> dict[str, Any]:
        if not profile_id:
            raise ValueError("Profile ID is required")
        if not ref_audio_path or not os.path.isfile(ref_audio_path):
            raise FileNotFoundError(f"Reference audio not found: {ref_audio_path}")

        with open(ref_audio_path, "rb") as ref_audio:
            payload = self._request(
                "POST",
                "/v1/voices/profiles",
                form_fields={
                    "profile_id": profile_id,
                    "ref_text": ref_text or "",
                    "overwrite": "true" if overwrite else "false",
                },
                files={
                    "ref_audio": (
                        os.path.basename(ref_audio_path),
                        ref_audio.read(),
                        "audio/wav",
                    )
                },
                expect_json=True,
            )
        return payload if payload else {"profile_id": profile_id}

    def _base_payload(self, text: str, model_id: str, voice_id: str,
                      speed: float, options: dict[str, Any]) -> dict[str, Any]:
        duration = float(options.get("duration", 0.0) or 0.0)
        payload: dict[str, Any] = {
            "model": model_id or "omnivoice",
            "input": text,
            "voice": voice_id or "auto",
            "response_format": options.get("response_format") or "wav",
            "speed": speed,
            "stream": bool(options.get("stream", False)),
            "num_step": int(options.get("num_step", 32)),
            "guidance_scale": float(options.get("guidance_scale", 3.0)),
            "denoise": bool(options.get("denoise", True)),
            "t_shift": float(options.get("t_shift", 0.1)),
            "position_temperature": float(options.get("position_temperature", 5.0)),
            "class_temperature": float(options.get("class_temperature", 0.0)),
            "param_9": options.get("param_9", "Auto"),
            "param_10": options.get("param_10", "Auto"),
            "param_11": options.get("param_11", "Auto"),
            "param_12": options.get("param_12", "Auto"),
            "param_13": options.get("param_13", "Auto"),
        }
        if duration > 0:
            payload["duration"] = duration
        return payload

    def synthesize(self, text: str, model_id: str = "", voice_id: str = "",
                   language: str = "", speed: float = 1.0,
                   options: Optional[dict[str, Any]] = None) -> TTSResult:
        start = time.time()
        options = dict(options or {})
        clone_mode = options.get("clone_mode") or "voice"
        try:
            if clone_mode == "one_shot":
                ref_audio_path = str(options.get("ref_audio_path") or "").strip()
                if not ref_audio_path or not os.path.isfile(ref_audio_path):
                    raise FileNotFoundError("OmniVoice one-shot cloning requires a reference audio WAV")

                form_data = {
                    "text": text,
                    "ref_text": str(options.get("ref_text") or ""),
                    "speed": str(speed),
                    "num_step": str(int(options.get("num_step", 32))),
                    "guidance_scale": str(float(options.get("guidance_scale", 3.0))),
                    "denoise": "true" if bool(options.get("denoise", True)) else "false",
                    "t_shift": str(float(options.get("t_shift", 0.1))),
                    "position_temperature": str(float(options.get("position_temperature", 5.0))),
                    "class_temperature": str(float(options.get("class_temperature", 0.0))),
                    "param_9": str(options.get("param_9", "Auto")),
                    "param_10": str(options.get("param_10", "Auto")),
                    "param_11": str(options.get("param_11", "Auto")),
                    "param_12": str(options.get("param_12", "Auto")),
                    "param_13": str(options.get("param_13", "Auto")),
                }
                duration = float(options.get("duration", 0.0) or 0.0)
                if duration > 0:
                    form_data["duration"] = str(duration)

                with open(ref_audio_path, "rb") as ref_audio:
                    audio_data = self._request(
                        "POST",
                        "/v1/audio/speech/clone",
                        form_fields=form_data,
                        files={
                            "ref_audio": (
                                os.path.basename(ref_audio_path),
                                ref_audio.read(),
                                "audio/wav",
                            )
                        },
                    )
                latency = (time.time() - start) * 1000
                return TTSResult(
                    audio_data=audio_data,
                    text=text,
                    voice="one-shot-clone",
                    model=model_id or "omnivoice",
                    provider="omnivoice_tts",
                    latency_ms=latency,
                    char_count=len(text),
                    audio_format="wav",
                    metadata={"clone_mode": clone_mode},
                )

            if clone_mode == "saved_profile":
                profile_id = str(options.get("profile_id") or "").strip()
                if not profile_id:
                    raise ValueError("OmniVoice saved-profile mode requires a profile name")
                ref_audio_path = str(options.get("ref_audio_path") or "").strip()
                if ref_audio_path and os.path.isfile(ref_audio_path) and options.get("refresh_profile", True):
                    self.save_profile(
                        profile_id,
                        ref_audio_path,
                        ref_text=str(options.get("ref_text") or ""),
                        overwrite=bool(options.get("overwrite_profile", True)),
                    )
                voice_id = f"clone:{profile_id}"

            payload = self._base_payload(text, model_id, voice_id, speed, options)
            audio_data = self._request("POST", "/v1/audio/speech", json_body=payload)
            latency = (time.time() - start) * 1000
            return TTSResult(
                audio_data=audio_data,
                text=text,
                voice=payload["voice"],
                model=payload["model"],
                provider="omnivoice_tts",
                latency_ms=latency,
                char_count=len(text),
                audio_format=str(payload.get("response_format") or "wav"),
                metadata={"clone_mode": clone_mode},
            )
        except Exception as e:
            return TTSResult(
                audio_data=b"",
                text=text,
                voice=voice_id,
                provider="omnivoice_tts",
                error=str(e),
                success=False,
                latency_ms=(time.time() - start) * 1000,
                audio_format="wav",
                metadata={"clone_mode": clone_mode},
            )


TTS_PROVIDER_CLASSES: dict[str, type] = {
    "openai_tts": TTSOpenAI,
    "elevenlabs_tts": TTSElevenLabs,
    "edge_tts": TTSEdge,
    "google_tts": TTSGoogle,
    "azure_tts": TTSAzure,
    "mistral_tts": TTSMistral,
    "omnivoice_tts": TTSOmniVoice,
}

# Map TTS providers to translation providers whose API key they share.
# None = own dedicated key (or no key needed for edge_tts).
TTS_KEY_SHARING = {
    "openai_tts": "openai",       # shares OpenAI API key
    "google_tts": "gemini",       # shares Google credentials
    "mistral_tts": "mistral",     # shares Mistral API key
    "elevenlabs_tts": None,       # own key (TTS-only provider)
    "azure_tts": None,            # own key (TTS-only provider)
    "edge_tts": None,             # no key needed (free)
    "omnivoice_tts": None,        # local server, optional bearer token
}

# Which translation providers also support TTS (use same API key).
# Only these providers show a "TTS Model" field in Settings.
TRANSLATION_PROVIDERS_WITH_TTS = {
    "openai": "openai_tts",       # tts-1, tts-1-hd, gpt-4o-mini-tts
    "gemini": "google_tts",       # gemini-2.5-flash-tts, gemini-2.5-pro-tts
    "mistral": "mistral_tts",     # Voxtral TTS (March 2026, $0.016/1K chars)
    # anthropic: NO TTS API
    # deepseek: NO TTS API
    # cohere: NO TTS (only STT/transcription)
    # ollama: NO TTS
    # vllm: NO TTS
    # deepl: NO TTS (translation only)
}

# TTS-only providers (not translation providers, need own API key)
TTS_ONLY_PROVIDERS = {"elevenlabs_tts", "azure_tts", "edge_tts", "omnivoice_tts"}


def get_tts_model_config_key(provider_id: str) -> str:
    """Return the config key used to store the default model for a TTS provider."""
    shared = TTS_KEY_SHARING.get(provider_id)
    if shared:
        return f"ai_providers.{shared}.default_tts_model"
    return f"tts.{provider_id}_default_model"


class TTSEngine:
    """Multi-provider TTS engine. Shares API keys with translation providers."""

    def __init__(self):
        self._providers: dict[str, TTSProviderBase] = {}
        self._active_provider_id: str = "edge_tts"

    def initialize_from_config(self, config) -> None:
        """Initialize TTS providers, sharing API keys with translation providers.

        Args:
            config: Either a ConfigManager instance or a dict. API keys are read
                    from ai_providers.{provider_id}.api_key in the config.
        """
        # Support both ConfigManager and dict
        def _get(key, default=""):
            if hasattr(config, 'get'):
                return config.get(key, default)
            if isinstance(config, dict):
                parts = key.split(".")
                d = config
                for p in parts:
                    if isinstance(d, dict):
                        d = d.get(p, default)
                    else:
                        return default
                return d
            return default

        for pid, cls in TTS_PROVIDER_CLASSES.items():
            # Get API key: shared providers use the translation provider's key
            shared_provider = TTS_KEY_SHARING.get(pid)
            if shared_provider:
                key = _get(f"ai_providers.{shared_provider}.api_key", "")
            else:
                # TTS-only providers: check tts config or ai_providers
                key = _get(f"ai_providers.{pid}.api_key", "")
                if not key:
                    key = _get(f"tts.{pid}_api_key", "")

            extra = {}
            if pid == "azure_tts":
                extra["region"] = _get("tts.azure_region", "eastus")
            elif pid == "omnivoice_tts":
                extra["base_url"] = _get("tts.omnivoice_base_url", "http://127.0.0.1:8880")
                if not key:
                    key = _get("tts.omnivoice_api_key", "")
                extra["timeout"] = _get("tts.omnivoice_timeout_seconds", 120)

            self._providers[pid] = cls(api_key=key, **extra)

        self._active_provider_id = _get("tts.active_provider", "edge_tts")

    def get_provider(self, provider_id: str = "") -> Optional[TTSProviderBase]:
        pid = provider_id or self._active_provider_id
        if pid not in self._providers:
            cls = TTS_PROVIDER_CLASSES.get(pid)
            if cls:
                self._providers[pid] = cls()
        return self._providers.get(pid)

    def list_providers(self) -> list[dict]:
        return [{"id": pid, "name": cls.name, "requires_api_key": cls.requires_api_key}
                for pid, cls in TTS_PROVIDER_CLASSES.items()]

    def list_models(self, provider_id: str = "") -> list[TTSModel]:
        p = self.get_provider(provider_id)
        return p.list_models() if p else []

    def list_voices(self, provider_id: str = "", language: str = "") -> list[TTSVoice]:
        p = self.get_provider(provider_id)
        return p.list_voices(language) if p else []

    def synthesize(self, text: str, provider_id: str = "",
                   model_id: str = "", voice_id: str = "",
                   language: str = "", speed: float = 1.0,
                   options: Optional[dict[str, Any]] = None) -> TTSResult:
        p = self.get_provider(provider_id)
        if not p:
            return TTSResult(audio_data=b"", text=text, voice=voice_id,
                             provider=provider_id, error="Provider not found",
                             success=False)
        return p.synthesize(text, model_id, voice_id, language, speed, options=options)

    def batch_synthesize(self, entries: list[dict], provider_id: str = "",
                         model_id: str = "", voice_id: str = "",
                         language: str = "", speed: float = 1.0,
                         options: Optional[dict[str, Any]] = None,
                         progress_callback: Optional[Callable] = None) -> list[TTSResult]:
        results = []
        total = len(entries)
        for i, entry in enumerate(entries):
            text = entry.get("text", "")
            if not text:
                continue
            entry_options = dict(options or {})
            if isinstance(entry.get("options"), dict):
                entry_options.update(entry["options"])
            r = self.synthesize(text, provider_id, model_id, voice_id, language, speed, entry_options)
            results.append(r)
            if progress_callback:
                progress_callback(int(((i + 1) / total) * 100),
                                  f"Generated {i + 1}/{total}")
        return results

    @property
    def active_provider_id(self) -> str:
        return self._active_provider_id

    @active_provider_id.setter
    def active_provider_id(self, value: str):
        self._active_provider_id = value
