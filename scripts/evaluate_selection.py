"""Create a blank human reference, then compare result snapshots at a fixed budget."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from landscape_culler.selection_evaluation import create_reference_template, evaluate_selection


def read_snapshot(path: Path) -> tuple[dict, str]:
    raw = path.read_bytes()
    return json.loads(raw.decode("utf-8-sig")), hashlib.sha256(raw).hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser(description="固定保留比例的人工选片质量评测（不运行模型，不读写原片）")
    commands = parser.add_subparsers(dest="command", required=True)
    template = commands.add_parser("template", help="从评分结果创建空白人工参考")
    template.add_argument("--results", type=Path, required=True)
    template.add_argument("--output", type=Path, required=True)
    evaluate = commands.add_parser("evaluate", help="按同一人工参考评测一个或多个评分结果")
    evaluate.add_argument("--reference", type=Path, required=True)
    evaluate.add_argument("--results", type=Path, nargs="+", required=True)
    evaluate.add_argument("--retain-ratio", type=float)
    evaluate.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.command == "template":
            snapshot, digest = read_snapshot(args.results)
            report = create_reference_template(snapshot)
            report["source_results_sha256"] = digest
        else:
            reference, digest = read_snapshot(args.reference)
            reports = []
            for path in args.results:
                payload, prediction_hash = read_snapshot(path)
                reports.append({"results_file": str(path), "results_sha256": prediction_hash,
                    **evaluate_selection(payload, reference, args.retain_ratio)})
            report = {"reference_sha256": digest, "evaluations": reports}
        # Exclusive create prevents accidentally overwriting a reference,
        # source snapshot or a previous evaluation artifact.
        with args.output.open("x", encoding="utf-8") as output:
            json.dump(report, output, ensure_ascii=False, indent=2, allow_nan=False)
            output.write("\n")
    except (OSError, ValueError, TypeError) as exc:
        parser.exit(2, f"评测未完成：{exc}\n")
    print(f"已保存：{args.output}")


if __name__ == "__main__":
    main()
