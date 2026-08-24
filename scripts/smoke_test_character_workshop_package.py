from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import tempfile
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _request(url: str, payload: dict[str, object] | None = None) -> dict[str, object]:
    data = None
    headers: dict[str, str] = {}
    method = "GET"
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json; charset=utf-8"
        method = "POST"
    request = Request(url, data=data, headers=headers, method=method)
    try:
        with urlopen(request, timeout=5) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code}: {body}") from exc


def _valid_build() -> dict[str, object]:
    return {
        "player_name": "便携测试玩家",
        "hero_name": "米菈便携版",
        "identity": "逃离实验室的魔导技师",
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
        "notes": [],
        "fate_roll": [2, 5],
    }


def _stop_process_tree(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            check=False,
            capture_output=True,
        )
    else:
        process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def main() -> int:
    parser = argparse.ArgumentParser(description="Smoke-test the packaged Character Workshop.")
    parser.add_argument("exe", type=Path, nargs="?")
    args = parser.parse_args()
    if args.exe is None:
        candidates = list(
            (Path(__file__).resolve().parents[1] / "release" / "character-workshop").glob(
                "*/Fabula-Ultima-Character-Workshop.exe"
            )
        )
        if len(candidates) != 1:
            raise FileNotFoundError("Could not uniquely locate the packaged executable.")
        executable = candidates[0].resolve()
    else:
        executable = args.exe.expanduser().resolve()
    if not executable.is_file():
        raise FileNotFoundError(executable)

    port = _free_port()
    with tempfile.TemporaryDirectory(prefix="fu-character-workshop-") as tempdir:
        process = subprocess.Popen(
            [
                str(executable),
                "--headless",
                "--no-browser",
                "--port",
                str(port),
                "--data-root",
                str(Path(tempdir) / "data"),
            ],
            cwd=executable.parent,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        try:
            base_url = f"http://127.0.0.1:{port}"
            deadline = time.monotonic() + 20
            health: dict[str, object] | None = None
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    raise RuntimeError(
                        f"Packaged process exited early with code {process.returncode}."
                    )
                try:
                    health = _request(f"{base_url}/health")
                    break
                except (OSError, RuntimeError, URLError):
                    time.sleep(0.2)
            if health is None:
                raise TimeoutError("Packaged Character Workshop did not become ready.")

            settings = _request(f"{base_url}/v1/workshop/settings")
            catalog = _request(f"{base_url}/v1/character-builder/catalog")

            build_payload = {"campaign_id": "packaged-e2e", "build": _valid_build()}
            built = _request(f"{base_url}/v1/character-cards/build", build_payload)
            card = built["card"]
            text_card = _request(
                f"{base_url}/v1/character-cards/text",
                {"campaign_id": "packaged-e2e", "card": card},
            )
            imported = _request(
                f"{base_url}/v1/character-cards/import",
                {
                    "campaign_id": "packaged-e2e",
                    "card": card,
                    "conflict": "reject",
                },
            )
            listing = _request(
                f"{base_url}/v1/character-cards?campaign_id=packaged-e2e"
            )
            result = {
                "service": health.get("service"),
                "portrait_enabled": health.get("portrait_generation"),
                "settings_available": catalog.get("capabilities", {}).get(
                    "connection_settings"
                ),
                "api_key_configured": settings.get("llm", {}).get(
                    "api_key_configured"
                ),
                "anima_workflow": settings.get("comfyui", {})
                .get("workflows", {})
                .get("anima"),
                "krea_lora_workflow": settings.get("comfyui", {})
                .get("workflows", {})
                .get("krea_lora"),
                "built": built.get("valid"),
                "text_contains_hero": "米菈便携版" in str(text_card.get("text") or ""),
                "imported": imported.get("character", {}).get("name"),
                "roster_count": len(listing.get("characters", [])),
            }
            print(json.dumps(result, ensure_ascii=True, indent=2))
            return 0 if result == {
                "service": "fu-character-workshop",
                "portrait_enabled": True,
                "settings_available": True,
                "api_key_configured": False,
                "anima_workflow": True,
                "krea_lora_workflow": True,
                "built": True,
                "text_contains_hero": True,
                "imported": "米菈便携版",
                "roster_count": 1,
            } else 1
        finally:
            _stop_process_tree(process)


if __name__ == "__main__":
    raise SystemExit(main())
