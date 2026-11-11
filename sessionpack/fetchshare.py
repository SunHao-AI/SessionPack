"""单会话分享链接抓取（fetch-share 子命令的实现层）。

由 spike/trae_share_export.py 正式化而来，两点差异：
- playwright 在函数体内惰性导入，核心运行时零第三方依赖红线不破；
- 抓取时在浏览器内为每张图采集"所在消息文本片段"，写 per-slug 清单
  ``images/<slug>/manifest.json``，供 TraeExportAdapter 把图片挂到消息
  meta（正文来自 inner_text，无占位符，关联只能在此处采集）。

产物（输出目录可直接 ``sessionpack import --export-dir``）：
- ``<slug>.txt``：正文；
- ``images/<slug>/<slug>_{i}{ext}``：图片字节（签名 CDN 须浏览器上下文取）；
- ``images/<slug>/manifest.json``：``{"slug", "images": [{"file","url","snippet"}]}``。
"""

from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import urlparse

# 与 TraeExportAdapter 的图片扩展名口径一致
IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".webp", ".gif", ".img")

# 在页面内执行：每张 img 向上找 innerText 非空的最内层祖先作为消息容器，
# 取其 innerText 前 120 字符作为关联片段（解析端归一化后做包含匹配）。
_SNIPPETS_JS = """
() => Array.from(document.images).map(img => {
  let el = img.parentElement;
  while (el && el !== document.body) {
    const t = (el.innerText || '').trim();
    if (t.length > 0 && !el.matches('img')) break;
    el = el.parentElement;
  }
  const host = (el && el.innerText) ? el.innerText : (document.body ? document.body.innerText : '');
  return {src: img.currentSrc || img.src, snippet: (host || '').slice(0, 120)};
})
"""


def slug_of(url: str) -> str:
    return urlparse(url).path.rstrip("/").split("/")[-1] or "share"


def load_urls(arg: str) -> list[str]:
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
    return []


def _fetch_one(browser, url: str) -> tuple[str, list[dict]]:
    """打开一个分享页，滚到底，返回 (正文, [{"url","body","snippet"}, ...])。"""
    page = browser.new_page(viewport={"width": 1280, "height": 900})
    images: list[dict] = []

    def on_response(resp):
        # 签名 CDN URL 带 Referer 校验，必须在浏览器上下文里直接取 body
        if resp.request.resource_type == "image":
            try:
                images.append({"url": resp.url, "body": resp.body()})
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
        # 图片↔消息关联：在页面内取每张 img 所在消息的文本片段
        snippets: dict[str, str] = {}
        try:
            for item in page.evaluate(_SNIPPETS_JS):
                src = item.get("src") or ""
                if src.startswith("http") and src not in snippets:
                    snippets[src] = item.get("snippet") or ""
        except Exception:  # noqa: BLE001 关联失败退化为空片段（兜底挂最后一条）
            pass
        # 兜底：扫描 DOM 里的 img 标签（网络拦截漏掉的，用上下文请求取 body）
        seen = {img["url"] for img in images}
        for src in page.eval_on_selector_all("img", "els => els.map(e => e.src)"):
            if src and src.startswith("http") and src not in seen:
                try:
                    images.append({"url": src, "body": page.request.get(src).body()})
                    seen.add(src)
                except Exception:  # noqa: BLE001
                    pass
        for img in images:
            img["snippet"] = snippets.get(img["url"], "")
        return text, images
    finally:
        page.close()


def _save(url: str, text: str, images: list[dict], out_dir: Path) -> int:
    slug = slug_of(url)
    img_dir = out_dir / "images" / slug  # 子目录，避免污染 export 顶层
    saved = 0
    entries: list[dict] = []
    for i, img in enumerate(images):
        body = img.get("body") or b""
        if not body:
            continue
        ext = Path(urlparse(img["url"]).path).suffix or ".img"
        if ext.lower() not in IMAGE_SUFFIXES:
            ext = ".img"
        fname = f"{slug}_{i}{ext}"
        img_dir.mkdir(parents=True, exist_ok=True)
        (img_dir / fname).write_bytes(body)
        entries.append({"file": fname, "url": img["url"],
                        "snippet": img.get("snippet", "")})
        saved += 1
    txt_path = out_dir / f"{slug}.txt"
    txt_path.write_text(text, encoding="utf-8")
    manifest = img_dir / "manifest.json"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(
        json.dumps({"slug": slug, "images": entries}, ensure_ascii=False, indent=1),
        "utf-8")
    return saved


def fetch_shares(sources: list[str], out_dir: Path,
                 force: bool = False) -> tuple[int, int, int, int]:
    """抓取一批分享链接。返回 (成功, 失败, 跳过, 图片数)。

    ImportError 由调用方处理（提示安装 playwright）。
    """
    from playwright.sync_api import sync_playwright

    out_dir.mkdir(parents=True, exist_ok=True)
    urls: list[str] = []
    for src in sources:
        urls.extend(load_urls(src))
    if not urls:
        raise ValueError("没有可抓取的分享链接")

    ok = failed = skipped = pics = 0
    with sync_playwright() as p:
        # 优先用系统 Edge（免下载浏览器二进制）；无则回退 chromium
        try:
            browser = p.chromium.launch(headless=True, channel="msedge")
        except Exception:  # noqa: BLE001
            browser = p.chromium.launch(headless=True)
        for url in urls:
            slug = slug_of(url)
            if (out_dir / f"{slug}.txt").exists() and not force:
                skipped += 1
                continue
            try:
                text, images = _fetch_one(browser, url)
            except Exception:  # noqa: BLE001
                failed += 1
                continue
            pics += _save(url, text, images, out_dir)
            ok += 1
        browser.close()
    return ok, failed, skipped, pics
