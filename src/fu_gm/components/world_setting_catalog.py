from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any

from fu_gm.models import MemoryVisibility


class WorldSettingCatalogError(ValueError):
    """A typed world-setting failure that the GM can repair."""

    def __init__(
        self,
        code: str,
        message: str,
        hint: str,
        *,
        result: dict[str, object] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.hint = hint
        self.result = dict(result or {})


class WorldSettingCatalog:
    """Versioned CRUD over world lore, separate from rules and character state."""

    PUBLIC_SCALARS = frozenset(
        {
            "campaign_title",
            "continent_name",
            "world_style",
            "world_shape",
            "map_card",
            "travel_day_length",
            "magic_tech_role",
            "group_concept",
            "starting_region",
            "party_dynamic",
            "description_style",
        }
    )
    PUBLIC_LISTS = frozenset(
        {
            "tone_preferences",
            "playstyle_themes",
            "consensus_notes",
            "core_themes",
            "historical_events",
            "villain_seeds",
            "villain_mirrors",
            "mysteries",
            "world_threats",
            "starting_bond_suggestions",
            "open_questions",
        }
    )
    PUBLIC_MAPPINGS = frozenset(
        {
            "pillars",
            "major_locations",
            "kingdoms",
            "factions",
            "custom_world_settings",
        }
    )
    PRIVATE_LISTS = frozenset(
        {
            "gm_secret_notes",
            "gm_inspiration_tags",
            "gm_guidance_notes",
            "gm_story_beats",
        }
    )
    PRIVATE_MAPPINGS = frozenset({"gm_prepared_locations"})
    MAP_CATEGORY = "map_locations"
    CATEGORIES = tuple(
        sorted(
            PUBLIC_SCALARS
            | PUBLIC_LISTS
            | PUBLIC_MAPPINGS
            | PRIVATE_LISTS
            | PRIVATE_MAPPINGS
            | {MAP_CATEGORY}
        )
    )
    GEOGRAPHY_CATEGORIES = frozenset(
        {"continent_name", "world_shape", "major_locations", "kingdoms", MAP_CATEGORY}
    )
    MAP_ATTRIBUTE_FIELDS = frozenset(
        {
            "terrain",
            "feature_type",
            "position_hint",
            "relative_to",
            "relative_position",
            "draw_icon",
            "icon_id",
            "faction",
            "discovered",
            "tags",
            "notes",
        }
    )
    MAP_FEATURE_TYPES = frozenset(
        {
            "settlement",
            "country",
            "mountain_range",
            "forest",
            "archipelago",
            "inland_sea",
            "lake",
            "coast",
            "region",
            "landmark",
            "fortress",
        }
    )
    MAP_POSITIONS = frozenset(
        {
            "north",
            "northeast",
            "east",
            "southeast",
            "south",
            "southwest",
            "west",
            "northwest",
            "center",
        }
    )

    def __init__(self, app: Any) -> None:
        self.app = app
        self.world_state = app.world_state
        self.profile = self.world_state.world_profile

    @property
    def revision(self) -> int:
        return max(0, int(self.profile.world_setting_revision or 0))

    def query(
        self,
        *,
        category: str = "",
        name: str = "",
        visibility: str = "public",
    ) -> dict[str, object]:
        clean_category = str(category or "").strip()
        clean_name = str(name or "").strip()
        if clean_category:
            self._require_category(clean_category)
        if visibility not in {"public", "gm_private", "all"}:
            raise WorldSettingCatalogError(
                "INVALID_WORLD_SETTING_VISIBILITY",
                "visibility 只能是 public、gm_private 或 all。",
                "选择一个有效可见范围后重试。",
            )
        records: list[dict[str, object]] = []
        if visibility in {"public", "all"}:
            records.extend(self._public_records(clean_category))
        if visibility in {"gm_private", "all"}:
            records.extend(self._private_records(clean_category))
        if clean_name:
            records = [item for item in records if str(item.get("name") or "") == clean_name]
        return {
            "revision": self.revision,
            "category": clean_category,
            "name": clean_name,
            "visibility": visibility,
            "records": records,
            "available_categories": list(self.CATEGORIES),
        }

    def create(
        self,
        *,
        category: str,
        name: str,
        value: str,
        attributes: dict[str, object] | None,
        visibility: str,
        authority: str,
        speaker: str,
        reason: str,
    ) -> dict[str, object]:
        category = self._require_category(category)
        visibility = self._require_visibility(category, visibility)
        value = self._require_value(value)
        name = self._normalized_name(category, name, value=value)
        attributes = self._validate_attributes(category, attributes)
        if self._exists(category, name, visibility=visibility):
            raise WorldSettingCatalogError(
                "WORLD_SETTING_ALREADY_EXISTS",
                f"世界设定 {category}.{name or category} 已存在。",
                "先查询当前内容；确实要改动时调用 update_world_setting。",
                result={"revision": self.revision, "category": category, "name": name},
            )
        if visibility == "public":
            self._create_public(category, name, value, attributes)
        else:
            self._create_private(category, name, value, attributes, authority, speaker, reason)
        return self._finish_mutation(
            "create",
            category=category,
            name=name,
            value=value,
            attributes=attributes,
            visibility=visibility,
            authority=authority,
            speaker=speaker,
            reason=reason,
        )

    def update(
        self,
        *,
        category: str,
        name: str,
        value: str,
        attributes: dict[str, object] | None,
        visibility: str,
        authority: str,
        speaker: str,
        reason: str,
    ) -> dict[str, object]:
        category = self._require_category(category)
        visibility = self._require_visibility(category, visibility)
        value = self._require_value(value)
        name = self._normalized_name(category, name, value=value, existing=True)
        attributes = self._validate_attributes(category, attributes)
        if not self._exists(category, name, visibility=visibility):
            raise WorldSettingCatalogError(
                "WORLD_SETTING_NOT_FOUND",
                f"没有找到世界设定 {category}.{name or category}。",
                "先查询准确名称；若这是新设定，改用 create_world_setting。",
                result={"revision": self.revision, "category": category, "name": name},
            )
        old_value = self._value(category, name, visibility=visibility)
        if visibility == "public":
            self._update_public(category, name, value, attributes)
        else:
            self._update_private(category, name, value, attributes)
        result_name = (
            value
            if category in self.PUBLIC_LISTS or category in self.PRIVATE_LISTS
            else name
        )
        return self._finish_mutation(
            "update",
            category=category,
            name=result_name,
            old_name=name if result_name != name else "",
            value=value,
            attributes=attributes,
            visibility=visibility,
            authority=authority,
            speaker=speaker,
            reason=reason,
            old_value=old_value,
        )

    def delete(
        self,
        *,
        category: str,
        name: str,
        visibility: str,
        authority: str,
        speaker: str,
        reason: str,
    ) -> dict[str, object]:
        category = self._require_category(category)
        visibility = self._require_visibility(category, visibility)
        name = self._normalized_name(category, name, existing=True)
        if not self._exists(category, name, visibility=visibility):
            raise WorldSettingCatalogError(
                "WORLD_SETTING_NOT_FOUND",
                f"没有找到世界设定 {category}.{name or category}。",
                "先查询准确类别和名称；不存在的设定无需删除。",
                result={"revision": self.revision, "category": category, "name": name},
            )
        old_value = self._value(category, name, visibility=visibility)
        if visibility == "public":
            self._delete_public(category, name)
        else:
            self._delete_private(category, name)
        return self._finish_mutation(
            "delete",
            category=category,
            name=name,
            value="",
            attributes={},
            visibility=visibility,
            authority=authority,
            speaker=speaker,
            reason=reason,
            old_value=old_value,
        )

    def rename(
        self,
        *,
        category: str,
        old_name: str,
        new_name: str,
        visibility: str,
        authority: str,
        speaker: str,
        reason: str,
    ) -> dict[str, object]:
        category = self._require_category(category)
        if category in self.PUBLIC_SCALARS or category in self.PUBLIC_LISTS or category in self.PRIVATE_LISTS:
            raise WorldSettingCatalogError(
                "WORLD_SETTING_RENAME_UNSUPPORTED",
                f"{category} 不是具名实体集合，不能使用重命名。",
                "标量直接 update；列表项用 update 把旧文本替换为新文本。",
            )
        visibility = self._require_visibility(category, visibility)
        old_name = self._require_name(old_name)
        new_name = self._require_name(new_name)
        if not self._exists(category, old_name, visibility=visibility):
            raise WorldSettingCatalogError(
                "WORLD_SETTING_NOT_FOUND",
                f"没有找到世界设定 {category}.{old_name}。",
                "先查询准确名称后再重命名。",
                result={"revision": self.revision, "category": category, "name": old_name},
            )
        if self._exists(category, new_name, visibility=visibility):
            raise WorldSettingCatalogError(
                "WORLD_SETTING_NAME_CONFLICT",
                f"{category}.{new_name} 已存在，不能覆盖。",
                "换一个名称，或先明确合并与删除策略。",
                result={"revision": self.revision, "category": category, "name": new_name},
            )
        value = self._value(category, old_name, visibility=visibility)
        if visibility == "public":
            self._rename_public(category, old_name, new_name)
        else:
            self._rename_private(category, old_name, new_name)
        return self._finish_mutation(
            "rename",
            category=category,
            name=new_name,
            old_name=old_name,
            value=value,
            attributes={},
            visibility=visibility,
            authority=authority,
            speaker=speaker,
            reason=reason,
        )

    def metadata_for(self, category: str, name: str, *, visibility: str) -> dict[str, object]:
        key = self._metadata_key(category, name, visibility=visibility)
        value = self.profile.world_setting_metadata.get(key)
        if isinstance(value, dict):
            return deepcopy(value)
        return {
            "category": category,
            "name": name,
            "visibility": visibility,
            "authority": "legacy_confirmed" if visibility == "public" else "gm_authored",
            "revision": 0,
        }

    def _public_records(self, category: str) -> list[dict[str, object]]:
        categories = [category] if category else sorted(
            self.PUBLIC_SCALARS | self.PUBLIC_LISTS | self.PUBLIC_MAPPINGS | {self.MAP_CATEGORY}
        )
        records: list[dict[str, object]] = []
        for current in categories:
            if current in self.PUBLIC_SCALARS:
                value = str(getattr(self.profile, current) or "").strip()
                if value:
                    records.append(self._record(current, "", value, "public"))
            elif current in self.PUBLIC_LISTS:
                for value in list(getattr(self.profile, current) or []):
                    if str(value).strip():
                        records.append(self._record(current, str(value), str(value), "public"))
            elif current in self.PUBLIC_MAPPINGS:
                for key, value in dict(getattr(self.profile, current) or {}).items():
                    records.append(self._record(current, str(key), str(value), "public"))
            elif current == self.MAP_CATEGORY:
                for key, location in sorted(self.world_state.map_locations.items()):
                    attributes = asdict(location)
                    attributes.pop("name", None)
                    value = str(attributes.pop("description", "") or "")
                    records.append(self._record(current, key, value, "public", attributes=attributes))
        return records

    def _private_records(self, category: str) -> list[dict[str, object]]:
        records: list[dict[str, object]] = []
        categories = [category] if category else sorted(self.PRIVATE_LISTS | self.PRIVATE_MAPPINGS)
        for current in categories:
            if current in self.PRIVATE_LISTS:
                for value in list(getattr(self.profile, current) or []):
                    records.append(self._record(current, str(value), str(value), "gm_private"))
            elif current in self.PRIVATE_MAPPINGS:
                for key, value in dict(getattr(self.profile, current) or {}).items():
                    records.append(self._record(current, str(key), str(value), "gm_private"))
        for item in self.profile.gm_private_world_settings.values():
            if not isinstance(item, dict):
                continue
            if category and str(item.get("category") or "") != category:
                continue
            records.append(
                {
                    "category": str(item.get("category") or ""),
                    "name": str(item.get("name") or ""),
                    "value": str(item.get("value") or ""),
                    "attributes": deepcopy(dict(item.get("attributes") or {})),
                    "visibility": "gm_private",
                    "authority": str(item.get("authority") or "gm_authored"),
                    "record_revision": int(item.get("revision") or 0),
                    "source_speaker": str(item.get("source_speaker") or ""),
                    "updated_at": str(item.get("updated_at") or ""),
                }
            )
        return records

    def _record(
        self,
        category: str,
        name: str,
        value: str,
        visibility: str,
        *,
        attributes: dict[str, object] | None = None,
    ) -> dict[str, object]:
        metadata = self.metadata_for(category, name, visibility=visibility)
        return {
            "category": category,
            "name": name,
            "value": value,
            "attributes": deepcopy(attributes or {}),
            "visibility": visibility,
            "authority": str(metadata.get("authority") or ""),
            "record_revision": int(metadata.get("revision") or 0),
            "source_speaker": str(metadata.get("source_speaker") or ""),
            "updated_at": str(metadata.get("updated_at") or ""),
        }

    def _create_public(
        self,
        category: str,
        name: str,
        value: str,
        attributes: dict[str, object],
    ) -> None:
        if category in self.PUBLIC_SCALARS:
            setattr(self.profile, category, value)
        elif category in self.PUBLIC_LISTS:
            getattr(self.profile, category).append(value)
        elif category in self.PUBLIC_MAPPINGS:
            getattr(self.profile, category)[name] = value
        elif category == self.MAP_CATEGORY:
            self._upsert_map_location(name, value, attributes)

    def _update_public(
        self,
        category: str,
        name: str,
        value: str,
        attributes: dict[str, object],
    ) -> None:
        if category in self.PUBLIC_SCALARS:
            setattr(self.profile, category, value)
        elif category in self.PUBLIC_LISTS:
            target = getattr(self.profile, category)
            index = target.index(name)
            if value != name and value in target:
                raise WorldSettingCatalogError(
                    "WORLD_SETTING_NAME_CONFLICT",
                    f"{category} 已经包含相同的新内容。",
                    "删除旧项或改成不同内容。",
                )
            target[index] = value
        elif category in self.PUBLIC_MAPPINGS:
            getattr(self.profile, category)[name] = value
        elif category == self.MAP_CATEGORY:
            self._upsert_map_location(name, value, attributes)

    def _delete_public(self, category: str, name: str) -> None:
        if category in self.PUBLIC_SCALARS:
            setattr(self.profile, category, "")
        elif category in self.PUBLIC_LISTS:
            getattr(self.profile, category).remove(name)
        elif category in self.PUBLIC_MAPPINGS:
            getattr(self.profile, category).pop(name, None)
        elif category == self.MAP_CATEGORY:
            self._drop_map_location(name)

    def _rename_public(self, category: str, old_name: str, new_name: str) -> None:
        if category in self.PUBLIC_MAPPINGS:
            target = getattr(self.profile, category)
            target[new_name] = target.pop(old_name)
        if category == self.MAP_CATEGORY:
            self._rename_map_location(old_name, new_name)

    def _create_private(
        self,
        category: str,
        name: str,
        value: str,
        attributes: dict[str, object],
        authority: str,
        speaker: str,
        reason: str,
    ) -> None:
        if category in self.PRIVATE_LISTS:
            getattr(self.profile, category).append(value)
            return
        if category in self.PRIVATE_MAPPINGS:
            getattr(self.profile, category)[name] = value
            return
        key = self._private_key(category, name)
        self.profile.gm_private_world_settings[key] = {
            "category": category,
            "name": name,
            "value": value,
            "attributes": deepcopy(attributes),
            "visibility": "gm_private",
            "authority": authority,
            "source_speaker": speaker,
            "reason": reason,
        }

    def _update_private(
        self,
        category: str,
        name: str,
        value: str,
        attributes: dict[str, object],
    ) -> None:
        if category in self.PRIVATE_LISTS:
            target = getattr(self.profile, category)
            target[target.index(name)] = value
            return
        if category in self.PRIVATE_MAPPINGS:
            getattr(self.profile, category)[name] = value
            return
        record = self.profile.gm_private_world_settings[self._private_key(category, name)]
        record["value"] = value
        if attributes:
            record["attributes"] = deepcopy(attributes)

    def _delete_private(self, category: str, name: str) -> None:
        if category in self.PRIVATE_LISTS:
            getattr(self.profile, category).remove(name)
            return
        if category in self.PRIVATE_MAPPINGS:
            getattr(self.profile, category).pop(name, None)
            return
        self.profile.gm_private_world_settings.pop(self._private_key(category, name), None)

    def _rename_private(self, category: str, old_name: str, new_name: str) -> None:
        if category in self.PRIVATE_MAPPINGS:
            target = getattr(self.profile, category)
            target[new_name] = target.pop(old_name)
            return
        old_key = self._private_key(category, old_name)
        record = self.profile.gm_private_world_settings.pop(old_key)
        record["name"] = new_name
        self.profile.gm_private_world_settings[self._private_key(category, new_name)] = record

    def _exists(self, category: str, name: str, *, visibility: str) -> bool:
        if visibility == "gm_private":
            if category in self.PRIVATE_LISTS:
                return name in getattr(self.profile, category)
            if category in self.PRIVATE_MAPPINGS:
                return name in getattr(self.profile, category)
            return self._private_key(category, name) in self.profile.gm_private_world_settings
        if category in self.PUBLIC_SCALARS:
            return bool(str(getattr(self.profile, category) or "").strip())
        if category in self.PUBLIC_LISTS:
            return name in getattr(self.profile, category)
        if category in self.PUBLIC_MAPPINGS:
            return name in getattr(self.profile, category)
        if category == self.MAP_CATEGORY:
            return name in self.world_state.map_locations
        return False

    def _value(self, category: str, name: str, *, visibility: str) -> str:
        if visibility == "gm_private":
            if category in self.PRIVATE_LISTS:
                return name
            if category in self.PRIVATE_MAPPINGS:
                return str(getattr(self.profile, category)[name])
            return str(
                self.profile.gm_private_world_settings[self._private_key(category, name)].get("value")
                or ""
            )
        if category in self.PUBLIC_SCALARS:
            return str(getattr(self.profile, category) or "")
        if category in self.PUBLIC_LISTS:
            return name
        if category in self.PUBLIC_MAPPINGS:
            return str(getattr(self.profile, category)[name])
        if category == self.MAP_CATEGORY:
            return str(self.world_state.map_locations[name].description or "")
        return ""

    def _finish_mutation(
        self,
        operation: str,
        *,
        category: str,
        name: str,
        value: str,
        attributes: dict[str, object],
        visibility: str,
        authority: str,
        speaker: str,
        reason: str,
        old_name: str = "",
        old_value: str = "",
    ) -> dict[str, object]:
        revision = self.revision + 1
        self.profile.world_setting_revision = revision
        now = self._now()
        old_metadata_key = self._metadata_key(
            category,
            old_name or name,
            visibility=visibility,
        )
        prior = deepcopy(self.profile.world_setting_metadata.get(old_metadata_key) or {})
        if old_name and old_metadata_key in self.profile.world_setting_metadata:
            self.profile.world_setting_metadata.pop(old_metadata_key, None)
        metadata_key = self._metadata_key(category, name, visibility=visibility)
        metadata = {
            **prior,
            "category": category,
            "name": name,
            "visibility": visibility,
            "authority": authority,
            "source_speaker": speaker,
            "reason": reason,
            "revision": revision,
            "created_at": str(prior.get("created_at") or now),
            "updated_at": now,
        }
        if operation == "delete":
            self.profile.world_setting_metadata.pop(old_metadata_key, None)
        else:
            self.profile.world_setting_metadata[metadata_key] = metadata
        if (
            visibility == "gm_private"
            and category not in self.PRIVATE_LISTS
            and category not in self.PRIVATE_MAPPINGS
        ):
            private_record = self.profile.gm_private_world_settings.get(
                self._private_key(category, name)
            )
            if isinstance(private_record, dict):
                private_record.update(
                    {
                        "authority": authority,
                        "source_speaker": speaker,
                        "reason": reason,
                        "revision": revision,
                        "created_at": str(private_record.get("created_at") or now),
                        "updated_at": now,
                    }
                )
        audit = {
            "operation": operation,
            "category": category,
            "name": name,
            "old_name": old_name,
            "value": value,
            "old_value": old_value,
            "attributes": deepcopy(attributes),
            "visibility": visibility,
            "authority": authority,
            "source_speaker": speaker,
            "reason": reason,
            "revision": revision,
            "at": now,
        }
        self.profile.world_setting_audit_log.append(audit)
        del self.profile.world_setting_audit_log[:-300]
        if visibility == "public":
            self._sync_public_projection(
                operation,
                category=category,
                name=name,
                old_name=old_name,
                value=value,
                old_value=old_value,
                attributes=attributes,
            )
        else:
            self.world_state._refresh_gm_guidance(self.profile)
        record: dict[str, object] | None = None
        if operation != "delete":
            records = list(
                self.query(
                    category=category,
                    name=name,
                    visibility=visibility,
                )["records"]
            )
            record = (
                records[0]
                if records
                else self._record(
                    category,
                    name,
                    value,
                    visibility,
                    attributes=attributes,
                )
            )
        return {
            "operation": operation,
            "category": category,
            "name": name,
            "old_name": old_name,
            "value": value,
            "visibility": visibility,
            "authority": authority,
            "revision": revision,
            "record": record,
        }

    def _sync_public_projection(
        self,
        operation: str,
        *,
        category: str,
        name: str,
        old_name: str,
        value: str,
        old_value: str,
        attributes: dict[str, object],
    ) -> None:
        if operation in {"update", "delete", "rename"}:
            self._remove_legacy_memory(category, old_name or name, old_value)
        if category in {"major_locations", "kingdoms"}:
            if operation in {"create", "update"}:
                feature_type = "country" if category == "kingdoms" else ""
                self.world_state.upsert_map_location(
                    name,
                    description=value,
                    feature_type=feature_type,
                )
            elif operation == "rename":
                self._rename_projected_location(old_name, name, category=category)
            elif operation == "delete":
                self._drop_projected_location(name, category=category, old_value=old_value)
        if category == self.MAP_CATEGORY and operation in {"create", "update"}:
            self.profile.major_locations[name] = value
        if category == self.MAP_CATEGORY and operation == "delete":
            self.profile.major_locations.pop(name, None)
        if category == self.MAP_CATEGORY and operation == "rename":
            if old_name in self.profile.major_locations:
                self.profile.major_locations[name] = self.profile.major_locations.pop(old_name)

        self.world_state.apply_world_profile(self.profile)
        self._sync_world_sheet()
        self.app.session_zero_manager.state.world = self.profile
        if self.app.session_zero_manager.state.active:
            self.app.session_zero_manager.ensure_custom_map_card()
            self.app.session_zero_manager.refresh_stage_from_state()
        story_arc = getattr(self.app, "story_arc_manager", None)
        if story_arc is not None:
            story_arc.sync_from_world_profile()
        if category in self.GEOGRAPHY_CATEGORIES:
            self._mark_map_changed(reason=f"world_setting_{operation}:{category}")
        self.world_state.record_memory_event(
            self._audit_public_summary(operation, category, name, old_name),
            kind="world_setting_change",
            visibility=MemoryVisibility.PUBLIC,
            entities=[item for item in (old_name, name) if item],
            tags=["world_setting", operation, category],
            source="gm_world_setting_tools",
            payload={
                "operation": operation,
                "category": category,
                "name": name,
                "old_name": old_name,
                "revision": self.revision,
            },
        )

    def _upsert_map_location(
        self,
        name: str,
        value: str,
        attributes: dict[str, object],
    ) -> None:
        location = self.world_state.upsert_map_location(name, description=value)
        for field_name, field_value in attributes.items():
            if field_name in {"tags", "notes"}:
                setattr(location, field_name, list(field_value))
            else:
                setattr(location, field_name, field_value)

    def _drop_map_location(self, name: str) -> None:
        self.world_state.map_locations.pop(name, None)
        self.world_state.map_notes.pop(name, None)
        layout = self.world_state.semantic_map
        layout.location_cells.pop(name, None)
        layout.location_points.pop(name, None)
        for location in self.world_state.map_locations.values():
            if location.relative_to == name:
                location.relative_to = ""
                location.relative_position = ""
        self.world_state.map_routes = {
            route_id: route
            for route_id, route in self.world_state.map_routes.items()
            if route.origin != name and route.destination != name
        }
        if self.world_state.world_sheet is not None:
            self.world_state.world_sheet.major_locations.pop(name, None)
            self.world_state.world_sheet.location_facilities.pop(name, None)

    def _rename_map_location(self, old_name: str, new_name: str) -> None:
        location = self.world_state.map_locations.pop(old_name)
        location.name = new_name
        self.world_state.map_locations[new_name] = location
        if old_name in self.world_state.map_notes:
            self.world_state.map_notes[new_name] = self.world_state.map_notes.pop(old_name)
        layout = self.world_state.semantic_map
        if old_name in layout.location_cells:
            layout.location_cells[new_name] = layout.location_cells.pop(old_name)
        if old_name in layout.location_points:
            layout.location_points[new_name] = layout.location_points.pop(old_name)
        for other in self.world_state.map_locations.values():
            if other.relative_to == old_name:
                other.relative_to = new_name
        for route in self.world_state.map_routes.values():
            if route.origin == old_name:
                route.origin = new_name
            if route.destination == old_name:
                route.destination = new_name
        if self.profile.starting_region == old_name:
            self.profile.starting_region = new_name
        party_sheet = self.world_state.party_sheet
        if party_sheet is not None and party_sheet.starting_region == old_name:
            party_sheet.starting_region = new_name
        world_sheet = self.world_state.world_sheet
        if world_sheet is not None:
            if old_name in world_sheet.major_locations:
                world_sheet.major_locations[new_name] = world_sheet.major_locations.pop(old_name)
            if old_name in world_sheet.location_facilities:
                world_sheet.location_facilities[new_name] = world_sheet.location_facilities.pop(old_name)

    def _rename_projected_location(self, old_name: str, new_name: str, *, category: str) -> None:
        location = self.world_state.map_locations.get(old_name)
        if location is None:
            return
        if category == "kingdoms" and location.feature_type != "country":
            return
        if new_name in self.world_state.map_locations:
            raise WorldSettingCatalogError(
                "WORLD_SETTING_NAME_CONFLICT",
                f"地图上已经存在【{new_name}】，无法级联重命名。",
                "先合并或重命名地图地点，再重试。",
            )
        self._rename_map_location(old_name, new_name)

    def _drop_projected_location(self, name: str, *, category: str, old_value: str) -> None:
        location = self.world_state.map_locations.get(name)
        if location is None:
            return
        if category == "kingdoms" and location.feature_type != "country":
            return
        if location.description and old_value and location.description != old_value:
            return
        self._drop_map_location(name)

    def _sync_world_sheet(self) -> None:
        sheet = self.world_state.world_sheet
        if sheet is None:
            return
        for field_name in (
            "campaign_title",
            "continent_name",
            "world_style",
            "starting_region",
        ):
            setattr(sheet, field_name, str(getattr(self.profile, field_name) or ""))
        for field_name in (
            "pillars",
            "major_locations",
            "factions",
        ):
            setattr(sheet, field_name, deepcopy(getattr(self.profile, field_name)))
        for field_name in (
            "core_themes",
            "villain_seeds",
            "villain_mirrors",
            "mysteries",
            "starting_bond_suggestions",
        ):
            setattr(sheet, field_name, list(getattr(self.profile, field_name)))

    def _remove_legacy_memory(self, category: str, name: str, old_value: str) -> None:
        exact = ""
        if category == "campaign_title":
            exact = f"Session 0 战役标题：{old_value}"
        elif category == "continent_name":
            exact = f"Session 0 大陆名称：{old_value}"
        elif category == "magic_tech_role":
            exact = f"Session 0 魔法与科技：{old_value}"
        elif category == "group_concept":
            exact = f"Session 0 小队原型：{old_value}"
        elif category == "kingdoms":
            exact = f"Session 0 国家【{name}】：{old_value}"
        elif category == "historical_events":
            exact = f"Session 0 历史事件：{old_value}"
        elif category == "world_threats":
            exact = f"Session 0 世界威胁：{old_value}"
        if exact:
            self.world_state.memories = [item for item in self.world_state.memories if item != exact]
        if category == "factions" and name in self.world_state.npc_relationships:
            self.world_state.npc_relationships[name] = [
                item for item in self.world_state.npc_relationships[name] if item != old_value
            ]
            if not self.world_state.npc_relationships[name]:
                self.world_state.npc_relationships.pop(name, None)

    def _mark_map_changed(self, *, reason: str) -> None:
        layout = self.world_state.semantic_map
        layout.revision = max(0, int(layout.revision or 0)) + 1
        layout.updated_at = self._now()
        if not layout.source:
            layout.source = "world_setting_catalog"
        status = dict(getattr(self.app, "_world_map_generation_status", {}) or {})
        if str(status.get("status") or "").lower() in {"generated", "ready", "stale"}:
            status["status"] = "stale"
            status["reason"] = reason
            self.app._world_map_generation_status = status

    @classmethod
    def _validate_attributes(
        cls,
        category: str,
        raw: dict[str, object] | None,
    ) -> dict[str, object]:
        if not raw:
            return {}
        if category != cls.MAP_CATEGORY:
            raise WorldSettingCatalogError(
                "WORLD_SETTING_ATTRIBUTES_UNSUPPORTED",
                "attributes 只用于 map_locations。",
                "普通世界设定只提交 name 与 value。",
            )
        unknown = sorted(set(raw) - cls.MAP_ATTRIBUTE_FIELDS)
        if unknown:
            raise WorldSettingCatalogError(
                "INVALID_MAP_LOCATION_ATTRIBUTES",
                "地图地点包含未知属性：" + "、".join(unknown),
                "只使用工具schema列出的地图属性。",
            )
        result: dict[str, object] = {}
        for key, value in raw.items():
            if key in {"draw_icon", "discovered"}:
                if not isinstance(value, bool):
                    raise WorldSettingCatalogError(
                        "INVALID_MAP_LOCATION_ATTRIBUTES",
                        f"{key} 必须是布尔值。",
                        "改为 true 或 false。",
                    )
                result[key] = value
            elif key in {"tags", "notes"}:
                if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
                    raise WorldSettingCatalogError(
                        "INVALID_MAP_LOCATION_ATTRIBUTES",
                        f"{key} 必须是字符串数组。",
                        "修正数组内容后重试。",
                    )
                result[key] = [str(item).strip() for item in value if str(item).strip()]
            else:
                clean = str(value or "").strip()
                # Optional map hints are frequently emitted as empty strings by
                # structured-output models.  They mean "unspecified", not a
                # malformed direction, and should not become stored attributes.
                if not clean:
                    continue
                if key == "feature_type" and clean and clean not in cls.MAP_FEATURE_TYPES:
                    raise WorldSettingCatalogError(
                        "INVALID_MAP_FEATURE_TYPE",
                        f"未知地图地点类型：{clean}。",
                        "使用工具schema中的 feature_type。",
                    )
                if key in {"position_hint", "relative_position"} and clean and clean not in cls.MAP_POSITIONS:
                    raise WorldSettingCatalogError(
                        "INVALID_MAP_POSITION",
                        f"未知地图方向：{clean}。",
                        "使用 north、south、east、west 等标准方向。",
                    )
                result[key] = clean
        return result

    def _normalized_name(
        self,
        category: str,
        name: str,
        *,
        value: str = "",
        existing: bool = False,
    ) -> str:
        if category in self.PUBLIC_SCALARS:
            return ""
        if category in self.PUBLIC_LISTS or category in self.PRIVATE_LISTS:
            return self._require_name(name if existing else value)
        return self._require_name(name)

    @classmethod
    def _require_category(cls, category: str) -> str:
        clean = str(category or "").strip()
        if clean not in cls.CATEGORIES:
            raise WorldSettingCatalogError(
                "UNKNOWN_WORLD_SETTING_CATEGORY",
                f"未知世界设定类别：{clean or '（空）'}。",
                "从 available_categories 中选择后重试。",
                result={"available_categories": list(cls.CATEGORIES)},
            )
        return clean

    @classmethod
    def _require_visibility(cls, category: str, visibility: str) -> str:
        clean = str(visibility or "public").strip()
        if clean not in {"public", "gm_private"}:
            raise WorldSettingCatalogError(
                "INVALID_WORLD_SETTING_VISIBILITY",
                "写入可见性只能是 public 或 gm_private。",
                "选择公开事实或GM幕后准备后重试。",
            )
        if category in cls.PRIVATE_LISTS | cls.PRIVATE_MAPPINGS and clean != "gm_private":
            raise WorldSettingCatalogError(
                "PRIVATE_WORLD_SETTING_CATEGORY",
                f"{category} 只能作为GM幕后资料保存。",
                "把 visibility 改为 gm_private。",
            )
        return clean

    @staticmethod
    def _require_name(name: str) -> str:
        clean = str(name or "").strip()
        if not clean:
            raise WorldSettingCatalogError(
                "WORLD_SETTING_NAME_REQUIRED",
                "这类世界设定需要准确名称。",
                "先查询现有名称，或为新实体提供名称。",
            )
        if len(clean) > 120:
            raise WorldSettingCatalogError(
                "WORLD_SETTING_NAME_TOO_LONG",
                "世界设定名称不能超过120个字符。",
                "把名称缩短，详细信息放入 value。",
            )
        return clean

    @staticmethod
    def _require_value(value: str) -> str:
        clean = str(value or "").strip()
        if not clean:
            raise WorldSettingCatalogError(
                "WORLD_SETTING_VALUE_REQUIRED",
                "世界设定内容不能为空。",
                "提供要创建或更新的具体内容。",
            )
        if len(clean) > 6000:
            raise WorldSettingCatalogError(
                "WORLD_SETTING_VALUE_TOO_LONG",
                "单条世界设定不能超过6000个字符。",
                "拆成多个相互独立的设定后分别提交。",
            )
        return clean

    @staticmethod
    def _private_key(category: str, name: str) -> str:
        return f"{category}\u241f{name}"

    @classmethod
    def _metadata_key(cls, category: str, name: str, *, visibility: str) -> str:
        return f"{visibility}\u241f{category}\u241f{name}"

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    @staticmethod
    def _audit_public_summary(operation: str, category: str, name: str, old_name: str) -> str:
        labels = {
            "create": "新增",
            "update": "修改",
            "delete": "删除",
            "rename": "重命名",
        }
        target = f"{old_name} -> {name}" if old_name else (name or category)
        return f"世界设定{labels.get(operation, operation)}：{category}.{target}"


__all__ = ["WorldSettingCatalog", "WorldSettingCatalogError"]
