from __future__ import annotations

import mimetypes
import os
from copy import deepcopy
from pathlib import Path
from typing import Any
from urllib.parse import quote

from fu_gm.components.character_card_manager import (
    CharacterCardError,
    CharacterCardManager,
)
from fu_gm.components.character_creation_manager import CharacterCreationManager
from fu_gm.components.character_manager import CharacterManager
from fu_gm.components.world_state import WorldState
from fu_gm.config import ComfyUIConfig, LLMConfig
from fu_gm.character_workshop_roster import CharacterWorkshopRoster
from fu_gm.character_workshop_settings import CharacterWorkshopSettings
from fu_gm.llm_client import OpenAICompatibleClient
from fu_gm.portrait_generation import (
    CharacterPortraitPromptService,
    ComfyUIClient,
    PortraitJobManager,
    PortraitPrompt,
)


class CharacterBuilderAPI:
    """HTTP-facing facade kept separate from the large legacy server module."""

    STATIC_FILES = {
        "/characters": "index.html",
        "/characters/index.html": "index.html",
        "/characters/styles.css": "styles.css",
        "/characters/app.js": "app.js",
        "/characters/portrait-placeholder.webp": "portrait-placeholder.webp",
    }

    def __init__(
        self,
        host: Any,
        *,
        data_root: str | Path = "data/character-workshop",
        settings: CharacterWorkshopSettings | None = None,
    ) -> None:
        self.host = host
        self.static_root = Path(__file__).resolve().parent / "web" / "character_builder"
        creation = CharacterCreationManager(CharacterManager(), WorldState())
        self.cards = CharacterCardManager(creation)
        self.roster = CharacterWorkshopRoster(data_root, self.cards)
        self.settings = settings
        self.prompt_service = CharacterPortraitPromptService()
        self.portrait_jobs = PortraitJobManager()
        self._prompt_llm_client: OpenAICompatibleClient | None = None
        self._prompt_llm_model = ""

    def static_file(self, route: str) -> tuple[bytes, str] | None:
        relative = self.STATIC_FILES.get(route)
        if relative is None:
            return None
        path = self.static_root / relative
        if not path.is_file():
            return None
        explicit_types = {
            ".html": "text/html; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".js": "text/javascript; charset=utf-8",
            ".webp": "image/webp",
        }
        content_type = explicit_types.get(
            path.suffix.lower(),
            mimetypes.guess_type(path.name)[0] or "application/octet-stream",
        )
        return path.read_bytes(), content_type

    def catalog(self) -> dict[str, Any]:
        result = self.cards.catalog()
        portrait_enabled = self._portrait_feature_enabled()
        comfy_config = self._comfyui_config()
        result["capabilities"] = {
            "distribution_mode": str(
                os.environ.get("FU_GM_DISTRIBUTION_MODE") or "development"
            ).strip(),
            "portrait_prompt": portrait_enabled,
            "portrait_generation": portrait_enabled,
            "connection_settings": self.settings is not None,
        }
        result["portrait_profiles"] = [
            {
                "id": profile_id,
                "label": label,
                "generation_ready": bool(
                    portrait_enabled and comfy_config.usable(profile_id)
                ),
            }
            for profile_id, label in (
                ("anima", "Anima"),
                ("krea2", "Krea 2"),
                ("krea_lora", "Krea 2 + LoRA"),
            )
        ]
        result["storage"] = "standalone_roster"
        return result

    def list_characters(self) -> dict[str, Any]:
        return self.roster.list_characters()

    def preview_build(self, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        try:
            result = self.cards.preview_build(payload)
        except (CharacterCardError, TypeError, ValueError) as exc:
            return 422, {"ok": False, "valid": False, "errors": [str(exc)]}
        return 200, result

    def build_card(self, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        try:
            card = self.cards.card_from_build(payload)
        except (CharacterCardError, TypeError, ValueError) as exc:
            return 422, {"ok": False, "valid": False, "errors": [str(exc)]}
        return 200, {"ok": True, "valid": True, "card": card}

    def text_card(self, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        card = payload.get("card")
        if not isinstance(card, dict):
            return 400, {"ok": False, "error": "缺少角色卡 JSON 对象。"}
        try:
            content = self.cards.export_character_text(card)
        except (CharacterCardError, TypeError, ValueError) as exc:
            return 422, {"ok": False, "valid": False, "errors": [str(exc)]}
        hero_name = str(card.get("build", {}).get("hero_name") or "未命名角色").strip()
        return 200, {"ok": True, "text": content, "hero_name": hero_name}

    def validate_card(self, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        card = payload.get("card")
        if not isinstance(card, dict):
            return 400, {"ok": False, "valid": False, "errors": ["缺少角色卡 JSON 对象。"]}
        result = self.cards.validate_card(card)
        return (200 if result.get("valid") else 422), result

    def import_preview(self, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        card = payload.get("card")
        if not isinstance(card, dict):
            return 400, {"ok": False, "valid": False, "errors": ["缺少角色卡 JSON 对象。"]}
        result = self.roster.preview_import(card)
        if not result.get("valid"):
            return 422, result
        return 200, result

    def import_card(self, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        card = payload.get("card")
        if not isinstance(card, dict):
            return 400, {"ok": False, "error": "缺少角色卡 JSON 对象。"}
        conflict = str(payload.get("conflict") or "reject")
        try:
            result = self.roster.import_card(card, conflict=conflict)
        except CharacterCardError as exc:
            return 409, {"ok": False, "error": str(exc)}
        return 200, result

    def export_card(self, hero_name: str) -> tuple[int, dict[str, Any]]:
        clean_name = str(hero_name or "").strip()
        try:
            card = self.roster.export_card(clean_name)
        except CharacterCardError as exc:
            return 404, {"ok": False, "error": str(exc)}
        return 200, {"ok": True, "storage": "standalone_roster", "card": card}

    def prompt_portrait(self, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        if not self._portrait_feature_enabled():
            return 403, {
                "ok": False,
                "code": "portrait_feature_disabled",
                "error": "当前发行版暂未开放自动立绘功能。",
            }
        model_profile = str(payload.get("model_profile") or "anima")
        require_llm = bool(payload.get("require_llm", True))
        try:
            client, model = self._portrait_prompt_llm()
            prompt = self.prompt_service.create_prompt(
                payload,
                model_profile=model_profile,
                allow_creative_fill=bool(payload.get("allow_creative_fill")),
                require_llm=require_llm,
                llm_client=client,
                llm_model=model,
            )
        except (TypeError, ValueError) as exc:
            return 422, {"ok": False, "error": str(exc)}
        return 200, {"ok": True, "prompt": self._prompt_payload(prompt)}

    def generate_portrait(self, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        if not self._portrait_feature_enabled():
            return 403, {
                "ok": False,
                "code": "portrait_feature_disabled",
                "error": "当前发行版暂未开放自动立绘功能。",
            }
        prompt_data = payload.get("prompt")
        require_llm = bool(payload.get("require_llm", True))
        try:
            if isinstance(prompt_data, dict):
                prompt_source = str(prompt_data.get("source") or "manual").strip().lower()
                if require_llm and prompt_source != "llm":
                    raise ValueError("请先让 LLM 根据当前角色资料整理立绘提示词。")
                prompt = PortraitPrompt(
                    model_profile=self.prompt_service.normalize_profile(
                        str(prompt_data.get("model_profile") or payload.get("model_profile") or "anima")
                    ),
                    positive_prompt=self._limited_text(
                        prompt_data.get("positive_prompt"), "positive_prompt", 8000
                    ),
                    negative_prompt=self._limited_text(
                        prompt_data.get("negative_prompt"), "negative_prompt", 4000
                    ),
                    style_notes=self._limited_text(
                        prompt_data.get("style_notes", ""), "style_notes", 2000, allow_empty=True
                    ),
                    source=prompt_source,
                    brief=deepcopy(prompt_data.get("brief") or {}),
                )
            else:
                client, model = self._portrait_prompt_llm()
                prompt = self.prompt_service.create_prompt(
                    payload,
                    model_profile=str(payload.get("model_profile") or "anima"),
                    allow_creative_fill=bool(payload.get("allow_creative_fill")),
                    require_llm=require_llm,
                    llm_client=client,
                    llm_model=model,
                )
            seed = payload.get("seed")
            seed = int(seed) if seed not in (None, "") else None
        except (TypeError, ValueError) as exc:
            return 422, {"ok": False, "error": str(exc)}
        config = self._comfyui_config()
        if not config.usable(prompt.model_profile):
            return 503, {
                "ok": False,
                "error": "ComfyUI 未启用，或该模型的 API-format 工作流尚未配置。",
                "model_profile": prompt.model_profile,
            }
        filename_prefix = str(payload.get("card_id") or payload.get("hero_name") or "fu_character")
        job = self.portrait_jobs.submit(
            ComfyUIClient(config).generate,
            prompt,
            seed=seed,
            filename_prefix=filename_prefix,
        )
        return 202, {"ok": True, "job": job, "prompt": self._prompt_payload(prompt)}

    def portrait_job(self, job_id: str) -> tuple[int, dict[str, Any]]:
        job = self.portrait_jobs.get(job_id)
        if job is None:
            return 404, {"ok": False, "error": "找不到立绘任务。"}
        if job["status"] == "completed":
            filename = Path(str(job.get("result", {}).get("output_path") or "")).name
            job["result"]["asset_url"] = (
                "/v1/portraits/file?name=" + quote(filename, safe="")
            )
            job["result"]["filename"] = filename
            job["result"].pop("output_path", None)
        return 200, {"ok": True, "job": job}

    def portrait_file(
        self,
        job_id: str = "",
        filename: str = "",
    ) -> tuple[int, bytes | dict[str, Any], str]:
        output_root = Path(self._comfyui_config().output_dir).resolve()
        clean_filename = str(filename or "").strip()
        if clean_filename:
            if Path(clean_filename).name != clean_filename or clean_filename in {".", ".."}:
                return 403, {"ok": False, "error": "立绘文件名不合法。"}, "application/json"
            output_path = output_root / clean_filename
        else:
            job = self.portrait_jobs.get(job_id)
            if job is None or job.get("status") != "completed":
                return 404, {"ok": False, "error": "立绘文件尚不可用。"}, "application/json"
            output_path = Path(str(job.get("result", {}).get("output_path") or ""))
        try:
            resolved = output_path.resolve(strict=True)
            resolved.relative_to(output_root)
        except (OSError, ValueError):
            return 403, {"ok": False, "error": "立绘文件路径不在允许目录内。"}, "application/json"
        content_type = mimetypes.guess_type(resolved.name)[0] or "application/octet-stream"
        return 200, resolved.read_bytes(), content_type

    def _portrait_prompt_llm(self) -> tuple[OpenAICompatibleClient | None, str]:
        if not self.host.use_llm:
            return None, ""
        config = (
            self.settings.llm_config()
            if self.settings is not None
            else LLMConfig.from_env()
        )
        if not config.api_key:
            return None, ""
        model = (
            config.action_model
            if self.settings is not None
            else str(
                os.environ.get("FU_GM_PORTRAIT_PROMPT_MODEL") or config.action_model
            )
        )
        if self.settings is not None:
            return OpenAICompatibleClient(config), model
        if self._prompt_llm_client is None or self._prompt_llm_model != model:
            self._prompt_llm_client = OpenAICompatibleClient(config)
            self._prompt_llm_model = model
        return self._prompt_llm_client, model

    def _comfyui_config(self) -> ComfyUIConfig:
        if self.settings is not None:
            return self.settings.comfyui_config()
        return ComfyUIConfig.from_env()

    def _portrait_feature_enabled(self) -> bool:
        if self.settings is not None:
            return True
        return str(
            os.environ.get("FU_GM_PORTRAIT_FEATURE_ENABLED", "1")
        ).strip().lower() not in {"0", "false", "no", "disabled", "off"}

    @staticmethod
    def _prompt_payload(prompt: PortraitPrompt) -> dict[str, Any]:
        return {
            "model_profile": prompt.model_profile,
            "positive_prompt": prompt.positive_prompt,
            "negative_prompt": prompt.negative_prompt,
            "style_notes": prompt.style_notes,
            "prompt_version": prompt.prompt_version,
            "source": prompt.source,
            "brief": deepcopy(prompt.brief),
        }

    @staticmethod
    def _limited_text(
        value: object,
        field_name: str,
        maximum: int,
        *,
        allow_empty: bool = False,
    ) -> str:
        clean = str(value or "").strip()
        if not clean and not allow_empty:
            raise ValueError(f"{field_name} 不能为空。")
        if len(clean) > maximum:
            raise ValueError(f"{field_name} 过长。")
        return clean
