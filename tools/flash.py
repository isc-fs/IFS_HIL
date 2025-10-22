#!/usr/bin/env python3
"""Mock flashing CLI entry point for the HIL bench."""

from pathlib import Path
import time
import typer

from tools.can_probe import check_can_connect
from tools.power_ctl import toggle_power

app = typer.Typer(help="HIL bench utility CLI.")


@app.command()
def flash(
    firmware: Path = typer.Option(Path("/srv/hil/build/firmware.bin"), help="Path to compiled firmware binary."),
    target: str = typer.Option("stm32f103", help="Target ECU identifier."),
) -> None:
    """Simulate flashing firmware onto the ECU."""
    typer.echo(f"Preparing to flash {target} with {firmware}")

    if not firmware.exists():
        typer.echo("Firmware binary missing; provide a built artifact.")
        raise typer.Exit(code=1)

    if not check_can_connect():
        typer.echo("CAN interface not detected; aborting.")
        raise typer.Exit(code=2)

    toggle_power(target, state=False)
    typer.echo("Transferring firmware...")
    time.sleep(1.0)
    toggle_power(target, state=True)
    typer.echo("Flash complete (simulated).")


@app.command()
def probe(channel: str = typer.Option("can0", help="CAN interface to probe.")) -> None:
    """Check if the CAN adapter is reachable."""
    if check_can_connect(channel):
        typer.echo(f"CAN channel {channel} is reachable.")
    else:
        raise typer.Exit(code=3)


@app.command()
def power(target: str, state: bool = typer.Option(..., prompt=True, help="Desired power state (true means on).")) -> None:
    """Toggle power rails for a device under test."""
    toggle_power(target, state)
    typer.echo(f"Power state for {target} set to {state} (simulated).")


def main() -> None:
    """CLI entry point used by console scripts."""
    app()


if __name__ == "__main__":
    main()
