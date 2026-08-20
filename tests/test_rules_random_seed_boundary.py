from __future__ import annotations

from unittest.mock import patch

from fu_gm.app_factory import build_app as real_build_app
from fu_gm.http_server import FUGMHttpService


def _capture_runtime_seed(tmp_path, configured_seed):
    captured: list[int | None] = []

    def build_with_spy(
        *,
        use_llm,
        seed,
        gm_style_prompt,
        deepseek_roleplay_mode,
        test_llm_bundle=None,
    ):
        captured.append(seed)
        return real_build_app(
            use_llm=False,
            seed=seed,
            gm_style_prompt=gm_style_prompt,
            deepseek_roleplay_mode=deepseek_roleplay_mode,
            test_llm_bundle=test_llm_bundle,
        )

    with patch("fu_gm.http_server.build_app", side_effect=build_with_spy):
        service = FUGMHttpService(
            data_root=tmp_path,
            use_llm=False,
            rules_seed=configured_seed,
        )
        service._runtime("seed-boundary")
    return captured


def test_production_service_does_not_use_a_fixed_rules_seed(tmp_path):
    assert _capture_runtime_seed(tmp_path, None) == [None]


def test_replay_harness_can_request_reproducible_rules_seed(tmp_path):
    assert _capture_runtime_seed(tmp_path, 0) == [0]


def test_seed_zero_still_reproduces_the_reference_d8_pair():
    app = real_build_app(use_llm=False, seed=0)
    rules = app.interceptor.rules_engine

    assert [rules.roll_die(8), rules.roll_die(8)] == [7, 7]
