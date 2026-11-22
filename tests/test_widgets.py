import io
import random
import unittest
from datetime import datetime

from rich.cells import cell_len
from rich.console import Console
from rich.text import Text

from ytmusic_sync.widgets import (
    SelectItem,
    _confirm_view,
    clock_12h,
    fmt_duration,
    fmt_size,
    fmt_speed,
    middle_truncate,
    render_select,
    segmented_bar,
    truncate,
)


class WidgetTests(unittest.TestCase):
    def test_segmented_bar_width_and_positive_segments(self):
        randomizer = random.Random(7319)
        colors = ("#ff0001", "#00ff02", "#0000ff", "#ffff04", "#ff00ff", "#00ffff")
        cases = [
            (0, [0, 0, 0, 0, 0, 0], 0),
            (1, [1, 0, 0, 0, 0, 0], 1),
            (3, [1, 1, 1, 0, 0, 0], 3),
            (7, [1, 2, 0, 0, 1, 0], 10),
        ]
        for _ in range(300):
            counts = [randomizer.choice((0, 0, 0, 1, 2, 5, 20)) for _ in colors]
            positive = sum(count > 0 for count in counts)
            width = randomizer.randint(positive, positive + 60)
            total = randomizer.randint(0, sum(counts) + 10)
            cases.append((width, counts, total))

        for width, counts, total in cases:
            bar = segmented_bar(width, list(zip(counts, colors)), total)
            self.assertEqual(cell_len(bar.plain), width)
            widths_by_color = {color: 0 for color in colors}
            for span in bar.spans:
                if span.style.color is not None:
                    color = span.style.color.name
                    if color in widths_by_color:
                        widths_by_color[color] += span.end - span.start
            for count, color in zip(counts, colors):
                if count > 0:
                    self.assertGreaterEqual(widths_by_color[color], 1)

    def test_truncation_respects_display_cells_for_wide_text(self):
        samples = (
            "A long English title",
            "歌曲标题与艺术家",
            "یہ ایک طویل گانا ہے",
            "مرحبا بالعالم",
            "A界B界C",
            "路径\\音乐\\专辑",
        )
        for text in samples:
            for width in range(0, 20):
                self.assertLessEqual(cell_len(truncate(text, width)), width)
                self.assertLessEqual(cell_len(middle_truncate(text, width)), width)
        self.assertEqual(truncate("歌曲标题", 1), "…")
        self.assertEqual(middle_truncate("C:\\music\\long\\path", 1), "…")

    def test_formatting_edge_cases(self):
        self.assertEqual(fmt_size(0), "0.0 B")
        self.assertEqual(fmt_size(1024), "1.0 KB")
        self.assertEqual(fmt_speed(0), "0.0 KB/s")
        self.assertEqual(fmt_speed(1024), "1.0 KB/s")
        self.assertEqual(fmt_duration(0), "0:00")
        self.assertEqual(fmt_duration(59), "0:59")
        self.assertEqual(fmt_duration(3601), "1:00:01")

        midnight = datetime(2024, 1, 1, 0, 0).timestamp()
        noon = datetime(2024, 1, 1, 12, 0).timestamp()
        self.assertEqual(clock_12h(midnight), "12:00 AM")
        self.assertEqual(clock_12h(noon), "12:00 PM")

    def test_boxed_selection_has_transparent_fill_and_bounded_rail(self):
        width = 40
        console = Console(width=width, height=60, file=io.StringIO(), color_system="truecolor")
        rendered = render_select(
            [SelectItem("Selected", "Description"), SelectItem("Unselected", "Other")],
            selected=0,
            variant="boxed",
            width=width,
        )
        lines = console.render_lines(rendered, console.options.update(width=width), pad=False)

        self.assertEqual(len(lines), 6)
        for line, glyph in zip(lines[:3], ("▖", "▌", "▘")):
            rail = next(segment for segment in line if segment.text.startswith(glyph))
            self.assertEqual(rail.style.color.name, "#ff9f1a")
            self.assertIsNone(rail.style.bgcolor)
            self.assertTrue(all(
                segment.style.bgcolor is not None
                for segment in line if segment is not rail
            ))
        for line, glyph in zip(lines[3:], ("▖", "▌", "▘")):
            rail = next(segment for segment in line if segment.text.startswith(glyph))
            self.assertEqual(rail.style.color.name, "#2f2f2f")
            self.assertTrue(all(segment.style.bgcolor is None for segment in line))

    def test_boxed_selector_stays_boxed_on_short_terminals(self):
        console = Console(width=40, height=12, file=io.StringIO())
        items = [SelectItem("One", "First"), SelectItem("Two", "Second")]
        boxed = render_select(items, 0, "boxed", width=40)
        compact = render_select(items, 0, "compact", width=40)
        self.assertEqual(len(console.render_lines(boxed, console.options, pad=False)), 6)
        self.assertEqual(len(console.render_lines(compact, console.options, pad=False)), 2)

    def test_confirmation_footer_is_outside_and_title_accepts_styled_text(self):
        console = Console(width=80, height=24, file=io.StringIO(), record=True, force_terminal=True)
        title = Text()
        title.append("Add Music", style="bold #ff9f1a")
        title.append("  Paste links", style="#8b8b8b")
        rendered = _confirm_view("Add these 3 playlist links to Playlists.txt?", title, True, console)
        console.print(rendered)
        lines = [line for line in console.export_text(styles=False).splitlines() if line]
        self.assertIn("Add Music  Paste links", "\n".join(lines))
        self.assertIn("←→ choose · enter confirm · y / n", lines[-1])


if __name__ == "__main__":
    unittest.main()
