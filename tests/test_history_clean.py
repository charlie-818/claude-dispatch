import server as srv


def _border(width):
    inner = width - 2
    return "╭" + "─" * inner + "╮"


def test_trim_stale_width_drops_pre_resize_reprint():
    old = [_border(70), "│ old copy at 70 cols │", _border(70)]
    new = [_border(52), "│ new copy at 52 cols │", _border(52)]
    lines = old + new
    result = srv.clean_history(lines)
    assert result == new


def test_trim_stale_width_unchanged_when_single_width():
    lines = [_border(52), "│ hello │", _border(52), "│ world │", _border(52)]
    result = srv._trim_stale_width(lines)
    assert result == lines


def test_collapse_repeats_dedupes_identical_composer_boxes():
    block = [_border(52), "│ composer │", _border(52)]
    lines = block + block
    result = srv._collapse_repeats(lines)
    assert result == block


def test_collapse_repeats_preserves_identical_plain_code_lines():
    lines = ["    x = 1", "    x = 1"]
    result = srv._collapse_repeats(lines)
    assert result == lines


def test_stray_narrow_box_does_not_trim_history():
    """One odd-width box inside the chat is not a resize — history stays."""
    lines = (["╭" + "─" * 50 + "╮", "│ real chat above          │", "╰" + "─" * 50 + "╯"]
             + ["╭" + "─" * 20 + "╮", "│ narrow one-off │", "╰" + "─" * 20 + "╯"]
             + ["╭" + "─" * 50 + "╮", "│ live composer            │", "╰" + "─" * 50 + "╯"])
    assert srv._trim_stale_width(lines) == lines
