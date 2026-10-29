"""Inspect or remove only empty, unreferenced ScopeBench networks; no daemon restart."""

# ruff: noqa: INP001, T201 -- standalone repair CLI
import argparse
import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def docker(*args: str) -> str:
    """Run a bounded Docker command without a shell; never force network removal."""
    return subprocess.check_output(  # noqa: S603 - fixed executable and structured arguments
        ["docker", *args],  # noqa: S607 - fixed Docker executable
        text=True,
        timeout=30,
        stderr=subprocess.STDOUT,
    )


def snapshot() -> tuple[list, list, set]:
    """Collect network/container references plus current ledger workspace project names."""
    network_ids = docker("network", "ls", "-q").split()
    networks = json.loads(docker("network", "inspect", *network_ids))
    container_ids = docker("ps", "-aq").split()
    containers = json.loads(docker("inspect", *container_ids)) if container_ids else []
    ledger = json.loads(
        (ROOT / "experiments/judge-information-full-suite-20261022/ledger.json").read_text()
    )
    protected = set()
    for record in ledger["runs"].values():
        if record["status"] == "running" and record.get("evidence_dir"):
            protected.update(
                p.name for p in Path(record["evidence_dir"]).glob("workspaces/*/*") if p.is_dir()
            )
    return networks, containers, protected


def eligible(network: dict, containers: list, protected: set) -> bool:
    """Exclude active units, endpoint attachments, and stopped-container references."""
    labels = network.get("Labels") or {}
    project = labels.get("com.docker.compose.project", "")
    if not network["Name"].startswith("sb-") or not project.startswith("sb-"):
        return False
    if project in protected or network.get("Containers"):
        return False
    for container in containers:
        refs = container.get("NetworkSettings", {}).get("Networks") or {}
        mode = container.get("HostConfig", {}).get("NetworkMode")
        container_project = (container.get("Config", {}).get("Labels") or {}).get(
            "com.docker.compose.project"
        )
        if container_project == project:
            return False
        if network["Name"] in refs or mode in (network["Name"], network["Id"]):
            return False
    return True


def main() -> None:
    """Default to inspection; --apply rechecks ownership/references before every removal."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    networks, containers, protected = snapshot()
    candidates = [n for n in networks if eligible(n, containers, protected)]
    receipt = {
        "observed_at_utc": datetime.now(UTC).isoformat(),
        "apply": args.apply,
        "candidates": candidates,
        "removed": [],
        "skipped": [],
    }
    for network in candidates:
        print(network["Name"])
        if not args.apply:
            continue
        current, containers, protected = snapshot()
        refreshed = next((n for n in current if n["Id"] == network["Id"]), None)
        if refreshed is None or not eligible(refreshed, containers, protected):
            receipt["skipped"].append(network["Name"])
            continue
        try:
            docker("network", "rm", network["Id"])
            receipt["removed"].append(network["Name"])
        except subprocess.CalledProcessError as exc:
            receipt["skipped"].append({"network": network["Name"], "reason": exc.output})
    out = (
        ROOT
        / ".scopebench-runs"
        / ("network-repair-" + datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ") + ".json")
    )
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(receipt, indent=2) + "\n")
    print(f"Candidates: {len(candidates)}; removed: {len(receipt['removed'])}; receipt: {out}")


if __name__ == "__main__":
    main()
