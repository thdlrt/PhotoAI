from __future__ import annotations

import argparse
import json
from pathlib import Path

from landscape_culler.offline_bundle import create_offline_bundle


def main() -> None:
    parser = argparse.ArgumentParser(description="制作 PhotoAI 16GB 离线资源包")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--source-content-root", type=Path, required=True)
    parser.add_argument("--source-model-runtime", type=Path, required=True)
    args = parser.parse_args()
    result = create_offline_bundle(
        args.output,
        source_content_root=args.source_content_root,
        source_model_runtime=args.source_model_runtime,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
