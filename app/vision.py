"""Loopback-only adapter for a user-run OpenAI-compatible vision/OCR service."""

import base64
import json
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen


MAX_VISUAL_TEXT_LENGTH = 12_000


def validate_loopback_base_url(base_url: str) -> str:
    normalized = str(base_url or "").strip().rstrip("/")
    parsed = urlparse(normalized)
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("视觉服务地址只能是本机 HTTP 地址（localhost 或 127.0.0.1）。")
    return normalized


def extract_visual_text(
    *,
    base_url: str,
    model: str,
    image_bytes: bytes,
    mime_type: str,
    timeout_seconds: int = 45,
) -> str:
    """Return only the extractor's visible-content result; callers persist it locally."""
    endpoint = f"{validate_loopback_base_url(base_url)}/chat/completions"
    data_url = f"data:{mime_type};base64,{base64.b64encode(image_bytes).decode('ascii')}"
    payload = {
        "model": str(model),
        "temperature": 0,
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": "提取图片中可检索的可见文字和事实。内容不清楚时留空；不要猜测、不要补全。",
                    },
                    {"type": "image_url", "image_url": {"url": data_url}},
                ],
            }
        ],
    }
    request = Request(
        endpoint,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=timeout_seconds) as response:  # nosec B310: endpoint is loopback-validated above
            response_data: Any = json.loads(response.read().decode("utf-8"))
    except (HTTPError, URLError, TimeoutError, ValueError, OSError) as exc:
        raise RuntimeError("本机视觉服务未能完成内容提取") from exc

    try:
        content = response_data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError("本机视觉服务返回格式无效") from exc
    if isinstance(content, list):
        content = "\n".join(
            str(item.get("text") or "") for item in content if isinstance(item, dict)
        )
    cleaned = "".join(char for char in str(content or "") if char >= " " and char != "\x7f").strip()
    return cleaned[:MAX_VISUAL_TEXT_LENGTH]
