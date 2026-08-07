"""Browser login + auto-claim for Epic, Steam, GOG."""

from __future__ import annotations

import logging
import os
import platform
import re
import socket
import subprocess
import time
from collections.abc import Callable
from pathlib import Path

from hunter import BROWSER_CHANNEL, BROWSER_HEADLESS, EPIC_CDP, EPIC_CDP_PORT, Game, LOGIN_TIMEOUT, PROFILE

log = logging.getLogger("drop_alert.claim")

STEALTH = "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});window.chrome={runtime:{}};"


def stores_for_games(games: list[Game]) -> list[str]:
    """Map games to store keys — only opens browsers for platforms being claimed."""
    out: set[str] = set()
    for g in games:
        if not g.claimable:
            continue
        if g.platform == "Steam":
            out.add("steam")
        elif g.platform == "Epic Games":
            out.add("epic")
        elif g.platform == "GOG":
            out.add("gog")
    return sorted(out)


def _launch(pw, profile: Path, *, headless: bool | None = None):
    profile.mkdir(parents=True, exist_ok=True)
    use_headless = BROWSER_HEADLESS if headless is None else headless
    kw = dict(
        user_data_dir=str(profile),
        headless=use_headless,
        viewport={"width": 1366, "height": 768},
        args=["--disable-blink-features=AutomationControlled", "--no-first-run"],
        ignore_default_args=["--enable-automation"],
        locale="en-US",
    )
    if BROWSER_CHANNEL:
        kw["channel"] = BROWSER_CHANNEL
    try:
        ctx = pw.chromium.launch_persistent_context(**kw)
    except Exception:
        kw.pop("channel", None)
        ctx = pw.chromium.launch_persistent_context(**kw)
    ctx.add_init_script(STEALTH)
    return ctx


def _click(page, sels: list[str]) -> bool:
    for s in sels:
        el = page.locator(s).first
        if el.count() and el.is_visible():
            el.click()
            return True
    return False


def _wait(page, sec: float = 0.5) -> None:
    time.sleep(sec)
    try:
        page.wait_for_load_state("domcontentloaded", timeout=8000)
    except Exception:
        pass


def _age_gate(page) -> None:
    for _ in range(3):
        if "agecheck" not in page.url:
            return
        sel = page.locator("#ageYear, select#ageYear").first
        if sel.count():
            sel.select_option("1990")
        _click(page, ["#view_product_page_btn", "a:has-text('View Page')", "button:has-text('View Page')"])
        _wait(page, 2)


# --- Epic CDP -----------------------------------------------------------------

def _chrome_path() -> Path | None:
    for p in [
        Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
        Path(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"),
        Path.home() / "AppData/Local/Google/Chrome/Application/chrome.exe",
    ]:
        if p.exists():
            return p
    return None


def _port_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            return True
    except OSError:
        return False


def _start_epic_chrome(*, headless: bool | None = None) -> bool:
    if _port_open(EPIC_CDP_PORT):
        return True
    chrome = _chrome_path()
    if not chrome:
        return False
    use_headless = BROWSER_HEADLESS if headless is None else headless
    prof = PROFILE["epic"]
    prof.mkdir(parents=True, exist_ok=True)
    args = [
        str(chrome), f"--remote-debugging-port={EPIC_CDP_PORT}",
        f"--user-data-dir={prof.resolve().as_posix()}", "--no-first-run",
        "https://store.epicgames.com/en-US/",
    ]
    if use_headless:
        args[1:1] = ["--headless=new"]
    subprocess.Popen(
        args,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if platform.system() == "Windows" else 0,
    )
    for _ in range(25):
        if _port_open(EPIC_CDP_PORT):
            time.sleep(2)
            return True
        time.sleep(1)
    return False


def _epic_logged_in(page) -> bool:
    try:
        names = {c["name"] for c in page.context.cookies(
            ["https://store.epicgames.com", "https://www.epicgames.com"])}
        if names & {"EPIC_SESSION_AP", "EPIC_SSO", "EPIC_BEARER_TOKEN"}:
            return True
    except Exception:
        pass
    for tab in page.context.pages:
        if tab.locator("[data-testid='user-display-name'], button[aria-label='Account']").count():
            return True
        if "store.epicgames.com" in tab.url:
            si = tab.locator("a:has-text('Sign in'), button:has-text('Sign in')").first
            if not (si.count() and si.is_visible()):
                return True
    return False


def _epic_page(pw, *, headless: bool = True):
    if not _start_epic_chrome(headless=headless):
        raise RuntimeError("Epic Chrome failed to start")
    browser = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{EPIC_CDP_PORT}")
    for ctx in browser.contexts:
        for tab in ctx.pages:
            if "epicgames.com" in tab.url:
                return browser, tab
        if ctx.pages:
            return browser, ctx.pages[0]
    return browser, browser.contexts[0].new_page()


def scrape_epic_f2p_slugs(limit: int = 30) -> list[tuple[str, str]]:
    """Scrape Epic free browse via CDP Chrome (page is JS-rendered)."""
    import re
    from playwright.sync_api import sync_playwright

    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    browse = "https://store.epicgames.com/en-US/browse?priceTier=free&category=game&count=40"
    with sync_playwright() as pw:
        browser, page = _epic_page(pw, headless=True)
        page.goto(browse, wait_until="domcontentloaded", timeout=120000)
        page.wait_for_timeout(4000)
        for a in page.locator("a[href*='/en-US/p/']").all():
            href = a.get_attribute("href") or ""
            m = re.search(r"/en-US/p/([a-z0-9][a-z0-9-]+)", href)
            if not m:
                continue
            slug = m.group(1)
            if slug in seen or slug in ("free-games", "browse"):
                continue
            seen.add(slug)
            title = (a.get_attribute("aria-label") or a.inner_text() or "").strip()
            if not title or len(title) < 2:
                title = slug.replace("-", " ").title()
            out.append((title, slug))
            if len(out) >= limit:
                break
        browser.close()
        pw.stop()
    return out


# --- store checks -------------------------------------------------------------

def _game_store(g: Game) -> str | None:
    if g.platform == "Steam":
        return "steam"
    if g.platform == "Epic Games":
        return "epic"
    if g.platform == "GOG":
        return "gog"
    return None


def _steam_ok(page) -> bool:
    if "login.steampowered.com" in page.url:
        return False
    u = page.locator("#input_username").first
    if u.count() and u.is_visible():
        return False

    try:
        for c in page.context.cookies(
            ["https://store.steampowered.com", "https://steamcommunity.com"]
        ):
            if c["name"] in ("steamLoginSecure", "steamLogin") and len(c.get("value", "")) > 10:
                return True
    except Exception:
        pass

    p = page.locator("#account_pulldown").first
    return p.count() > 0 and p.is_visible()


def _gog_ok(page) -> bool:
    """Detect GOG web session — cookies + UI (GOG changes hooks often)."""
    try:
        domains = [
            "https://www.gog.com",
            "https://login.gog.com",
            "https://auth.gog.com",
            "https://embed.gog.com",
        ]
        for c in page.context.cookies(domains):
            name = c["name"]
            val = c.get("value", "")
            if name == "galaxy-login-signature" and len(val) > 30:
                return True
            if name in ("gog_lc", "gog-al", "GalaxyLoginToken") and len(val) > 15:
                return True
            if "login" in name.lower() and len(val) > 40:
                return True
    except Exception:
        pass

    anon = page.locator("[hook-test='menuAnonymousButton']").first
    if anon.count() and anon.is_visible():
        return False

    for text in ("Sign in", "Log in"):
        el = page.locator(f"a:has-text('{text}'), button:has-text('{text}')").first
        if el.count() and el.is_visible():
            return False

    for sel in (
        "[hook-test='menuUserButton']",
        "[hook-test='menuLoggedButton']",
        'a[href*="/account"]',
        'a[href^="https://www.gog.com/u/"]',
        '[data-testid="header-profile"]',
        '[class*="userMenu"]',
        '[class*="UserMenu"]',
        ".header__user",
    ):
        el = page.locator(sel).first
        if el.count() and el.is_visible():
            return True

    # Account page redirects to login when signed out
    try:
        cur = page.url
        page.goto("https://www.gog.com/account", wait_until="domcontentloaded", timeout=45000)
        _wait(page, 2)
        if "login" in page.url.lower():
            page.goto(cur, wait_until="domcontentloaded", timeout=30000)
            return False
        if page.locator("text=Order history").count() or page.locator("text=My products").count():
            return True
        if not page.locator("a:has-text('Sign in')").first.is_visible():
            return True
        page.goto(cur, wait_until="domcontentloaded", timeout=30000)
    except Exception:
        pass

    return False


def _check_store(store: str) -> bool:
    from playwright.sync_api import sync_playwright

    if store == "epic" and EPIC_CDP:
        try:
            with sync_playwright() as pw:
                browser, page = _epic_page(pw)
                ok = _epic_logged_in(page)
                browser.close()
                pw.stop()
                return ok
        except Exception:
            return False

    urls = {"steam": "https://store.steampowered.com", "gog": "https://www.gog.com/en/"}
    checks = {"steam": _steam_ok, "gog": _gog_ok}
    if store not in urls:
        return False
    for attempt in range(2):
        with sync_playwright() as pw:
            ctx = _launch(pw, PROFILE[store])
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.goto(urls[store], wait_until="domcontentloaded", timeout=60000)
            _wait(page, 1.5 if attempt else 0.5)
            ok = checks[store](page)
            ctx.close()
            if ok:
                return True
        time.sleep(1)
    return False


def _interactive_login(store: str) -> bool:
    from playwright.sync_api import sync_playwright

    log.info("[%s] Interactive login — complete sign-in in browser (%d min max)", store, LOGIN_TIMEOUT)
    deadline = time.time() + LOGIN_TIMEOUT * 60

    if store == "epic" and EPIC_CDP:
        with sync_playwright() as pw:
            browser, page = _epic_page(pw)
            _click(page, ["a:has-text('Sign in')", "button:has-text('Sign in')"])
            while time.time() < deadline:
                if _epic_logged_in(page):
                    time.sleep(2)
                    browser.close()
                    pw.stop()
                    return True
                time.sleep(2)
            browser.close()
            pw.stop()
        return False

    urls = {"steam": "https://store.steampowered.com/login/", "gog": "https://www.gog.com/en/"}
    checks = {"steam": _steam_ok, "gog": _gog_ok}
    with sync_playwright() as pw:
        ctx = _launch(pw, PROFILE[store])
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(urls[store], wait_until="domcontentloaded", timeout=60000)
        _wait(page, 3)
        while time.time() < deadline:
            if checks[store](page):
                time.sleep(2)
                ctx.close()
                return True
            time.sleep(2)
        ctx.close()
    return False


def ensure_logins(stores: list[str], *, interactive: bool) -> set[str]:
    ok: set[str] = set()
    for store in stores:
        if _check_store(store):
            log.info("[%s] Already logged in (session OK)", store)
            ok.add(store)
        elif interactive or not BROWSER_HEADLESS:
            if _interactive_login(store):
                log.info("[%s] Login saved — session stored", store)
                ok.add(store)
            else:
                log.warning("[%s] Login failed", store)
        else:
            log.warning("[%s] Not logged in — run: python hunter.py --login-only --stores %s", store, store)
    return ok


# --- claim --------------------------------------------------------------------

def _visible(page, selector: str) -> bool:
    el = page.locator(selector).first
    return el.count() > 0 and el.is_visible()


def _steam_needs_add(page) -> bool:
    """True if store still shows Add to Library / Add to Account."""
    if "agecheck" in page.url:
        return True
    for text in ("Add to Library", "Add to Account"):
        if _visible(page, f"#game_area_purchase a:has-text('{text}')"):
            return True
        if _visible(page, f"#game_area_purchase button:has-text('{text}')"):
            return True
        if _visible(page, f"a.btn_green_steamui:has-text('{text}')"):
            return True
    return False


def _steam_in_library(page) -> bool:
    if "agecheck" in page.url:
        return False
    if _visible(page, "div.game_area_already_owned"):
        return True
    if _visible(page, "#game_area_purchase a:has-text('Add to Library')"):
        return False
    if _visible(page, "#game_area_purchase a:has-text('Add to Account')"):
        return False
    if _visible(page, "a:has-text('Install Game')"):
        return True
    return False


def _steam_app_id(g: Game) -> str | None:
    m = re.search(r"/app/(\d+)/", g.url)
    if m:
        return m.group(1)
    m = re.match(r"(?:f2p|free)-(\d+)$", g.id)
    if m:
        return m.group(1)
    return None


def _steam_addfreelicense(page, app_id: str) -> bool:
    """Add free license via Steam endpoint — works for F2P and weekly giveaways."""
    page.goto(
        f"https://store.steampowered.com/freelicense/addfreelicense/{app_id}",
        wait_until="commit",
        timeout=25000,
    )
    _wait(page, 0.6)
    body = page.content().lower()
    if any(
        x in body
        for x in (
            "successfully",
            "added to your account",
            "already own",
            "already in your library",
            "is already registered",
            "play game",
        )
    ):
        return True
    if "addfreelicense" in page.url or "freelicense" in page.url:
        return True
    return False


def _claim_steam(page, g: Game) -> bool:
    app_id = _steam_app_id(g)
    if app_id and _steam_addfreelicense(page, app_id):
        log.info("[Steam] Added via license URL: %s", g.title)
        return True

    page.goto(g.url, wait_until="domcontentloaded", timeout=25000)
    _wait(page)
    _age_gate(page)
    if _steam_in_library(page):
        log.info("[Steam] Already in library: %s", g.title)
        return True

    btns = [
        "#game_area_purchase a:has-text('Add to Library')",
        "#game_area_purchase button:has-text('Add to Library')",
        "div.game_area_purchase_game_wrapper a:has-text('Add to Library')",
        "a.btn_green_steamui:has-text('Add to Library')",
        "a:has-text('Add to Library')",
        "button:has-text('Add to Library')",
        "#game_area_purchase a:has-text('Add to Account')",
        "a.btn_green_steamui:has-text('Add to Account')",
        "a:has-text('Add to Account')",
    ]
    if g.kind == "f2p":
        btns.extend([
            "#game_area_purchase a:has-text('Play Game')",
            "a.btn_green_steamui:has-text('Play Game')",
            "a:has-text('Play Game')",
        ])
    if not _click(page, btns):
        log.warning("[Steam] No claim button for %s", g.title)
        return False

    try:
        page.wait_for_url(re.compile(r"addfreelicense|freelicense|run/"), timeout=8000)
    except Exception:
        pass
    _wait(page, 0.8)

    if "addfreelicense" in page.url or "freelicense" in page.url:
        return True

    return _steam_in_library(page)


def _epic_needs_add(page) -> bool:
    for sel in [
        "button:has-text('Add to Library')",
        "span:has-text('Add to Library')",
        "[data-testid='purchase-cta-button']:has-text('Add to Library')",
        "button:has-text('Get')",
        "button:has-text('Claim')",
    ]:
        if _visible(page, sel):
            return True
    return False


def _epic_in_library(page) -> bool:
    for text in ("In Library", "Owned"):
        el = page.locator(f"text={text}").first
        if el.count() and el.is_visible():
            return True
    return False


def _claim_epic(page, g: Game) -> bool:
    page.goto(g.url, wait_until="domcontentloaded", timeout=25000)
    _wait(page, 0.8)
    if _epic_in_library(page):
        log.info("[Epic] Already in library: %s", g.title)
        return True

    if not _click(page, [
        "button:has-text('Add to Library')",
        "span:has-text('Add to Library')",
        "[data-testid='purchase-cta-button']:has-text('Add to Library')",
        "button:has-text('Get')",
        "button:has-text('Claim')",
        "[data-testid='purchase-cta-button']",
    ]):
        log.warning("[Epic] No Add to Library/Get for %s", g.title)
        return False

    _wait(page, 0.8)
    _click(page, [
        "button:has-text('Place Order')",
        "button:has-text('Check Out')",
        "button:has-text('Order')",
        "button:has-text('Accept')",
    ])
    _wait(page, 1.2)
    return _epic_in_library(page)


def _claim_gog(page, g: Game) -> bool:
    page.goto(g.url, wait_until="domcontentloaded", timeout=25000)
    _wait(page)
    if page.locator("text=Owned").count():
        return True
    if "gamerpower.com" in page.url:
        _click(page, ["a:has-text('GET')", "a:has-text('Get')"])
        _wait(page)
    if not _click(page, ["button:has-text('Get it free')", "a:has-text('Get it free')"]):
        return False
    _wait(page)
    _click(page, ["button:has-text('Yes, claim it')", "span:has-text('Yes, claim it')"])
    _wait(page, 2)
    return page.locator("text=Owned").count() > 0


def run_claims(
    games: list[Game],
    stores: set[str],
    *,
    progress: Callable[[str, int, int], None] | None = None,
) -> tuple[list[Game], list[Game]]:
    from playwright.sync_api import sync_playwright

    def _prog(msg: str, i: int = 0, n: int = 0) -> None:
        if progress:
            progress(msg, i, n)

    epic = [g for g in games if g.platform == "Epic Games" and g.claimable]
    steam = [g for g in games if g.platform == "Steam" and g.claimable]
    gog = [g for g in games if g.platform == "GOG" and g.claimable]

    claimed: list[Game] = []
    failed: list[Game] = []

    if "epic" in stores and epic:
        if EPIC_CDP:
            try:
                _prog(f"Epic — 0/{len(epic)}", 0, len(epic))
                with sync_playwright() as pw:
                    browser, page = _epic_page(pw, headless=True)
                    if not _epic_logged_in(page):
                        failed.extend(epic)
                    else:
                        for i, g in enumerate(epic, 1):
                            _prog(f"Epic — {g.title[:30]}", i, len(epic))
                            try:
                                if _claim_epic(page, g):
                                    claimed.append(g)
                                else:
                                    failed.append(g)
                            except Exception:
                                failed.append(g)
                    browser.close()
                    pw.stop()
            except Exception:
                log.exception("Epic claim failed")
                failed.extend(epic)
        else:
            failed.extend(epic)

    if "steam" in stores and steam:
        _prog(f"Steam — 0/{len(steam)}", 0, len(steam))
        with sync_playwright() as pw:
            ctx = _launch(pw, PROFILE["steam"], headless=True)
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.goto("https://store.steampowered.com", wait_until="domcontentloaded", timeout=30000)
            if not _steam_ok(page):
                failed.extend(steam)
            else:
                for i, g in enumerate(steam, 1):
                    _prog(f"Steam — {g.title[:30]}", i, len(steam))
                    try:
                        if _claim_steam(page, g):
                            claimed.append(g)
                        else:
                            failed.append(g)
                    except Exception:
                        failed.append(g)
            ctx.close()

    if "gog" in stores and gog:
        _prog(f"GOG — 0/{len(gog)}", 0, len(gog))
        with sync_playwright() as pw:
            ctx = _launch(pw, PROFILE["gog"], headless=True)
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.goto("https://www.gog.com/en/", wait_until="domcontentloaded", timeout=30000)
            if not _gog_ok(page):
                failed.extend(gog)
            else:
                for i, g in enumerate(gog, 1):
                    _prog(f"GOG — {g.title[:30]}", i, len(gog))
                    try:
                        if _claim_gog(page, g):
                            claimed.append(g)
                        else:
                            failed.append(g)
                    except Exception:
                        failed.append(g)
            ctx.close()

    seen: set[str] = set()
    uniq_c = [g for g in claimed if g.fp() not in seen and not seen.add(g.fp())]
    seen_c = {g.fp() for g in uniq_c}
    uniq_f = [g for g in failed if g.fp() not in seen_c]
    return uniq_c, uniq_f
