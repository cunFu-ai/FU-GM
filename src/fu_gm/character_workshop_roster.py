from __future__ import annotations

import json
import threading
import uuid
from copy import deepcopy
from pathlib import Path
from typing import Any

from fu_gm.components.character_card_manager import (
    CharacterCardError,
    CharacterCardManager,
)


ROSTER_SCHEMA = "fabula-ultima.character-workshop-roster"
ROSTER_SCHEMA_VERSION = 1


class CharacterWorkshopRoster:
    """Standalone, player-facing storage for validated character cards."""

    def __init__(self, data_root: str | Path, cards: CharacterCardManager) -> None:
        self.data_root = Path(data_root)
        self.path = self.data_root / "roster.json"
        self.cards = cards
        self._lock = threading.RLock()

    def list_characters(self) -> dict[str, Any]:
        with self._lock:
            stored_cards = self._read_cards()
        characters: list[dict[str, Any]] = []
        warnings: list[str] = []
        for index, card in enumerate(stored_cards):
            result = self.cards.validate_card(card)
            if not result.get("valid"):
                detail = "；".join(result.get("errors") or ["未知错误"])
                warnings.append(f"名册中的第 {index + 1} 张角色卡已跳过：{detail}")
                continue
            characters.append(result["character"])
        return {
            "ok": True,
            "storage": "standalone_roster",
            "characters": characters,
            "warnings": warnings,
        }

    def preview_import(self, card: dict[str, Any]) -> dict[str, Any]:
        result = self.cards.validate_card(card)
        if not result.get("valid"):
            return result
        with self._lock:
            stored_cards = self._read_cards()
        result["conflicts"] = self._conflicts(result["character"], stored_cards)
        result["conflict_modes"] = ["reject", "replace", "copy"]
        result["storage"] = "standalone_roster"
        return result

    def import_card(
        self,
        card: dict[str, Any],
        *,
        conflict: str = "reject",
    ) -> dict[str, Any]:
        mode = str(conflict or "reject").strip().lower()
        if mode not in {"reject", "replace", "copy"}:
            raise CharacterCardError("冲突处理方式必须是 reject、replace 或 copy。")

        validated = self._require_valid(card)
        normalized = validated["card"]
        summary = validated["character"]
        warnings = list(validated.get("warnings") or [])

        with self._lock:
            stored_cards = self._read_cards()
            conflicts = self._conflicts(summary, stored_cards)
            conflict_indexes = sorted({item["index"] for item in conflicts})

            if conflicts and mode == "reject":
                raise CharacterCardError(conflicts[0]["message"])

            if conflicts and mode == "copy":
                normalized = self._copy_card(normalized, stored_cards)
                validated = self._require_valid(normalized)
                normalized = validated["card"]
                summary = validated["character"]
                warnings.extend(validated.get("warnings") or [])
                warnings.append(f"角色冲突，已作为【{summary['name']}】加入名册。")
            elif conflicts and mode == "replace":
                stored_cards = [
                    existing
                    for index, existing in enumerate(stored_cards)
                    if index not in conflict_indexes
                ]

            stored_cards.append(normalized)
            self._write_cards(stored_cards)

        return {
            "ok": True,
            "storage": "standalone_roster",
            "character": summary,
            "warnings": warnings,
            "card": normalized,
        }

    def export_card(self, hero_name: str) -> dict[str, Any]:
        clean_name = str(hero_name or "").strip()
        if not clean_name:
            raise CharacterCardError("未指定要导出的角色。")
        with self._lock:
            stored_cards = self._read_cards()
        for card in stored_cards:
            result = self.cards.validate_card(card)
            if result.get("valid") and result["character"].get("name") == clean_name:
                return deepcopy(result["card"])
        raise CharacterCardError(f"找不到角色：【{clean_name}】。")

    def _copy_card(
        self,
        card: dict[str, Any],
        stored_cards: list[dict[str, Any]],
    ) -> dict[str, Any]:
        copied = deepcopy(card)
        existing_names = set()
        for existing in stored_cards:
            result = self.cards.validate_card(existing)
            if result.get("valid"):
                existing_names.add(str(result["character"].get("name") or ""))

        base_name = str(copied.get("build", {}).get("hero_name") or "未命名角色")
        new_name = self._unique_copy_name(base_name, existing_names)
        new_card_id = str(uuid.uuid4())
        copied.setdefault("card", {})["id"] = new_card_id
        copied["card"]["revision"] = 1
        copied["card"]["exported_at"] = self.cards.now()
        copied.setdefault("build", {})["hero_name"] = new_name
        snapshot = copied.get("character_snapshot")
        if isinstance(snapshot, dict):
            snapshot["name"] = new_name
            snapshot["card_id"] = new_card_id
            snapshot["card_revision"] = 1
        return copied

    @staticmethod
    def _unique_copy_name(base_name: str, existing_names: set[str]) -> str:
        candidate = f"{base_name}（副本）"
        if candidate not in existing_names:
            return candidate
        for number in range(2, 1000):
            candidate = f"{base_name}（副本 {number}）"
            if candidate not in existing_names:
                return candidate
        raise CharacterCardError("无法为角色副本生成可用名称。")

    def _conflicts(
        self,
        candidate: dict[str, Any],
        stored_cards: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        conflicts: list[dict[str, Any]] = []
        candidate_id = str(candidate.get("card_id") or "")
        candidate_name = str(candidate.get("name") or "")
        for index, card in enumerate(stored_cards):
            result = self.cards.validate_card(card)
            if not result.get("valid"):
                continue
            existing = result["character"]
            if candidate_id and existing.get("card_id") == candidate_id:
                conflicts.append(
                    {
                        "index": index,
                        "type": "card_id",
                        "existing": existing.get("name", ""),
                        "message": f"同一角色卡已存在为【{existing.get('name', '')}】。",
                    }
                )
            elif existing.get("name") == candidate_name:
                conflicts.append(
                    {
                        "index": index,
                        "type": "name",
                        "existing": existing.get("name", ""),
                        "message": f"角色名【{candidate_name}】已经存在。",
                    }
                )
        return conflicts

    def _require_valid(self, card: dict[str, Any]) -> dict[str, Any]:
        result = self.cards.validate_card(card)
        if result.get("valid"):
            return result
        raise CharacterCardError("；".join(result.get("errors") or ["角色卡校验失败。"]))

    def _read_cards(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise CharacterCardError("本地名册文件损坏，无法读取。") from exc
        if not isinstance(payload, dict):
            raise CharacterCardError("本地名册格式不正确。")
        if payload.get("schema") != ROSTER_SCHEMA:
            raise CharacterCardError("本地名册类型不受支持。")
        if payload.get("schema_version") != ROSTER_SCHEMA_VERSION:
            raise CharacterCardError("本地名册版本不受支持。")
        cards = payload.get("cards")
        if not isinstance(cards, list) or not all(isinstance(card, dict) for card in cards):
            raise CharacterCardError("本地名册中的角色卡列表不正确。")
        return cards

    def _write_cards(self, cards: list[dict[str, Any]]) -> None:
        self.data_root.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema": ROSTER_SCHEMA,
            "schema_version": ROSTER_SCHEMA_VERSION,
            "cards": cards,
        }
        temporary = self.path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary.replace(self.path)
