from __future__ import annotations

import pathlib

from tools.pin_ai_models.pin_ai_models import DEFAULT_MODELS_ROOT, DEFAULT_RUNNERS, parse_runner_override, resolve_path


def test_default_runner_map_holds_real_ace_ai_values():
    by_name = {r.name: r for r in DEFAULT_RUNNERS}
    assert by_name["ollama"].source_default == "~/.ollama/models"
    assert by_name["hf/mlx"].source_default == "~/.cache/huggingface"
    assert by_name["lmstudio"].source_default == "~/.cache/lm-studio/models"
    assert pathlib.Path(DEFAULT_MODELS_ROOT).parent.name == "Models SSD 4TB"


def test_real_models_root_value_parses():
    resolved = resolve_path("/Volumes/Models SSD 4TB/models")
    assert resolved.name == "models"
    assert resolved.parent.name == "Models SSD 4TB"


def test_runner_override_parses_real_shape():
    runner = parse_runner_override("hf/mlx=~/.cache/huggingface=huggingface")
    assert runner.name == "hf/mlx"
    assert runner.source_default == "~/.cache/huggingface"
    assert runner.target_subdir == "huggingface"
