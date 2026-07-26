import typer

app = typer.Typer(
    name="envcheck",
    help="Environment Parity Checker — detect drift across dev, staging, and prod",
    no_args_is_help=True,
)


@app.callback()
def callback():
    """envcheck: compare environment configurations across environments."""


@app.command()
def version():
    """Show the installed version."""
    from importlib.metadata import version as get_version

    ver = get_version("envcheck")
    typer.echo(f"envcheck v{ver}")
