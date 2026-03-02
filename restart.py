"""
Clean restart of the Crypto Trading System.
Kills all component processes on ports 8080-8086, then starts everything fresh.

Usage:
    python restart.py          # Full restart with browser
    python restart.py --no-gui # Restart backend only
"""

import socket
import subprocess
import sys
import time
import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

PORTS = [8080, 8081, 8082, 8083, 8084, 8085, 8086]
PORT_NAMES = {
    8080: "GUI",
    8081: "MKTDATA",
    8082: "GUIBROKER",
    8083: "OM",
    8084: "EXCHCONN",
    8085: "POSMANAGER",
    8086: "ALGO",
}


def find_pids_on_ports():
    """Use netstat to find PIDs listening on our ports."""
    pids = {}
    try:
        result = subprocess.run(
            ["netstat", "-ano"],
            capture_output=True, text=True, timeout=10,
        )
        for line in result.stdout.splitlines():
            parts = line.split()
            if len(parts) >= 5 and "LISTENING" in parts:
                addr = parts[1]
                pid = parts[4]
                for port in PORTS:
                    if addr.endswith(f":{port}") and pid != "0":
                        pids[port] = int(pid)
    except Exception as e:
        print(f"  Warning: netstat failed: {e}")
    return pids


def kill_all():
    """Find and kill all processes on component ports."""
    print("\n  Scanning for running components...")
    pids = find_pids_on_ports()

    if not pids:
        print("  No running components found.")
        return

    killed = set()
    for port, pid in sorted(pids.items()):
        name = PORT_NAMES.get(port, f"port {port}")
        if pid in killed:
            print(f"  {name:12s} :{port} -> PID {pid} (already killed)")
            continue
        print(f"  {name:12s} :{port} -> killing PID {pid}")
        try:
            subprocess.run(
                ["taskkill", "/F", "/PID", str(pid)],
                capture_output=True, timeout=5,
            )
            killed.add(pid)
        except Exception as e:
            print(f"    Warning: failed to kill PID {pid}: {e}")

    # Wait for ports to free up
    print("  Waiting for ports to release...")
    for _ in range(10):
        time.sleep(0.5)
        still_busy = []
        for port in PORTS:
            try:
                with socket.create_connection(("localhost", port), timeout=0.3):
                    still_busy.append(port)
            except (OSError, socket.timeout) as e:
                print(f"  Port {port} is free: {e}")
        if not still_busy:
            break
    else:
        if still_busy:
            print(f"  Warning: ports still in use: {still_busy}")


def main():
    print("=" * 60)
    print("  CRYPTO TRADING SYSTEM - Clean Restart")
    print("=" * 60)

    kill_all()

    print("\n  Starting all components...\n")

    args = [sys.executable, os.path.join(BASE_DIR, "run_all.py")]
    if "--no-gui" in sys.argv:
        args.append("--no-gui")

    os.execv(sys.executable, args)


if __name__ == "__main__":
    main()
