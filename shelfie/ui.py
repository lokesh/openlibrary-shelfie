"""Terminal UI helpers built on rich + questionary.

Centralizes every styled print, prompt, spinner, and progress bar so cli.py
stays focused on the OL-specific logic.
"""

from contextlib import contextmanager
from urllib.parse import urlparse

import questionary
from questionary import Style as QStyle
from rich.box import SIMPLE_HEAVY
from rich.columns import Columns
from rich.console import Console
from rich.padding import Padding
from rich.progress import (
    BarColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeElapsedColumn,
)
from rich.rule import Rule
from rich.table import Table
from rich.text import Text

console = Console()

# Shared questionary palette. Cyan accents, green answers — matches the
# rich console scheme below.
QSTYLE = QStyle(
    [
        ("qmark", "fg:#00d7d7 bold"),
        ("question", "bold"),
        ("answer", "fg:#5fd75f bold"),
        ("pointer", "fg:#00d7d7 bold"),
        ("highlighted", "fg:#00d7d7 bold"),
        ("selected", "fg:#5fd75f"),
        ("instruction", "fg:#808080 italic"),
        ("separator", "fg:#404040"),
    ]
)


class UserExit(Exception):
    """Raised when the user cancels a prompt or hits Ctrl-C / Esc."""


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------


def _ask(question):
    """Run a questionary question; raise UserExit on cancel."""
    try:
        result = question.ask()
    except KeyboardInterrupt:
        raise UserExit
    if result is None:
        raise UserExit
    return result


def choose(prompt, options):
    """Arrow-key menu. Returns the selected option."""
    return _ask(
        questionary.select(
            prompt,
            choices=options,
            style=QSTYLE,
            qmark="?",
            instruction="(↑/↓ then enter, esc to quit)",
            use_indicator=False,
            pointer="❯",
        )
    )


def ask(prompt, default=""):
    """Free-text prompt with optional default."""
    return _ask(questionary.text(prompt, default=default, style=QSTYLE, qmark="?"))


def confirm(prompt):
    """Y/N prompt. Defaults to no."""
    return bool(
        _ask(questionary.confirm(prompt, default=False, style=QSTYLE, qmark="?"))
    )


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------


def header(title):
    """Section header — a styled rule with a title."""
    console.print()
    console.print(Rule(f"[bold cyan] {title} [/bold cyan]", style="cyan", align="left"))


def banner(server_url, stats_pairs):
    """Startup banner. `stats_pairs` is a list of (label, value) tuples."""
    logo = Text.from_markup(
        "[bold cyan]"
        "   ____  _          _  __ _\n"
        "  / ___|| |__   ___| |/ _(_) ___\n"
        "  \\___ \\| '_ \\ / _ \\ | |_| |/ _ \\\n"
        "   ___) | | | |  __/ |  _| |  __/\n"
        "  |____/|_| |_|\\___|_|_| |_|\\___|"
        "[/bold cyan]"
    )

    # Stats laid out as a 2-col borderless table — labels dim, values bold.
    stats = Table.grid(padding=(0, 2))
    stats.add_column(style="dim")
    stats.add_column(justify="right", style="bold cyan")
    stats.add_column(style="dim")
    stats.add_column(justify="right", style="bold cyan")

    pairs = list(stats_pairs)
    for i in range(0, len(pairs), 2):
        left = pairs[i]
        right = pairs[i + 1] if i + 1 < len(pairs) else ("", "")
        stats.add_row(left[0], str(left[1]), right[0], str(right[1]))

    body = Table.grid(padding=(0, 0))
    body.add_column()
    body.add_row(logo)
    body.add_row("")
    body.add_row(Text.from_markup("[bold]shelfie[/bold] [dim]·[/dim] Open Library Dev Tool"))
    body.add_row(Text.from_markup(f"[dim]connected to[/dim] [cyan]{server_url}[/cyan]"))
    body.add_row("")
    body.add_row(stats)

    console.print(Padding(body, (1, 2)))


def stats_table(title, rows):
    """Build a small two-column (label, value) table for stats panes."""
    table = Table(
        title=f"[bold cyan]{title}[/bold cyan]",
        title_justify="left",
        show_header=False,
        box=SIMPLE_HEAVY,
        pad_edge=False,
        expand=False,
    )
    table.add_column(style="dim")
    table.add_column(justify="right", style="bold")
    for label, value in rows:
        table.add_row(label, str(value))
    return table


# ---------------------------------------------------------------------------
# Status messages
# ---------------------------------------------------------------------------


def success(msg):
    console.print(f"[green]✓[/green] {msg}", highlight=False)


def info(msg):
    console.print(f"[cyan]›[/cyan] {msg}", highlight=False)


def warn(msg):
    console.print(f"[yellow]![/yellow] {msg}", highlight=False)


def error(msg):
    console.print(f"[red]✗[/red] {msg}", highlight=False)


def dim(msg):
    console.print(f"[dim]{msg}[/dim]", highlight=False)


def plain(msg):
    """Print without any markup interpretation."""
    console.print(msg, highlight=False)


# ---------------------------------------------------------------------------
# Friendly error reporting
# ---------------------------------------------------------------------------
#
# Most users hitting an error in shelfie will be early-stage developers
# setting up a local Open Library stack for the first time. Raw `requests`
# exceptions ("HTTPConnectionPool ... NameResolutionError") aren't readable
# at that level. friendly_error() inspects the exception and returns a
# plain-English headline plus actionable hints; report_error() prints them.

# Hostnames that only exist inside the OL Docker network. If we see a DNS
# failure on one of these, the user is almost certainly running outside the
# stack and needs the docker-compose command, not a network-debugging rabbit
# hole.
DOCKER_HOSTNAMES = {"web", "infobase", "solr"}


def _parse_host_port(url):
    if not url:
        return None, None
    try:
        parsed = urlparse(url if "://" in url else "http://" + url)
        return parsed.hostname, parsed.port
    except (ValueError, AttributeError):
        return None, None


def _truncate(text, n=200):
    text = " ".join((text or "").split())
    return text if len(text) <= n else text[:n] + "…"


def friendly_error(exc, target_url=None):
    """Translate a network/HTTP exception into (headline, hints).

    headline is a single short sentence; hints is a list of follow-up lines
    suggesting what to try. Designed for early-stage developers — prefers
    plain language and concrete next steps over technical accuracy.
    """
    host, port = _parse_host_port(target_url)
    label = host or "the server"
    msg = str(exc)
    msg_lower = msg.lower()

    # HTTP status codes — works for both our OLError (`.code`) and
    # requests.HTTPError (`.response.status_code`).
    code = getattr(exc, "code", None) or getattr(
        getattr(exc, "response", None), "status_code", None
    )
    if code:
        return _http_status_message(code, host, exc)

    # DNS — multiple message shapes across platforms and urllib3 versions.
    dns_markers = (
        "name or service not known",
        "nameresolutionerror",
        "nodename nor servname",
        "temporary failure in name resolution",
        "failed to resolve",
        "getaddrinfo failed",
    )
    if any(m in msg_lower for m in dns_markers):
        return _dns_message(host)

    if "connection refused" in msg_lower:
        return _refused_message(host, port)

    if "timed out" in msg_lower or "timeout" in msg_lower:
        return _timeout_message(host)

    if "connection" in msg_lower and "reset" in msg_lower:
        return (
            f"Connection to {label} was reset.",
            ["The service may be restarting. Wait a moment and try again."],
        )

    return (f"{type(exc).__name__} talking to {label}.", [_truncate(msg)])


def _dns_message(host):
    if host in DOCKER_HOSTNAMES:
        return (
            f"Can't resolve '{host}' — looks like you're not inside the OL Docker stack.",
            [
                "Shelfie's defaults are Docker hostnames; they only resolve inside OL's network.",
                "From your OL clone, run:  docker compose run --rm shelfie",
                "Or pass --url to point at a different host.",
            ],
        )
    label = f"'{host}'" if host else "the hostname"
    return (
        f"Can't resolve {label}.",
        ["Check your network connection or the URL you're using."],
    )


def _refused_message(host, port):
    label = f"{host}:{port}" if host and port else (host or "the server")
    if host in DOCKER_HOSTNAMES:
        return (
            f"{label} refused the connection — the service isn't running.",
            [
                "From your OL clone, see what's up:  docker compose ps",
                "Start the stack if needed:  docker compose up -d",
            ],
        )
    return (f"{label} refused the connection.", ["Is the service running on that port?"])


def _timeout_message(host):
    label = host or "the server"
    if host in DOCKER_HOSTNAMES:
        return (
            f"{label} timed out.",
            [
                "It may still be starting up — wait ~30s and try again.",
                f"Check progress:  docker compose logs {host}",
            ],
        )
    return (f"{label} timed out.", ["Try again, or check your network."])


def _http_status_message(code, host, exc):
    label = host or "the server"
    if code in (401, 403):
        return (
            f"Authentication rejected ({code}).",
            [
                "Default dev credentials: openlibrary@example.com / admin123",
                "Override with --email and --password.",
            ],
        )
    if code == 404:
        return (f"Not found (404) on {label}.", ["The endpoint or resource doesn't exist."])
    if 500 <= code < 600:
        hints = []
        if host in DOCKER_HOSTNAMES:
            hints.append(f"Check service logs:  docker compose logs {host}")
        body = _truncate(getattr(exc, "text", "") or "")
        if body:
            hints.append(f"Server said: {body}")
        return (f"{label} returned {code} (server error).", hints)
    return (f"HTTP {code} from {label}.", [_truncate(str(exc))])


def report_error(exc, target_url=None, operation=None):
    """Print a friendly error message with hints for what to try next."""
    headline, hints = friendly_error(exc, target_url)
    prefix = f"{operation}: " if operation else ""
    error(prefix + headline)
    for hint in hints:
        dim(f"  {hint}")


# ---------------------------------------------------------------------------
# Long-running operations
# ---------------------------------------------------------------------------


@contextmanager
def spinner(message):
    """Show a spinner while a block runs. Spinner clears on exit."""
    with console.status(f"[cyan]{message}[/cyan]", spinner="dots"):
        yield


def import_progress():
    """Progress factory for parallel imports — tracks ok/err counters."""
    return Progress(
        SpinnerColumn(),
        TextColumn("[bold]{task.description}"),
        BarColumn(bar_width=None),
        TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
        TextColumn(
            "[green]✓ {task.fields[ok]:>3}[/green]  [red]✗ {task.fields[err]:>3}[/red]"
        ),
        TimeElapsedColumn(),
        console=console,
        transient=False,
    )


def step_progress():
    """Progress factory for sequential steps (no error/success split)."""
    return Progress(
        SpinnerColumn(),
        TextColumn("[bold]{task.description}"),
        BarColumn(bar_width=None),
        TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
        TextColumn("{task.completed}/{task.total}"),
        TimeElapsedColumn(),
        console=console,
        transient=False,
    )


def failure_logger(progress, limit=3):
    """Returns a callable that prints up to `limit` formatted failures
    inside the given progress, then silently swallows the rest.

    Why: long batches with thousands of items shouldn't dump every error
    to the console — the first few are usually enough to diagnose.
    """
    shown = 0

    def log(label, err):
        nonlocal shown
        if shown < limit:
            progress.console.print(
                f"  [red]✗[/red] {label} [dim]—[/dim] {err}", highlight=False
            )
            shown += 1

    return log


__all__ = [
    "console",
    "UserExit",
    "choose",
    "ask",
    "confirm",
    "header",
    "banner",
    "stats_table",
    "success",
    "info",
    "warn",
    "error",
    "dim",
    "plain",
    "friendly_error",
    "report_error",
    "spinner",
    "import_progress",
    "step_progress",
    "failure_logger",
    # Re-exports for cli.py table-building.
    "Table",
    "Columns",
    "SIMPLE_HEAVY",
]
