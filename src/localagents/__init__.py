import sys


def main() -> None:
    # `localagents statusline` runs on every status-bar refresh: answer it without importing the
    # server (mcp, httpx, uvicorn), which takes far longer than the render itself.
    if sys.argv[1:2] == ["statusline"]:
        from .status import render

        print(render())
        return
    from .server import main as serve

    serve()


__all__ = ["main"]
