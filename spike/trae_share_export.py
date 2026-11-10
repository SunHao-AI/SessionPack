"""spike：批量抓取 Trae 分享链接的正文与图片（Playwright 原型）。

不并入 sessionpack 核心（违背「零第三方运行时依赖」红线），仅作可行性验证
与伴生工具。已验证结论：
- 分享页是虚拟列表，须滚动 .virtualized-message-list-view__scroller 才渲染末尾消息；
- 图片在签名 CDN（Referer 校验），必须在浏览器上下文里取字节；
- 正文 <slug>.txt 可被 TraeExportAdapter 直接解析，输出目录可直接
  `sessionpack import --export-dir <输出目录>`。

用法:
    .venv\\Scripts\\python.exe spike/trae_share_export.py <链接或链接列表文件>... <输出目录> [--force]

链接列表文件：每行一个分享链接，# 开头为注释。
已抓过的链接（存在 <slug>.txt）默认跳过，--force 强制重抓。
"""

from __future__ import annotations

import sys
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright


def _slug(url: str) -> str:
    return urlparse(url).path.rstrip("/").split("/")[-1] or "share"


def _load_urls(arg: str) -> list[str]:
    """单个链接直接返回；文件则按行解析（# 注释、空行跳过）。"""
    p = Path(arg)
    if p.is_file():
        urls = [
            line.strip()
            for line in p.read_text("utf-8").splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
        return [u for u in urls if u.startswith("http")]
    if arg.startswith("http"):
        return [arg]
    print(f"跳过无效来源: {arg}")
    return []


def _fetch_one(browser, url: str) -> tuple[str, list[tuple[str, bytes]]]:
    """打开一个分享页，滚到底，返回 (正文, [(图片URL, 字节), ...])。"""
    page = browser.new_page(viewport={"width": 1280, "height": 900})
    images: list[tuple[str, bytes]] = []

    def on_response(resp):
        # 签名 CDN URL 带 Referer 校验，必须在浏览器上下文里直接取 body
        if resp.request.resource_type == "image":
            try:
                images.append((resp.url, resp.body()))
            except Exception:  # noqa: BLE001
                pass

    page.on("response", on_response)
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(3000)
        # 虚拟列表懒加载：滚动该容器到底部才能渲染末尾（含图片）的消息
        scroller = ".virtualized-message-list-view__scroller"
        try:
            page.wait_for_selector(scroller, timeout=15000)
        except Exception:  # noqa: BLE001 无虚拟列表时退化为普通滚动
            scroller = None
        last_len = 0
        for _ in range(120):
            if scroller:
                page.evaluate(
                    f"() => {{ const e = document.querySelector('{scroller}');"
                    f" if (e) e.scrollTop = e.scrollHeight; }}")
            else:
                page.mouse.wheel(0, 2000)
            page.wait_for_timeout(250)
            cur = page.evaluate("() => document.body.innerText.length")
            if cur == last_len:
                break
            last_len = cur
        page.wait_for_timeout(3000)
        text = page.inner_text("body")
        # 兜底：扫描 DOM 里的 img 标签（网络拦截漏掉的，用上下文请求取 body）
        seen = {u for u, _ in images}
        for src in page.eval_on_selector_all("img", "els => els.map(e => e.src)"):
            if src and src.startswith("http") and src not in seen:
                try:
                    images.append((src, page.request.get(src).body()))
                    seen.add(src)
                except Exception:  # noqa: BLE001
                    pass
        return text, images
    finally:
        page.close()


def _save(url: str, text: str, images: list[tuple[str, bytes]], out_dir: Path) -> int:
    slug = _slug(url)
    img_dir = out_dir / "images" / slug  # 子目录，避免污染 export 顶层
    saved = 0
    for i, (u, body) in enumerate(images):
        if not body:
            continue
        ext = Path(urlparse(u).path).suffix or ".img"
        if ext.lower() not in (".png", ".jpg", ".jpeg", ".webp", ".gif", ".img"):
            ext = ".img"
        img_dir.mkdir(parents=True, exist_ok=True)
        dest = img_dir / f"{slug}_{i}{ext}"
        dest.write_bytes(body)
        saved += 1
        print(f"    图片 {dest.relative_to(out_dir)} ({len(body)} 字节)")
    txt_path = out_dir / f"{slug}.txt"
    txt_path.write_text(text, encoding="utf-8")
    print(f"    正文 {txt_path.name} ({len(text)} 字符, 图片 {saved}/{len(images)})")
    return saved


def main(sources: list[str], out_dir: Path, force: bool = False) -> int:
    out_dir.mkdir(parents=True, exist_ok=True)
    urls: list[str] = []
    for src in sources:
        urls.extend(_load_urls(src))
    if not urls:
        print("没有可抓取的分享链接")
        return 2

    ok = failed = skipped = 0
    with sync_playwright() as p:
        # 优先用系统 Edge（免下载浏览器二进制）；无则回退 chromium
        try:
            browser = p.chromium.launch(headless=True, channel="msedge")
        except Exception:  # noqa: BLE001
            browser = p.chromium.launch(headless=True)
        for i, url in enumerate(urls, 1):
            slug = _slug(url)
            if (out_dir / f"{slug}.txt").exists() and not force:
                print(f"[{i}/{len(urls)}] 跳过（已抓取）: {slug}")
                skipped += 1
                continue
            print(f"[{i}/{len(urls)}] 抓取: {url}")
            try:
                text, images = _fetch_one(browser, url)
            except Exception as exc:  # noqa: BLE001
                print(f"    失败: {type(exc).__name__}: {exc}")
                failed += 1
                continue
            _save(url, text, images, out_dir)
            ok += 1
        browser.close()

    print(f"完成: 成功 {ok}, 失败 {failed}, 跳过 {skipped}, 输出目录 {out_dir}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    flags = {a for a in sys.argv[1:] if a.startswith("--")}
    positional = [a for a in sys.argv[1:] if not a.startswith("--")]
    if len(positional) < 2:
        print(__doc__)
        sys.exit(2)
    raise SystemExit(main(positional[:-1], Path(positional[-1]),
                          force="--force" in flags))
