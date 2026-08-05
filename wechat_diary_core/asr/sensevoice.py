"""Lazy, reusable SenseVoice wrapper with optional dependencies."""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path
from typing import Any, Callable


EMOTION_TAGS = {
    "HAPPY",
    "SAD",
    "ANGRY",
    "NEUTRAL",
    "FEARFUL",
    "DISGUSTED",
    "SURPRISED",
    "EMO_UNKNOWN",
}
LANGUAGE_TAGS = {"zh", "en", "yue", "ja", "ko", "nospeech"}
ITN_TAGS = {"withitn", "woitn"}


class ASRUnavailableError(RuntimeError):
    """The configured optional ASR engine cannot be used in this process."""


def sensevoice_dependencies_available(
    find_spec: Callable[[str], Any] = importlib.util.find_spec,
) -> tuple[bool, list[str]]:
    missing: list[str] = []
    for module in ("funasr", "torch", "modelscope"):
        try:
            available = find_spec(module) is not None
        except (ImportError, ModuleNotFoundError, ValueError):
            available = False
        if not available:
            missing.append(module)
    return not missing, missing


class SenseVoiceTranscriber:
    """Load one model lazily and reuse it for every voice in the export run."""

    def __init__(
        self,
        *,
        model: str = "iic/SenseVoiceSmall",
        language: str = "zh",
        device: str = "cpu",
        model_factory: Callable[..., Any] | None = None,
        postprocess: Callable[[str], str] | None = None,
    ) -> None:
        self.model_name = model
        self.language = language
        self.device = device
        self._model_factory = model_factory
        self._postprocess = postprocess
        self._model: Any = None
        self._load_error: BaseException | None = None

    def transcribe(self, audio_path: str | Path) -> dict[str, Any]:
        path = Path(audio_path)
        if not path.is_file():
            raise FileNotFoundError(f"语音文件不存在：{path}")
        model = self._get_model()
        try:
            response = model.generate(
                input=str(path),
                cache={},
                language=self.language,
                use_itn=True,
            )
            raw_text = str(response[0]["text"])
        except Exception as exc:
            raise RuntimeError(f"SenseVoice 转写失败：{exc}") from exc

        tags = re.findall(r"<\|([A-Za-z_]+)\|>", raw_text)
        emotion = [tag for tag in tags if tag in EMOTION_TAGS]
        events = [
            tag
            for tag in tags
            if tag not in EMOTION_TAGS and tag not in LANGUAGE_TAGS and tag not in ITN_TAGS
        ]
        text = self._get_postprocess()(raw_text).strip()
        if not text:
            raise RuntimeError("SenseVoice 未返回可用文字")
        return {"text": text, "emotion": emotion, "events": events}

    def _get_model(self) -> Any:
        if self._model is not None:
            return self._model
        if self._load_error is not None:
            raise ASRUnavailableError(f"SenseVoice 可选依赖未就绪：{self._load_error}") from self._load_error
        try:
            factory = self._model_factory
            if factory is None:
                from funasr import AutoModel

                factory = AutoModel
            self._model = factory(
                model=self.model_name,
                disable_update=True,
                device=self.device,
            )
        except (ImportError, ModuleNotFoundError) as exc:
            self._load_error = exc
            raise ASRUnavailableError(
                "未安装 funasr/torch/modelscope；请按需安装 requirements-asr.txt"
            ) from exc
        except Exception as exc:
            self._load_error = exc
            raise ASRUnavailableError(f"SenseVoice 模型加载失败：{exc}") from exc
        return self._model

    def _get_postprocess(self) -> Callable[[str], str]:
        if self._postprocess is not None:
            return self._postprocess
        try:
            from funasr.utils.postprocess_utils import rich_transcription_postprocess
        except (ImportError, ModuleNotFoundError) as exc:
            raise ASRUnavailableError("funasr 后处理模块不可用") from exc
        self._postprocess = rich_transcription_postprocess
        return self._postprocess


__all__ = [
    "ASRUnavailableError",
    "EMOTION_TAGS",
    "SenseVoiceTranscriber",
    "sensevoice_dependencies_available",
]
