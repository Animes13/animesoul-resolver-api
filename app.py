# -*- coding: utf-8 -*-

import os
import json
import re
import time
import hashlib
import threading
from pathlib import Path
from urllib.parse import unquote

from fastapi import FastAPI, Query
from fastapi.responses import JSONResponse

from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.action_chains import ActionChains


app = FastAPI(title="AnimeSoul Blogger Resolver API")


# ============================================================
# CONFIG
# ============================================================

CHROMIUM = os.environ.get("CHROMIUM_PATH", "/usr/bin/chromium")
CHROMEDRIVER = os.environ.get("CHROMEDRIVER_PATH", "/usr/bin/chromedriver")

OUT_DIR = Path(os.environ.get("OUT_DIR", "/tmp/blogger-test"))
OUT_DIR.mkdir(parents=True, exist_ok=True)

CACHE_TTL = int(os.environ.get("CACHE_TTL", "900"))  # 15 minutos
WAIT_SECONDS = int(os.environ.get("WAIT_SECONDS", "45"))

USER_AGENT = (
    "Mozilla/5.0 (Linux; Android 10) "
    "AppleWebKit/537.36 Chrome/124 Mobile Safari/537.36"
)

cache = {}
cache_lock = threading.Lock()


# ============================================================
# HELPERS
# ============================================================

def clean_url(url):
    if not url:
        return ""

    url = unquote(str(url))
    url = url.replace("\\/", "/")
    url = url.replace("&amp;", "&")
    url = url.strip()

    return url


def is_valid_blogger_url(url):
    if not url:
        return False

    u = url.lower()

    return (
        u.startswith("https://www.blogger.com/video.g?token=")
        or u.startswith("http://www.blogger.com/video.g?token=")
        or u.startswith("https://blogger.com/video.g?token=")
        or u.startswith("http://blogger.com/video.g?token=")
    )


def is_media_url(url):
    if not url:
        return False

    u = url.lower()

    return (
        ".mp4" in u
        or ".m3u8" in u
        or ".webm" in u
        or ".mkv" in u
        or "googlevideo.com/videoplayback" in u
        or "videoplayback" in u
        or "mime=video" in u
        or "mime%3dvideo" in u
        or "mime=audio" in u
        or "mime%3daudio" in u
    )


def url_interessante(url, mime=""):
    u = (url or "").lower()
    m = (mime or "").lower()

    termos_midia = [
        ".mp4",
        ".m3u8",
        ".webm",
        ".mkv",
        "googlevideo",
        "videoplayback",
        "mime=video",
        "mime%3dvideo",
        "mime=audio",
        "mime%3daudio",
        "range=",
    ]

    termos_importantes = [
        "youtube.googleapis.com",
        "youtube.com",
        "ytimg.com",
        "iframe_api",
        "www-widgetapi",
        "embed",
        "get_video_info",
        "player",
        "blogger",
        "vi_blogger",
        "browserinfo",
        "streamingdata",
        "playback",
    ]

    mime_midia = (
        "video" in m
        or "audio" in m
        or "mpegurl" in m
        or "mp2t" in m
        or "octet-stream" in m
    )

    if any(t in u for t in termos_midia) or mime_midia:
        return "midia"

    if any(t in u for t in termos_importantes):
        return "importante"

    return None


def escolher_melhor_url(urls):
    urls = [clean_url(u) for u in urls if u]

    if not urls:
        return None

    # Melhor: googlevideo itag 18 geralmente vem com áudio + vídeo juntos
    for url in urls:
        ul = url.lower()
        if "googlevideo.com/videoplayback" in ul and "itag=18" in ul:
            return url

    # Depois googlevideo mp4
    for url in urls:
        ul = url.lower()
        if "googlevideo.com/videoplayback" in ul and (
            "mime=video/mp4" in ul or "mime%3dvideo%2fmp4" in ul
        ):
            return url

    # Depois qualquer googlevideo/videoplayback
    for url in urls:
        ul = url.lower()
        if "googlevideo.com/videoplayback" in ul or "videoplayback" in ul:
            return url

    # Depois mp4
    for url in urls:
        if ".mp4" in url.lower():
            return url

    # Depois m3u8
    for url in urls:
        if ".m3u8" in url.lower():
            return url

    return urls[0]


def cache_get(url):
    now = time.time()

    with cache_lock:
        item = cache.get(url)

        if not item:
            return None

        if now - item["time"] > CACHE_TTL:
            cache.pop(url, None)
            return None

        return item["result"]


def cache_set(url, result):
    with cache_lock:
        cache[url] = {
            "time": time.time(),
            "result": result,
        }


# ============================================================
# SELENIUM
# ============================================================

def criar_driver():
    options = Options()
    options.binary_location = CHROMIUM

    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--disable-software-rasterizer")
    options.add_argument("--disable-extensions")
    options.add_argument("--disable-background-networking")
    options.add_argument("--disable-sync")
    options.add_argument("--disable-default-apps")
    options.add_argument("--disable-popup-blocking")
    options.add_argument("--autoplay-policy=no-user-gesture-required")
    options.add_argument("--window-size=1280,720")
    options.add_argument("--user-agent=%s" % USER_AGENT)

    options.set_capability("goog:loggingPrefs", {
        "performance": "ALL",
        "browser": "ALL",
    })

    service = Service(CHROMEDRIVER)

    return webdriver.Chrome(service=service, options=options)


class BloggerResolver:
    def __init__(self, url):
        self.url = clean_url(url)
        self.achados_midia = set()
        self.achados_importantes = set()
        self.todos_logs = []

        key = hashlib.md5(self.url.encode("utf-8")).hexdigest()
        self.out_dir = OUT_DIR / key
        self.out_dir.mkdir(parents=True, exist_ok=True)

    def salvar_estado(self, driver, nome):
        try:
            html_path = self.out_dir / f"{nome}.html"
            png_path = self.out_dir / f"{nome}.png"

            html = driver.page_source
            html_path.write_text(html, encoding="utf-8", errors="ignore")
            driver.save_screenshot(str(png_path))

            ids = re.findall(r"vi_blogger/([^/]+)/", html)
            for vid in sorted(set(ids)):
                self.achados_importantes.add("vi_blogger:" + vid)

        except Exception:
            pass

    def analisar_logs(self, driver):
        try:
            logs = driver.get_log("performance")
        except Exception:
            return

        for entry in logs:
            try:
                msg = json.loads(entry["message"])["message"]
                method = msg.get("method", "")
                params = msg.get("params", {})

                url = ""
                mime = ""
                status = ""

                if method == "Network.requestWillBeSent":
                    req = params.get("request", {})
                    url = req.get("url", "")

                elif method == "Network.responseReceived":
                    resp = params.get("response", {})
                    url = resp.get("url", "")
                    mime = resp.get("mimeType", "")
                    status = resp.get("status", "")

                if not url:
                    continue

                url = clean_url(url)

                self.todos_logs.append({
                    "method": method,
                    "status": status,
                    "mime": mime,
                    "url": url,
                })

                tipo = url_interessante(url, mime)

                if tipo == "midia":
                    self.achados_midia.add(url)

                elif tipo == "importante":
                    self.achados_importantes.add(url)

            except Exception:
                pass

    def clicar_player(self, driver):
        try:
            driver.get_log("performance")
        except Exception:
            pass

        # Clique JS no botão play
        try:
            play = driver.find_element("css selector", ".ppVepb")
            driver.execute_script("arguments[0].click();", play)
        except Exception:
            pass

        time.sleep(3)
        self.analisar_logs(driver)

        # Clique real no botão play
        try:
            play = driver.find_element("css selector", ".ppVepb")
            ActionChains(driver).move_to_element(play).click().perform()
        except Exception:
            pass

        time.sleep(5)
        self.analisar_logs(driver)

        # Clique no main
        try:
            main = driver.find_element("css selector", "main.iLXc1d")
            ActionChains(driver).move_to_element(main).click().perform()
        except Exception:
            pass

        time.sleep(5)
        self.analisar_logs(driver)

        # Clique no centro da tela
        try:
            driver.execute_script("""
                const x = window.innerWidth / 2;
                const y = window.innerHeight / 2;
                const el = document.elementFromPoint(x, y);
                if (el) {
                    el.dispatchEvent(new MouseEvent('mousedown', {bubbles:true, clientX:x, clientY:y}));
                    el.dispatchEvent(new MouseEvent('mouseup', {bubbles:true, clientX:x, clientY:y}));
                    el.dispatchEvent(new MouseEvent('click', {bubbles:true, clientX:x, clientY:y}));
                }
            """)
        except Exception:
            pass

        time.sleep(8)
        self.analisar_logs(driver)

    def tentar_video_js(self, driver):
        try:
            driver.execute_script("""
                const videos = document.querySelectorAll('video');
                for (const v of videos) {
                    v.muted = true;
                    v.play().catch(()=>{});
                }
            """)
        except Exception:
            pass

    def salvar_iframes(self, driver):
        try:
            driver.switch_to.default_content()
        except Exception:
            return

        try:
            iframes = driver.find_elements("tag name", "iframe")
        except Exception:
            return

        for i, iframe in enumerate(iframes):
            try:
                src = iframe.get_attribute("src") or ""

                if src:
                    self.achados_importantes.add(clean_url(src))

                driver.switch_to.frame(iframe)

                html = driver.page_source

                try:
                    iframe_html_path = self.out_dir / f"iframe_{i}.html"
                    iframe_png_path = self.out_dir / f"iframe_{i}.png"

                    iframe_html_path.write_text(html, encoding="utf-8", errors="ignore")
                    driver.save_screenshot(str(iframe_png_path))
                except Exception:
                    pass

                # Vídeos dentro do iframe
                try:
                    videos = driver.find_elements("tag name", "video")

                    for v in videos:
                        src_attr = v.get_attribute("src") or ""
                        current_src = driver.execute_script(
                            "return arguments[0].currentSrc || '';",
                            v
                        )

                        if src_attr and is_media_url(src_attr):
                            self.achados_midia.add(clean_url(src_attr))

                        if current_src and is_media_url(current_src):
                            self.achados_midia.add(clean_url(current_src))

                except Exception:
                    pass

                # URLs dentro do HTML do iframe
                urls = re.findall(r'https?://[^"\'<>\\]+', html)

                for u in urls:
                    ul = u.lower()

                    if (
                        ".mp4" in ul
                        or ".m3u8" in ul
                        or "googlevideo" in ul
                        or "videoplayback" in ul
                        or "youtube" in ul
                        or "ytimg" in ul
                        or "blogger" in ul
                    ):
                        self.achados_importantes.add(clean_url(u))

                        if is_media_url(u):
                            self.achados_midia.add(clean_url(u))

                driver.switch_to.default_content()

            except Exception:
                try:
                    driver.switch_to.default_content()
                except Exception:
                    pass

    def salvar_debug(self):
        try:
            logs_path = self.out_dir / "network_logs.json"
            logs_path.write_text(
                json.dumps(self.todos_logs, indent=2, ensure_ascii=False),
                encoding="utf-8"
            )

            importantes_path = self.out_dir / "urls_importantes.txt"
            importantes_path.write_text(
                "\n".join(sorted(self.achados_importantes)),
                encoding="utf-8"
            )

            midia_path = self.out_dir / "urls_midia.txt"
            midia_path.write_text(
                "\n".join(sorted(self.achados_midia)),
                encoding="utf-8"
            )

        except Exception:
            pass

    def resolve(self):
        driver = None

        try:
            driver = criar_driver()
            driver.set_page_load_timeout(60)

            driver.get(self.url)

            time.sleep(8)

            self.salvar_estado(driver, "blogger_antes")
            self.analisar_logs(driver)

            best = escolher_melhor_url(self.achados_midia)
            if best:
                self.salvar_debug()
                return best

            self.clicar_player(driver)

            self.salvar_estado(driver, "blogger_depois")
            self.tentar_video_js(driver)
            self.salvar_iframes(driver)
            self.analisar_logs(driver)

            best = escolher_melhor_url(self.achados_midia)
            if best:
                self.salvar_debug()
                return best

            # Observa rede por alguns segundos
            for i in range(WAIT_SECONDS):
                self.analisar_logs(driver)

                if i in (10, 20, 30, 40):
                    self.tentar_video_js(driver)
                    self.salvar_iframes(driver)

                best = escolher_melhor_url(self.achados_midia)
                if best:
                    self.salvar_debug()
                    return best

                time.sleep(1)

            self.salvar_debug()

            return escolher_melhor_url(self.achados_midia)

        finally:
            if driver:
                try:
                    driver.quit()
                except Exception:
                    pass


# ============================================================
# ROTAS DA API
# ============================================================

@app.get("/")
def home():
    return {
        "ok": True,
        "name": "AnimeSoul Blogger Resolver API",
        "endpoints": {
            "health": "/health",
            "resolve": "/resolve?url=https://www.blogger.com/video.g?token=TOKEN"
        }
    }


@app.get("/health")
def health():
    return {
        "ok": True,
        "chromium": CHROMIUM,
        "chromedriver": CHROMEDRIVER,
        "cache_items": len(cache),
    }


@app.get("/resolve")
def resolve_endpoint(url: str = Query(..., description="Blogger video.g token URL")):
    url = clean_url(url)

    if not is_valid_blogger_url(url):
        return JSONResponse(
            status_code=400,
            content={
                "ok": False,
                "error": "URL inválida. Envie uma URL do Blogger video.g?token=...",
                "url": url,
            }
        )

    cached = cache_get(url)
    if cached:
        return {
            "ok": True,
            "cached": True,
            "url": cached,
        }

    try:
        resolver = BloggerResolver(url)
        final_url = resolver.resolve()

        if final_url:
            cache_set(url, final_url)

            return {
                "ok": True,
                "cached": False,
                "url": final_url,
            }

        return JSONResponse(
            status_code=404,
            content={
                "ok": False,
                "error": "Nenhuma URL de mídia encontrada",
            }
        )

    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={
                "ok": False,
                "error": str(e),
            }
        )