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
_SCENE_MODES = {"identity_context", "clean_portrait"}
_DEFAULT_NEGATIVE_PROMPTS = {
    # The bundled workflows use distilled Turbo checkpoints at ComfyUI CFG 1.0,
    # where classifier-free guidance and its negative branch are disabled.
    "anima": "",
    "krea2": "",
    "krea_lora": "",
}
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
    "scene",
    "activity",
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
    "scene": "scene",
    "activity": "current activity",
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
    prompt_version: str = "portrait-prompt-v6"
    source: str = "deterministic"
    brief: dict[str, Any] = field(default_factory=dict)


@dataclass
class ComfyUIResult:
    prompt_id: str
    output_path: str
    source_filename: str
    model_profile: str
    seed: int | None


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
        scene_mode = self.scene_mode(payload)
        brief = self.build_brief(payload)
        if scene_mode == "clean_portrait":
            brief.pop("scene", None)
            brief.pop("activity", None)
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
                    scene_mode=scene_mode,
                    allow_creative_fill=allow_creative_fill,
                    llm_client=llm_client,
                    llm_model=llm_model,
                )
            except Exception as exc:
                if require_llm:
                    raise ValueError(f"LLM 整理立绘提示词失败：{exc}") from exc
        return self._deterministic_prompt(
            brief,
            model_profile=profile,
            scene_mode=scene_mode,
        )

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

    @classmethod
    def default_negative_prompt(cls, model_profile: str) -> str:
        profile = cls.normalize_profile(model_profile)
        return _DEFAULT_NEGATIVE_PROMPTS[profile]

    @classmethod
    def scene_mode(cls, payload: dict[str, Any]) -> str:
        presentation = (
            payload.get("presentation")
            if isinstance(payload.get("presentation"), dict)
            else {}
        )
        portrait = (
            presentation.get("portrait")
            if isinstance(presentation.get("portrait"), dict)
            else {}
        )
        return cls.normalize_scene_mode(
            portrait.get("scene_mode") or payload.get("scene_mode") or "identity_context"
        )

    @staticmethod
    def normalize_scene_mode(value: str) -> str:
        token = re.sub(r"[\s_-]+", "", str(value or "identity_context").strip().lower())
        aliases = {
            "identitycontext": "identity_context",
            "context": "identity_context",
            "narrative": "identity_context",
            "scene": "identity_context",
            "cleanportrait": "clean_portrait",
            "clean": "clean_portrait",
            "portrait": "clean_portrait",
        }
        mode = aliases.get(token, token)
        if mode not in _SCENE_MODES:
            raise ValueError(f"未知立绘画面模式：{value}")
        return mode

    def _deterministic_prompt(
        self,
        brief: dict[str, Any],
        *,
        model_profile: str,
        scene_mode: str,
    ) -> PortraitPrompt:
        # A deterministic fallback cannot infer whether a scene is work, daily life,
        # or combat. Inventory weapons are therefore omitted unless the player has
        # already made them part of an explicit activity or pose.
        details = [
            f"{_FIELD_LABELS[key]}: {self._value_text(value)}"
            for key, value in brief.items()
            if key not in {"weapon", "equipment"}
        ]
        negative = self.default_negative_prompt(model_profile)
        if model_profile == "anima" and scene_mode == "identity_context":
            positive = ", ".join(
                [
                    "one primary character",
                    "vertical contextual JRPG character illustration",
                    "full-body shot",
                    "eye-level camera",
                    "character centered on the vertical axis",
                    "body turned three-quarters toward the camera",
                    "identity-revealing everyday environment",
                    "character engaged in a role-appropriate activity",
                    "face and both hands visible",
                    "clear hand-task or hand-prop interaction",
                    "clear memorable silhouette",
                    "expressive readable face and eyes",
                    "clean expressive linework",
                    "detailed costume design",
                    *details,
                    "role-specific tools and occupational props relevant to the stated activity",
                    "coherent materials and restrained visual motifs",
                    "main character remains the dominant focal point",
                    "environment supports the character without obscuring them",
                ]
            )
            style = (
                "Anime illustration prompt mixing concise tags with explicit natural-language traits, "
                "contextual identity scene."
            )
        elif model_profile == "anima":
            positive = ", ".join(
                [
                    "solo character",
                    "full-body JRPG character illustration",
                    "clear memorable silhouette",
                    "expressive face and eyes",
                    "clean expressive linework",
                    "detailed costume design",
                    *details,
                    "role-specific costume details",
                    "coherent materials and restrained visual motifs",
                    "entire figure visible",
                    "plain atmospheric backdrop with a subtle grounding shadow",
                ]
            )
            style = "Anime illustration prompt mixing concise tags with explicit natural-language traits."
        elif scene_mode == "identity_context":
            positive = (
                "Create a polished vertical JRPG character illustration with one clearly dominant "
                "original character in an everyday setting that makes their identity immediately "
                "legible. Show the character actively doing something appropriate to their role, "
                "with an expressive readable face, role-appropriate tools, coherent materials, "
                "and restrained visual motifs. "
                + "; ".join(details)
                + ". Use a full-body shot at eye level, place the character on the central vertical axis, "
                "turn their body three-quarters toward the camera, keep their face and both hands visible, "
                "and show the active hand-object relationship "
                "clearly. Keep the main character visually dominant, with occupational props arranged "
                "around the action without obscuring the character."
            )
            style = (
                "Natural-language art direction for Krea 2 with the configured style LoRA, contextual identity scene."
                if model_profile == "krea_lora"
                else "Natural-language art direction for Krea 2, contextual identity scene in a 2:3 vertical composition."
            )
        else:
            positive = (
                "Create a polished full-body JRPG character portrait of one original character, "
                "with a clear memorable silhouette, expressive face, coherent functional clothing, "
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
        scene_mode: str,
        allow_creative_fill: bool,
        llm_client: OpenAICompatibleClient,
        llm_model: str,
    ) -> PortraitPrompt:
        system = (
            "你是《最终物语》原创角色立绘提示词设计师。根据 JSON brief 中的身份、主题、"
            "故乡、职业、技能、法术、装备和外貌信息，整理成可直接用于生图模型的英文提示词。"
            "必须保持角色设定与规则事实不变；不得推断姓名对应的性别、族裔、年龄或其他身份属性；"
            "不得加入 brief 中没有依据的新剧情、关系、阵营、专有名词或能力。画面必须以一个"
            "原创角色为唯一视觉焦点，轮廓清楚、面部可辨、外貌与已决定展示的道具准确、无文字与水印。"
            "将抽象性格和主题转化为明确的表情、姿态、配色和材质，不要把抽象主题扩写成额外剧情；"
            "优先保证角色的固定身体特征、身份动作和标志性服装在缩小到角色卡尺寸后仍可辨认。"
            "brief 中的 weapon 和 equipment 仅表示角色可以携带的装备，不是必须入镜或手持的清单。"
            "请先在内部根据身份、场所、活动、姿态和整体叙事语义，把当前画面判断为工作状态、生活状态或"
            "战斗特写；不要输出这段判断，也不要依赖单个词语机械分类。工作状态与生活状态下，positive_prompt "
            "应完全不提武器，不得为了说明省略武器而写 no weapon、without weapon、stowed、sheathed 或 holstered。"
            "只有角色身份的视觉连续性确实依赖随身兵器时，才可在非战斗画面中将它低调别在身侧，并明确让它"
            "保持次要；只有战斗特写或玩家明确指定的持械动作，才可让武器进入双手动作或成为视觉重点。不得仅"
            "因为职业、技能或装备列表中存在武器，就自动安排持械、擦拭武器或备战动作。工作与生活画面的"
            "道具选择应优先采用与当前活动直接相关的职业工具和生活物件。"
            "禁止画师姓名、在世艺术家风格、现有 IP 或角色名称，以及色情或裸露内容。"
            "输出 JSON 对象，字段仅为 positive_prompt、style_notes，两个字段都必须使用英文。"
        )
        if scene_mode == "identity_context":
            system += (
                "当前画面模式为‘身份情境’：目标是一幅能让人一眼看出角色身份的竖幅叙事型角色插画，"
                "不是棚拍式站立人设图。若 brief.scene 或 brief.activity 已填写，必须严格采用；若缺失，"
                "则根据 character identity 优先，其次参考 classes and levels、signature skills、known spells、"
                "origin、world style 和 magic and technology，语义推导一个普通、日常、"
                "非剧情化且符合身份的场所与当前动作。普通职业场所和日常动作不视为新增剧情事实，但不得"
                "为其虚构命名地点、具体事件、组织、关系或任务。角色必须正在做事，而不是仅仅持物站立。"
                "在撰写 positive_prompt 前，先在内部完成一次单一分镜定案，不输出分析过程，并把以下决定"
                "全部明确写入 positive_prompt：一个具体且非专名的日常场所；一个可在单幅画中清楚表现的"
                "连续动作，包括双手与关键道具的关系；一个固定景别；一个固定机位与视角；角色在画面中的"
                "位置、身体朝向与视线方向；从二到四之间选定一个确切数量的身份道具，优先选择非武器的"
                "职业工具，并分别说明每件道具"
                "的位置；一个明确的主光源、来向与照明效果。需要表现交易、授课、诊疗等互动时，还必须确定"
                "次要人物的确切数量、位置和行为，并保持其低细节、弱焦点；不需要互动时则明确保持角色独处。"
                "最终英文提示词必须像已经定案的分镜指令，每句话都能直接执行。不得提供备选地点、备选动作、"
                "备选景别、备选道具或含糊数量；不得使用 or、and/or、either、such as、for example、e.g.、"
                "one or two、two to four、up to、at most、approximately、several、a few 等选择式、举例式或"
                "含糊数量措辞，也不得用斜杠、括号或范围把决定留给生图模型。"
                "景别只能选用一个明确术语，不得同时堆叠 full-body shot、medium-long shot、three-quarter-length "
                "shot、waist-up shot 或 close-up；人物的 three-quarter body angle 属于身体朝向，必须与景别"
                "分开表述，不能用 three-quarter view 含糊代替景别。"
                "若 brief 本身含有多个可选方案，在不改变角色固定身份事实的前提下选定其中一个最适合画面的"
                "方案。若年龄等身份信息未提供，直接省略，不要写 age-ambiguous、unspecified 或 unknown。"
                "背景应具有叙事信息，同时不得遮挡角色面部、标志服装与关键道具。"
            )
        else:
            system += (
                "当前画面模式为‘纯角色立绘’：只表现单个原创角色，从头到脚的全身构图，采用简洁克制、"
                "不抢夺注意力的背景，不添加其他人物或身份场景。根据角色整体语义决定当前状态；默认采用"
                "生活化或工作化的自然姿态，并从 positive_prompt 中直接省略武器。只有玩家明确设定战斗特写"
                "或持械动作时才让武器参与动作；身份连续性确有必要时可低调别在身侧。"
            )
        if allow_creative_fill and scene_mode == "identity_context":
            system += (
                "玩家允许你根据职业功能、主题与世界风格补全缺失的服装材质、配色、姿势、"
                "表情、职业工具与生活物件陈列、环境细节和光线等次要美术细节，但这些补全不得成为新的剧情或规则事实。"
            )
        elif allow_creative_fill:
            system += (
                "玩家允许你根据职业功能、主题与世界风格补全缺失的服装材质、配色、姿势、"
                "表情、日常随身物件和光线等次要美术细节，但不得添加环境叙事或新的剧情与规则事实。"
            )
        else:
            system += "除当前画面模式明确授权的身份情境推导外，未填写的美术细节保持中性和简洁。"
        if scene_mode == "identity_context":
            profile_guidance = {
                "anima": (
                    "Anima/AnimaTurbo：英文 booru 标签与简洁自然语言混合；one primary character, "
                    "contextual character scene, vertical composition, clean anime linework, readable face and costume"
                ),
                "krea2": "Krea 2：清晰的英文自然语言美术指导，原创 JRPG 身份情境插画，2:3 竖幅",
                "krea_lora": (
                    "Krea 2 + LoRA：清晰连贯的英文自然语言美术指导；当前工作流已加载风格 LoRA，"
                    "不要堆叠互相冲突的画风标签；强调身份场景、当前动作、材质与唯一视觉焦点"
                ),
            }
        else:
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
            "scene_mode": scene_mode,
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
        if not positive:
            raise ValueError("立绘提示词模型没有返回完整字段。")
        return PortraitPrompt(
            model_profile=model_profile,
            positive_prompt=positive[:8000],
            negative_prompt=self.default_negative_prompt(model_profile),
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
        return self._download_result(
            image,
            prompt_id=prompt_id,
            filename_prefix=filename_prefix,
            model_profile=prompt.model_profile,
            seed=resolved_seed,
        )

    def recover_latest(
        self,
        *,
        filename_prefix: str,
        model_profile: str,
    ) -> ComfyUIResult | None:
        """Recover the newest completed image after the workshop process restarts."""

        self._validate_base_url()
        safe_prefix = self._safe_filename(filename_prefix)
        queue = self.transport.get_json(
            f"{self.config.base_url}/queue",
            min(30.0, self.config.timeout_seconds),
        )
        if self._queue_has_prefix(queue, safe_prefix):
            return None

        history = self.transport.get_json(
            f"{self.config.base_url}/history?max_items=100",
            min(30.0, self.config.timeout_seconds),
        )
        candidates: list[tuple[float, int, str, dict[str, Any], int | None]] = []
        if isinstance(history, dict):
            for order, (raw_prompt_id, record) in enumerate(history.items()):
                if not isinstance(record, dict):
                    continue
                prompt_id = str(raw_prompt_id or "").strip()
                outputs = record.get("outputs")
                if not prompt_id or not isinstance(outputs, dict):
                    continue
                timestamp = self._history_timestamp(record)
                seed = self._history_seed(record)
                for node_output in outputs.values():
                    if not isinstance(node_output, dict):
                        continue
                    images = node_output.get("images")
                    if not isinstance(images, list):
                        continue
                    for image in images:
                        if not isinstance(image, dict):
                            continue
                        filename = str(image.get("filename") or "")
                        if filename == safe_prefix or filename.startswith(safe_prefix + "_"):
                            candidates.append((timestamp, order, prompt_id, image, seed))

        if not candidates:
            raise FileNotFoundError("ComfyUI 历史中没有找到这个角色已完成的立绘。")
        _, _, prompt_id, image, seed = max(candidates, key=lambda item: (item[0], item[1]))
        return self._download_result(
            image,
            prompt_id=prompt_id,
            filename_prefix=safe_prefix,
            model_profile=model_profile,
            seed=seed,
        )

    def _download_result(
        self,
        image: dict[str, Any],
        *,
        prompt_id: str,
        filename_prefix: str,
        model_profile: str,
        seed: int | None,
    ) -> ComfyUIResult:
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
        if not content:
            raise RuntimeError("ComfyUI 返回了空的立绘文件。")
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
            model_profile=model_profile,
            seed=seed,
        )

    @classmethod
    def _queue_has_prefix(cls, queue: object, filename_prefix: str) -> bool:
        if not isinstance(queue, dict):
            return False
        for queue_name in ("queue_running", "queue_pending"):
            entries = queue.get(queue_name)
            if not isinstance(entries, list):
                continue
            if any(cls._contains_filename_prefix(entry, filename_prefix) for entry in entries):
                return True
        return False

    @classmethod
    def _contains_filename_prefix(cls, value: object, filename_prefix: str) -> bool:
        if isinstance(value, dict):
            for key, item in value.items():
                if key == "filename_prefix":
                    leaf = str(item or "").replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
                    if leaf == filename_prefix:
                        return True
                if cls._contains_filename_prefix(item, filename_prefix):
                    return True
        elif isinstance(value, list):
            return any(cls._contains_filename_prefix(item, filename_prefix) for item in value)
        return False

    @staticmethod
    def _history_timestamp(record: dict[str, Any]) -> float:
        latest = 0.0
        status = record.get("status")
        messages = status.get("messages") if isinstance(status, dict) else None
        if not isinstance(messages, list):
            return latest
        for message in messages:
            if not isinstance(message, list) or len(message) < 2 or not isinstance(message[1], dict):
                continue
            try:
                latest = max(latest, float(message[1].get("timestamp") or 0))
            except (TypeError, ValueError):
                continue
        return latest

    @staticmethod
    def _history_seed(record: dict[str, Any]) -> int | None:
        prompt = record.get("prompt")
        if not isinstance(prompt, list) or len(prompt) < 3 or not isinstance(prompt[2], dict):
            return None
        for node in prompt[2].values():
            if not isinstance(node, dict) or not isinstance(node.get("inputs"), dict):
                continue
            seed = node["inputs"].get("seed")
            if seed in (None, ""):
                continue
            try:
                return int(seed)
            except (TypeError, ValueError):
                continue
        return None

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
