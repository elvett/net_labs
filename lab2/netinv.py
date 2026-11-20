#!/usr/bin/env python3
import argparse
import csv
import json
import re
import shlex
import sys
import time
from datetime import datetime, timezone

import docker
import paramiko
import requests

LABEL_PROJECT = "com.docker.compose.project"
LABEL_SERVICE = "com.docker.compose.service"
SSH_TIMEOUT = 10                      # seconds
HTTP_TIMEOUT = 5                      # seconds
COUNTER_REGEX = r"Counter:\s*(\d+)"   # how the app prints the visit counter
FIELDS = ("service", "image", "state", "started_at", "networks", "ports")


# ------------------------------------------------------------------ helpers
def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def norm_time(s):
    """CLI and API return StartedAt with nanoseconds: normalise to microseconds."""
    m = re.match(r"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.(\d+))?", s or "")
    if not m:
        return s or ""
    return f"{m.group(1)}.{(m.group(2) or '0')[:6].ljust(6, '0')}Z"


def norm_ports_api(ports):
    """{'80/tcp': [{'HostIp': '0.0.0.0', 'HostPort': '8080'}]} -> ['0.0.0.0:8080->80/tcp']"""
    res = []
    for cport, binds in (ports or {}).items():
        for b in binds or []:
            res.append(f"{b.get('HostIp') or '0.0.0.0'}:{b['HostPort']}->{cport}")
    return sorted(res)


def norm_ports_cli(text):
    """`docker port` line '80/tcp -> 0.0.0.0:8080' -> '0.0.0.0:8080->80/tcp'."""
    res = []
    for line in text.splitlines():
        m = re.match(r"^(\d+/\w+)\s+->\s+(.+):(\d+)$", line.strip())
        if m:
            res.append(f"{m.group(2).strip('[]')}:{m.group(3)}->{m.group(1)}")
    return sorted(res)


# ---------------------------------------------------------------------- SSH
def ssh_connect(args):
    ssh = paramiko.SSHClient()
    ssh.load_system_host_keys()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    # No password in code: key file (--key) or ssh-agent only.
    ssh.connect(args.host, username=args.user, key_filename=args.key,
                timeout=SSH_TIMEOUT, banner_timeout=SSH_TIMEOUT, auth_timeout=SSH_TIMEOUT)
    return ssh


def ssh_run(ssh, cmd):
    _, out, err = ssh.exec_command(cmd, timeout=SSH_TIMEOUT)
    text = out.read().decode().strip()
    error = err.read().decode().strip()
    code = out.channel.recv_exit_status()
    if code != 0:
        raise RuntimeError(f"command `{cmd}` exited with code {code}: {error}")
    return text


def show_environment(ssh, project):
    print("== Versions ==")
    print("Python        :", sys.version.split()[0])
    print("Docker Engine :", ssh_run(ssh, "docker version --format '{{.Server.Version}}'"))
    print("Docker Compose:", ssh_run(ssh, "docker compose version --short"))
    print("Docker SDK    :", docker.__version__)
    print("\n== docker info (summary) ==")
    print(ssh_run(ssh, "docker info --format 'Server version: {{.ServerVersion}} | "
                       "OS: {{.OperatingSystem}} | Containers: {{.Containers}}'"))
    print("\n== docker compose ps ==")
    print(ssh_run(ssh, f"docker compose -p {shlex.quote(project)} ps"))


# ------------------------------------------------------------ collection: CLI
def collect_cli(ssh, project):
    ps = ssh_run(
        ssh,
        f"docker ps -a --no-trunc --filter label={LABEL_PROJECT}={shlex.quote(project)} "
        "--format '{{.ID}} {{.Image}} {{.State}}'",
    )
    containers = []
    for line in ps.splitlines():
        cid, image, state = line.split()
        service, started = ssh_run(
            ssh,
            "docker inspect --format '{{index .Config.Labels \"" + LABEL_SERVICE + "\"}} "
            "{{.State.StartedAt}}' " + cid,
        ).split()
        nets_raw = ssh_run(
            ssh,
            "docker inspect --format '{{range $name, $network := .NetworkSettings.Networks}}"
            "{{println $name $network.IPAddress}}{{end}}' " + cid,
        )
        networks = {}
        for n in nets_raw.splitlines():
            parts = n.split()
            if parts:
                networks[parts[0]] = parts[1] if len(parts) > 1 else ""
        try:
            ports = norm_ports_cli(ssh_run(ssh, f"docker port {cid}"))
        except RuntimeError:  # e.g. a stopped container has no ports
            ports = []
        containers.append({
            "service": service, "id": cid, "image": image, "state": state,
            "started_at": norm_time(started), "networks": networks, "ports": ports,
        })
    return {
        "host": ssh_run(ssh, "hostname"), "project": project,
        "collected_at": now_iso(), "source": "cli",
        "containers": sorted(containers, key=lambda x: x["service"]),
    }


# ------------------------------------------------------------ collection: API
def docker_client(args):
    return docker.DockerClient(base_url=f"ssh://{args.user}@{args.host}",
                               use_ssh_client=False, timeout=15)


def collect_api(client, project):
    containers = []
    for c in client.containers.list(all=True, filters={"label": f"{LABEL_PROJECT}={project}"}):
        c.reload()
        at = c.attrs
        containers.append({
            "service": c.labels[LABEL_SERVICE],
            "id": c.id,
            "image": at["Config"]["Image"],
            "state": at["State"]["Status"],
            "started_at": norm_time(at["State"]["StartedAt"]),
            "networks": {n: v["IPAddress"] for n, v in at["NetworkSettings"]["Networks"].items()},
            "ports": norm_ports_api(at["NetworkSettings"]["Ports"]),
        })
    return {
        "host": client.info()["Name"], "project": project,
        "collected_at": now_iso(), "source": "api",
        "containers": sorted(containers, key=lambda x: x["service"]),
    }


# ------------------------------------------------------------------- checks
def network_table(snap):
    table = {}
    for c in snap["containers"]:
        for net, ip in c["networks"].items():
            table.setdefault(net, []).append(f"{c['service']}({ip or '-'})")
    return table


def check_ports(snap, proxy_service):
    """Only the web proxy may publish ports to the host."""
    violations = []
    proxy_found = False
    for c in snap["containers"]:
        if c["service"] == proxy_service:
            proxy_found = True
            if not c["ports"]:
                violations.append(f"{c['service']}: the proxy publishes no ports")
        elif c["ports"]:
            violations.append(f"{c['service']}: published ports {', '.join(c['ports'])}")
    if not proxy_found:
        violations.append(f"proxy service '{proxy_service}' not found in the project")
    return violations


def http_probe(url):
    try:
        r = requests.get(url, timeout=HTTP_TIMEOUT)
        return {"status": r.status_code, "time": round(r.elapsed.total_seconds(), 3),
                "body": r.text, "error": ""}
    except requests.RequestException as e:
        return {"status": "ERROR", "time": "", "body": "", "error": type(e).__name__}


def get_counter(body):
    m = re.search(COUNTER_REGEX, body or "")
    return int(m.group(1)) if m else None


def compare(cli, api):
    """Match containers by full ID. Returns {id: [differences]}."""
    c = {x["id"]: x for x in cli["containers"]}
    p = {x["id"]: x for x in api["containers"]}
    diffs = {}
    for cid in sorted(c.keys() | p.keys()):
        d = []
        if cid not in p:
            d.append("present only in CLI")
        elif cid not in c:
            d.append("present only in API")
        else:
            # networks is a dict and ports a sorted list: ordering does not matter
            for f in FIELDS:
                if c[cid][f] != p[cid][f]:
                    d.append(f"{f}: CLI={c[cid][f]} | API={p[cid][f]}")
        diffs[cid] = d
    return diffs


# ------------------------------------------------------- scenario steps
# Every step prints its result and appends rows (check, subject, result, details)
# that end up in report.csv.
def report_network(api, proxy_service, rows):
    print("\n== Network -> containers ==")
    for net, items in sorted(network_table(api).items()):
        print(f"{net}: {', '.join(items)}")
        rows.append(("network_table", net, "INFO", ", ".join(items)))
    violations = check_ports(api, proxy_service)
    if violations:
        print("Violations:\n  " + "\n  ".join(violations))
    else:
        print("meets the requirements")
    rows.append(("port_publishing", api["project"], "FAIL" if violations else "PASS",
                 "; ".join(violations) or "meets the requirements"))


def report_http(url, rows):
    print("\n== HTTP through the web proxy ==")
    counters = []
    for i in (1, 2):
        r = http_probe(url)
        counter = get_counter(r["body"])
        counters.append(counter)
        print(f"request {i}: status={r['status']} time={r['time']}s counter={counter} {r['error']}")
        rows.append((f"http_request_{i}", url, r["status"],
                     f"time={r['time']}s counter={counter} {r['error']}".strip()))
    chain_ok = None not in counters and counters[1] > counters[0]
    print("Proxy -> app -> DB chain:", "confirmed" if chain_ok else "NOT confirmed")
    rows.append(("proxy_app_db_chain", f"{counters[0]} -> {counters[1]}",
                 "PASS" if chain_ok else "FAIL", "the visit counter must increase"))


def report_comparison(ssh, client, cli, api, project, rows):
    diffs = compare(cli, api)
    found = sum(1 for d in diffs.values() if d)
    if found:
        # A difference may be a real error or just a state change between the two polls:
        # collect again to tell them apart.
        print(f"\nDifferences found: {found}. Collecting again in 2 s...")
        time.sleep(2)
        cli, api = collect_cli(ssh, project), collect_api(client, project)
        diffs = compare(cli, api)
        persisted = any(diffs.values())
        verdict = ("differences persisted: likely a processing error" if persisted
                   else "differences disappeared: container state changed between polls")
        print(verdict)
        rows.append(("cli_vs_api_recheck", "all", "FAIL" if persisted else "PASS", verdict))
    service_of = {c["id"]: c["service"] for c in cli["containers"] + api["containers"]}
    print("\n== CLI/API comparison ==")
    for cid, d in diffs.items():
        status = "FAIL" if d else "PASS"
        print(f"{service_of[cid]} ({cid[:12]}): {status} {'; '.join(d)}")
        rows.append(("cli_vs_api", f"{service_of[cid]} {cid[:12]}", status, "; ".join(d) or "match"))


def write_report(rows):
    with open("report.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["check", "subject", "result", "details"])
        w.writerows(rows)
    print("\nReport saved: report.csv")


def run(args):
    try:
        ssh = ssh_connect(args)
    except (paramiko.SSHException, OSError) as e:
        print(f"[ERROR] node {args.host} is unreachable: {type(e).__name__}: {e}")
        return 2
    try:
        show_environment(ssh, args.project)
        cli = collect_cli(ssh, args.project)
        if not cli["containers"]:
            print(f"[ERROR] no containers found in project '{args.project}'")
            return 3
        client = docker_client(args)
        api = collect_api(client, args.project)
        with open("inventory.json", "w", encoding="utf-8") as f:
            json.dump({"cli": cli, "api": api}, f, ensure_ascii=False, indent=2)
        print("\nInventory saved: inventory.json")

        rows = []
        report_network(api, args.proxy_service, rows)
        report_http(args.proxy_url, rows)
        report_comparison(ssh, client, cli, api, args.project, rows)
        client.close()
        write_report(rows)
        return 0
    except (RuntimeError, docker.errors.DockerException, paramiko.SSHException, OSError) as e:
        print(f"[ERROR] {type(e).__name__}: {e}")
        return 1
    finally:
        ssh.close()


def monitor(args):
    print(f"Monitoring every {args.interval} s (Ctrl+C to stop) -> monitoring.csv")
    client = None
    with open("monitoring.csv", "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if f.tell() == 0:
            w.writerow(["timestamp", "services", "http_status", "http_time_s", "overall", "error"])
        try:
            while True:
                all_running = False
                try:
                    if client is None:
                        client = docker_client(args)
                    pairs = []
                    for c in client.containers.list(
                            all=True, filters={"label": f"{LABEL_PROJECT}={args.project}"}):
                        c.reload()
                        pairs.append((c.labels.get(LABEL_SERVICE, c.name), c.attrs["State"]["Status"]))
                    services = ";".join(f"{svc}={st}" for svc, st in sorted(pairs))
                    all_running = bool(pairs) and all(st == "running" for _, st in pairs)
                except Exception as e:  # Docker unreachable: keep the loop alive
                    services = f"DOCKER_ERROR:{type(e).__name__}"
                    client = None       # recreate the client on the next iteration
                r = http_probe(args.proxy_url)
                overall = "OK" if all_running and r["status"] == 200 else "DEGRADED"
                row = [now_iso(), services, r["status"], r["time"], overall, r["error"]]
                w.writerow(row)
                f.flush()
                print(*row)
                time.sleep(args.interval)
        except KeyboardInterrupt:
            print("\nMonitoring stopped.")
    return 0


def parse_args():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--host", required=True, help="IP/name of the Linux VM")
    p.add_argument("--user", required=True, help="SSH user (must have access to Docker)")
    p.add_argument("--key", default=None, help="private SSH key (optional if ssh-agent is used)")
    p.add_argument("--project", required=True, help="Docker Compose project name")
    p.add_argument("--proxy-service", default="proxy", help="Compose service name of the web proxy")
    p.add_argument("--proxy-url", required=True, help="e.g. http://192.168.122.169:8080/")
    p.add_argument("--monitor", action="store_true", help="periodic monitoring mode")
    p.add_argument("--interval", type=int, default=10, help="monitoring interval, seconds")
    return p.parse_args()


if __name__ == "__main__":
    args = parse_args()
    sys.exit(monitor(args) if args.monitor else run(args))
