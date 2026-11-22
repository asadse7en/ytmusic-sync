"""Shared Rich presentation helpers and interactive selectors."""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Iterable

from rich.cells import cell_len
from rich.columns import Columns
from rich.console import Console, Group, RenderableType
from rich.live import Live
from rich.panel import Panel
from rich.style import Style
from rich.text import Text

from . import keys
from .theme import (
    ACCENT,
    BAR_OFF,
    BORDER,
    BOX_BORDER,
    DIM,
    DIMMER,
    DIVIDER,
    EDGE_OFF,
    GREEN,
    SEL_EDGE,
    SEL_FILL,
    TEXT,
    TILE_BORDER,
    TRACK,
    get_console,
)


def _take_cells(text: str, width: int) -> str:
    if width <= 0:
        return ""
    result = []
    used = 0
    for char in text:
        size = cell_len(char)
        if used + size > width:
            break
        result.append(char)
        used += size
    return "".join(result)


def truncate(text: str, width: int) -> str:
    """Truncate text to a display-cell width with an ellipsis."""
    if width <= 0:
        return ""
    if cell_len(text) <= width:
        return text
    if width == 1:
        return "…"
    return _take_cells(text, width - 1) + "…"


def middle_truncate(text: str, width: int) -> str:
    """Truncate a path around its middle to a display-cell width."""
    if width <= 0:
        return ""
    if cell_len(text) <= width:
        return text
    if width == 1:
        return "…"
    available = width - 1
    left_width = (available + 1) // 2
    right_width = available - left_width
    left = _take_cells(text, left_width)
    right = ""
    if right_width:
        suffix = []
        used = 0
        for char in reversed(text):
            size = cell_len(char)
            if used + size > right_width:
                break
            suffix.append(char)
            used += size
        right = "".join(reversed(suffix))
    return left + "…" + right


def title_text(
    name: str,
    dim: str | None = None,
    name_color: str = ACCENT,
    dim_color: str = DIM,
) -> Text:
    """Build a title with a bold name span and a regular dim span."""
    result = Text()
    result.append(name, style=Style(color=name_color, bold=True))
    result.append(f"  {dim or ''}", style=Style(color=dim_color, bold=False))
    return result


def panel_with_title(
    body: RenderableType,
    name: str | Text,
    dim: str | None = None,
    border: str = BORDER,
    name_color: str = ACCENT,
    dim_color: str = DIM,
    padding: tuple[int, int] = (1, 2),
) -> Panel:
    """Create a rounded panel using the shared title treatment."""
    from rich import box

    return Panel(
        body,
        title=title_text(name, dim, name_color, dim_color) if isinstance(name, str) else name,
        title_align="left",
        border_style=border,
        box=box.ROUNDED,
        padding=padding,
    )


def footer_hint(text: str) -> Text:
    """Return a dimmer footer line."""
    result = Text(text, style=Style(color=DIMMER))
    result.no_wrap = True
    result.overflow = "crop"
    return result


def divider(width: int, color: str = DIVIDER) -> Text:
    """Return a horizontal divider of the requested display width."""
    return Text("─" * max(0, width), style=Style(color=color))


def ruled_heading(label: str, width: int) -> Text:
    """Return an indented label followed by a divider filling the width."""
    width = max(0, width)
    indent = min(2, width)
    label_width = max(0, width - indent - 1)
    label = truncate(label, label_width)
    remaining = max(0, width - indent - cell_len(label) - (1 if label else 0))
    result = Text(" " * indent)
    result.append(label, style=Style(color=ACCENT))
    if label:
        result.append(" ")
    result.append("─" * remaining, style=Style(color=DIVIDER))
    return result


def _largest_remainder(
    weights: list[float],
    width: int,
    minimum_indexes: list[int] | None = None,
) -> list[int]:
    if width <= 0 or not weights or sum(weights) <= 0:
        return [0] * len(weights)
    total = sum(weights)
    exact = [weight * width / total for weight in weights]
    cells = [math.floor(value) for value in exact]
    remainder = width - sum(cells)
    order = sorted(range(len(weights)), key=lambda index: (exact[index] - cells[index], -index), reverse=True)
    for index in order[:remainder]:
        cells[index] += 1

    positive = minimum_indexes if minimum_indexes is not None else [
        index for index, weight in enumerate(weights) if weight > 0
    ]
    if len(positive) <= width:
        positive_set = set(positive)
        for index in positive:
            if cells[index] == 0:
                donors = [
                    candidate for candidate in range(len(cells))
                    if cells[candidate] > (1 if candidate in positive_set else 0)
                ]
                if donors:
                    donor = max(donors, key=lambda candidate: (cells[candidate], weights[candidate]))
                    cells[donor] -= 1
                    cells[index] = 1
    return cells


def segmented_bar(width: int, parts: list[tuple[int, str]], total: int) -> Text:
    """Render proportional segments and a track using exact display-cell widths."""
    width = max(0, width)
    counts = [max(0, count) for count, _ in parts]
    used = sum(counts)
    if total > used:
        weights = [float(count) for count in counts] + [float(total - used)]
    elif used:
        weights = [float(count) for count in counts] + [0.0]
    else:
        weights = [0.0] * len(parts) + [1.0]
    allocations = _largest_remainder(
        weights,
        width,
        [index for index, count in enumerate(counts) if count > 0],
    )

    result = Text()
    for (_, color), cells in zip(parts, allocations):
        if cells:
            result.append("━" * cells, style=Style(color=color))
    track_cells = allocations[-1] if allocations else width
    if track_cells:
        result.append("━" * track_cells, style=Style(color=TRACK))
    return result


def sparkline(values: Iterable[float], width: int = 15) -> Text:
    """Render values as a compact accent-colored sparkline."""
    width = max(0, width)
    points = list(values)
    if not width or not points:
        return Text()
    if len(points) > width:
        if width == 1:
            points = points[-1:]
        else:
            points = [points[round(i * (len(points) - 1) / (width - 1))] for i in range(width)]
    low, high = min(points), max(points)
    if low == high:
        indexes = [4] * len(points)
    else:
        indexes = [round((value - low) * 7 / (high - low)) for value in points]
    return Text("".join("▁▂▃▄▅▆▇█"[index] for index in indexes), style=Style(color=ACCENT))


def stage_meter(stage_index: int, total: int = 4) -> Text:
    """Render completed, current, and pending stage blocks."""
    result = Text()
    for index in range(max(0, total)):
        if index < stage_index:
            color = GREEN
            mark = "▰"
        elif index == stage_index:
            color = ACCENT
            mark = "▰"
        else:
            color = DIMMER
            mark = "▱"
        result.append(mark, style=Style(color=color))
    return result


def fmt_size(bytes: float) -> str:
    """Format a byte count using binary units up through gigabytes."""
    size = float(bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


def fmt_speed(bytes_per_s: float) -> str:
    """Format a transfer speed in kilobytes or megabytes per second."""
    speed = max(0.0, float(bytes_per_s)) / 1024
    if speed >= 1024:
        return f"{speed / 1024:.1f} MB/s"
    return f"{speed:.1f} KB/s"


def fmt_duration(seconds: float) -> str:
    """Format a duration as m:ss or h:mm:ss."""
    value = max(0, int(seconds))
    minutes, secs = divmod(value, 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


def clock_12h(timestamp: float) -> str:
    """Format a timestamp as a 12-hour clock time."""
    return datetime.fromtimestamp(timestamp).strftime("%I:%M %p").lstrip("0")


def tile(label: str, value: str, width: int) -> Panel:
    """Build a compact rounded metric tile."""
    from rich import box

    content_width = max(1, width - 4)
    body = Text()
    body.append(truncate(label, content_width), style=Style(color=DIM))
    body.append("\n")
    body.append(truncate(str(value), content_width), style=Style(color=ACCENT, bold=True))
    return Panel(body, border_style=TILE_BORDER, box=box.ROUNDED, padding=(0, 1),
                 width=max(4, width), expand=False)


@dataclass
class SelectItem:
    """One selectable row and its optional presentation metadata."""

    label: str
    desc: str = ""
    hotkey: str = ""
    value: Any = None
    enabled: bool = True
    dim: bool = False
    desc_color: str | None = None
    desc_align: str = "left"


def _secondary(item: SelectItem) -> RenderableType | None:
    value = item.value
    if callable(value):
        return value(item)
    if not isinstance(value, (str, bytes)) and hasattr(value, "__rich_console__"):
        return value
    return None


def _content(
    item: SelectItem,
    selected: bool,
    label_width: int,
    content_width: int,
    *,
    selected_background: bool = True,
    accent_selected_desc: bool = True,
) -> Text:
    label_width = min(max(0, label_width), content_width)
    label = truncate(item.label, label_width)
    label_padding = max(0, label_width - cell_len(label))
    rest_width = max(0, content_width - label_width)
    hotkey = truncate(item.hotkey, rest_width)
    hotkey_width = cell_len(hotkey)
    prefix_width = rest_width - hotkey_width
    desc_width = max(0, prefix_width - (1 if hotkey and prefix_width else 0))
    desc = truncate(item.desc, desc_width)
    if item.desc_align == "right":
        separator = " " if hotkey and prefix_width else ""
        padding = " " * max(0, prefix_width - cell_len(desc) - len(separator))
        prefix = padding + desc + separator
    else:
        padding = " " * max(0, prefix_width - cell_len(desc))
        prefix = desc + padding
    dimmed = item.dim or not item.enabled
    label_color = DIMMER if dimmed else (ACCENT if selected else TEXT)
    desc_selected = selected and accent_selected_desc
    desc_color = DIMMER if dimmed else (ACCENT if desc_selected else (item.desc_color or DIM))
    desc_bold = desc_selected and not dimmed
    hotkey_color = DIMMER if dimmed or not selected else ACCENT
    bgcolor = SEL_FILL if selected and selected_background else None
    result = Text()
    result.append(label, style=Style(color=label_color, bold=selected and not dimmed, bgcolor=bgcolor))
    result.append(" " * label_padding, style=Style(color=label_color, bold=selected and not dimmed, bgcolor=bgcolor))
    result.append(prefix, style=Style(color=desc_color, bold=desc_bold, bgcolor=bgcolor))
    if hotkey:
        result.append(hotkey, style=Style(color=hotkey_color, bold=False, bgcolor=bgcolor))
    if not hotkey and cell_len(prefix) < rest_width:
        result.append(" " * (rest_width - cell_len(prefix)),
                      style=Style(color=desc_color, bold=desc_bold, bgcolor=bgcolor))
    return result


class _SelectRenderable:
    def __init__(self, items: list[SelectItem], selected: int, variant: str, label_width: int, width: int | None):
        self.items = items
        self.selected = selected
        self.variant = variant
        self.label_width = label_width
        self.width = width

    def __rich_console__(self, console: Console, options):
        width = max(1, min(self.width or options.max_width, options.max_width))
        compact = self.variant == "compact"
        if compact:
            for index, item in enumerate(self.items):
                selected = index == self.selected
                bg = SEL_FILL if selected else None
                row = Text()
                row.append("▌", style=Style(color=ACCENT if selected else BAR_OFF, bgcolor=bg))
                content_width = width - 1
                content = _content(item, selected, self.label_width, content_width)
                row.append_text(content)
                if cell_len(row.plain) < width:
                    row.append(" " * (width - cell_len(row.plain)), style=Style(bgcolor=bg))
                yield row
                secondary = _secondary(item)
                if secondary is not None:
                    line = Text()
                    line.append("▌", style=Style(color=ACCENT if selected else BAR_OFF, bgcolor=bg))
                    line.append(" ", style=Style(bgcolor=bg))
                    line.append_text(_as_text(secondary, width - 2, console))
                    if selected:
                        line.stylize(Style(bgcolor=SEL_FILL), 1, cell_len(line.plain))
                    if cell_len(line.plain) < width:
                        line.append(" " * (width - cell_len(line.plain)), style=Style(bgcolor=bg))
                    yield line
            return

        inner = max(1, width - 2)
        content_width = max(0, inner - 2)
        for index, item in enumerate(self.items):
            selected = index == self.selected
            bar_color = ACCENT if selected else BAR_OFF
            edge_color = SEL_EDGE if selected else EDGE_OFF
            right_aligned = item.desc_align == "right"
            border_style = Style(color=edge_color)
            content_width = max(0, inner - (1 if right_aligned else 2))
            yield Text.assemble(
                ("▖", Style(color=bar_color)),
                ("╭", border_style),
                ("─" * (inner - 1), border_style),
                ("╮", border_style),
            )
            row = Text()
            row.append("▌", style=Style(color=bar_color))
            row.append(" ")
            row.append_text(_content(
                item,
                selected,
                self.label_width,
                content_width,
                selected_background=False,
                accent_selected_desc=False,
            ))
            if not right_aligned:
                row.append(" ")
            row.append("│", style=Style(color=edge_color))
            yield row
            yield Text.assemble(
                ("▘", Style(color=bar_color)),
                ("╰", border_style),
                ("─" * (inner - 1), border_style),
                ("╯", border_style),
            )


def _as_text(renderable: RenderableType, width: int, console: Console) -> Text:
    if isinstance(renderable, Text):
        result = renderable.copy()
        result.truncate(width, overflow="ellipsis")
        return result
    if isinstance(renderable, str):
        return Text(truncate(renderable, width), style=Style(color=DIM))
    rendered = console.render_lines(renderable, options=console.options.update(width=max(1, width)), pad=False)
    result = Text()
    if rendered:
        used = 0
        for segment in rendered[0]:
            segment_text = segment.text.partition("\n")[0] if segment.text else ""
            segment_text = _take_cells(segment_text, width - used)
            if segment_text:
                result.append(segment_text, style=segment.style)
                used += cell_len(segment_text)
            if used >= width:
                break
    return result


def render_select(
    items: list[SelectItem],
    selected: int,
    variant: str,
    label_width: int = 16,
    width: int | None = None,
) -> RenderableType:
    """Build a boxed or compact selector with stable row alignment."""
    if variant not in ("boxed", "compact"):
        raise ValueError(f"Unknown selector variant: {variant}")
    return _SelectRenderable(items, selected, variant, label_width, width)


def run_select(
    view_factory: Callable[[int], RenderableType],
    items: list[SelectItem],
    start: int = 0,
    variant: str = "boxed",
    on_key: Callable[[str, int], int | str | None] | None = None,
) -> int | None:
    """Run a key-driven selector and return the selected item index."""
    if variant not in ("boxed", "compact"):
        raise ValueError(f"Unknown selector variant: {variant}")
    enabled = [index for index, item in enumerate(items) if item.enabled]
    if not enabled:
        return None
    selected = start if 0 <= start < len(items) and items[start].enabled else enabled[0]
    console = get_console()
    with Live(view_factory(selected), console=console, auto_refresh=False, screen=True) as live:
        while True:
            key = keys.read_key()
            if key is None:
                continue
            if key in ("up", "down"):
                step = -1 if key == "up" else 1
                position = enabled.index(selected)
                selected = enabled[(position + step) % len(enabled)]
            elif key == "enter":
                return selected
            elif key in ("esc", "ctrl-c"):
                return None
            elif len(key) == 1 and key.isprintable():
                match = next(
                    (index for index, item in enumerate(items)
                     if item.enabled and item.hotkey.casefold() == key.casefold()),
                    None,
                )
                if match is not None:
                    return match
                if on_key is not None:
                    result = on_key(key, selected)
                    selected, should_exit = _apply_key_result(result, selected, enabled)
                    if should_exit:
                        return None
            elif on_key is not None:
                result = on_key(key, selected)
                selected, should_exit = _apply_key_result(result, selected, enabled)
                if should_exit:
                    return None
            live.update(view_factory(selected), refresh=True)


def _apply_key_result(result: int | str | None, selected: int, enabled: list[int]) -> tuple[int, bool]:
    if result == "exit":
        return selected, True
    if isinstance(result, int) and result in enabled:
        return result, False
    if result == "refresh" or result is None:
        return selected, False
    return selected, False


def _confirm_view(message: RenderableType, title: str | Text | None, yes: bool,
                  console: Console) -> RenderableType:
    from rich import box

    width = max(1, console.width)
    if isinstance(message, str):
        msg = Text(truncate(message, max(1, width - (6 if title else 0))))
    elif isinstance(message, Text):
        msg = message.copy()
        msg.truncate(max(1, width - (6 if title else 0)), overflow="ellipsis")
    else:
        msg = message
    if isinstance(msg, Text):
        msg.no_wrap = True
        msg.overflow = "ellipsis"
    options = []
    for label, selected in (("Yes", yes), ("No", not yes)):
        style = Style(color=ACCENT, bold=True, bgcolor=SEL_FILL) if selected else Style(color=TEXT, bold=False)
        options.append(Panel(
            Text(label, style=style, justify="center"),
            border_style=ACCENT if selected else BOX_BORDER,
            style=Style(bgcolor=SEL_FILL) if selected else Style(),
            box=box.ROUNDED,
            padding=(0, 2),
            width=12,
            expand=False,
        ))
    body = Group(msg, Columns(options, equal=True, expand=False, align="left"))
    if title is not None:
        panel = panel_with_title(body, title, padding=(0, 1))
    else:
        panel = body
    return Group(panel, footer_hint("←→ choose · enter confirm · y / n"))


def confirm_boxes(
    message: RenderableType,
    title: str | Text | None = None,
    default: bool = True,
) -> bool:
    """Ask for confirmation using two side-by-side bordered choices."""
    console = get_console()
    yes = default
    with Live(_confirm_view(message, title, yes, console), console=console, auto_refresh=False,
              screen=True) as live:
        while True:
            key = keys.read_key()
            if key is None:
                continue
            if key in ("left", "right"):
                yes = key == "left"
            elif key == "enter":
                return yes
            elif key.casefold() == "y":
                return True
            elif key.casefold() == "n" or key in ("esc", "ctrl-c"):
                return False
            live.update(_confirm_view(message, title, yes, console), refresh=True)
