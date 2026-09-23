"""Test double for stdin_print_cli_wrapper: record argv and prompt-file contents."""

from __future__ import annotations

import json
import sys
from pathlib import Path


def main() -> int:
    out_path = Path(sys.argv[1])
    argv = sys.argv[2:]
    prompt_file_text = None
    if "--prompt-file" in argv:
        flag_index = argv.index("--prompt-file")
        prompt_file_text = Path(argv[flag_index + 1]).read_text(encoding="utf-8")
    out_path.write_text(
        json.dumps({"argv": argv, "prompt_file_text": prompt_file_text}),
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
