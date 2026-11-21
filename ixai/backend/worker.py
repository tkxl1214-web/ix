"""Dedicated, resumable training process. Run with `python -m backend.worker --job-id …`."""

import argparse
import json
import math
import os
import shutil
import traceback
from pathlib import Path

import torch
from torch.nn.utils import clip_grad_norm_

from . import database
from .model import TinyTransformer
from .model_store import ACTIVE_PATH, atomic_save
from .tokenizer import ByteTokenizer
from .config import MODEL_DIR


def load_texts(connection, dataset_ids):
    if not dataset_ids:
        return []
    placeholders = ",".join("?" for _ in dataset_ids)
    rows = connection.execute(
        f"SELECT id, content FROM datasets WHERE status='approved' AND consent=1 AND id IN ({placeholders}) ORDER BY id",
        dataset_ids,
    ).fetchall()
    return [row["content"] for row in rows]


def split_corpus(texts, block_size):
    """Hold out complete records; for a single record, hold out its final tenth."""
    encoded = [ByteTokenizer().encode(text) for text in texts if text.strip()]
    encoded = [tokens for tokens in encoded if len(tokens) >= 3]
    if not encoded:
        raise ValueError("승인된 데이터에 학습할 텍스트가 없습니다.")
    if len(encoded) > 1:
        validation = encoded[::5]
        training = encoded[1::5] + encoded[2::5] + encoded[3::5] + encoded[4::5]
        if not training:
            training, validation = encoded[1:], encoded[:1]
    else:
        tokens = encoded[0]
        cut = max(2, min(len(tokens) - 1, int(len(tokens) * 0.9)))
        training, validation = [tokens[:cut]], [tokens[cut - 1 :]]
    train_ids = torch.tensor([token for record in training for token in record], dtype=torch.long)
    val_ids = torch.tensor([token for record in validation for token in record], dtype=torch.long)
    return train_ids, val_ids


def batches(data, block_size, count, device):
    seq_len = min(block_size, max(1, data.numel() - 1))
    max_start = max(1, data.numel() - seq_len)
    starts = torch.linspace(0, max_start - 1, steps=count).long()
    pairs = [(data[start : start + seq_len], data[start + 1 : start + seq_len + 1]) for start in starts]
    xs, ys = zip(*pairs)
    x = torch.stack(xs).to(device)
    y = torch.stack(ys).to(device)
    return x, y


@torch.inference_mode()
def evaluate(model, data, block_size, device):
    model.eval()
    x, y = batches(data, block_size, min(8, max(1, data.numel() // max(1, block_size))), device)
    logits, loss = model(x, y)
    accuracy = (logits.argmax(dim=-1) == y).float().mean().item()
    return float(loss.item()), accuracy


def request_state(job_id):
    with database.connect() as connection:
        row = connection.execute("SELECT pause_requested, cancel_requested FROM training_jobs WHERE id=?", (job_id,)).fetchone()
    return (row["pause_requested"], row["cancel_requested"]) if row else (0, 1)


def update_job(job_id, **values):
    allowed = {
        "status", "progress", "epoch", "training_loss", "validation_loss", "baseline_loss",
        "validation_accuracy", "checkpoint_path", "candidate_path", "pause_requested",
        "cancel_requested", "error", "started_at", "finished_at",
    }
    values = {key: value for key, value in values.items() if key in allowed}
    if not values:
        return
    assignments = ",".join(f"{key}=CURRENT_TIMESTAMP" if value == "CURRENT_TIMESTAMP" else f"{key}=?" for key, value in values.items())
    parameters = tuple(value for value in values.values() if value != "CURRENT_TIMESTAMP")
    with database.transaction() as connection:
        connection.execute(f"UPDATE training_jobs SET {assignments} WHERE id=?", (*parameters, job_id))


def persist_checkpoint(path, model, optimizer, config, epoch, step, job_id, device):
    atomic_save({
        "config": config,
        "model_state": model.state_dict(),
        "optimizer_state": optimizer.state_dict(),
        "epoch": epoch,
        "step": step,
        "job_id": job_id,
        "device": str(device),
    }, path)


def promote_candidate(job_id, candidate_path, validation_loss):
    with database.transaction() as connection:
        current = connection.execute("SELECT COALESCE(MAX(CAST(SUBSTR(version,2) AS INTEGER)),0) FROM model_versions").fetchone()[0]
        version = f"v{int(current) + 1:04d}"
        version_path = MODEL_DIR / "versions" / f"{version}.pt"
        version_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(candidate_path, version_path)
        checkpoint = torch.load(version_path, map_location="cpu", weights_only=False)
        checkpoint["version"] = version
        checkpoint["trained"] = True
        atomic_save(checkpoint, version_path)
        atomic_save(checkpoint, ACTIVE_PATH)
        connection.execute("UPDATE model_versions SET active=0")
        connection.execute(
            "INSERT INTO model_versions(version, checkpoint_path, validation_loss, training_job_id, active) VALUES(?,?,?,?,1)",
            (version, str(version_path), validation_loss, job_id),
        )
    return version


def run(job_id: str, resume: bool = False):
    database.initialize()
    cpu_threads = max(1, min(4, os.cpu_count() or 1))
    torch.set_num_threads(cpu_threads)
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    with database.connect() as connection:
        job = connection.execute("SELECT * FROM training_jobs WHERE id=?", (job_id,)).fetchone()
        if not job:
            raise ValueError("학습 작업을 찾을 수 없습니다.")
        if job["status"] == "cancelled" or job["cancel_requested"]:
            return
        dataset_ids = json.loads(job["dataset_ids"])
        texts = load_texts(connection, dataset_ids)
    train_ids, val_ids = split_corpus(texts, 128)
    active = torch.load(ACTIVE_PATH, map_location="cpu", weights_only=False)
    config = active["config"]
    model = TinyTransformer(config).to(device)
    model.load_state_dict(active["model_state"])
    optimizer = torch.optim.AdamW(model.parameters(), lr=2e-4, weight_decay=0.02)
    candidate_path = MODEL_DIR / "jobs" / job_id / "candidate.pt"
    candidate_path.parent.mkdir(parents=True, exist_ok=True)
    start_epoch = 0
    global_step = 0
    if resume and job["checkpoint_path"] and Path(job["checkpoint_path"]).exists():
        saved = torch.load(job["checkpoint_path"], map_location=device, weights_only=False)
        if saved.get("job_id") == job_id:
            model.load_state_dict(saved["model_state"])
            if saved.get("optimizer_state"):
                optimizer.load_state_dict(saved["optimizer_state"])
            start_epoch = int(saved.get("epoch", 0))
            global_step = int(saved.get("step", 0))
    baseline_loss = job["baseline_loss"] if resume and job["baseline_loss"] is not None else evaluate(model, val_ids, config["block_size"], device)[0]
    update_job(job_id, status="running", started_at="CURRENT_TIMESTAMP", pause_requested=0, cancel_requested=0,
               baseline_loss=baseline_loss, checkpoint_path=str(candidate_path), candidate_path=str(candidate_path))
    epochs = int(job["total_epochs"])
    batch_size = 8 if device.type == "cpu" else 16
    # A modest cap keeps first-time CPU training responsive; each epoch still performs real optimizer steps.
    update_cap = max(4, min(120, int(os.getenv("TRAIN_UPDATES_PER_EPOCH", "24"))))
    updates_per_epoch = max(4, min(update_cap, math.ceil(train_ids.numel() / max(1, config["block_size"] * batch_size))))
    best_training_loss = None
    last_validation_loss = baseline_loss
    last_accuracy = 0.0

    for epoch in range(start_epoch, epochs):
        model.train()
        losses = []
        seq_len = min(config["block_size"], max(1, train_ids.numel() - 1))
        max_start = max(1, train_ids.numel() - seq_len)
        for step_in_epoch in range(updates_per_epoch):
            pause, cancel = request_state(job_id)
            if pause or cancel:
                persist_checkpoint(candidate_path, model, optimizer, config, epoch, global_step, job_id, device)
                update_job(job_id, status="cancelled" if cancel else "paused", checkpoint_path=str(candidate_path), finished_at="CURRENT_TIMESTAMP")
                return
            starts = torch.randint(0, max_start, (batch_size,))
            x = torch.stack([train_ids[s : s + seq_len] for s in starts]).to(device)
            y = torch.stack([train_ids[s + 1 : s + seq_len + 1] for s in starts]).to(device)
            optimizer.zero_grad(set_to_none=True)
            _, loss = model(x, y)
            loss.backward()
            clip_grad_norm_(model.parameters(), 1.0)
            optimizer.step()
            losses.append(float(loss.detach().item()))
            global_step += 1
            best_training_loss = sum(losses) / len(losses)
            progress = min(0.98, (epoch + (step_in_epoch + 1) / updates_per_epoch) / epochs)
            update_job(job_id, progress=progress, epoch=epoch + 1, training_loss=best_training_loss)
            if step_in_epoch % 4 == 0:
                persist_checkpoint(candidate_path, model, optimizer, config, epoch, global_step, job_id, device)

        last_validation_loss, last_accuracy = evaluate(model, val_ids, config["block_size"], device)
        best_training_loss = sum(losses) / max(1, len(losses))
        with database.transaction() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO training_metrics(job_id,epoch,training_loss,validation_loss,validation_accuracy) VALUES(?,?,?,?,?)",
                (job_id, epoch + 1, best_training_loss, last_validation_loss, last_accuracy),
            )
        persist_checkpoint(candidate_path, model, optimizer, config, epoch + 1, global_step, job_id, device)
        update_job(job_id, progress=min(0.99, (epoch + 1) / epochs), epoch=epoch + 1, training_loss=best_training_loss,
                   validation_loss=last_validation_loss, validation_accuracy=last_accuracy)

    _, cancel = request_state(job_id)
    promoted_version = None
    if cancel:
        persist_checkpoint(candidate_path, model, optimizer, config, epochs, global_step, job_id, device)
        update_job(job_id, status="cancelled", checkpoint_path=str(candidate_path), finished_at="CURRENT_TIMESTAMP")
        return
    if last_validation_loss < baseline_loss:
        promoted_version = promote_candidate(job_id, candidate_path, last_validation_loss)
    update_job(job_id, status="completed", progress=1.0, validation_loss=last_validation_loss,
               validation_accuracy=last_accuracy, finished_at="CURRENT_TIMESTAMP")
    print(json.dumps({"job_id": job_id, "status": "completed", "baseline_loss": baseline_loss,
                      "validation_loss": last_validation_loss, "promoted_version": promoted_version}))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--job-id", required=True)
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()
    try:
        run(args.job_id, args.resume)
    except Exception as error:
        database.initialize()
        update_job(args.job_id, status="failed", error=f"{type(error).__name__}: {error}"[:1000], finished_at="CURRENT_TIMESTAMP")
        traceback.print_exc()
        raise


if __name__ == "__main__":
    main()
