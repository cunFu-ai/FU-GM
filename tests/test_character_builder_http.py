from __future__ import annotations

import http.client
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from fu_gm.http_server import FUGMHttpService, make_server


def valid_build() -> dict[str, object]:
    return {
        "player_name": "阿凛",
        "hero_name": "米菈",
        "identity": "逃离财团实验室的魔导技师",
        "theme": "自由",
        "origin": "永雨工业城下层",
        "classes": {"造物使": 2, "御魂使": 2, "守护者": 1},
        "attributes": {"DEX": 8, "INS": 8, "MIG": 8, "WLP": 8},
        "bonds": [{"target": "永雨工业城下层", "emotions": ["信赖"]}],
        "skills": {"便携装置": 1, "秘密配方": 1, "灵魂魔法": 2, "保镖": 1},
        "skill_options": {"便携装置": ["魔导装置"]},
        "spells": ["治愈", "护盾"],
        "bound_arcana": [],
        "abilities": [],
        "equipment": ["钢匕首", "符文盾", "旅行装束"],
        "equipment_slots": {},
        "notes": ["她知道辉钢财团的地下能源管线。"],
        "fate_roll": [2, 5],
    }


class CharacterBuilderHttpTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.service = FUGMHttpService(data_root=self.tempdir.name, use_llm=False)

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_catalog_preview_and_prompt_are_available_offline(self) -> None:
        status, catalog = self.service.handle(
            "GET", "/v1/character-builder/catalog"
        )
        self.assertEqual(status, 200)
        self.assertEqual(len(catalog["classes"]), 15)
        self.assertEqual(
            [profile["id"] for profile in catalog["portrait_profiles"]],
            ["anima", "krea2", "krea_lora"],
        )

        status, preview = self.service.handle(
            "POST",
            "/v1/character-builder/preview",
            {"build": valid_build()},
        )
        self.assertEqual(status, 200)
        self.assertTrue(preview["valid"])
        self.assertEqual(preview["derived"]["max_hp"], 50)

        status, prompt = self.service.handle(
            "POST",
            "/v1/portraits/prompt",
            {
                "model_profile": "anima",
                "require_llm": False,
                "build": valid_build(),
                "presentation": {
                    "appearance": {"hair": "银白短发", "outfit": "绿色旅行外套"}
                },
            },
        )
        self.assertEqual(status, 200)
        self.assertEqual(prompt["prompt"]["source"], "deterministic")
        self.assertIn("hair: 银白短发", prompt["prompt"]["positive_prompt"])

    def test_card_build_import_list_and_export_round_trip(self) -> None:
        payload = {
            "build": valid_build(),
            "presentation": {
                "appearance": {"hair": "银白短发"},
                "portrait": {"model_profile": "anima", "seed": 73},
            },
            "extensions": {"cunfu.homebrew": {"features": ["巨岩扩展内容"]}},
        }
        status, built = self.service.handle("POST", "/v1/character-cards/build", payload)
        self.assertEqual(status, 200)
        card = built["card"]

        status, text_export = self.service.handle(
            "POST",
            "/v1/character-cards/text",
            {"card": card},
        )
        self.assertEqual(status, 200)
        self.assertIn("角色名：米菈", text_export["text"])
        self.assertIn("- 治愈术", text_export["text"])
        self.assertNotIn("银白短发", text_export["text"])

        status, imported = self.service.handle(
            "POST",
            "/v1/character-cards/import",
            {"card": card, "conflict": "reject"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(imported["character"]["name"], "米菈")

        status, listing = self.service.handle(
            "GET", "/v1/character-cards"
        )
        self.assertEqual(status, 200)
        self.assertEqual([item["name"] for item in listing["characters"]], ["米菈"])

        status, exported = self.service.handle(
            "GET",
            "/v1/character-cards/export?hero_name=米菈",
        )
        self.assertEqual(status, 200)
        self.assertEqual(exported["card"]["card"]["id"], card["card"]["id"])
        self.assertEqual(
            exported["card"]["extensions"],
            {"cunfu.homebrew": {"features": ["巨岩扩展内容"]}},
        )
        self.assertEqual("fabula-ultima.character-card", exported["card"]["$schema"])
        self.assertEqual("standalone_roster", listing["storage"])
        self.assertEqual({}, self.service.runtimes)
        self.assertTrue(
            (Path(self.tempdir.name) / "character-workshop" / "roster.json").is_file()
        )

        status, copied = self.service.handle(
            "POST",
            "/v1/character-cards/import",
            {"card": card, "conflict": "copy"},
        )
        self.assertEqual(status, 200)
        self.assertEqual("米菈（副本）", copied["character"]["name"])
        self.assertNotEqual(card["card"]["id"], copied["card"]["card"]["id"])
        self.assertEqual(card["extensions"], copied["card"]["extensions"])

    def test_portrait_generation_fails_closed_when_comfyui_is_disabled(self) -> None:
        with patch.dict("os.environ", {"FU_GM_COMFYUI_ENABLED": "0"}):
            status, response = self.service.handle(
                "POST",
                "/v1/portraits/generate",
                {
                    "model_profile": "anima",
                    "require_llm": False,
                    "prompt": {
                        "model_profile": "anima",
                        "positive_prompt": "solo JRPG hero",
                        "negative_prompt": "text, watermark",
                    },
                },
            )
        self.assertEqual(status, 503)
        self.assertFalse(response["ok"])

    def test_portable_mode_disables_portrait_prompt_and_generation(self) -> None:
        with patch.dict(
            "os.environ",
            {
                "FU_GM_DISTRIBUTION_MODE": "portable",
                "FU_GM_PORTRAIT_FEATURE_ENABLED": "0",
            },
        ):
            status, catalog = self.service.handle(
                "GET", "/v1/character-builder/catalog"
            )
            prompt_status, prompt = self.service.handle(
                "POST",
                "/v1/portraits/prompt",
                {"model_profile": "anima", "require_llm": False},
            )
            generation_status, generation = self.service.handle(
                "POST",
                "/v1/portraits/generate",
                {"model_profile": "anima", "require_llm": False},
            )

        self.assertEqual(status, 200)
        self.assertEqual(catalog["capabilities"]["distribution_mode"], "portable")
        self.assertFalse(catalog["capabilities"]["portrait_prompt"])
        self.assertFalse(catalog["capabilities"]["portrait_generation"])
        self.assertEqual(prompt_status, 403)
        self.assertEqual(prompt["code"], "portrait_feature_disabled")
        self.assertEqual(generation_status, 403)
        self.assertEqual(generation["code"], "portrait_feature_disabled")

    def test_portrait_file_can_be_reopened_by_safe_filename(self) -> None:
        output_root = Path(self.tempdir.name) / "portraits"
        output_root.mkdir()
        portrait = output_root / "mira.png"
        portrait.write_bytes(b"portrait-bytes")

        with patch.dict(
            "os.environ",
            {"FU_GM_COMFYUI_OUTPUT_DIR": str(output_root)},
        ):
            status, body, content_type = self.service.character_builder.portrait_file(
                filename="mira.png"
            )
            denied, _, _ = self.service.character_builder.portrait_file(
                filename="../secret.png"
            )

        self.assertEqual(status, 200)
        self.assertEqual(body, b"portrait-bytes")
        self.assertEqual(content_type, "image/png")
        self.assertEqual(denied, 403)


class CharacterBuilderStaticFileTests(unittest.TestCase):
    def test_page_and_assets_are_served_with_security_headers(self) -> None:
        with tempfile.TemporaryDirectory() as data_root:
            service = FUGMHttpService(data_root=data_root, use_llm=False)
            server = make_server("127.0.0.1", 0, service=service)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                connection = http.client.HTTPConnection(*server.server_address, timeout=3)
                connection.request("GET", "/characters")
                response = connection.getresponse()
                body = response.read().decode("utf-8")
                headers = {key.lower(): value for key, value in response.getheaders()}
                connection.close()

                self.assertEqual(response.status, 200)
                self.assertIn("最终物语角色工房", body)
                self.assertIn("Fabula Ultima", body)
                self.assertIn("最终物语中的英雄", body)
                self.assertNotIn('id="campaignId"', body)
                self.assertNotIn("FINAL STORY", body)
                self.assertIn('id="portraitViewer"', body)
                self.assertIn('id="portraitViewerImage"', body)
                self.assertIn('id="settingsButton"', body)
                self.assertIn('id="settingsComfyPort"', body)
                self.assertIn('id="settingsLlmApiKey"', body)
                self.assertTrue(headers["content-type"].startswith("text/html"))
                self.assertEqual(headers["x-content-type-options"], "nosniff")

                connection = http.client.HTTPConnection(*server.server_address, timeout=3)
                connection.request("GET", "/characters/portrait-placeholder.webp")
                response = connection.getresponse()
                image = response.read()
                connection.close()
                self.assertEqual(response.status, 200)
                self.assertEqual(response.getheader("Content-Type"), "image/webp")
                self.assertGreater(len(image), 10_000)

                connection = http.client.HTTPConnection(*server.server_address, timeout=3)
                connection.request("GET", "/characters/app.js")
                response = connection.getresponse()
                script = response.read().decode("utf-8")
                connection.close()
                self.assertEqual(response.status, 200)
                self.assertIn("result.job.job_id", script)
                self.assertIn("exportActiveDraftText", script)
                self.assertIn("setEquipmentQuantity", script)
                self.assertIn("if (draft.build.spells.length > 0)", script)
                self.assertIn('node("strong", { text: `Lv${rank}` })', script)
                self.assertIn('node("strong", { text: "Lv1" })', script)
                self.assertNotIn('rank > 1 ? `SL ${rank}` : "习得"', script)
                self.assertGreaterEqual(script.count("enablePortraitViewer("), 3)
                self.assertIn('.replace(/\\bSL\\b/gi, "技能等级")', script)
                self.assertIn("function portraitFeatureEnabled()", script)
                self.assertIn("/v1/workshop/settings/test-${kind}", script)
                self.assertIn('runSettingsTest("comfyui")', script)
                self.assertIn('runSettingsTest("llm")', script)
                self.assertNotIn("localStorage.setItem(\"api_key", script)
                self.assertIn('label: "Krea 2 + LoRA"', script)
                self.assertIn("本地版已跳过自动立绘", script)
                self.assertNotIn("campaign_id", script)
                self.assertNotIn("写入 FU-GM", script)
                self.assertNotIn("导入 FU-GM", script)
                self.assertIn("保存到本地名册", script)
                self.assertIn("FABULA ULTIMA", script)
                self.assertNotIn(
                    'reviewLine("法术", draft.build.spells.join("、") || "无")',
                    script,
                )
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
