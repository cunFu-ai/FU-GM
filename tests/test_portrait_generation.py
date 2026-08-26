from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from fu_gm.config import ComfyUIConfig
from fu_gm.portrait_generation import (
    CharacterPortraitPromptService,
    ComfyUIClient,
    PortraitJobManager,
)


class FakePromptClient:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def create_chat_completion(self, **kwargs):
        self.calls.append(kwargs)
        return json.dumps(
            {
                "positive_prompt": "solo hero, silver hair, green travel coat",
                "style_notes": "clean anime illustration",
            }
        )


class FailingPromptClient:
    def create_chat_completion(self, **kwargs):
        raise RuntimeError("provider unavailable")


class UnexpectedNegativePromptClient:
    def create_chat_completion(self, **kwargs):
        return json.dumps(
            {
                "positive_prompt": "one merchant at a market stall",
                "negative_prompt": "this field must be ignored",
                "style_notes": "contextual scene",
            }
        )


class FakeComfyTransport:
    def __init__(self) -> None:
        self.submitted = {}

    def post_json(self, url, payload, timeout):
        self.submitted = payload
        return {"prompt_id": "prompt-123"}

    def get_json(self, url, timeout):
        return {
            "prompt-123": {
                "outputs": {
                    "9": {
                        "images": [
                            {
                                "filename": "portrait.png",
                                "subfolder": "",
                                "type": "output",
                            }
                        ]
                    }
                }
            }
        }

    def get_bytes(self, url, timeout):
        return b"fake-png"


class RecoveryComfyTransport:
    def __init__(self, *, active: bool = False) -> None:
        self.active = active
        self.history_requested = False
        self.view_url = ""

    def post_json(self, url, payload, timeout):
        raise AssertionError("recovery must not submit a new prompt")

    def get_json(self, url, timeout):
        if url.endswith("/queue"):
            graph = {
                "16": {
                    "inputs": {"filename_prefix": "FU-GM/card-123"},
                    "class_type": "SaveImage",
                }
            }
            return {
                "queue_running": [[1, "active-prompt", graph, {}, ["16"]]] if self.active else [],
                "queue_pending": [],
            }
        if "/history?" in url:
            self.history_requested = True
            return {
                "older": {
                    "prompt": [0, "older", {"2": {"inputs": {"seed": 11}}}],
                    "outputs": {
                        "16": {
                            "images": [
                                {
                                    "filename": "card-123_00001_.png",
                                    "subfolder": "FU-GM",
                                    "type": "output",
                                }
                            ]
                        }
                    },
                    "status": {"messages": [["execution_success", {"timestamp": 1000}]]},
                },
                "newer": {
                    "prompt": [0, "newer", {"2": {"inputs": {"seed": 22}}}],
                    "outputs": {
                        "16": {
                            "images": [
                                {
                                    "filename": "card-123_00002_.png",
                                    "subfolder": "FU-GM",
                                    "type": "output",
                                }
                            ]
                        }
                    },
                    "status": {"messages": [["execution_success", {"timestamp": 2000}]]},
                },
                "unrelated": {
                    "prompt": [0, "unrelated", {"2": {"inputs": {"seed": 33}}}],
                    "outputs": {
                        "16": {
                            "images": [
                                {
                                    "filename": "another-card_00003_.png",
                                    "subfolder": "FU-GM",
                                    "type": "output",
                                }
                            ]
                        }
                    },
                    "status": {"messages": [["execution_success", {"timestamp": 3000}]]},
                },
            }
        raise AssertionError(f"unexpected recovery URL: {url}")

    def get_bytes(self, url, timeout):
        self.view_url = url
        return b"recovered-png"


class PortraitGenerationTests(unittest.TestCase):
    def test_bundled_anima_workflow_is_clean_and_parameterized(self) -> None:
        workflow_path = (
            Path(__file__).resolve().parents[1]
            / "config"
            / "comfyui_workflows"
            / "anima-api.json"
        )
        workflow = json.loads(workflow_path.read_text(encoding="utf-8"))
        serialized = json.dumps(workflow, ensure_ascii=False).lower()

        self.assertEqual(workflow["597:23"]["inputs"]["value"], "{{POSITIVE_PROMPT}}")
        self.assertEqual(workflow["576"]["inputs"]["text"], "{{NEGATIVE_PROMPT}}")
        self.assertEqual(workflow["599"]["inputs"]["width"], "{{WIDTH}}")
        self.assertEqual(workflow["599"]["inputs"]["height"], "{{HEIGHT}}")
        self.assertNotIn("danbooru", serialized)
        self.assertNotIn("nsfw", serialized)
        self.assertEqual(
            [node_id for node_id, node in workflow.items() if node["class_type"] == "SaveImage"],
            ["593"],
        )

    def test_bundled_krea_lora_workflow_matches_active_comfy_graph(self) -> None:
        workflow_path = (
            Path(__file__).resolve().parents[1]
            / "config"
            / "comfyui_workflows"
            / "krea-lora-api.json"
        )
        workflow = json.loads(workflow_path.read_text(encoding="utf-8"))
        serialized = json.dumps(workflow, ensure_ascii=False).lower()

        self.assertEqual(workflow["6"]["inputs"]["text"], "{{POSITIVE_PROMPT}}")
        self.assertEqual(workflow["20"]["inputs"]["text"], "{{NEGATIVE_PROMPT}}")
        self.assertEqual(workflow["2"]["inputs"]["seed"], "{{SEED}}")
        self.assertEqual(workflow["10"]["inputs"]["width"], "{{WIDTH}}")
        self.assertEqual(workflow["10"]["inputs"]["height"], "{{HEIGHT}}")
        self.assertEqual(
            workflow["25"]["inputs"]["lora_name"],
            "z3zz4-k2-4_c1-st5000.safetensors",
        )
        self.assertNotIn("yoneyamamai", serialized)
        self.assertNotIn("huggingface.co", serialized)
        self.assertEqual(
            [node_id for node_id, node in workflow.items() if node["class_type"] == "SaveImage"],
            ["16"],
        )

    def test_prompt_brief_uses_only_explicit_public_fields(self) -> None:
        service = CharacterPortraitPromptService()
        payload = {
            "build": {
                "hero_name": "米菈",
                "identity": "魔导技师",
                "theme": "自由",
                "origin": "永雨工业城",
            },
            "presentation": {
                "appearance": {
                    "hair": "银白短发",
                    "outfit": "绿色旅行外套",
                    "private_secret": "她其实是帝国继承人",
                }
            },
            "gm_private_context": "反派将在第三幕杀死她",
        }

        prompt = service.create_prompt(payload, model_profile="anima")

        self.assertIn("银白短发", prompt.positive_prompt)
        self.assertIn("绿色旅行外套", prompt.positive_prompt)
        self.assertNotIn("帝国继承人", prompt.positive_prompt)
        self.assertNotIn("第三幕", json.dumps(prompt.brief, ensure_ascii=False))

    def test_prompt_brief_includes_public_character_mechanics(self) -> None:
        service = CharacterPortraitPromptService()

        brief = service.build_brief(
            {
                "build": {
                    "identity": "魔导技师",
                    "classes": {"造物使": 2, "守护者": 1},
                    "skills": {"便携装置": 1},
                    "spells": ["治愈", "护盾"],
                    "bound_arcana": ["机械奥灵"],
                    "equipment": ["符文盾", "旅行装束"],
                    "notes": ["GM 私密伏笔"],
                }
            }
        )

        self.assertEqual(brief["classes"], {"造物使": 2, "守护者": 1})
        self.assertEqual(brief["spells"], ["治愈", "护盾"])
        self.assertNotIn("notes", brief)

    def test_prompt_brief_includes_explicit_scene_and_activity(self) -> None:
        service = CharacterPortraitPromptService()

        brief = service.build_brief(
            {
                "build": {"identity": "商人"},
                "presentation": {
                    "appearance": {
                        "scene": "热闹的露天市集",
                        "activity": "笑着与顾客讲价",
                    }
                },
            }
        )

        self.assertEqual(brief["identity"], "商人")
        self.assertEqual(brief["scene"], "热闹的露天市集")
        self.assertEqual(brief["activity"], "笑着与顾客讲价")

    def test_llm_prompt_is_schema_limited(self) -> None:
        service = CharacterPortraitPromptService()
        client = FakePromptClient()

        prompt = service.create_prompt(
            {"appearance": {"hair": "银白短发"}},
            model_profile="krea2",
            llm_client=client,
            llm_model="fake",
        )

        self.assertEqual(prompt.source, "llm")
        self.assertEqual(prompt.model_profile, "krea2")
        self.assertEqual(prompt.negative_prompt, "")
        self.assertEqual(len(client.calls), 1)
        system = client.calls[0]["messages"][0].content
        self.assertIn("不得推断", system)
        self.assertIn("字段仅为 positive_prompt、style_notes", system)
        self.assertNotIn("negative_prompt", system)

    def test_identity_context_tells_llm_to_semantically_stage_the_character(self) -> None:
        service = CharacterPortraitPromptService()
        client = FakePromptClient()

        prompt = service.create_prompt(
            {
                "build": {"identity": "学者", "classes": {"学者": 2}},
                "presentation": {
                    "appearance": {"hair": "深色短发"},
                    "portrait": {"scene_mode": "identity_context"},
                },
            },
            model_profile="krea-lora",
            allow_creative_fill=True,
            llm_client=client,
            llm_model="fake",
        )

        system = client.calls[0]["messages"][0].content
        request_payload = json.loads(client.calls[0]["messages"][1].content)
        self.assertEqual(prompt.prompt_version, "portrait-prompt-v6")
        self.assertEqual(request_payload["scene_mode"], "identity_context")
        self.assertEqual(request_payload["brief"]["identity"], "学者")
        self.assertIn("语义推导一个普通、日常", system)
        self.assertIn("角色必须正在做事", system)
        self.assertIn("低细节、弱焦点", system)
        self.assertIn("单一分镜定案", system)
        self.assertIn("一个固定景别", system)
        self.assertIn("角色在画面中的", system)
        self.assertIn("分别说明每件道具的位置", system)
        self.assertIn("一个明确的主光源", system)
        self.assertIn("不得提供备选地点", system)
        self.assertIn("不得使用 or", system)
        self.assertIn("up to", system)
        self.assertNotIn("至多三个可见叙事意象", system)
        self.assertIn("景别只能选用一个明确术语", system)
        self.assertIn("必须与景别分开表述", system)
        self.assertIn("不要写 age-ambiguous", system)
        self.assertIn("不是必须入镜或手持的清单", system)
        self.assertIn("不要依赖单个词语机械分类", system)
        self.assertIn("工作状态、生活状态或战斗特写", system)
        self.assertIn("positive_prompt 应完全不提武器", system)
        self.assertIn("不得为了说明省略武器", system)
        self.assertIn("低调别在身侧", system)
        self.assertIn("职业工具和生活物件", system)
        self.assertNotIn("negative_prompt", system)
        self.assertNotIn("画面必须是单个原创角色", system)

    def test_clean_portrait_strips_scene_fields_and_keeps_single_full_body_contract(self) -> None:
        service = CharacterPortraitPromptService()
        client = FakePromptClient()

        prompt = service.create_prompt(
            {
                "build": {"identity": "商人"},
                "presentation": {
                    "appearance": {
                        "hair": "赤褐色长发",
                        "scene": "露天市集",
                        "activity": "与顾客讲价",
                    },
                    "portrait": {"scene_mode": "clean_portrait"},
                },
            },
            model_profile="anima",
            allow_creative_fill=True,
            llm_client=client,
            llm_model="fake",
        )

        system = client.calls[0]["messages"][0].content
        request_payload = json.loads(client.calls[0]["messages"][1].content)
        self.assertEqual(request_payload["scene_mode"], "clean_portrait")
        self.assertNotIn("scene", request_payload["brief"])
        self.assertNotIn("activity", request_payload["brief"])
        self.assertNotIn("scene", prompt.brief)
        self.assertIn("从头到脚的全身构图", system)
        self.assertIn("不添加其他人物或身份场景", system)
        self.assertIn("从 positive_prompt 中直接省略武器", system)

    def test_identity_context_deterministic_fallback_does_not_ban_background_people(self) -> None:
        service = CharacterPortraitPromptService()

        prompt = service.create_prompt(
            {
                "build": {"identity": "商人"},
                "presentation": {"portrait": {"scene_mode": "identity_context"}},
            },
            model_profile="anima",
        )

        self.assertIn("identity-revealing everyday environment", prompt.positive_prompt)
        self.assertIn("full-body shot", prompt.positive_prompt)
        self.assertIn("eye-level camera", prompt.positive_prompt)
        self.assertNotIn("full-body or three-quarter", prompt.positive_prompt)
        self.assertEqual(prompt.negative_prompt, "")

    def test_deterministic_fallback_omits_inventory_weapons_by_default(self) -> None:
        service = CharacterPortraitPromptService()

        prompt = service.create_prompt(
            {
                "build": {
                    "identity": "旅行商人",
                    "equipment": ["钢匕首", "旅行装束"],
                },
                "presentation": {
                    "appearance": {"weapon": "钢匕首"},
                    "portrait": {"scene_mode": "identity_context"},
                },
            },
            model_profile="krea2",
        )

        self.assertIn("role-appropriate tools", prompt.positive_prompt)
        self.assertNotIn("钢匕首", prompt.positive_prompt)
        self.assertNotIn("weapon:", prompt.positive_prompt)
        self.assertNotIn("carried equipment:", prompt.positive_prompt)

    def test_llm_returned_negative_prompt_is_ignored(self) -> None:
        service = CharacterPortraitPromptService()

        prompt = service.create_prompt(
            {
                "build": {"identity": "商人"},
                "presentation": {"portrait": {"scene_mode": "identity_context"}},
            },
            model_profile="krea-lora",
            llm_client=UnexpectedNegativePromptClient(),
            llm_model="fake",
        )

        self.assertEqual(prompt.negative_prompt, "")

    def test_bundled_turbo_profiles_default_to_empty_negative_prompt(self) -> None:
        service = CharacterPortraitPromptService()

        for profile in ("anima", "krea2", "krea_lora"):
            with self.subTest(profile=profile):
                self.assertEqual(service.default_negative_prompt(profile), "")

    def test_krea_lora_profile_is_canonical_and_uses_its_own_dimensions(self) -> None:
        service = CharacterPortraitPromptService()
        prompt = service.create_prompt(
            {"appearance": {"hair": "银白短发"}},
            model_profile="krea-lora",
        )
        config = ComfyUIConfig(krea_lora_width=1280, krea_lora_height=1832)

        self.assertEqual(prompt.model_profile, "krea_lora")
        self.assertIn("style LoRA", prompt.style_notes)
        self.assertEqual(config.dimensions(prompt.model_profile), (1280, 1832))

    def test_required_llm_rejects_missing_configuration(self) -> None:
        service = CharacterPortraitPromptService()

        with self.assertRaisesRegex(ValueError, "尚未配置"):
            service.create_prompt(
                {"appearance": {"hair": "银白短发"}},
                model_profile="anima",
                require_llm=True,
            )

    def test_required_llm_does_not_fall_back_on_provider_failure(self) -> None:
        service = CharacterPortraitPromptService()

        with self.assertRaisesRegex(ValueError, "LLM 整理"):
            service.create_prompt(
                {"appearance": {"hair": "银白短发"}},
                model_profile="anima",
                require_llm=True,
                llm_client=FailingPromptClient(),
                llm_model="fake",
            )

    def test_comfy_client_replaces_only_declared_placeholders(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            workflow_path = root / "anima.json"
            workflow_path.write_text(
                json.dumps(
                    {
                        "1": {
                            "inputs": {
                                "text": "{{POSITIVE_PROMPT}}",
                                "negative": "{{NEGATIVE_PROMPT}}",
                                "seed": "{{SEED}}",
                                "width": "{{WIDTH}}",
                                "height": "{{HEIGHT}}",
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            transport = FakeComfyTransport()
            config = ComfyUIConfig(
                enabled=True,
                anima_workflow=str(workflow_path),
                output_dir=str(root / "output"),
                poll_interval_seconds=0.01,
            )
            client = ComfyUIClient(config, transport=transport, sleeper=lambda _value: None)
            prompt = CharacterPortraitPromptService().create_prompt(
                {"appearance": {"hair": "银白短发"}},
                model_profile="anima",
            )

            result = client.generate(prompt, seed=42, filename_prefix="mira")

            inputs = transport.submitted["prompt"]["1"]["inputs"]
            self.assertEqual(inputs["seed"], 42)
            self.assertEqual(inputs["width"], 768)
            self.assertIn("银白短发", inputs["text"])
            self.assertEqual(inputs["negative"], "")
            self.assertEqual(Path(result.output_path).read_bytes(), b"fake-png")

    def test_comfy_client_recovers_newest_matching_history_image(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            transport = RecoveryComfyTransport()
            client = ComfyUIClient(
                ComfyUIConfig(enabled=True, output_dir=tempdir),
                transport=transport,
            )

            result = client.recover_latest(
                filename_prefix="card-123",
                model_profile="krea_lora",
            )

            self.assertIsNotNone(result)
            self.assertEqual(result.prompt_id, "newer")
            self.assertEqual(result.source_filename, "card-123_00002_.png")
            self.assertEqual(result.seed, 22)
            self.assertEqual(Path(result.output_path).read_bytes(), b"recovered-png")
            self.assertIn("filename=card-123_00002_.png", transport.view_url)

    def test_comfy_client_waits_when_matching_prompt_is_still_queued(self) -> None:
        transport = RecoveryComfyTransport(active=True)
        client = ComfyUIClient(ComfyUIConfig(enabled=True), transport=transport)

        result = client.recover_latest(
            filename_prefix="card-123",
            model_profile="krea_lora",
        )

        self.assertIsNone(result)
        self.assertFalse(transport.history_requested)

    def test_remote_comfy_requires_explicit_opt_in(self) -> None:
        config = ComfyUIConfig(
            enabled=True,
            base_url="http://192.168.1.20:8188",
            anima_workflow="missing.json",
        )
        client = ComfyUIClient(config, transport=FakeComfyTransport())
        prompt = CharacterPortraitPromptService().create_prompt(
            {"appearance": {"hair": "银白短发"}},
            model_profile="anima",
        )

        with self.assertRaisesRegex(ValueError, "本机"):
            client.generate(prompt)

    def test_job_manager_records_completion(self) -> None:
        jobs = PortraitJobManager()
        record = jobs.submit(lambda: {"output_path": "portrait.png"})

        for _ in range(100):
            current = jobs.get(record["job_id"])
            if current["status"] == "completed":
                break
        self.assertEqual(current["status"], "completed")
        self.assertEqual(current["result"]["output_path"], "portrait.png")


if __name__ == "__main__":
    unittest.main()
