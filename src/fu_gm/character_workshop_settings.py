from __future__ import annotations

import json
import os
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib import error, request
from urllib.parse import urlparse

from fu_gm.config import (
    DEFAULT_LLM_API_BASE_URL,
    DEFAULT_LLM_MODEL,
    ComfyUIConfig,
    LLMConfig,
)
from fu_gm.llm_client import ChatMessage, OpenAICompatibleClient


SETTINGS_VERSION = 1
WORKFLOW_FILENAMES = {
    "anima": "anima-api.json",
    "krea2": "krea2-api.json",
    "krea_lora": "krea-lora-api.json",
}


def bundled_workflow_root() -> Path:
    """Locate editable packaged workflows before PyInstaller's temp copy."""

    if getattr(sys, "frozen", False):
        beside_executable = Path(sys.executable).resolve().parent / "workflows"
        if beside_executable.is_dir():
            return beside_executable
        bundle_root = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
        return bundle_root / "config" / "comfyui_workflows"
    return Path(__file__).resolve().parents[2] / "config" / "comfyui_workflows"


@dataclass(frozen=True)
class WorkshopConnectionSnapshot:
    comfyui_port: int
    llm_api_base_url: str
    llm_model: str


class CharacterWorkshopSettings:
    """Owns standalone connection settings without persisting API credentials."""

    def __init__(
        self,
        data_root: str | Path,
        *,
        workflow_root: str | Path | None = None,
        use_environment_defaults: bool = False,
    ) -> None:
        self.data_root = Path(data_root).expanduser().resolve()
        self.data_root.mkdir(parents=True, exist_ok=True)
        self.settings_path = self.data_root / "settings.json"
        self.workflow_root = Path(
            workflow_root if workflow_root is not None else bundled_workflow_root()
        ).expanduser().resolve()
        self.output_root = (self.data_root / "portraits").resolve()
        self._lock = threading.RLock()
        self._llm_api_key = ""

        default_port = 8188
        default_base_url = DEFAULT_LLM_API_BASE_URL
        default_model = DEFAULT_LLM_MODEL
        if use_environment_defaults:
            comfy = ComfyUIConfig.from_env()
            parsed = urlparse(comfy.base_url)
            if parsed.hostname in {"127.0.0.1", "localhost", "::1"} and parsed.port:
                default_port = parsed.port
            llm = LLMConfig.from_env()
            default_base_url = llm.api_base_url or default_base_url
            default_model = (
                str(os.environ.get("FU_GM_PORTRAIT_PROMPT_MODEL") or llm.action_model)
                .strip()
                or default_model
            )
            self._llm_api_key = str(llm.api_key or "").strip()

        self._snapshot = WorkshopConnectionSnapshot(
            comfyui_port=default_port,
            llm_api_base_url=default_base_url,
            llm_model=default_model,
        )
        self._load()

    def public_payload(self) -> dict[str, Any]:
        with self._lock:
            snapshot = self._snapshot
            key_configured = bool(self._llm_api_key)
        return {
            "ok": True,
            "comfyui": {
                "host": "127.0.0.1",
                "port": snapshot.comfyui_port,
                "base_url": f"http://127.0.0.1:{snapshot.comfyui_port}",
                "workflows": self.workflow_status(),
            },
            "llm": {
                "api_base_url": snapshot.llm_api_base_url,
                "model": snapshot.llm_model,
                "api_key_configured": key_configured,
                "api_key_storage": "memory_only",
            },
        }

    def update(self, payload: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise ValueError("设置必须是 JSON 对象。")
        comfy_payload = payload.get("comfyui")
        llm_payload = payload.get("llm")
        if comfy_payload is not None and not isinstance(comfy_payload, dict):
            raise ValueError("ComfyUI 设置格式不正确。")
        if llm_payload is not None and not isinstance(llm_payload, dict):
            raise ValueError("LLM 设置格式不正确。")
        comfy_payload = comfy_payload or {}
        llm_payload = llm_payload or {}

        with self._lock:
            current = self._snapshot
            port = self._validate_port(
                comfy_payload.get("port", current.comfyui_port)
            )
            api_base_url = self._validate_api_base_url(
                llm_payload.get("api_base_url", current.llm_api_base_url)
            )
            model = self._validate_model(llm_payload.get("model", current.llm_model))

            if bool(llm_payload.get("clear_api_key")):
                self._llm_api_key = ""
            elif "api_key" in llm_payload:
                submitted_key = str(llm_payload.get("api_key") or "").strip()
                if submitted_key:
                    if len(submitted_key) > 4096:
                        raise ValueError("API Key 过长。")
                    self._llm_api_key = submitted_key

            self._snapshot = WorkshopConnectionSnapshot(
                comfyui_port=port,
                llm_api_base_url=api_base_url,
                llm_model=model,
            )
            self._save()
        return self.public_payload()

    def comfyui_config(self) -> ComfyUIConfig:
        with self._lock:
            port = self._snapshot.comfyui_port
        return ComfyUIConfig(
            enabled=True,
            base_url=f"http://127.0.0.1:{port}",
            output_dir=str(self.output_root),
            anima_workflow=str(self.workflow_root / WORKFLOW_FILENAMES["anima"]),
            krea2_workflow=str(self.workflow_root / WORKFLOW_FILENAMES["krea2"]),
            krea_lora_workflow=str(
                self.workflow_root / WORKFLOW_FILENAMES["krea_lora"]
            ),
            allow_remote=False,
        )

    def llm_config(self) -> LLMConfig:
        with self._lock:
            snapshot = self._snapshot
            api_key = self._llm_api_key
        return LLMConfig(
            api_base_url=snapshot.llm_api_base_url,
            api_key=api_key,
            action_model=snapshot.llm_model,
            expressor_model=snapshot.llm_model,
            timeout_seconds=120.0,
            endpoint_attempt_timeout_seconds=45.0,
            prompt_cache_enabled=False,
            reactive_recovery_enabled=False,
            reactive_recovery_max_retries=0,
        )

    def workflow_status(self) -> dict[str, bool]:
        return {
            profile: (self.workflow_root / filename).is_file()
            for profile, filename in WORKFLOW_FILENAMES.items()
        }

    def test_comfyui(self) -> dict[str, Any]:
        config = self.comfyui_config()
        url = f"{config.base_url}/system_stats"
        http_request = request.Request(
            url,
            headers={"Accept": "application/json"},
            method="GET",
        )
        try:
            with request.urlopen(http_request, timeout=4.0) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except (OSError, error.URLError, json.JSONDecodeError) as exc:
            raise ValueError(
                f"无法连接本机 ComfyUI（端口 {self._snapshot.comfyui_port}）。"
            ) from exc
        system = payload.get("system") if isinstance(payload, dict) else {}
        devices = payload.get("devices") if isinstance(payload, dict) else []
        first_device = devices[0] if isinstance(devices, list) and devices else {}
        return {
            "ok": True,
            "message": "已连接本机 ComfyUI。",
            "version": str(system.get("comfyui_version") or "未知"),
            "device": str(first_device.get("name") or "未报告设备"),
            "workflows": self.workflow_status(),
        }

    def test_llm(self) -> dict[str, Any]:
        config = self.llm_config()
        if not config.api_key:
            raise ValueError("请先填写 API Key。")
        result = OpenAICompatibleClient(config).create_chat_completion(
            model=config.action_model,
            messages=[
                ChatMessage(
                    role="system",
                    content="这是连接测试。只回复 OK，不要输出其他内容。",
                ),
                ChatMessage(role="user", content="请确认连接。"),
            ],
            temperature=0.0,
            max_tokens=12,
            operation="character_workshop_connection_test",
            max_recovery_retries=0,
        )
        if not str(result or "").strip():
            raise ValueError("LLM 服务返回了空内容。")
        return {
            "ok": True,
            "message": "LLM 连接成功。",
            "model": config.action_model,
        }

    def _load(self) -> None:
        if not self.settings_path.is_file():
            return
        try:
            raw = json.loads(self.settings_path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                return
            comfy = raw.get("comfyui") if isinstance(raw.get("comfyui"), dict) else {}
            llm = raw.get("llm") if isinstance(raw.get("llm"), dict) else {}
            self._snapshot = WorkshopConnectionSnapshot(
                comfyui_port=self._validate_port(
                    comfy.get("port", self._snapshot.comfyui_port)
                ),
                llm_api_base_url=self._validate_api_base_url(
                    llm.get("api_base_url", self._snapshot.llm_api_base_url)
                ),
                llm_model=self._validate_model(
                    llm.get("model", self._snapshot.llm_model)
                ),
            )
        except (OSError, ValueError, json.JSONDecodeError):
            return

    def _save(self) -> None:
        payload = {
            "version": SETTINGS_VERSION,
            "comfyui": {"port": self._snapshot.comfyui_port},
            "llm": {
                "api_base_url": self._snapshot.llm_api_base_url,
                "model": self._snapshot.llm_model,
            },
        }
        temporary = self.settings_path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary.replace(self.settings_path)

    @staticmethod
    def _validate_port(value: object) -> int:
        try:
            port = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError("ComfyUI 端口必须是数字。") from exc
        if port < 1 or port > 65535:
            raise ValueError("ComfyUI 端口必须在 1 到 65535 之间。")
        return port

    @staticmethod
    def _validate_api_base_url(value: object) -> str:
        clean = str(value or "").strip().rstrip("/")
        if not clean or len(clean) > 1000:
            raise ValueError("LLM 接口地址不能为空。")
        parsed = urlparse(clean)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("LLM 接口地址必须是有效的 http 或 https 地址。")
        return clean

    @staticmethod
    def _validate_model(value: object) -> str:
        clean = " ".join(str(value or "").split()).strip()
        if not clean:
            raise ValueError("LLM 模型不能为空。")
        if len(clean) > 300:
            raise ValueError("LLM 模型名称过长。")
        return clean
