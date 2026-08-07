"""Resident SenseVoice JSON-Lines worker for an isolated Python environment."""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Callable
from typing import Any, TextIO


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


def serve(
    input_stream: TextIO,
    output_stream: TextIO,
    *,
    model_name: str,
    language: str,
    device: str,
    model_factory: Callable[..., Any] | None = None,
    postprocess: Callable[[str], str] | None = None,
) -> int:
    """Load the model once, then serve one response for every input line."""

    try:
        if model_factory is None or postprocess is None:
            from funasr import AutoModel
            from funasr.utils.postprocess_utils import rich_transcription_postprocess

            model_factory = model_factory or AutoModel
            postprocess = postprocess or rich_transcription_postprocess
        model = model_factory(
            model=model_name,
            device=device,
            disable_update=True,
        )
    except Exception as exc:
        _emit(output_stream, {"error": f"SenseVoice worker 初始化失败：{exc}"})
        return 2

    for line in input_stream:
        if not line.strip():
            continue
        try:
            request = json.loads(line)
            if not isinstance(request, dict) or not str(request.get("audio") or ""):
                raise ValueError("请求必须包含非空 audio")
            response = model.generate(
                input=str(request["audio"]),
                language=language,
                use_itn=True,
                cache={},
            )
            raw_text = str(response[0]["text"])
            tags = re.findall(r"<\|([A-Za-z_]+)\|>", raw_text)
            text = postprocess(raw_text).strip()
            if not text:
                raise RuntimeError("SenseVoice 未返回可用文字")
            _emit(
                output_stream,
                {
                    "text": text,
                    "emotion": [tag for tag in tags if tag in EMOTION_TAGS],
                    "events": [
                        tag
                        for tag in tags
                        if tag not in EMOTION_TAGS
                        and tag not in LANGUAGE_TAGS
                        and tag not in ITN_TAGS
                    ],
                },
            )
        except Exception as exc:
            # One bad file is a request failure; the resident model stays alive.
            _emit(output_stream, {"error": f"SenseVoice 转写失败：{exc}"})
    return 0


def _emit(stream: TextIO, payload: dict[str, Any]) -> None:
    stream.write(json.dumps(payload, ensure_ascii=False) + "\n")
    stream.flush()


def main(argv: list[str] | None = None) -> int:
    protocol_output = sys.stdout
    # Third-party imports and inference may print to stdout. Keep fd-backed
    # protocol output separate and redirect all incidental prints to stderr.
    sys.stdout = sys.stderr
    parser = argparse.ArgumentParser(description="SenseVoice JSON-Lines worker")
    parser.add_argument("--model", default="iic/SenseVoiceSmall")
    parser.add_argument("--language", default="zh")
    parser.add_argument("--device", default="cpu")
    args = parser.parse_args(argv)
    return serve(
        sys.stdin,
        protocol_output,
        model_name=args.model,
        language=args.language,
        device=args.device,
    )


if __name__ == "__main__":
    raise SystemExit(main())
