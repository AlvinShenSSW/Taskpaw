"""Exercise only the frozen V2 tray method's used Pillow APIs, without a GUI."""

import ast
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


def test_pillow_tray_image_contract(monkeypatch):
    pillow = pytest.importorskip("PIL.Image")
    source = Path(__file__).resolve().parents[1] / "taskpaw.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    method = next(
        node
        for cls in tree.body
        if isinstance(cls, ast.ClassDef) and cls.name == "TaskPawApp"
        for node in cls.body
        if isinstance(node, ast.FunctionDef) and node.name == "_create_tray_icon"
    )
    icons, threads = [], []

    def icon(name, image, title, menu):
        value = SimpleNamespace(
            name=name, image=image, title=title, menu=menu, run=lambda: None
        )
        icons.append(value)
        return value

    monkeypatch.setitem(
        sys.modules,
        "pystray",
        SimpleNamespace(
            Menu=lambda *args: args, MenuItem=lambda *args: args, Icon=icon
        ),
    )
    namespace = {
        "APP_NAME": "TaskPaw",
        "threading": SimpleNamespace(
            Thread=lambda **kwargs: SimpleNamespace(
                start=lambda: threads.append(kwargs)
            )
        ),
    }
    # Execute just the method AST: no module setup, Tk creation, user paths or app.
    exec(
        compile(ast.Module(body=[method], type_ignores=[]), str(source), "exec"),
        namespace,
    )
    app = SimpleNamespace(
        _show_window=lambda: None,
        _quit_from_tray=lambda: None,
        _quit=lambda: pytest.fail("unexpected tray fallback"),
    )
    namespace["_create_tray_icon"](app)
    assert len(icons) == len(threads) == 1
    image = icons[0].image
    assert isinstance(image, pillow.Image)
    assert image.mode == "RGBA" and image.size == (64, 64)
    assert image.getpixel((0, 0)) == (74, 158, 222, 255)
    assert image.getpixel((32, 32)) == (255, 255, 255, 200)
    assert [entry[0] for entry in icons[0].menu] == ["Show Window", "Exit"]
    assert threads[0]["daemon"] is True
