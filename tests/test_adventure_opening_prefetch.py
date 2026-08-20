from __future__ import annotations

import json
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
    ChapterPackage,
    PreparedSessionContractCache,
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
        *,
        reviewer_status: str = "reviewed",
        reviewer_error: str = "",
    ) -> None:
        self.contract = deepcopy(contract)
        self.fingerprint = fingerprint
        self.model = "prefetch-model"
        self.reachability_reviewer = SimpleNamespace(
            model="review-model",
            last_status=reviewer_status,
            last_error=reviewer_error,
        )
        self.last_error = ""

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
        reviewer_status: str = "reviewed",
        reviewer_error: str = "",
    ) -> None:
        prepared = deepcopy(contract)
        prepared.preparation_fingerprint = fingerprint
        self.prepared = prepared
        self.started = started
        self.release = release
        self.refresh_calls: list[dict[str, object]] = []
        self.model_refresh_count = 0
        self.contract_planner = SimpleNamespace(
            concretizer=_FakeConcretizer(
                prepared,
                fingerprint,
                reviewer_status=reviewer_status,
                reviewer_error=reviewer_error,
            )
        )

    def refresh_plan(self, **kwargs: object) -> SessionPacingPlan:
        self.refresh_calls.append(dict(kwargs))
        if bool(kwargs.get("allow_model_prep")):
            self.model_refresh_count += 1
            if self.started is not None:
                self.started.set()
            if self.release is not None and not self.release.wait(timeout=10.0):
                raise TimeoutError("test did not release the prefetch worker")
        return SessionPacingPlan(
            session_number=1,
            dramatic_contract=deepcopy(self.prepared),
        )


def _prepared_contract(fingerprint: str) -> SessionDramaticContract:
    return SessionDramaticContract(
        session_number=1,
        title="后台准备的第一章契约",
        location="卡里巴村监狱",
        dramatic_question="两名英雄能否趁封印异常逃出监狱？",
        opening_disruption="地下封印突然发出逆向脉动。",
        signature_image="雨水沿牢门符文向上流。",
        important_npcs=[
            SessionNPCRole(
                name="后台守门人",
                public_role="卡里巴监狱夜巡长",
                goal_now="在封印失控前守住证物室",
            )
        ],
        preparation_fingerprint=fingerprint,
        preparation_status="ready",
        preparation_source="prefetch",
    )


class AdventureOpeningPrefetchTests(unittest.TestCase):
    campaign_id = "opening-prefetch-test"
    session_id = "s1"
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

    def _seed(self, service: FUGMHttpService):
        return seed_kariba_ready_campaign(
            service,
            campaign_id=self.campaign_id,
            session_id=self.session_id,
            channel_id=self.channel_id,
            skip_map_render=True,
        )

    def _authoritative_boundary(
        self,
        service: FUGMHttpService,
        runtime,
    ) -> dict[str, object]:
        arc = runtime.app.story_arc_manager.state
        return {
            "gate": asdict(
                service.session_gates.get(
                    self.campaign_id,
                    self.channel_id,
                    self.session_id,
                )
            ),
            "ledger": deepcopy(runtime.app.session_ledger.to_snapshot()),
            "current_scene": deepcopy(runtime.app.scene_manager.current_scene),
            "scene_history": deepcopy(runtime.app.scene_manager.history),
            "characters": deepcopy(runtime.app.character_manager.all()),
            "npc_personas": deepcopy(runtime.app.world_state.npc_personas),
            "current_plan": deepcopy(arc.current_pacing_plan),
            "contract_history": deepcopy(arc.session_contract_history),
            "session_progress": deepcopy(arc.current_session_progress),
        }

    @staticmethod
    def _persistent_fingerprint(prefetcher, runtime, generation: str) -> str:
        with runtime.transaction_lock:
            authority_digest = (
                prefetcher._authoritative_state_digest_locked(runtime)
            )
        return prefetcher._persistent_fingerprint(
            generation_fingerprint=generation,
            authority_digest=authority_digest,
        )

    def _install_valid_envelope(self, service, runtime) -> tuple[str, str]:
        prefetcher = service.adventure_opening_prefetcher
        detached = prefetcher._detached_manager(runtime)
        plan = detached.refresh_plan(
            conflict_active=False,
            allow_model_prep=False,
            deadline=None,
            register_session_npcs=False,
            preparation_source="fingerprint_test",
        )
        generation_fingerprint = str(
            plan.dramatic_contract.preparation_fingerprint or ""
        )
        self.assertTrue(generation_fingerprint)
        persistent_fingerprint = self._persistent_fingerprint(
            prefetcher,
            runtime,
            generation_fingerprint,
        )
        contract = deepcopy(plan.dramatic_contract)
        contract.preparation_status = "ready"
        runtime.app.session_zero_manager.state.prepared_chapter_one_session = (
            PreparedSessionContractCache(
                schema_version=3,
                fingerprint=persistent_fingerprint,
                contract=contract,
                model="prefetch-model",
                review_model="review-model",
                quality_status="model_reviewed",
                diagnostics={"last_error": ""},
                prepared_at=datetime.now(timezone.utc).isoformat(),
                source_state_version=runtime.state_version,
            )
        )
        return generation_fingerprint, persistent_fingerprint

    def test_authoritative_fingerprint_invalidates_every_chapter_one_input_axis(
        self,
    ) -> None:
        def change_safety(runtime) -> None:
            runtime.app.session_zero_manager.state.world.safety_veils.append(
                "儿童受伤只作远景处理"
            )

        def change_world(runtime) -> None:
            runtime.app.session_zero_manager.state.world.magic_tech_role += (
                " 魔法器械不能在暴雨中启动。"
            )

        def change_first_act(runtime) -> None:
            runtime.app.session_zero_manager.state.world.selected_first_act_summary += (
                " 开局牢房改在监狱西翼。"
            )

        def change_confirmed_character(runtime) -> None:
            draft = (
                runtime.app.session_zero_manager.state.world.hero_drafts[
                    "测试玩家甲"
                ]
            )
            draft.identity = "离家出走、畏惧封印光芒的猫耳秘宝猎人"

        def change_participant(runtime) -> None:
            runtime.app.session_zero_manager.state.participants[0].role = (
                "玩家兼桌面记录员"
            )

        def change_chapter_package(runtime) -> None:
            runtime.app.world_state.register_chapter_package(
                ChapterPackage(
                    chapter_title="迟到的权威章节包",
                    synopsis="这个章节骨架在旧场次准备生成后才被确认。",
                    status="ready",
                )
            )

        cases = (
            ("safety_boundary", change_safety),
            ("world_field", change_world),
            ("first_act", change_first_act),
            ("confirmed_character", change_confirmed_character),
            ("participant_roster", change_participant),
            ("chapter_package", change_chapter_package),
        )
        for index, (label, mutate) in enumerate(cases):
            with self.subTest(axis=label):
                service = self._service()
                campaign_id = f"{self.campaign_id}-{index}"
                runtime = seed_kariba_ready_campaign(
                    service,
                    campaign_id=campaign_id,
                    session_id=self.session_id,
                    channel_id=self.channel_id,
                    skip_map_render=True,
                )
                _, old_fingerprint = self._install_valid_envelope(
                    service,
                    runtime,
                )
                old_state_version = runtime.state_version

                # Deliberately leave the process-local counter untouched. The
                # durable semantic fingerprint must detect the change itself.
                mutate(runtime)
                self.assertEqual(runtime.state_version, old_state_version)
                result = service.adventure_opening_prefetcher.prime_for_consumption(
                    runtime,
                    campaign_id=campaign_id,
                    session_id=self.session_id,
                    channel_id=self.channel_id,
                    wait_timeout_seconds=0.0,
                )

                self.assertEqual(result["status"], "stale")
                self.assertEqual(result["expected"], old_fingerprint[:12])
                self.assertNotEqual(result["actual"], old_fingerprint[:12])
                self.assertIsNone(
                    runtime.app.session_zero_manager.state
                    .prepared_chapter_one_session
                )

    def test_preparation_schema_revision_invalidates_persisted_envelope(
        self,
    ) -> None:
        service = self._service()
        runtime = self._seed(service)
        _, old_fingerprint = self._install_valid_envelope(service, runtime)

        with patch(
            "fu_gm.components.session_prep_concretizer."
            "SessionPrepConcretizer._CACHE_SCHEMA",
            "session-prep-test-next-revision",
        ):
            result = service.adventure_opening_prefetcher.prime_for_consumption(
                runtime,
                campaign_id=self.campaign_id,
                session_id=self.session_id,
                channel_id=self.channel_id,
                wait_timeout_seconds=0.0,
            )

        self.assertEqual(result["status"], "stale")
        self.assertEqual(result["expected"], old_fingerprint[:12])
        self.assertNotEqual(result["actual"], old_fingerprint[:12])

    def test_prefetch_deduplicates_and_only_persists_a_private_envelope(self) -> None:
        service = self._service()
        runtime = self._seed(service)
        prefetcher = service.adventure_opening_prefetcher
        started = threading.Event()
        release = threading.Event()
        self.addCleanup(release.set)
        fingerprint = "a" * 64
        detached = _FakeDetachedPacingManager(
            _prepared_contract(fingerprint),
            fingerprint,
            started=started,
            release=release,
        )
        before = self._authoritative_boundary(service, runtime)

        with patch.object(
            prefetcher,
            "_detached_manager",
            return_value=detached,
        ):
            first = prefetcher.schedule(
                campaign_id=self.campaign_id,
                session_id=self.session_id,
                channel_id=self.channel_id,
            )
            self.assertEqual(first["status"], "queued")
            self.assertTrue(started.wait(timeout=5.0))
            self.assertEqual(
                self._authoritative_boundary(service, runtime),
                before,
            )
            self.assertIsNone(
                runtime.app.session_zero_manager.state
                .prepared_chapter_one_session
            )

            duplicate = prefetcher.schedule(
                campaign_id=self.campaign_id,
                session_id=self.session_id,
                channel_id=self.channel_id,
            )
            self.assertEqual(duplicate["status"], "running")
            self.assertTrue(duplicate["reused"])
            self.assertEqual(detached.model_refresh_count, 1)

            release.set()
            completed = prefetcher.wait(
                self.campaign_id,
                timeout_seconds=5.0,
            )

        self.assertEqual(completed["status"], "ready")
        self.assertEqual(
            self._authoritative_boundary(service, runtime),
            before,
        )
        envelope = (
            runtime.app.session_zero_manager.state
            .prepared_chapter_one_session
        )
        self.assertIsInstance(envelope, PreparedSessionContractCache)
        persistent_fingerprint = self._persistent_fingerprint(
            prefetcher,
            runtime,
            fingerprint,
        )
        self.assertEqual(envelope.fingerprint, persistent_fingerprint)
        self.assertEqual(
            envelope.contract.preparation_fingerprint,
            fingerprint,
        )
        self.assertEqual(envelope.schema_version, 3)
        self.assertEqual(envelope.contract.title, "后台准备的第一章契约")
        self.assertNotIn("后台守门人", runtime.app.world_state.npc_personas)
        self.assertEqual(detached.model_refresh_count, 1)
        self.assertFalse(detached.refresh_calls[0]["register_session_npcs"])
        self.assertEqual(
            detached.refresh_calls[0]["preparation_source"],
            "prefetch",
        )
        self.assertFalse(detached.refresh_calls[1]["register_session_npcs"])

        restored_service = self._service()
        restored = restored_service._runtime(self.campaign_id)
        restored_envelope = (
            restored.app.session_zero_manager.state
            .prepared_chapter_one_session
        )
        self.assertTrue(restored.loaded_from_disk)
        self.assertIsInstance(restored_envelope, PreparedSessionContractCache)
        self.assertIsInstance(
            restored_envelope.contract,
            SessionDramaticContract,
        )
        self.assertIsInstance(
            restored_envelope.contract.important_npcs[0],
            SessionNPCRole,
        )
        self.assertEqual(
            restored_envelope.fingerprint,
            persistent_fingerprint,
        )

    def test_worker_waits_for_logical_write_lease_before_model_work(self) -> None:
        service = self._service()
        runtime = self._seed(service)
        prefetcher = service.adventure_opening_prefetcher
        started = threading.Event()
        release = threading.Event()
        self.addCleanup(release.set)
        generation = "f" * 64
        detached = _FakeDetachedPacingManager(
            _prepared_contract(generation),
            generation,
            started=started,
            release=release,
        )

        with runtime.write_lease_condition:
            runtime.write_lease_owner = "chapter-one-invitation-transaction"
        with patch.object(
            prefetcher,
            "_detached_manager",
            return_value=detached,
        ):
            prefetcher.schedule(
                campaign_id=self.campaign_id,
                session_id=self.session_id,
                channel_id=self.channel_id,
            )
            self.assertFalse(started.wait(timeout=0.15))
            with runtime.write_lease_condition:
                runtime.write_lease_owner = ""
                runtime.write_lease_condition.notify_all()
            self.assertTrue(started.wait(timeout=5.0))
            release.set()
            completed = prefetcher.wait(
                self.campaign_id,
                timeout_seconds=5.0,
            )

        self.assertEqual(completed["status"], "ready")
        self.assertEqual(detached.model_refresh_count, 1)

    def test_authoritative_change_while_running_drops_result_without_save(self) -> None:
        service = self._service()
        runtime = self._seed(service)
        prefetcher = service.adventure_opening_prefetcher
        started = threading.Event()
        release = threading.Event()
        self.addCleanup(release.set)
        old_fingerprint = "b" * 64
        old_manager = _FakeDetachedPacingManager(
            _prepared_contract(old_fingerprint),
            old_fingerprint,
            started=started,
            release=release,
        )
        new_fingerprint = "c" * 64
        current_manager = _FakeDetachedPacingManager(
            _prepared_contract(new_fingerprint),
            new_fingerprint,
        )
        snapshot_path = runtime.app.memory_store._snapshot_path(self.campaign_id)
        snapshot_before = snapshot_path.read_bytes()

        with patch.object(
            prefetcher,
            "_detached_manager",
            side_effect=[old_manager, current_manager],
        ), patch.object(service, "_autosave_campaign") as autosave:
            prefetcher.schedule(
                campaign_id=self.campaign_id,
                session_id=self.session_id,
                channel_id=self.channel_id,
            )
            self.assertTrue(started.wait(timeout=5.0))
            runtime.app.session_zero_manager.state.world.selected_first_act_summary += (
                " 玩家在等待期间补充：证物室位于东侧。"
            )
            runtime.state_version += 1
            release.set()
            completed = prefetcher.wait(
                self.campaign_id,
                timeout_seconds=5.0,
            )

        self.assertEqual(completed["status"], "stale")
        self.assertEqual(completed["reason"], "authoritative_inputs_changed")
        autosave.assert_not_called()
        self.assertIsNone(
            runtime.app.session_zero_manager.state
            .prepared_chapter_one_session
        )
        self.assertEqual(snapshot_path.read_bytes(), snapshot_before)
        persisted = json.loads(snapshot_path.read_text(encoding="utf-8"))
        self.assertIsNone(
            persisted["session_zero"]["prepared_chapter_one_session"]
        )

    def test_prime_uses_published_candidate_while_worker_waits_for_transaction_lock(
        self,
    ) -> None:
        service = self._service()
        runtime = self._seed(service)
        prefetcher = service.adventure_opening_prefetcher
        started = threading.Event()
        release = threading.Event()
        self.addCleanup(release.set)
        fingerprint = "d" * 64
        detached = _FakeDetachedPacingManager(
            _prepared_contract(fingerprint),
            fingerprint,
            started=started,
            release=release,
        )

        with patch.object(
            prefetcher,
            "_detached_manager",
            return_value=detached,
        ):
            scheduled = prefetcher.schedule(
                campaign_id=self.campaign_id,
                session_id=self.session_id,
                channel_id=self.channel_id,
            )
            self.assertEqual(scheduled["status"], "queued")
            self.assertTrue(started.wait(timeout=5.0))

            # Mirror the real tool transaction: the foreground request owns
            # the campaign RLock while the detached provider call finishes.
            # The worker must publish its immutable candidate before trying
            # to reacquire this lock for persistence.
            with runtime.transaction_lock:
                release.set()
                candidate_event = prefetcher._candidate_events[
                    self.campaign_id
                ]
                self.assertTrue(candidate_event.wait(timeout=5.0))
                with prefetcher._lock:
                    future = prefetcher._jobs[self.campaign_id]
                    candidate = deepcopy(
                        prefetcher._candidates.get(self.campaign_id)
                    )
                self.assertIsNotNone(candidate)
                self.assertFalse(future.done())
                self.assertIsNone(
                    runtime.app.session_zero_manager.state
                    .prepared_chapter_one_session
                )

                started_prime = time.monotonic()
                result = prefetcher.prime_for_consumption(
                    runtime,
                    campaign_id=self.campaign_id,
                    session_id=self.session_id,
                    channel_id=self.channel_id,
                    wait_timeout_seconds=3.0,
                )
                elapsed = time.monotonic() - started_prime

                self.assertEqual(result["status"], "prefetch_hit")
                persistent_fingerprint = self._persistent_fingerprint(
                    prefetcher,
                    runtime,
                    fingerprint,
                )
                self.assertEqual(
                    result["fingerprint"],
                    persistent_fingerprint[:12],
                )
                self.assertLess(elapsed, 0.5)
                # If prime_for_consumption had waited on Future.result(), the
                # worker could not finish while this transaction lock is held.
                self.assertFalse(future.done())

                concretizer = (
                    runtime.app.campaign_pacing_manager.contract_planner
                    .concretizer
                )
                primed = concretizer.export_cache_entry()
                self.assertIsNotNone(primed)
                self.assertEqual(primed["fingerprint"], fingerprint)
                self.assertEqual(
                    primed["contract"].title,
                    "后台准备的第一章契约",
                )

            completed = prefetcher.wait(
                self.campaign_id,
                timeout_seconds=5.0,
            )

        self.assertEqual(completed["status"], "ready")
        self.assertEqual(detached.model_refresh_count, 1)

    def test_reloaded_envelope_is_adopted_by_formal_start_as_a_cache_hit(self) -> None:
        service = self._service()
        runtime = self._seed(service)
        detached = service.adventure_opening_prefetcher._detached_manager(runtime)
        plan = detached.refresh_plan(
            conflict_active=False,
            allow_model_prep=False,
            deadline=None,
            register_session_npcs=False,
            preparation_source="fingerprint_test",
        )
        fingerprint = plan.dramatic_contract.preparation_fingerprint
        self.assertTrue(fingerprint)
        persistent_fingerprint = self._persistent_fingerprint(
            service.adventure_opening_prefetcher,
            runtime,
            fingerprint,
        )
        prepared = deepcopy(plan.dramatic_contract)
        prepared.title = "持久缓存命中的第一章契约"
        prepared.important_npcs.append(
            SessionNPCRole(
                name="后台守门人",
                public_role="卡里巴监狱夜巡长",
                goal_now="守住证物室",
            )
        )
        runtime.app.session_zero_manager.state.prepared_chapter_one_session = (
            PreparedSessionContractCache(
                schema_version=3,
                fingerprint=persistent_fingerprint,
                contract=deepcopy(prepared),
                model="prefetch-model",
                review_model="review-model",
                quality_status="model_reviewed",
                diagnostics={"last_error": ""},
                prepared_at=datetime.now(timezone.utc).isoformat(),
                # Process-local state counters are deliberately not cache
                # truth; this remains reusable after a restart despite the
                # intentionally impossible source version.
                source_state_version=999_999,
            )
        )
        service._autosave_campaign(runtime, self.campaign_id)
        self.assertNotIn("后台守门人", runtime.app.world_state.npc_personas)

        restored_service = self._service()
        restored = restored_service._runtime(self.campaign_id)
        restored.app.ensure_world_map_for_adventure = lambda **_kwargs: {
            "status": "existing",
            "reason": "prefetch test",
        }
        loaded_envelope = (
            restored.app.session_zero_manager.state
            .prepared_chapter_one_session
        )
        self.assertTrue(restored.loaded_from_disk)
        self.assertNotEqual(restored.state_version, 999_999)
        self.assertIsInstance(loaded_envelope, PreparedSessionContractCache)
        self.assertEqual(
            loaded_envelope.contract.title,
            "持久缓存命中的第一章契约",
        )
        self.assertEqual(restored.app.world_state.present_players, [])
        before_first_message = (
            restored_service.adventure_opening_prefetcher
            ._current_fingerprint_locked(restored)
        )
        # The HTTP ingress performs this bookkeeping before the GM tool can
        # consume the prepared envelope. It must not turn a valid persisted
        # Session 0 roster into a cache miss after service reconstruction.
        restored.app.world_state.mark_player_present("测试玩家甲")
        after_first_message = (
            restored_service.adventure_opening_prefetcher
            ._current_fingerprint_locked(restored)
        )
        self.assertEqual(before_first_message, persistent_fingerprint)
        self.assertEqual(after_first_message, persistent_fingerprint)
        loaded_contract = loaded_envelope.contract
        context = GMToolExecutionContext(
            campaign_id=self.campaign_id,
            session_id=self.session_id,
            channel_id=self.channel_id,
            speaker="测试玩家甲",
            gate_status="session_zero",
            directly_addressed=True,
            metadata={
                "current_message": "大家都同意进入第一章。",
                "_gm_agent_deadline_monotonic": time.monotonic() + 120.0,
            },
        )

        receipt = restored_service.gm_runtime_tools.start_session(
            context,
            {
                "phase": "adventure",
                "reason": "全桌明确同意进入第一章",
                "evidence": "大家都同意进入第一章",
            },
        )

        self.assertTrue(receipt.ok, receipt.message)
        cache_result = receipt.result["session_prep_cache"]
        self.assertEqual(cache_result["status"], "persistent_hit")
        self.assertTrue(cache_result["consumed"])
        concretizer = (
            restored.app.campaign_pacing_manager.contract_planner.concretizer
        )
        self.assertTrue(concretizer.last_cache_hit)
        adopted = (
            restored.app.story_arc_manager.state.current_pacing_plan
            .dramatic_contract
        )
        self.assertEqual(adopted.title, "持久缓存命中的第一章契约")
        self.assertIsNot(adopted, loaded_contract)
        self.assertIn("后台守门人", restored.app.world_state.npc_personas)
        self.assertIsNone(
            restored.app.session_zero_manager.state
            .prepared_chapter_one_session
        )

    def test_reachability_review_failure_marks_prefetch_fallback_and_blocks_consumption(self) -> None:
        service = self._service()
        runtime = self._seed(service)
        prefetcher = service.adventure_opening_prefetcher
        fingerprint = "d" * 64
        detached = _FakeDetachedPacingManager(
            _prepared_contract(fingerprint),
            fingerprint,
            reviewer_status="fallback_provider_error",
            reviewer_error="reachability reviewer timed out",
        )

        with patch.object(
            prefetcher,
            "_detached_manager",
            return_value=detached,
        ):
            queued = prefetcher.schedule(
                campaign_id=self.campaign_id,
                session_id=self.session_id,
                channel_id=self.channel_id,
            )
            self.assertEqual(queued["status"], "queued")
            completed = prefetcher.wait(
                self.campaign_id,
                timeout_seconds=5.0,
            )

            live_concretizer = (
                runtime.app.campaign_pacing_manager.contract_planner.concretizer
            )
            with patch.object(
                live_concretizer,
                "prime_cache",
                wraps=live_concretizer.prime_cache,
            ) as prime_cache:
                consumption = prefetcher.prime_for_consumption(
                    runtime,
                    campaign_id=self.campaign_id,
                    session_id=self.session_id,
                    channel_id=self.channel_id,
                    wait_timeout_seconds=0.0,
                )

        self.assertEqual(completed["status"], "ready")
        self.assertEqual(completed["quality_status"], "fallback")
        envelope = (
            runtime.app.session_zero_manager.state
            .prepared_chapter_one_session
        )
        self.assertIsNotNone(envelope)
        self.assertEqual(envelope.quality_status, "fallback")
        self.assertEqual(consumption["status"], "degraded")
        self.assertEqual(consumption["quality_status"], "fallback")
        prime_cache.assert_not_called()


if __name__ == "__main__":
    unittest.main()
