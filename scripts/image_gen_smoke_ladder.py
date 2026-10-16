#!/usr/bin/env python3
"""Run a minimal image-provider smoke ladder before spending a canary."""

from __future__ import annotations

import argparse
import importlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _existing_file(value: str | None, *, label: str) -> str | None:
    if not value:
        return None
    path = Path(value).expanduser()
    if not path.is_file():
        raise SystemExit(f"{label} does not exist or is not a file: {value}")
    return str(path)


def _style_assets(paths: Iterable[str], limit: int) -> Dict[str, str]:
    assets: Dict[str, str] = {}
    for raw in paths:
        path = Path(raw).expanduser()
        if path.is_dir():
            candidates = sorted(
                p for p in path.iterdir()
                if p.is_file() and p.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp"}
            )
        elif path.is_file():
            candidates = [path]
        else:
            raise SystemExit(f"style reference path does not exist: {raw}")
        for candidate in candidates:
            if len(assets) >= limit:
                return assets
            assets[f"style_{len(assets) + 1:02d}"] = str(candidate)
    return assets


def _provider(name: str):
    if name != "openai-codex":
        raise SystemExit("Only openai-codex is supported by this smoke ladder today.")
    module = importlib.import_module("plugins.image_gen.openai-codex")
    return module.OpenAICodexImageGenProvider()


def _run_step(provider: Any, step: Dict[str, Any]) -> Dict[str, Any]:
    result = provider.generate(**step["request"])
    success = bool(result.get("success"))
    provider_outputs = 1 if success and result.get("image") else 0
    return {
        "name": step["name"],
        "goal": step["goal"],
        "status": "passed" if success else "failed",
        "request": {
            key: value
            for key, value in step["request"].items()
            if key not in {"prompt"}
        },
        "success": success,
        "provider": result.get("provider"),
        "model": result.get("model"),
        "image": result.get("image"),
        "providerOutputs": provider_outputs,
        "error_type": result.get("error_type"),
        "failure_class": result.get("failure_class"),
        "error": result.get("error"),
        "retryable_provider_failure": result.get("retryable_provider_failure"),
        "exception_type": result.get("exception_type"),
        "exception_chain": result.get("exception_chain"),
        "status_code": result.get("status_code"),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider", default="openai-codex")
    parser.add_argument("--source-image", help="Single source image for steps 2 and 3.")
    parser.add_argument(
        "--style-reference",
        action="append",
        default=[],
        help="Style image or directory for step 3. May be repeated.",
    )
    parser.add_argument("--style-limit", type=int, default=3)
    parser.add_argument("--output", default="asset-lab/generated/raw/provider-smoke-ladder-manifest.json")
    parser.add_argument("--start-step", type=int, choices=(1, 2, 3), default=1)
    parser.add_argument("--continue-on-failure", action="store_true")
    args = parser.parse_args()

    provider = _provider(args.provider)
    source_image = _existing_file(args.source_image, label="source image")
    style_assets = _style_assets(args.style_reference, args.style_limit)

    steps: List[Dict[str, Any]] = [
        {
            "name": "prompt-only-gpt-image-2-codex-auth",
            "goal": "confirm Codex auth/image provider can connect at all",
            "request": {
                "prompt": "Provider smoke test: one small neutral blue square icon on white.",
                "aspect_ratio": "square",
                "mode": "text_to_image",
                "review_only": True,
            },
        },
    ]
    if source_image:
        steps.append({
            "name": "single-image-source-derived",
            "goal": "confirm image input upload works",
            "request": {
                "prompt": "Provider smoke test: preserve the input subject identity while making a simple review-only image.",
                "aspect_ratio": "square",
                "mode": "source_derived_canon",
                "source_image": source_image,
                "output_background": "#FF00FF",
                "review_only": True,
            },
        })
    if source_image and style_assets:
        steps.append({
            "name": "multi-image-style-atlas-source-derived",
            "goal": "confirm ordered source plus style-reference payload works",
            "request": {
                "prompt": "Provider smoke test: preserve Image 1 identity; use additional images only as style references.",
                "aspect_ratio": "square",
                "mode": "source_derived_canon",
                "source_image": source_image,
                "lock_assets": style_assets,
                "output_background": "#FF00FF",
                "review_only": True,
            },
        })

    steps = steps[args.start_step - 1:]
    if not steps:
        raise SystemExit(f"start step {args.start_step} requires source/style inputs that were not provided")

    manifest: Dict[str, Any] = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "provider": args.provider,
        "start_step": args.start_step,
        "status": "running",
        "providerOutputs": 0,
        "steps": [],
    }
    for step in steps:
        result = _run_step(provider, step)
        manifest["steps"].append(result)
        manifest["providerOutputs"] += result["providerOutputs"]
        if not result["success"] and not args.continue_on_failure:
            manifest["status"] = "failed"
            manifest["failed_step"] = result["name"]
            manifest["failure_class"] = result.get("failure_class")
            manifest["error_type"] = result.get("error_type")
            break
    else:
        manifest["status"] = "passed" if all(step["success"] for step in manifest["steps"]) else "failed"

    output = Path(args.output).expanduser()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(manifest, indent=2))
    return 0 if manifest["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
