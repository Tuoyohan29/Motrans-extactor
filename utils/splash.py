"""Animation de démarrage de l'Extracteur : logo MOTRANS en dégradé, révélé en fondu,
reflet lumineux qui balaie le mot, sous-titre en machine à écrire.

Bibliothèque standard uniquement. Purement cosmétique : ne s'affiche que sur un vrai
terminal interactif (sinon, rien — les journaux prennent le relais), et peut être
désactivée par SPLASH=false ou NO_COLOR (NO_COLOR garde l'animation mais sans couleur).
"""

from __future__ import annotations

import math
import os
import shutil
import sys
import time

# Glyphes 5x5 (plein = █). Seules les lettres de MOTRANS / EXTRACTEUR utiles sont définies.
_GLYPHS = {
    "M": ["█   █", "██ ██", "█ █ █", "█   █", "█   █"],
    "O": [" ███ ", "█   █", "█   █", "█   █", " ███ "],
    "T": ["█████", "  █  ", "  █  ", "  █  ", "  █  "],
    "R": ["████ ", "█   █", "████ ", "█  █ ", "█   █"],
    "A": [" ███ ", "█   █", "█████", "█   █", "█   █"],
    "N": ["█   █", "██  █", "█ █ █", "█  ██", "█   █"],
    "S": [" ████", "█    ", " ███ ", "    █", "████ "],
}
_GH = 5                       # hauteur des glyphes
_GAP = 1                      # colonnes entre deux lettres
_SIGMA = 3.2                  # largeur du reflet
# Dégradé : or → orange MoTrans → braise.
_STOPS = [(255, 196, 75), (232, 89, 12), (168, 50, 8)]
_SUBTITLE = "EXTRACTEUR"


def _lerp(a: tuple[int, int, int], b: tuple[int, int, int], t: float) -> tuple[int, int, int]:
    return tuple(round(a[i] + (b[i] - a[i]) * t) for i in range(3))


def _gradient(width: int) -> list[tuple[int, int, int]]:
    if width <= 1:
        return [_STOPS[0]]
    out = []
    segs = len(_STOPS) - 1
    for col in range(width):
        pos = col / (width - 1) * segs
        i = min(int(pos), segs - 1)
        out.append(_lerp(_STOPS[i], _STOPS[i + 1], pos - i))
    return out


def _build_grid(word: str) -> tuple[list[str], int]:
    rows = ["" for _ in range(_GH)]
    for index, char in enumerate(word):
        glyph = _GLYPHS.get(char, ["     "] * _GH)
        gap = " " * _GAP if index else ""
        for r in range(_GH):
            rows[r] += gap + glyph[r]
    return rows, len(rows[0])


def _shimmer(rgb: tuple[int, int, int], col: int, center: float | None) -> tuple[int, int, int]:
    if center is None:
        return rgb
    d = col - center
    f = math.exp(-(d * d) / (2 * _SIGMA * _SIGMA))
    if f <= 0.03:
        return rgb
    r, g, b = rgb
    return (round(r + (255 - r) * f * 0.92), round(g + (255 - g) * f * 0.92), round(b + (255 - b) * f * 0.92))


def _supports() -> tuple[bool, bool]:
    """(afficher, couleur)."""
    if os.environ.get("SPLASH", "").strip().lower() in ("0", "false", "no", "off"):
        return False, False
    if not sys.stdout.isatty():
        return False, False
    return True, os.environ.get("NO_COLOR") is None


class _Painter:
    def __init__(self, grid: list[str], width: int, colors: list, color: bool, pad: int):
        self.grid, self.width, self.colors, self.color, self.pad = grid, width, colors, color, pad

    def frame(self, reveal: int, center: float | None) -> list[str]:
        lines = []
        prefix = " " * self.pad
        for row in self.grid:
            parts = [prefix]
            for col, ch in enumerate(row):
                if ch == " " or col > reveal:
                    parts.append(" ")
                elif not self.color:
                    parts.append(ch)
                else:
                    r, g, b = _shimmer(self.colors[col], col, center)
                    parts.append(f"\x1b[38;2;{r};{g};{b}m{ch}\x1b[0m")
            lines.append("".join(parts))
        return lines


def show_splash(extractor_id: str = "", version: str = "", ussd: str = "", sms: str = "",
                *, force: bool = False, fast: bool = False, stream=None) -> None:
    stream = stream or sys.stdout
    show, color = (True, os.environ.get("NO_COLOR") is None) if force else _supports()
    if not show:
        return

    grid, width = _build_grid("MOTRANS")
    cols = shutil.get_terminal_size((80, 24)).columns
    colors = _gradient(width)
    scale = 0.25 if fast else 1.0

    try:
        stream.write("\x1b[?25l\n")
        if cols >= width + 2:
            _animate_banner(stream, _Painter(grid, width, colors, color, max(0, (cols - width) // 2)), scale)
        else:
            _compact_banner(stream, color, cols)
        _subtitle(stream, color, cols, width, extractor_id, version, ussd, sms, scale)
    except (KeyboardInterrupt, BrokenPipeError):
        pass
    finally:
        stream.write("\x1b[?25h")
        stream.flush()


def _animate_banner(stream, painter: _Painter, scale: float) -> None:
    height = len(painter.grid)
    stream.write("\n" * height)

    def flush(lines: list[str]) -> None:
        stream.write(f"\x1b[{height}A")
        for ln in lines:
            stream.write("\r\x1b[2K" + ln + "\n")
        stream.flush()

    for reveal in range(0, painter.width + 1, 2):          # fondu de gauche à droite
        flush(painter.frame(reveal, None))
        time.sleep(0.018 * scale)
    for _ in range(2):                                      # deux passages de reflet
        for center in range(-6, painter.width + 6, 2):
            flush(painter.frame(painter.width, center))
            time.sleep(0.011 * scale)
    flush(painter.frame(painter.width, None))


def _compact_banner(stream, color: bool, cols: int) -> None:
    word = " ".join("MOTRANS")
    pad = " " * max(0, (cols - len(word)) // 2)
    stops = _gradient(len(word))
    out = [pad]
    for i, ch in enumerate(word):
        if color and ch != " ":
            r, g, b = stops[i]
            out.append(f"\x1b[1;38;2;{r};{g};{b}m{ch}\x1b[0m")
        else:
            out.append(ch)
    stream.write("".join(out) + "\n")
    stream.flush()


def _subtitle(stream, color: bool, cols: int, width: int, extractor_id: str, version: str,
              ussd: str, sms: str, scale: float) -> None:
    gold = "\x1b[38;2;255;196;75m" if color else ""
    grey = "\x1b[38;2;150;150;160m" if color else ""
    dim = "\x1b[38;2;110;115;125m" if color else ""
    reset = "\x1b[0m" if color else ""

    inner = max(width, 20)
    pad = " " * max(0, (cols - inner) // 2)

    # Filet dégradé sous le logo.
    if color:
        bar = "".join(f"\x1b[38;2;{r};{g};{b}m─" for r, g, b in _gradient(inner)) + reset
    else:
        bar = "─" * inner
    stream.write(pad + bar + "\n")

    # Sous-titre « E X T R A C T E U R » en machine à écrire, centré.
    spaced = " ".join(_SUBTITLE)
    spad = " " * max(0, (cols - len(spaced)) // 2)
    stream.write(spad)
    for ch in spaced:
        stream.write(f"{gold}{ch}{reset}" if ch != " " else " ")
        stream.flush()
        time.sleep(0.05 * scale)
    stream.write("\n")

    meta = []
    if extractor_id:
        meta.append(f"{extractor_id}")
    if version:
        meta.append(f"v{version}")
    if ussd:
        meta.append(f"USSD:{ussd}")
    if sms:
        meta.append(f"SMS:{sms}")
    if meta:
        line = "  ·  ".join(meta)
        mpad = " " * max(0, (cols - len(line)) // 2)
        stream.write(f"{mpad}{dim}{line}{reset}\n")

    # Petit balayage de « chargement ».
    frames = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
    label = "initialisation"
    lpad = " " * max(0, (cols - (len(label) + 2)) // 2)
    for i in range(int(14 / max(scale, 0.25)) if scale >= 1 else 4):
        stream.write(f"\r{lpad}{gold}{frames[i % len(frames)]}{reset} {grey}{label}{reset}")
        stream.flush()
        time.sleep(0.06 * scale)
    check = "\x1b[38;2;46;160;67m✓\x1b[0m" if color else "OK"
    stream.write(f"\r\x1b[2K{lpad}{check} {grey}prêt{reset}\n\n")
    stream.flush()


if __name__ == "__main__":
    show_splash("EXT-03", "0.1.0", "termux", "termux", force=True, fast="--fast" in sys.argv)
