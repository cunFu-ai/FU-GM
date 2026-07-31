from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from fu_gm.components.gm_tool_state_transaction import (
    GMToolStateTransactionFactory,
)
from fu_gm.components.gm_tool_admission_policy import (
    GMToolDecisionAdmissionPolicy,
)
from fu_gm.gm_adventure_tools import GMAdventureToolService
from fu_gm.gm_campaign_tools import GMCampaignToolService
from fu_gm.gm_clock_tools import GMClockToolService
from fu_gm.gm_dungeon_tools import GMDungeonToolService
from fu_gm.gm_gameplay_tools import GMGameplayToolService
from fu_gm.gm_map_tools import GMMapToolService
from fu_gm.gm_npc_tools import GMNPCToolService
from fu_gm.gm_reference_tools import GMReferenceToolService
from fu_gm.gm_runtime_tools import GMRuntimeToolService
from fu_gm.gm_scene_tools import GMSceneToolService
from fu_gm.gm_session_zero_tools import GMSessionZeroToolService
from fu_gm.gm_supervisor_tools import GMSupervisorToolService
from fu_gm.gm_tool_contracts import GMToolRegistry


@dataclass(frozen=True)
class GMToolSuite:
    """Composition root for typed GM domain capabilities.

    Domain services depend only on their narrow host protocols and the shared
    contracts module. Keeping registration here prevents the HTTP transport
    constructor from becoming the ownership boundary for every new tool.
    """

    registry: GMToolRegistry
    campaigns: GMCampaignToolService
    session_zero: GMSessionZeroToolService
    scenes: GMSceneToolService
    clocks: GMClockToolService
    npcs: GMNPCToolService
    gameplay: GMGameplayToolService
    maps: GMMapToolService
    runtime: GMRuntimeToolService
    adventure: GMAdventureToolService
    dungeons: GMDungeonToolService
    references: GMReferenceToolService
    supervisor: GMSupervisorToolService

    @classmethod
    def build(cls, host: Any) -> "GMToolSuite":
        campaigns = GMCampaignToolService(host)
        registry = campaigns.build_registry()
        session_zero = GMSessionZeroToolService(host)
        scenes = GMSceneToolService(host)
        clocks = GMClockToolService(host)
        npcs = GMNPCToolService(host)
        gameplay = GMGameplayToolService(host)
        maps = GMMapToolService(host)
        runtime = GMRuntimeToolService(host)
        adventure = GMAdventureToolService(host)
        dungeons = GMDungeonToolService(host)
        references = GMReferenceToolService()
        supervisor = GMSupervisorToolService(host)

        for service in (
            session_zero,
            scenes,
            clocks,
            npcs,
            gameplay,
            maps,
            runtime,
            adventure,
            dungeons,
            references,
            supervisor,
        ):
            service.register_tools(registry)
        registry.set_transaction_factory(GMToolStateTransactionFactory(host))
        registry.set_admission_guard(GMToolDecisionAdmissionPolicy(host))
        return cls(
            registry=registry,
            campaigns=campaigns,
            session_zero=session_zero,
            scenes=scenes,
            clocks=clocks,
            npcs=npcs,
            gameplay=gameplay,
            maps=maps,
            runtime=runtime,
            adventure=adventure,
            dungeons=dungeons,
            references=references,
            supervisor=supervisor,
        )
