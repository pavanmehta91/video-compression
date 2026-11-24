"""Compatibility launcher for the original script command."""

import sys

from video_compression.cli import main


if __name__ == "__main__":
    sys.exit(main())
