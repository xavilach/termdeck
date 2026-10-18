"""Browser tests of the web UI dialogs. Skipped when Playwright or Chromium is unavailable."""

import os
from pathlib import Path

import pytest

pytest.importorskip("playwright.async_api")
from playwright.async_api import async_playwright  # noqa: E402

from termdeck.profiles import ProfileStore  # noqa: E402
from termdeck.web.server import start  # noqa: E402


# The autouse fixture points HOME at a temp dir; keep finding the installed browser.
BROWSERS = os.environ.get("PLAYWRIGHT_BROWSERS_PATH") or str(Path.home() / ".cache" / "ms-playwright")


@pytest.fixture
async def page(tmp_path, monkeypatch):
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", BROWSERS)
    store = ProfileStore(tmp_path / "profiles.json")
    async with async_playwright() as pw:
        try:
            browser = await pw.chromium.launch()
        except Exception as exc:  # browser not installed, no sandbox, ...
            pytest.skip(f"chromium cannot be launched: {str(exc)[:80]}")
        runner, url = await start(store=store)
        try:
            page = await browser.new_page()
            page.store = store
            await page.goto(url)
            await page.wait_for_function("typeof prompt === 'function' && document.querySelector('#new-profile')")
            await page.wait_for_function("document.querySelector('#new-profile') && !document.querySelector('#editor[open]')")
            yield page
        finally:
            await browser.close()
            await runner.cleanup()


async def test_prompt_input_enter_resolves_with_typed_value(page):
    await page.evaluate("void (window.answer = prompt({title: 'Name', body: document.createElement('p'), input: {}}))")
    await page.wait_for_selector("#prompt[open] input")
    assert await page.evaluate("document.activeElement.tagName") == "INPUT"
    await page.keyboard.type("héllo 世界")
    await page.keyboard.press("Enter")
    assert await page.evaluate("window.answer") == "héllo 世界"
    assert await page.query_selector("#prompt[open]") is None


async def test_danger_prompt_focuses_cancel_and_enter_declines(page):
    await page.evaluate("void (window.answer = prompt({title: 'Trust?', body: document.createElement('p'), okLabel: 'Trust', danger: true, armDelay: true}))")
    await page.wait_for_selector("#prompt[open]")
    assert await page.evaluate("document.activeElement.id") == "prompt-cancel"
    assert await page.is_disabled("#prompt-ok")
    await page.keyboard.press("Enter")
    assert await page.evaluate("window.answer") is False
    assert await page.query_selector("#prompt[open]") is None


async def test_editor_enter_saves_and_cancel_does_not(page):
    await page.click("#new-profile")
    await page.wait_for_selector("#editor[open]")
    await page.fill('#editor-body input[id^="f-name-"]', "cancelled")
    await page.click('#editor button[data-close="cancel"]')
    await page.wait_for_selector("#editor[open]", state="detached")
    assert page.store.load() == {}

    await page.click("#new-profile")
    await page.wait_for_selector("#editor[open]")
    await page.fill('#editor-body input[id^="f-name-"]', "box")
    await page.fill('#editor-body input[id^="f-host-"]', "box.example")
    await page.press('#editor-body input[id^="f-host-"]', "Enter")
    await page.wait_for_selector("#editor[open]", state="detached")
    assert page.store.get("box").ssh.host == "box.example"


async def _new_ssh_profile(page, name):
    await page.click("#new-profile")
    await page.wait_for_selector("#editor[open]")
    await page.fill('#editor-body input[id^="f-name-"]', name)
    await page.fill('#editor-body input[id^="f-host-"]', f"{name}.example")


async def test_half_filled_forward_is_reported_and_blocks_the_save(page):
    await _new_ssh_profile(page, "fwd")
    await page.click("text=Add forward")
    await page.fill('.forward-row input[placeholder="destination host"]', "db.internal")
    await page.click("#editor-save")
    await page.wait_for_selector("#editor-error:not([hidden])")
    assert await page.text_content("#editor-error") == "Port forward 1: enter the listen port"
    assert await page.query_selector("#editor[open]") is not None
    assert page.store.load() == {}


async def test_untouched_forward_row_is_dropped_silently(page):
    await _new_ssh_profile(page, "plain")
    await page.click("text=Add forward")
    await page.click("#editor-save")
    await page.wait_for_selector("#editor[open]", state="detached")
    assert page.store.get("plain").ssh.forwards == []


async def test_quick_connect_box_opens_a_tab(page):
    await page.fill("#quick-input", "bob@host.example")
    await page.press("#quick-input", "Enter")
    await page.wait_for_selector(".tab")
    assert "bob@host.example" in await page.text_content(".tab")


async def _save_and_get(page, name):
    await page.click("#editor-save")
    await page.wait_for_selector("#editor[open]", state="detached")
    return page.store.get(name).ssh.forwards


async def test_forward_with_listen_port_zero_is_saved(page):
    await _new_ssh_profile(page, "zero")
    await page.click("text=Add forward")
    await page.fill('.forward-row input[placeholder="port"] >> nth=0', "0")
    await page.fill('.forward-row input[placeholder="destination host"]', "db.internal")
    await page.fill('.forward-row input[placeholder="port"] >> nth=1', "5432")
    (fwd,) = await _save_and_get(page, "zero")
    assert (fwd.kind, fwd.listen_port, fwd.dest_host, fwd.dest_port) == ("local", 0, "db.internal", 5432)


async def test_clearing_the_listen_port_is_reported(page):
    await _new_ssh_profile(page, "cleared")
    await page.click("text=Add forward")
    listen = page.locator('.forward-row input[placeholder="port"]').nth(0)
    await listen.fill("8080")
    await page.fill('.forward-row input[placeholder="destination host"]', "db.internal")
    await page.fill('.forward-row input[placeholder="port"] >> nth=1', "5432")
    await listen.fill("")
    await page.click("#editor-save")
    await page.wait_for_selector("#editor-error:not([hidden])")
    assert await page.text_content("#editor-error") == "Port forward 1: enter the listen port"
    assert page.store.load() == {}


async def test_socks_forward_needs_only_the_listen_port(page):
    await _new_ssh_profile(page, "socks")
    await page.click("text=Add forward")
    await page.select_option(".forward-row select", "dynamic")
    await page.fill('.forward-row input[placeholder="port"] >> nth=0', "1080")
    (fwd,) = await _save_and_get(page, "socks")
    assert (fwd.kind, fwd.listen_port, fwd.dest_host) == ("dynamic", 1080, "")


async def test_empty_quick_connect_does_nothing(page):
    await page.focus("#quick-input")
    await page.press("#quick-input", "Enter")
    await page.fill("#quick-input", "   ")
    await page.press("#quick-input", "Enter")
    # The request (if any) is async; a round trip through the page lets it settle.
    await page.evaluate("new Promise((r) => setTimeout(r, 0))")
    assert await page.query_selector(".tab") is None
    assert await page.is_hidden("#toast")


@pytest.fixture
async def page_with_file(tmp_path, monkeypatch):
    """Like `page`, but the profile file is prepared by the test before the page loads."""
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", BROWSERS)
    path = tmp_path / "profiles.json"
    store = ProfileStore(path)
    async with async_playwright() as pw:
        try:
            browser = await pw.chromium.launch()
        except Exception as exc:
            pytest.skip(f"chromium cannot be launched: {str(exc)[:80]}")
        runner, url = await start(store=store)
        try:
            page = await browser.new_page()
            page.store = store

            async def open_page():
                await page.goto(url)
                await page.wait_for_function("typeof prompt === 'function' && document.querySelector('#new-profile')")

            page.open_page = open_page
            yield page
        finally:
            await browser.close()
            await runner.cleanup()


async def test_corrupt_profile_file_is_shown_and_quick_connect_works(page_with_file):
    page = page_with_file
    page.store.path.write_text("{oops")
    await page.open_page()
    await page.wait_for_selector("#profile-problems:not([hidden])")
    assert "Profiles not loaded" in await page.text_content("#profile-problems")
    await page.fill("#quick-input", "bob@host.example")
    await page.press("#quick-input", "Enter")
    await page.wait_for_selector(".tab")
    assert "bob@host.example" in await page.text_content(".tab")
    assert page.store.path.read_text() == "{oops"


async def test_invalid_entry_is_counted_and_saving_shows_no_error(page_with_file):
    import json

    page = page_with_file
    page.store.path.write_text(json.dumps({"profiles": [
        {"name": "bad", "kind": "serial", "serial": {"device": "d", "parity": "Q"}}]}))
    await page.open_page()
    await page.wait_for_selector("#profile-problems:not([hidden])")
    assert "1 profile skipped" in await page.text_content("#profile-problems")
    await _new_ssh_profile(page, "fine")
    await page.click("#editor-save")
    await page.wait_for_selector("#editor[open]", state="detached")
    assert page.store.get("fine").ssh.host == "fine.example"
    assert await page.is_hidden("#toast")
    assert "1 profile skipped" in await page.text_content("#profile-problems")
    assert [e["name"] for e in json.loads(page.store.path.read_text())["profiles"]] == ["fine", "bad"]


async def test_uploading_an_existing_file_asks_and_cancel_leaves_it_unchanged(page, ssh_server):
    from termdeck.ssh import remember_host_key

    remember_host_key("127.0.0.1", ssh_server.port, ssh_server.key)
    (ssh_server.root / "keep.txt").write_text("original")
    await page.fill("#quick-input", f"alice@127.0.0.1:{ssh_server.port}")
    await page.press("#quick-input", "Enter")
    await page.wait_for_selector("#prompt[open] input")  # the password question
    await page.keyboard.type("secret")
    await page.keyboard.press("Enter")
    await page.wait_for_function("() => !document.querySelector('[data-action=sftp]').disabled")
    await page.click("[data-action=sftp]")
    await page.wait_for_function("() => document.querySelector('#sftp-status').textContent.includes('items')")

    upload = "void (window.uploaded = sftpUpload([new File(['replacement'], 'keep.txt')]))"
    await page.evaluate(upload)
    await page.wait_for_selector("#prompt[open]")
    assert "Replace files?" in await page.text_content("#prompt")
    assert "keep.txt" in await page.text_content("#prompt")
    await page.click("#prompt-cancel")
    await page.evaluate("() => window.uploaded")  # the upload function returned: nothing was sent
    assert (ssh_server.root / "keep.txt").read_text() == "original"

    await page.evaluate(upload)  # and Replace does replace
    await page.wait_for_selector("#prompt[open]")
    await page.click("#prompt-ok")
    await page.evaluate("() => window.uploaded")
    await page.wait_for_function("() => !document.querySelector('#toast').hidden")
    assert (ssh_server.root / "keep.txt").read_text() == "replacement"


async def test_sftp_status_keeps_listing_and_upload_progress_apart(page, ssh_server):
    from termdeck.ssh import remember_host_key

    remember_host_key("127.0.0.1", ssh_server.port, ssh_server.key)
    (ssh_server.root / "one.txt").write_text("1")
    await page.fill("#quick-input", f"alice@127.0.0.1:{ssh_server.port}")
    await page.press("#quick-input", "Enter")
    await page.wait_for_selector("#prompt[open] input")
    await page.keyboard.type("secret")
    await page.keyboard.press("Enter")
    await page.wait_for_function("() => !document.querySelector('[data-action=sftp]').disabled")
    await page.click("[data-action=sftp]")
    await page.wait_for_function("() => document.querySelector('#sftp-info').textContent === '1 items'")
    assert await page.text_content("#sftp-transfer") == ""

    # Hold the upload request back, so the transfer stays in progress while the page is inspected.
    await page.evaluate("""() => {
        const send = XMLHttpRequest.prototype.send;
        XMLHttpRequest.prototype.send = function (body) { window.release = () => send.call(this, body); };
    }""")
    await page.evaluate("void (window.first = sftpUpload([new File(['data'], 'new.txt')]))")
    await page.wait_for_function("() => typeof window.release === 'function'")
    assert "Uploading 1 file" in await page.text_content("#sftp-transfer")
    assert await page.is_visible("#sftp-transfer button")
    assert await page.text_content("#sftp-info") == "1 items"  # the listing text is not wiped

    await page.evaluate("() => refreshSftp()")  # a listing refresh must not wipe the transfer either
    await page.wait_for_function("() => document.querySelector('#sftp-info').textContent === '1 items'")
    assert "Uploading 1 file" in await page.text_content("#sftp-transfer")

    await page.evaluate("void sftpUpload([new File(['more'], 'other.txt')])")  # a second drop
    await page.wait_for_function("() => !document.querySelector('#toast').hidden")
    assert "already running" in await page.text_content("#toast")
    assert not (ssh_server.root / "other.txt").exists()

    await page.evaluate("() => window.release()")
    await page.evaluate("() => window.first")
    await page.wait_for_function("() => document.querySelector('#sftp-transfer').textContent === ''")
    await page.wait_for_function("() => document.querySelector('#sftp-info').textContent === '2 items'")
    assert (ssh_server.root / "new.txt").read_text() == "data"


async def test_proxy_host_field_appears_with_a_proxy_and_advanced_groups_have_subheadings(page):
    await page.click("#new-profile")
    await page.wait_for_selector("#editor[open]")
    host = 'label.field:has(input[id^="f-proxy_host-"])'
    assert await page.query_selector(host) is not None
    await page.click(".slider")
    assert not await page.is_visible(host)  # Proxy is None: no host to ask for
    await page.select_option('#editor-body select[id^="f-proxy_type-"]', label="SOCKS 5")
    assert await page.is_visible(host)
    await page.select_option('#editor-body select[id^="f-proxy_type-"]', label="None")
    assert not await page.is_visible(host)

    section = page.locator("#editor-body .section", has=page.locator("h3", has_text="SSH (advanced)"))
    headings = await section.locator("h4.subsection").all_text_contents()
    assert {"Session", "Proxy", "Authentication"} <= set(headings)
    for heading in await section.locator("h4.subsection").all():
        assert await heading.is_visible()


async def test_page_takes_its_themes_from_the_server(page):
    assert await page.evaluate("typeof THEMES") == "undefined"  # no copy of the themes in the page
    assert await page.evaluate("Object.keys(state.themes).includes('putty')")
    assert await page.evaluate("state.themes.dark.background") == "#0f1218"


async def test_tab_uses_the_profiles_palette(page):
    from termdeck.profiles import Profile, SerialSettings

    profile = Profile("coloured", "serial", serial=SerialSettings(device="loop://"))
    profile.terminal.colours = "background=#112233"
    page.store.save(profile)
    await page.fill("#quick-input", "coloured")
    await page.press("#quick-input", "Enter")
    await page.wait_for_selector(".tab")
    background = await page.evaluate("getComputedStyle(document.querySelector('.term')).backgroundColor")
    assert background == "rgb(17, 34, 51)"


async def test_quick_connect_tab_uses_the_default_themes_background(page):
    await page.fill("#quick-input", "bob@host.example")
    await page.press("#quick-input", "Enter")
    await page.wait_for_selector(".tab")
    assert await page.evaluate("getComputedStyle(document.querySelector('.term')).backgroundColor") == "rgb(15, 18, 24)"
