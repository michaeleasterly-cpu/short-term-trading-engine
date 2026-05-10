"""Apply service-instance settings from ``railway.json`` via Railway's GraphQL API.

Why this exists
---------------
Railway reads ``railway.json`` on each build and uses
``build.buildCommand`` correctly. But fields like ``deploy.cronSchedule``,
``deploy.restartPolicyType``, ``deploy.startCommand``, and the GitHub
source link are stored at the **service-instance** level — they are NOT
auto-populated from ``railway.json`` on either ``railway up`` (local
upload) or a fresh GitHub-source build. They have to be set explicitly
via API or the dashboard.

This script reads ``railway.json`` from the current working directory and
applies the deploy block to one or more services via the
``serviceInstanceUpdate`` mutation. Idempotent — safe to re-run after any
config edit, accidental dashboard tweak, or service rebuild.

Two ``railway.json`` shapes are supported:

* **Flat** (single service): ``deploy: { startCommand, cronSchedule, ... }``.
  Pass ``--service`` and ``--environment`` to apply to a specific instance.
* **Nested** (multi-service manifest): ``deploy: { <service-name>: { ... }, ... }``.
  Pass ``--all`` to apply each block to its corresponding service. The
  script resolves service names → IDs via ``project.services``.

The nested shape is documentation-only as far as Railway is concerned
(the platform schema doesn't recognize it), but it's a clean place to
keep the multi-service manifest in version control.

Usage
-----
::

    # Single service (flat shape):
    python ops/apply_railway_service_config.py \\
        --service e6a06855-... --environment 685d532e-...

    # All services declared in railway.json (nested shape):
    python ops/apply_railway_service_config.py --all --project 22ee9c6f-...
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
LIST_SERVICES_QUERY = """
query($id: String!) {
  project(id: $id) {
    environments { edges { node { id name } } }
    services { edges { node { id name } } }
  }
}
""".strip()


def _load_token() -> str:
    """Resolve a Railway API token from (in order) env, then CLI config.

    ``RAILWAY_API_TOKEN`` and ``RAILWAY_TOKEN`` are the names the Railway
    CLI also honors for non-interactive use; ``RAILWAY_API_KEY`` is what
    this repo's ``.env`` happens to be named.
    """
    import os

    for env in ("RAILWAY_API_TOKEN", "RAILWAY_TOKEN", "RAILWAY_API_KEY"):
        token = os.getenv(env)
        if token:
            return token
    cfg = pathlib.Path.home() / ".railway" / "config.json"
    if not cfg.exists():
        sys.exit(
            "no RAILWAY_API_TOKEN/RAILWAY_TOKEN/RAILWAY_API_KEY in env and no "
            f"Railway CLI config at {cfg}; run `railway login` or set the env var"
        )
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
            "User-Agent": "ste-ops-apply-railway-service-config/2.0",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        sys.exit(f"HTTP {exc.code}: {exc.read().decode('utf-8', errors='replace')}")


def _input_from_block(block: dict) -> dict:
    cron = block.get("cronSchedule")
    restart = block.get("restartPolicyType")
    start_cmd = block.get("startCommand")
    if cron is None or restart is None:
        sys.exit("each deploy block must include cronSchedule + restartPolicyType")
    payload: dict = {"cronSchedule": cron, "restartPolicyType": restart}
    if start_cmd:
        # serviceInstanceUpdate accepts startCommand on the service-instance,
        # which is what causes Railway to actually run the named entrypoint.
        payload["startCommand"] = start_cmd
    src = block.get("source")
    if src and src.get("repo"):
        payload["source"] = {"repo": src["repo"]}
    # watchPatterns: glob patterns that gate rebuilds on push. Without this,
    # every push to the linked branch rebuilds the service even if no runtime
    # code changed (docs, markdown, JSON outputs). Joined with "\n" because
    # Railway's serviceInstanceUpdate accepts watchPatterns as a single
    # newline-separated string, not a JSON array.
    patterns = block.get("watchPatterns")
    if patterns:
        if not isinstance(patterns, list):
            sys.exit("watchPatterns must be a list of glob strings")
        payload["watchPatterns"] = "\n".join(patterns)
    return payload


def _apply(token: str, *, service_id: str, environment_id: str, payload_input: dict) -> None:
    result = _post(
        token,
        {
            "query": MUTATION,
            "variables": {"svc": service_id, "env": environment_id, "input": payload_input},
        },
    )
    if result.get("errors"):
        sys.exit(json.dumps(result["errors"], indent=2))
    if not result.get("data", {}).get("serviceInstanceUpdate"):
        sys.exit(f"unexpected response: {json.dumps(result, indent=2)}")


def _resolve_project(token: str, project_id: str) -> tuple[dict[str, str], dict[str, str]]:
    """Return ``(service_name → service_id, env_name → env_id)``."""
    result = _post(
        token, {"query": LIST_SERVICES_QUERY, "variables": {"id": project_id}}
    )
    if result.get("errors"):
        sys.exit(json.dumps(result["errors"], indent=2))
    project = result["data"]["project"]
    services = {
        edge["node"]["name"]: edge["node"]["id"]
        for edge in project["services"]["edges"]
    }
    envs = {
        edge["node"]["name"]: edge["node"]["id"]
        for edge in project["environments"]["edges"]
    }
    return services, envs


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--service", help="Railway service ID (flat shape).")
    parser.add_argument("--environment", help="Railway environment ID (flat shape).")
    parser.add_argument(
        "--all",
        action="store_true",
        help="Apply each nested deploy block to its named service.",
    )
    parser.add_argument(
        "--project",
        help="Railway project ID — required with --all to resolve service names.",
    )
    parser.add_argument(
        "--env-name",
        default="production",
        help="Environment name to target with --all (default: production).",
    )
    parser.add_argument(
        "--repo",
        default=None,
        help='Optional "owner/repo" override (flat shape only). Nested blocks '
        "carry their own source.repo.",
    )
    parser.add_argument(
        "--config",
        default="railway.json",
        help="Path to railway.json (default: railway.json in cwd).",
    )
    args = parser.parse_args()

    config = _load_railway_json(pathlib.Path(args.config))
    deploy = config.get("deploy") or {}
    nested = deploy and all(isinstance(v, dict) for v in deploy.values())

    token = _load_token()

    if args.all:
        if not nested:
            sys.exit("--all requires a nested deploy.<service> shape in railway.json")
        if not args.project:
            sys.exit("--all requires --project")
        services, envs = _resolve_project(token, args.project)
        if args.env_name not in envs:
            sys.exit(
                f"environment {args.env_name!r} not found in project; have: {list(envs)}"
            )
        env_id = envs[args.env_name]
        applied: list[str] = []
        for service_name, block in deploy.items():
            if service_name not in services:
                print(
                    f"  skipping {service_name!r}: service does not exist in project; "
                    "create it with `railway add --service`",
                    file=sys.stderr,
                )
                continue
            payload = _input_from_block(block)
            _apply(
                token,
                service_id=services[service_name],
                environment_id=env_id,
                payload_input=payload,
            )
            applied.append(service_name)
            print(
                f"applied {service_name!r}: cronSchedule={payload.get('cronSchedule')!r} "
                f"restartPolicyType={payload.get('restartPolicyType')!r} "
                f"startCommand={payload.get('startCommand')!r}"
            )
        if not applied:
            sys.exit("no services applied — check service names in railway.json")
        return 0

    # Flat shape (single-service) path.
    if not args.service or not args.environment:
        sys.exit(
            "flat-shape mode requires --service and --environment "
            "(or pass --all with --project for nested shape)"
        )
    if nested:
        sys.exit(
            "railway.json uses the nested-services shape; either pass --all "
            "or downshift railway.json to a flat deploy block."
        )
    payload = _input_from_block(deploy)
    if args.repo:
        payload["source"] = {"repo": args.repo}
    _apply(
        token,
        service_id=args.service,
        environment_id=args.environment,
        payload_input=payload,
    )
    print(
        f"applied: cronSchedule={payload.get('cronSchedule')!r} "
        f"restartPolicyType={payload.get('restartPolicyType')!r} "
        + (f"source.repo={payload['source']['repo']!r}" if "source" in payload else "")
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
