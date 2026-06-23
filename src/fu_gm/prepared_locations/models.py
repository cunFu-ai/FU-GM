from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class LocationStoryHook:
    """A structured landmark, mystery, or request attached to a location."""

    title: str
    kind: str
    summary: str
    beats: tuple[str, ...] = ()


@dataclass(frozen=True)
class PreparedLocationSeed:
    """Backstage location material that becomes canon only through play."""

    name: str
    archetype: str
    inspiration_tags: tuple[str, ...]
    brief: str
    use_when: str
    questions: tuple[str, ...]
    hooks: tuple[str, ...] = ()
    source_book: str = "FU-GM"
    source_section: str = "预备地点库"
    keywords: tuple[str, ...] = ()
    terrain: tuple[str, ...] = ()
    travel_dice: tuple[str, ...] = ()
    common_elements: tuple[str, ...] = ()
    rare_elements: tuple[str, ...] = ()
    dangers: tuple[str, ...] = ()
    discoveries: tuple[str, ...] = ()
    themes: tuple[str, ...] = ()
    typical_features: tuple[str, ...] = ()
    campaign_position: str = ""
    villain_plans: str = ""
    story_hooks: tuple[LocationStoryHook, ...] = ()
    icon_name: str = ""

    def prompt_payload(self, *, detailed: bool = False) -> dict[str, object]:
        payload: dict[str, object] = {
            "name": self.name,
            "archetype": self.archetype,
            "brief": self.brief,
            "use_when": self.use_when,
            "source_book": self.source_book,
            "keywords": list(self.keywords),
            "terrain": list(self.terrain),
            "themes": list(self.themes),
            "dangers": list(self.dangers),
            "discoveries": list(self.discoveries),
            "questions": list(self.questions),
            "hooks": list(self.hooks),
        }
        if detailed:
            payload.update(
                {
                    "source_section": self.source_section,
                    "travel_dice": list(self.travel_dice),
                    "common_elements": list(self.common_elements),
                    "rare_elements": list(self.rare_elements),
                    "typical_features": list(self.typical_features),
                    "campaign_position": self.campaign_position,
                    "villain_plans": self.villain_plans,
                    "story_hooks": [
                        {
                            "title": hook.title,
                            "kind": hook.kind,
                            "summary": hook.summary,
                            "beats": list(hook.beats),
                        }
                        for hook in self.story_hooks
                    ],
                    "icon_name": self.icon_name,
                }
            )
        return payload


def story_hook(
    title: str,
    kind: str,
    summary: str,
    *beats: str,
) -> LocationStoryHook:
    return LocationStoryHook(title=title, kind=kind, summary=summary, beats=tuple(beats))


def location_seed(
    *,
    name: str,
    archetype: str,
    inspiration_tags: tuple[str, ...],
    brief: str,
    use_when: str,
    questions: tuple[str, ...],
    source_book: str,
    keywords: tuple[str, ...],
    terrain: tuple[str, ...],
    travel_dice: tuple[str, ...],
    common_elements: tuple[str, ...],
    rare_elements: tuple[str, ...],
    dangers: tuple[str, ...],
    discoveries: tuple[str, ...],
    themes: tuple[str, ...],
    typical_features: tuple[str, ...],
    campaign_position: str,
    villain_plans: str,
    story_hooks: tuple[LocationStoryHook, ...],
    icon_name: str,
    source_section: str = "世界：示例地点",
) -> PreparedLocationSeed:
    return PreparedLocationSeed(
        name=name,
        archetype=archetype,
        inspiration_tags=inspiration_tags,
        brief=brief,
        use_when=use_when,
        questions=questions,
        hooks=tuple(hook.title for hook in story_hooks),
        source_book=source_book,
        source_section=source_section,
        keywords=keywords,
        terrain=terrain,
        travel_dice=travel_dice,
        common_elements=common_elements,
        rare_elements=rare_elements,
        dangers=dangers,
        discoveries=discoveries,
        themes=themes,
        typical_features=typical_features,
        campaign_position=campaign_position,
        villain_plans=villain_plans,
        story_hooks=story_hooks,
        icon_name=icon_name,
    )
