# LLM Musical Self-Expression — Generation Pipeline

**[▶ Browse the corpus](https://nateykang.github.io/REPO-NAME/)** — every piece's
sheet music and audio, rendered live in your browser (nothing to install).
<!-- TODO: replace REPO-NAME with the final repository name when Pages is enabled -->

*How do LLMs express themselves musically?* This repository contains the data-generation
pipeline used for the paper: it prompts language models to compose music in symbolic
formats, renders their pieces to MusicXML/MIDI/MP3, and bakes everything into
self-contained batch folders with a JSON manifest.

The experimental design descends from
[sara-fish/llm-musical-self-expression](https://github.com/sara-fish/llm-musical-self-expression).

## Design

Each generation cell is **model × prompt variant × mode**:

- **Prompt variant** (`prompts/variants.csv`) — a one-line self-expression instruction
  ("Write music that you think best expresses yourself.", …) placed in the **system
  prompt** (`You are a composer. {variant}`). The user prompt (`prompts/prompt.md` +
  the mode's output contract) is fixed across variants, so the variant is the only
  thing that changes between arms.
- **Mode** — the symbolic format the model composes in:
  - `codegen` — the model writes Python that builds a `music21` Score. The code runs in
    an **isolated subprocess sandbox** (CPU/memory limits, wall-clock timeout, scrubbed
    environment; see `src/llm_music/sandbox.py`), which exports MusicXML and derives
    MIDI from it so audio always matches the engraving.
  - `abc` — the model writes ABC notation directly. No code execution; the raw ABC is
    the stored artifact.
- **Model** — a friendly id from the registry (`src/llm_music/models/registry.py`),
  which maps ids to provider adapters (Anthropic, OpenAI, or OpenRouter for everything
  else). Thinking/non-thinking arms of the same model are separate registry entries
  with the reasoning configuration pinned explicitly.

Failed attempts are retried with the model shown its previous error (up to
`--max-attempts`, default 5). Provider backpressure (HTTP 429/529, transport failures)
is retried separately and **never counted against the model's attempts**, so `attempts`
stays a clean model-reliability covariate.

Optionally, `--independent-description` (or `llm-music redescribe` after the fact) asks
the same model to describe its piece in a **fresh, stateless call** that sees only the
scrubbed score — titles, comments, and credits from the composing call are removed —
for comparing en-route vs. post-hoc self-descriptions.

## Setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

cp .env.example .env          # add keys for the providers you use

# optional — pre-baked MP3 audio (scores render without it):
bash scripts/setup_soundfont.sh   # FluidSynth + a General MIDI SoundFont
brew install abcmidi lame         # abc2midi (ABC modes) + MP3 encoding
```

Without the audio tools the pipeline still produces scores and manifests; only the
pre-baked audio files are skipped.

## Usage

```bash
llm-music models                                   # list registered model ids

# one piece
llm-music run --model opus-4.8 --prompt express-yourself --mode abc

# a full matrix, with per-cell repeats
llm-music batch --models opus-4.8,gpt-5.5,gemini-2.5-pro \
                --prompts express-yourself,uniquely-you,emotional-state \
                --mode codegen --samples 3 --workers 6

# continue an interrupted batch (already-recorded cells are skipped)
llm-music batch --models ... --prompts ... --mode codegen --samples 3 \
                --resume data/<batch-folder>

# post-hoc music-only descriptions for an existing batch
llm-music redescribe data/<batch-folder>
```

## Output format

Each run writes `data/<timestamp>__models_N_prompts_M/`:

```
data.json                          # manifest: one entry per piece
scores/<prompt>/<model>[_sN].musicxml
audio/<prompt>/<model>[_sN].mp3    # when audio baking is available
```

A manifest entry records the full provenance of a piece: model, prompt id/label/text,
system prompt, mode, sample index, title and the model's own descriptions, the raw
artifact (`abc` or music21 `code`), attempt count, per-attempt error/code trail for
failed attempts, and (optionally) the independent description with its scrubbed-input
method. The manifest is rewritten after every piece, so interrupted runs are always
valid and resumable.

## Adding a model

Append one line to `MODEL_REGISTRY` in
[`src/llm_music/models/registry.py`](src/llm_music/models/registry.py) mapping a
friendly id to `(provider, provider_model_id[, options])`. Adding a new *provider*
means implementing the two-method `LLMClient` protocol
(`src/llm_music/models/base.py`) and extending `_build_client`.

## Tests

```bash
pytest
```

The tests exercise prompt assembly, response parsing, the sandbox boundary, retry
accounting, batch resume, and the description scrubbers — all offline (no API keys
needed).
