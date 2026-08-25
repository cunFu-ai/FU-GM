from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass


DEFAULT_LLM_API_BASE_URL = "https://api.deepseek.com"
DEFAULT_LLM_MODEL = "deepseek-v4-flash-vision-exp"


def parse_api_base_urls(raw: str) -> tuple[str, ...]:
    """Parse a comma-separated endpoint list while preserving order."""

    urls: list[str] = []
    for value in str(raw or "").split(","):
        url = value.strip().rstrip("/")
        if url and url not in urls:
            urls.append(url)
    return tuple(urls)


def uses_high_latency_model(model: str) -> bool:
    """Return whether observed provider latency needs a wider first attempt."""

    normalized = str(model or "").strip().lower()
    return normalized in {"gpt-5.6-luna", "gpt-5.6-terra"} or normalized.endswith(
        ("/gpt-5.6-luna", "/gpt-5.6-terra")
    )


def model_api_key_env_names(model: str) -> tuple[str, ...]:
    """Return stable environment names for one model's dedicated credential."""

    normalized = str(model or "").strip().lower()
    model_token = re.sub(r"[^a-z0-9]+", "_", normalized).strip("_").upper()
    names: list[str] = []
    if model_token:
        names.append(f"FU_GM_MODEL_{model_token}_API_KEY")
    if normalized.startswith("deepseek-"):
        names.append("FU_GM_DEEPSEEK_API_KEY")
        # 兼容从 V4 Flash 升级前已经部署的模型专用密钥名。实验模型与
        # 正式模型使用同一个 DeepSeek 官方账户，不应因模型 ID 变化而
        # 错误回落到可能属于其他供应商的全局密钥。
        names.append("FU_GM_MODEL_DEEPSEEK_V4_FLASH_API_KEY")
    for family in ("luna", "terra"):
        if re.search(rf"(?:^|[^a-z0-9]){family}(?:$|[^a-z0-9])", normalized):
            names.append(f"FU_GM_{family.upper()}_API_KEY")
    return tuple(dict.fromkeys(names))


def resolve_model_api_key(
    model: str,
    fallback: str = "",
    *,
    values: Mapping[str, str] | None = None,
) -> str:
    """Select a model-specific credential without exposing it to telemetry."""

    source = os.environ if values is None else values
    for name in model_api_key_env_names(model):
        value = str(source.get(name, "") or "").strip()
        if value:
            return value
    return str(fallback or "").strip()


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
    backup_api_base_urls: tuple[str, ...] = ()
    http_user_agent: str = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 FU-GM/1.0"
    timeout_seconds: float = 60.0
    endpoint_attempt_timeout_seconds: float = 20.0
    reasoning_effort: str = ""
    thinking_enabled: bool = False
    response_format_enabled: bool = True
    prompt_cache_enabled: bool = True
    prompt_cache_mode: str = "auto"
    prompt_cache_key_prefix: str = "fugm"
    prompt_cache_ttl: str = "30m"
    reactive_recovery_enabled: bool = True
    reactive_recovery_max_retries: int = 2
    reactive_recovery_target_chars: int = 48000
    # This flag is only for non-authoritative auxiliaries such as log
    # summarization and offline player simulation. The core GM and NPC
    # decision paths always fail closed and never consult it.
    allow_heuristic_fallback: bool = False

    @classmethod
    def for_test_client(cls, model: str = "test-only") -> "LLMConfig":
        """构造不会读取 dotenv、也不具备外部端点凭据的测试配置。"""

        model_name = str(model or "test-only").strip() or "test-only"
        return cls(
            api_base_url="",
            api_key="",
            action_model=model_name,
            expressor_model=model_name,
            backup_api_base_urls=(),
            timeout_seconds=300.0,
            endpoint_attempt_timeout_seconds=300.0,
            response_format_enabled=True,
            prompt_cache_enabled=False,
            reactive_recovery_enabled=False,
            reactive_recovery_max_retries=0,
            allow_heuristic_fallback=False,
        )

    @classmethod
    def from_env(cls) -> "LLMConfig":
        _load_dotenv(os.environ.get("FU_GM_DOTENV_PATH", ".env"))
        base_url = os.environ.get(
            "FU_GM_API_BASE_URL",
            DEFAULT_LLM_API_BASE_URL,
        ).rstrip("/")
        action_model = os.environ.get("FU_GM_ACTION_MODEL", DEFAULT_LLM_MODEL)
        expressor_model = os.environ.get(
            "FU_GM_EXPRESSOR_MODEL",
            DEFAULT_LLM_MODEL,
        )
        high_latency = uses_high_latency_model(action_model) or uses_high_latency_model(
            expressor_model
        )
        backup_urls = os.environ.get(
            "FU_GM_BACKUP_API_BASE_URLS",
            os.environ.get("FU_GM_BACKUP_API_BASE_URL", ""),
        )
        default_api_key = os.environ.get("FU_GM_API_KEY", "")
        return cls(
            api_base_url=base_url,
            api_key=resolve_model_api_key(action_model, default_api_key),
            action_model=action_model,
            expressor_model=expressor_model,
            backup_api_base_urls=parse_api_base_urls(backup_urls),
            http_user_agent=os.environ.get(
                "FU_GM_HTTP_USER_AGENT",
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 FU-GM/1.0",
            ),
            timeout_seconds=float(
                os.environ.get("FU_GM_TIMEOUT_SECONDS", "180" if high_latency else "120")
            ),
            endpoint_attempt_timeout_seconds=float(
                os.environ.get(
                    "FU_GM_ENDPOINT_ATTEMPT_TIMEOUT_SECONDS",
                    "45" if high_latency else "20",
                )
            ),
            reasoning_effort=os.environ.get("FU_GM_REASONING_EFFORT", ""),
            thinking_enabled=os.environ.get("FU_GM_THINKING_ENABLED", "").lower() in {"1", "true", "yes", "enabled"},
            response_format_enabled=os.environ.get(
                "FU_GM_RESPONSE_FORMAT_ENABLED",
                "1",
            ).lower()
            not in {"0", "false", "no", "disabled", "off"},
            prompt_cache_enabled=os.environ.get(
                "FU_GM_PROMPT_CACHE_ENABLED",
                "1",
            ).lower()
            not in {"0", "false", "no", "disabled", "off"},
            prompt_cache_mode=os.environ.get(
                "FU_GM_PROMPT_CACHE_MODE",
                "auto",
            ).strip().lower(),
            prompt_cache_key_prefix=os.environ.get(
                "FU_GM_PROMPT_CACHE_KEY_PREFIX",
                "fugm",
            ).strip(),
            prompt_cache_ttl=os.environ.get(
                "FU_GM_PROMPT_CACHE_TTL",
                "30m",
            ).strip(),
            reactive_recovery_enabled=os.environ.get("FU_GM_REACTIVE_RECOVERY_ENABLED", "1").lower()
            not in {"0", "false", "no", "disabled"},
            reactive_recovery_max_retries=int(os.environ.get("FU_GM_REACTIVE_RECOVERY_MAX_RETRIES", "2")),
            reactive_recovery_target_chars=int(os.environ.get("FU_GM_REACTIVE_RECOVERY_TARGET_CHARS", "48000")),
            allow_heuristic_fallback=os.environ.get("FU_GM_ALLOW_HEURISTIC_FALLBACK", "0").lower()
            not in {"0", "false", "no", "disabled"},
        )

    def chat_completions_url(self) -> str:
        return self._chat_completions_url_for_base(self.api_base_url)

    def chat_completions_urls(self) -> tuple[str, ...]:
        bases = (self.api_base_url, *self.backup_api_base_urls)
        urls: list[str] = []
        for base_url in bases:
            url = self._chat_completions_url_for_base(base_url)
            if url and url not in urls:
                urls.append(url)
        return tuple(urls)

    @staticmethod
    def _chat_completions_url_for_base(base_url: str) -> str:
        base_url = str(base_url or "").rstrip("/")
        if base_url.endswith("/chat/completions"):
            return base_url
        if "api.deepseek.com" in base_url:
            return f"{base_url}/chat/completions"
        if base_url.endswith("/v1"):
            return f"{base_url}/chat/completions"
        return f"{base_url}/v1/chat/completions"

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
