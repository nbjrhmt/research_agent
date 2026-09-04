"""
scripts/check_lock_consistency.py —— requirements 锁文件一致性校验(CI 专用, 离线、无网络)

用途(P1-2 CI 第③项): 校验依赖锁文件 requirements-lock.txt 与顶层声明 requirements.txt
是否保持一致, 防止"声明升级了依赖版本但锁文件未重新生成"导致 CI 与本地可复现安装
(requirements-lock.txt)行为不一致。

校验规则:
    1. requirements-lock.txt 的每一行必须是 `name==version` 精确锁定格式(空行/注释行忽略);
    2. requirements.txt 里声明的每个顶层依赖(如 langgraph>=0.2.0), 其版本约束
       (>= / == / ~= / > / < / != 等)必须能在锁文件中找到对应包, 且锁定的精确版本
       满足该约束;
    3. 不一致时报错并以退出码 1 结束(CI 失败), 并给出修复提示。

用法:
    python scripts/check_lock_consistency.py
退出码: 0 = 一致; 1 = 存在不一致(CI 应失败)。

修复方式(依赖升级后需要重新生成锁文件):
    pip install pip-tools
    pip-compile --output-file=requirements-lock.txt requirements.txt
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REQ_FILE = ROOT / "requirements.txt"
LOCK_FILE = ROOT / "requirements-lock.txt"

# 简单回退解析器所需: 每段版本号按非数字分隔后转整数元组比较(如 1.2.3 / 1.2.3rc1 / 2.0.0.post1)
_VERSION_TOKEN_RE = re.compile(r"\d+")


def _version_key(text: str):
    return tuple(int(n) for n in _VERSION_TOKEN_RE.findall(text or ""))


def _canonical(name: str) -> str:
    """包名归一化: 小写并把 - _ . 全部折叠为 - (与 pip/packaging 规则一致)。"""
    return re.sub(r"[-_.]+", "-", (name or "").strip().lower())


def _parse_simple_requirement(line: str):
    """无 packaging 依赖时的简单解析: 返回 (规范名, 运算符, 版本)。"""
    m = re.match(r"^\s*([A-Za-z0-9_.\-]+)\s*(>=|<=|==|!=|~=|>|<)?\s*([0-9A-Za-z.\-]+)?\s*$", line)
    if not m:
        raise ValueError(f"无法解析 requirements.txt 行: {line!r}")
    return _canonical(m.group(1)), m.group(2) or ">=", m.group(3) or "0"


def _spec_satisfied(operator: str, want: str, locked: str) -> bool:
    """简单版本比较(数字段逐位比较), 用于 packaging 缺失时的回退。"""
    if operator in (None, "", ">="):
        return _version_key(locked) >= _version_key(want)
    if operator == "==":
        return _version_key(locked) == _version_key(want)
    if operator == "~=":
        return _version_key(locked) >= _version_key(want)
    if operator == ">":
        return _version_key(locked) > _version_key(want)
    if operator == "<":
        return _version_key(locked) < _version_key(want)
    if operator == "<=":
        return _version_key(locked) <= _version_key(want)
    if operator == "!=":
        return _version_key(locked) != _version_key(want)
    return True


def _load_top_level_requirements() -> list:
    """读取 requirements.txt 顶层声明, 返回 [(规范名, Requirement/None), ...]。"""
    try:
        from packaging.requirements import Requirement as _Req
    except ImportError:
        _Req = None

    declared = []
    for raw in REQ_FILE.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()   # 去掉行内注释
        if not line:
            continue
        if line.startswith(("-", "index", "--")):
            print(f"[warn] 跳过非普通依赖行: {line!r}(锁文件一致性仅校验普通依赖)")
            continue
        if _Req is not None:
            try:
                declared.append((_Req(line), None))
                continue
            except Exception:  # noqa: BLE001 —— 解析失败落回简单解析器
                pass
        name, op, ver = _parse_simple_requirement(line)
        declared.append((None, (name, op, ver)))
    return declared


def _load_lock_pins() -> dict:
    """读取 requirements-lock.txt, 返回 {规范名: 精确版本}。"""
    pins = {}
    for lineno, raw in enumerate(LOCK_FILE.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        m = re.match(r"^([A-Za-z0-9_.\-]+)==(.+)$", line)
        if not m:
            raise ValueError(f"requirements-lock.txt 第 {lineno} 行不是精确锁定格式(name==version): "
                             f"{line!r}; 请用 pip-compile 重新生成锁文件")
        pins[_canonical(m.group(1))] = m.group(2).strip()
    return pins


def main() -> int:
    if not REQ_FILE.exists() or not LOCK_FILE.exists():
        print(f"缺少 {REQ_FILE.name} 或 {LOCK_FILE.name}, 无法校验", file=sys.stderr)
        return 1

    errors = []
    try:
        pins = _load_lock_pins()
    except ValueError as exc:
        print(f"❌ {exc}", file=sys.stderr)
        return 1
    print(f"锁文件共锁定 {len(pins)} 个包: {LOCK_FILE.name}")

    for req, fallback in _load_top_level_requirements():
        if req is not None:  # packaging 解析成功
            name = _canonical(req.name)
            spec = req.specifier
            if name not in pins:
                errors.append(f"顶层依赖 {req.name} 未出现在锁文件中")
                continue
            locked = pins[name]
            try:
                from packaging.version import Version

                if not spec.contains(Version(locked), prereleases=True):
                    errors.append(f"锁定版本不满足约束: {req.name} 声明 {req.specifier}, "
                                  f"锁文件锁定 {locked}")
            except ImportError:  # 理论不可达(上面已 import 成功)
                pass
            continue
        # 回退简单解析
        name, op, ver = fallback
        if name not in pins:
            errors.append(f"顶层依赖 {name} 未出现在锁文件中")
            continue
        if not _spec_satisfied(op, ver, pins[name]):
            errors.append(f"锁定版本不满足约束: {name} {op}{ver}, 锁文件锁定 {pins[name]}")

    if errors:
        print(f"❌ 依赖锁文件不一致, 共 {len(errors)} 处问题:", file=sys.stderr)
        for err in errors:
            print(f"   - {err}", file=sys.stderr)
        print("\n修复建议: 在项目根目录执行", file=sys.stderr)
        print("    pip install pip-tools", file=sys.stderr)
        print("    pip-compile --output-file=requirements-lock.txt requirements.txt", file=sys.stderr)
        print("然后重新提交 requirements-lock.txt。", file=sys.stderr)
        return 1

    print("✅ requirements.txt 全部顶层依赖的版本约束在 requirements-lock.txt 中均满足")
    return 0


if __name__ == "__main__":
    sys.exit(main())
