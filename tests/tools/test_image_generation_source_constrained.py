import base64
import importlib
import json
from types import SimpleNamespace


def test_image_generate_handler_forwards_source_constrained_fields(monkeypatch):
    from tools import image_generation_tool

    captured = {}

    def fake_dispatch(prompt, aspect_ratio, extra_kwargs=None):
        captured["prompt"] = prompt
        captured["aspect_ratio"] = aspect_ratio
        captured["extra_kwargs"] = extra_kwargs
        return json.dumps({"success": True, "image": "/tmp/out.png"})

    monkeypatch.setattr(image_generation_tool, "_dispatch_to_plugin_provider", fake_dispatch)

    payload = json.loads(image_generation_tool._handle_image_generate({
        "prompt": "convert source to sprite",
        "aspect_ratio": "square",
        "mode": "source_constrained_edit",
        "source_image": "/tmp/source-crop.png",
        "mask_image": "/tmp/foreground-mask.png",
        "lock_assets": {"edge": "/tmp/edge-lock.png"},
        "output_background": "#FF00FF",
        "review_only": True,
    }))

    assert payload["success"] is True
    assert captured["prompt"] == "convert source to sprite"
    assert captured["aspect_ratio"] == "square"
    assert captured["extra_kwargs"] == {
        "mode": "source_constrained_edit",
        "source_image": "/tmp/source-crop.png",
        "mask_image": "/tmp/foreground-mask.png",
        "lock_assets": {"edge": "/tmp/edge-lock.png"},
        "output_background": "#FF00FF",
        "review_only": True,
    }


def test_image_generate_handler_forwards_source_derived_canon_fields(monkeypatch):
    from tools import image_generation_tool

    captured = {}

    def fake_dispatch(prompt, aspect_ratio, extra_kwargs=None):
        captured["prompt"] = prompt
        captured["aspect_ratio"] = aspect_ratio
        captured["extra_kwargs"] = extra_kwargs
        return json.dumps({"success": True, "image": "/tmp/out.png"})

    monkeypatch.setattr(image_generation_tool, "_dispatch_to_plugin_provider", fake_dispatch)

    payload = json.loads(image_generation_tool._handle_image_generate({
        "prompt": "source identity plus approved atlas style",
        "aspect_ratio": "square",
        "mode": "source_derived_canon",
        "source_image": "/tmp/source-crop.png",
        "lock_assets": {"atlas_01": "/tmp/atlas-01.png"},
        "output_background": "#FF00FF",
        "review_only": True,
    }))

    assert payload["success"] is True
    assert captured["prompt"] == "source identity plus approved atlas style"
    assert captured["aspect_ratio"] == "square"
    assert captured["extra_kwargs"] == {
        "mode": "source_derived_canon",
        "source_image": "/tmp/source-crop.png",
        "lock_assets": {"atlas_01": "/tmp/atlas-01.png"},
        "output_background": "#FF00FF",
        "review_only": True,
    }


def test_image_generate_handler_forwards_source_derived_canon_overworld_fields(monkeypatch):
    from tools import image_generation_tool

    captured = {}

    def fake_dispatch(prompt, aspect_ratio, extra_kwargs=None):
        captured["prompt"] = prompt
        captured["aspect_ratio"] = aspect_ratio
        captured["extra_kwargs"] = extra_kwargs
        return json.dumps({"success": True, "image": "/tmp/out.png"})

    monkeypatch.setattr(image_generation_tool, "_dispatch_to_plugin_provider", fake_dispatch)

    payload = json.loads(image_generation_tool._handle_image_generate({
        "prompt": "fresh native 48x48 map token",
        "aspect_ratio": "square",
        "mode": "source_derived_canon_overworld",
        "source_image": "/tmp/approved-battle.png",
        "lock_assets": {"ow_atlas": "/tmp/overworld-atlas.png"},
        "output_background": "#FF00FF",
        "review_only": True,
    }))

    assert payload["success"] is True
    assert captured["prompt"] == "fresh native 48x48 map token"
    assert captured["aspect_ratio"] == "square"
    assert captured["extra_kwargs"] == {
        "mode": "source_derived_canon_overworld",
        "source_image": "/tmp/approved-battle.png",
        "lock_assets": {"ow_atlas": "/tmp/overworld-atlas.png"},
        "output_background": "#FF00FF",
        "review_only": True,
    }


def test_plugin_dispatch_refuses_mode_when_provider_lacks_capability(monkeypatch):
    from tools import image_generation_tool

    provider = SimpleNamespace(
        name="plain-provider",
        capabilities=lambda: {"text_to_image": True},
        generate=lambda **_: {"success": True, "image": "/tmp/should-not-exist.png"},
    )

    monkeypatch.setattr(image_generation_tool, "_read_configured_image_provider", lambda: "plain-provider")
    monkeypatch.setattr(image_generation_tool, "_read_configured_image_model", lambda: None)
    monkeypatch.setitem(__import__("sys").modules, "agent.image_gen_registry", SimpleNamespace(get_provider=lambda _: provider))
    monkeypatch.setitem(__import__("sys").modules, "hermes_cli.plugins", SimpleNamespace(_ensure_plugins_discovered=lambda force=False: None))

    result = json.loads(image_generation_tool._dispatch_to_plugin_provider(
        "convert source",
        "square",
        {"mode": "source_derived_canon", "source_image": "/tmp/source.png"},
    ))

    assert result["success"] is False
    assert result["error_type"] == "unsupported_image_generation_mode"
    assert result["provider"] == "plain-provider"
    assert result["mode"] == "source_derived_canon"
    assert "prompt-only" in result["error"]


def test_openai_codex_builds_source_constrained_response_payload(tmp_path):
    plugin = importlib.import_module("plugins.image_gen.openai-codex")
    png = tmp_path / "source-crop.png"
    png.write_bytes(base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/p9sAAAAASUVORK5CYII="
    ))

    response_input = plugin._build_response_input(
        prompt="transform the source",
        mode="source_constrained_edit",
        source_image=str(png),
        mask_image=str(png),
        lock_assets={"edge": str(png), "silhouette": str(png)},
        output_background="#FF00FF",
        review_only=True,
    )
    content = response_input[0]["content"]

    assert response_input[0]["role"] == "user"
    assert content[0] == {"type": "input_text", "text": "transform the source"}
    assert "Mode: source_constrained_edit" in content[1]["text"]
    assert "Requested output background: #FF00FF." in content[1]["text"]
    assert [item["type"] for item in content].count("input_image") == 3
    assert all(item["image_url"].startswith("data:image/png;base64,") for item in content if item["type"] == "input_image")

    tool = plugin._build_image_generation_tool(
        size="1024x1024",
        quality="high",
        mode="source_constrained_edit",
        mask_image=str(png),
    )
    assert tool["type"] == "image_generation"
    assert tool["model"] == "gpt-image-2"
    assert tool["action"] == "edit"
    assert tool["background"] == "opaque"
    assert "input_fidelity" not in tool
    assert tool["input_image_mask"]["image_url"].startswith("data:image/png;base64,")


def test_openai_codex_capabilities_and_fail_closed_without_source():
    plugin = importlib.import_module("plugins.image_gen.openai-codex")
    provider = plugin.OpenAICodexImageGenProvider()

    assert provider.capabilities()["source_constrained_edit"] is True
    assert provider.capabilities()["mask_edit"] is True

    result = provider.generate(
        "transform this source",
        aspect_ratio="square",
        mode="source_constrained_edit",
    )

    assert result["success"] is False
    assert result["error_type"] == "missing_source_image"
    assert result["mode"] == "source_constrained_edit"


def test_openai_codex_builds_source_derived_canon_payload(tmp_path):
    plugin = importlib.import_module("plugins.image_gen.openai-codex")
    png = tmp_path / "source-crop.png"
    png.write_bytes(base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/p9sAAAAASUVORK5CYII="
    ))

    response_input = plugin._build_response_input(
        prompt="convert source into Swagbound canon",
        mode="source_derived_canon",
        source_image=str(png),
        mask_image=str(png),
        lock_assets={"atlas_01": str(png), "atlas_02": str(png)},
        output_background="#FF00FF",
        review_only=True,
    )
    content = response_input[0]["content"]

    assert "Mode: source_derived_canon" in content[1]["text"]
    assert "identity authority" in content[1]["text"]
    assert "style-reference-only" in content[1]["text"]
    assert "Mask image ignored" in content[1]["text"]
    assert [item["type"] for item in content].count("input_image") == 3

    tool = plugin._build_image_generation_tool(
        size="1024x1024",
        quality="high",
        mode="source_derived_canon",
        mask_image=str(png),
    )
    assert tool["type"] == "image_generation"
    assert tool["model"] == "gpt-image-2"
    assert tool["action"] == "auto"
    assert "input_fidelity" not in tool
    assert "input_image_mask" not in tool


def test_openai_codex_builds_source_derived_canon_overworld_payload(tmp_path):
    plugin = importlib.import_module("plugins.image_gen.openai-codex")
    png = tmp_path / "approved-battle.png"
    png.write_bytes(base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/p9sAAAAASUVORK5CYII="
    ))

    response_input = plugin._build_response_input(
        prompt="convert battle identity into map token",
        mode="source_derived_canon_overworld",
        source_image=str(png),
        mask_image=str(png),
        lock_assets={"ow_atlas": str(png)},
        output_background="#FF00FF",
        review_only=True,
    )
    content = response_input[0]["content"]

    assert "Mode: source_derived_canon_overworld" in content[1]["text"]
    assert "identity reference only" in content[1]["text"]
    assert "not as an edit canvas" in content[1]["text"]
    assert "fresh native 48x48" in content[1]["text"]
    assert "Do not make a miniature battle portrait" in content[1]["text"]
    assert "Mask image ignored" in content[1]["text"]
    assert [item["type"] for item in content].count("input_image") == 2

    tool = plugin._build_image_generation_tool(
        size="1024x1024",
        quality="high",
        mode="source_derived_canon_overworld",
        mask_image=str(png),
    )
    assert tool["type"] == "image_generation"
    assert tool["model"] == "gpt-image-2"
    assert tool["action"] == "auto"
    assert "input_fidelity" not in tool
    assert "input_image_mask" not in tool


def test_openai_codex_text_to_image_omits_input_fidelity():
    plugin = importlib.import_module("plugins.image_gen.openai-codex")

    tool = plugin._build_image_generation_tool(
        size="1024x1024",
        quality="medium",
        mode="text_to_image",
    )

    assert tool["type"] == "image_generation"
    assert tool["model"] == "gpt-image-2"
    assert "input_fidelity" not in tool


def test_openai_codex_diagnoses_connection_errors():
    plugin = importlib.import_module("plugins.image_gen.openai-codex")

    exc = RuntimeError("Connection error: getaddrinfo failed for chatgpt.com")
    details = plugin._diagnose_codex_exception(exc)

    assert details["failure_class"] == "provider-connection"
    assert details["error_type"] == "provider_dns_error"
    assert details["retryable_provider_failure"] is True
    assert details["exception_type"] == "RuntimeError"


def test_openai_codex_diagnoses_safety_blocks():
    plugin = importlib.import_module("plugins.image_gen.openai-codex")

    class SafetyBlocked(Exception):
        status_code = 400
        body = {"error": {"message": "Rejected by the safety system."}}

    details = plugin._diagnose_codex_exception(SafetyBlocked("Bad request"))

    assert details["failure_class"] == "safety-block"
    assert details["error_type"] == "safety_blocked"
    assert details["retryable_provider_failure"] is False
    assert details["status_code"] == 400
