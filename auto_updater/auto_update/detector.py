import subprocess
import os
import logging
from typing import Optional, Tuple

class UpdateDetector:
    """Detects if git repository updates are available."""
    
    def __init__(self, repo_path: Optional[str] = None):
        self.repo_path = repo_path or self._get_repo_root()
        self.logger = logging.getLogger(__name__)
        
    def _get_repo_root(self) -> str:
        """Get the git repository root directory."""
        try:
            result = subprocess.run(
                ["git", "rev-parse", "--show-toplevel"],
                capture_output=True,
                text=True,
                check=True
            )
            return result.stdout.strip()
        except subprocess.CalledProcessError:
            # Fallback to current directory structure
            script_dir = os.path.dirname(os.path.abspath(__file__))
            return os.path.abspath(os.path.join(script_dir, "..", ".."))
    
    def _run_git_command(self, cmd: list[str]) -> Tuple[bool, str]:
        """Run a git command and return success status and output."""
        try:
            result = subprocess.run(
                cmd,
                cwd=self.repo_path,
                capture_output=True,
                text=True,
                check=True
            )
            return True, result.stdout.strip()
        except subprocess.CalledProcessError as e:
            self.logger.error(f"Git command failed: {' '.join(cmd)}, Error: {e.stderr}")
            return False, e.stderr.strip() if e.stderr else str(e)
    
    def get_current_branch(self) -> Optional[str]:
        """Get the current git branch name."""
        success, output = self._run_git_command(["git", "rev-parse", "--abbrev-ref", "HEAD"])
        return output if success else None
    
    def get_local_commit(self) -> Optional[str]:
        """Get the current local commit hash."""
        success, output = self._run_git_command(["git", "rev-parse", "HEAD"])
        return output if success else None
    
    def fetch_remote(self) -> bool:
        """Fetch latest changes from remote repository."""
        success, _ = self._run_git_command(["git", "fetch"])
        if success:
            self.logger.info("Successfully fetched remote changes")
        return success
    
    def get_remote_commit(self, branch: Optional[str] = None) -> Optional[str]:
        """Get the latest remote commit hash for the given branch."""
        if not branch:
            branch = self.get_current_branch()
        
        if not branch:
            return None
            
        success, output = self._run_git_command(["git", "rev-parse", f"origin/{branch}"])
        return output if success else None
    
    def is_update_needed(self) -> bool:
        """Check if an update is needed by comparing local and remote commits."""
        self.logger.info("Checking for updates...")
        
        # Fetch latest changes first
        if not self.fetch_remote():
            self.logger.error("Failed to fetch remote changes")
            return False
        
        local_commit = self.get_local_commit()
        remote_commit = self.get_remote_commit()
        
        if not local_commit or not remote_commit:
            self.logger.error("Failed to get commit information")
            return False
        
        is_behind = local_commit != remote_commit
        
        if is_behind:
            self.logger.info(f"Update available: {local_commit[:8]} -> {remote_commit[:8]}")
        else:
            self.logger.info("Repository is up to date")
            
        return is_behind
    
    def get_commit_info(self) -> dict:
        """Get detailed information about current and remote commits."""
        return {
            "current_branch": self.get_current_branch(),
            "local_commit": self.get_local_commit(),
            "remote_commit": self.get_remote_commit(),
            "repo_path": self.repo_path
        }