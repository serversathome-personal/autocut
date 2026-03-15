#!/usr/bin/env python3
"""Auto-Editor GUI – Silence Remover for DaVinci Resolve workflows."""

import json
import logging
import os
import shutil
import subprocess
import sys
import threading
import tkinter as tk
import traceback
import xml.etree.ElementTree as ET
from datetime import date
from fractions import Fraction
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

# Crash log — captures errors even when launched via pythonw (no console)
_LOG_FILE = Path(__file__).resolve().parent / "auto-editor-gui.log"
logging.basicConfig(
    filename=str(_LOG_FILE),
    level=logging.DEBUG,
    format="%(asctime)s %(levelname)s %(message)s",
)

def _global_exc(exc_type, exc_value, exc_tb):
    msg = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
    logging.critical("Unhandled exception:\n%s", msg)
    try:
        messagebox.showerror("Fatal Error", msg)
    except Exception:
        pass
    sys.__excepthook__(exc_type, exc_value, exc_tb)

sys.excepthook = _global_exc

# DPI awareness (Windows)
try:
    import ctypes
    ctypes.windll.shcore.SetProcessDpiAwareness(1)
except Exception:
    pass

# Optional drag-and-drop
try:
    import tkinterdnd2
    HAS_DND = True
except ImportError:
    HAS_DND = False

# ---------------------------------------------------------------------------
# Paths & constants
# ---------------------------------------------------------------------------
SCRIPTS_DIR = Path(__file__).resolve().parent
AUTO_EDITOR_EXE = SCRIPTS_DIR / "auto-editor.exe"
CONFIG_FILE = SCRIPTS_DIR / "auto-editor-gui-config.json"
VIDEOS_DIR = Path.home() / "Videos"

PRESETS = {
    "Tutorial (default)": {"threshold": 4, "margin_before": 0.20, "margin_after": 0.30},
    "Fast-paced":         {"threshold": 4, "margin_before": 0.10, "margin_after": 0.15},
    "Relaxed":            {"threshold": 3, "margin_before": 0.30, "margin_after": 0.50},
}

VIDEO_EXTS = (
    ".mp4", ".mkv", ".mov", ".avi", ".wmv", ".flv", ".webm",
    ".m4v", ".ts", ".mts", ".m2ts",
)
AUDIO_EXTS = (".wav", ".mp3", ".flac", ".aac", ".ogg", ".m4a", ".opus")
ALL_MEDIA_EXTS = VIDEO_EXTS + AUDIO_EXTS


def which(name: str) -> str | None:
    local = SCRIPTS_DIR / name
    if local.is_file():
        return str(local)
    for d in os.environ.get("PATH", "").split(os.pathsep):
        p = Path(d) / name
        if p.is_file():
            return str(p)
    return None


def check_dependencies() -> list[str]:
    errors: list[str] = []
    if not which("ffmpeg.exe") and not which("ffmpeg"):
        errors.append(
            "ffmpeg not found on PATH.\n"
            "Install via:  winget install Gyan.FFmpeg\n"
            "or download from https://www.gyan.dev/ffmpeg/builds/"
        )
    if not AUTO_EDITOR_EXE.is_file():
        errors.append(
            f"auto-editor.exe not found at:\n{AUTO_EDITOR_EXE}\n\n"
            "Install via:  pip install auto-editor\n"
            "Then copy the binary to the scripts folder, or download from\n"
            "https://github.com/WyattBlue/auto-editor/releases"
        )
    return errors


# ---------------------------------------------------------------------------
# Timeline combiner — merges multiple FCPXML timelines into one
# ---------------------------------------------------------------------------
def _parse_fcpxml_time(s: str) -> Fraction:
    """Parse FCPXML rational time like '33/30s' or '0s' into a Fraction."""
    s = s.rstrip("s")
    if "/" in s:
        num, den = s.split("/")
        return Fraction(int(num), int(den))
    return Fraction(int(s))


def _fmt_fcpxml_time(frac: Fraction, timebase: int) -> str:
    """Format a Fraction as FCPXML rational time like '33/30s'."""
    frames = int(frac * timebase)
    if frames == 0:
        return "0s"
    return f"{frames}/{timebase}s"


def combine_timelines(xml_paths: list[str], output_path: str,
                      timeline_name: str) -> None:
    """Merge per-clip FCPXML timelines into a single sequential timeline.

    Each input is an FCPXML file produced by auto-editor --export resolve.
    This function places all clips end-to-end on one timeline spine.
    """
    if not xml_paths:
        return

    # ------------------------------------------------------------------
    # 1. Parse every individual FCPXML
    # ------------------------------------------------------------------
    parsed: list[dict] = []
    for xp in xml_paths:
        tree = ET.parse(xp)
        root = tree.getroot()

        resources = root.find("resources")
        fmt_el = resources.find("format")
        # Extract timebase from frameDuration (e.g. "1/30s" → 30)
        fd = fmt_el.get("frameDuration", "1/30s")
        timebase = int(fd.rstrip("s").split("/")[1]) if "/" in fd else 30

        assets = resources.findall("asset")
        spine = root.find(".//spine")
        clips = list(spine)  # all asset-clip elements

        # Calculate this timeline's total duration
        total = Fraction(0)
        for clip in clips:
            total += _parse_fcpxml_time(clip.get("duration", "0s"))

        parsed.append({
            "path": xp,
            "timebase": timebase,
            "format": fmt_el,
            "assets": assets,
            "clips": clips,
            "duration": total,
        })

    # ------------------------------------------------------------------
    # 2. Build combined FCPXML
    # ------------------------------------------------------------------
    base = parsed[0]
    timebase = base["timebase"]

    fcpxml = ET.Element("fcpxml", version="1.11")
    resources = ET.SubElement(fcpxml, "resources")

    # Add format from first file
    resources.append(base["format"])
    fmt_id = base["format"].get("id")

    # Collect all assets with unique IDs, and build ref mapping
    next_res_id = 2  # r1 is the format
    ref_map: dict[str, str] = {}  # (file_index, old_ref) → new_ref

    for fi, info in enumerate(parsed):
        for asset in info["assets"]:
            old_id = asset.get("id")
            new_id = f"r{next_res_id}"
            next_res_id += 1
            ref_map[(fi, old_id)] = new_id

            asset.set("id", new_id)
            asset.set("format", fmt_id)
            resources.append(asset)

    # Build library → event → project → sequence → spine
    library = ET.SubElement(fcpxml, "library")
    event = ET.SubElement(library, "event", name=timeline_name)
    project = ET.SubElement(event, "project", name=timeline_name)

    # Copy sequence attributes from first file
    first_seq = ET.parse(base["path"]).find(".//sequence")
    seq_attribs = dict(first_seq.attrib)
    sequence = ET.SubElement(project, "sequence", **seq_attribs)
    spine = ET.SubElement(sequence, "spine")

    # ------------------------------------------------------------------
    # 3. Place all clips sequentially on the spine
    # ------------------------------------------------------------------
    accumulated = Fraction(0)

    for fi, info in enumerate(parsed):
        for clip in info["clips"]:
            # Update offset to place after all previous clips
            clip_duration = _parse_fcpxml_time(clip.get("duration", "0s"))
            clip.set("offset", _fmt_fcpxml_time(accumulated, timebase))
            accumulated += clip_duration

            # Remap the asset reference
            old_ref = clip.get("ref")
            new_ref = ref_map.get((fi, old_ref), old_ref)
            clip.set("ref", new_ref)

            spine.append(clip)

    # ------------------------------------------------------------------
    # 4. Write combined FCPXML
    # ------------------------------------------------------------------
    tree = ET.ElementTree(fcpxml)
    ET.indent(tree, space="  ")
    tree.write(output_path, encoding="utf-8", xml_declaration=True)


# ---------------------------------------------------------------------------
# Main application
# ---------------------------------------------------------------------------
class App:
    def __init__(self) -> None:
        if HAS_DND:
            self.root = tkinterdnd2.Tk()
        else:
            self.root = tk.Tk()

        self.root.title("Auto-Editor \u2013 Silence Remover")
        self.root.minsize(700, 680)
        self.root.resizable(True, True)

        self._process: subprocess.Popen | None = None
        self._cancel_flag = False
        self._batch_files: list[str] = []
        self._output_dir: str = ""

        # --- Style ---------------------------------------------------------
        style = ttk.Style()
        try:
            style.theme_use("vista")
        except tk.TclError:
            try:
                style.theme_use("clam")
            except tk.TclError:
                pass

        # --- Variables -----------------------------------------------------
        self.var_files = tk.StringVar()
        self.var_project_folder = tk.BooleanVar(value=False)
        self.var_project_name = tk.StringVar()
        self.var_timeline = tk.StringVar()
        self.var_preset = tk.StringVar(value="Tutorial (default)")
        self.var_threshold = tk.IntVar(value=4)
        self.var_margin_before = tk.DoubleVar(value=0.20)
        self.var_margin_after = tk.DoubleVar(value=0.30)
        self.var_export_xml = tk.BooleanVar(value=True)
        self.var_output = tk.StringVar(value="")
        self.var_status = tk.StringVar(value="Ready")

        self._suppress_preset_change = False

        self._build_ui()
        self._load_config()

        errs = check_dependencies()
        if errs:
            messagebox.showerror("Missing Dependencies", "\n\n".join(errs))

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    # -----------------------------------------------------------------------
    # UI construction
    # -----------------------------------------------------------------------
    def _build_ui(self) -> None:
        pad = {"padx": 8, "pady": 4}
        main = ttk.Frame(self.root, padding=10)
        main.pack(fill="both", expand=True)

        # === Input section =================================================
        lf_input = ttk.LabelFrame(main, text="Input", padding=8)
        lf_input.pack(fill="x", **pad)

        row_file = ttk.Frame(lf_input)
        row_file.pack(fill="x", pady=2)
        ttk.Label(row_file, text="File(s):").pack(side="left")
        self.ent_files = ttk.Entry(row_file, textvariable=self.var_files, state="readonly")
        self.ent_files.pack(side="left", fill="x", expand=True, padx=(4, 4))
        ttk.Button(row_file, text="Browse\u2026", command=self._browse).pack(side="left")

        if HAS_DND:
            self.ent_files.drop_target_register(tkinterdnd2.DND_FILES)
            self.ent_files.dnd_bind("<<Drop>>", self._on_drop)

        # --- Project folder controls ---
        row_pf = ttk.Frame(lf_input)
        row_pf.pack(fill="x", pady=2)
        self.chk_project = ttk.Checkbutton(
            row_pf,
            text="Create project folder (move clips + output together)",
            variable=self.var_project_folder,
            command=self._on_project_toggle,
        )
        self.chk_project.pack(side="left")

        row_name = ttk.Frame(lf_input)
        row_name.pack(fill="x", pady=2)
        self.lbl_name = ttk.Label(row_name, text="Project name:")
        self.lbl_name.pack(side="left")
        self.ent_name = ttk.Entry(row_name, textvariable=self.var_project_name)
        self.ent_name.pack(side="left", fill="x", expand=True, padx=(4, 0))
        self.var_project_name.trace_add("write", lambda *_: self._update_output())

        row_path = ttk.Frame(lf_input)
        row_path.pack(fill="x", pady=(0, 2))
        self.lbl_project_path = ttk.Label(
            row_path, text="", foreground="gray", font=("", 8),
        )
        self.lbl_project_path.pack(side="left", padx=(90, 0))

        # Timeline name (single-file mode only)
        self.row_timeline = ttk.Frame(lf_input)
        self.row_timeline.pack(fill="x", pady=2)
        ttk.Label(self.row_timeline, text="Timeline Name:").pack(side="left")
        self.ent_timeline = ttk.Entry(self.row_timeline, textvariable=self.var_timeline)
        self.ent_timeline.pack(side="left", fill="x", expand=True, padx=(4, 0))

        self._on_project_toggle()

        # === Settings section ==============================================
        lf_settings = ttk.LabelFrame(main, text="Settings", padding=8)
        lf_settings.pack(fill="x", **pad)

        row_preset = ttk.Frame(lf_settings)
        row_preset.pack(fill="x", pady=2)
        ttk.Label(row_preset, text="Preset:").pack(side="left")
        self.cmb_preset = ttk.Combobox(
            row_preset,
            textvariable=self.var_preset,
            values=list(PRESETS.keys()) + ["Custom"],
            state="readonly",
            width=22,
        )
        self.cmb_preset.pack(side="left", padx=(4, 0))
        self.cmb_preset.bind("<<ComboboxSelected>>", self._on_preset_change)

        row_thresh = ttk.Frame(lf_settings)
        row_thresh.pack(fill="x", pady=2)
        ttk.Label(row_thresh, text="Audio threshold:").pack(side="left")
        self.scl_threshold = ttk.Scale(
            row_thresh, from_=1, to=20, variable=self.var_threshold,
            orient="horizontal", command=self._on_slider_move,
        )
        self.scl_threshold.pack(side="left", fill="x", expand=True, padx=(4, 4))
        self.lbl_thresh_val = ttk.Label(row_thresh, text="4%", width=4)
        self.lbl_thresh_val.pack(side="left")

        row_hint = ttk.Frame(lf_settings)
        row_hint.pack(fill="x")
        ttk.Label(
            row_hint,
            text="Lower = keeps quieter speech, Higher = cuts more",
            foreground="gray",
            font=("", 8),
        ).pack(side="left", padx=(90, 0))

        row_margins = ttk.Frame(lf_settings)
        row_margins.pack(fill="x", pady=2)
        ttk.Label(row_margins, text="Margin before (s):").pack(side="left")
        self.spn_before = ttk.Spinbox(
            row_margins, from_=0.0, to=2.0, increment=0.05,
            textvariable=self.var_margin_before, width=6,
            command=self._on_manual_change,
        )
        self.spn_before.pack(side="left", padx=(4, 16))
        ttk.Label(row_margins, text="Margin after (s):").pack(side="left")
        self.spn_after = ttk.Spinbox(
            row_margins, from_=0.0, to=2.0, increment=0.05,
            textvariable=self.var_margin_after, width=6,
            command=self._on_manual_change,
        )
        self.spn_after.pack(side="left", padx=(4, 0))
        self.spn_before.bind("<KeyRelease>", lambda _: self._on_manual_change())
        self.spn_after.bind("<KeyRelease>", lambda _: self._on_manual_change())

        row_export = ttk.Frame(lf_settings)
        row_export.pack(fill="x", pady=2)
        self.chk_xml = ttk.Checkbutton(
            row_export,
            text="Export XML for DaVinci Resolve",
            variable=self.var_export_xml,
            command=self._update_output,
        )
        self.chk_xml.pack(side="left")

        # === Output section ================================================
        lf_output = ttk.LabelFrame(main, text="Output", padding=8)
        lf_output.pack(fill="x", **pad)
        row_out = ttk.Frame(lf_output)
        row_out.pack(fill="x")
        ttk.Label(row_out, text="Output:").pack(side="left")
        self.ent_output = ttk.Entry(row_out, textvariable=self.var_output, state="readonly")
        self.ent_output.pack(side="left", fill="x", expand=True, padx=(4, 0))

        # === Action section ================================================
        lf_action = ttk.LabelFrame(main, text="Actions", padding=8)
        lf_action.pack(fill="both", expand=True, **pad)

        row_btns = ttk.Frame(lf_action)
        row_btns.pack(fill="x", pady=2)
        self.btn_process = ttk.Button(
            row_btns, text="Process", command=self._on_process,
        )
        self.btn_process.pack(side="left", padx=(0, 8))
        self.btn_copy = ttk.Button(
            row_btns, text="Copy Command", command=self._copy_command,
        )
        self.btn_copy.pack(side="left", padx=(0, 8))
        self.btn_open_folder = ttk.Button(
            row_btns, text="Open Output Folder", command=self._open_folder,
        )
        self.btn_open_folder.pack(side="left")
        self.btn_open_folder.state(["disabled"])

        self.txt_log = tk.Text(lf_action, height=10, wrap="word", font=("Consolas", 9))
        scrollbar = ttk.Scrollbar(lf_action, orient="vertical", command=self.txt_log.yview)
        self.txt_log.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        self.txt_log.pack(fill="both", expand=True, pady=(4, 0))

        # === Status bar ====================================================
        self.lbl_status = ttk.Label(
            self.root, textvariable=self.var_status, relief="sunken", padding=(6, 3),
        )
        self.lbl_status.pack(fill="x", side="bottom")

    # -----------------------------------------------------------------------
    # Project folder helpers
    # -----------------------------------------------------------------------
    def _get_project_dir(self) -> Path:
        name = self.var_project_name.get().strip() or date.today().isoformat()
        return VIDEOS_DIR / name

    def _on_project_toggle(self) -> None:
        pf = self.var_project_folder.get()
        if pf:
            self.ent_name.configure(state="normal")
            self.lbl_name.configure(text="Project name:")
            self.row_timeline.pack_forget()
        else:
            if len(self._batch_files) <= 1:
                self.ent_name.configure(state="disabled")
            self.row_timeline.pack(fill="x", pady=2)
        self._update_output()

    # -----------------------------------------------------------------------
    # File selection
    # -----------------------------------------------------------------------
    def _browse(self) -> None:
        ext_pairs = " ".join(f"*{e}" for e in ALL_MEDIA_EXTS)
        initial = str(VIDEOS_DIR) if VIDEOS_DIR.is_dir() else ""
        paths = filedialog.askopenfilenames(
            title="Select media file(s)",
            initialdir=initial,
            filetypes=[("Media files", ext_pairs), ("All files", "*.*")],
        )
        if not paths:
            return
        self._set_files(list(paths))

    def _on_drop(self, event) -> None:
        raw: str = event.data
        paths: list[str] = []
        if raw.startswith("{"):
            for part in raw.split("} {"):
                paths.append(part.strip("{}"))
        else:
            paths = raw.split()
        paths = [p for p in paths if Path(p).suffix.lower() in ALL_MEDIA_EXTS]
        if paths:
            self._set_files(paths)

    def _set_files(self, paths: list[str]) -> None:
        self._batch_files = paths
        if len(paths) == 1:
            self.var_files.set(paths[0])
            stem = Path(paths[0]).stem
            self.var_timeline.set(stem)
            self.ent_timeline.configure(state="normal")
            if not self.var_project_folder.get():
                self.var_project_name.set(stem)
        else:
            self.var_files.set(f"{len(paths)} files selected")
            self.var_timeline.set("")
            self.ent_timeline.configure(state="disabled")
            self.var_project_folder.set(True)
            self.var_project_name.set(date.today().isoformat())
        self._on_project_toggle()
        self._update_output()

    # -----------------------------------------------------------------------
    # Preset / settings logic
    # -----------------------------------------------------------------------
    def _on_preset_change(self, _event=None) -> None:
        name = self.var_preset.get()
        if name in PRESETS:
            p = PRESETS[name]
            self._suppress_preset_change = True
            self.var_threshold.set(p["threshold"])
            self.lbl_thresh_val.configure(text=f"{p['threshold']}%")
            self.var_margin_before.set(p["margin_before"])
            self.var_margin_after.set(p["margin_after"])
            self._suppress_preset_change = False

    def _on_slider_move(self, val_str: str) -> None:
        val = int(round(float(val_str)))
        self.var_threshold.set(val)
        self.lbl_thresh_val.configure(text=f"{val}%")
        if not self._suppress_preset_change:
            self.var_preset.set("Custom")

    def _on_manual_change(self) -> None:
        if not self._suppress_preset_change:
            self.var_preset.set("Custom")

    # -----------------------------------------------------------------------
    # Output path
    # -----------------------------------------------------------------------
    def _update_output(self) -> None:
        if not self._batch_files:
            self.var_output.set("")
            self.lbl_project_path.configure(text="")
            return

        ext = ".fcpxml" if self.var_export_xml.get() else ".mp4"
        use_pf = self.var_project_folder.get()

        if use_pf:
            proj = self._get_project_dir()
            pname = self.var_project_name.get().strip() or date.today().isoformat()
            self.lbl_project_path.configure(text=str(proj))
            n = len(self._batch_files)
            if n > 1 and self.var_export_xml.get():
                self.var_output.set(f"{proj / (pname + '.fcpxml')}  (combined timeline)")
            else:
                self.var_output.set(
                    f"{proj}  ({n} clip{'s' if n != 1 else ''} + output)"
                )
        else:
            self.lbl_project_path.configure(text="")
            if len(self._batch_files) == 1:
                p = Path(self._batch_files[0])
                self.var_output.set(str(p.with_name(p.stem + "_cut" + ext)))
            else:
                folder = str(Path(self._batch_files[0]).parent)
                self.var_output.set(
                    f"{folder}  ({len(self._batch_files)} output files)"
                )

    # -----------------------------------------------------------------------
    # Build command
    # -----------------------------------------------------------------------
    def _build_command(self, input_path: str, output_path: str | None = None) -> list[str]:
        threshold = self.var_threshold.get() / 100.0
        mb = self.var_margin_before.get()
        ma = self.var_margin_after.get()

        cmd = [str(AUTO_EDITOR_EXE)]
        cmd.append(input_path)
        cmd.extend(["--edit", f"audio:{threshold}"])
        cmd.extend(["--margin", f"{mb}s,{ma}s"])

        if self.var_export_xml.get():
            cmd.extend(["--export", "resolve"])

        if output_path:
            cmd.extend(["--output", output_path])

        cmd.append("--no-open")
        return cmd

    def _build_command_display(self) -> str:
        if not self._batch_files:
            return ""
        p = Path(self._batch_files[0])
        ext = ".fcpxml" if self.var_export_xml.get() else ".mp4"
        if self.var_project_folder.get():
            proj = self._get_project_dir()
            inp = proj / p.name
            out = proj / (p.stem + "_cut" + ext)
        else:
            inp = p
            out = p.with_name(p.stem + "_cut" + ext)
        cmd = self._build_command(str(inp), str(out))
        return " ".join(f'"{c}"' if " " in c else c for c in cmd)

    def _copy_command(self) -> None:
        cmd = self._build_command_display()
        if cmd:
            self.root.clipboard_clear()
            self.root.clipboard_append(cmd)
            self.var_status.set("Command copied to clipboard")

    # -----------------------------------------------------------------------
    # Process
    # -----------------------------------------------------------------------
    def _log(self, text: str) -> None:
        self.txt_log.insert("end", text)
        self.txt_log.see("end")

    def _on_process(self) -> None:
        if self._process is not None:
            self._cancel_flag = True
            if self._process.poll() is None:
                self._process.terminate()
            self.var_status.set("Cancelling\u2026")
            return

        if not self._batch_files:
            messagebox.showwarning("No Input", "Please select a media file first.")
            return

        errs = check_dependencies()
        if errs:
            messagebox.showerror("Missing Dependencies", "\n\n".join(errs))
            return

        if self.var_project_folder.get():
            name = self.var_project_name.get().strip()
            if not name:
                messagebox.showwarning("No Project Name", "Please enter a project name.")
                return
            bad = set(name) & set('<>:"/\\|?*')
            if bad:
                messagebox.showwarning(
                    "Invalid Name",
                    f"Project name contains invalid characters: {''.join(bad)}",
                )
                return

        self.txt_log.delete("1.0", "end")
        self._cancel_flag = False
        self.btn_process.configure(text="Cancel")
        self.btn_open_folder.state(["disabled"])
        threading.Thread(target=self._run_batch, daemon=True).start()

    def _run_batch(self) -> None:
        files = list(self._batch_files)
        total = len(files)
        all_ok = True
        use_pf = self.var_project_folder.get()
        is_xml = self.var_export_xml.get()
        individual_xmls: list[str] = []

        # --- Project folder: create and move clips -------------------------
        if use_pf:
            proj_dir = self._get_project_dir()
            self._output_dir = str(proj_dir)

            self.root.after(0, self._log, f"Creating project folder: {proj_dir}\n")
            try:
                proj_dir.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                self.root.after(0, self._log, f"[ERROR] Cannot create folder: {exc}\n")
                self.root.after(0, self.btn_process.configure, {"text": "Process"})
                self.root.after(0, self.var_status.set, "Error creating project folder")
                return

            moved_files: list[str] = []
            for fpath in files:
                src = Path(fpath)
                dst = proj_dir / src.name
                if src == dst:
                    moved_files.append(str(dst))
                    continue
                if dst.exists():
                    self.root.after(
                        0, self._log,
                        f"[SKIP] {src.name} already exists in project folder\n",
                    )
                    moved_files.append(str(dst))
                    continue
                try:
                    self.root.after(0, self._log, f"Moving: {src.name}\n")
                    shutil.move(str(src), str(dst))
                    moved_files.append(str(dst))
                except OSError as exc:
                    self.root.after(
                        0, self._log, f"[ERROR] Failed to move {src.name}: {exc}\n",
                    )
                    all_ok = False
                    moved_files.append(str(src))

            files = moved_files
            self.root.after(0, self._log, "\n")
        else:
            if files:
                self._output_dir = str(Path(files[0]).parent)

        # --- Process each file ---------------------------------------------
        for idx, fpath in enumerate(files, 1):
            if self._cancel_flag:
                self.root.after(0, self._log, "\n--- Cancelled by user ---\n")
                break

            p = Path(fpath)
            ext = ".fcpxml" if is_xml else ".mp4"

            if use_pf:
                out_path = str(proj_dir / (p.stem + "_cut" + ext))
            elif total == 1 and self.var_timeline.get().strip():
                out_path = str(p.with_name(
                    self.var_timeline.get().strip() + "_cut" + ext
                ))
            else:
                out_path = str(p.with_name(p.stem + "_cut" + ext))

            status = (
                f"Processing {idx}/{total}: {p.name}"
                if total > 1 else f"Processing: {p.name}"
            )
            self.root.after(0, self.var_status.set, status)

            cmd = self._build_command(str(p), out_path)
            cmd_str = " ".join(f'"{c}"' if " " in c else c for c in cmd)
            self.root.after(0, self._log, f"> {cmd_str}\n")

            try:
                self._process = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
                for raw_line in self._process.stdout:
                    line = raw_line.decode("utf-8", errors="replace")
                    self.root.after(0, self._log, line)
                self._process.wait()

                if self._process.returncode != 0:
                    self.root.after(
                        0, self._log,
                        f"\n[ERROR] auto-editor exited with code "
                        f"{self._process.returncode}\n\n",
                    )
                    all_ok = False
                else:
                    self.root.after(0, self._log, f"[OK] {Path(out_path).name}\n\n")
                    if is_xml:
                        individual_xmls.append(out_path)
            except Exception as exc:
                self.root.after(0, self._log, f"\n[ERROR] {exc}\n\n")
                all_ok = False
            finally:
                self._process = None

        # --- Combine timelines if batch + XML mode -------------------------
        combined_path = ""
        if (all_ok and not self._cancel_flag and is_xml
                and len(individual_xmls) > 1):
            pname = (self.var_project_name.get().strip()
                     if use_pf else "combined")
            if use_pf:
                combined_path = str(proj_dir / (pname + ".fcpxml"))
            else:
                combined_path = str(
                    Path(individual_xmls[0]).parent / (pname + ".fcpxml")
                )

            self.root.after(0, self.var_status.set, "Combining timelines\u2026")
            self.root.after(
                0, self._log,
                f"Combining {len(individual_xmls)} timelines into one\u2026\n",
            )

            try:
                combine_timelines(individual_xmls, combined_path, pname)
                self.root.after(
                    0, self._log,
                    f"[OK] Combined timeline: {Path(combined_path).name}\n\n",
                )
            except Exception as exc:
                self.root.after(
                    0, self._log,
                    f"[ERROR] Failed to combine timelines: {exc}\n\n",
                )
                logging.exception("combine_timelines failed")
                combined_path = ""

        # --- Done ----------------------------------------------------------
        self.root.after(0, self.btn_process.configure, {"text": "Process"})
        self.root.after(0, self.btn_open_folder.state, ["!disabled"])

        if self._cancel_flag:
            self.root.after(0, self.var_status.set, "Cancelled")
        elif all_ok:
            if is_xml:
                self.root.after(
                    0, self.var_status.set,
                    "Done!  Next: DaVinci Resolve \u2192 File \u2192 Import \u2192 Timeline",
                )
                msg = "Processing complete!\n\n"
                if combined_path:
                    msg += (
                        f"Combined timeline:\n"
                        f"{Path(combined_path).name}\n\n"
                    )
                if use_pf:
                    msg += f"Project folder:\n{self._output_dir}\n\n"
                msg += (
                    "To import:\n"
                    "  1. Open DaVinci Resolve\n"
                    "  2. File \u2192 Import \u2192 Timeline\n"
                    "  3. Select the combined .fcpxml file"
                )
                self.root.after(0, lambda: messagebox.showinfo("Done!", msg))
            else:
                self.root.after(0, self.var_status.set, "Done!")
        else:
            self.root.after(0, self.var_status.set, "Finished with errors (see log)")

    # -----------------------------------------------------------------------
    # Open folder
    # -----------------------------------------------------------------------
    def _open_folder(self) -> None:
        folder = self._output_dir
        if folder and Path(folder).is_dir():
            os.startfile(folder)

    # -----------------------------------------------------------------------
    # Config persistence
    # -----------------------------------------------------------------------
    def _save_config(self) -> None:
        cfg = {
            "preset": self.var_preset.get(),
            "threshold": self.var_threshold.get(),
            "margin_before": self.var_margin_before.get(),
            "margin_after": self.var_margin_after.get(),
            "export_xml": self.var_export_xml.get(),
            "project_folder": self.var_project_folder.get(),
        }
        try:
            CONFIG_FILE.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
        except OSError:
            pass

    def _load_config(self) -> None:
        if not CONFIG_FILE.is_file():
            return
        try:
            cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if "preset" in cfg:
            self.var_preset.set(cfg["preset"])
        if "threshold" in cfg:
            self.var_threshold.set(cfg["threshold"])
            self.lbl_thresh_val.configure(text=f"{cfg['threshold']}%")
        if "margin_before" in cfg:
            self.var_margin_before.set(cfg["margin_before"])
        if "margin_after" in cfg:
            self.var_margin_after.set(cfg["margin_after"])
        if "export_xml" in cfg:
            self.var_export_xml.set(cfg["export_xml"])
        if "project_folder" in cfg:
            self.var_project_folder.set(cfg["project_folder"])
            self._on_project_toggle()

    def _on_close(self) -> None:
        if self._process and self._process.poll() is None:
            if not messagebox.askyesno(
                "Processing", "A file is still being processed. Quit anyway?"
            ):
                return
            self._process.terminate()
        self._save_config()
        self.root.destroy()

    # -----------------------------------------------------------------------
    def run(self) -> None:
        self.root.mainloop()


if __name__ == "__main__":
    App().run()
