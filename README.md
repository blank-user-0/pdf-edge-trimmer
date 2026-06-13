# PDF Edge Trimmer

PDF Edge Trimmer is a local browser app and command-line tool for trimming top,
bottom, left, and right PDF page edges. It runs locally on `127.0.0.1`; no PDF is
uploaded to a web service.

## Features

- Trim top, bottom, left, and right edges in inches.
- Preview cut lines before generating output.
- Choose one of three output modes:
  - `compressed`: permanent small-file output, rebuilt as JPEG page images.
  - `redact`: permanent redaction, keeps remaining PDF objects but can get large.
  - `crop`: visual crop only, smallest/fastest but hidden content can remain.
- Optional `Both side edges` field to set left and right together.
- CLI, local browser UI, and macOS app launcher build script.

## Install

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e .
```

## Run The Browser App

```bash
pdf-edge-trimmer --web
```

Or, from source without installing:

```bash
PYTHONPATH=src python3 -m pdf_edge_trimmer --web
```

## Build The macOS Launcher

The launcher starts the local server in the background and opens the browser
without showing a Terminal window.

```bash
scripts/build_macos_app.sh
open "dist/PDF Edge Trimmer.app"
```

Logs are written to `logs/pdf-edge-trimmer.log`. Generated PDFs default to
`output/`.

## CLI Examples

Permanent compressed output, close to original file size:

```bash
pdf-edge-trimmer \
  --input input.pdf \
  --output output.pdf \
  --top-inches 0.5 \
  --bottom-inches 0.5 \
  --left-inches 0.5 \
  --right-inches 0.5 \
  --mode compressed \
  --jpeg-quality 60 \
  --overwrite
```

Permanent redaction:

```bash
pdf-edge-trimmer --input input.pdf --output output.pdf --bottom-inches 0.5 --mode redact
```

Visual crop only:

```bash
pdf-edge-trimmer --input input.pdf --output output.pdf --bottom-inches 0.5 --mode crop
```

## Validation

```bash
PYTHONPATH=src python3 -m unittest discover -s tests
```

## File Size And Quality

See [docs/file-size-and-quality.md](docs/file-size-and-quality.md).

The default `compressed` mode is the practical choice when removed edges should
not be recoverable but the output needs to stay near the original size. It does
make pages image-only, so searchable/OCR text is lost in the generated PDF.

## GitHub Publishing

This folder is designed to be its own repository:

```bash
git init
git add .
git commit -m "Create PDF Edge Trimmer app"
git branch -M main
git remote add origin git@github.com:YOUR-USER/pdf-edge-trimmer.git
git push -u origin main
```

Create the GitHub repository first, then replace `YOUR-USER` with your GitHub
username or organization.

## Legal Notice

Use this tool only for lawful formatting and educational workflows. Do not use it
to remove required copyright, license, attribution, or safety notices.
