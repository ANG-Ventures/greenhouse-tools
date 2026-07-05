#!/bin/sh
# Capture ACE-AI local-AI path SHAPE only. Copies no model bytes and no secrets.
set -eu
OUT="$(dirname "$0")/fixtures/ace_ai_shape/manifest.json"
mkdir -p "$(dirname "$OUT")"
python3 - "$OUT" <<'PY'
import json, os, pathlib, socket, sys, time
out = pathlib.Path(sys.argv[1])
home = pathlib.Path.home()
runners = {
    "ollama": home / ".ollama/models",
    "hf/mlx": home / ".cache/huggingface",
    "lmstudio": home / ".cache/lm-studio/models",
}
def shape(p):
    if p.is_symlink():
        return {"path": str(p), "kind": "symlink", "target": os.readlink(p)}
    if p.exists():
        return {"path": str(p), "kind": "dir" if p.is_dir() else "file", "nonempty": bool(p.is_dir() and any(p.iterdir()))}
    return {"path": str(p), "kind": "missing"}
manifest = {
    "schema": 1,
    "captured_by": "tools/pin_ai_models/capture_ace_ai_shape.sh",
    "captured_host": socket.gethostname(),
    "captured_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    "models_volume": "/Volumes/Models SSD 4TB",
    "models_root": "/Volumes/Models SSD 4TB/models",
    "models_root_basename": "Models SSD 4TB",
    "runners": {name: shape(path) for name, path in runners.items()},
}
out.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
print(out)
PY
