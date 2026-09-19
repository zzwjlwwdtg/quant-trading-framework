"""_baseline_manifest.py — 生成"当前运行版本"完整指纹, 让"测试跑过 = 什么版本?"有答案.

WP00 (audit 2026-09-19): 之前依赖 git HEAD 表示版本, 但 audit 时工作树有 33 个
未提交修改; SHA 完全不代表真实运行的源码. 现在:

- 每个 root-level .py: SHA-256 + mtime + size
- 关键 config/signals JSON: SHA-256 + mtime
- 依赖清单 (Python + 关键第三方 lib version, 不含密钥)
- Git 状态摘要: HEAD, 未提交文件数量 (WP00 要求"识别未提交修改")

输出 signals/baseline_manifest_{ts}.json, 供:
- audit 报告引用 ("跑测试时哪个版本")
- 回滚/复现 (对比两个 manifest 差异)
- 后台 healthcheck ("运行的代码 vs 期望版本")

CLI: python _baseline_manifest.py [--out PATH]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()
    except Exception:
        return "error"


def _file_meta(path: Path) -> dict:
    try:
        st = path.stat()
        return {
            "path":   str(path.relative_to(SCRIPT_DIR)),
            "sha256": _sha256(path),
            "size":   st.st_size,
            "mtime":  datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).isoformat(),
        }
    except Exception as e:
        return {"path": str(path), "error": str(e)[:80]}


def _root_python_files() -> list[dict]:
    """所有根层 .py (不包含 tests/ 子目录)."""
    return sorted(
        (_file_meta(p) for p in SCRIPT_DIR.glob("*.py")),
        key=lambda x: x.get("path", ""),
    )


def _key_signal_files() -> list[dict]:
    """thesis / calibration / regime / cohort 等决策相关 signals."""
    keys = [
        "thesis_config.json",
        "thesis_archive.jsonl",
        "confidence_calibration.json",
        "regime_state.json",
        "backtest_verdicts/",   # 目录 → 只记 mtime, 内容 hash 太多
    ]
    out = []
    for k in keys:
        p = SCRIPT_DIR / "signals" / k
        if p.exists():
            if p.is_dir():
                out.append({
                    "path":  f"signals/{k}",
                    "type":  "directory",
                    "count": len(list(p.glob("*.json"))),
                    "mtime": datetime.fromtimestamp(p.stat().st_mtime, tz=timezone.utc).isoformat(),
                })
            else:
                m = _file_meta(p)
                out.append(m)
    return out


def _python_env() -> dict:
    """Python + 关键 lib version. 不含密钥, 只读版本."""
    libs = {}
    for pkg in ["yfinance", "pandas", "numpy", "moomoo", "hmmlearn",
                 "scikit-learn", "requests"]:
        try:
            mod = __import__(pkg.replace("-", "_").replace("scikit_learn", "sklearn"))
            ver = getattr(mod, "__version__", "unknown")
            libs[pkg] = ver
        except ImportError:
            libs[pkg] = "not_installed"
        except Exception:
            libs[pkg] = "error"
    return {
        "python_version": sys.version.split()[0],
        "platform":       sys.platform,
        "libs":           libs,
    }


def _git_status() -> dict:
    """Git HEAD + 未提交文件计数. WP00: SHA 不代表运行版本, 必须报告 dirty 状态."""
    try:
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=SCRIPT_DIR, capture_output=True, text=True, timeout=10,
        ).stdout.strip()
    except Exception:
        head = "unknown"
    try:
        # --porcelain: 格式为 "XY path" 其中 X=index-status Y=worktree-status.
        # NOT .strip()! porcelain 行首空格是有语义的 (index-status 位).
        raw = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=SCRIPT_DIR, capture_output=True, text=True, timeout=10,
        ).stdout
        status = [l for l in raw.rstrip("\n").split("\n") if l]
        modified = sum(1 for l in status if len(l) >= 2 and l[1] == "M")
        untracked = sum(1 for l in status if l.startswith("??"))
        deleted = sum(1 for l in status if len(l) >= 2 and l[1] == "D")
    except Exception:
        modified = untracked = deleted = -1
    return {
        "head":               head[:12] if head != "unknown" else head,
        "head_full":          head,
        "modified_files":     modified,
        "untracked_files":    untracked,
        "deleted_files":      deleted,
        "is_dirty":           bool(modified or deleted),
        "dirty_reason":       "SHA does not represent running code" if modified else "clean",
    }


def build_manifest() -> dict:
    """完整基线 manifest — 单一入口, 供 CLI 和 healthcheck 用."""
    return {
        "schema_version": 1,
        "generated_at":   datetime.now(timezone.utc).isoformat(),
        "git":            _git_status(),
        "python_env":     _python_env(),
        "root_py_files":  _root_python_files(),
        "key_signals":    _key_signal_files(),
    }


def _write(manifest: dict, out_path: Path) -> Path:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    return out_path


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument(
        "--out",
        default=None,
        help="Output path (default: signals/baseline_manifest_{ts}.json)",
    )
    ap.add_argument("--print", action="store_true", help="Also print summary to stdout")
    args = ap.parse_args()

    m = build_manifest()
    if args.out:
        out_path = Path(args.out)
    else:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        out_path = SCRIPT_DIR / "signals" / f"baseline_manifest_{ts}.json"
    written = _write(m, out_path)
    print(f"[baseline_manifest] wrote {written}")

    if args.print or True:
        g = m["git"]
        print(f"  git HEAD:       {g['head']}"
              + (f"  ({g['modified_files']}M/{g['untracked_files']}?"
                 f"/{g['deleted_files']}D)" if g.get("is_dirty") else "  (clean)"))
        print(f"  Python:         {m['python_env']['python_version']} ({m['python_env']['platform']})")
        print(f"  root .py files: {len(m['root_py_files'])}")
        print(f"  key signals:    {len(m['key_signals'])}")
        if g.get("is_dirty"):
            print(f"  ⚠ dirty tree: {g['dirty_reason']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
