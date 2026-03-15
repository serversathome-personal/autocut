# AutoCut

A Windows GUI tool for automatically removing silence from OBS Studio screen recordings, producing FCPXML timeline files for DaVinci Resolve.

## Features

- **Silence removal** powered by [auto-editor](https://github.com/WyattBlue/auto-editor)
- **FCPXML export** for DaVinci Resolve (free version compatible)
- **Batch processing** — select multiple clips, process them all at once
- **Project folder workflow** — creates a folder in `~/Videos/`, moves clips into it, and generates a combined timeline
- **Presets** — Tutorial, Fast-paced, and Relaxed with adjustable threshold and margins
- **Drag and drop** support (with tkinterdnd2)

## Requirements

- Windows 10/11
- Python 3.10+
- [auto-editor](https://github.com/WyattBlue/auto-editor) v29+ (`auto-editor.exe` in the same folder as the script, or on PATH)
- FFmpeg (bundled with auto-editor)

## Usage

1. Place `auto-editor.exe` in the same folder as `autocut.pyw`
2. Double-click `autocut.pyw` or run: `pythonw autocut.pyw`
3. Select your OBS clips, pick a preset, and hit **Process**
4. Import the resulting `.fcpxml` files into DaVinci Resolve via **File → Import → Timeline**

## Presets

| Preset | Threshold | Margin Before | Margin After | Best For |
|---|---|---|---|---|
| Tutorial | 4% | 0.2s | 0.3s | Narrated walkthroughs |
| Fast-paced | 4% | 0.1s | 0.15s | Snappy edits |
| Relaxed | 3% | 0.3s | 0.5s | Conversational content |

## License

MIT
