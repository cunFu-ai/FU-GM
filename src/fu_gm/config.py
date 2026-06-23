from __future__ import annotations

import os
from dataclasses import dataclass


def _load_dotenv(path: str = ".env") -> None:
    try:
        if not os.path.exists(path):
            return
        with open(path, "r", encoding="utf-8") as handle:
            for raw_line in handle:
                line = raw_line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                os.environ.setdefault(key.strip(), value.strip())
    except OSError:
        # macOS LaunchAgent 可能无法读取位于“文稿”等受保护目录下的 .env。
        # 运行脚本会预先把必要变量注入环境，因此这里失败时应降级而不是中断服务。
        return


@dataclass
class LLMConfig:
    api_base_url: str
    api_key: str
    action_model: str
    expressor_model: str
    timeout_seconds: float = 60.0
    reasoning_effort: str = ""
    thinking_enabled: bool = False
    reactive_recovery_enabled: bool = True
    reactive_recovery_max_retries: int = 1
    reactive_recovery_target_chars: int = 48000
    allow_heuristic_fallback: bool = True

    @classmethod
    def from_env(cls) -> "LLMConfig":
        _load_dotenv(os.environ.get("FU_GM_DOTENV_PATH", ".env"))
        base_url = os.environ.get("FU_GM_API_BASE_URL", "https://api.apiyi.com").rstrip("/")
        return cls(
            api_base_url=base_url,
            api_key=os.environ.get("FU_GM_API_KEY", ""),
            action_model=os.environ.get("FU_GM_ACTION_MODEL", "gpt-5.4-nano"),
            expressor_model=os.environ.get("FU_GM_EXPRESSOR_MODEL", "gpt-5.4-nano"),
            timeout_seconds=float(os.environ.get("FU_GM_TIMEOUT_SECONDS", "60")),
            reasoning_effort=os.environ.get("FU_GM_REASONING_EFFORT", ""),
            thinking_enabled=os.environ.get("FU_GM_THINKING_ENABLED", "").lower() in {"1", "true", "yes", "enabled"},
            reactive_recovery_enabled=os.environ.get("FU_GM_REACTIVE_RECOVERY_ENABLED", "1").lower()
            not in {"0", "false", "no", "disabled"},
            reactive_recovery_max_retries=int(os.environ.get("FU_GM_REACTIVE_RECOVERY_MAX_RETRIES", "1")),
            reactive_recovery_target_chars=int(os.environ.get("FU_GM_REACTIVE_RECOVERY_TARGET_CHARS", "48000")),
            allow_heuristic_fallback=os.environ.get("FU_GM_ALLOW_HEURISTIC_FALLBACK", "1").lower()
            not in {"0", "false", "no", "disabled"},
        )

    def chat_completions_url(self) -> str:
        if self.api_base_url.endswith("/chat/completions"):
            return self.api_base_url
        if "api.deepseek.com" in self.api_base_url:
            return f"{self.api_base_url}/chat/completions"
        if self.api_base_url.endswith("/v1"):
            return f"{self.api_base_url}/chat/completions"
        return f"{self.api_base_url}/v1/chat/completions"


@dataclass
class ImageGenerationConfig:
    api_base_url: str
    api_key: str
    model: str
    enabled: bool = False
    size: str = "1024x1024"
    timeout_seconds: float = 180.0
    output_dir: str = "data/generated_images"
    response_format: str = "b64_json"

    @classmethod
    def from_env(cls) -> "ImageGenerationConfig":
        _load_dotenv(os.environ.get("FU_GM_DOTENV_PATH", ".env"))
        base_url = os.environ.get("FU_GM_IMAGE_API_BASE_URL", "").rstrip("/")
        return cls(
            api_base_url=base_url,
            api_key=os.environ.get("FU_GM_IMAGE_API_KEY", ""),
            model=os.environ.get("FU_GM_IMAGE_MODEL", "gpt-image-2"),
            enabled=os.environ.get("FU_GM_IMAGE_ENABLED", "").lower() in {"1", "true", "yes", "enabled", "on"},
            size=os.environ.get("FU_GM_IMAGE_SIZE", "1024x1024"),
            timeout_seconds=float(os.environ.get("FU_GM_IMAGE_TIMEOUT_SECONDS", "180")),
            output_dir=os.environ.get("FU_GM_IMAGE_OUTPUT_DIR", "data/generated_images"),
            response_format=os.environ.get("FU_GM_IMAGE_RESPONSE_FORMAT", "b64_json"),
        )

    def image_generations_url(self) -> str:
        if not self.api_base_url:
            return ""
        if self.api_base_url.endswith("/images/generations"):
            return self.api_base_url
        if self.api_base_url.endswith("/v1"):
            return f"{self.api_base_url}/images/generations"
        return f"{self.api_base_url}/v1/images/generations"

    def usable(self) -> bool:
        return bool(self.enabled and self.api_base_url and self.api_key and self.model)
