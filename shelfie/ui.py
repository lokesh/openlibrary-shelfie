"""Terminal UI helpers built on rich + questionary.

Centralizes every styled print, prompt, spinner, and progress bar so cli.py
stays focused on the OL-specific logic.
"""

from contextlib import contextmanager

import questionary
from questionary import Style as QStyle
from rich.box import HEAVY_HEAD
from rich.columns import Columns
from rich.console import Console
from rich.live import Live
from rich.padding import Padding
from rich.panel import Panel
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


def choose(prompt, options):
    """Arrow-key menu. Returns the selected option."""
    try:
        result = questionary.select(
            prompt,
            choices=options,
            style=QSTYLE,
            qmark="?",
            instruction="(↑/↓ then enter, esc to quit)",
            use_indicator=False,
            pointer="❯",
        ).ask()
    except KeyboardInterrupt:
        raise UserExit
    if result is None:
        raise UserExit
    return result


def ask(prompt, default=""):
    """Free-text prompt with optional default."""
    try:
        result = questionary.text(
            prompt, default=default, style=QSTYLE, qmark="?"
        ).ask()
    except KeyboardInterrupt:
        raise UserExit
    if result is None:
        raise UserExit
    return result


def confirm(prompt):
    """Y/N prompt. Defaults to no."""
    try:
        return bool(
            questionary.confirm(
                prompt, default=False, style=QSTYLE, qmark="?"
            ).ask()
        )
    except KeyboardInterrupt:
        raise UserExit


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
    stats_table = Table.grid(padding=(0, 2))
    stats_table.add_column(style="dim")
    stats_table.add_column(justify="right", style="bold cyan")
    stats_table.add_column(style="dim")
    stats_table.add_column(justify="right", style="bold cyan")

    pairs = list(stats_pairs)
    # Two rows side-by-side
    for i in range(0, len(pairs), 2):
        left = pairs[i]
        right = pairs[i + 1] if i + 1 < len(pairs) else ("", "")
        stats_table.add_row(left[0], str(left[1]), right[0], str(right[1]))

    body = Table.grid(padding=(0, 0))
    body.add_column()
    body.add_row(logo)
    body.add_row("")
    body.add_row(
        Text.from_markup(
            f"[bold]shelfie[/bold] [dim]·[/dim] Open Library Dev Tool"
        )
    )
    body.add_row(Text.from_markup(f"[dim]connected to[/dim] [cyan]{server_url}[/cyan]"))
    body.add_row("")
    body.add_row(stats_table)

    console.print(Padding(body, (1, 2)))


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


# Re-exports so cli.py only imports from ui.
__all__ = [
    "console",
    "UserExit",
    "choose",
    "ask",
    "confirm",
    "header",
    "banner",
    "success",
    "info",
    "warn",
    "error",
    "dim",
    "plain",
    "spinner",
    "import_progress",
    "step_progress",
    "Table",
    "Panel",
    "Columns",
    "Padding",
    "Text",
    "HEAVY_HEAD",
    "Live",
]
