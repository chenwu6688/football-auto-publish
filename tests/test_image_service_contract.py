"""`ImageService` 的契约守卫（替代原 `test_image_phash.py` 的 12 条行为断言）。

2026-11-11 把 `_compute_phash` / `search_images` / `download_article_images` /
`capture_match_screenshot` 四条旧路径删掉了（0 生产调用方）。原来盯它们的
8 条用例随之消失，但**它们顺带守着的三件事必须留下**：

  ① `download_image` 的返回键集合（删改时最容易顺手删掉 `md5`）；
  ② 生产模块里不许出现本机绝对路径（`/home/<某人>/...`，本地永不报错、换机器才炸）；
  ③ 不许再出现裁图/去水印类方法（去 logo 转载在法律上升风险）。
  ④ 新增：全仓不得再出现 `Image.getdata()`——删掉 `_compute_phash` 之后它是
     Pillow 14（2027-10-15 移除）唯一的历史入口，这条守卫防止它换个地方复活。

判据一律走 AST 或真实调用，**不拿注释和文档字符串当证据**。
"""

import ast
import io
import random
from pathlib import Path

from PIL import Image

import constants
import file_writer
import image_service

ROOT = Path(image_service.__file__).parent

# 生产模块：不允许出现本机绝对路径
PRODUCTION_MODULES = ("image_service.py", "file_writer.py", "orchestrator.py",
                      "constants.py", "content_stats.py")


# ---------------------------------------------------------------- 返回键契约
class _FakeResp:
    """最小化的 requests.Response 替身：只喂 `_download_one` 真正读的四个方法。"""

    def __init__(self, data: bytes):
        self._data = data
        self.headers = {"content-length": str(len(data))}

    def raise_for_status(self):
        pass

    def iter_content(self, chunk_size=8192):
        yield self._data


def _noise_jpeg(w: int = 600, h: int = 400) -> bytes:
    """造一张够大的噪声 JPEG（`min_size_bytes` 默认 50KB，纯色图会被拒）。"""
    im = Image.new("RGB", (w, h))
    px = im.load()
    rnd = random.Random(20261111)
    for x in range(w):
        for y in range(h):
            px[x, y] = (rnd.randrange(256), rnd.randrange(256), rnd.randrange(256))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=95)
    return buf.getvalue()


def test_download_image_returns_expected_keys(monkeypatch, tmp_path):
    """`download_image` 成功时的键集合是发布侧的唯一契约。

    2026-11-11 删 `_compute_phash` 时把 `"phash"` 键一并去掉：它既没有读取方，
    也没有进过 `metadata.json`（实测 0 次）。这里把剩下的 6 个键钉死，
    防止下次删改顺手带走 `md5`（MD5 是**唯一在用的**去重键）。
    """
    svc = image_service.ImageService({"images": {"min_width": 600, "min_height": 400}})
    monkeypatch.setattr(svc.session, "get", lambda *a, **k: _FakeResp(_noise_jpeg()))

    got = svc.download_image("https://example.invalid/a.jpg", tmp_path, "p", 1, set())

    assert got is not None, "闸门没放行（尺寸或体积判据与夹具不匹配）"
    assert sorted(got) == ["filename", "height", "local_path", "md5", "url", "width"]
    assert len(got["md5"]) == 32, "MD5 必须落成 32 位十六进制，跨批去重靠它"


def test_removed_legacy_methods_stay_removed():
    """被删的四条旧路径不得无声复活。

    它们都不是「坏了」，而是「没有调用方」：`search_images`/`download_article_images`
    已被 `data_collector.search_images` + orchestrator 自己的循环取代，
    `capture_match_screenshot` 全仓 0 调用且写 /tmp，`_compute_phash` 无消费者。
    要加回来就得同时给出调用方，否则这条用例会红——让你在加之前先想一次。
    """
    gone = ("search_images", "download_article_images",
            "capture_match_screenshot", "_compute_phash")
    alive = [n for n in gone if hasattr(image_service.ImageService, n)]
    assert alive == [], f"这些方法已被删除（无调用方），不要凭注释加回来：{alive}"


# ---------------------------------------------------------------- 弃用 API
def test_no_deprecated_pillow_getdata_anywhere():
    """全仓不得再调用 `Image.getdata()`（Pillow 13 弃用、14 移除）。

    扫的是真实 `ast.Attribute` 调用节点：注释、文档字符串、markdown 里的示例都不算。
    2026-11-11 之前唯一的调用点是 `_compute_phash`，随该方法一起删掉了。
    """
    skip_dirs = {".git", "__pycache__", "output", "node_modules", "data", ".workbuddy"}
    hits = []
    for path in ROOT.rglob("*.py"):
        if skip_dirs & set(path.parts):
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr == "getdata":
                hits.append(f"{path.relative_to(ROOT)}:{node.lineno}")
    assert hits == [], (
        "`getdata()` 在 Pillow 14（2027-10-15）会被移除，改用 `tobytes()`："
        + "; ".join(hits))


# ---------------------------------------------------------------- 法律/可移植性
def test_no_watermark_cropping_api():
    """不得再出现「按比例裁掉底部水印」这类方法。

    硬纪律：**严禁「去 logo」转载**——《著作权法》53 条(7) 把「故意删除权利管理
    信息」单列侵权，去 logo 不是降风险，是升风险。2026-11-11 删掉的
    `download_and_crop_image`（底部 8~12% 裁剪）就是反例，且它零调用方。
    """
    src = Path(image_service.__file__).read_text(encoding="utf-8")
    methods = [n.name for n in ast.walk(ast.parse(src))
               if isinstance(n, ast.FunctionDef)]
    bad = [m for m in methods if "crop" in m.lower() or "watermark" in m.lower()]
    assert bad == [], (
        f"image_service 里出现了裁图/去水印类方法 {bad}：去 logo 转载在法律上升风险，不得再引入")


def test_no_hardcoded_home_path_in_production_modules():
    """生产模块里不许出现 `/home/<某人>/` 这类本机绝对路径字面量（AST 扫字符串常量）。"""
    offenders = []
    for name in PRODUCTION_MODULES:
        f = ROOT / name
        if not f.exists():
            continue
        for node in ast.walk(ast.parse(f.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if node.value.startswith("/home/"):
                    offenders.append(f"{name}:{node.lineno} → {node.value}")
    assert offenders == [], "生产模块里出现本机绝对路径：" + "; ".join(offenders)


def test_file_writer_default_base_dir_is_project_output(monkeypatch, tmp_path):
    """`FileWriter()` 不传参时的目录必须可移植（跟着 OUTPUT_DIR 走）。"""
    monkeypatch.setattr(constants, "OUTPUT_DIR", tmp_path)
    assert file_writer.FileWriter().base_dir == tmp_path


def test_file_writer_explicit_base_dir_wins(tmp_path):
    """显式传参优先于默认值。"""
    assert file_writer.FileWriter(base_dir=str(tmp_path / "x")).base_dir == tmp_path / "x"
