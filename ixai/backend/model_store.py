"""Active checkpoint management and hot reload for inference requests."""

import os
import threading
from pathlib import Path

import torch

from . import database
from .config import DEFAULT_MODEL_CONFIG, MODEL_DIR
from .model import TinyTransformer


ACTIVE_PATH = MODEL_DIR / "active.pt"
_lock = threading.RLock()
_model = None
_device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
_loaded_mtime = None
_loaded_config = None


def atomic_save(payload: dict, destination: Path):
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    torch.save(payload, temporary)
    os.replace(temporary, destination)


def initialize_model():
    """Create a saved, untrained baseline so chat uses the local network immediately."""
    global _model, _loaded_mtime, _loaded_config
    with _lock:
        if not ACTIVE_PATH.exists():
            with database.connect() as connection:
                row = connection.execute("SELECT checkpoint_path FROM model_versions WHERE active=1 LIMIT 1").fetchone()
            saved_path = Path(row["checkpoint_path"]) if row else None
            if saved_path and saved_path.exists():
                atomic_save(torch.load(saved_path, map_location="cpu", weights_only=False), ACTIVE_PATH)
            else:
                model = TinyTransformer(DEFAULT_MODEL_CONFIG)
                atomic_save({"config": DEFAULT_MODEL_CONFIG, "model_state": model.state_dict(), "version": "v0000", "trained": False}, ACTIVE_PATH)
        initial = torch.load(ACTIVE_PATH, map_location="cpu", weights_only=False)
        initial_version = initial.get("version", "v0000")
        immutable_path = MODEL_DIR / "versions" / f"{initial_version}.pt"
        if not immutable_path.exists():
            atomic_save(initial, immutable_path)
        with database.transaction() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO model_versions(version, checkpoint_path, validation_loss, active) VALUES(?,?,?,1)",
                (initial_version, str(immutable_path), 1_000_000.0),
            )
            connection.execute("UPDATE model_versions SET active=0")
            connection.execute("UPDATE model_versions SET checkpoint_path=?,active=1 WHERE version=?", (str(immutable_path), initial_version))
        checkpoint = torch.load(ACTIVE_PATH, map_location="cpu", weights_only=False)
        _model = TinyTransformer(checkpoint.get("config", DEFAULT_MODEL_CONFIG)).to(_device)
        _model.load_state_dict(checkpoint["model_state"])
        _model.eval()
        _loaded_mtime = ACTIVE_PATH.stat().st_mtime_ns
        _loaded_config = checkpoint.get("config", DEFAULT_MODEL_CONFIG)
        return {"version": checkpoint.get("version", "v0000"), "trained": bool(checkpoint.get("trained", False)), "parameters": _model.parameter_count, "device": str(_device)}


def get_model():
    global _model, _loaded_mtime, _loaded_config
    with _lock:
        mtime = ACTIVE_PATH.stat().st_mtime_ns
        if _model is None or _loaded_mtime != mtime:
            checkpoint = torch.load(ACTIVE_PATH, map_location="cpu", weights_only=False)
            _loaded_config = checkpoint.get("config", DEFAULT_MODEL_CONFIG)
            _model = TinyTransformer(_loaded_config).to(_device)
            _model.load_state_dict(checkpoint["model_state"])
            _model.eval()
            _loaded_mtime = mtime
        return _model, _device, _loaded_config


def active_metadata() -> dict:
    checkpoint = torch.load(ACTIVE_PATH, map_location="cpu", weights_only=False)
    config = checkpoint.get("config", DEFAULT_MODEL_CONFIG)
    width = int(config["n_embd"])
    layers = int(config["n_layer"])
    parameter_count = int(config["vocab_size"]) * width + layers * (12 * width * width + 13 * width) + 2 * width
    return {
        "version": checkpoint.get("version", "v0000"),
        "trained": bool(checkpoint.get("trained", False)),
        "parameters": parameter_count,
        "device": str(_device),
    }
