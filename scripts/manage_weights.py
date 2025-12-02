#!/usr/bin/env python3
"""
CLI tool to manage master repositories and programming languages weights.

This script provides commands to:
- List, add, update, and remove master repositories
- Mark repositories as inactive
- List, add, update, and remove programming language weights
- Export and import configurations
- Validate JSON files
"""

import json
import argparse
import sys
from pathlib import Path
from datetime import datetime
from typing import Dict, Any, Optional


# Default paths
SCRIPT_DIR = Path(__file__).parent
PROJECT_ROOT = SCRIPT_DIR.parent
WEIGHTS_DIR = PROJECT_ROOT / "gittensor" / "validator" / "weights"
REPOS_FILE = WEIGHTS_DIR / "master_repositories.json"
LANGUAGES_FILE = WEIGHTS_DIR / "programming_languages.json"


class WeightsManager:
    """Manager for repository and language weights."""
    
    def __init__(self, repos_file: Path = REPOS_FILE, languages_file: Path = LANGUAGES_FILE):
        self.repos_file = repos_file
        self.languages_file = languages_file
        
    def load_repos(self) -> Dict[str, Any]:
        """Load master repositories from JSON file."""
        if not self.repos_file.exists():
            return {}
        with open(self.repos_file, 'r', encoding='utf-8') as f:
            return json.load(f)
    
    def save_repos(self, data: Dict[str, Any]) -> None:
        """Save master repositories to JSON file."""
        # Sort repositories alphabetically
        sorted_data = dict(sorted(data.items()))
        with open(self.repos_file, 'w', encoding='utf-8') as f:
            json.dump(sorted_data, f, indent=2, ensure_ascii=False)
        print(f"✓ Saved to {self.repos_file}")
    
    def load_languages(self) -> Dict[str, float]:
        """Load programming languages from JSON file."""
        if not self.languages_file.exists():
            return {}
        with open(self.languages_file, 'r', encoding='utf-8') as f:
            return json.load(f)
    
    def save_languages(self, data: Dict[str, float]) -> None:
        """Save programming languages to JSON file."""
        # Sort languages alphabetically
        sorted_data = dict(sorted(data.items()))
        with open(self.languages_file, 'w', encoding='utf-8') as f:
            json.dump(sorted_data, f, indent=2, ensure_ascii=False)
        print(f"✓ Saved to {self.languages_file}")
    
    def find_repo_case_insensitive(self, repos: Dict[str, Any], name: str) -> Optional[str]:
        """Find repository by name (case-insensitive). Returns the actual key if found."""
        name_lower = name.lower()
        for repo_name in repos.keys():
            if repo_name.lower() == name_lower:
                return repo_name
        return None
    
    # Repository management
    def list_repos(self, inactive_only: bool = False, active_only: bool = False, 
                   min_weight: Optional[float] = None, max_weight: Optional[float] = None) -> None:
        """List all repositories with optional filters."""
        repos = self.load_repos()
        
        if not repos:
            print("No repositories found.")
            return
        
        filtered = []
        for name, data in sorted(repos.items()):
            weight = data.get('weight', 0)
            is_inactive = 'inactiveAt' in data
            
            # Apply filters
            if inactive_only and not is_inactive:
                continue
            if active_only and is_inactive:
                continue
            if min_weight is not None and weight < min_weight:
                continue
            if max_weight is not None and weight > max_weight:
                continue
            
            filtered.append((name, data))
        
        if not filtered:
            print("No repositories match the filters.")
            return
        
        print(f"\nFound {len(filtered)} repositories:\n")
        print(f"{'Repository':<50} {'Weight':<10} {'Status':<15} {'Inactive Since'}")
        print("-" * 95)
        
        for name, data in filtered:
            weight = data.get('weight', 0)
            status = "INACTIVE" if 'inactiveAt' in data else "ACTIVE"
            inactive_at = data.get('inactiveAt', '-')
            print(f"{name:<50} {weight:<10.2f} {status:<15} {inactive_at}")
        
        print(f"\nTotal: {len(filtered)} repositories")
    
    def add_repo(self, name: str, weight: float) -> None:
        """Add a new repository."""
        repos = self.load_repos()
        
        # Check for case-insensitive duplicate
        existing = self.find_repo_case_insensitive(repos, name)
        if existing:
            print(f"✗ Repository '{name}' already exists as '{existing}'. Use 'update' to modify it.")
            sys.exit(1)
        
        repos[name] = {"weight": weight}
        self.save_repos(repos)
        print(f"✓ Added repository '{name}' with weight {weight}")
    
    def update_repo(self, name: str, weight: Optional[float] = None, 
                    mark_inactive: bool = False, mark_active: bool = False,
                    add_branches: Optional[list] = None, remove_branches: Optional[list] = None) -> None:
        """Update an existing repository."""
        repos = self.load_repos()
        
        # Find repository case-insensitively
        actual_name = self.find_repo_case_insensitive(repos, name)
        if not actual_name:
            print(f"✗ Repository '{name}' not found.")
            sys.exit(1)
        
        if weight is not None:
            repos[actual_name]['weight'] = weight
            print(f"✓ Updated weight for '{actual_name}' to {weight}")
        
        if mark_inactive:
            repos[actual_name]['inactiveAt'] = datetime.utcnow().isoformat() + 'Z'
            print(f"✓ Marked '{actual_name}' as inactive")
        
        if mark_active:
            if 'inactiveAt' in repos[actual_name]:
                del repos[actual_name]['inactiveAt']
                print(f"✓ Marked '{actual_name}' as active")
            else:
                print(f"ℹ Repository '{actual_name}' is already active")
        
        if add_branches:
            # Get existing branches or create empty list
            existing_branches = repos[actual_name].get('additional_acceptable_branches', [])
            # Add new branches, avoiding duplicates
            for branch in add_branches:
                if branch not in existing_branches:
                    existing_branches.append(branch)
            repos[actual_name]['additional_acceptable_branches'] = existing_branches
            print(f"✓ Added branches to '{actual_name}': {add_branches}")
            print(f"  Current branches: {existing_branches}")
        
        if remove_branches is not None:
            if 'additional_acceptable_branches' not in repos[actual_name]:
                print(f"ℹ Repository '{actual_name}' has no additional_acceptable_branches")
            elif len(remove_branches) == 0:
                # No branches specified, remove all
                del repos[actual_name]['additional_acceptable_branches']
                print(f"✓ Removed all additional_acceptable_branches from '{actual_name}'")
            else:
                # Remove specific branches
                existing_branches = repos[actual_name]['additional_acceptable_branches']
                removed = []
                not_found = []
                for branch in remove_branches:
                    if branch in existing_branches:
                        existing_branches.remove(branch)
                        removed.append(branch)
                    else:
                        not_found.append(branch)
                
                if removed:
                    if existing_branches:
                        repos[actual_name]['additional_acceptable_branches'] = existing_branches
                        print(f"✓ Removed branches from '{actual_name}': {removed}")
                        print(f"  Current branches: {existing_branches}")
                    else:
                        # All branches removed, delete the key
                        del repos[actual_name]['additional_acceptable_branches']
                        print(f"✓ Removed branches from '{actual_name}': {removed}")
                        print(f"  No branches remaining")
                
                if not_found:
                    print(f"ℹ Branches not found: {not_found}")
        
        self.save_repos(repos)
    
    def remove_repo(self, name: str, force: bool = False) -> None:
        """Remove a repository."""
        repos = self.load_repos()
        
        # Find repository case-insensitively
        actual_name = self.find_repo_case_insensitive(repos, name)
        if not actual_name:
            print(f"✗ Repository '{name}' not found.")
            sys.exit(1)
        
        if not force:
            confirm = input(f"Are you sure you want to remove '{actual_name}'? (yes/no): ")
            if confirm.lower() not in ['yes', 'y']:
                print("Cancelled.")
                return
        
        del repos[actual_name]
        self.save_repos(repos)
        print(f"✓ Removed repository '{actual_name}'")
    
    def get_repo(self, name: str) -> None:
        """Get details of a specific repository."""
        repos = self.load_repos()
        
        # Find repository case-insensitively
        actual_name = self.find_repo_case_insensitive(repos, name)
        if not actual_name:
            print(f"✗ Repository '{name}' not found.")
            sys.exit(1)
        
        data = repos[actual_name]
        print(f"\nRepository: {actual_name}")
        print(f"Weight: {data.get('weight', 0)}")
        if 'inactiveAt' in data:
            print(f"Status: INACTIVE (since {data['inactiveAt']})")
        else:
            print("Status: ACTIVE")
        if 'additional_acceptable_branches' in data:
            branches = data['additional_acceptable_branches']
            print(f"Additional Acceptable Branches: {', '.join(branches)}")
    
    # Language management
    def list_languages(self, min_weight: Optional[float] = None, 
                       max_weight: Optional[float] = None) -> None:
        """List all programming languages with optional filters."""
        languages = self.load_languages()
        
        if not languages:
            print("No languages found.")
            return
        
        filtered = []
        for ext, weight in sorted(languages.items()):
            if min_weight is not None and weight < min_weight:
                continue
            if max_weight is not None and weight > max_weight:
                continue
            filtered.append((ext, weight))
        
        if not filtered:
            print("No languages match the filters.")
            return
        
        print(f"\nFound {len(filtered)} languages:\n")
        print(f"{'Extension':<20} {'Weight':<10}")
        print("-" * 30)
        
        for ext, weight in filtered:
            print(f"{ext:<20} {weight:<10.2f}")
        
        print(f"\nTotal: {len(filtered)} languages")
    
    def add_language(self, extension: str, weight: float) -> None:
        """Add a new programming language."""
        languages = self.load_languages()
        
        if extension in languages:
            print(f"✗ Language '{extension}' already exists. Use 'update' to modify it.")
            sys.exit(1)
        
        languages[extension] = weight
        self.save_languages(languages)
        print(f"✓ Added language '{extension}' with weight {weight}")
    
    def update_language(self, extension: str, weight: float) -> None:
        """Update an existing programming language weight."""
        languages = self.load_languages()
        
        if extension not in languages:
            print(f"✗ Language '{extension}' not found.")
            sys.exit(1)
        
        old_weight = languages[extension]
        languages[extension] = weight
        self.save_languages(languages)
        print(f"✓ Updated weight for '{extension}' from {old_weight} to {weight}")
    
    def remove_language(self, extension: str, force: bool = False) -> None:
        """Remove a programming language."""
        languages = self.load_languages()
        
        if extension not in languages:
            print(f"✗ Language '{extension}' not found.")
            sys.exit(1)
        
        if not force:
            confirm = input(f"Are you sure you want to remove '{extension}'? (yes/no): ")
            if confirm.lower() not in ['yes', 'y']:
                print("Cancelled.")
                return
        
        del languages[extension]
        self.save_languages(languages)
        print(f"✓ Removed language '{extension}'")
    
    def get_language(self, extension: str) -> None:
        """Get details of a specific language."""
        languages = self.load_languages()
        
        if extension not in languages:
            print(f"✗ Language '{extension}' not found.")
            sys.exit(1)
        
        weight = languages[extension]
        print(f"\nLanguage: {extension}")
        print(f"Weight: {weight}")
    
    # Utility functions
    def stats(self) -> None:
        """Display statistics about repositories and languages."""
        repos = self.load_repos()
        languages = self.load_languages()
        
        total_repos = len(repos)
        active_repos = sum(1 for r in repos.values() if 'inactiveAt' not in r)
        inactive_repos = total_repos - active_repos
        
        if repos:
            weights = [r.get('weight', 0) for r in repos.values()]
            avg_weight = sum(weights) / len(weights)
            max_weight = max(weights)
            min_weight = min(weights)
        else:
            avg_weight = max_weight = min_weight = 0
        
        print("\n=== Repository Statistics ===")
        print(f"Total repositories: {total_repos}")
        print(f"Active: {active_repos}")
        print(f"Inactive: {inactive_repos}")
        print(f"Average weight: {avg_weight:.2f}")
        print(f"Max weight: {max_weight:.2f}")
        print(f"Min weight: {min_weight:.2f}")
        
        print("\n=== Language Statistics ===")
        print(f"Total languages: {len(languages)}")
        if languages:
            lang_weights = list(languages.values())
            print(f"Average weight: {sum(lang_weights) / len(lang_weights):.2f}")
            print(f"Max weight: {max(lang_weights):.2f}")
            print(f"Min weight: {min(lang_weights):.2f}")
    
    def validate(self) -> None:
        """Validate JSON files."""
        errors = []
        
        # Validate repositories
        try:
            repos = self.load_repos()
            for name, data in repos.items():
                if not isinstance(data, dict):
                    errors.append(f"Repository '{name}': data must be a dictionary")
                elif 'weight' not in data:
                    errors.append(f"Repository '{name}': missing 'weight' field")
                elif not isinstance(data['weight'], (int, float)):
                    errors.append(f"Repository '{name}': weight must be a number")
        except json.JSONDecodeError as e:
            errors.append(f"Repositories file: Invalid JSON - {e}")
        except Exception as e:
            errors.append(f"Repositories file: {e}")
        
        # Validate languages
        try:
            languages = self.load_languages()
            for ext, weight in languages.items():
                if not isinstance(weight, (int, float)):
                    errors.append(f"Language '{ext}': weight must be a number")
        except json.JSONDecodeError as e:
            errors.append(f"Languages file: Invalid JSON - {e}")
        except Exception as e:
            errors.append(f"Languages file: {e}")
        
        if errors:
            print("✗ Validation failed:\n")
            for error in errors:
                print(f"  - {error}")
            sys.exit(1)
        else:
            print("✓ All files are valid")


def main():
    """Main CLI entry point."""
    parser = argparse.ArgumentParser(
        description="Manage master repositories and programming language weights",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Repository management
  %(prog)s repo list
  %(prog)s repo list --inactive-only
  %(prog)s repo list --min-weight 1.0 --max-weight 10.0
  %(prog)s repo add owner/repo 5.5
  %(prog)s repo update owner/repo --weight 6.0
  %(prog)s repo update owner/repo --mark-inactive
  %(prog)s repo update owner/repo --mark-active
  %(prog)s repo update owner/repo --add-branches dev/v0.7.0 main
  %(prog)s repo update owner/repo --remove-branches dev
  %(prog)s repo update owner/repo --remove-branches
  %(prog)s repo remove owner/repo
  %(prog)s repo get owner/repo
  
  # Language management
  %(prog)s lang list
  %(prog)s lang list --min-weight 1.0
  %(prog)s lang add py 1.75
  %(prog)s lang update py 2.0
  %(prog)s lang remove py
  %(prog)s lang get py
  
  # Utilities
  %(prog)s stats
  %(prog)s validate
        """
    )
    
    subparsers = parser.add_subparsers(dest='command', help='Command to execute')
    
    # Repository commands
    repo_parser = subparsers.add_parser('repo', help='Manage repositories')
    repo_subparsers = repo_parser.add_subparsers(dest='repo_command', help='Repository command')
    
    # repo list
    repo_list = repo_subparsers.add_parser('list', help='List repositories')
    repo_list.add_argument('--inactive-only', action='store_true', help='Show only inactive repositories')
    repo_list.add_argument('--active-only', action='store_true', help='Show only active repositories')
    repo_list.add_argument('--min-weight', type=float, help='Minimum weight filter')
    repo_list.add_argument('--max-weight', type=float, help='Maximum weight filter')
    
    # repo add
    repo_add = repo_subparsers.add_parser('add', help='Add a repository')
    repo_add.add_argument('name', help='Repository name (e.g., owner/repo)')
    repo_add.add_argument('weight', type=float, help='Repository weight')
    
    # repo update
    repo_update = repo_subparsers.add_parser('update', help='Update a repository')
    repo_update.add_argument('name', help='Repository name')
    repo_update.add_argument('--weight', type=float, help='New weight')
    repo_update.add_argument('--mark-inactive', action='store_true', help='Mark as inactive')
    repo_update.add_argument('--mark-active', action='store_true', help='Mark as active')
    repo_update.add_argument('--add-branches', nargs='+', help='Add acceptable branches (e.g., dev/v0.7.0)')
    repo_update.add_argument('--remove-branches', nargs='*', help='Remove specific branches, or all if no branches specified')
    
    # repo remove
    repo_remove = repo_subparsers.add_parser('remove', help='Remove a repository')
    repo_remove.add_argument('name', help='Repository name')
    repo_remove.add_argument('--force', action='store_true', help='Skip confirmation')
    
    # repo get
    repo_get = repo_subparsers.add_parser('get', help='Get repository details')
    repo_get.add_argument('name', help='Repository name')
    
    # Language commands
    lang_parser = subparsers.add_parser('lang', help='Manage programming languages')
    lang_subparsers = lang_parser.add_subparsers(dest='lang_command', help='Language command')
    
    # lang list
    lang_list = lang_subparsers.add_parser('list', help='List languages')
    lang_list.add_argument('--min-weight', type=float, help='Minimum weight filter')
    lang_list.add_argument('--max-weight', type=float, help='Maximum weight filter')
    
    # lang add
    lang_add = lang_subparsers.add_parser('add', help='Add a language')
    lang_add.add_argument('extension', help='File extension (e.g., py, js)')
    lang_add.add_argument('weight', type=float, help='Language weight')
    
    # lang update
    lang_update = lang_subparsers.add_parser('update', help='Update a language')
    lang_update.add_argument('extension', help='File extension')
    lang_update.add_argument('weight', type=float, help='New weight')
    
    # lang remove
    lang_remove = lang_subparsers.add_parser('remove', help='Remove a language')
    lang_remove.add_argument('extension', help='File extension')
    lang_remove.add_argument('--force', action='store_true', help='Skip confirmation')
    
    # lang get
    lang_get = lang_subparsers.add_parser('get', help='Get language details')
    lang_get.add_argument('extension', help='File extension')
    
    # Utility commands
    subparsers.add_parser('stats', help='Display statistics')
    subparsers.add_parser('validate', help='Validate JSON files')
    
    args = parser.parse_args()
    
    if not args.command:
        parser.print_help()
        sys.exit(1)
    
    manager = WeightsManager()
    
    # Repository commands
    if args.command == 'repo':
        if not args.repo_command:
            repo_parser.print_help()
            sys.exit(1)
        
        if args.repo_command == 'list':
            manager.list_repos(
                inactive_only=args.inactive_only,
                active_only=args.active_only,
                min_weight=args.min_weight,
                max_weight=args.max_weight
            )
        elif args.repo_command == 'add':
            manager.add_repo(args.name, args.weight)
        elif args.repo_command == 'update':
            manager.update_repo(
                args.name,
                weight=args.weight,
                mark_inactive=args.mark_inactive,
                mark_active=args.mark_active,
                add_branches=args.add_branches,
                remove_branches=args.remove_branches
            )
        elif args.repo_command == 'remove':
            manager.remove_repo(args.name, force=args.force)
        elif args.repo_command == 'get':
            manager.get_repo(args.name)
    
    # Language commands
    elif args.command == 'lang':
        if not args.lang_command:
            lang_parser.print_help()
            sys.exit(1)
        
        if args.lang_command == 'list':
            manager.list_languages(
                min_weight=args.min_weight,
                max_weight=args.max_weight
            )
        elif args.lang_command == 'add':
            manager.add_language(args.extension, args.weight)
        elif args.lang_command == 'update':
            manager.update_language(args.extension, args.weight)
        elif args.lang_command == 'remove':
            manager.remove_language(args.extension, force=args.force)
        elif args.lang_command == 'get':
            manager.get_language(args.extension)
    
    # Utility commands
    elif args.command == 'stats':
        manager.stats()
    elif args.command == 'validate':
        manager.validate()


if __name__ == '__main__':
    main()
