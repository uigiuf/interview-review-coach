"""语音识别层 —— OpenAI Whisper 兼容协议。

兼容 OpenAI 官方、Groq、AssemblyAI（部分接口）、自托管 whisper.cpp server、
faster-whisper-server 等。

环境变量：
  ASR_BASE_URL  接口地址，默认 https://api.openai.com/v1
  ASR_API_KEY   密钥
  ASR_MODEL     模型名，默认 whisper-1（Groq 请填 whisper-large-v3）
  ASR_LANGUAGE  语言提示，默认 zh（中文识别更准）

单文件大小限制：OpenAI 与 Groq 官方接口均为 25MB；自托管服务通常无此限。
"""

from __future__ import annotations

import json
import os
from typing import Optional

import httpx


def _cfg() -> tuple[str, str, str, str]:
    return (
        os.environ.get("ASR_BASE_URL", "https://api.openai.com/v1").rstrip("/"),
        os.environ.get("ASR_API_KEY", ""),
        os.environ.get("ASR_MODEL", "whisper-1"),
        os.environ.get("ASR_LANGUAGE", "zh"),
    )


def asr_available() -> bool:
    return bool(os.environ.get("ASR_API_KEY"))


def transcribe(audio_bytes: bytes, filename: str, content_type: Optional[str] = None) -> dict:
    """把音频二进制送到 ASR 端点转写。

    返回：{"text": str, "segments": list, "language": str}
    """
    base, key, model, language = _cfg()
    if not key:
        raise RuntimeError("ASR 未配置：请设置 ASR_API_KEY")

    files = {"file": (filename or "audio.m4a", audio_bytes, content_type or "audio/mpeg")}
    data = {
        "model": model,
        "response_format": "verbose_json",  # 拿 segments 和时间戳
    }
    if language:
        data["language"] = language

    resp = httpx.post(
        f"{base}/audio/transcriptions",
        headers={"Authorization": f"Bearer {key}"},
        files=files,
        data=data,
        timeout=600,  # 长录音转写可能几分钟
    )
    if resp.status_code >= 400:
        # 尝试解析错误细节
        try:
            err = resp.json()
            msg = err.get("error", {}).get("message") or json.dumps(err, ensure_ascii=False)
        except Exception:
            msg = resp.text[:400]
        raise RuntimeError(f"ASR 调用失败({resp.status_code})：{msg}")

    result = resp.json()
    return {
        "text": (result.get("text") or "").strip(),
        "segments": result.get("segments") or [],
        "language": result.get("language"),
        "duration": result.get("duration"),
    }


def segments_to_readable(segments: list) -> str:
    """把 verbose_json 的 segments 平铺成带时间戳的可读文本（无说话人分离时的兜底展示）。"""
    if not segments:
        return ""
    lines = []
    for s in segments:
        start = s.get("start") or 0
        m, sec = int(start // 60), int(start % 60)
        text = (s.get("text") or "").strip()
        if text:
            lines.append(f"[{m:02d}:{sec:02d}] {text}")
    return "\n".join(lines)
