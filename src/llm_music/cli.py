"""Command-line interface: `llm-music run` (single) and `llm-music batch` (matrix)."""

from __future__ import annotations

import argparse
import json
import logging
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from .generate import generate_piece
from .models import get_client, list_models
from .modes import MODES
from .store import append_result, open_batch, write_manifest


def _timestamp() -> str:
    return time.strftime("%Y%m%d_%H%M%S", time.localtime())


def _split(csv: str) -> list[str]:
    return [x.strip() for x in csv.split(",") if x.strip()]


def _run_matrix(models: list[str], prompts: list[str], mode: str, max_attempts: int,
                samples: int = 1, workers: int = 6, bake_audio: bool = True,
                independent_description: bool = False, resume: Path | None = None):
    # The batch folder + manifest are created up front and rewritten after every
    # piece, so an interrupted run still leaves a valid, viewable partial batch —
    # and `resume` continues one: cells already recorded in its manifest (ok OR
    # failed-after-retries; both are results) are skipped, missing ones run.
    if resume:
        manifest = json.loads((resume / "data.json").read_text(encoding="utf-8"))
        ts, batch = manifest["timestamp"], resume
        entries = list(manifest["pieces"])
        done = {(e["model"], e["prompt"], e.get("sample", 0)) for e in entries}
        print(f"  → resuming {batch}: {len(done)} pieces already recorded")
    else:
        ts = _timestamp()
        batch = open_batch(ts, models, prompts)
        entries, done = [], set()
        print(f"  → writing to {batch}")

    # Generation is network-bound API calls, so we fan out across independent cells
    # with a thread pool. Clients are created once per model and shared (the SDKs are
    # thread-safe for concurrent requests). Cells are ordered sample-major so the
    # first `len(models)` in flight hit *different* providers — spreads rate limits.
    clients = {m: get_client(m) for m in models}
    cells = [(m, p, s) for s in range(samples) for p in prompts for m in models
             if (m, p, s) not in done]
    total = len(cells)
    results = []
    lock = threading.Lock()

    with tempfile.TemporaryDirectory(prefix="llm_music_batch_") as scratch:
        def work_cell(cell):
            m, p, s = cell
            wd = Path(scratch) / m / p / str(s)
            return cell, generate_piece(clients[m], p, mode, wd,
                                        max_attempts=max_attempts, bake_audio=bake_audio,
                                        independent_description=independent_description)

        with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
            for fut in as_completed([ex.submit(work_cell, c) for c in cells]):
                (m, p, s), r = fut.result()
                with lock:
                    results.append(r)
                    entries.append(append_result(batch, r, sample=s))
                    write_manifest(batch, ts, models, prompts, entries)
                    n = len(results)
                tag = f" #{s + 1}" if samples > 1 else ""
                info = (f"ok ({r.attempts} att): {r.title!r}" if r.ok
                        else f"FAILED after {r.attempts}: {r.error}")
                print(f"  [{n}/{total}] {m} × {p}{tag} … {info}", flush=True)
    return batch, results


def cmd_run(args) -> int:
    models, prompts = [args.model], [args.prompt]
    batch, results = _run_matrix(models, prompts, args.mode, args.max_attempts,
                                 independent_description=args.independent_description)
    print(f"\nWrote batch: {batch}")
    return 0 if all(r.ok and not r.independent_description_error for r in results) else 1


def cmd_batch(args) -> int:
    models, prompts = _split(args.models), _split(args.prompts)
    if not models or not prompts:
        print("error: --models and --prompts must be non-empty", file=sys.stderr)
        return 2
    n_cells = len(models) * len(prompts) * args.samples
    print(f"Batch: {len(models)} model(s) × {len(prompts)} prompt(s) × {args.samples} "
          f"sample(s) = {n_cells} [{args.mode}], {args.workers} workers")
    resume = Path(args.resume) if args.resume else None
    if resume and not (resume / "data.json").is_file():
        print(f"error: --resume {resume} has no data.json", file=sys.stderr)
        return 2
    batch, results = _run_matrix(models, prompts, args.mode, args.max_attempts,
                                 args.samples, args.workers, bake_audio=not args.no_audio,
                                 independent_description=args.independent_description,
                                 resume=resume)
    n_ok = sum(r.ok for r in results)
    n_description_failed = sum(bool(r.independent_description_error) for r in results)
    print(f"\nWrote batch: {batch}  ({n_ok}/{len(results)} succeeded)")
    if n_description_failed:
        print(f"warning: {n_description_failed} independent description(s) failed")
    return 0 if n_ok == len(results) and not n_description_failed else 1


def cmd_models(_args) -> int:
    print("Registered models:")
    for name in list_models():
        print(f"  {name}")
    return 0


def cmd_redescribe(args) -> int:
    """Apply the same music-only description call to an existing batch."""
    from .describe import describe_music, install_description, music_from_entry

    batch = Path(args.batch)
    manifest_path = batch / "data.json"
    if not manifest_path.is_file():
        print(f"error: no data.json under {batch}", file=sys.stderr)
        return 2
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    selected = []
    for index, entry in enumerate(manifest.get("pieces", [])):
        if not entry.get("ok"):
            continue
        if args.author and entry.get("model") != args.author:
            continue
        if args.prompt and entry.get("prompt") != args.prompt:
            continue
        if args.sample is not None and entry.get("sample", 0) != args.sample:
            continue
        if entry.get("independent_description") and not args.force:
            continue
        selected.append((index, entry))
        if args.limit is not None and len(selected) >= args.limit:
            break
    if not selected:
        print("No matching undescribed pieces.")
        return 0

    clients = {}
    completed = failed = 0
    # Sequential processing plus a write after every response makes this safely
    # resumable without a separate checkpoint file.
    for index, entry in selected:
        model = args.model or entry["model"]
        try:
            music, representation = music_from_entry(entry, batch)
            if model not in clients:
                clients[model] = get_client(model)
            client = clients[model]
            result = describe_music(client, music, representation,
                                    max_attempts=args.max_attempts)
        except Exception as exc:
            result = None
            entry["independent_description_error"] = str(exc)
        if result and result.ok:
            install_description(entry, result, model, representation)
            completed += 1
            status = "ok"
        else:
            if result:
                entry["independent_description_error"] = result.error
            failed += 1
            status = f"FAILED: {entry['independent_description_error']}"
        checkpoint = manifest_path.with_suffix(".json.tmp")
        checkpoint.write_text(
            json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        checkpoint.replace(manifest_path)
        print(f"  [{completed + failed}/{len(selected)}] piece {index} "
              f"{entry.get('model')} × {entry.get('prompt')} … {status}", flush=True)
    print(f"Updated {manifest_path}: {completed} described, {failed} failed")
    return 0 if not failed else 1


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="llm-music", description=__doc__)
    p.add_argument("-q", "--quiet", action="store_true",
                   help="only log warnings and errors")
    sub = p.add_subparsers(dest="command", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--mode", choices=list(MODES), default="codegen")
    common.add_argument("--max-attempts", type=int, default=5)
    common.add_argument("--independent-description", action="store_true",
                        help="replace the composing call's notes with a fresh, music-only "
                             "description from the same model (original notes are retained)")

    pr = sub.add_parser("run", parents=[common], help="generate one model × prompt")
    pr.add_argument("--model", required=True)
    pr.add_argument("--prompt", default="express-yourself")
    pr.set_defaults(func=cmd_run)

    pb = sub.add_parser("batch", parents=[common], help="generate a model × prompt matrix")
    pb.add_argument("--models", required=True, help="comma-separated friendly ids")
    pb.add_argument("--prompts", default="express-yourself",
                    help="comma-separated variant ids (see prompts/variants.csv)")
    pb.add_argument("--samples", type=int, default=1,
                    help="repeats per model×prompt cell (for sampling distributions)")
    pb.add_argument("--workers", type=int, default=6,
                    help="concurrent generations (network-bound; raise to go faster)")
    pb.add_argument("--no-audio", action="store_true",
                    help="skip audio baking (for large sampling runs)")
    pb.add_argument("--resume", default=None, metavar="BATCH_DIR",
                    help="continue an interrupted batch: skip cells already in its "
                         "data.json, generate only the missing ones (same flags + this)")
    pb.set_defaults(func=cmd_batch)

    pm = sub.add_parser("models", help="list registered models")
    pm.set_defaults(func=cmd_models)

    pd = sub.add_parser(
        "redescribe", help="generate fresh music-only descriptions for an existing batch"
    )
    pd.add_argument("batch", help="path to a data/<batch> folder")
    pd.add_argument("--model", default=None,
                    help="describer model (default: each piece's author model)")
    pd.add_argument("--author", default=None, help="restrict to one author model")
    pd.add_argument("--prompt", default=None, help="restrict to one generation prompt")
    pd.add_argument("--sample", type=int, default=None, help="restrict to one sample index")
    pd.add_argument("--limit", type=int, default=None, help="cap pieces for a pilot")
    pd.add_argument("--max-attempts", type=int, default=3)
    pd.add_argument("--force", action="store_true",
                    help="regenerate descriptions that already have this treatment")
    pd.set_defaults(func=cmd_redescribe)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.WARNING if args.quiet else logging.INFO,
                        format="%(levelname)s %(name)s: %(message)s")
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
