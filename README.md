# Clinical Scribe

Clinical Scribe is a local-first, multilingual clinical documentation assistant for Windows. Phase 3 adds recoverable CPU speaker diarization, audited Doctor/Patient assignment, per-segment language metadata, and optional local English translation without replacing source text.

The clinical pipeline is:

`local audio -> resumable chunks -> API -> SQLite job -> worker -> preprocess -> transcribe -> diarize -> preserve language -> interpret examination -> extract facts -> validate claims -> clerking sheet -> clinical note -> treatment plan -> optional differential -> clinician review -> export`

No heavy AI task runs in an API handler. Original audio and transcripts are immutable. Notes are generated from validated structured facts through a Medical Clerking Sheet, never directly from a transcript. Export requires clinician approval. Differential diagnosis is off by default and cannot modify the note. GitHub pushes are manual, approved-note-only, and exclude audio.

## Folder structure

```text
api/                     FastAPI application boundary
config/                  Validated non-secret settings
core/
  audio/                 Audio processing contracts
  transcription/         Swappable TranscriptionEngine
  diarization/           Speaker assignment and correction
  languages/             Language metadata and optional local translation
  multilingual/          Compatibility imports for the original package name
  examination/           Clinical Examination Interpreter
  extraction/            Structured fact extraction
  validation/            Hallucination firewall
  clerking/              Medical Clerking Sheet generation
  note_generation/       Swappable NoteGenerationEngine
  treatment/             Traceable treatment plan
  diagnosis/             Optional differential engine
  github/                Manual GitHub export boundary
storage/                 SQLite initializer (runtime data ignored)
worker/                  Separate heavy-AI process
tests/                   Smoke and safety tests
docs/architecture/       Architecture decision records
scripts/                 Windows setup helpers
```

## Exact Windows PowerShell setup

Run these commands in PowerShell. The first form uses the checked-in bootstrap script:

```powershell
Set-Location -LiteralPath 'C:\ClinicalScribe'
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\scripts\bootstrap.ps1
```

Equivalent manual commands, using the Python runtime available on this workstation:

```powershell
Set-Location -LiteralPath 'C:\ClinicalScribe'
$Python = 'C:\Users\60162\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
& $Python -m venv .venv
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e '.[dev,transcription]'
Copy-Item -LiteralPath '.env.example' -Destination '.env' -ErrorAction SilentlyContinue
python .\scripts\init_db.py
python -m pytest
```

Start the API:

```powershell
Set-Location -LiteralPath 'C:\ClinicalScribe'
.\.venv\Scripts\Activate.ps1
python -m uvicorn api.main:app --host 127.0.0.1 --port 8000
```

In a second PowerShell window, start the worker:

```powershell
Set-Location -LiteralPath 'C:\ClinicalScribe'
.\.venv\Scripts\Activate.ps1
python -m worker.main
```

Confirm the API health endpoint:

```powershell
Invoke-RestMethod -Method Get -Uri 'http://127.0.0.1:8000/health'
```

Open `http://127.0.0.1:8000/` for recording and speaker/language review. Microphone chunks are first stored in browser IndexedDB. They remain there through a network interruption and are removed only after the server reports a successfully assembled original recording.

When assembly completes, the API creates one durable SQLite `TRANSCRIPTION` job and returns without running AI. Start `python -m worker.main` separately to process it. The default BALANCED profile uses Faster Whisper `small` on CPU with int8 compute; FAST uses `tiny`, while ACCURATE uses `medium`. The first use may download model weights into the local Faster Whisper cache.

The worker decodes a derived 16 kHz mono copy, trims edge silence, peak-normalises it, detects language for every timestamped segment, and stores both canonical raw JSON and an immutable database row. It then queues a separate durable `DIARIZATION` job. The default `cpu-acoustic-v1` engine assigns `SPEAKER_00`/`SPEAKER_01` without guessing clinical roles. The review UI lets a clinician assign Doctor/Patient/Other, rename or merge voices, and reassign individual segments. Every correction is appended as a new revision.

After diarization, a separate durable `STRUCTURING` job extracts assertion-aware `ClinicalFact` records and generates the typed Medical Clerking Sheet. No transcript text is sent directly to note generation. Each displayed value is linked to its supporting fact and checked by the DO-NOT-INFER validator; missing fields display `Not discussed.`, while absent examination documentation displays `Not performed`. The session enters review only after the worker records `STRUCTURE_COMPLETE` and then `CLERKING_SHEET_GENERATED`.

Four immutable artefact streams are maintained: `RAW_TRANSCRIPT`, `CLEAN_TRANSCRIPT`, `TRANSLATED_TRANSCRIPT`, and `SPEAKER_LABELLED_TRANSCRIPT`. The translated artefact records an English target and per-segment translation status; when optional translation is disabled, non-English source is retained with `NOT_REQUESTED` rather than being presented as English. Enable translation only with a local model directory via `multilingual.translation_enabled` and `multilingual.local_model_path`; the adapter prevents network model downloads. Install `.[translation]` only when this feature is needed.

### Recording API

- `POST /api/v1/sessions` creates a session. The JSON body and every patient field are optional.
- `POST /api/v1/sessions/{id}/chunks?sequence_number=0&is_final=false` accepts raw chunk bytes. Send the lowercase or uppercase SHA-256 digest in `X-Chunk-SHA256`; retries with the same position and digest are idempotent.
- `GET /api/v1/sessions/{id}/upload` returns received positions, known missing positions, final sequence metadata, and assembly status.
- `GET /api/v1/sessions/{id}/speaker-review` returns the current reviewed projection with source text, optional English text, language metadata, speakers, roles, and assignment confidence.
- `POST /api/v1/sessions/{id}/speaker-corrections` appends audited speaker role/name/merge and per-segment assignment revisions, then creates a new speaker-labelled transcript artefact.
- `GET /api/v1/sessions/{id}/clerking-sheet` returns the latest typed, evidence-linked Medical Clerking Sheet for review.

The server assembles only a complete contiguous sequence and never overwrites an existing chunk or assembled original. A final chunk may arrive before missing chunks; uploading those missing chunks later automatically completes assembly.

## Git initialization and first push

First set `github.repository_url` in `config/config.yaml` to the non-secret HTTPS repository URL. Never put a token in that file or in the Git remote URL.

```powershell
Set-Location -LiteralPath 'C:\ClinicalScribe'
git init -b main
git add --all
git commit -m 'chore: establish Clinical Scribe foundation'
$RepoUrl = .\.venv\Scripts\python.exe -c "from config import load_config; print(load_config().github.repository_url)"
if ([string]::IsNullOrWhiteSpace($RepoUrl)) { throw 'Set github.repository_url in config/config.yaml before pushing.' }
git remote add origin $RepoUrl
$Token = $env:CLINICAL_SCRIBE_GITHUB_TOKEN
if ([string]::IsNullOrWhiteSpace($Token)) { throw 'Set CLINICAL_SCRIBE_GITHUB_TOKEN for this PowerShell session.' }
$Basic = [Convert]::ToBase64String([Text.Encoding]::ASCII.GetBytes("x-access-token:$Token"))
git -c "http.extraHeader=Authorization: Basic $Basic" push --set-upstream origin main
Remove-Variable Token, Basic
```

Set the token for the current PowerShell process only:

```powershell
$env:CLINICAL_SCRIBE_GITHUB_TOKEN = Read-Host 'GitHub fine-grained PAT' -MaskInput
```

The `.env` file is ignored and is not required for Git itself. Token values must never be committed, placed in YAML, written into a remote URL, or logged.

## Configuration and data safety

- `config/config.yaml` contains non-secret operational settings.
- `.env.example` is a template. The real `.env` is ignored.
- `storage/`, database files, audio formats, logs, caches, and virtual environments are ignored.
- Runtime source artefacts use append-only/versioned rows; SQLite triggers reject update/delete operations on audio chunks, transcripts, structured facts, and clerking sheets.
- Raw transcript JSON filenames include their content checksum; the database stores that SHA-256 for integrity verification.
- Structured clinical facts require one of `POSITIVE`, `NEGATIVE`, `NOT_MENTIONED`, or `UNCERTAIN`.
- Validation findings require `SUPPORTED`, `UNSUPPORTED`, `CONTRADICTED`, or `UNCERTAIN`.

## Verification

```powershell
Set-Location -LiteralPath 'C:\ClinicalScribe'
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\python.exe .\scripts\init_db.py
git status --short
git log -1 --oneline
git remote -v
```
