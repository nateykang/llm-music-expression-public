#!/usr/bin/env python3
"""Build data/browse.json — the slim piece index the static viewer loads.

Full batch manifests carry per-attempt error trails, failed-attempt code, and
a copy of the (identical) prompt text for every piece; the viewer shows none
of that, and at corpus scale (thousands of pieces) fetching every manifest
would move tens of megabytes per page load. This script extracts only the
fields the browse UI displays, deduplicates the prompt texts (one per mode)
and system prompts (one per prompt id), and records only artifact paths that
actually exist on disk — so the published site never offers a file it can't
serve.

Run it after adding or changing batches under data/:

    python scripts/build_browse_index.py
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def build(base: Path) -> dict:
    prompt_labels: dict = {}
    system_prompts: dict = {}
    prompt_texts: dict = {}
    pieces: list = []
    batches = sorted((p for p in base.iterdir() if (p / "data.json").is_file()),
                     key=lambda p: p.name, reverse=True)
    for batch in batches:
        manifest = json.loads((batch / "data.json").read_text(encoding="utf-8"))
        for entry in manifest.get("pieces", []):
            if not entry.get("ok"):
                continue
            prompt = entry["prompt"]
            mode = entry.get("mode", "")
            prompt_labels[prompt] = entry.get("prompt_label") or prompt
            if entry.get("system_prompt"):
                system_prompts.setdefault(prompt, entry["system_prompt"])
            if entry.get("prompt_text"):
                prompt_texts.setdefault(mode, entry["prompt_text"])
            rec = {
                "dir": f"{base.name}/{batch.name}",
                "model": entry["model"],
                "prompt": prompt,
                "mode": mode,
                "sample": entry.get("sample", 0),
                "title": entry.get("title", ""),
                "short": entry.get("short_description", ""),
                "long": entry.get("long_description", ""),
            }
            if entry.get("abc"):
                rec["abc"] = entry["abc"]
            for key in ("score", "midi", "audio"):
                rel = entry.get(key)
                if rel and (batch / rel).is_file():
                    rec[key] = rel
            pieces.append(rec)
    return {
        "prompt_labels": prompt_labels,
        "system_prompts": system_prompts,
        "prompt_texts": prompt_texts,
        "pieces": pieces,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--data-dir", default="data",
                    help="folder holding the batch subfolders (default: data)")
    args = ap.parse_args()
    base = Path(args.data_dir)
    if not base.is_dir():
        print(f"error: no such data dir {base}")
        return 2
    index = build(base)
    out = base / "browse.json"
    out.write_text(json.dumps(index, ensure_ascii=False), encoding="utf-8")
    n_batches = len({p["dir"] for p in index["pieces"]})
    print(f"Wrote {out}: {len(index['pieces'])} pieces from {n_batches} batches "
          f"({out.stat().st_size / 1e6:.1f} MB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
