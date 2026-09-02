# Consoles that keep their login in session cookies (Chromium does not persist
# those across browser restarts) get a standalone Edge of their own: launched
# once via subprocess with a CDP port, parked off-screen, and reused by every
# later run through connect_over_cdp.  The monitor process may exit; the
# browser keeps the session alive.
EDGE_CANDIDATES = (
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
)
CDP_BASE_PORT = 9330
_OFFSCREEN = ("--window-position=-32000,-32000",
              "--disable-backgrounding-occluded-windows",
              "--disable-renderer-backgrounding")


def _cdp_port(name: str) -> int:
    return CDP_BASE_PORT + sorted(RECIPES).index(name)


def _edge_executable() -> str:
    from shutil import which

    for path in EDGE_CANDIDATES:
        if Path(path).exists():
            return path
    found = which("msedge")
    if found:
        return found
    raise ProviderError("Microsoft Edge not found; install it or point "
                        "EDGE_CANDIDATES at your browser")


def _port_open(port: int) -> bool:
    import socket

    try:
        import socket as s
        with s.create_connection(("127.0.0.1", port), timeout=0.3):
            return True
    except OSError:
        return False


def _ensure_cdp_browser(name: str, recipe: Recipe, profile: Path,
                        on_screen: bool) -> int:
    """Start the provider's standalone Edge if it is not already running."""
    import subprocess

    port = _cdp_port(name)
    if _port_open(port):
        return port
    args = [_edge_executable(),
            f"--remote-debugging-port={port}",
            f"--user-data-dir={profile}",
            "--no-first-run", "--no-default-browser-check",
            "--window-size=1440,900",
            *_OFFSCREEN if not on_screen else (),
            recipe.start_url]
    subprocess.Popen(args)
    for _ in range(60):                     # up to 12 s for the CDP endpoint
        if _port_open(port):
            return port
        time.sleep(0.2)
    raise ProviderError(f"[{name}] the standalone browser did not open its "
                        f"debug port {port}")


def _connect(name: str):
    pw = sync_playwright().start()
    browser = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{_cdp_port(name)}")
    context = browser.contexts[0]
    return pw, browser, context


def _cdp_fetch(recipe: Recipe, name: str, wait_s: float) -> list[SubscriptionItem]:
    """One fetch through the resident standalone browser."""
    _ensure_cdp_browser(name, recipe, _profile_dir(name), on_screen=False)
    pw, _browser, context = _connect(name)
    captured: dict[str, list[dict[str, Any]]] = {}
    last_error: ProviderError | None = None

    def on_response(resp) -> None:
        for fragment in recipe.expect:
            if fragment in resp.url:
                try:
                    captured.setdefault(fragment, []).append(resp.json())
                except Exception:
                    pass

    page = context.new_page()
    try:
        page.on("response", on_response)
        page.goto(recipe.start_url, wait_until="domcontentloaded")
        remaining, reloaded = wait_s, False
        while remaining > 0:
            page.wait_for_timeout(500)
            remaining -= 0.5
            try:
                return recipe.parse(captured)
            except ProviderError as exc:
                last_error = exc
            if remaining < wait_s / 2 and not reloaded:
                page.reload(wait_until="domcontentloaded")
                reloaded = True
    finally:
        page.close()
        pw.stop()                           # disconnect only; the browser stays

    raise ProviderError(
        f"the {name} console is not signed in (last error: {last_error}). "
        f"Run 'epd_monitor.py login --provider {name}' again."
    )


def _cdp_login(name: str, recipe: Recipe, profile: Path, timeout_s: float) -> bool:
    """Headed sign-in in the standalone browser, then park it off-screen for
    the regular fetches."""
    _ensure_cdp_browser(name, recipe, profile, on_screen=True)
    pw, _browser, context = _connect(name)
    captured: dict[str, list[dict[str, Any]]] = {}

    def on_response(resp) -> None:
        for fragment in recipe.expect:
            if fragment in resp.url:
                try:
                    captured.setdefault(fragment, []).append(resp.json())
                except Exception:
                    pass

    page = context.new_page()
    try:
        page.on("response", on_response)
        page.goto(recipe.start_url, wait_until="domcontentloaded")
        remaining = timeout_s
        while remaining > 0:
            page.wait_for_timeout(1000)
            remaining -= 1
            try:
                recipe.parse(captured)
                break
            except ProviderError:
                continue
    finally:
        try:
            page.close()
        except Exception:
            pass
        pw.stop()

    try:
        items = recipe.parse(captured)
    except ProviderError as exc:
        print(f"[{name}] login not completed ({exc}); run login again", file=sys.stderr)
        return False

    # The window stays open (the session lives in it); park it off-screen.
    try:
        pw2 = sync_playwright().start()
        browser = pw2.chromium.connect_over_cdp(f"http://127.0.0.1:{_cdp_port(name)}")
        page = browser.contexts[0].pages[0] if browser.contexts[0].pages else \
            browser.contexts[0].new_page()
        _park_offscreen(browser.contexts[0], page)
        pw2.stop()
    except Exception:
        pass
    print(f"[{name}] signed in ({len(items)} item(s)); browser parked off-screen "
          "and left running")
    return True


def open_login(provider_type: str, timeout_s: float = 540.0) -> bool:
    """Open the provider's page headed and wait for the user to sign in.

    On success the window parks itself off-screen and stays running for later
    fetches.  If a resident browser is already signed in, this is a no-op.
    """
    recipe = RECIPES.get(provider_type)
    if recipe is None:
        raise ProviderError(f"unknown web provider '{provider_type}'; "
                            f"known: {sorted(RECIPES)}")
    if _cdp_port(provider_type) and _port_open(_cdp_port(provider_type)):
        # A resident browser exists; verify it still holds a session.
        return _cdp_fetch(recipe, provider_type, timeout_s)
    return _PW_POOL.submit(_login, provider_type, recipe,
                           _profile_dir(provider_type), timeout_s).result()


class _WebProviderBase(ProviderBase):
    """Shared fetch for the recipe-driven web providers."""

    recipe: Recipe

    async def fetch(self) -> list[SubscriptionItem]:
        recipe = self.recipe
        name = self.provider_type
        profile = _profile_dir(name)
        headless = bool(self._cfg.get("headless", True))
        wait_s = float(self._cfg.get("wait_seconds", DEFAULT_WAIT_S))
        if headless:
            return await _pw_run(_capture_and_parse, recipe, profile, wait_s)
        return await _pw_run(_cdp_fetch, recipe, name, wait_s)
