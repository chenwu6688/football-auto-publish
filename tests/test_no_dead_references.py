"""检测所有被调用的函数在模块中有定义。

防止「重构删除函数后 main() 仍然调用已删除函数」这类 bug。

命令行用法:
    python3 tests/test_no_dead_references.py          # 详细报告
    python3 -m pytest tests/test_no_dead_references.py  # pytest 模式

工作原理:
    AST 解析每个 .py 文件，找出所有函数定义/导入和函数调用，
    检查每个调用是否在「已定义 + 已导入 + 内置函数」集合中。
"""

import ast
import builtins
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent

FILES_TO_CHECK = [
    "orchestrator.py",
    "publisher.py",
]

# 合法的名称
BUILTIN_NAMES = set(dir(builtins))
ALLOWED_NAMES = BUILTIN_NAMES | {"True", "False", "None"}

# 通过 module.func() 调用的已知模块名
KNOWN_MODULES = {"data_collector", "file_writer", "image_service",
                 "constants", "utils", "logger"}


def get_defined_and_called(filepath):
    """返回 (defined_names, calls_with_lines, imports)"""
    with open(filepath, "r", encoding="utf-8") as f:
        tree = ast.parse(f.read(), filename=str(filepath))

    defined = set()
    imports = {}
    calls = []

    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            defined.add(node.name)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    defined.add(target.id)
        elif isinstance(node, ast.AnnAssign):
            if isinstance(node.target, ast.Name):
                defined.add(node.target.id)
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                name = alias.asname if alias.asname else alias.name
                imports[name] = f"{node.module}.{alias.name}"
        elif isinstance(node, ast.Import):
            for alias in node.names:
                name = alias.asname if alias.asname else alias.name
                imports[name] = alias.name
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            calls.append((node.lineno, node.func.id))

    return defined, imports, calls


def check_file(filepath):
    """返回 [(行号, 函数名), ...] 的未定义调用列表。"""
    defined, imports, calls = get_defined_and_called(filepath)
    available = defined | set(imports.keys()) | ALLOWED_NAMES

    undefined = []
    for line, name in calls:
        if name not in available and name not in KNOWN_MODULES:
            undefined.append((line, name))
    return undefined


def run_check(verbose=True):
    """运行所有检查，返回是否有错误。"""
    all_errors = []
    for filename in FILES_TO_CHECK:
        filepath = PROJECT_ROOT / filename
        if not filepath.exists():
            all_errors.append(f"❌ 文件不存在: {filename}")
            continue

        undefined = check_file(filepath)
        if undefined:
            for line, name in undefined:
                all_errors.append(f"   L{line}: 调用 `{name}()` 但未定义且未导入")
            all_errors.insert(0,
                f"❌ {filename}: {len(undefined)} 个未定义的函数调用")

    if all_errors:
        if verbose:
            print("\n" + "=" * 70)
            print("🧪 静态引用检查失败！发现未定义的函数调用")
            print("=" * 70)
            for err in all_errors:
                print(err)
            print("=" * 70)
            print("这些函数可能在重构时被删除，但代码中仍有调用。")
            print("=" * 70 + "\n")
        return False
    else:
        if verbose:
            print(f"\n✅ 静态引用检查通过: 所有调用函数都有定义\n")
        return True


# ---- pytest 测试接口 ----
def test_all_functions_defined():
    result = run_check(verbose=False)
    assert result, (
        "发现未定义的函数调用！请运行 python3 tests/test_no_dead_references.py 查看详情"
    )


def test_missing_functions_count():
    total_undefined = 0
    for filename in FILES_TO_CHECK:
        filepath = PROJECT_ROOT / filename
        if filepath.exists():
            total_undefined += len(check_file(filepath))
    assert total_undefined == 0, (
        f"发现 {total_undefined} 个未定义的函数调用"
    )


if __name__ == "__main__":
    ok = run_check(verbose=True)
    sys.exit(0 if ok else 1)
