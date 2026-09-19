"""SPAGASUS JARVIS - computer vision & screen intelligence.

The pipeline captures real screenshots and feeds them to Gemini Multimodal Vision
(or an OpenAI-compatible vision endpoint / custom VISION_API_URL) for real-time
screen understanding, error analysis, and visual Q&A.
"""
from __future__ import annotations

import base64
import json
import urllib.error
import urllib.request
from pathlib import Path

from .config import LLM_API_KEY, LLM_BASE_URL, LLM_MODEL, VISION_API_URL
from .tools import screenshot


def capture() -> str:
    """Take a real screenshot and return its file path."""
    return screenshot("spagasus")


def analyze_screen(question: str = "Describe what is on this screen in 2 concise sentences.") -> dict:
    """Take screenshot -> vision model -> structured result {path, description, ok, b64}."""
    path = capture()
    if not path or not Path(path).exists():
        return {
            "path": "",
            "description": "Failed to capture the screen.",
            "ok": False,
            "b64": "",
        }

    try:
        with open(path, "rb") as fh:
            img_bytes = fh.read()
            b64 = base64.b64encode(img_bytes).decode("utf-8")
    except Exception as exc:  # noqa: BLE001
        return {
            "path": path,
            "description": f"Could not read screenshot: {exc}",
            "ok": False,
            "b64": "",
        }

    # 1. Custom VISION_API_URL if configured
    if VISION_API_URL:
        try:
            req = urllib.request.Request(
                VISION_API_URL,
                data=json.dumps({"image": b64, "question": question}).encode("utf-8"),
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=25) as resp:
                data = json.loads(resp.read().decode("utf-8", "replace"))
            desc = data.get("description") or data.get("text") or "No description returned."
            return {"path": path, "description": desc, "ok": True, "b64": b64}
        except Exception as exc:  # noqa: BLE001
            return {
                "path": path,
                "description": f"Custom vision endpoint failed: {exc}",
                "ok": False,
                "b64": b64,
            }

    # 2. Native Gemini / OpenAI Multimodal Chat Completions
    if LLM_API_KEY:
        try:
            prompt_text = (
                f"You are SPAGASUS JARVIS analyzing the user's computer screen. "
                f"Question: {question}. "
                f"Give a direct, concise 1-3 sentence answer explaining key details, active applications, text or errors shown."
            )
            payload = {
                "model": LLM_MODEL or "gemini-2.5-flash",
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": prompt_text},
                            {
                                "type": "image_url",
                                "image_url": {"url": f"data:image/png;base64,{b64}"},
                            },
                        ],
                    }
                ],
                "max_tokens": 400,
            }
            req = urllib.request.Request(
                f"{LLM_BASE_URL.rstrip('/')}/chat/completions",
                data=json.dumps(payload).encode("utf-8"),
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {LLM_API_KEY}",
                },
            )
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read().decode("utf-8", "replace"))
            choices = data.get("choices") or []
            if choices and choices[0].get("message", {}).get("content"):
                desc = choices[0]["message"]["content"].strip()
                return {"path": path, "description": desc, "ok": True, "b64": b64}
            return {
                "path": path,
                "description": "Screen captured, but AI vision returned no response.",
                "ok": False,
                "b64": b64,
            }
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")[:250]
            return {
                "path": path,
                "description": f"AI Vision error (HTTP {exc.code}): {body}",
                "ok": False,
                "b64": b64,
            }
        except Exception as exc:  # noqa: BLE001
            return {
                "path": path,
                "description": f"AI Vision analysis failed: {exc}",
                "ok": False,
                "b64": b64,
            }

    return {
        "path": path,
        "description": (
            f"Screen captured and saved to {path}. "
            "To enable live AI screen analysis, add your API key in the Keys tab."
        ),
        "ok": True,
        "b64": b64,
    }


def describe_screen(question: str = "Describe what is on this screen in 2 concise sentences.") -> str:
    """Screenshot -> vision model -> string description for voice or chat reply."""
    res = analyze_screen(question=question)
    desc = res.get("description") or ""
    if res.get("ok"):
        return f"Screen analysis: {desc}"
    return desc or f"Screen captured to {res.get('path', 'database')}."
