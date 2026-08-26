from __future__ import annotations

import argparse
import http.client
import json
import logging
import os
import sys
import threading
import webbrowser
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Sequence
from urllib.parse import parse_qs, unquote, urlparse

from fu_gm.character_builder_api import CharacterBuilderAPI
from fu_gm.character_workshop_settings import (
    CharacterWorkshopSettings,
    bundled_workflow_root,
)


APP_DIR_NAME = "Fabula-Ultima-Character-Workshop"
DEFAULT_PORT = 8767


@dataclass
class WorkshopFilePayload:
    body: bytes
    content_type: str


class CharacterWorkshopService:
    """Standalone Fabula Ultima character workshop HTTP boundary."""

    _GET_ROUTES = {
        "/characters",
        "/characters/index.html",
        "/characters/styles.css",
        "/characters/app.js",
        "/characters/portrait-placeholder.webp",
        "/v1/character-builder/catalog",
        "/v1/character-cards",
        "/v1/character-cards/export",
        "/v1/workshop/settings",
        "/v1/portraits/file",
    }
    _POST_ROUTES = {
        "/v1/character-builder/preview",
        "/v1/character-cards/build",
        "/v1/character-cards/text",
        "/v1/character-cards/validate",
        "/v1/character-cards/import/preview",
        "/v1/character-cards/import",
        "/v1/workshop/settings",
        "/v1/workshop/settings/test-comfyui",
        "/v1/workshop/settings/test-llm",
        "/v1/portraits/prompt",
        "/v1/portraits/generate",
        "/v1/portraits/recover",
    }

    def __init__(
        self,
        *,
        data_root: str | Path,
        use_llm: bool = True,
        workflow_root: str | Path | None = None,
    ) -> None:
        self.data_root = Path(data_root)
        self.data_root.mkdir(parents=True, exist_ok=True)
        self.use_llm = use_llm
        self.settings = CharacterWorkshopSettings(
            self.data_root,
            workflow_root=workflow_root,
            use_environment_defaults=str(
                os.environ.get("FU_GM_DISTRIBUTION_MODE") or "development"
            ).strip().lower()
            != "portable",
        )
        self.character_builder = CharacterBuilderAPI(
            self,
            data_root=self.data_root,
            settings=self.settings,
        )

    def handle(
        self,
        method: str,
        path: str,
        payload: dict[str, object] | None = None,
    ):
        parsed = urlparse(path)
        route = parsed.path.rstrip("/") or "/"
        query = parse_qs(parsed.query)
        if method == "GET" and route == "/health":
            return 200, {
                "ok": True,
                "service": "fu-character-workshop",
                "distribution_mode": str(
                    os.environ.get("FU_GM_DISTRIBUTION_MODE") or "development"
                ),
                "portrait_generation": self.character_builder._portrait_feature_enabled(),
                "storage": "standalone_roster",
            }
        if method == "GET" and route == "/":
            route = "/characters"
        if method == "GET" and route in CharacterBuilderAPI.STATIC_FILES:
            static_file = self.character_builder.static_file(route)
            if static_file is None:
                return 404, {"ok": False, "error": "角色工房网页资源不存在。"}
            body, content_type = static_file
            return 200, WorkshopFilePayload(body, content_type)
        if method == "GET" and route == "/v1/character-builder/catalog":
            return 200, self.character_builder.catalog()
        if method == "GET" and route == "/v1/character-cards":
            return 200, self.character_builder.list_characters()
        if method == "GET" and route == "/v1/workshop/settings":
            return 200, self.settings.public_payload()
        if method == "GET" and route == "/v1/character-cards/export":
            return self.character_builder.export_card(
                query.get("hero_name", [""])[0],
            )
        if method == "GET" and route.startswith("/v1/portrait-jobs/"):
            return self.character_builder.portrait_job(unquote(route.split("/")[-1]))
        if method == "GET" and route == "/v1/portraits/file":
            status, body, content_type = self.character_builder.portrait_file(
                query.get("job_id", [""])[0],
                query.get("name", [""])[0],
            )
            return (
                status,
                WorkshopFilePayload(body, content_type)
                if isinstance(body, bytes)
                else body,
            )
        allowed = (
            method == "GET" and route in self._GET_ROUTES
        ) or (
            method == "POST" and route in self._POST_ROUTES
        )
        if not allowed:
            return 404, {"ok": False, "error": "本地角色工房未开放此接口。"}
        payload = payload or {}
        try:
            if route == "/v1/character-builder/preview":
                return self.character_builder.preview_build(payload)
            if route == "/v1/character-cards/build":
                return self.character_builder.build_card(payload)
            if route == "/v1/character-cards/text":
                return self.character_builder.text_card(payload)
            if route == "/v1/character-cards/validate":
                return self.character_builder.validate_card(payload)
            if route == "/v1/character-cards/import/preview":
                return self.character_builder.import_preview(payload)
            if route == "/v1/character-cards/import":
                return self.character_builder.import_card(payload)
            if route == "/v1/workshop/settings":
                return 200, self.settings.update(payload)
            if route == "/v1/workshop/settings/test-comfyui":
                return 200, self.settings.test_comfyui()
            if route == "/v1/workshop/settings/test-llm":
                return 200, self.settings.test_llm()
            if route == "/v1/portraits/prompt":
                return self.character_builder.prompt_portrait(payload)
            if route == "/v1/portraits/generate":
                return self.character_builder.generate_portrait(payload)
            if route == "/v1/portraits/recover":
                return self.character_builder.recover_portrait(payload)
        except ValueError as exc:
            return 422, {"ok": False, "error": str(exc)}
        except Exception as exc:
            logging.exception("Character Workshop request failed: %s %s", method, route)
            return 500, {"ok": False, "error": str(exc)}
        return 404, {"ok": False, "error": "本地角色工房未开放此接口。"}


class _WorkshopRequestHandler(BaseHTTPRequestHandler):
    service: CharacterWorkshopService

    def do_GET(self) -> None:
        self._respond(*self.service.handle("GET", self.path))

    def do_POST(self) -> None:
        content_type = self.headers.get_content_type().lower()
        if content_type != "application/json" and not content_type.endswith("+json"):
            self._respond(415, {"ok": False, "error": "POST 请求必须使用 application/json。"})
            return
        try:
            content_length = int(self.headers.get("Content-Length") or "0")
        except ValueError:
            self._respond(400, {"ok": False, "error": "Content-Length 不合法。"})
            return
        if content_length < 0 or content_length > 4 * 1024 * 1024:
            self._respond(413, {"ok": False, "error": "请求内容不能超过 4 MB。"})
            return
        try:
            payload = json.loads(self.rfile.read(content_length).decode("utf-8")) if content_length else {}
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._respond(400, {"ok": False, "error": "请求体不是合法的 UTF-8 JSON。"})
            return
        if not isinstance(payload, dict):
            self._respond(400, {"ok": False, "error": "JSON 顶层必须是对象。"})
            return
        self._respond(*self.service.handle("POST", self.path, payload))

    def log_message(self, format: str, *args: Any) -> None:
        return

    def _respond(
        self,
        status: int,
        payload: dict[str, Any] | str | WorkshopFilePayload,
    ) -> None:
        if isinstance(payload, WorkshopFilePayload):
            body = payload.body
            content_type = payload.content_type
        elif isinstance(payload, str):
            body = payload.encode("utf-8")
            content_type = "text/plain; charset=utf-8"
        else:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            content_type = "application/json; charset=utf-8"
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data: http: https:; connect-src 'self'; "
            "frame-ancestors 'none'; base-uri 'none'; object-src 'none'",
        )
        self.end_headers()
        self.wfile.write(body)


def make_workshop_server(
    host: str,
    port: int,
    *,
    service: CharacterWorkshopService,
) -> ThreadingHTTPServer:
    class Handler(_WorkshopRequestHandler):
        pass

    Handler.service = service
    return ThreadingHTTPServer((host, port), Handler)


def portable_root() -> Path:
    configured = str(os.environ.get("FU_CHARACTER_WORKSHOP_HOME") or "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    local_app_data = str(os.environ.get("LOCALAPPDATA") or "").strip()
    base = Path(local_app_data) if local_app_data else Path.home() / "AppData" / "Local"
    return (base / APP_DIR_NAME).resolve()


def prepare_portable_environment(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    os.environ["FU_GM_DISTRIBUTION_MODE"] = "portable"
    os.environ["FU_GM_PORTRAIT_FEATURE_ENABLED"] = "1"
    os.environ["FU_GM_COMFYUI_ENABLED"] = "1"
    os.environ["FU_GM_DOTENV_PATH"] = str(root / "disabled.env")
    for name in tuple(os.environ):
        if name.startswith("FU_GM_") and name.endswith("API_KEY"):
            os.environ.pop(name, None)


def _existing_workshop_url(port: int) -> str:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=0.8)
    try:
        connection.request("GET", "/health")
        response = connection.getresponse()
        body = json.loads(response.read().decode("utf-8"))
        if response.status == 200 and body.get("service") == "fu-character-workshop":
            return f"http://127.0.0.1:{port}/characters"
    except (OSError, ValueError, json.JSONDecodeError):
        return ""
    finally:
        connection.close()
    return ""


def _build_server(data_root: Path, preferred_port: int, *, use_llm: bool = True):
    service = CharacterWorkshopService(
        data_root=data_root,
        use_llm=use_llm,
        workflow_root=bundled_workflow_root(),
    )
    last_error: OSError | None = None
    for port in range(preferred_port, preferred_port + 20):
        try:
            return make_workshop_server("127.0.0.1", port, service=service)
        except OSError as exc:
            last_error = exc
    raise RuntimeError("无法找到可用的本地端口。") from last_error


def _run_smoke_test(root: Path) -> int:
    server = _build_server(root / "data" / "character-workshop", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        connection = http.client.HTTPConnection(*server.server_address, timeout=5)
        connection.request("GET", "/characters")
        page = connection.getresponse()
        page_body = page.read().decode("utf-8")
        connection.close()

        connection = http.client.HTTPConnection(*server.server_address, timeout=5)
        connection.request("GET", "/v1/character-builder/catalog")
        catalog_response = connection.getresponse()
        catalog = json.loads(catalog_response.read().decode("utf-8"))
        connection.close()

        valid = (
            page.status == 200
            and "最终物语角色工房" in page_body
            and catalog_response.status == 200
            and catalog.get("capabilities", {}).get("portrait_generation") is True
            and catalog.get("capabilities", {}).get("connection_settings") is True
        )
        return 0 if valid else 1
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _run_headless(server, url: str, *, open_browser: bool) -> int:
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


def _run_window(server, url: str, root: Path, *, open_browser: bool) -> int:
    import tkinter as tk
    from tkinter import messagebox, ttk

    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()

    window = tk.Tk()
    window.title("最终物语角色工房")
    window.geometry("480x270")
    window.minsize(480, 270)
    window.maxsize(480, 270)

    frame = ttk.Frame(window, padding=24)
    frame.pack(fill="both", expand=True)
    ttk.Label(
        frame,
        text="最终物语角色工房",
        font=("Microsoft YaHei UI", 17, "bold"),
    ).pack(anchor="w")
    ttk.Label(
        frame,
        text="本地服务已启动，可在网页设置 ComfyUI 与 LLM。",
        font=("Microsoft YaHei UI", 10),
    ).pack(anchor="w", pady=(6, 18))

    url_value = tk.StringVar(value=url)
    ttk.Entry(frame, textvariable=url_value, state="readonly").pack(fill="x")

    buttons = ttk.Frame(frame)
    buttons.pack(fill="x", pady=(22, 0))

    def open_workshop() -> None:
        webbrowser.open(url)

    def open_data_folder() -> None:
        root.mkdir(parents=True, exist_ok=True)
        os.startfile(root)  # type: ignore[attr-defined]

    closing = False

    def close_app() -> None:
        nonlocal closing
        if closing:
            return
        closing = True
        for child in buttons.winfo_children():
            child.configure(state="disabled")
        window.title("正在停止角色工房...")

        def stop_server() -> None:
            server.shutdown()
            server.server_close()
            window.after(0, window.destroy)

        threading.Thread(target=stop_server, daemon=True).start()

    ttk.Button(buttons, text="打开角色工房", command=open_workshop).pack(side="left")
    ttk.Button(buttons, text="打开数据目录", command=open_data_folder).pack(
        side="left", padx=10
    )
    ttk.Button(buttons, text="停止并退出", command=close_app).pack(side="right")
    ttk.Label(
        frame,
        text="关闭此窗口会停止本地服务；角色名册保存在当前 Windows 用户目录。",
        foreground="#555555",
        font=("Microsoft YaHei UI", 9),
    ).pack(anchor="w", pady=(24, 0))

    window.protocol("WM_DELETE_WINDOW", close_app)
    if open_browser:
        window.after(450, open_workshop)
    try:
        window.mainloop()
    except tk.TclError as exc:
        messagebox.showerror("角色工房", str(exc))
        return 1
    finally:
        if server_thread.is_alive() and not closing:
            server.shutdown()
            server.server_close()
        server_thread.join(timeout=2)
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="启动最终物语本地角色工房。")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--data-root", default="")
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--smoke-test", action="store_true")
    parser.add_argument(
        "--development",
        action="store_true",
        help="使用项目环境配置，并保留 LLM 与 ComfyUI 立绘功能。",
    )
    args = parser.parse_args(argv)

    root = portable_root()
    if args.development:
        os.environ["FU_GM_DISTRIBUTION_MODE"] = "development"
    else:
        prepare_portable_environment(root)
    log_dir = root / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        filename=log_dir / "launcher.log",
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        encoding="utf-8",
    )

    data_root = (
        Path(args.data_root).expanduser().resolve()
        if args.data_root
        else root / "data" / "character-workshop"
    )
    data_root.mkdir(parents=True, exist_ok=True)
    if args.smoke_test:
        return _run_smoke_test(root / "smoke-test")

    existing = _existing_workshop_url(args.port)
    if existing:
        if not args.no_browser:
            webbrowser.open(existing)
        return 0

    try:
        server = _build_server(data_root, args.port, use_llm=True)
        port = int(server.server_address[1])
        url = f"http://127.0.0.1:{port}/characters"
        logging.info("Character Workshop started at %s", url)
        if args.headless:
            return _run_headless(server, url, open_browser=not args.no_browser)
        return _run_window(server, url, root, open_browser=not args.no_browser)
    except Exception:
        logging.exception("Character Workshop failed to start")
        if not args.headless:
            try:
                from tkinter import messagebox

                messagebox.showerror(
                    "角色工房启动失败",
                    f"无法启动角色工房。请查看日志：\n{log_dir / 'launcher.log'}",
                )
            except Exception:
                pass
        return 1


if __name__ == "__main__":
    sys.exit(main())
