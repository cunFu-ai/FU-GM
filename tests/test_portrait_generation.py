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
                "negative_prompt": "text, watermark",
                "style_notes": "clean anime illustration",
            }
        )


class FailingPromptClient:
    def create_chat_completion(self, **kwargs):
        raise RuntimeError("provider unavailable")


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
        self.assertEqual(len(client.calls), 1)
        self.assertIn("不得推断", client.calls[0]["messages"][0].content)

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
            self.assertEqual(Path(result.output_path).read_bytes(), b"fake-png")

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
