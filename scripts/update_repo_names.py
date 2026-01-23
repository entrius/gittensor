#!/usr/bin/env python3
"""
Repository Name Updater

This script checks all repository names in master_repositories.json and updates
them if they redirect to another repository using concurrent processing.

Usage:
    python scripts/update_repo_names.py [--dry-run] [--verbose] [--concurrency N]
"""

import json
import argparse
import asyncio
import aiohttp
import sys
import time
from pathlib import Path
from typing import Dict, Any, Optional, List, Tuple
from datetime import datetime


class RepoNameUpdater:
    def __init__(self, dry_run: bool = False, verbose: bool = False, concurrency: int = 10):
        self.dry_run = dry_run
        self.verbose = verbose
        self.concurrency = concurrency
        self.updates = []
        self.errors = []
        self.processed = 0
        self.total = 0
        
    async def check_repo_redirect(self, session: aiohttp.ClientSession, repo_name: str) -> Optional[str]:
        """Check if a repository redirects to another location."""
        if not self._is_valid_repo_name(repo_name):
            return None
            
        try:
            url = f"https://github.com/{repo_name}"
            async with session.head(url, allow_redirects=True, timeout=aiohttp.ClientTimeout(total=10)) as response:
                if str(response.url) != url:
                    # Extract new repo name from redirected URL
                    path_parts = str(response.url).split('/')
                    if len(path_parts) >= 5:  # https://github.com/owner/repo
                        new_repo = f"{path_parts[3]}/{path_parts[4]}"
                        if new_repo != repo_name:
                            if self.verbose:
                                print(f"  ➡️  {repo_name} → {new_repo}")
                            return new_repo
                            
        except Exception as e:
            self.errors.append(f"{repo_name}: {str(e)}")
            if self.verbose:
                print(f"  ❌ {repo_name}: {str(e)}")
                
        return None
    
    def _is_valid_repo_name(self, repo_name: str) -> bool:
        """Check if repository name is valid."""
        return '/' in repo_name and len(repo_name.split('/')) == 2
    
    async def process_batch(self, session: aiohttp.ClientSession, batch: List[Tuple[str, Dict[str, Any]]]) -> Dict[str, Any]:
        """Process a batch of repositories concurrently."""
        tasks = []
        for repo_name, repo_info in batch:
            task = self.check_repo_redirect(session, repo_name)
            tasks.append((repo_name, repo_info, task))
        
        results = {}
        completed_tasks = await asyncio.gather(*[task for _, _, task in tasks], return_exceptions=True)
        
        for (repo_name, repo_info, _), new_name in zip(tasks, completed_tasks):
            if isinstance(new_name, Exception):
                self.errors.append(f"{repo_name}: {str(new_name)}")
                results[repo_name] = repo_info
            elif new_name:
                results[new_name] = repo_info
                self.updates.append((repo_name, new_name))
                print(f"Repository moved: {repo_name} → {new_name}")
            else:
                results[repo_name] = repo_info
            
            self.processed += 1
        
        return results
    
    async def update_repository_names(self, repos_data: Dict[str, Any]) -> Dict[str, Any]:
        """Process all repositories with concurrent requests."""
        self.total = len(repos_data)
        print(f"🔄 Processing {self.total} repositories with {self.concurrency} concurrent requests...")
        
        updated_data = {}
        repo_items = list(repos_data.items())
        
        # Create batches for concurrent processing
        batch_size = self.concurrency
        batches = [repo_items[i:i + batch_size] for i in range(0, len(repo_items), batch_size)]
        
        connector = aiohttp.TCPConnector(limit=self.concurrency, limit_per_host=5)
        timeout = aiohttp.ClientTimeout(total=10)
        
        async with aiohttp.ClientSession(
            connector=connector,
            timeout=timeout,
            headers={'User-Agent': 'GitTensor-Repo-Updater/1.0'}
        ) as session:
            
            for i, batch in enumerate(batches, 1):
                if self.verbose:
                    print(f"📦 Processing batch {i}/{len(batches)} ({len(batch)} repos)")
                
                batch_results = await self.process_batch(session, batch)
                updated_data.update(batch_results)
                
                # Show progress
                progress = (self.processed / self.total) * 100
                print(f"Progress: {self.processed}/{self.total} ({progress:.1f}%)")
                
                # Small delay between batches to be respectful
                if i < len(batches):
                    await asyncio.sleep(0.1)
        
        return updated_data
    
    def load_repositories_file(self, file_path: Path) -> Dict[str, Any]:
        """Load the repositories JSON file."""
        print(f"📂 Loading repositories from: {file_path}")
        
        if not file_path.exists():
            print(f"❌ File not found: {file_path}")
            sys.exit(1)
        
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            print(f"✅ Loaded {len(data)} repositories")
            return data
        except json.JSONDecodeError as e:
            print(f"❌ Invalid JSON: {e}")
            sys.exit(1)
        except Exception as e:
            print(f"❌ Error loading file: {e}")
            sys.exit(1)
    
    def save_repositories_file(self, file_path: Path, data: Dict[str, Any]) -> None:
        """Save the updated repositories JSON file."""
        if self.dry_run:
            print(f"🔍 DRY RUN: Would save {len(data)} repositories to {file_path}")
            return
        
        try:
            # Create backup
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            backup_path = file_path.with_suffix(f'.backup_{timestamp}.json')
            
            if file_path.exists():
                import shutil
                shutil.copy2(file_path, backup_path)
                print(f"💾 Backup created: {backup_path}")
            
            # Save updated file
            with open(file_path, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=2, ensure_ascii=False, sort_keys=True)
            
            print(f"✅ Updated file saved: {file_path}")
            
        except Exception as e:
            print(f"❌ Error saving file: {e}")
            sys.exit(1)
    
    def print_summary(self) -> None:
        """Print summary of updates and errors."""
        print("\n" + "="*60)
        print("📊 SUMMARY")
        print("="*60)
        
        print(f"Total repositories: {self.total}")
        print(f"Processed: {self.processed}")
        print(f"Updates found: {len(self.updates)}")
        print(f"Errors: {len(self.errors)}")
        
        if self.updates:
            print(f"\n✅ REPOSITORY UPDATES ({len(self.updates)}):")
            for old_name, new_name in self.updates:
                print(f"  {old_name} → {new_name}")
        
        if self.errors and self.verbose:
            print(f"\n❌ ERRORS ({len(self.errors)}):")
            for error in self.errors[:10]:  # Show first 10 errors
                print(f"  {error}")
            if len(self.errors) > 10:
                print(f"  ... and {len(self.errors) - 10} more errors")
        
        if self.dry_run and self.updates:
            print(f"\n🔍 DRY RUN: No changes made. Run without --dry-run to apply updates.")


async def main():
    parser = argparse.ArgumentParser(description="Update repository names using concurrent requests")
    parser.add_argument('--dry-run', action='store_true', help='Show changes without applying them')
    parser.add_argument('--verbose', '-v', action='store_true', help='Show detailed output')
    parser.add_argument('--concurrency', '-c', type=int, default=10, help='Number of concurrent requests (default: 10)')
    parser.add_argument('--file', '-f', type=Path, 
                       default=Path('gittensor/validator/weights/master_repositories.json'),
                       help='Path to repositories JSON file')
    
    args = parser.parse_args()
    
    if args.concurrency <= 0:
        print("❌ Concurrency must be positive")
        sys.exit(1)
    
    print("🚀 GitTensor Repository Name Updater")
    print(f"Mode: {'DRY RUN' if args.dry_run else 'LIVE UPDATE'}")
    print(f"Concurrency: {args.concurrency}")
    print("-" * 50)
    
    updater = RepoNameUpdater(
        dry_run=args.dry_run,
        verbose=args.verbose,
        concurrency=args.concurrency
    )
    
    try:
        # Load repositories
        repos_data = updater.load_repositories_file(args.file)
        
        # Process repositories
        start_time = time.time()
        updated_data = await updater.update_repository_names(repos_data)
        end_time = time.time()
        
        # Save results
        updater.save_repositories_file(args.file, updated_data)
        
        # Print summary
        updater.print_summary()
        
        print(f"\n⏱️  Processing time: {end_time - start_time:.1f} seconds")
        print("🎉 Process completed successfully!")
        
    except KeyboardInterrupt:
        print("\n⚠️  Operation cancelled by user")
        updater.print_summary()
        sys.exit(1)
    except Exception as e:
        print(f"\n❌ Error: {e}")
        sys.exit(1)


if __name__ == '__main__':
    asyncio.run(main())