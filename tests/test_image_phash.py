"""配图感知哈希与「可移植默认路径」的契约测试。

2026-11-11 全量测试里发现三处真实缺陷，都在本文件盯住：

① `_compute_phash` 用 `img.getdata()` —— Pillow 13 已弃用、**Pillow 14（2027-10-15）
   直接移除**，而 `requirements.txt` 写的是 `Pillow>=10.0`（无上限）。CI 装到 14
   那天，配图去重会抛 AttributeError，整条配图链路跟着挂。改用 `tobytes()`：
   单通道灰度图的字节序与 getdata 的像素序完全一致（同一个 32×32 缓冲区）。
② `download_article_images` 的 `base_dir` 默认值是 `"/home/chenwu/每日自媒体文案"`
   —— 一台开发机的绝对路径。
③ `file_writer.FileWriter` 的 `base_dir` 默认值同样是那个本机路径。

②③ 的共同点是：生产侧每次都显式传参，所以本地永远不报错；换台机器（或 CI）才会
炸，而且失败会被 `except Exception` 吞掉，表现成「今天怎么一张图都没有」。
**默认值本身必须可移植，不能靠「调用方都记得传参」兜。**
"""

import warnings

from PIL import Image

import constants
import file_writer
import image_service


# ---------------------------------------------------------------- 夹具
def _gradient(w=200, h=150, flip=False):
    """纵向灰度渐变：翻过来后像素分布相反，phash 应当不同。"""
    im = Image.new("RGB", (w, h))
    px = im.load()
    for x in range(w):
        for y in range(h):
            v = int(255 * ((h - 1 - y) / h if flip else y / h))
            px[x, y] = (v, v, v)
    return im


def _svc():
    return image_service.ImageService()


# ---------------------------------------------------------------- phash
def test_phash_is_deterministic():
    """同一张图算两遍必须一致，否则去重键每次都变。"""
    svc = _svc()
    img = _gradient()
    assert svc._compute_phash(img) == svc._compute_phash(img)


def test_phash_is_hex_string():
    """返回值是十六进制串（`hex()[2:]` 会吃掉前导零，故长度不固定——不要断言长度）。"""
    h = _svc()._compute_phash(_gradient())
    assert h and all(c in "0123456789abcdef" for c in h)


def test_phash_does_not_use_deprecated_pillow_api():
    """回归 2026-11-11 的缺陷：算 phash 不得触发任何 Pillow 弃用告警。

    Pillow 13 的 `Image.Image.getdata()` 会发 DeprecationWarning，14 直接移除。
    这里把弃用告警升级成异常：用 `getdata` 的写法会当场红，而不是等到 2027 年
    CI 装到 Pillow 14 才在配图环节炸。
    """
    img = _gradient()
    with warnings.catch_warnings():
        warnings.simplefilter("error", DeprecationWarning)
        _svc()._compute_phash(img)                     # 触发弃用即抛错


def test_phash_is_resolution_invariant():
    """缩放到 32×32 再算，所以同一内容不同分辨率应得同一哈希。"""
    svc = _svc()
    assert svc._compute_phash(_gradient(200, 150)) == svc._compute_phash(_gradient(320, 240))


def test_phash_distinguishes_different_content():
    """内容不同（上下翻转）必须给出不同哈希，否则等于把所有图都当重复。"""
    svc = _svc()
    assert svc._compute_phash(_gradient()) != svc._compute_phash(_gradient(flip=True))


def test_phash_is_not_used_as_dedupe_key():
    """把口径写死：phash 只记录不比较，实际去重是 MD5。

    这不是「夸现状」，而是防止有人看注释写「用于去重」就以为跨分辨率重复已被拦住。
    同一内容换分辨率 → phash 相同、MD5 不同 → `_download_one` 仍会收下第二张。
    """
    svc = _svc()
    a, b = _gradient(200, 150), _gradient(320, 240)
    assert svc._compute_phash(a) == svc._compute_phash(b)
    import hashlib
    ba = a.tobytes()
    bb = b.tobytes()
    assert hashlib.md5(ba).hexdigest() != hashlib.md5(bb).hexdigest()


# ---------------------------------------------------------------- 可移植默认路径
def test_image_service_default_base_dir_is_project_output(monkeypatch, tmp_path):
    """`download_article_images` 缺省目录必须跟着 `constants.OUTPUT_DIR` 走。"""
    monkeypatch.setattr(constants, "OUTPUT_DIR", tmp_path)
    svc = _svc()
    monkeypatch.setattr(svc, "search_images", lambda *a, **k: [])
    out = svc.download_article_images("曼联", 1, "2026-01-01")
    assert out == []
    assert (tmp_path / "2026-01-01" / "images").is_dir()


def test_image_service_explicit_base_dir_wins(monkeypatch, tmp_path):
    """显式传参优先于默认值（生产侧传的就是显式路径）。"""
    elsewhere = tmp_path / "explicit"
    monkeypatch.setattr(constants, "OUTPUT_DIR", tmp_path / "output")
    svc = _svc()
    monkeypatch.setattr(svc, "search_images", lambda *a, **k: [])
    svc.download_article_images("曼联", 1, "2026-01-01", base_dir=str(elsewhere))
    assert (elsewhere / "2026-01-01" / "images").is_dir()


def test_image_service_has_no_watermark_cropping_api():
    """不得再出现「按比例裁掉底部水印」这类方法。

    项目硬纪律：**严禁「去 logo」转载**——《著作权法》53 条(7) 把「故意删除权利
    管理信息」单列侵权，去 logo 不是降风险，是升风险。2026-11-11 删掉的
    `download_and_crop_image`（底部 8~12% 裁剪）就是反例，且它零调用方。
    用 AST 查真实方法名，不拿注释当证据。
    """
    import ast
    from pathlib import Path
    src = Path(image_service.__file__).read_text(encoding="utf-8")
    methods = [n.name for n in ast.walk(ast.parse(src))
               if isinstance(n, ast.FunctionDef)]
    bad = [m for m in methods if "crop" in m.lower() or "watermark" in m.lower()]
    assert bad == [], (
        f"image_service 里出现了裁图/去水印类方法 {bad}：去 logo 转载在法律上升风险，"
        f"不得再引入")


def test_file_writer_default_base_dir_is_project_output(monkeypatch, tmp_path):
    """`FileWriter()` 不传参时的目录必须可移植（跟着 OUTPUT_DIR 走）。"""
    monkeypatch.setattr(constants, "OUTPUT_DIR", tmp_path)
    assert file_writer.FileWriter().base_dir == tmp_path


def test_file_writer_explicit_base_dir_wins(tmp_path):
    got = file_writer.FileWriter(base_dir=str(tmp_path / "x")).base_dir
    assert got == tmp_path / "x"


def test_no_hardcoded_home_path_in_production_modules():
    """生产模块里不许出现 `/home/<某人>/` 这类本机绝对路径字面量。

    2026-11-11 之前 `image_service` 与 `file_writer` 的默认值都是
    `"/home/chenwu/每日自媒体文案"`：本地跑得通，CI 上会往不存在的目录写，
    失败还被 except 吞掉。用 AST 扫字符串常量（不看注释，注释不算证据）。
    """
    import ast
    from pathlib import Path
    root = Path(image_service.__file__).parent
    offenders = []
    for name in ("image_service.py", "file_writer.py", "orchestrator.py",
                 "constants.py", "content_stats.py"):
        f = root / name
        if not f.exists():
            continue
        tree = ast.parse(f.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if node.value.startswith("/home/"):
                    offenders.append(f"{name}:{node.lineno} → {node.value}")
    assert offenders == [], "生产模块里出现本机绝对路径：" + "; ".join(offenders)
