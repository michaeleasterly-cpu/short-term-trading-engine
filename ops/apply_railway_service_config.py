"""Apply service-instance settings from ``railway.json`` via Railway's GraphQL API.

Why this exists
---------------
Railway reads ``railway.json`` on each build and uses ``build.buildCommand`` /
``deploy.startCommand`` correctly. But fields like ``deploy.cronSchedule``,
``deploy.restartPolicyType``, and the GitHub source link are stored at the
**service-instance** level — they are NOT auto-populated from ``railway.json``
on either ``railway up`` (local upload) or a fresh GitHub-source build. They
have to be set explicitly via API or in the dashboard.

This script reads ``railway.json`` from the current working directory and
applies its ``deploy.cronSchedule``, ``deploy.restartPolicyType``, and
``source.repo`` (when given) to the named service via the
``serviceInstanceUpdate`` mutation. Idempotent — safe to re-run after any
config edit, accidental dashboard tweak, or service rebuild.

Usage
-----
::

    # Reads token from ~/.railway/config.json. Service + env IDs from flags.
    python ops/apply_railway_service_config.py \\
        --service e6a06855-f65f-427a-9874-f714aaaf6e30 \\
        --environment 685d532e-6301-429d-a099-cc16b33480bf \\
        --repo michaeleasterly-cpu/short-term-trading-engine

The repo flag is optional — omit it to leave the source link untouched.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys
import urllib.error
import urllib.request


GRAPHQL_URL = "https://backboard.railway.com/graphql/v2"
MUTATION = """
mutation($svc: String!, $env: String!, $input: ServiceInstanceUpdateInput!) {
  serviceInstanceUpdate(serviceId: $svc, environmentId: $env, input: $input)
}
""".strip()


def _load_token() -> str:
    """Read the Railway access token from ``~/.railway/config.json``."""
    cfg = pathlib.Path.home() / ".railway" / "config.json"
    if not cfg.exists():
        sys.exit(f"no Railway CLI config at {cfg}; run `railway login` first")
    data = json.loads(cfg.read_text())
    token = (data.get("user") or {}).get("accessToken")
    if not token:
        sys.exit("no accessToken in ~/.railway/config.json; run `railway login`")
    return token


def _load_railway_json(path: pathlib.Path) -> dict:
    if not path.exists():
        sys.exit(f"railway.json not found at {path}")
    return json.loads(path.read_text())


def _post(token: str, payload: dict) -> dict:
    req = urllib.request.Request(
        GRAPHQL_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            # Cloudflare in front of backboard.railway.com 403s the default
            # urllib UA; identify as a Railway client.
            "User-Agent": "ste-ops-apply-railway-service-config/1.0",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        sys.exit(f"HTTP {exc.code}: {exc.read().decode('utf-8', errors='replace')}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--service", required=True, help="Railway service ID")
    parser.add_argument("--environment", required=True, help="Railway environment ID")
    parser.add_argument(
        "--repo",
        default=None,
        help='Optional "owner/repo" to set as the GitHub source.',
    )
    parser.add_argument(
        "--config",
        default="railway.json",
        help="Path to railway.json (default: railway.json in cwd).",
    )
    args = parser.parse_args()

    config = _load_railway_json(pathlib.Path(args.config))
    deploy = config.get("deploy") or {}
    cron = deploy.get("cronSchedule")
    restart = deploy.get("restartPolicyType")
    if cron is None or restart is None:
        sys.exit(
            f"deploy.cronSchedule and deploy.restartPolicyType must both be set in {args.config}"
        )

    payload_input: dict = {"cronSchedule": cron, "restartPolicyType": restart}
    if args.repo:
        payload_input["source"] = {"repo": args.repo}

    token = _load_token()
    result = _post(
        token,
        {
            "query": MUTATION,
            "variables": {
                "svc": args.service,
                "env": args.environment,
                "input": payload_input,
            },
        },
    )
    if result.get("errors"):
        sys.exit(json.dumps(result["errors"], indent=2))
    if not result.get("data", {}).get("serviceInstanceUpdate"):
        sys.exit(f"unexpected response: {json.dumps(result, indent=2)}")

    print(
        f"applied: cronSchedule={cron!r} restartPolicyType={restart!r}"
        + (f" source.repo={args.repo!r}" if args.repo else "")
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
