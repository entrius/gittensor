import json
import logging
import subprocess
import time
from dataclasses import dataclass
from typing import Any, List, Optional


@dataclass
class PM2ProcessInfo:
    """Information about a PM2 process."""

    name: str
    pid: Optional[int]
    status: str
    uptime: Optional[int]
    restarts: int
    cpu: float
    memory: int


class PM2Manager:
    """Interface for managing PM2 processes."""

    def __init__(self):
        self.logger = logging.getLogger(__name__)

    def _run_pm2_command(self, cmd: List[str]) -> tuple[bool, Any]:
        """Run a PM2 command and return success status and parsed output."""
        try:
            result = subprocess.run(["pm2"] + cmd, capture_output=True, text=True, check=True)

            # Try to parse JSON output for jlist command
            if "jlist" in cmd:
                try:
                    return True, json.loads(result.stdout)
                except json.JSONDecodeError:
                    return True, result.stdout

            return True, result.stdout

        except subprocess.CalledProcessError as e:
            self.logger.error(f"PM2 command failed: {' '.join(['pm2'] + cmd)}")
            self.logger.error(f"Error: {e.stderr}")
            return False, e.stderr
        except FileNotFoundError:
            self.logger.error("PM2 not found. Please install PM2 first.")
            return False, "PM2 not installed"

    def get_process_info(self, process_name: str) -> Optional[PM2ProcessInfo]:
        """Get information about a specific PM2 process."""
        success, data = self._run_pm2_command(["jlist"])

        if not success or not isinstance(data, list):
            return None

        for process in data:
            if process.get("name") == process_name:
                return PM2ProcessInfo(
                    name=process.get("name", ""),
                    pid=process.get("pid"),
                    status=process.get("pm2_env", {}).get("status", "unknown"),
                    uptime=process.get("pm2_env", {}).get("pm_uptime"),
                    restarts=process.get("pm2_env", {}).get("restart_time", 0),
                    cpu=float(process.get("monit", {}).get("cpu", 0)),
                    memory=int(process.get("monit", {}).get("memory", 0)),
                )

        return None

    def is_process_running(self, process_name: str) -> bool:
        """Check if a PM2 process is currently running."""
        info = self.get_process_info(process_name)
        return info is not None and info.status == "online"

    def restart_process(self, process_name: str) -> bool:
        """Restart a PM2 process."""
        self.logger.info(f"Restarting PM2 process: {process_name}")
        success, output = self._run_pm2_command(["restart", process_name])

        if success:
            self.logger.info(f"Successfully restarted {process_name}")
        else:
            self.logger.error(f"Failed to restart {process_name}: {output}")

        return success

    def stop_process(self, process_name: str) -> bool:
        """Stop a PM2 process."""
        self.logger.info(f"Stopping PM2 process: {process_name}")
        success, output = self._run_pm2_command(["stop", process_name])

        if success:
            self.logger.info(f"Successfully stopped {process_name}")
        else:
            self.logger.error(f"Failed to stop {process_name}: {output}")

        return success

    def start_process(self, process_name: str) -> bool:
        """Start a PM2 process."""
        self.logger.info(f"Starting PM2 process: {process_name}")
        success, output = self._run_pm2_command(["start", process_name])

        if success:
            self.logger.info(f"Successfully started {process_name}")
        else:
            self.logger.error(f"Failed to start {process_name}: {output}")

        return success

    def wait_for_healthy_restart(self, process_name: str, max_wait_seconds: int = 60) -> bool:
        """Wait for a process to restart and become healthy."""
        self.logger.info(f"Waiting for {process_name} to become healthy...")

        start_time = time.time()

        while time.time() - start_time < max_wait_seconds:
            info = self.get_process_info(process_name)

            if info and info.status == "online":
                # Wait a bit longer to ensure stability
                time.sleep(5)

                # Check again to make sure it's stable
                info = self.get_process_info(process_name)
                if info and info.status == "online":
                    self.logger.info(f"{process_name} is healthy and running")
                    return True

            time.sleep(2)

        self.logger.error(f"{process_name} failed to become healthy within {max_wait_seconds}s")
        return False

    def get_all_processes(self) -> List[PM2ProcessInfo]:
        """Get information about all PM2 processes."""
        success, data = self._run_pm2_command(["jlist"])

        if not success or not isinstance(data, list):
            return []

        processes = []
        for process in data:
            processes.append(
                PM2ProcessInfo(
                    name=process.get("name", ""),
                    pid=process.get("pid"),
                    status=process.get("pm2_env", {}).get("status", "unknown"),
                    uptime=process.get("pm2_env", {}).get("pm_uptime"),
                    restarts=process.get("pm2_env", {}).get("restart_time", 0),
                    cpu=float(process.get("monit", {}).get("cpu", 0)),
                    memory=int(process.get("monit", {}).get("memory", 0)),
                )
            )

        return processes

    def get_logs(self, process_name: str, lines: int = 50) -> Optional[str]:
        """Get recent logs for a PM2 process."""
        success, output = self._run_pm2_command(["logs", process_name, "--lines", str(lines), "--nostream"])
        return output if success else None

