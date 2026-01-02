import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

from .detector import UpdateDetector
from .orchestrator import UpdateOrchestrator


@dataclass
class ManagedProcess:
    """Configuration for a process managed by auto-update."""

    name: str
    neuron_type: str  # 'validator' or 'miner'
    pm2_process_name: str
    enabled: bool = True
    last_update: Optional[float] = None
    update_count: int = 0
    last_error: Optional[str] = None


@dataclass
class AutoUpdateConfig:
    """Configuration for the auto-update manager."""

    check_interval_seconds: int = 900  # 15 minutes
    enabled: bool = True
    max_consecutive_failures: int = 3
    failure_cooldown_seconds: int = 1800  # 30 minutes
    processes: List[ManagedProcess] = field(default_factory=list)


class AutoUpdateManager:
    """Main auto-update manager that coordinates all update operations."""

    def __init__(self, config: AutoUpdateConfig):
        self.config = config
        self.orchestrator = UpdateOrchestrator()
        self.detector = UpdateDetector()
        self.logger = logging.getLogger(__name__)

        # State tracking
        self.is_running = False
        self.last_check_time = 0
        self.consecutive_failures = 0
        self.last_failure_time = 0
        self.update_thread: Optional[threading.Thread] = None

        # Callbacks
        self.on_update_started: Optional[Callable[[str], None]] = None
        self.on_update_completed: Optional[Callable[[str, bool], None]] = None
        self.on_update_failed: Optional[Callable[[str, str], None]] = None

    def add_managed_process(self, process: ManagedProcess) -> None:
        """Add a process to be managed by auto-update."""
        # Remove existing process with same name if it exists
        self.config.processes = [p for p in self.config.processes if p.name != process.name]
        self.config.processes.append(process)
        self.logger.info(f'Added managed process: {process.name} ({process.neuron_type})')

    def remove_managed_process(self, process_name: str) -> bool:
        """Remove a process from auto-update management."""
        initial_count = len(self.config.processes)
        self.config.processes = [p for p in self.config.processes if p.name != process_name]

        if len(self.config.processes) < initial_count:
            self.logger.info(f'Removed managed process: {process_name}')
            return True
        return False

    def get_managed_process(self, process_name: str) -> Optional[ManagedProcess]:
        """Get a managed process by name."""
        for process in self.config.processes:
            if process.name == process_name:
                return process
        return None

    def _is_in_failure_cooldown(self) -> bool:
        """Check if we're in cooldown period after failures."""
        if self.consecutive_failures >= self.config.max_consecutive_failures:
            time_since_failure = time.time() - self.last_failure_time
            return time_since_failure < self.config.failure_cooldown_seconds
        return False

    def _record_update_success(self, process: ManagedProcess) -> None:
        """Record successful update for a process."""
        process.last_update = time.time()
        process.update_count += 1
        process.last_error = None
        self.consecutive_failures = 0

        self.logger.info(f'Update successful for {process.name} (total updates: {process.update_count})')

        if self.on_update_completed:
            self.on_update_completed(process.name, True)

    def _record_update_failure(self, process: ManagedProcess, error: str) -> None:
        """Record failed update for a process."""
        process.last_error = error
        self.consecutive_failures += 1
        self.last_failure_time = time.time()

        self.logger.error(f'Update failed for {process.name}: {error}')
        self.logger.error(f'Consecutive failures: {self.consecutive_failures}/{self.config.max_consecutive_failures}')

        if self.on_update_failed:
            self.on_update_failed(process.name, error)

        if self.on_update_completed:
            self.on_update_completed(process.name, False)

    def _check_and_update_process(self, process: ManagedProcess) -> None:
        """Check and update a single process if needed."""
        if not process.enabled:
            return

        self.logger.info(f'Checking process: {process.name}')

        try:
            # Check if update is needed
            if not self.detector.is_update_needed():
                self.logger.info(f'No update needed for {process.name}')
                return

            # Notify update started
            if self.on_update_started:
                self.on_update_started(process.name)

            # Perform update
            success = self.orchestrator.update_neuron_process(process.neuron_type, process.pm2_process_name)

            if success:
                self._record_update_success(process)
            else:
                self._record_update_failure(process, 'Update orchestration failed')

        except Exception as e:
            error_msg = f'Unexpected error during update: {str(e)}'
            self._record_update_failure(process, error_msg)

    def _update_loop(self) -> None:
        """Main update loop that runs in a separate thread."""
        self.logger.info('Auto-update loop started')

        while self.is_running:
            try:
                current_time = time.time()

                # Check if it's time for an update check
                if current_time - self.last_check_time >= self.config.check_interval_seconds:
                    self.logger.info('Starting update check cycle')

                    # Check if we're in cooldown period
                    if self._is_in_failure_cooldown():
                        remaining_cooldown = self.config.failure_cooldown_seconds - (
                            current_time - self.last_failure_time
                        )
                        self.logger.warning(
                            f'In failure cooldown, skipping update check. {remaining_cooldown:.0f}s remaining'
                        )
                    else:
                        # Check and update all enabled processes
                        for process in self.config.processes:
                            if not self.is_running:  # Check if we should stop
                                break

                            self._check_and_update_process(process)

                    self.last_check_time = current_time

                # Sleep for a short time to avoid busy waiting
                time.sleep(30)

            except Exception as e:
                self.logger.error(f'Error in update loop: {e}')
                time.sleep(60)  # Wait longer on error

        self.logger.info('Auto-update loop stopped')

    def start(self) -> bool:
        """Start the auto-update manager."""
        if self.is_running:
            self.logger.warning('Auto-update manager is already running')
            return False

        if not self.config.enabled:
            self.logger.info('Auto-update is disabled')
            return False

        if not self.config.processes:
            self.logger.warning('No processes configured for auto-update')
            return False

        self.logger.info('Starting auto-update manager...')
        self.is_running = True
        self.update_thread = threading.Thread(target=self._update_loop, daemon=True)
        self.update_thread.start()

        self.logger.info(f'Auto-update manager started with {len(self.config.processes)} managed processes')
        return True

    def stop(self) -> None:
        """Stop the auto-update manager."""
        if not self.is_running:
            return

        self.logger.info('Stopping auto-update manager...')
        self.is_running = False

        if self.update_thread and self.update_thread.is_alive():
            self.update_thread.join(timeout=10)

        self.logger.info('Auto-update manager stopped')

    def force_update_all(self) -> Dict[str, bool]:
        """Force update all managed processes immediately."""
        self.logger.info('Forcing update for all managed processes')
        results = {}

        for process in self.config.processes:
            if process.enabled:
                try:
                    if self.on_update_started:
                        self.on_update_started(process.name)

                    success = self.orchestrator.update_neuron_process(process.neuron_type, process.pm2_process_name)

                    results[process.name] = success

                    if success:
                        self._record_update_success(process)
                    else:
                        self._record_update_failure(process, 'Forced update failed')

                except Exception as e:
                    error_msg = f'Error during forced update: {str(e)}'
                    results[process.name] = False
                    self._record_update_failure(process, error_msg)
            else:
                results[process.name] = False

        return results

    def get_status(self) -> Dict:
        """Get current status of the auto-update manager."""
        return {
            'enabled': self.config.enabled,
            'running': self.is_running,
            'check_interval': self.config.check_interval_seconds,
            'last_check': self.last_check_time,
            'consecutive_failures': self.consecutive_failures,
            'in_cooldown': self._is_in_failure_cooldown(),
            'managed_processes': len(self.config.processes),
            'processes': [
                {
                    'name': p.name,
                    'neuron_type': p.neuron_type,
                    'pm2_process_name': p.pm2_process_name,
                    'enabled': p.enabled,
                    'last_update': p.last_update,
                    'update_count': p.update_count,
                    'last_error': p.last_error,
                }
                for p in self.config.processes
            ],
            'update_status': self.orchestrator.get_update_status(),
        }
