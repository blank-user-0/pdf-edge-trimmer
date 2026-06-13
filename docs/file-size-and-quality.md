# File Size And Quality

PDF Edge Trimmer supports three output modes because PDF size, removability,
searchability, and image quality trade off against each other.

## Modes

| Mode | File size | Removed area recoverable? | Searchable text | Notes |
|---|---:|---|---|---|
| Permanent compressed image | Usually near original size | No | No | Rebuilds pages as JPEG images. Best for small files. |
| Permanent redaction | Can be much larger | No | Usually yes for remaining text/OCR | Pixel edits can force large lossless image streams. |
| Visual crop only | Usually smallest | Yes | Yes | Only changes the page box; hidden content may remain. |

## Why Permanent Redaction Gets Large

Many scanned or publisher-built PDFs store each page as a highly compressed
JPEG-like image. When edge pixels are permanently changed, the PDF library may
rewrite that image as a larger lossless stream. A small visual trim can therefore
inflate the whole page image.

In local NEC sample testing:

- 2-page original sample: 477.5 KiB
- permanent compressed image, JPEG quality 60: 495.2 KiB
- permanent compressed image, JPEG quality 50: 435.3 KiB
- permanent redaction: 3298.0 KiB

## Recommended Defaults

Use `Permanent compressed image` with JPEG quality `60` when you need removed
edges to be unrecoverable while keeping file size close to the original. Increase
quality for sharper text, or decrease it for smaller files.
