from __future__ import annotations

import base64
import re
from dataclasses import dataclass
from pathlib import Path

from fu_gm.config import ImageGenerationConfig
from fu_gm.llm_client import Transport, UrlLibTransport


@dataclass
class ImageGenerationResult:
    model: str
    prompt: str
    output_path: str = ""
    remote_url: str = ""
    revised_prompt: str = ""
    raw_keys: list[str] | None = None


class ImageGenerationClient:
    """Small OpenAI-compatible image generation client.

    The service is intentionally narrow: FU-GM only needs a campaign/map visual,
    while travel distance and route rules stay in the Python map graph.
    """

    def __init__(self, config: ImageGenerationConfig, transport: Transport | None = None) -> None:
        self.config = config
        self.transport = transport or UrlLibTransport()

    def create_image(
        self,
        prompt: str,
        *,
        output_dir: str | Path | None = None,
        filename_prefix: str = "world_map",
    ) -> ImageGenerationResult:
        if not self.config.usable():
            raise ValueError("图片生成配置未启用或缺少 api_base_url/api_key/model。")
        payload = {
            "model": self.config.model,
            "prompt": prompt,
            "size": self.config.size,
            "n": 1,
        }
        if self.config.response_format:
            payload["response_format"] = self.config.response_format
        data = self.transport.post_json(
            url=self.config.image_generations_url(),
            headers={
                "Authorization": f"Bearer {self.config.api_key}",
                "Content-Type": "application/json",
            },
            payload=payload,
            timeout=self.config.timeout_seconds,
        )
        item = self._first_image_item(data)
        if not isinstance(item, dict):
            raise ValueError("图片服务响应中没有可用的 data[0]。")

        result = ImageGenerationResult(
            model=self.config.model,
            prompt=prompt,
            revised_prompt=str(item.get("revised_prompt") or data.get("revised_prompt") or ""),
            raw_keys=sorted(str(key) for key in data.keys()),
        )
        if item.get("b64_json"):
            directory = Path(output_dir or self.config.output_dir)
            directory.mkdir(parents=True, exist_ok=True)
            output_path = directory / f"{self._safe_filename(filename_prefix)}.png"
            output_path.write_bytes(base64.b64decode(str(item["b64_json"])))
            result.output_path = str(output_path)
            return result
        if item.get("url"):
            result.remote_url = str(item["url"])
            return result
        raise ValueError("图片服务响应中既没有 b64_json，也没有 url。")

    def _first_image_item(self, data: dict) -> dict | None:
        images = data.get("data")
        if isinstance(images, list) and images:
            return images[0] if isinstance(images[0], dict) else None
        if isinstance(data.get("output"), list) and data["output"]:
            first = data["output"][0]
            if isinstance(first, dict):
                if isinstance(first.get("content"), list) and first["content"]:
                    content = first["content"][0]
                    if isinstance(content, dict):
                        return content
                return first
        return data if any(key in data for key in ("b64_json", "url")) else None

    def _safe_filename(self, value: str) -> str:
        cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", value.strip())
        cleaned = cleaned.strip("._")
        return cleaned or "generated_image"
