"""Screenshots of the TUI and web UI for interface reviews.

    uv run python tools/screenshots.py [--tui] [--web] [--out screenshots]

Uses sample profiles in a temporary TERMDECK_HOME; never touches real config
or the network. TUI: SVG from Textual, PNG via cairosvg. Web: needs Playwright
with a browser (`uv run playwright install chromium`).
"""

import argparse
import asyncio
import os
import sys
import tempfile
from pathlib import Path

os.environ["TERMDECK_HOME"] = tempfile.mkdtemp(prefix="termdeck-shots-")

from termdeck.profiles import Profile, ProfileStore, SerialSettings, SSHSettings  # noqa: E402


def sample_store() -> ProfileStore:
    store = ProfileStore(Path(os.environ["TERMDECK_HOME"]) / "profiles.json")
    store.save(Profile("build-server", "ssh", group="Work", ssh=SSHSettings(host="build.example.com", username="dev")))
    store.save(Profile("router", "ssh", group="Home", ssh=SSHSettings(host="192.168.1.1", username="admin")))
    store.save(Profile("dev-board", "serial", group="Lab", serial=SerialSettings(device="loop://")))
    store.save(Profile("modem", "serial", group="Lab", serial=SerialSettings(device="loop://", baudrate=9600)))
    return store


def to_png(svg: Path) -> None:
    try:
        import cairosvg
    except ImportError:
        return
    # cairosvg can't load Textual's web font; use a local monospace one so columns line up.
    text = svg.read_text(encoding="utf-8").replace('"Fira Code"', '"DejaVu Sans Mono"').replace(
        "Fira Code, monospace", '"DejaVu Sans Mono", monospace')
    cairosvg.svg2png(bytestring=text.encode(), write_to=str(svg.with_suffix(".png")), output_width=1600)


async def tui_shots(out: Path) -> list[Path]:
    from textual.widgets import Select, Switch

    from termdeck.tui.app import InputDialog, TermdeckTUI

    shots = []

    def save(app, name):
        path = out / f"tui-{name}.svg"
        app.save_screenshot(filename=path.name, path=str(out))
        to_png(path)
        shots.append(path)

    for size in [(120, 40), (80, 24)]:
        tag = f"{size[0]}x{size[1]}"
        app = TermdeckTUI(store=sample_store())
        async with app.run_test(size=size) as pilot:
            await pilot.pause(0.3)
            save(app, f"{tag}-main")
            await pilot.press("n")
            await pilot.pause(0.3)
            save(app, f"{tag}-new-ssh")
            app.screen.query_one("#profile--kind", Select).value = "serial"
            app.screen.query_one("#advanced", Switch).value = True
            await pilot.pause(0.3)
            save(app, f"{tag}-new-serial-advanced")
            await pilot.press("escape")
            await pilot.pause(0.2)
            await pilot.press("enter")  # Discard changes? (the type was changed)
            await pilot.pause(0.2)
            app.push_screen(InputDialog("Password", "dev@build.example.com's password:", secret=True))
            await pilot.pause(0.2)
            save(app, f"{tag}-password-dialog")
            await pilot.press("escape")
            app.open_tab(app.store.get("dev-board"), saved=True)
            await pilot.pause(0.5)
            await pilot.press(*"AT+VERSION")
            await pilot.pause(0.3)
            save(app, f"{tag}-serial-session")
    return shots


async def web_shots(out: Path) -> list[Path]:
    from aiohttp import web
    from playwright.async_api import async_playwright

    from termdeck.web.server import auth_key, create_app

    runner = web.AppRunner(create_app(sample_store()))
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    shots = []
    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch()
            for scheme in ["light", "dark"]:
                for w, h, scale in [(1280, 800, 1), (900, 600, 1), (1280, 800, 1.5), (1280, 800, 2)]:
                    tag = f"{scheme}-{w}x{h}@{scale}x"
                    # A fresh context per run: no saved UI state carries over.
                    context = await browser.new_context(viewport={"width": w, "height": h},
                                                        device_scale_factor=scale, color_scheme=scheme)
                    page = await context.new_page()
                    # Start tokens are single use: one per page.
                    token = runner.app[auth_key].new_link()
                    await page.goto(f"http://127.0.0.1:{port}/#token={token}")
                    await page.wait_for_selector(".profile")

                    async def save(name):
                        path = out / f"web-{tag}-{name}.png"
                        await page.screenshot(path=str(path), animations="disabled")
                        shots.append(path)

                    await save("main")
                    await page.click("#new-profile")
                    await page.wait_for_selector("#editor[open]")
                    await save("new-ssh")
                    await page.select_option('#editor-body select[id^="f-kind-"]', '"serial"')
                    if not await page.is_checked("#advanced-toggle"):
                        await page.click("label:has(#advanced-toggle)")
                    await save("new-serial-advanced")
                    await page.keyboard.press("Escape")
                    await page.click("#prompt-ok")  # Discard changes? (the type was changed)
                    await page.dblclick(".profile:has-text('dev-board')")
                    await page.wait_for_timeout(800)
                    await page.keyboard.type("AT+VERSION")
                    await page.wait_for_timeout(300)
                    await save("serial-session")
                    await context.close()
            await browser.close()
    finally:
        await runner.cleanup()
    return shots


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--tui", action="store_true")
    parser.add_argument("--web", action="store_true")
    parser.add_argument("--out", type=Path, default=Path("screenshots"))
    args = parser.parse_args()
    if not (args.tui or args.web):
        args.tui = args.web = True
    args.out.mkdir(parents=True, exist_ok=True)
    shots = []
    if args.tui:
        shots += asyncio.run(tui_shots(args.out))
    if args.web:
        try:
            shots += asyncio.run(web_shots(args.out))
        except Exception as exc:  # no browser installed, etc.
            print(f"web screenshots skipped: {str(exc).strip().splitlines()[0]}", file=sys.stderr)
    for path in shots:
        print(path.with_suffix(".png") if path.with_suffix(".png").exists() else path)


if __name__ == "__main__":
    main()
