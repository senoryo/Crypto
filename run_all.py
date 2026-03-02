"""
Crypto Trading System Launcher
Starts all components in the correct order with proper dependency management.

Usage:
    python run_all.py          # Start all components
    python run_all.py --no-gui # Start backend only (no browser)

Component startup order (respects dependencies):
    1. MKTDATA   (port 8081) - no dependencies
    2. EXCHCONN  (port 8084) - no dependencies
    3. POSMANAGER(port 8085) - needs MKTDATA
    4. OM        (port 8083) - needs EXCHCONN, POSMANAGER
    5. GUIBROKER (port 8082) - needs OM
    6. ALGO      (port 8086) - needs MKTDATA, OM
    7. GUI       (port 8080) - needs GUIBROKER, MKTDATA, POSMANAGER
    8. PROXY     (PORT env)  - only on Render / when PORT is set
"""

import subprocess
import sys
import time
import signal
import os
import webbrowser

PYTHON = sys.executable
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Detect Render / proxy mode: RENDER=true or PORT env set to non-8080 value
_port_env = os.environ.get("PORT", "")
USE_PROXY = (
    os.environ.get("RENDER", "").lower() == "true"
    or (_port_env != "" and _port_env != "8080")
)

COMPONENTS = [
    {
        "name": "MKTDATA",
        "module": "mktdata.mktdata",
        "port": 8081,
        "delay_after": 1.0,
    },
    {
        "name": "EXCHCONN",
        "module": "exchconn.exchconn",
        "port": 8084,
        "delay_after": 1.0,
    },
    {
        "name": "POSMANAGER",
        "module": "posmanager.posmanager",
        "port": 8085,
        "delay_after": 1.0,
    },
    {
        "name": "OM",
        "module": "om.order_manager",
        "port": 8083,
        "delay_after": 1.0,
    },
    {
        "name": "GUIBROKER",
        "module": "guibroker.guibroker",
        "port": 8082,
        "delay_after": 1.0,
    },
    {
        "name": "ALGO",
        "module": "algo",
        "port": 8086,
        "delay_after": 1.0,
    },
    {
        "name": "GUI",
        "module": "gui.server",
        "port": 8080,
        "delay_after": 0.5,
    },
]


def _shutdown(processes):
    """Terminate all child processes gracefully."""
    print("\n\n  Shutting down all components...")
    for name, proc in reversed(processes):
        if proc.poll() is None:
            print(f"  Stopping {name} (PID: {proc.pid})...")
            proc.terminate()
    time.sleep(2)
    for name, proc in processes:
        if proc.poll() is None:
            print(f"  Force killing {name}...")
            proc.kill()
    print("  All components stopped.")


def start_all(open_browser: bool = True):
    """Start all components in order."""
    processes = []

    components = list(COMPONENTS)
    if USE_PROXY:
        proxy_port = int(os.environ.get("PORT", 10000))
        components.append({
            "name": "PROXY",
            "module": "proxy",
            "port": proxy_port,
            "delay_after": 0.5,
        })

    print("=" * 60)
    print("  CRYPTO TRADING SYSTEM - Starting All Components")
    if USE_PROXY:
        print(f"  (Proxy mode: all traffic via port {proxy_port})")
    print("=" * 60)

    for comp in components:
        name = comp["name"]
        module = comp["module"]
        port = comp["port"]
        delay = comp["delay_after"]

        print(f"\n  Starting {name} on port {port}...")
        proc = subprocess.Popen(
            [PYTHON, "-m", module],
            cwd=BASE_DIR,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        processes.append((name, proc))
        print(f"  {name} started (PID: {proc.pid})")
        time.sleep(delay)

    print("\n" + "=" * 60)
    print("  All components started!")
    print("=" * 60)

    if USE_PROXY:
        print(f"\n  PROXY:      http://0.0.0.0:{proxy_port}")
    else:
        print(f"\n  GUI:        http://localhost:8080")
    print(f"  MKTDATA:    ws://localhost:8081")
    print(f"  GUIBROKER:  ws://localhost:8082")
    print(f"  OM:         ws://localhost:8083")
    print(f"  EXCHCONN:   ws://localhost:8084")
    print(f"  POSMANAGER: ws://localhost:8085")
    print(f"  ALGO:       ws://localhost:8086")
    print(f"\n  Press Ctrl+C to stop all components\n")

    # SIGTERM handler for Render's graceful shutdown
    def _sigterm_handler(signum, frame):
        _shutdown(processes)
        sys.exit(0)

    signal.signal(signal.SIGTERM, _sigterm_handler)

    if open_browser and not USE_PROXY:
        time.sleep(1)
        webbrowser.open("http://localhost:8080")

    # Wait and handle shutdown
    try:
        while True:
            # Check if any process has died
            for name, proc in processes:
                ret = proc.poll()
                if ret is not None:
                    print(f"\n  WARNING: {name} exited with code {ret} (check logs/{name}.log)")
            time.sleep(2)
    except KeyboardInterrupt:
        _shutdown(processes)


if __name__ == "__main__":
    no_gui = "--no-gui" in sys.argv
    start_all(open_browser=not no_gui)
