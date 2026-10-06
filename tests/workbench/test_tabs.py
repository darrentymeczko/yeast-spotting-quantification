"""Mouse gestures must reorder documents without accidentally closing them."""

import tkinter as tk

import pytest

from workbench.tabs import TabStrip


@pytest.fixture
def strip(tk_root):
    window = tk.Toplevel(tk_root)
    window.geometry("1000x120+0+0")
    closed, reordered = [], []
    tabs = TabStrip(window, on_select=lambda key: tabs.select(key),
                    on_close=closed.append,
                    on_reorder=lambda: reordered.append(tabs.keys()))
    tabs.pack(fill="x")
    keys = [object() for _ in range(4)]
    for i, label in enumerate(("Home", "Short", "A much longer document title", "Last")):
        tabs.add(keys[i], label, "home", closable=i != 0, movable=i != 0)
    tabs.select(keys[1])
    window.update()
    yield tabs, keys, closed, reordered
    window.destroy()


def gesture(widget, sequence, x, y):
    widget.event_generate(sequence, x=x - widget.winfo_rootx(),
                          y=y - widget.winfo_rooty(), rootx=x, rooty=y)
    widget.update()


def drag(tabs, widget, x, y=None):
    start_x = widget.winfo_rootx() + 2
    start_y = widget.winfo_rooty() + 1
    y = start_y if y is None else y
    gesture(widget, "<ButtonPress-1>", start_x, start_y)
    gesture(widget, "<B1-Motion>", x, y)
    gesture(widget, "<ButtonRelease-1>", x, y)


@pytest.mark.parametrize("part", ["frame", "text", "icon", "bar", "inner"])
def test_drag_from_each_part_moves_both_directions(strip, part):
    tabs, keys, closed, reordered = strip
    tab = tabs._tabs[id(keys[1])]
    widget = tab.text.master if part == "inner" else getattr(tab, part)
    last = tabs._tabs[id(keys[-1])].frame
    right = last.winfo_rootx() + last.winfo_width() + 10
    drag(tabs, widget, right)
    assert tabs.keys() == [keys[0], keys[2], keys[3], keys[1]]
    assert tabs.row.pack_slaves() == [tabs._tabs[id(k)].frame for k in tabs.keys()]
    assert tabs.selected is keys[1]
    assert reordered == [tabs.keys()]
    drag(tabs, widget, tabs.winfo_rootx() - 20)
    assert tabs.keys() == keys  # Home remains the first tab.
    assert tabs.row.pack_slaves() == [tabs._tabs[id(k)].frame for k in keys]
    assert not tabs._marker.winfo_ismapped()
    assert closed == []


def test_drop_marker_and_release_outside_cancel(strip):
    tabs, keys, _, reordered = strip
    widget = tabs._tabs[id(keys[1])].text
    x, y = widget.winfo_rootx() + 2, widget.winfo_rooty() + 1
    gesture(widget, "<ButtonPress-1>", x, y)
    gesture(widget, "<B1-Motion>", tabs.winfo_rootx() + 900, y)
    assert tabs._marker.winfo_ismapped()
    assert tabs.keys() == keys
    gesture(widget, "<ButtonRelease-1>", x + 900, y + 100)
    assert tabs.keys() == keys
    assert reordered == []
    assert not tabs._marker.winfo_ismapped()


def test_click_jitter_selects_without_reordering(strip):
    tabs, keys, closed, reordered = strip
    widget = tabs._tabs[id(keys[2])].text
    drag(tabs, widget, widget.winfo_rootx() + 3)
    assert tabs.selected is keys[2]
    assert tabs.keys() == keys
    assert reordered == closed == []


def test_close_button_does_not_start_a_drag(strip):
    tabs, keys, closed, reordered = strip
    widget = tabs._tabs[id(keys[1])].close
    drag(tabs, widget, tabs.winfo_rootx() + 900)
    assert closed == [keys[1]]
    assert tabs.keys() == keys
    assert reordered == []


def test_home_is_fixed_and_removing_dragged_tab_clears_marker(strip):
    tabs, keys, _, reordered = strip
    drag(tabs, tabs._tabs[id(keys[0])].text, tabs.winfo_rootx() + 900)
    assert tabs.keys() == keys
    assert not tabs.move(keys[0], 3)
    widget = tabs._tabs[id(keys[1])].text
    x, y = widget.winfo_rootx() + 2, widget.winfo_rooty() + 1
    gesture(widget, "<ButtonPress-1>", x, y)
    gesture(widget, "<B1-Motion>", x + 700, y)
    tabs.remove(keys[1])
    assert tabs._drag_tab is None
    assert not tabs._marker.winfo_ismapped()
    assert reordered == []
