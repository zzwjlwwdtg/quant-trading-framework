"""
_capture_screenshots.py — 无头 Chromium 自动截图 dashboard, 写到 docs/

跑法:
  # 用 live GitHub Pages (默认, 稳定, 与用户看到的一致)
  python _capture_screenshots.py

  # 用 local webui (需 webui.bat 在跑, 数据最新)
  python _capture_screenshots.py --local

  # 用本地 docs/index.html (需先跑 snapshot_generator)
  python _capture_screenshots.py --static

截 4 张:
  docs/dashboard-full.png              主视图 (默认折叠)
  docs/dashboard-full-expanded.png     ?expand=all
  docs/supply-chain-nvda-graph.png     ?graph=NVDA&depth=1
  docs/supply-chain-nvda-depth2.png    ?graph=NVDA&depth=2

每张:
  1. 打开 URL, 等 fetchJson 完成 (最多 60s, 直到 loading spinner 消失)
  2. 全页面截图 (full_page=True)
  3. 存到 docs/ 覆盖同名
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DOCS_DIR = REPO_ROOT / "docs"

# ── URL 源 ──────────────────────────────────────────────────────────────────
LIVE_URL  = "https://zzwjlwwdtg.github.io/quant-trading-framework/"
LOCAL_URL = "http://127.0.0.1:8080/"
STATIC_URL = (DOCS_DIR / "index.html").resolve().as_uri()


# ── 截图任务 ────────────────────────────────────────────────────────────────
# (输出文件名, URL 后缀, 视窗尺寸, 首选源)
#   "live"  = GitHub Pages 静态快照 (有 policy_toolkit 等预算好的 JSON)
#   "local" = 本地 webui (有实时后端 API, 支持 depth=2 BFS)
#   "auto"  = 用 --local / 默认 live
CAPTURES = [
    ("dashboard-full.png",           "",                    (1600, 900), "auto"),
    ("dashboard-full-expanded.png",  "?expand=all",         (1600, 900), "auto"),
    # depth=2 BFS 需 14+ 个 layer-1 ticker 的 depth-1 JSON, 静态快照只有 15 个,
    # 强制走本地 webui 后端能补齐; depth=1 用哪个都行, 一起走本地保持一致.
    ("supply-chain-nvda-graph.png",  "?graph=NVDA&depth=1", (1400, 900), "local"),
    ("supply-chain-nvda-depth2.png", "?graph=NVDA&depth=2", (1400, 900), "local"),
]


def _wait_for_data_loaded(page, timeout_sec: int = 60) -> None:
    """等 dashboard 数据 fetch 完成 —— 心跳看有没有 signal 卡出现或者 loading 文案消失。
    dashboard 用 fetchJson 并发拉多个 endpoint, 我们盯 body 里的一些标志物文本。"""
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        try:
            # 优先检测: signal-card 已渲染
            # 备选: 顶部 macro block 显示了 bond monitor 数字
            cards = page.locator("div.signal-card").count()
            if cards >= 3:
                # 再等 3s 让所有 fetch 收尾 (期权/GEX/supply chain 是懒加载)
                page.wait_for_timeout(3000)
                return
        except Exception:
            pass
        page.wait_for_timeout(500)
    # 超时也继续截, 只 print warning
    print(f"  warn: data-loaded probe timeout {timeout_sec}s, capturing anyway")


def _wait_for_graph(page, timeout_sec: int = 90) -> None:
    """等蜘蛛网 SVG 渲染出来 (?graph=... 会弹 modal + D3 生成节点)。

    depth=1: 通常 <5s 因为 layer-1 cache 命中
    depth=2: 首次 30-60s (BFS 展开所有下游的下游)
    检测: modal 内的 svg circle ≥ 5 = 图已渲染
    """
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        try:
            # 只看 modal 内的 svg, 避免匹配主页其它 SVG (期权墙等)
            circles = page.locator("#sc-modal svg circle").count()
            if circles >= 5:
                page.wait_for_timeout(3500)  # 让 D3 力导向 layout stabilize
                return
        except Exception:
            pass
        page.wait_for_timeout(1000)
    print(f"  warn: graph probe timeout {timeout_sec}s")


def capture(default_base: str, default_label: str) -> list[Path]:
    from playwright.sync_api import sync_playwright
    saved: list[Path] = []

    with sync_playwright() as p:
        browser = p.chromium.launch()
        for filename, suffix, (vw, vh), preferred in CAPTURES:
            # 决定单张的 base:
            #   preferred="live"  → 强制 LIVE_URL
            #   preferred="local" → 强制 LOCAL_URL
            #   preferred="auto"  → 用 default (--local / 默认 live)
            if preferred == "live":
                base_url, source_label = LIVE_URL, "live"
            elif preferred == "local":
                base_url, source_label = LOCAL_URL, "local"
            else:
                base_url, source_label = default_base, default_label
            url = base_url + suffix
            out_path = DOCS_DIR / filename
            print(f"[{source_label}] {filename} <- {url} ({vw}x{vh})")

            context = browser.new_context(
                viewport={"width": vw, "height": vh},
                device_scale_factor=1,  # 1x, 不 retina
            )
            page = context.new_page()
            try:
                page.goto(url, wait_until="networkidle", timeout=90_000)
            except Exception as e:
                print(f"  warn: goto networkidle timeout: {e}")

            is_graph = "graph=" in suffix
            is_depth2 = "depth=2" in suffix
            if is_graph:
                # 主页数据 + 蜘蛛网都要等
                _wait_for_data_loaded(page, timeout_sec=45)
                # depth=2 首次 30-60s BFS, 给 120s buffer
                _wait_for_graph(page, timeout_sec=120 if is_depth2 else 30)
                # 只截 modal (蜘蛛网 overlay), 不要拖着下面整页 dashboard
                try:
                    modal = page.locator("#sc-modal")
                    modal.screenshot(path=str(out_path))
                except Exception as e:
                    print(f"  warn: modal locator failed ({e}), fallback viewport")
                    page.screenshot(path=str(out_path), full_page=False)
            else:
                _wait_for_data_loaded(page, timeout_sec=60)
                page.screenshot(path=str(out_path), full_page=True)
            saved.append(out_path)
            print(f"  saved: {out_path.name} ({out_path.stat().st_size // 1024} KB)")
            context.close()

        browser.close()
    return saved


def main():
    ap = argparse.ArgumentParser()
    grp = ap.add_mutually_exclusive_group()
    grp.add_argument("--local", action="store_true",
                     help="use http://127.0.0.1:8080 (needs webui.bat)")
    grp.add_argument("--static", action="store_true",
                     help="use file:// docs/index.html (needs snapshot first)")
    args = ap.parse_args()

    if args.local:
        base, label = LOCAL_URL, "local"
    elif args.static:
        base, label = STATIC_URL, "static"
    else:
        base, label = LIVE_URL, "live"

    print(f"== capturing from {label}: {base} ==")
    saved = capture(base, label)
    print(f"\n== done: {len(saved)} screenshots ==")
    for p in saved:
        print(f"  {p.relative_to(REPO_ROOT)}")


if __name__ == "__main__":
    main()
