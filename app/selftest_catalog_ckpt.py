"""run_catalog 检查点/断点续抓/自动重启逻辑自测(离线桩对象, 不启动浏览器)。

模拟 130 个 培养层次×年级×院系 组合, 验证:
- 每完成 CHECKPOINT_EVERY(50) 个组合原子落盘一次(partial=true), 最终整档 partial=false;
- 落盘快照包含进行中的年级/院系节点(先挂载后填充, 无数据丢失);
- progress 事件携带 stage/done/total/eta_seconds;
- 断点续抓: 预置 partial 目录时跳过已抓组合(含空专业列表——那也是有效结果),
  只补剩余组合并合并缓存; 全部已缓存时零站点访问直接补齐整档;
- 持续性故障: 自动重启 CATALOG_MAX_RESTARTS 次后向上抛出, 每轮检查点已落盘;
- 瞬时故障: 自动重启一次后续抓成功, 任务正常完成;
- _read_options 对站点异步重渲染(stale)的自愈。
"""
import copy
import importlib.util
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys_path = str(ROOT / "plugins")
if sys_path not in __import__("sys").path:
    __import__("sys").path.insert(0, sys_path)

# plugin.py 不在包内, 按路径加载; 其内部会把 plugins/ 加入 sys.path 以导入 _sdk
_spec = importlib.util.spec_from_file_location(
    "cg_plugin", ROOT / "plugins" / "course-grab" / "plugin.py")
cg = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cg)

from selenium.common.exceptions import StaleElementReferenceException  # noqa: E402

# 保留真实 _read_options 引用(后续 _patch_module 会用桩替换模块属性)
REAL_READ_OPTIONS = cg._read_options

LEVELS = [{"v": f"L{i}", "t": f"层次{i}"} for i in range(2)]
GRADES = [{"v": f"G{i}", "t": f"年级{i}"} for i in range(5)]
DEPTS = [{"v": f"D{i}", "t": f"院系{i}"} for i in range(13)]   # 2×5×13 = 130 组合
MAJORS = [{"v": "M1", "t": "专业1"}, {"v": "M2", "t": "专业2"}]


class FakeCtx:
    def __init__(self):
        self.progress_calls = []
        self.status_calls = []
        self.logs = []

    def cfg(self, key, default=None):
        return {"headless": True}.get(key, default)

    def progress(self, **fields):
        self.progress_calls.append(fields)

    def status(self, text, **fields):
        self.status_calls.append(text)

    def log(self, msg):
        self.logs.append(msg)


def _patch_module(saves: list, fail_at: int | None, preset: dict | None = None,
                  select_calls: list | None = None,
                  fail_first_reads: int = 0) -> dict:
    """用桩替换插件模块的浏览器/下拉/落盘函数。

    fail_at=N 表示每轮枚举中第 N 个新抓组合读专业时抛异常(模拟持续故障);
    fail_first_reads=N 表示全局前 N 次读专业各抛一次异常(模拟瞬时故障,
    自动重启后恢复); preset 为预置的未完成目录(partial=true), 模拟断点续抓
    的起点; select_calls 记录所有 _select 调用的 value 序列。
    """
    sels = [types.SimpleNamespace(name=f"sel{i}") for i in range(4)]
    level_sel, grade_sel, dept_sel, major_sel = sels
    state = {"combo": 0, "reads": 0, "attempt": 0}

    def fake_login(ctx, browser, desk_xpath=None):
        state["attempt"] += 1
        state["combo"] = 0   # 每轮是新浏览器会话, 组合计数从零开始

    def fake_read(sel):
        if sel is major_sel:
            state["combo"] += 1
            state["reads"] += 1
            if fail_at is not None and state["combo"] == fail_at:
                raise RuntimeError("模拟网络中断")
            if state["reads"] <= fail_first_reads:
                raise RuntimeError("模拟瞬时连接中断")
            return MAJORS
        return {id(level_sel): LEVELS, id(grade_sel): GRADES,
                id(dept_sel): DEPTS}[id(sel)]

    def fake_save(path, data):
        # 组合数 = 院系节点数(每个院系节点对应一次"读专业", 即一个组合)
        n = sum(len(g["depts"]) for l in data["levels"] for g in l["grades"])
        saves.append({
            "partial": data["partial"],
            "combos": n,
            "levels": len(data["levels"]),
            "updated_at": data.get("updated_at"),
        })

    def fake_select(sel, value):
        if select_calls is not None:
            select_calls.append(value)

    cg.make_driver = lambda ctx, headless=True: types.SimpleNamespace(quit=lambda: None)
    cg.login = fake_login
    cg._specialty_selects = lambda browser: tuple(sels)
    cg._read_options = fake_read
    cg._select = fake_select
    cg.time.sleep = lambda s: None
    cg._save_json_atomic = fake_save
    # 必须显式桩掉: 真实实现会读到本机真实的 majors.json(可能恰有半成品)
    cg._load_partial_catalog = (lambda p=preset: p) if preset is not None else (lambda: None)
    return state


def _run(fail_at: int | None = None, preset: dict | None = None,
         fail_first_reads: int = 0) -> tuple:
    saves: list = []
    select_calls: list = []
    ctx = FakeCtx()
    state = _patch_module(saves, fail_at, preset=preset,
                          select_calls=select_calls, fail_first_reads=fail_first_reads)
    err = None
    try:
        cg.run_catalog(ctx)
    except BaseException as e:   # 模拟中断/超限时异常会向上抛
        err = e
    return saves, ctx, err, select_calls, state


def _preset_partial(cached_keys: set) -> dict:
    """构造 partial=true 目录: cached_keys 中 {(lv,gr,dp)} 视为已抓取。

    专业列表默认 MAJORS, 仅 (L0,G2,D3) 置空——空专业列表同样是有效抓取结果,
    续抓时必须同样跳过。
    """
    levels = []
    for l in LEVELS:
        ln = {"v": l["v"], "t": l["t"], "grades": []}
        for g in GRADES:
            gn = {"v": g["v"], "t": g["t"], "depts": []}
            for d in DEPTS:
                if (l["v"], g["v"], d["v"]) in cached_keys:
                    m = [] if (l["v"], g["v"], d["v"]) == ("L0", "G2", "D3") else MAJORS
                    gn["depts"].append({"v": d["v"], "t": d["t"], "majors": m})
            ln["grades"].append(gn)
        levels.append(ln)
    return {"updated_at": "preset", "partial": True, "levels": levels}


def main() -> None:
    # 1. 完整抓取: 检查点在 50/100 落盘(partial), 最终 130 整档落盘
    saves, ctx, err, _, _ = _run()
    assert err is None, err
    assert [(s["partial"], s["combos"]) for s in saves] == [
        (True, 50), (True, 100), (False, 130)], saves
    assert all(s["updated_at"] for s in saves), saves
    print("[ok] full run: checkpoints at 50/100 (partial) + final full save")

    # 检查点 50: 恰在 层次0 × 年级3 × 第11个院系 完成后
    # (先挂载后填充 => 快照含 1 个层次、4 个年级, 年级3 有 11 个院系)
    # fake_save 只记录了计数, 结构验证用桩重放: 直接再跑一次截取落盘内容。
    # 真实 _save_json_atomic 立即序列化冻结快照, 桩用深拷贝模拟同样语义,
    # 否则 levels 引用活动 tree, 断言看到的永远是最终状态
    captured: list = []
    orig = cg._save_json_atomic
    _patch_module(captured, None)
    cg._save_json_atomic = lambda path, data: captured.append(copy.deepcopy(data))
    try:
        cg.run_catalog(FakeCtx())
    finally:
        cg._save_json_atomic = orig
    ck50 = captured[0]
    assert ck50["partial"] is True
    assert len(ck50["levels"]) == 1, len(ck50["levels"])
    assert len(ck50["levels"][0]["grades"]) == 4
    assert len(ck50["levels"][0]["grades"][3]["depts"]) == 11
    assert ck50["levels"][0]["grades"][3]["depts"][-1]["majors"] == MAJORS
    ck100 = captured[1]
    assert len(ck100["levels"]) == 2
    assert len(ck100["levels"][1]["grades"]) == 3
    assert len(ck100["levels"][1]["grades"][2]["depts"]) == 9
    print("[ok] checkpoint snapshot contains in-progress grade/dept nodes")

    # 2. progress 事件: 1 条初始(done=0, 让界面先拿到 total) + 每组合 1 条
    pc = ctx.progress_calls
    assert len(pc) == 131, len(pc)
    assert all(p["stage"] == "catalog" for p in pc)
    assert all(p["total"] == 130 for p in pc)
    assert [p["done"] for p in pc] == list(range(0, 131))
    assert pc[0].get("eta_seconds") is None   # 初始事件尚无耗时数据
    assert all(isinstance(p["eta_seconds"], int) and p["eta_seconds"] >= 0
               for p in pc[1:])
    print("[ok] progress events carry stage/done/total/eta_seconds")

    # 3. 持续性故障(每轮第 120 个组合读专业必失败): 自动重启 5 次后向上抛出,
    #    每轮的检查点均已落盘, 已抓组合不丢
    saves, ctx, err, _, _ = _run(fail_at=120)
    assert isinstance(err, RuntimeError), err
    max_restarts = cg.CATALOG_MAX_RESTARTS
    assert sum("自动重启续抓" in s for s in ctx.status_calls) == max_restarts
    # 每轮: 初始进度 + 组合 1..119 = 120 条; 检查点 50/100 各落盘一次
    assert len(ctx.progress_calls) == (1 + max_restarts) * 120, len(ctx.progress_calls)
    assert [(s["partial"], s["combos"]) for s in saves] == [
        (True, 50), (True, 100)] * (1 + max_restarts), saves
    print("[ok] persistent failure: auto-restarts then aborts, checkpoints kept")

    # 4. 瞬时故障(全局首次读专业失败一次): 自动重启一次后续抓成功, 任务正常完成
    cached38 = {("L0", "G0", "D5")}
    cached38 |= {("L0", "G1", f"D{i}") for i in range(12)}
    cached38 |= {("L0", "G2", f"D{i}") for i in range(13)}
    cached38 |= {("L0", "G3", f"D{i}") for i in range(10)}
    cached38 |= {("L0", "G4", "D0")}
    cached38 |= {("L1", "G0", "D12")}
    assert len(cached38) == 38
    saves, ctx, err, sel_calls, state = _run(preset=_preset_partial(cached38),
                                             fail_first_reads=1)
    assert err is None, err
    assert [(s["partial"], s["combos"]) for s in saves] == [
        (True, 50), (True, 100), (False, 130)], saves
    assert state["reads"] == 93   # 首次失败 1 次 + 重启后成功 92 次
    assert sum("自动重启续抓" in s for s in ctx.status_calls) == 1
    print("[ok] transient failure auto-restarts once and completes via resume")

    # 5. 断点续抓: 预置 38 个已抓组合(含 1 个空专业列表), 只补剩余 92 个
    #    布局特意让 L0/G1 的首个未缓存组合(D12)等于上一 grade 最后处理的院系,
    #    用于触发"同值不触发 change"的强制刷新(先选空占位项再选目标值)
    saves, ctx, err, sel_calls, _ = _run(preset=_preset_partial(cached38))
    assert err is None, err
    assert [(s["partial"], s["combos"]) for s in saves] == [
        (True, 50), (True, 100), (False, 130)], saves
    # 只新抓 92 个组合; 空专业列表的 (L0,G2,D3) 也被跳过(否则会是 93 次)
    assert sum(sel_calls.count(f"D{i}") for i in range(13)) == 92, len(sel_calls)
    assert sel_calls.count("") == 1, sel_calls   # 同值强制刷新恰好一次
    # 整级全缓存的下拉不选中: 培养层次各 1 次; 年级 9 次
    # (仅 L0/G2 整级缓存跳过; L1/G0 只缓存了 D12, 其余院系仍要抓 → 照常选中)
    assert sel_calls.count("L0") == 1 and sel_calls.count("L1") == 1
    assert sum(sel_calls.count(f"G{i}") for i in range(5)) == 9
    assert sel_calls.count("G2") == 1 and sel_calls.count("G0") == 2
    print("[ok] resume: skips 38 cached combos (incl. empty-majors), grabs 92")

    # 5b. 续抓结果合并: 缓存组合(含空列表)原样保留, 新抓组合并入, 共 130 组合
    captured2: list = []
    _patch_module(captured2, None, preset=_preset_partial(cached38))
    orig = cg._save_json_atomic
    cg._save_json_atomic = lambda path, data: captured2.append(copy.deepcopy(data))
    try:
        cg.run_catalog(FakeCtx())
    finally:
        cg._save_json_atomic = orig
    final = captured2[-1]
    assert final["partial"] is False
    nodes = {(l["v"], g["v"], d["v"]): d["majors"]
             for l in final["levels"] for g in l["grades"] for d in g["depts"]}
    assert len(nodes) == 130, len(nodes)
    assert nodes[("L0", "G2", "D3")] == []        # 缓存中的空专业列表原样保留
    assert nodes[("L0", "G1", "D5")] == MAJORS    # 缓存组合原样并入
    assert nodes[("L0", "G1", "D12")] == MAJORS   # 续抓的新组合
    assert nodes[("L1", "G0", "D11")] == MAJORS   # 整级缓存的年级中未缓存部分
    print("[ok] resume merges cached + freshly grabbed nodes")

    # 6. 全部组合都已缓存(上次在最终落盘前中断): 零站点访问, 直接补齐整档
    all_keys = {(l["v"], g["v"], d["v"])
                for l in LEVELS for g in GRADES for d in DEPTS}
    saves, ctx, err, sel_calls, _ = _run(preset=_preset_partial(all_keys))
    assert err is None, err
    assert sel_calls == [], sel_calls            # 无任何下拉操作/站点查询
    assert [(s["partial"], s["combos"]) for s in saves] == [(False, 130)], saves
    assert [p["done"] for p in ctx.progress_calls] == list(range(0, 131))
    assert ctx.progress_calls[0].get("eta_seconds") is None   # 初始事件无耗时数据
    assert all(p["eta_seconds"] == 0 for p in ctx.progress_calls[1:])
    print("[ok] fully-cached partial flips to complete with zero site access")

    # 7. _read_options 对站点异步重渲染(stale)的自愈:
    #    前 2 轮 options 读取即抛 StaleElementReferenceException, 第 3 轮恢复
    class PoisonOption:
        @staticmethod
        def get_attribute(_):
            raise StaleElementReferenceException("stale")

        @property
        def text(self):
            raise StaleElementReferenceException("stale")

    class FlakySelect:
        """前 rounds 次访问 options 返回毒选项(读取即 stale), 之后返回健康选项"""

        def __init__(self, rounds):
            self.rounds = rounds
            self.reads = 0

        @property
        def options(self):
            self.reads += 1
            if self.reads <= self.rounds:
                return [PoisonOption(), PoisonOption()]
            return [types.SimpleNamespace(get_attribute=lambda _: "M1", text="专业1"),
                    types.SimpleNamespace(get_attribute=lambda _: "M2", text="专业2")]

    sel = FlakySelect(rounds=2)
    assert REAL_READ_OPTIONS(sel) == MAJORS
    assert sel.reads == 3
    # 重试耗尽仍 stale → 向上抛出, 由任务级自动重启兜底
    sel2 = FlakySelect(rounds=99)
    try:
        REAL_READ_OPTIONS(sel2, attempts=4)
        raise AssertionError("应当抛出 StaleElementReferenceException")
    except StaleElementReferenceException:
        pass
    print("[ok] _read_options self-heals from stale element re-render")

    print("ALL CATALOG CHECKPOINT TESTS PASSED")


if __name__ == "__main__":
    main()
