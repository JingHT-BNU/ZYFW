"""Plugin logic regression tests with offline Selenium stubs."""
import importlib.util
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "plugins"))


def _load(name: str, relative: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


cg = _load("selftest_course_grab", "plugins/course-grab/plugin.py")
sp = _load("selftest_spoc", "plugins/spoc-autoplay/plugin.py")


class Ctx:
    def __init__(self):
        self.statuses = []
        self.logs = []

    def cfg(self, key, default=None):
        return {"headless": True}.get(key, default)

    def status(self, text, **fields):
        self.statuses.append((text, fields))

    def log(self, text):
        self.logs.append(text)

    def progress(self, **fields):
        pass


class Cell:
    def __init__(self, text):
        self.text = text


class Row:
    def __init__(self, cells):
        self.cells = cells

    def find_elements(self, by, selector):
        return self.cells


def _dialog_cell(class_num, free):
    cells = [Cell(class_num), Cell("course")] + [Cell("") for _ in range(4)]
    cells.append(Cell(str(free)))
    cells.extend(Cell("") for _ in range(5))
    return cells


def test_course_helpers() -> None:
    rows = [Row(_dialog_cell("09", 0)), Row(_dialog_cell("10", 2)),
            Row(_dialog_cell("99", 8))]
    candidates = cg._dialog_candidates(rows, ["09", "10"])
    assert [cells[0].text for cells, _ in candidates] == ["09", "10"]
    assert next(free for _cells, free in candidates if free > 0) == 2

    assert cg._normalize_courses({
        " gen06122 ": "09，10 11", "PHY12003": ["27", " 28 "], "bad": None,
    }) == {"GEN06122": ["09", "10", "11"], "PHY12003": ["27", "28"], "BAD": []}
    print("[ok] course candidate scanning and config normalization")


def test_auto_mode_keeps_polling_remaining_courses() -> None:
    calls = []
    originals = cg.make_driver, cg.login, cg.try_grab_course, cg.time.sleep
    try:
        cg.make_driver = lambda *_a, **_kw: types.SimpleNamespace(quit=lambda: None)
        cg.login = lambda *_a, **_kw: None
        cg.try_grab_course = lambda _ctx, _browser, code, _classes, attempt: (
            calls.append(code) or True)
        cg.time.sleep = lambda _seconds: None
        cg.run_auto_mode(Ctx(), {"GEN06122": [], "PHY12003": ["27"]}, 0.5)
    finally:
        cg.make_driver, cg.login, cg.try_grab_course, cg.time.sleep = originals
    assert sorted(calls) == ["GEN06122", "PHY12003"], calls
    print("[ok] auto mode attempts every configured course in one round")


def test_spoc_helpers() -> None:
    tracker = sp.Tracker(2)
    tracker.mark("a", "已完成")
    tracker.mark("a", "已完成")
    tracker.mark("b", "出错")
    tracker.mark("b", "已完成")
    assert tracker.snapshot()["done"] == 2

    assert sp._bounded_number("4.5", 3, 1, 16) == 4.5
    assert sp._bounded_number("bad", 3, 1, 16) == 3
    assert sp._bounded_number(-9, 3, 1, 16) == 1
    assert sp._bounded_number(99, 3, 1, 16) == 16

    originals = sp.list_leaves, sp.fetch_schedule
    try:
        sp.list_leaves = lambda *_a: [
            {"id": 1, "leaf_type": 0, "shown": True, "locked": False, "name": "A"},
            {"id": 1, "leaf_type": 0, "shown": True, "locked": False, "name": "dup"},
        ]
        sp.fetch_schedule = lambda *_a: {}
        targets = sp.collect_targets(
            Ctx(), None, [("sign", "cid"), ("sign", "cid")], True)
    finally:
        sp.list_leaves, sp.fetch_schedule = originals
    assert len(targets) == 1, targets
    print("[ok] SPOC tracker counts unique outcomes and deduplicates targets")


def main() -> None:
    test_course_helpers()
    test_auto_mode_keeps_polling_remaining_courses()
    test_spoc_helpers()
    print("ALL PLUGIN TESTS PASSED")


if __name__ == "__main__":
    main()
