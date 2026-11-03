"""Windowed Auralis entrypoint; pythonw keeps Windows Desktop console-free."""
import sys

from overlay.main import main


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
