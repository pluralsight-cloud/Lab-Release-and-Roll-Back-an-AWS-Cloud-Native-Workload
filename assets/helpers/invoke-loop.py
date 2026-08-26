#!/usr/bin/env python3
import argparse
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path


EXPECTED_V1 = {"order_id": "order-1001", "status": "confirmed", "version": "v1"}
EXPECTED_V2_ERROR = {
    "errorMessage": "Simulated v2 order-processing failure.",
    "errorType": "RuntimeError",
}


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--count", type=int, default=60)
    parser.add_argument("--interval", type=float, default=2.0)
    parser.add_argument(
        "--evidence-file",
        type=Path,
        default=Path("/home/cloud_user/lab/output/invoke-loop.jsonl"),
    )
    args = parser.parse_args()
    if args.count < 1:
        parser.error("--count must be at least 1")
    if args.interval < 0:
        parser.error("--interval cannot be negative")
    return args


def main():
    args = parse_args()
    args.evidence_file.parent.mkdir(parents=True, exist_ok=True)
    probability = 1 - 0.9**args.count
    print(
        f"Sampling {args.count} invocations from globomantics-orders:prod "
        f"every {args.interval:g}s "
        f"({probability:.2%} chance of >=1 v2 at 10% weight)."
    )

    counts = {"1": 0, "2": 0, "other": 0, "function_errors": 0}
    unexpected = False

    with tempfile.TemporaryDirectory() as temporary_directory, args.evidence_file.open(
        "w", encoding="utf-8"
    ) as evidence:
        payload_path = Path(temporary_directory) / "payload.json"
        for invocation in range(1, args.count + 1):
            result = subprocess.run(
                [
                    "aws",
                    "lambda",
                    "invoke",
                    "--function-name",
                    "globomantics-orders",
                    "--qualifier",
                    "prod",
                    "--payload",
                    "{}",
                    "--cli-binary-format",
                    "raw-in-base64-out",
                    "--output",
                    "json",
                    "--no-cli-pager",
                    str(payload_path),
                ],
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                print(
                    f"AWS CLI invocation {invocation} failed: {result.stderr.strip()}",
                    file=sys.stderr,
                )
                return 1

            try:
                metadata = json.loads(result.stdout)
                payload = json.loads(payload_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError) as error:
                print(f"Invocation {invocation} returned invalid JSON: {error}", file=sys.stderr)
                return 1
            if not isinstance(metadata, dict) or not isinstance(payload, dict):
                print(
                    f"Invocation {invocation} returned malformed metadata or payload.",
                    file=sys.stderr,
                )
                return 1

            version = metadata.get("ExecutedVersion", "unknown")
            function_error = metadata.get("FunctionError")
            counts[version if version in ("1", "2") else "other"] += 1
            if function_error is not None:
                counts["function_errors"] += 1

            record = {
                "cli_exit_code": result.returncode,
                "executed_version": version,
                "function_error": function_error,
                "invocation": invocation,
                "payload": payload,
            }
            evidence.write(json.dumps(record, separators=(",", ":"), sort_keys=True) + "\n")
            evidence.flush()

            if function_error is not None:
                error_type = payload.get("errorType", "unknown")
                error_message = payload.get("errorMessage", "no message")
                print(
                    f"v{version} failure {invocation:03d}: "
                    f"FunctionError={function_error} ExecutedVersion={version} "
                    f"{error_type}: {error_message}"
                )

            if version == "1" and function_error is not None:
                print(
                    f"Unexpected result: v1 invocation {invocation} returned FunctionError",
                    file=sys.stderr,
                )
                unexpected = True
            if version == "1" and payload != EXPECTED_V1:
                print(
                    f"Unexpected v1 payload for invocation {invocation}.",
                    file=sys.stderr,
                )
                unexpected = True
            if version == "2" and function_error is None:
                print(
                    f"Unexpected result: v2 invocation {invocation} did not return FunctionError",
                    file=sys.stderr,
                )
                unexpected = True
            if version == "2" and any(
                payload.get(field) != expected
                for field, expected in EXPECTED_V2_ERROR.items()
            ):
                print(
                    f"Unexpected v2 error payload for invocation {invocation}.",
                    file=sys.stderr,
                )
                unexpected = True
            if version not in ("1", "2"):
                print(
                    f"Unexpected result: invocation {invocation} executed version {version}",
                    file=sys.stderr,
                )
                unexpected = True

            if invocation % 10 == 0 or invocation == args.count:
                print(
                    f"Progress {invocation:03d}/{args.count:03d}: "
                    f"v1={counts['1']} v2={counts['2']} "
                    f"errors={counts['function_errors']} other={counts['other']}"
                )

            if invocation < args.count and args.interval:
                time.sleep(args.interval)

    print(
        f"Summary: total={args.count} v1={counts['1']} v2={counts['2']} "
        f"function_errors={counts['function_errors']} other={counts['other']} "
        f"evidence={args.evidence_file}"
    )
    if unexpected:
        return 1
    if counts["2"] == 0:
        print(
            "No failing v2 response was sampled; run the helper again.",
            file=sys.stderr,
        )
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
