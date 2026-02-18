#!/usr/bin/env python3
"""ft_sipping - Animated Sip-ping Tool"""

import argparse
import os
import platform
import re
import shutil
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

from PIL import Image

# --- ANSI escape helpers ---
ESC = "\033"
HIDE_CURSOR = f"{ESC}[?25l"
SHOW_CURSOR = f"{ESC}[?25h"
CLEAR_LINE = f"{ESC}[K"
RESET = f"{ESC}[0m"


def cursor_up(n):
    return f"{ESC}[{n}A" if n > 0 else ""


# --- GIF loading ---

def load_gif_frames(gif_path):
    """Load all frames from a GIF, properly compositing with disposal methods."""
    with Image.open(gif_path) as img:
        frames = []
        canvas = Image.new("RGBA", img.size, (0, 0, 0, 0))

        for i in range(img.n_frames):
            img.seek(i)
            frame = img.convert("RGBA")

            new_canvas = canvas.copy()
            new_canvas.paste(frame, mask=frame)
            frames.append(new_canvas.copy())

            disposal = img.disposal_method
            if disposal == 2:  # Restore to background
                canvas = Image.new("RGBA", img.size, (0, 0, 0, 0))
            else:  # 0, 1 = don't dispose; 3 = restore previous (rare, treat as keep)
                canvas = new_canvas

    return frames


def mirror_frames(frames):
    """Horizontally flip all frames."""
    return [f.transpose(Image.Transpose.FLIP_LEFT_RIGHT) for f in frames]


# --- ANSI rendering ---

def frame_to_ansi(image, width):
    """Convert a PIL RGBA Image to a list of ANSI half-block art lines.

    Uses color state tracking to minimize escape codes — only emits
    new sequences when fg/bg changes from the previous pixel pair.
    """
    orig_w, orig_h = image.size
    height = int(width * orig_h / orig_w)
    # Ensure even height for half-block pairing
    height += height % 2

    resized = image.resize((width, height), Image.Resampling.LANCZOS)
    pixels = resized.load()

    lines = []
    for y in range(0, height, 2):
        parts = []
        prev_fg = None
        prev_bg = None
        in_color = False

        for x in range(width):
            r1, g1, b1, a1 = pixels[x, y]
            if y + 1 < height:
                r2, g2, b2, a2 = pixels[x, y + 1]
            else:
                r2, g2, b2, a2 = 0, 0, 0, 0

            # Determine character and required colors
            if a1 < 30 and a2 < 30:
                cur_fg, cur_bg, char = None, None, " "
            elif a1 < 30:
                cur_fg, cur_bg, char = (r2, g2, b2), None, "▄"
            elif a2 < 30:
                cur_fg, cur_bg, char = (r1, g1, b1), None, "▀"
            else:
                cur_fg, cur_bg, char = (r1, g1, b1), (r2, g2, b2), "▀"

            # Only emit escape codes when color state changes
            if cur_fg != prev_fg or cur_bg != prev_bg:
                if cur_fg is None:
                    # Going transparent — reset if we were in color
                    if in_color:
                        parts.append(RESET)
                        in_color = False
                else:
                    # Need color — reset old state and emit new
                    if in_color:
                        parts.append(RESET)
                    if cur_bg is not None:
                        parts.append(
                            f"{ESC}[38;2;{cur_fg[0]};{cur_fg[1]};{cur_fg[2]}"
                            f";48;2;{cur_bg[0]};{cur_bg[1]};{cur_bg[2]}m"
                        )
                    else:
                        parts.append(
                            f"{ESC}[38;2;{cur_fg[0]};{cur_fg[1]};{cur_fg[2]}m"
                        )
                    in_color = True
                prev_fg = cur_fg
                prev_bg = cur_bg

            parts.append(char)

        # Single reset at end of line
        if in_color:
            parts.append(RESET)

        lines.append("".join(parts))

    return lines


# --- Display helpers ---

def display_sipping(frame_lines, text, up_amount, log_count):
    """Draw a sipping frame with status text below the permanent log lines."""
    out = cursor_up(up_amount)
    for line in frame_lines:
        out += f"  {line}{CLEAR_LINE}\n"
    out += "\n" * log_count
    out += f"  {text}{CLEAR_LINE}\n"
    sys.stdout.write(out)
    sys.stdout.flush()


def display_clink(orig_lines, mirror_lines, text, up_amount, log_count):
    """Draw original + mirrored frames side-by-side with status text below permanent log lines."""
    out = cursor_up(up_amount)
    rows = max(len(orig_lines), len(mirror_lines))
    for i in range(rows):
        left = orig_lines[i] if i < len(orig_lines) else ""
        right = mirror_lines[i] if i < len(mirror_lines) else ""
        out += f"  {left} {right}{CLEAR_LINE}\n"
    out += "\n" * log_count
    out += f"  {text}{CLEAR_LINE}\n"
    sys.stdout.write(out)
    sys.stdout.flush()


# --- Ping ---

def do_ping(host):
    """Execute a single ping and return a result dict."""
    is_win = platform.system() == "Windows"
    cmd = (
        ["ping", "-n", "1", "-w", "2000", host]
        if is_win
        else ["ping", "-c", "1", "-W", "2", host]
    )

    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
        output = proc.stdout

        # Accept both . and , as decimal separator (locale tolerance)
        time_match = re.search(r"time[=<](\d+[.,]?\d*)\s*ms", output, re.IGNORECASE)
        ttl_match = re.search(r"ttl[=](\d+)", output, re.IGNORECASE)

        if time_match:
            time_str = time_match.group(1).replace(",", ".")
            return {
                "success": True,
                "time_ms": float(time_str),
                "ttl": int(ttl_match.group(1)) if ttl_match else 0,
            }
        return {"success": False, "error": "Request timed out"}
    except subprocess.TimeoutExpired:
        return {"success": False, "error": "Request timed out"}
    except Exception as e:
        return {"success": False, "error": str(e)}


def resolve_host(host):
    """Resolve hostname to IP address using socket."""
    try:
        results = socket.getaddrinfo(host, None, socket.AF_UNSPEC, socket.SOCK_STREAM)
        if results:
            return results[0][4][0]
    except socket.gaierror:
        pass
    return host


# --- Main ---

def main():
    # Ensure UTF-8 output (needed on Windows with non-UTF-8 codepages)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    # Enable ANSI escape codes on Windows
    if platform.system() == "Windows":
        os.system("")

    parser = argparse.ArgumentParser(
        description="ft_sipping - Animated Sip-ping Tool",
        formatter_class=lambda prog: argparse.HelpFormatter(prog, max_help_position=38),
    )
    parser.add_argument("host", help="Target host to sip-ping")
    parser.add_argument(
        "-c", "--count", type=int, default=4, help="Number of sip-pings (default: 4)"
    )
    parser.add_argument(
        "-i",
        "--interval",
        type=float,
        default=1.0,
        help="Interval between sip-pings in seconds (default: 1.0)",
    )
    parser.add_argument(
        "--width",
        type=int,
        default=18,
        help="Animation width in characters (default: 18)",
    )
    args = parser.parse_args()

    # Validate inputs
    if args.count < 1:
        parser.error("count must be at least 1")
    if args.interval < 0:
        parser.error("interval must be non-negative")
    if args.width < 4:
        parser.error("width must be at least 4")

    # Auto-cap width to terminal size (clink needs 2*width + gap + text)
    term_cols = shutil.get_terminal_size().columns
    max_width = (term_cols - 30) // 2  # Reserve ~30 cols for gap + text
    if max_width > 0 and args.width > max_width:
        args.width = max(4, max_width)

    # Locate GIF asset
    gif_path = Path(__file__).parent / "assets" / "sip.gif"
    if not gif_path.exists():
        print(f"Error: GIF not found at {gif_path}")
        sys.exit(1)

    # Pre-render frames
    raw_frames = load_gif_frames(gif_path)
    sip_ansi = [frame_to_ansi(f, args.width) for f in raw_frames]
    mir_ansi = [frame_to_ansi(f, args.width) for f in mirror_frames(raw_frames)]

    num_rows = len(sip_ansi[0])
    total_frames = len(sip_ansi)

    # Resolve IP
    ip = resolve_host(args.host)
    display_host = f"{args.host} ({ip})" if ip != args.host else args.host
    print(f"\n  Sip-ping {display_host}...\n")

    # Reserve space for animation area + text line below
    sys.stdout.write("\n" * (num_rows + 1))
    sys.stdout.flush()

    # Stats
    sent = 0
    received = 0
    times = []

    # Ctrl+C handler
    interrupted = False

    def on_sigint(_sig, _frame):
        nonlocal interrupted
        interrupted = True

    prev_handler = signal.getsignal(signal.SIGINT)
    signal.signal(signal.SIGINT, on_sigint)

    sys.stdout.write(HIDE_CURSOR)
    sys.stdout.flush()

    log_count = 0  # permanent log lines written below animation

    try:
        for seq in range(args.count):
            if interrupted:
                break

            sent += 1

            # cursor_up amount grows as log lines accumulate below animation
            up_amount = num_rows + 1 + log_count

            # --- Sipping animation ---
            sip_text = f"Sip-ping #{seq + 1}..."
            for i in range(total_frames):
                if interrupted:
                    break
                display_sipping(sip_ansi[i], sip_text, up_amount, log_count)
                time.sleep(0.03)

            if interrupted:
                break

            # --- Actual ping ---
            result = do_ping(args.host)

            # Align "Clink!"/"Spill!" with the start of the right cup in clink animation
            sep = " " * max(1, args.width + 1 - len(sip_text))
            if result["success"]:
                received += 1
                times.append(result["time_ms"])
                clink_text = f"{sip_text}{sep}Clink! {result['time_ms']:.0f}ms TTL={result['ttl']}"
            else:
                clink_text = f"{sip_text}{sep}Spill! {result['error']}"

            # --- Clink animation (both cups side-by-side) ---
            for i in range(total_frames):
                if interrupted:
                    break
                display_clink(sip_ansi[i], mir_ansi[i], clink_text, up_amount, log_count)
                time.sleep(0.03)

            if interrupted:
                break

            # Advance cursor one row to seal the transient text as a permanent log line
            sys.stdout.write("\n")
            sys.stdout.flush()
            log_count += 1

            if seq < args.count - 1:
                time.sleep(args.interval)

    finally:
        sys.stdout.write(SHOW_CURSOR)
        sys.stdout.flush()
        signal.signal(signal.SIGINT, prev_handler)

    # Summary
    loss = ((sent - received) / sent * 100) if sent > 0 else 0
    print(f"\n* Sip-ping statistics for {args.host} *")
    print(f"    {sent} sip, {received} clink, {loss:.0f}% spill")
    if times:
        avg = sum(times) / len(times)
        print(f"    min/avg/max = {min(times):.0f}/{avg:.0f}/{max(times):.0f} ms")


if __name__ == "__main__":
    main()
