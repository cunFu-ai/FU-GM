from __future__ import annotations

import json
import os
import re
import threading
import time
import uuid
from copy import deepcopy
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Protocol
from urllib import error, request
from urllib.parse import urlencode, urlparse

from fu_gm.config import ComfyUIConfig
from fu_gm.llm_client import ChatMessage, OpenAICompatibleClient


_MODEL_PROFILES = {"anima", "krea2", "krea_lora"}
_BRIEF_FIELDS = (
    "species",
    "age",
    "gender_presentation",
    "body",
    "skin",
    "hair",
    "eyes",
    "face",
    "marks",
    "outfit",
    "armor",
    "accessories",
    "weapon",
    "magic",
    "pose",
    "expression",
    "framing",
    "palette",
    "lighting",
    "background",
    "style_notes",
    "identity",
    "theme",
    "origin",
    "world_style",
    "magic_tech_role",
    "classes",
    "skills",
    "spells",
    "bound_arcana",
    "equipment",
)
_FIELD_LABELS = {
    "species": "species",
    "age": "age presentation",
    "gender_presentation": "gender presentation",
    "body": "build",
    "skin": "skin",
    "hair": "hair",
    "eyes": "eyes",
    "face": "facial features",
    "marks": "distinctive marks",
    "outfit": "outfit",
    "armor": "armor",
    "accessories": "accessories",
    "weapon": "weapon",
    "magic": "magic",
    "pose": "pose",
    "expression": "expression",
    "framing": "framing",
    "palette": "color palette",
    "lighting": "lighting",
    "background": "background",
    "style_notes": "style direction",
    "identity": "character identity",
    "theme": "emotional theme",
    "origin": "origin",
    "world_style": "world style",
    "magic_tech_role": "magic and technology",
    "classes": "classes and levels",
    "skills": "signature skills",
    "spells": "known spells",
    "bound_arcana": "bound arcana",
    "equipment": "carried equipment",
}


@dataclass
class PortraitPrompt:
    model_profile: str
    positive_prompt: str
    negative_prompt: str
    style_notes: str = ""
    prompt_version: str = "portrait-prompt-v2"
    source: str = "deterministic"
    brief: dict[str, Any] = field(default_factory=dict)


@dataclass
class ComfyUIResult:
    prompt_id: str
    output_path: str
    source_filename: str
    model_profile: str
    seed: int


class ComfyTransport(Protocol):
    def post_json(self, url: str, payload: dict[str, Any], timeout: float) -> dict[str, Any]: ...

    def get_json(self, url: str, timeout: float) -> dict[str, Any]: ...

    def get_bytes(self, url: str, timeout: float) -> bytes: ...


class UrlLibComfyTransport:
    @staticmethod
    def _read(url: str, *, timeout: float, payload: bytes | None = None) -> bytes:
        headers = {"Accept": "application/json"}
        if payload is not None:
            headers["Content-Type"] = "application/json"
        http_request = request.Request(
            url=url,
            data=payload,
            headers=headers,
            method="POST" if payload is not None else "GET",
        )
        try:
            with request.urlopen(http_request, timeout=timeout) as response:
                return response.read()
        except error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"ComfyUI HTTP {exc.code}: {body[:500]}") from exc

    def post_json(self, url: str, payload: dict[str, Any], timeout: float) -> dict[str, Any]:
        raw = self._read(
            url,
            timeout=timeout,
            payload=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        )
        return json.loads(raw.decode("utf-8"))

    def get_json(self, url: str, timeout: float) -> dict[str, Any]:
        return json.loads(self._read(url, timeout=timeout).decode("utf-8"))

    def get_bytes(self, url: str, timeout: float) -> bytes:
        return self._read(url, timeout=timeout)


class CharacterPortraitPromptService:
    """Turns player-facing character fields into a bounded portrait brief."""

    def build_brief(self, payload: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise ValueError("立绘参数必须是 JSON 对象。")
        build = payload.get("build") if isinstance(payload.get("build"), dict) else {}
        presentation = (
            payload.get("presentation")
            if isinstance(payload.get("presentation"), dict)
            else {}
        )
        appearance = (
            presentation.get("appearance")
            if isinstance(presentation.get("appearance"), dict)
            else payload.get("appearance")
        )
        appearance = appearance if isinstance(appearance, dict) else {}
        source = {**build, **appearance}
        brief: dict[str, Any] = {}
        for field_name in _BRIEF_FIELDS:
            clean = self._clean_brief_value(source.get(field_name))
            if clean not in (None, "", [], {}):
                brief[field_name] = clean
        return brief

    def create_prompt(
        self,
        payload: dict[str, Any],
        *,
        model_profile: str,
        allow_creative_fill: bool = False,
        require_llm: bool = False,
        llm_client: OpenAICompatibleClient | None = None,
        llm_model: str = "",
    ) -> PortraitPrompt:
        profile = self.normalize_profile(model_profile)
        brief = self.build_brief(payload)
        if not brief:
            raise ValueError("至少填写一项外貌、服装、武器或角色概念后才能生成立绘提示词。")
        if require_llm and (llm_client is None or not llm_model):
            raise ValueError(
                "尚未配置可用的立绘提示词模型，请检查 FU_GM API 与模型配置。"
            )
        if llm_client is not None and llm_model:
            try:
                return self._llm_prompt(
                    brief,
                    model_profile=profile,
                    allow_creative_fill=allow_creative_fill,
                    llm_client=llm_client,
                    llm_model=llm_model,
                )
            except Exception as exc:
                if require_llm:
                    raise ValueError(f"LLM 整理立绘提示词失败：{exc}") from exc
        return self._deterministic_prompt(brief, model_profile=profile)

    @staticmethod
    def normalize_profile(value: str) -> str:
        token = re.sub(r"[\s_-]+", "", str(value or "anima").strip().lower())
        aliases = {
            "anima": "anima",
            "krea": "krea2",
            "krea2": "krea2",
            "krealora": "krea_lora",
            "krea2lora": "krea_lora",
        }
        profile = aliases.get(token, token)
        if profile not in _MODEL_PROFILES:
            raise ValueError(f"未知立绘模型配置：{value}")
        return profile

    def _deterministic_prompt(
        self,
        brief: dict[str, Any],
        *,
        model_profile: str,
    ) -> PortraitPrompt:
        details = [
            f"{_FIELD_LABELS[key]}: {self._value_text(value)}"
            for key, value in brief.items()
        ]
        negative = (
            "multiple characters, duplicate person, extra limbs, malformed hands, floating weapons, "
            "incorrect equipment, cropped head, cropped feet, obscured face, busy background, "
            "text, logo, watermark, UI elements, low resolution, blurry"
        )
        if model_profile == "anima":
            positive = ", ".join(
                [
                    "solo character",
                    "full-body JRPG character illustration",
                    "clear memorable silhouette",
                    "expressive face and eyes",
                    "clean expressive linework",
                    "detailed costume design",
                    *details,
                    "functional equipment",
                    "coherent materials and restrained visual motifs",
                    "entire figure visible",
                    "plain atmospheric backdrop with a subtle grounding shadow",
                ]
            )
            style = "Anime illustration prompt mixing concise tags with explicit natural-language traits."
        else:
            positive = (
                "Create a polished full-body JRPG character portrait of one original character, "
                "with a clear memorable silhouette, expressive face, accurate functional equipment, "
                "coherent materials, and restrained visual motifs. "
                + "; ".join(details)
                + ". Show the entire figure in a natural three-quarter pose. Use controlled lighting "
                "and a restrained fantasy backdrop with a subtle grounding shadow."
            )
            style = (
                "Natural-language art direction for Krea 2 with the configured style LoRA, portrait framing."
                if model_profile == "krea_lora"
                else "Natural-language art direction for Krea 2, portrait aspect ratio 2:3."
            )
        return PortraitPrompt(
            model_profile=model_profile,
            positive_prompt=positive,
            negative_prompt=negative,
            style_notes=style,
            brief=deepcopy(brief),
        )

    def _llm_prompt(
        self,
        brief: dict[str, Any],
        *,
        model_profile: str,
        allow_creative_fill: bool,
        llm_client: OpenAICompatibleClient,
        llm_model: str,
    ) -> PortraitPrompt:
        system = (
            "你是《最终物语》原创角色立绘提示词设计师。根据 JSON brief 中的身份、主题、"
            "故乡、职业、技能、法术、装备和外貌信息，整理成可直接用于生图模型的英文提示词。"
            "必须保持角色设定与规则事实不变；不得推断姓名对应的性别、族裔、年龄或其他身份属性；"
            "不得加入 brief 中没有依据的新剧情、关系、阵营或能力。画面必须是单个原创角色、"
            "从头到脚的全身立绘、轮廓清楚、面部可辨、装备准确、背景克制、无文字与水印。"
            "将抽象性格和主题转化为表情、姿态、配色、材质和至多三个可见叙事意象；"
            "优先保证角色的固定身体特征、主武器和标志性服装在缩小到角色卡尺寸后仍可辨认。"
            "禁止画师姓名、在世艺术家风格、现有 IP 或角色名称，以及色情或裸露内容。"
            "输出 JSON 对象，字段仅为 positive_prompt、negative_prompt、style_notes；"
            "positive_prompt 与 negative_prompt 必须使用英文。negative_prompt 只描述低画质、"
            "错误人体、重复角色、裁切、文字、水印和不安全内容等应排除的画面问题；"
            "不得把男性、女性、儿童、老人、肤色、体型等身份或外貌类别本身列为负向词，"
            "也不得否定 brief 中明确提供的特征。"
        )
        if allow_creative_fill:
            system += (
                "玩家允许你根据职业功能、主题与世界风格补全缺失的服装材质、配色、姿势、"
                "表情、道具陈列和光线等次要美术细节，但这些补全不得成为新的剧情或规则事实。"
            )
        else:
            system += "未明确填写的美术细节保持中性和简洁，不要主动扩写。"
        profile_guidance = {
            "anima": (
                "Anima/AnimaTurbo：英文 booru 标签与简洁自然语言混合；solo, full body, "
                "original JRPG character, clean anime linework, readable costume layers"
            ),
            "krea2": "Krea 2：清晰的英文自然语言美术指导，原创 JRPG 全身角色立绘，2:3 竖幅",
            "krea_lora": (
                "Krea 2 + LoRA：清晰连贯的英文自然语言美术指导；当前工作流已加载风格 LoRA，"
                "不要堆叠互相冲突的画风标签；强调人物身份锚点、材质、姿势和可读轮廓"
            ),
        }
        request_payload = {
            "model_profile": model_profile,
            "brief": brief,
            "profile_guidance": profile_guidance[model_profile],
        }
        raw = llm_client.create_chat_completion(
            model=llm_model,
            messages=[
                ChatMessage(role="system", content=system),
                ChatMessage(role="user", content=json.dumps(request_payload, ensure_ascii=False)),
            ],
            temperature=0.4 if allow_creative_fill else 0.2,
            response_format={"type": "json_object"},
            max_tokens=1200,
            operation="character_portrait_prompt",
            max_recovery_retries=0,
            retry_without_response_format_on_empty=True,
        )
        data = self._parse_json_object(raw)
        positive = str(data.get("positive_prompt") or "").strip()
        negative = str(data.get("negative_prompt") or "").strip()
        if not positive or not negative:
            raise ValueError("立绘提示词模型没有返回完整字段。")
        return PortraitPrompt(
            model_profile=model_profile,
            positive_prompt=positive[:8000],
            negative_prompt=negative[:4000],
            style_notes=str(data.get("style_notes") or "")[:2000],
            source="llm",
            brief=deepcopy(brief),
        )

    @staticmethod
    def _parse_json_object(raw: str) -> dict[str, Any]:
        clean = str(raw or "").strip()
        if clean.startswith("```"):
            clean = re.sub(r"^```(?:json)?\s*|\s*```$", "", clean, flags=re.I | re.S)
        data = json.loads(clean)
        if not isinstance(data, dict):
            raise ValueError("立绘提示词响应必须是 JSON 对象。")
        return data

    @staticmethod
    def _value_text(value: object) -> str:
        if isinstance(value, list):
            return ", ".join(str(item) for item in value)
        if isinstance(value, dict):
            return json.dumps(value, ensure_ascii=False, separators=(", ", ": "))
        return str(value)

    @classmethod
    def _clean_brief_value(cls, value: object, *, depth: int = 0) -> object | None:
        if depth > 2:
            return None
        if isinstance(value, str):
            clean = " ".join(value.split()).strip()
            return clean[:1000] or None
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return value
        if isinstance(value, list):
            cleaned = [
                cls._clean_brief_value(item, depth=depth + 1)
                for item in value[:24]
            ]
            return [item for item in cleaned if item not in (None, "", [], {})]
        if isinstance(value, dict):
            cleaned: dict[str, object] = {}
            for raw_key, raw_value in list(value.items())[:24]:
                key = " ".join(str(raw_key or "").split()).strip()[:200]
                item = cls._clean_brief_value(raw_value, depth=depth + 1)
                if key and item not in (None, "", [], {}):
                    cleaned[key] = item
            return cleaned
        return None


class ComfyUIClient:
    """Executes trusted API-format workflows with a tiny placeholder surface."""

    def __init__(
        self,
        config: ComfyUIConfig,
        transport: ComfyTransport | None = None,
        *,
        monotonic=time.monotonic,
        sleeper=time.sleep,
    ) -> None:
        self.config = config
        self.transport = transport or UrlLibComfyTransport()
        self.monotonic = monotonic
        self.sleeper = sleeper

    def generate(
        self,
        prompt: PortraitPrompt,
        *,
        seed: int | None = None,
        filename_prefix: str = "fu_character",
    ) -> ComfyUIResult:
        self._validate_base_url()
        if not self.config.usable(prompt.model_profile):
            raise ValueError("ComfyUI 未启用，或当前模型尚未配置工作流文件。")
        resolved_seed = int(seed if seed is not None else int.from_bytes(os.urandom(8), "big"))
        workflow = self._load_workflow(prompt.model_profile)
        width, height = self.config.dimensions(prompt.model_profile)
        variables: dict[str, object] = {
            "POSITIVE_PROMPT": prompt.positive_prompt,
            "NEGATIVE_PROMPT": prompt.negative_prompt,
            "SEED": resolved_seed,
            "WIDTH": width,
            "HEIGHT": height,
            "FILENAME_PREFIX": self._safe_filename(filename_prefix),
        }
        rendered = self._replace_placeholders(workflow, variables)
        submitted = self.transport.post_json(
            f"{self.config.base_url}/prompt",
            {"prompt": rendered, "client_id": str(uuid.uuid4())},
            self.config.timeout_seconds,
        )
        prompt_id = str(submitted.get("prompt_id") or "").strip()
        if not prompt_id:
            raise RuntimeError("ComfyUI 没有返回 prompt_id。")
        image = self._wait_for_image(prompt_id)
        query = urlencode(
            {
                "filename": image["filename"],
                "subfolder": image.get("subfolder", ""),
                "type": image.get("type", "output"),
            }
        )
        content = self.transport.get_bytes(
            f"{self.config.base_url}/view?{query}",
            self.config.timeout_seconds,
        )
        output_dir = Path(self.config.output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        suffix = Path(str(image["filename"])).suffix.lower()
        if suffix not in {".png", ".jpg", ".jpeg", ".webp"}:
            suffix = ".png"
        output_path = output_dir / f"{self._safe_filename(filename_prefix)}_{prompt_id[:12]}{suffix}"
        output_path.write_bytes(content)
        return ComfyUIResult(
            prompt_id=prompt_id,
            output_path=str(output_path.resolve()),
            source_filename=str(image["filename"]),
            model_profile=prompt.model_profile,
            seed=resolved_seed,
        )

    def _load_workflow(self, model_profile: str) -> dict[str, Any]:
        path = Path(self.config.workflow_path(model_profile)).expanduser()
        if not path.is_file():
            raise ValueError(f"找不到 ComfyUI 工作流：{path}")
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not data:
            raise ValueError("ComfyUI 工作流必须是非空的 API-format JSON 对象。")
        return data

    def _wait_for_image(self, prompt_id: str) -> dict[str, Any]:
        deadline = self.monotonic() + self.config.timeout_seconds
        while self.monotonic() < deadline:
            history = self.transport.get_json(
                f"{self.config.base_url}/history/{prompt_id}",
                min(30.0, self.config.timeout_seconds),
            )
            record = history.get(prompt_id, history)
            if isinstance(record, dict):
                status = record.get("status")
                if isinstance(status, dict) and status.get("status_str") == "error":
                    raise RuntimeError("ComfyUI 工作流执行失败。")
                outputs = record.get("outputs")
                if isinstance(outputs, dict):
                    for node_output in outputs.values():
                        if not isinstance(node_output, dict):
                            continue
                        images = node_output.get("images")
                        if isinstance(images, list) and images and isinstance(images[0], dict):
                            if images[0].get("filename"):
                                return images[0]
            self.sleeper(self.config.poll_interval_seconds)
        raise TimeoutError(f"等待 ComfyUI 任务 {prompt_id} 超时。")

    def _validate_base_url(self) -> None:
        parsed = urlparse(self.config.base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("ComfyUI 地址必须是 http 或 https URL。")
        if not self.config.allow_remote and parsed.hostname not in {
            "127.0.0.1",
            "localhost",
            "::1",
        }:
            raise ValueError("默认只允许连接本机 ComfyUI；远程地址需要显式启用。")

    @classmethod
    def _replace_placeholders(cls, value: object, variables: dict[str, object]) -> object:
        if isinstance(value, dict):
            return {key: cls._replace_placeholders(item, variables) for key, item in value.items()}
        if isinstance(value, list):
            return [cls._replace_placeholders(item, variables) for item in value]
        if not isinstance(value, str):
            return value
        exact = re.fullmatch(r"\{\{([A-Z_]+)\}\}", value)
        if exact and exact.group(1) in variables:
            return variables[exact.group(1)]
        result = value
        for key, replacement in variables.items():
            result = result.replace("{{" + key + "}}", str(replacement))
        return result

    @staticmethod
    def _safe_filename(value: str) -> str:
        cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value or "").strip())
        return cleaned.strip("._") or "fu_character"


class PortraitJobManager:
    def __init__(self) -> None:
        self._jobs: dict[str, dict[str, Any]] = {}
        self._lock = threading.RLock()

    def submit(self, worker, *args, **kwargs) -> dict[str, Any]:
        job_id = str(uuid.uuid4())
        record = {
            "job_id": job_id,
            "status": "queued",
            "created_at": datetime_now(),
            "updated_at": datetime_now(),
            "result": {},
            "error": "",
        }
        with self._lock:
            self._jobs[job_id] = record
        thread = threading.Thread(
            target=self._run,
            args=(job_id, worker, args, kwargs),
            daemon=True,
            name=f"fu-portrait-{job_id[:8]}",
        )
        thread.start()
        return deepcopy(record)

    def get(self, job_id: str) -> dict[str, Any] | None:
        with self._lock:
            record = self._jobs.get(str(job_id or ""))
            return deepcopy(record) if record is not None else None

    def _run(self, job_id: str, worker, args: tuple, kwargs: dict[str, Any]) -> None:
        self._update(job_id, status="running")
        try:
            result = worker(*args, **kwargs)
            if hasattr(result, "__dataclass_fields__"):
                result = asdict(result)
            self._update(job_id, status="completed", result=result)
        except Exception as exc:
            self._update(job_id, status="failed", error=str(exc))

    def _update(self, job_id: str, **changes: Any) -> None:
        with self._lock:
            record = self._jobs[job_id]
            record.update(changes)
            record["updated_at"] = datetime_now()


def datetime_now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()
