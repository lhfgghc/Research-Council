from __future__ import annotations

import json

import httpx
from fastapi.testclient import TestClient

from app.config import SettingsStore
from app import main
from app.services.literature import title_similarity


def test_model_profiles_preserve_keys_and_public_view_masks_them(tmp_path) -> None:
    store = SettingsStore(tmp_path / "settings.json")
    saved = store.save(
        {
            "model_profiles": [
                {
                    "id": "model_a",
                    "name": "模型 A",
                    "model": "provider/model-a",
                    "base_url": "https://a.example/v1",
                    "api_key": "secret-a",
                    "context_window_tokens": 128000,
                },
                {
                    "id": "model_b",
                    "name": "模型 B",
                    "model": "provider/model-b",
                    "base_url": "https://b.example/v1",
                    "api_key": "secret-b",
                },
            ],
            "role_models": {"method_architect": ["model_a", "model_b"]},
        }
    )
    assert saved.model_profiles[0].api_key == "secret-a"
    assert saved.role_models["method_architect"] == ["model_a", "model_b"]

    public = store.public_view(saved)
    assert "api_key" not in public
    assert "api_key" not in public["model_profiles"][0]
    assert public["model_profiles"][0]["has_api_key"] is True
    assert "api_key" not in public["model_profiles"][1]

    updated = store.save(
        {
            "model_profiles": [
                {
                    "id": "model_a",
                    "name": "模型 A（改名）",
                    "model": "provider/model-a",
                    "base_url": "https://a.example/v1/",
                }
            ]
        }
    )
    assert updated.model_profiles[0].name == "模型 A（改名）"
    assert updated.model_profiles[0].api_key == "secret-a"
    cleared = store.save({"model_profiles": [{**updated.model_profiles[0].model_dump(), "api_key": ""}]})
    assert cleared.model_profiles[0].api_key == ""


def test_legacy_connections_migrate_without_sharing_keys_between_endpoints(tmp_path, monkeypatch) -> None:
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({
        "base_url": "https://shared.example/v1/", "model": "model-a", "api_key": "legacy-key",
        "model_profiles": [
            {"id": "a", "model": "model-a"},
            {"id": "b", "model": "model-b", "context_window_tokens": 32000},
            {"id": "c", "model": "model-c", "base_url": "https://other.example/v1"},
        ],
        "role_models": {"method_architect": ["a", "b", "c"]},
    }), encoding="utf-8")
    store = SettingsStore(path)
    migrated = store.load()
    assert migrated.role_models["pi"] == ["a"]
    assert migrated.role_models["method_architect"] == ["a", "b", "c"]
    assert [profile.api_key for profile in migrated.model_profiles] == ["legacy-key", "legacy-key", ""]
    assert migrated.profile("b").context_window_tokens == 32000
    store.save({})
    persisted = json.loads(path.read_text(encoding="utf-8"))
    assert not ({"api_key", "base_url", "model"} & persisted.keys())
    monkeypatch.setenv("COMMAND_CODE_API_KEY", "different-environment-key")
    assert store.load().profile("c").api_key == ""


def test_model_settings_routes_reveal_and_test_only_the_selected_model(tmp_path, monkeypatch) -> None:
    store = SettingsStore(tmp_path / "settings.json")
    monkeypatch.setattr(main, "settings_store", store)
    connection_requests = []

    def provider(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/messages"):
            payload = json.loads(request.content)
            assert payload["stream"] is False
            assert payload["thinking"] == {"type": "adaptive"}
            assert payload["output_config"] == {"effort": "low"}
            connection_requests.append((str(request.url), request.headers["x-api-key"], payload["model"]))
            return httpx.Response(200, json={"type": "message", "content": [{"type": "text", "text": "OK"}]})
        connection_requests.append((str(request.url), request.headers["Authorization"], json.loads(request.content)["model"]))
        return httpx.Response(200, json={"choices": [{"message": {"content": "OK"}}]})

    client_factory = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: client_factory(transport=httpx.MockTransport(provider), **kwargs))
    browser = TestClient(main.app, base_url="http://localhost", client=("127.0.0.1", 42000))
    profiles = [
        {"id": name, "model": f"model-{name}", "base_url": f"https://{name}.example/v1", "api_key": f"key-{name}"}
        for name in ("a", "b")
    ]
    profiles[0]["api_format"] = "anthropic"
    profiles[0]["model"] = "claude-sonnet-5-5"
    response = browser.put("/api/settings", json={"model_profiles": profiles})
    assert response.status_code == 200 and "key-a" not in response.text and "key-b" not in response.text
    revealed = browser.get("/api/settings/models/b/key")
    assert revealed.json() == {"api_key": "key-b"} and revealed.headers["cache-control"] == "no-store"
    remote = TestClient(main.app, base_url="http://localhost", client=("192.0.2.1", 42000))
    assert remote.get("/api/settings/models/b/key").status_code == 403
    assert browser.get("/api/settings/models/b/key", headers={"Host": "untrusted.example"}).status_code == 403
    assert browser.post("/api/settings/models/b/test").status_code == 200
    assert browser.post("/api/settings/models/a/test").status_code == 200
    assert connection_requests == [
        ("https://b.example/v1/chat/completions", "Bearer key-b", "model-b"),
        ("https://a.example/v1/messages", "key-a", "claude-sonnet-5-5"),
    ]

    changed = {**profiles[1], "base_url": "https://changed.example/v1"}
    del changed["api_key"]
    assert browser.put("/api/settings", json={"model_profiles": [profiles[0], changed]}).status_code == 200
    assert browser.get("/api/settings/models/b/key").json() == {"api_key": ""}
    assert browser.post("/api/settings/models/b/test").status_code == 400


def test_title_similarity_handles_english_and_chinese() -> None:
    assert title_similarity(
        "decision-preserving context compression for multi-agent research",
        "Decision preserving context compression for research agents",
    ) > 0.4
    assert title_similarity("多智能体科研讨论的上下文压缩", "多智能体讨论中的上下文压缩方法") > 0.3
    assert title_similarity("完全不同", "unrelated topic") == 0.0
