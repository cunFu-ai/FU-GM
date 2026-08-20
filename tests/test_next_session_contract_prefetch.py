from __future__ import annotations

import tempfile
import threading
import time
import unittest
from copy import deepcopy
from dataclasses import asdict
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

from fu_gm.gm_tool_contracts import GMToolExecutionContext
from fu_gm.http_server import FUGMHttpService
from fu_gm.models import (
    PreparedSessionContractCache,
    SceneType,
    SessionDramaticContract,
    SessionNPCRole,
    SessionPacingPlan,
)
from fu_gm.testing.kariba_fixture import seed_kariba_ready_campaign


class _FakeConcretizer:
    def __init__(
        self,
        contract: SessionDramaticContract,
        fingerprint: str,
    ) -> None:
        self.contract = deepcopy(contract)
        self.fingerprint = fingerprint
        self.model = "next-prefetch-model"
        self.last_error = ""
        self.last_gatekeeper_repair_status = "not_needed"
        self.reachability_reviewer = SimpleNamespace(
            model="next-review-model",
            last_status="reviewed",
            last_error="",
        )

    def export_cache_entry(self) -> dict[str, object]:
        return {
            "fingerprint": self.fingerprint,
            "contract": deepcopy(self.contract),
            "diagnostics": {
                "last_error": "",
                "last_gatekeeper_repair_status": "not_needed",
                "reachability_last_status": "reviewed",
            },
        }


class _FakeDetachedPacingManager:
    def __init__(
        self,
        contract: SessionDramaticContract,
        fingerprint: str,
        *,
        started: threading.Event | None = None,
        release: threading.Event | None = None,
    ) -> None:
        prepared = deepcopy(contract)
        prepared.preparation_fingerprint = fingerprint
        self.prepared = prepared
        self.started = started
        self.release = release
        self.refresh_calls: list[dict[str, object]] = []
        self.model_refresh_count = 0
        self.contract_planner = SimpleNamespace(
            concretizer=_FakeConcretizer(prepared, fingerprint)
        )

    def refresh_plan(self, **kwargs: object) -> SessionPacingPlan:
        self.refresh_calls.append(dict(kwargs))
        if bool(kwargs.get("allow_model_prep")):
            self.model_refresh_count += 1
            if self.started is not None:
                self.started.set()
            if self.release is not None and not self.release.wait(timeout=10.0):
                raise TimeoutError("test did not release next-session worker")
        session_number = int(kwargs.get("force_session_number") or 2)
        prepared = deepcopy(self.prepared)
        prepared.session_number = session_number
        return SessionPacingPlan(
            session_number=session_number,
            dramatic_contract=prepared,
        )


def _prepared_contract(fingerprint: str) -> SessionDramaticContract:
    return SessionDramaticContract(
        session_number=2,
        title="后台准备的第二场契约",
        location="卡里巴村旧水道",
        dramatic_question="英雄能否追上被转移的封印证物？",
        opening_disruption="旧水道的逆流突然裹来一枚监狱徽记。",
        signature_image="蓝色封印光沿着水面逆流而上。",
        important_npcs=[
            SessionNPCRole(
                name="水道引路人",
                public_role="熟悉旧水道的送信人",
                goal_now="在追兵抵达前找回失踪的同伴",
            )
        ],
        preparation_fingerprint=fingerprint,
        preparation_status="ready",
        preparation_source="next_session_prefetch",
    )


class NextSessionContractPrefetchTests(unittest.TestCase):
    campaign_id = "next-session-prefetch-test"
    old_session_id = "s1"
    next_session_id = "s2"
    channel_id = "group-1"

    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)

    def _service(self) -> FUGMHttpService:
        service = FUGMHttpService(
            data_root=self.tempdir.name,
            use_llm=False,
            adventure_opening_flow_mode="optimized",
        )
        self.addCleanup(
            service.adventure_opening_prefetcher._executor.shutdown,
            wait=True,
        )
        return service

    def _completed_runtime(
        self,
        service: FUGMHttpService,
        *,
        campaign_id: str | None = None,
    ):
        clean_campaign = campaign_id or self.campaign_id
        runtime = seed_kariba_ready_campaign(
            service,
            campaign_id=clean_campaign,
            session_id=self.old_session_id,
            channel_id=self.channel_id,
            skip_map_render=True,
        )
        readiness = service._adventure_readiness_snapshot(
            runtime,
            materialize_confirmed_characters=True,
        )
        self.assertTrue(readiness["ready"], readiness)
        state = runtime.app.story_arc_manager.state
        state.session_count = 1
        state.processed_session_ids = [self.old_session_id]
        state.current_pacing_plan.dramatic_contract.status = "completed"
        state.current_session_progress.closure_ready = True
        runtime.app.session_ledger.session_id = self.old_session_id
        runtime.app.session_ledger.active = False
        runtime.app.session_ledger.settled = True
        runtime.app.world_state.mark_player_present("测试玩家甲")
        service.session_gates.deactivate(
            clean_campaign,
            self.channel_id,
            self.old_session_id,
            reason="test_session_completed",
        )
        service._autosave_campaign(runtime, clean_campaign)
        return runtime

    def _authority_boundary(
        self,
        service: FUGMHttpService,
        runtime,
    ) -> dict[str, object]:
        state = runtime.app.story_arc_manager.state
        return {
            "gate": asdict(
                service.session_gates.get(
                    runtime.campaign_id,
                    self.channel_id,
                    self.old_session_id,
                )
            ),
            "ledger": deepcopy(runtime.app.session_ledger.to_snapshot()),
            "current_plan": deepcopy(state.current_pacing_plan),
            "contract_history": deepcopy(state.session_contract_history),
            "current_progress": deepcopy(state.current_session_progress),
            "npc_personas": deepcopy(runtime.app.world_state.npc_personas),
        }

    def _install_valid_real_envelope(
        self,
        service: FUGMHttpService,
        runtime,
    ) -> tuple[str, str]:
        prefetcher = service.adventure_opening_prefetcher
        with runtime.transaction_lock:
            detached = prefetcher._detached_manager(runtime)
            plan = detached.refresh_plan(
                conflict_active=False,
                force_session_number=2,
                allow_model_prep=False,
                deadline=None,
                register_session_npcs=False,
                preparation_source="next_fingerprint_test",
            )
            generation = str(
                plan.dramatic_contract.preparation_fingerprint or ""
            )
            self.assertTrue(generation)
            authority = prefetcher._next_authoritative_state_digest_locked(
                runtime
            )
            persistent = prefetcher._next_persistent_fingerprint(
                generation_fingerprint=generation,
                authority_digest=authority,
                target_session_number=2,
            )
            prepared = deepcopy(plan.dramatic_contract)
            prepared.title = "跨重启命中的第二场契约"
            prepared.preparation_status = "ready"
            prepared.important_npcs.append(
                SessionNPCRole(
                    name="跨重启的水道引路人",
                    public_role="旧水道送信人",
                    goal_now="把证物交给英雄",
                )
            )
            runtime.app.story_arc_manager.state.prepared_next_session_contract = (
                PreparedSessionContractCache(
                    schema_version=1,
                    fingerprint=persistent,
                    contract=prepared,
                    model="next-prefetch-model",
                    review_model="next-review-model",
                    quality_status="model_reviewed",
                    diagnostics={"last_error": ""},
                    prepared_at=datetime.now(timezone.utc).isoformat(),
                    source_state_version=999_999,
                )
            )
        service._autosave_campaign(runtime, runtime.campaign_id)
        return generation, persistent

    def test_worker_deduplicates_and_persists_only_private_candidate(self) -> None:
        service = self._service()
        runtime = self._completed_runtime(service)
        prefetcher = service.adventure_opening_prefetcher
        started = threading.Event()
        release = threading.Event()
        self.addCleanup(release.set)
        generation = "a" * 64
        detached = _FakeDetachedPacingManager(
            _prepared_contract(generation),
            generation,
            started=started,
            release=release,
        )
        before = self._authority_boundary(service, runtime)

        with patch.object(
            prefetcher,
            "_detached_manager",
            return_value=detached,
        ):
            first = prefetcher.schedule_next_session(
                campaign_id=self.campaign_id,
                source_session_id=self.old_session_id,
            )
            self.assertEqual(first["status"], "queued")
            self.assertTrue(started.wait(timeout=5.0))
            self.assertEqual(
                self._authority_boundary(service, runtime),
                before,
            )
            self.assertIsNone(
                runtime.app.story_arc_manager.state
                .prepared_next_session_contract
            )

            duplicate = prefetcher.schedule_next_session(
                campaign_id=self.campaign_id,
                source_session_id=self.old_session_id,
            )
            self.assertEqual(duplicate["status"], "running")
            self.assertTrue(duplicate["reused"])
            self.assertEqual(detached.model_refresh_count, 1)

            release.set()
            completed = prefetcher.wait_next_session(
                self.campaign_id,
                timeout_seconds=5.0,
            )

        self.assertEqual(completed["status"], "ready")
        self.assertEqual(completed["target_session_number"], 2)
        self.assertEqual(
            self._authority_boundary(service, runtime),
            before,
        )
        envelope = (
            runtime.app.story_arc_manager.state
            .prepared_next_session_contract
        )
        self.assertIsInstance(envelope, PreparedSessionContractCache)
        self.assertEqual(envelope.contract.session_number, 2)
        self.assertEqual(envelope.contract.title, "后台准备的第二场契约")
        self.assertNotIn("水道引路人", runtime.app.world_state.npc_personas)
        self.assertEqual(detached.model_refresh_count, 1)
        self.assertFalse(detached.refresh_calls[0]["register_session_npcs"])
        self.assertFalse(detached.refresh_calls[1]["register_session_npcs"])

        restored_service = self._service()
        restored = restored_service._runtime(self.campaign_id)
        restored_envelope = (
            restored.app.story_arc_manager.state
            .prepared_next_session_contract
        )
        self.assertTrue(restored.loaded_from_disk)
        self.assertIsInstance(
            restored_envelope,
            PreparedSessionContractCache,
        )
        self.assertIsInstance(
            restored_envelope.contract,
            SessionDramaticContract,
        )
        self.assertIsInstance(
            restored_envelope.contract.important_npcs[0],
            SessionNPCRole,
        )

    def test_worker_waits_for_logical_write_lease_before_model_work(self) -> None:
        service = self._service()
        runtime = self._completed_runtime(service)
        prefetcher = service.adventure_opening_prefetcher
        started = threading.Event()
        release = threading.Event()
        self.addCleanup(release.set)
        generation = "b" * 64
        detached = _FakeDetachedPacingManager(
            _prepared_contract(generation),
            generation,
            started=started,
            release=release,
        )

        with runtime.write_lease_condition:
            runtime.write_lease_owner = "outer-message-transaction"
        with patch.object(
            prefetcher,
            "_detached_manager",
            return_value=detached,
        ):
            prefetcher.schedule_next_session(
                campaign_id=self.campaign_id,
                source_session_id=self.old_session_id,
            )
            self.assertFalse(started.wait(timeout=0.15))
            with runtime.write_lease_condition:
                runtime.write_lease_owner = ""
                runtime.write_lease_condition.notify_all()
            self.assertTrue(started.wait(timeout=5.0))
            release.set()
            completed = prefetcher.wait_next_session(
                self.campaign_id,
                timeout_seconds=5.0,
            )

        self.assertEqual(completed["status"], "ready")
        self.assertEqual(detached.model_refresh_count, 1)

    def test_authority_change_while_model_runs_invalidates_without_state_counter(
        self,
    ) -> None:
        service = self._service()
        runtime = self._completed_runtime(service)
        prefetcher = service.adventure_opening_prefetcher
        started = threading.Event()
        release = threading.Event()
        self.addCleanup(release.set)
        generation = "c" * 64
        detached = _FakeDetachedPacingManager(
            _prepared_contract(generation),
            generation,
            started=started,
            release=release,
        )
        state_version = runtime.state_version

        with patch.object(
            prefetcher,
            "_detached_manager",
            return_value=detached,
        ), patch.object(service, "_autosave_campaign") as autosave:
            prefetcher.schedule_next_session(
                campaign_id=self.campaign_id,
                source_session_id=self.old_session_id,
            )
            self.assertTrue(started.wait(timeout=5.0))
            runtime.app.world_state.world_profile.magic_tech_role += (
                " 新增事实：旧水道内魔法器械会失灵。"
            )
            self.assertEqual(runtime.state_version, state_version)
            release.set()
            completed = prefetcher.wait_next_session(
                self.campaign_id,
                timeout_seconds=5.0,
            )

        self.assertEqual(completed["status"], "stale")
        self.assertEqual(
            completed["reason"],
            "authoritative_inputs_changed",
        )
        autosave.assert_not_called()
        self.assertIsNone(
            runtime.app.story_arc_manager.state
            .prepared_next_session_contract
        )

    def test_persisted_cache_invalidates_all_later_session_authority_axes(
        self,
    ) -> None:
        def change_safety(runtime) -> None:
            runtime.app.world_state.world_profile.safety_veils.append(
                "儿童受伤只作远景处理"
            )

        def change_world(runtime) -> None:
            runtime.app.world_state.subject_facts.setdefault(
                "旧水道",
                [],
            ).append("封印潮汐会让魔法器械暂时失灵")

        def change_character(runtime) -> None:
            character = runtime.app.character_manager.all()[0]
            character.identity = (
                str(character.identity or "")
                + "；现在背负上一场留下的封印灼痕"
            )

        def change_participant(runtime) -> None:
            runtime.app.session_zero_manager.state.participants[0].role = (
                "玩家兼桌面记录员"
            )

        cases = (
            ("safety_boundary", change_safety),
            ("world_fact", change_world),
            ("character", change_character),
            ("participant", change_participant),
        )
        for index, (label, mutate) in enumerate(cases):
            with self.subTest(axis=label):
                service = self._service()
                campaign_id = f"{self.campaign_id}-{index}"
                runtime = self._completed_runtime(
                    service,
                    campaign_id=campaign_id,
                )
                _, old_fingerprint = self._install_valid_real_envelope(
                    service,
                    runtime,
                )
                state_version = runtime.state_version

                mutate(runtime)
                self.assertEqual(runtime.state_version, state_version)
                result = (
                    service.adventure_opening_prefetcher
                    .prime_next_session_for_consumption(
                        runtime,
                        campaign_id=campaign_id,
                        wait_timeout_seconds=0.0,
                    )
                )

                self.assertEqual(result["status"], "stale")
                self.assertEqual(
                    result["reason"],
                    "authoritative_inputs_changed",
                )
                self.assertEqual(
                    result["expected"],
                    old_fingerprint[:12],
                )
                self.assertNotEqual(
                    result["actual"],
                    old_fingerprint[:12],
                )

    def test_prompt_schema_revision_invalidates_later_session_cache(self) -> None:
        service = self._service()
        runtime = self._completed_runtime(service)
        _, old_fingerprint = self._install_valid_real_envelope(
            service,
            runtime,
        )

        with patch(
            "fu_gm.components.session_prep_concretizer."
            "SessionPrepConcretizer._CACHE_SCHEMA",
            "session-prep-next-revision-test",
        ):
            result = (
                service.adventure_opening_prefetcher
                .prime_next_session_for_consumption(
                    runtime,
                    campaign_id=self.campaign_id,
                    wait_timeout_seconds=0.0,
                )
            )

        self.assertEqual(result["status"], "stale")
        self.assertEqual(result["expected"], old_fingerprint[:12])
        self.assertNotEqual(result["actual"], old_fingerprint[:12])

    def test_restart_validates_then_formal_start_consumes_cached_contract(
        self,
    ) -> None:
        service = self._service()
        runtime = self._completed_runtime(service)
        _, persistent = self._install_valid_real_envelope(service, runtime)
        self.assertNotIn(
            "跨重启的水道引路人",
            runtime.app.world_state.npc_personas,
        )

        restored_service = self._service()
        restored = restored_service._runtime(self.campaign_id)
        loaded = (
            restored.app.story_arc_manager.state
            .prepared_next_session_contract
        )
        self.assertTrue(restored.loaded_from_disk)
        self.assertNotEqual(restored.state_version, 999_999)
        self.assertIsInstance(loaded, PreparedSessionContractCache)
        self.assertEqual(loaded.fingerprint, persistent)
        before = self._authority_boundary(restored_service, restored)

        prime = (
            restored_service.adventure_opening_prefetcher
            .prime_next_session_for_consumption(
                restored,
                campaign_id=self.campaign_id,
                wait_timeout_seconds=0.0,
            )
        )

        self.assertEqual(prime["status"], "persistent_hit")
        self.assertFalse(prime["source_state_version_match"])
        self.assertEqual(
            self._authority_boundary(restored_service, restored),
            before,
        )
        self.assertNotIn(
            "跨重启的水道引路人",
            restored.app.world_state.npc_personas,
        )

        context = GMToolExecutionContext(
            campaign_id=self.campaign_id,
            session_id=self.next_session_id,
            channel_id=self.channel_id,
            speaker="测试玩家甲",
            gate_status="inactive",
            directly_addressed=True,
            metadata={
                "current_message": "开始下一场。",
                "_gm_agent_deadline_monotonic": time.monotonic() + 120.0,
            },
        )
        with patch.object(
            restored_service,
            "_handle_gate_signal",
            return_value={"blocked": False},
        ):
            receipt = restored_service.gm_runtime_tools.start_session(
                context,
                {
                    "phase": "adventure",
                    "reason": "玩家明确开始下一场",
                    "evidence": "开始下一场",
                },
            )

        self.assertTrue(receipt.ok, receipt.message)
        cache = receipt.result["session_prep_cache"]
        self.assertEqual(cache["status"], "persistent_hit")
        self.assertTrue(cache["consumed"])
        adopted = (
            restored.app.story_arc_manager.state.current_pacing_plan
            .dramatic_contract
        )
        self.assertEqual(adopted.session_number, 2)
        self.assertEqual(adopted.title, "跨重启命中的第二场契约")
        self.assertIn(
            "跨重启的水道引路人",
            restored.app.world_state.npc_personas,
        )
        self.assertIsNone(
            restored.app.story_arc_manager.state
            .prepared_next_session_contract
        )

    def test_later_start_cache_miss_never_generates_on_foreground(self) -> None:
        service = self._service()
        runtime = self._completed_runtime(service)
        concretizer = (
            runtime.app.campaign_pacing_manager.contract_planner.concretizer
        )
        context = GMToolExecutionContext(
            campaign_id=self.campaign_id,
            session_id=self.next_session_id,
            channel_id=self.channel_id,
            speaker="测试玩家甲",
            gate_status="inactive",
            directly_addressed=True,
            metadata={
                "current_message": "开始下一场。",
                "_gm_agent_deadline_monotonic": time.monotonic() + 120.0,
            },
        )

        with patch.object(
            service,
            "_handle_gate_signal",
            return_value={"blocked": False},
        ), patch.object(
            concretizer,
            "concretize",
            wraps=concretizer.concretize,
        ) as concretize:
            receipt = service.gm_runtime_tools.start_session(
                context,
                {
                    "phase": "adventure",
                    "reason": "玩家明确开始下一场",
                    "evidence": "开始下一场",
                },
            )

        self.assertTrue(receipt.ok, receipt.message)
        self.assertEqual(
            receipt.result["session_prep_cache"]["status"],
            "miss",
        )
        self.assertFalse(receipt.result["session_prep_cache"]["consumed"])
        self.assertFalse(concretize.call_args.kwargs["allow_model"])

    def test_next_contract_is_consumed_when_new_session_resumes_old_scene(
        self,
    ) -> None:
        service = self._service()
        runtime = self._completed_runtime(service)
        scene = runtime.app.start_scene(
            "未完的旧水道追逐",
            SceneType.STANDARD,
            location="卡里巴村旧水道",
            participants=["夏尔"],
            objective="追上被转移的封印证物",
        )
        self._install_valid_real_envelope(service, runtime)
        service._autosave_campaign(runtime, self.campaign_id)

        restored_service = self._service()
        restored = restored_service._runtime(self.campaign_id)
        context = GMToolExecutionContext(
            campaign_id=self.campaign_id,
            session_id=self.next_session_id,
            channel_id=self.channel_id,
            speaker="测试玩家甲",
            gate_status="inactive",
            directly_addressed=True,
            metadata={
                "current_message": "继续旧水道这一幕，开始下一场。",
                "_gm_agent_deadline_monotonic": time.monotonic() + 120.0,
            },
        )

        with patch.object(
            restored_service,
            "_handle_gate_signal",
            return_value={"blocked": False},
        ):
            receipt = restored_service.gm_runtime_tools.start_session(
                context,
                {
                    "phase": "adventure",
                    "reason": "玩家明确继续现场并开始下一场",
                    "evidence": "开始下一场",
                },
            )

        self.assertTrue(receipt.ok, receipt.message)
        self.assertTrue(receipt.result["adventure_resumed"])
        self.assertEqual(
            receipt.result["resumed_scene"]["scene_id"],
            scene.scene_id,
        )
        self.assertTrue(receipt.result["session_prep_cache"]["consumed"])
        self.assertEqual(
            restored.app.story_arc_manager.state.current_pacing_plan
            .dramatic_contract.title,
            "跨重启命中的第二场契约",
        )
        self.assertIsNone(
            restored.app.story_arc_manager.state
            .prepared_next_session_contract
        )

    def test_authoritative_end_commit_schedules_next_contract_after_lock(self) -> None:
        service = self._service()
        runtime = service._runtime(self.campaign_id)
        service.session_gates.activate(
            self.campaign_id,
            self.channel_id,
            self.old_session_id,
            status="adventure",
        )
        runtime.app.start_session_tracking(self.old_session_id)
        observed: dict[str, object] = {}

        def capture_schedule(**kwargs: object) -> dict[str, object]:
            observed["kwargs"] = dict(kwargs)
            observed["session_count"] = (
                runtime.app.story_arc_manager.state.session_count
            )
            observed["ledger_active"] = runtime.app.session_ledger.active
            observed["gate_status"] = service.session_gates.get(
                self.campaign_id,
                self.channel_id,
                self.old_session_id,
            ).status
            acquired = runtime.transaction_lock.acquire(blocking=False)
            observed["outer_lock_released"] = acquired
            if acquired:
                runtime.transaction_lock.release()
            return {"status": "queued", "reused": False}

        with patch.object(
            service.adventure_opening_prefetcher,
            "schedule_next_session",
            side_effect=capture_schedule,
        ) as schedule, patch.object(
            service.adventure_opening_prefetcher,
            "model_available",
            return_value=True,
        ):
            result = service._end_session(
                {
                    "campaign_id": self.campaign_id,
                    "session_id": self.old_session_id,
                    "channel_id": self.channel_id,
                }
            )

        self.assertTrue(result["ok"])
        self.assertEqual(
            result["next_session_contract_prefetch"]["status"],
            "queued",
        )
        schedule.assert_called_once_with(
            campaign_id=self.campaign_id,
            source_session_id=self.old_session_id,
        )
        self.assertEqual(observed["session_count"], 1)
        self.assertFalse(observed["ledger_active"])
        self.assertEqual(observed["gate_status"], "inactive")
        self.assertTrue(observed["outer_lock_released"])


if __name__ == "__main__":
    unittest.main()
