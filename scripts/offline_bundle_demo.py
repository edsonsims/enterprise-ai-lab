"""Install a trusted core wheel in a macOS process with networking denied.

The process shares the host filesystem/runtime. This is an observed process
network boundary, not a container, virtual machine or physical air gap.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import platform
import socket
import subprocess
import sys
import time
import venv


ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / "src/enterprise_ai_lab/offline_bundle.py"
PROFILE = "(version 1) (allow default) (deny network*)"


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def prepare_parent(path):
    """Allow new nested paths, while refusing symlinked parent components."""
    for parent in reversed(tuple(path.parents)):
        if os.path.lexists(parent):
            if parent.is_symlink() or not parent.is_dir():
                raise RuntimeError("unsafe_output_parent")
        else:
            parent.mkdir(mode=0o700)


def bundle_module():
    spec = importlib.util.spec_from_file_location("trusted_bundle_verifier", MODULE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run(command, cwd, timeout=120):
    result = subprocess.run(command, cwd=cwd, capture_output=True, text=True,
                            timeout=timeout, check=False)
    if result.returncode:
        raise RuntimeError(f"child_failed:{result.returncode}:{result.stderr[-1200:]}")
    return result.stdout


def denied_probe(kind, port):
    """Exercise numeric loopback addresses; do not infer denial from timeout."""
    sock = None
    stage = "socket"
    try:
        sock = socket.socket(socket.AF_INET, kind)
        sock.settimeout(2)
        stage = "connect" if kind == socket.SOCK_STREAM else "sendto"
        if kind == socket.SOCK_STREAM:
            sock.connect(("127.0.0.1", port))
            sock.sendall(b"unexpected-network-access")
        else:
            sock.sendto(b"unexpected-network-access", ("127.0.0.1", port))
    except OSError as exc:
        # macOS Seatbelt returns EPERM/EACCES. A timeout/refused connection is
        # insufficient evidence of the configured denial.
        if exc.errno not in (1, 13):
            raise RuntimeError("network_failure_was_not_permission_denial") from exc
        return {"family": "IPv4", "transport": "TCP" if kind == socket.SOCK_STREAM else "UDP",
                "stage": stage, "errno": exc.errno, "permission_denied": True}
    finally:
        if sock is not None:
            sock.close()
    raise RuntimeError("network_access_was_not_denied")


def worker(args):
    work = Path(args.work_dir).resolve(strict=True)
    bundle = Path(args.bundle).resolve(strict=True)
    verifier = bundle_module()
    checked = verifier.verify_bundle(bundle, args.digest)
    # Execute the copied stdlib verifier as a separate isolated Python process.
    run([sys.executable, "-I", str(bundle / "verify_bundle.py"), "verify",
         "--bundle", str(bundle), "--expected-manifest-sha256", args.digest], work)
    probes = [denied_probe(socket.SOCK_STREAM, args.tcp_port),
              denied_probe(socket.SOCK_DGRAM, args.udp_port)]
    descendant_code = (
        "import json,socket; s=None\n"
        "try:\n s=socket.socket(); s.connect(('127.0.0.1'," + str(args.tcp_port) + "))\n"
        "except OSError as e:\n print(json.dumps({'errno':e.errno})); raise SystemExit(0 if e.errno in (1,13) else 3)\n"
        "finally:\n if s is not None: s.close()\n"
        "raise SystemExit(4)\n"
    )
    descendant = json.loads(run([sys.executable, "-I", "-c", descendant_code], work))
    env_dir = work / "runtime"
    if env_dir.exists():
        raise RuntimeError("runtime_already_exists")
    venv.EnvBuilder(with_pip=True, system_site_packages=False).create(env_dir)
    python = env_dir / "bin/python"
    # Recheck the independently supplied manifest digest directly before install.
    checked = verifier.verify_bundle(bundle, args.digest)
    wheel = bundle / checked["wheel_relative_path"]
    installed = run([str(python), "-I", "-m", "pip", "--isolated", "install",
                     "--no-index", "--no-deps", "--no-cache-dir",
                     "--disable-pip-version-check", str(wheel)], work)
    inventory_code = (
        "import importlib.metadata,json,pathlib,sys,enterprise_ai_lab;"
        "d=importlib.metadata.distribution('enterprise-ai-lab');"
        "print(json.dumps({'version':d.version,'prefix':sys.prefix,"
        "'package':str(pathlib.Path(enterprise_ai_lab.__file__).resolve()),"
        "'distributions':sorted(x.metadata['Name'] for x in importlib.metadata.distributions())}))"
    )
    inventory = json.loads(run([str(python), "-I", "-c", inventory_code], work))
    if not Path(inventory["package"]).is_relative_to(env_dir):
        raise RuntimeError("import_did_not_come_from_fresh_runtime")
    if inventory["version"] != checked["manifest"]["project"]["version"]:
        raise RuntimeError("installed_version_mismatch")
    result = json.loads(run([str(python), "-I", "-m", "enterprise_ai_lab", "demo",
                             "--output", str(work / "demo")], work))
    if result["safe_query_leakage"] != 0 or result["unsafe_query_leakage"] <= 0:
        raise RuntimeError("unexpected_original_demo_result")
    reports = {}
    for path in sorted((work / "demo").rglob("*")):
        if path.is_file():
            reports[str(path.relative_to(work / "demo"))] = sha(path)
    return {"network_probes": probes, "descendant_network_errno": descendant["errno"],
            "fresh_runtime": True, "system_site_packages": False,
            "installed_from_bundle": True, "import_inside_fresh_runtime": True,
            "installed_project_version": inventory["version"],
            "runtime_distributions": inventory["distributions"],
            "pip_stdout_sha256": hashlib.sha256(installed.encode()).hexdigest(),
            "demo": {k: v for k, v in result.items() if k != "output"},
            "demo_file_sha256": reports}


def demonstrate(args):
    if platform.system() != "Darwin" or not Path("/usr/bin/sandbox-exec").is_file():
        raise RuntimeError("macos_sandbox_exec_required")
    work = Path(args.work_dir).absolute()
    output = Path(args.out).absolute()
    if os.path.lexists(work) or os.path.lexists(output):
        raise RuntimeError("output_already_exists")
    if output.is_relative_to(work):
        raise RuntimeError("report_must_be_outside_work_directory")
    prepare_parent(work)
    prepare_parent(output)
    work.mkdir(mode=0o700)
    start = time.monotonic()
    verifier = bundle_module()
    created = verifier.create_bundle(Path(args.wheel), work / "bundle")
    repeated = verifier.create_bundle(Path(args.wheel), work / "bundle-repeat")
    if created != repeated:
        raise RuntimeError("same_input_bundle_was_not_repeatable")
    runtime_work = work / "isolated-install"
    runtime_work.mkdir(mode=0o700)
    with socket.socket() as tcp, socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp:
        tcp.bind(("127.0.0.1", 0))
        tcp.listen()
        tcp.settimeout(2)
        udp.bind(("127.0.0.1", 0))
        udp.settimeout(2)
        tcp_port, udp_port = tcp.getsockname()[1], udp.getsockname()[1]
        with socket.create_connection(("127.0.0.1", tcp_port), timeout=2) as sender:
            sender.sendall(b"positive-control")
        connection, _ = tcp.accept()
        with connection:
            if connection.recv(100) != b"positive-control":
                raise RuntimeError("tcp_control_failed")
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sender:
            sender.sendto(b"positive-control", ("127.0.0.1", udp_port))
        if udp.recv(100) != b"positive-control":
            raise RuntimeError("udp_control_failed")
        command = ["/usr/bin/sandbox-exec", "-p", PROFILE, sys.executable, "-I",
                   str(Path(__file__).resolve()), "--worker", "--bundle", str(work / "bundle"),
                   "--digest", created["manifest_sha256"], "--work-dir", str(runtime_work),
                   "--tcp-port", str(tcp_port), "--udp-port", str(udp_port)]
        child = json.loads(run(command, runtime_work, timeout=180))
        for listener in (tcp, udp):
            listener.settimeout(0.05)
            try:
                if listener is tcp:
                    unexpected, _ = tcp.accept()
                    unexpected.close()
                else:
                    udp.recv(100)
            except TimeoutError:
                continue
            raise RuntimeError("unexpected_network_delivery")
    report = {"schema_version": 1, "study": "core-wheel-process-network-denial",
              "recorded_at": datetime.now(timezone.utc).isoformat(),
              "platform": platform.platform(), "python": platform.python_version(),
              "network_policy": PROFILE, "network_control": {"TCP": "delivered", "UDP": "delivered"},
              "no_sandbox_delivery_observed": True,
              "same_frozen_wheel_repeat_manifest": True,
              "manifest_sha256": created["manifest_sha256"], "manifest": created["manifest"],
              "source_sha256": {"scripts/offline_bundle_demo.py": sha(__file__),
                                "src/enterprise_ai_lab/offline_bundle.py": sha(MODULE)},
              "observations": child, "elapsed_seconds": round(time.monotonic() - start, 6),
              "scope": {"core_lexical_demo": True, "optional_integrations_exercised": False,
                        "container_image_exercised": False, "filesystem_isolated": False,
                        "physical_air_gap": False, "human_review_performed": False}}
    with output.open("x") as fd:
        os.chmod(output, 0o600)
        json.dump(report, fd, indent=2, sort_keys=True)
        fd.write("\n")
    return {"report": str(output), "manifest_sha256": created["manifest_sha256"],
            "verified": True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wheel")
    parser.add_argument("--work-dir", required=True)
    parser.add_argument("--out")
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--bundle", help=argparse.SUPPRESS)
    parser.add_argument("--digest", help=argparse.SUPPRESS)
    parser.add_argument("--tcp-port", type=int, help=argparse.SUPPRESS)
    parser.add_argument("--udp-port", type=int, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if not args.worker and (not args.wheel or not args.out):
        parser.error("--wheel and --out are required")
    print(json.dumps(worker(args) if args.worker else demonstrate(args), sort_keys=True))


if __name__ == "__main__":
    main()
