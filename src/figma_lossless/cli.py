from __future__ import annotations

import argparse
import http.server
import json
import os
import socketserver
import sys
from pathlib import Path

from .assets import vendor_assets
from .collector import CollectOptions, FigmaCollector
from .compiler import CompileOptions, DesignCompiler
from .copy_export import export_copy
from .design_export import FORMATS, export_design
from .mode import (
    MODES,
    clear_mode,
    is_locked,
    locked_message,
    resolve_mode,
    set_mode,
)
from .report import render_report
from .reuse_proposal import (
    DEFAULT_MAX_SOURCE_FREQUENCY,
    DEFAULT_MIN_OVERLAP,
    propose_reuse,
)
from .slot_proposal import (
    DEFAULT_GROUP_SIMILARITY,
    DEFAULT_MAX_INVARIANCE_RATIO,
    propose_slots,
)
from .validators import GateValidator, ValidateOptions, create_snapshot_template


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="figma-lossless",
        description=(
            "Collect Figma REST evidence losslessly, compile it into a "
            "verifiable design contract, and block incomplete UI work."
        ),
    )
    commands = parser.add_subparsers(dest="command", required=True)

    compile_parser = commands.add_parser(
        "compile", help="Compile raw MCP responses into Design IR."
    )
    compile_parser.add_argument("--input", type=Path)
    compile_parser.add_argument("--output", type=Path, required=True)
    compile_parser.add_argument("--feature-id", required=True)
    compile_parser.add_argument("--flow-contract", type=Path)
    compile_parser.add_argument("--component-map", type=Path)
    compile_parser.add_argument("--data-contract", type=Path)
    compile_parser.add_argument(
        "--rest-input",
        type=Path,
        help="Collector output directory holding canonical Figma REST node JSON.",
    )
    compile_parser.add_argument(
        "--variable-defs",
        type=Path,
        help="Figma MCP get_variable_defs payload used to resolve token names.",
    )

    snapshot_parser = commands.add_parser(
        "snapshot-template",
        help="Create the strict implementation snapshot an adapter must fill.",
    )
    snapshot_parser.add_argument("--bundle", type=Path, required=True)
    snapshot_parser.add_argument("--output", type=Path, required=True)
    snapshot_parser.add_argument(
        "--use-reference-screenshots",
        action="store_true",
        help="Use Figma images as actual images for harness self-tests only.",
    )

    vendor_parser = commands.add_parser(
        "vendor-assets",
        help="Download short-lived Figma URLs and record immutable SHA-256 hashes.",
    )
    vendor_parser.add_argument("--bundle", type=Path, required=True)
    vendor_parser.add_argument("--timeout", type=int, default=30)

    collect_parser = commands.add_parser(
        "collect",
        help="Fetch canonical node JSON evidence from the Figma REST API.",
    )
    collect_parser.add_argument("--file-key", required=True)
    collect_parser.add_argument("--output", type=Path, required=True)
    collect_parser.add_argument("--node-ids")
    collect_parser.add_argument("--manifest", type=Path)
    collect_parser.add_argument("--token-file", type=Path)
    collect_parser.add_argument(
        "--include-images",
        action="store_true",
        help=(
            "Also render each collected node via the Figma REST images "
            "endpoint and store the PNGs as reference-screenshot evidence."
        ),
    )

    validate_parser = commands.add_parser(
        "validate", help="Run all hard verification gates and render a report."
    )
    validate_parser.add_argument("--bundle", type=Path, required=True)
    validate_parser.add_argument("--actual", type=Path, required=True)
    validate_parser.add_argument("--actual-b", type=Path)
    validate_parser.add_argument("--output", type=Path, required=True)
    validate_parser.add_argument("--config", type=Path)
    validate_parser.add_argument("--flow-results", type=Path)

    export_copy_parser = commands.add_parser(
        "export-copy",
        help=(
            "Flatten every screen's exact-copy contract into one artifact "
            "for locale/i18n catalog diffing."
        ),
    )
    export_copy_parser.add_argument("--bundle", type=Path, required=True)
    export_copy_parser.add_argument("--output", type=Path, required=True)
    export_copy_parser.add_argument(
        "--format", choices=["json", "csv"], default="json"
    )

    propose_slots_parser = commands.add_parser(
        "propose-slots",
        help=(
            "Propose invariant exact-copy values as reviewable data slots "
            "without changing any gate."
        ),
    )
    propose_slots_parser.add_argument("--bundle", type=Path, required=True)
    propose_slots_parser.add_argument("--output", type=Path, required=True)
    propose_slots_parser.add_argument(
        "--format", choices=["json", "md"], default="json"
    )
    propose_slots_parser.add_argument("--min-variants", type=int, default=2)
    propose_slots_parser.add_argument(
        "--max-invariance-ratio",
        type=float,
        default=DEFAULT_MAX_INVARIANCE_RATIO,
    )
    propose_slots_parser.add_argument(
        "--group-similarity",
        type=float,
        default=DEFAULT_GROUP_SIMILARITY,
    )

    propose_reuse_parser = commands.add_parser(
        "propose-reuse",
        help=(
            "Propose existing repository files from exact Figma copy overlap "
            "without applying any candidate."
        ),
    )
    propose_reuse_parser.add_argument("--bundle", type=Path, required=True)
    propose_reuse_parser.add_argument("--repo-index", type=Path, required=True)
    propose_reuse_parser.add_argument("--output", type=Path, required=True)
    propose_reuse_parser.add_argument(
        "--format", choices=["json", "md"], default="json"
    )
    propose_reuse_parser.add_argument(
        "--min-overlap", type=int, default=DEFAULT_MIN_OVERLAP
    )
    propose_reuse_parser.add_argument(
        "--max-source-frequency",
        type=int,
        default=DEFAULT_MAX_SOURCE_FREQUENCY,
        help=(
            "Exclude copy values found in more than this many unique source "
            "files."
        ),
    )

    export_design_parser = commands.add_parser(
        "export-design",
        help=(
            "Write the compiled design as one spec document to hand to an "
            "implementer, with no verification attached."
        ),
    )
    export_design_parser.add_argument("--bundle", type=Path, required=True)
    export_design_parser.add_argument("--output", type=Path, required=True)
    export_design_parser.add_argument(
        "--format", choices=list(FORMATS), default="md"
    )
    export_design_parser.add_argument(
        "--all-properties",
        action="store_true",
        help=(
            "Keep zero spacing and empty effect lists, which the document "
            "omits by default for readability."
        ),
    )
    export_design_parser.add_argument(
        "--exclude",
        help=(
            "Comma-separated layer names to leave out of the spec along with "
            'everything inside them, e.g. "Status Bar,Home Indicator".'
        ),
    )
    export_design_parser.add_argument(
        "--screens",
        help=(
            "Comma-separated Figma node ids to export instead of every screen."
        ),
    )

    serve_parser = commands.add_parser(
        "serve-report", help="Serve a generated report without a build step."
    )
    serve_parser.add_argument("--directory", type=Path, required=True)
    serve_parser.add_argument("--port", type=int, default=4173)

    mode_parser = commands.add_parser(
        "mode",
        help=(
            "Show or change the operating mode. The harness ships in "
            "'extract' (collect/compile/export only); 'verify' unlocks "
            "capture, gates and the enforcement hooks for this directory."
        ),
    )
    mode_group = mode_parser.add_mutually_exclusive_group()
    mode_group.add_argument("--set", choices=MODES, dest="set_mode")
    mode_group.add_argument(
        "--clear",
        action="store_true",
        help="Remove .figma-lossless/mode.json so the default (extract) applies.",
    )
    return parser


def _run(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    # Extraction is open; everything that judges an implementation is locked
    # until verify mode is unlocked on purpose (see mode.py). Refusing here,
    # before any file is touched, keeps a curious `validate` from leaving a
    # half-written report behind.
    mode, _ = resolve_mode()
    if is_locked(args.command, mode):
        print(locked_message(args.command), file=sys.stderr)
        return 1

    if args.command == "mode":
        # Always report the *effective* mode: a file write can be shadowed by
        # FIGMA_LOSSLESS_MODE in this process, and saying "extract" while the
        # process is still in verify would be the lie this command exists to
        # prevent.
        if args.set_mode:
            path = set_mode(args.set_mode)
            mode, source = resolve_mode()
            if mode != args.set_mode:
                print(
                    f"warning: wrote {args.set_mode!r} to {path} but "
                    f"FIGMA_LOSSLESS_MODE={os.environ.get('FIGMA_LOSSLESS_MODE')!r} "
                    "overrides it in this process",
                    file=sys.stderr,
                )
            print(
                json.dumps(
                    {"mode": mode, "source": source, "file": str(path), "fileMode": args.set_mode}
                )
            )
            return 0
        if args.clear:
            removed = clear_mode()
            mode, source = resolve_mode()
            print(json.dumps({"mode": mode, "source": source, "removed": removed}))
            return 0
        mode, source = resolve_mode()
        print(json.dumps({"mode": mode, "source": source}))
        return 0

    if args.command == "compile":
        if args.input is None and args.rest_input is None:
            raise ValueError("compile requires --input and/or --rest-input")
        manifest = DesignCompiler(
            CompileOptions(
                feature_id=args.feature_id,
                input_dir=args.input or args.rest_input,
                output_dir=args.output,
                flow_contract=args.flow_contract,
                component_map=args.component_map,
                data_contract=args.data_contract,
                rest_input=args.rest_input,
                variable_defs=args.variable_defs,
            )
        ).compile()
        print(json.dumps(manifest["counts"], ensure_ascii=False))
        return 0

    if args.command == "snapshot-template":
        snapshot = create_snapshot_template(
            args.bundle,
            args.output,
            use_reference_screenshots=args.use_reference_screenshots,
        )
        print(
            json.dumps(
                {"screens": len(snapshot["screens"]), "path": str(args.output)},
                ensure_ascii=False,
            )
        )
        return 0

    if args.command == "vendor-assets":
        result = vendor_assets(args.bundle, timeout_seconds=args.timeout)
        print(json.dumps(result, ensure_ascii=False))
        return 0 if result["failed"] == 0 else 2

    if args.command == "collect":
        node_ids = (
            [item.strip() for item in args.node_ids.split(",")]
            if args.node_ids
            else []
        )
        manifest = FigmaCollector(
            CollectOptions(
                file_key=args.file_key,
                output_dir=args.output,
                node_ids=node_ids,
                manifest_path=args.manifest,
                token_file=args.token_file,
                include_images=args.include_images,
            )
        ).collect()
        payload = {
            "fileKey": manifest["fileKey"],
            "fileVersion": manifest["fileVersion"],
            "expected": len(manifest["expectedNodeIds"]),
            "collected": len(manifest["collectedNodeIds"]),
            "complete": manifest["complete"],
            "failedNodeIds": [item["id"] for item in manifest["failures"]],
        }
        images_ok = True
        if args.include_images:
            images_ok = bool(manifest.get("imagesComplete"))
            payload["imagesComplete"] = manifest.get("imagesComplete")
            payload["failedImageNodeIds"] = [
                item["id"] for item in manifest.get("imageFailures", [])
            ]
        print(json.dumps(payload, ensure_ascii=False))
        return 0 if manifest["complete"] and images_ok else 2

    if args.command == "validate":
        validator = GateValidator(
            ValidateOptions(
                bundle_dir=args.bundle,
                actual_snapshot=args.actual,
                output_dir=args.output,
                config_path=args.config,
                flow_results_path=args.flow_results,
                actual_snapshot_b=args.actual_b,
            )
        )
        result = validator.validate()
        report_path = render_report(result, args.output)
        coverage_status = result.get("gates", {}).get("coverage", {}).get(
            "status", "NOT_EVALUATED"
        )
        status = (
            "PASS"
            if result["passed"]
            else (
                "INCOMPLETE"
                if coverage_status == "INCOMPLETE"
                and result["summary"]["hardFailures"] == 0
                else "FAIL"
            )
        )
        print(
            json.dumps(
                {
                    "status": status,
                    "coverageStatus": coverage_status,
                    "passed": result["passed"],
                    **result["summary"],
                    "report": str(report_path),
                },
                ensure_ascii=False,
            )
        )
        return 0 if result["passed"] else 2

    if args.command == "export-copy":
        result = export_copy(args.bundle, args.output, format=args.format)
        print(json.dumps(result, ensure_ascii=False))
        return 0

    if args.command == "propose-slots":
        result = propose_slots(
            args.bundle,
            args.output,
            format=args.format,
            min_variants=args.min_variants,
            max_invariance_ratio=args.max_invariance_ratio,
            group_similarity=args.group_similarity,
        )
        print(json.dumps(result, ensure_ascii=False))
        return 0

    if args.command == "propose-reuse":
        result = propose_reuse(
            args.bundle,
            args.repo_index,
            args.output,
            format=args.format,
            min_overlap=args.min_overlap,
            max_source_frequency=args.max_source_frequency,
        )
        print(json.dumps(result, ensure_ascii=False))
        return 0

    if args.command == "export-design":
        result = export_design(
            args.bundle,
            args.output,
            format=args.format,
            all_properties=args.all_properties,
            exclude_names=(
                [item.strip() for item in args.exclude.split(",") if item.strip()]
                if args.exclude
                else None
            ),
            screen_ids=(
                [item.strip() for item in args.screens.split(",") if item.strip()]
                if args.screens
                else None
            ),
        )
        print(json.dumps(result, ensure_ascii=False))
        return 0

    if args.command == "serve-report":
        directory = args.directory.resolve()
        os.chdir(directory)
        handler = http.server.SimpleHTTPRequestHandler
        with socketserver.TCPServer(("127.0.0.1", args.port), handler) as server:
            print(f"Serving {directory} at http://127.0.0.1:{args.port}/report.html")
            try:
                server.serve_forever()
            except KeyboardInterrupt:
                pass
        return 0

    return 1


def main(argv: list[str] | None = None) -> int:
    try:
        return _run(argv)
    except (ValueError, OSError, json.JSONDecodeError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
