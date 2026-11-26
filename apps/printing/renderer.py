import os
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

from playwright.sync_api import sync_playwright

from .worker import (
    ASSETS,
    CHROMIUM_EXECUTABLE,
    MAX_HTML_BYTES,
    MAX_PDF_BYTES,
    read_logo,
)


ORIGIN = "https://assets.erp.invalid"
DOCUMENT_URL = f"{ORIGIN}/document"
STYLES_URL = f"{ORIGIN}/print.css"
LOGO_URL = f"{ORIGIN}/logo.png"
FONTS = b"""
@font-face {
    font-family: "ERP Arabic";
    src: local("Noto Naskh Arabic"), local("NotoNaskhArabic-Regular");
    font-display: block;
}
:root {
    --bs-body-font-family: "ERP Arabic", "Noto Naskh Arabic", sans-serif;
}
body {
    font-family: var(--bs-body-font-family);
}
"""
CSP = (
    "default-src 'none'; script-src 'none'; "
    f"style-src 'unsafe-inline' {STYLES_URL}; img-src {LOGO_URL}; "
    "object-src 'none'; connect-src 'none'; frame-src 'none'; "
    "base-uri 'none'; form-action 'none'"
)


def _render(markup):
    styles = (ASSETS / "bootstrap.rtl.min.css").read_bytes() + FONTS
    logo = read_logo()
    document_loaded = False

    def fulfill_local_resource(route):
        nonlocal document_loaded
        request = route.request
        if request.method == "GET":
            if request.url == DOCUMENT_URL and request.resource_type == "document" and not document_loaded:
                document_loaded = True
                route.fulfill(
                    body=markup,
                    content_type="text/html; charset=utf-8",
                    headers={"Content-Security-Policy": CSP, "X-Content-Type-Options": "nosniff"},
                )
                return
            if request.url == STYLES_URL and request.resource_type == "stylesheet":
                route.fulfill(body=styles, content_type="text/css; charset=utf-8")
                return
            if logo is not None and request.url == LOGO_URL and request.resource_type == "image":
                route.fulfill(body=logo, content_type="image/png")
                return
        route.abort()

    with TemporaryDirectory(prefix="chromium-") as browser_directory, sync_playwright() as playwright:
        browser_files = Path(browser_directory)
        config = browser_files / "config"
        cache = browser_files / "cache"
        config.mkdir()
        cache.mkdir()
        browser = playwright.chromium.launch(
            executable_path=CHROMIUM_EXECUTABLE,
            headless=True,
            env=dict(os.environ, XDG_CONFIG_HOME=str(config), XDG_CACHE_HOME=str(cache)),
            args=["--disable-background-networking", "--disable-breakpad", "--disable-crash-reporter", "--disable-sync"],
        )
        try:
            context = browser.new_context(
                java_script_enabled=False, offline=True, service_workers="block", accept_downloads=False,
            )
            context.route("**/*", fulfill_local_resource)
            page = context.new_page()
            page.emulate_media(media="print")
            page.goto(DOCUMENT_URL, wait_until="load")
            assets_loaded = page.evaluate("""async (hasLogo) => {
                const faces = await document.fonts.load('12pt "ERP Arabic"', 'اللغة العربية');
                await document.fonts.ready;
                if (!faces.length || !hasLogo) return faces.length > 0;

                const slot = document.getElementById('print-logo');
                const logo = slot?.content.querySelector('img[src="https://assets.erp.invalid/logo.png"]');
                if (!logo) return false;
                const image = logo.cloneNode(true);
                slot.replaceWith(image);
                await image.decode();
                return image.naturalWidth > 0;
            }""", logo is not None)
            if not assets_loaded:
                raise RuntimeError("Printing assets did not load")
            return page.pdf(format="A4", print_background=True, prefer_css_page_size=True)
        finally:
            browser.close()


def main():
    markup = sys.stdin.buffer.read(MAX_HTML_BYTES + 1)
    if not 0 < len(markup) <= MAX_HTML_BYTES:
        return 1
    markup.decode("utf-8")
    pdf = _render(markup)
    if len(pdf) > MAX_PDF_BYTES:
        return 1
    sys.stdout.buffer.write(pdf)
    return 0


if __name__ == "__main__":
    sys.exit(main())
