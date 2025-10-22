import subprocess
import os
import logging
import time
from typing import Optional, Dict, Any
from .detector import UpdateDetector
from .pm2_interface import PM2Manager

class UpdateOrchestrator:
    """Orchestrates the complete update workflow."""
    
    def __init__(self, repo_path: Optional[str] = None):
        self.detector = UpdateDetector(repo_path)
        self.pm2_manager = PM2Manager()
        self.logger = logging.getLogger(__name__)
        self.repo_path = self.detector.repo_path
        
    def _run_shell_command(self, cmd: str, cwd: Optional[str] = None) -> tuple[bool, str]:
        """Run a shell command and return success status and output."""
        try:
            result = subprocess.run(
                cmd,
                shell=True,
                executable='/bin/bash',
                cwd=cwd or self.repo_path,
                capture_output=True,
                text=True,
                check=True
            )
            return True, result.stdout
        except subprocess.CalledProcessError as e:
            self.logger.error(f"Command failed: {cmd}")
            self.logger.error(f"Error: {e.stderr}")
            return False, e.stderr or str(e)
    
    def perform_git_update(self, target_commit: Optional[str] = None) -> bool:
        """Perform git update to latest commit or specified commit."""
        self.logger.info("Starting git update...")
        
        if not target_commit:
            # Get the latest remote commit
            current_branch = self.detector.get_current_branch()
            if not current_branch:
                self.logger.error("Could not determine current branch")
                return False
                
            target_commit = self.detector.get_remote_commit(current_branch)
            if not target_commit:
                self.logger.error("Could not determine target commit")
                return False
        
        # Perform hard reset to target commit
        reset_cmd = f"git reset --hard {target_commit}"
        success, output = self._run_shell_command(reset_cmd)
        
        if success:
            self.logger.info(f"Successfully updated to commit: {target_commit[:8]}")
            return True
        else:
            self.logger.error(f"Failed to update git repository: {output}")
            return False
    
    def run_setup_scripts(self) -> bool:
        """Run the setup environment script."""
        self.logger.info("Running setup scripts...")
        
        # Get project structure paths
        venv_path = os.path.join(self.repo_path, "gittensor-venv")
        setup_script = os.path.join(self.repo_path, "scripts", "setup_env_light.sh")
        
        if not os.path.exists(setup_script):
            self.logger.error(f"Setup script not found: {setup_script}")
            return False
        
        if not os.path.exists(venv_path):
            self.logger.error(f"Virtual environment not found: {venv_path}")
            return False
        
        # Run setup script with virtual environment activated
        setup_cmd = f"source {venv_path}/bin/activate && bash {setup_script}"
        success, output = self._run_shell_command(setup_cmd)
        
        if success:
            self.logger.info("Setup scripts completed successfully")
            return True
        else:
            self.logger.error(f"Setup scripts failed: {output}")
            return False
    
    def rollback_git_update(self, previous_commit: str) -> bool:
        """Rollback git to previous commit in case of failure."""
        self.logger.warning(f"Rolling back to previous commit: {previous_commit[:8]}")
        
        rollback_cmd = f"git reset --hard {previous_commit}"
        success, output = self._run_shell_command(rollback_cmd)
        
        if success:
            self.logger.info("Rollback completed successfully")
            return True
        else:
            self.logger.error(f"Rollback failed: {output}")
            return False
    
    def update_neuron_process(self, neuron_type: str, process_name: str) -> bool:
        """Update a neuron process with full workflow."""
        self.logger.info(f"Starting update for {neuron_type} process: {process_name}")
        
        # Get current state for potential rollback
        previous_commit = self.detector.get_local_commit()
        if not previous_commit:
            self.logger.error("Could not get current commit for rollback")
            return False
        
        # Check if process exists
        if not self.pm2_manager.get_process_info(process_name):
            self.logger.error(f"Process {process_name} not found in PM2")
            return False
        
        try:
            # Step 1: Stop the process
            self.logger.info(f"Stopping {process_name}...")
            if not self.pm2_manager.stop_process(process_name):
                self.logger.error(f"Failed to stop {process_name}")
                return False
            
            # Step 2: Perform git update
            if not self.perform_git_update():
                self.logger.error("Git update failed")
                return False
            
            # Step 3: Run setup scripts
            if not self.run_setup_scripts():
                self.logger.error("Setup scripts failed, rolling back...")
                self.rollback_git_update(previous_commit)
                return False
            
            # Step 4: Start the process
            self.logger.info(f"Starting {process_name}...")
            if not self.pm2_manager.start_process(process_name):
                self.logger.error(f"Failed to start {process_name}, rolling back...")
                self.rollback_git_update(previous_commit)
                self.pm2_manager.start_process(process_name)
                return False
            
            # Step 5: Wait for healthy restart
            if not self.pm2_manager.wait_for_healthy_restart(process_name, max_wait_seconds=120):
                self.logger.error(f"{process_name} failed to start healthy, rolling back...")
                self.pm2_manager.stop_process(process_name)
                self.rollback_git_update(previous_commit)
                self.pm2_manager.start_process(process_name)
                return False
            
            self.logger.info(f"Successfully updated {process_name}")
            return True
            
        except Exception as e:
            self.logger.error(f"Unexpected error during update: {e}")
            self.logger.info("Attempting rollback...")
            self.rollback_git_update(previous_commit)
            self.pm2_manager.start_process(process_name)
            return False
    
    def get_update_status(self) -> Dict[str, Any]:
        """Get current update status information."""
        commit_info = self.detector.get_commit_info()
        
        return {
            "repo_path": self.repo_path,
            "current_branch": commit_info["current_branch"],
            "local_commit": commit_info["local_commit"],
            "remote_commit": commit_info["remote_commit"],
            "update_available": commit_info["local_commit"] != commit_info["remote_commit"],
            "timestamp": time.time()
        }