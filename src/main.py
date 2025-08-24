# Cobradle The Ultimate Downloader
# Developed by JohnVictoryz and ThAnaSiS Protected under the M.I.T License 

import asyncio
import argparse
import json
import logging
import mimetypes
import os
import platform
import re
import shutil
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Union, Any
from urllib.parse import urlparse, unquote
import hashlib
import signal

# Auto-install required packages
def install_requirements():
    required_packages = ['requests', 'aiohttp', 'aiofiles', 'tqdm']
    for package in required_packages:
        try:
            __import__(package)
        except ImportError:
            print(f"Installing {package}...")
            import subprocess
            subprocess.check_call([sys.executable, "-m", "pip", "install", package])

install_requirements()

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import aiohttp
import aiofiles
from tqdm import tqdm

# Configuration
CONFIG_DIR = Path.home() / '.cobradle'
CONFIG_FILE = CONFIG_DIR / 'config.json'
HISTORY_FILE = CONFIG_DIR / 'history.json'
QUEUE_FILE = CONFIG_DIR / 'queue.json'
TEMP_DIR = CONFIG_DIR / 'temp'
LOG_DIR = CONFIG_DIR / 'logs'

# Ensure directories exist
for dir_path in [CONFIG_DIR, TEMP_DIR, LOG_DIR]:
    dir_path.mkdir(exist_ok=True)

@dataclass
class DownloadConfig:
    """Configuration for downloads"""
    max_concurrent_downloads: int = 5
    max_connections_per_download: int = 8
    chunk_size: int = 8192
    timeout: int = 30
    retries: int = 3
    user_agent: str = "Cobradle/1.0 (Advanced Downloader)"
    output_dir: str = str(Path.cwd() / "downloads")
    temp_dir: str = str(TEMP_DIR)
    resume: bool = True
    verify_ssl: bool = True
    use_compression: bool = True
    memory_limit: int = 500  # MB
    bandwidth_limit: Optional[int] = None  # KB/s

class FileDownloader:
    """Handles individual file downloads with threading and resume capability"""
    
    def __init__(self, config: DownloadConfig):
        self.config = config
        self.session = self._create_session()
        
    def _create_session(self) -> requests.Session:
        """Create configured requests session"""
        session = requests.Session()
        
        retry_strategy = Retry(
            total=self.config.retries,
            backoff_factor=1,
            status_forcelist=[429, 500, 502, 503, 504],
        )
        
        adapter = HTTPAdapter(max_retries=retry_strategy)
        session.mount("http://", adapter)
        session.mount("https://", adapter)
        
        session.headers.update({
            'User-Agent': self.config.user_agent,
            'Accept-Encoding': 'gzip, deflate' if self.config.use_compression else 'identity',
        })
        
        return session
    
    def get_file_info(self, url: str) -> dict:
        """Get file information without downloading"""
        try:
            response = self.session.head(url, timeout=self.config.timeout, verify=self.config.verify_ssl)
            response.raise_for_status()
            
            filename = self._extract_filename(url, response.headers)
            size = int(response.headers.get('content-length', 0))
            content_type = response.headers.get('content-type', 'application/octet-stream')
            
            return {
                'filename': filename,
                'size': size,
                'content_type': content_type,
                'supports_resume': 'accept-ranges' in response.headers
            }
        except Exception as e:
            logging.error(f"Error getting file info for {url}: {e}")
            return {'filename': self._extract_filename(url), 'size': 0, 'content_type': 'unknown', 'supports_resume': False}
    
    def _extract_filename(self, url: str, headers: dict = None) -> str:
        """Extract filename from URL or headers"""
        if headers and 'content-disposition' in headers:
            content_disposition = headers['content-disposition']
            filename_match = re.search(r'filename[*]?=["\']?([^"\';\r\n]+)["\']?', content_disposition)
            if filename_match:
                return filename_match.group(1).strip()
        
        parsed_url = urlparse(url)
        filename = unquote(parsed_url.path.split('/')[-1])
        
        if not filename or '.' not in filename:
            filename = f"download_{int(time.time())}"
            
        return filename
    
    def download_file(self, url: str, output_path: str = None, progress_callback=None) -> bool:
        """Download a single file with progress tracking"""
        try:
            file_info = self.get_file_info(url)
            filename = file_info['filename']
            file_size = file_info['size']
            
            if not output_path:
                output_path = Path(self.config.output_dir) / filename
            else:
                output_path = Path(output_path)
                
            output_path.parent.mkdir(parents=True, exist_ok=True)
            
            # Check if file already exists and supports resume
            resume_pos = 0
            mode = 'wb'
            if self.config.resume and output_path.exists():
                resume_pos = output_path.stat().st_size
                if resume_pos < file_size:
                    mode = 'ab'
                    logging.info(f"Resuming download from byte {resume_pos}")
                else:
                    logging.info(f"File already complete: {filename}")
                    return True
            
            headers = {}
            if resume_pos > 0:
                headers['Range'] = f'bytes={resume_pos}-'
            
            # Start download
            response = self.session.get(
                url, 
                headers=headers, 
                stream=True, 
                timeout=self.config.timeout,
                verify=self.config.verify_ssl
            )
            response.raise_for_status()
            
            total_size = file_size if resume_pos == 0 else file_size - resume_pos
            downloaded = 0
            
            with open(output_path, mode) as f:
                with tqdm(
                    total=total_size,
                    initial=0,
                    unit='B',
                    unit_scale=True,
                    desc=filename,
                    disable=progress_callback is not None
                ) as pbar:
                    
                    for chunk in response.iter_content(chunk_size=self.config.chunk_size):
                        if chunk:
                            f.write(chunk)
                            downloaded += len(chunk)
                            pbar.update(len(chunk))
                            
                            if progress_callback:
                                progress_callback(downloaded, total_size, filename)
                            
                            # Apply bandwidth limiting
                            if self.config.bandwidth_limit:
                                time.sleep(len(chunk) / (self.config.bandwidth_limit * 1024))
            
            logging.info(f"Successfully downloaded: {filename}")
            self._save_to_history(url, str(output_path), file_size)
            return True
            
        except Exception as e:
            logging.error(f"Error downloading {url}: {e}")
            return False
    
    def _save_to_history(self, url: str, filepath: str, size: int):
        """Save download to history"""
        history = []
        if HISTORY_FILE.exists():
            try:
                with open(HISTORY_FILE, 'r') as f:
                    history = json.load(f)
            except:
                pass
        
        history.append({
            'url': url,
            'filepath': filepath,
            'size': size,
            'timestamp': datetime.now().isoformat(),
            'status': 'completed'
        })
        
        with open(HISTORY_FILE, 'w') as f:
            json.dump(history[-1000:], f, indent=2)  # Keep last 1000 entries

class BatchDownloader:
    """Handles batch downloads with parallel processing"""
    
    def __init__(self, config: DownloadConfig):
        self.config = config
        self.downloader = FileDownloader(config)
        
    def download_batch(self, urls: List[str], output_dir: str = None) -> dict:
        """Download multiple URLs with parallel processing"""
        if output_dir:
            self.config.output_dir = output_dir
            
        results = {'success': [], 'failed': []}
        
        with ThreadPoolExecutor(max_workers=self.config.max_concurrent_downloads) as executor:
            future_to_url = {
                executor.submit(self.downloader.download_file, url): url 
                for url in urls
            }
            
            for future in as_completed(future_to_url):
                url = future_to_url[future]
                try:
                    success = future.result()
                    if success:
                        results['success'].append(url)
                        print(f"✓ Successfully downloaded: {url}")
                    else:
                        results['failed'].append(url)
                        print(f"✗ Failed to download: {url}")
                except Exception as e:
                    results['failed'].append(url)
                    print(f"✗ Error downloading {url}: {e}")
        
        return results

class QueueManager:
    """Manages download queue with persistence"""
    
    def __init__(self):
        self.queue = self._load_queue()
        
    def _load_queue(self) -> list:
        """Load queue from file"""
        if QUEUE_FILE.exists():
            try:
                with open(QUEUE_FILE, 'r') as f:
                    return json.load(f)
            except:
                pass
        return []
    
    def _save_queue(self):
        """Save queue to file"""
        with open(QUEUE_FILE, 'w') as f:
            json.dump(self.queue, f, indent=2)
    
    def add(self, url: str, options: dict = None):
        """Add URL to queue"""
        item = {
            'url': url,
            'options': options or {},
            'added': datetime.now().isoformat(),
            'status': 'pending'
        }
        self.queue.append(item)
        self._save_queue()
        print(f"Added to queue: {url}")
    
    def list_queue(self):
        """List current queue"""
        if not self.queue:
            print("Queue is empty")
            return
            
        print(f"\nDownload Queue ({len(self.queue)} items):")
        print("-" * 50)
        for i, item in enumerate(self.queue, 1):
            status = item.get('status', 'pending')
            url = item['url'][:60] + '...' if len(item['url']) > 60 else item['url']
            print(f"{i:2d}. [{status:8s}] {url}")
    
    def remove(self, index: int):
        """Remove item from queue"""
        try:
            removed = self.queue.pop(index - 1)
            self._save_queue()
            print(f"Removed from queue: {removed['url']}")
        except IndexError:
            print(f"Invalid queue index: {index}")
    
    def clear(self):
        """Clear entire queue"""
        self.queue.clear()
        self._save_queue()
        print("Queue cleared")
    
    def process_queue(self, config: DownloadConfig):
        """Process all items in queue"""
        if not self.queue:
            print("Queue is empty")
            return
        
        downloader = BatchDownloader(config)
        pending_urls = [item['url'] for item in self.queue if item.get('status') == 'pending']
        
        if not pending_urls:
            print("No pending items in queue")
            return
        
        print(f"Processing {len(pending_urls)} items from queue...")
        results = downloader.download_batch(pending_urls)
        
        # Update queue status
        for item in self.queue:
            if item['url'] in results['success']:
                item['status'] = 'completed'
            elif item['url'] in results['failed']:
                item['status'] = 'failed'
        
        self._save_queue()
        
        print(f"\nQueue processing complete:")
        print(f"  Success: {len(results['success'])}")
        print(f"  Failed: {len(results['failed'])}")

def create_parser():
    """Create argument parser with working commands"""
    parser = argparse.ArgumentParser(
        prog='cobradle',
        description='Cobradle - Advanced Multi-threaded Downloader',
        formatter_class=argparse.RawDescriptionHelpFormatter
    )
    
    subparsers = parser.add_subparsers(dest='command', help='Available commands')
    
    # Download command
    download_parser = subparsers.add_parser('download', aliases=['dl', 'get'], help='Download files')
    download_parser.add_argument('urls', nargs='+', help='URLs to download')
    download_parser.add_argument('-o', '--output', help='Output directory')
    download_parser.add_argument('-j', '--parallel', type=int, default=5, help='Max parallel downloads')
    download_parser.add_argument('-c', '--continue', dest='resume', action='store_true', help='Resume partial downloads')
    download_parser.add_argument('--chunk-size', type=int, default=8192, help='Chunk size in bytes')
    download_parser.add_argument('--timeout', type=int, default=30, help='Connection timeout')
    download_parser.add_argument('--retries', type=int, default=3, help='Number of retries')
    download_parser.add_argument('--user-agent', help='Custom user agent')
    download_parser.add_argument('--no-ssl-verify', action='store_true', help='Disable SSL verification')
    
    # Batch command
    batch_parser = subparsers.add_parser('batch', aliases=['b'], help='Batch download from file')
    batch_parser.add_argument('file', help='File containing URLs (one per line)')
    batch_parser.add_argument('-o', '--output', help='Output directory')
    batch_parser.add_argument('-j', '--parallel', type=int, default=5, help='Max parallel downloads')
    
    # Queue management
    queue_parser = subparsers.add_parser('queue', aliases=['q'], help='Manage download queue')
    queue_subparsers = queue_parser.add_subparsers(dest='queue_action')
    queue_subparsers.add_parser('list', help='List queued downloads')
    queue_subparsers.add_parser('clear', help='Clear queue')
    queue_subparsers.add_parser('process', help='Process queue')
    
    add_parser = queue_subparsers.add_parser('add', help='Add URL to queue')
    add_parser.add_argument('url', help='URL to add')
    
    remove_parser = queue_subparsers.add_parser('remove', help='Remove from queue')
    remove_parser.add_argument('index', type=int, help='Queue index to remove')
    
    # History
    history_parser = subparsers.add_parser('history', aliases=['h'], help='Download history')
    history_subparsers = history_parser.add_subparsers(dest='history_action')
    history_subparsers.add_parser('list', help='List download history')
    history_subparsers.add_parser('clear', help='Clear history')
    
    # Config
    config_parser = subparsers.add_parser('config', help='Configuration')
    config_subparsers = config_parser.add_subparsers(dest='config_action')
    config_subparsers.add_parser('show', help='Show config')
    
    set_parser = config_subparsers.add_parser('set', help='Set config value')
    set_parser.add_argument('key', help='Config key')
    set_parser.add_argument('value', help='Config value')
    
    # File operations
    file_parser = subparsers.add_parser('file', help='File operations')
    file_subparsers = file_parser.add_subparsers(dest='file_action')
    
    hash_parser = file_subparsers.add_parser('hash', help='Calculate file hash')
    hash_parser.add_argument('file', help='File to hash')
    hash_parser.add_argument('--algorithm', choices=['md5', 'sha1', 'sha256'], default='sha256')
    
    # Info
    info_parser = subparsers.add_parser('info', help='System information')
    info_parser.add_argument('--system', action='store_true', help='Show system info')
    
    # Global options
    parser.add_argument('--verbose', '-v', action='count', default=0, help='Increase verbosity')
    parser.add_argument('--quiet', '-q', action='store_true', help='Quiet mode')
    parser.add_argument('--version', action='version', version='Cobradle 1.0.0')
    
    return parser

def load_config() -> DownloadConfig:
    """Load configuration from file"""
    config = DownloadConfig()
    
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE, 'r') as f:
                config_data = json.load(f)
                for key, value in config_data.items():
                    if hasattr(config, key):
                        setattr(config, key, value)
        except Exception as e:
            logging.warning(f"Error loading config: {e}")
    
    return config

def save_config(config: DownloadConfig):
    """Save configuration to file"""
    with open(CONFIG_FILE, 'w') as f:
        json.dump(asdict(config), f, indent=2)

def handle_download_command(args, config: DownloadConfig):
    """Handle download command with real functionality"""
    # Apply command-line overrides to config
    if args.parallel:
        config.max_concurrent_downloads = args.parallel
    if args.chunk_size:
        config.chunk_size = args.chunk_size
    if args.timeout:
        config.timeout = args.timeout
    if args.retries:
        config.retries = args.retries
    if args.user_agent:
        config.user_agent = args.user_agent
    if args.no_ssl_verify:
        config.verify_ssl = False
    if args.resume:
        config.resume = True
    if args.output:
        config.output_dir = args.output
    
    # Ensure output directory exists
    Path(config.output_dir).mkdir(parents=True, exist_ok=True)
    
    if len(args.urls) == 1:
        # Single file download
        downloader = FileDownloader(config)
        success = downloader.download_file(args.urls[0])
        if success:
            print(f"✓ Download completed successfully")
        else:
            print(f"✗ Download failed")
            return 1
    else:
        # Batch download
        batch_downloader = BatchDownloader(config)
        results = batch_downloader.download_batch(args.urls)
        
        print(f"\nDownload Summary:")
        print(f"  Success: {len(results['success'])}")
        print(f"  Failed: {len(results['failed'])}")
        
        if results['failed']:
            print(f"\nFailed downloads:")
            for url in results['failed']:
                print(f"  - {url}")
            return 1
    
    return 0

def handle_batch_command(args, config: DownloadConfig):
    """Handle batch download command"""
    if not Path(args.file).exists():
        print(f"Error: File not found: {args.file}")
        return 1
    
    try:
        with open(args.file, 'r') as f:
            urls = [line.strip() for line in f if line.strip() and not line.startswith('#')]
    except Exception as e:
        print(f"Error reading file: {e}")
        return 1
    
    if not urls:
        print("No URLs found in file")
        return 1
    
    print(f"Found {len(urls)} URLs to download")
    
    # Apply overrides
    if args.parallel:
        config.max_concurrent_downloads = args.parallel
    if args.output:
        config.output_dir = args.output
    
    batch_downloader = BatchDownloader(config)
    results = batch_downloader.download_batch(urls)
    
    print(f"\nBatch Download Summary:")
    print(f"  Success: {len(results['success'])}")
    print(f"  Failed: {len(results['failed'])}")
    
    return 0 if not results['failed'] else 1

def handle_queue_command(args):
    """Handle queue management commands"""
    queue_manager = QueueManager()
    
    if args.queue_action == 'list':
        queue_manager.list_queue()
    elif args.queue_action == 'add':
        queue_manager.add(args.url)
    elif args.queue_action == 'remove':
        queue_manager.remove(args.index)
    elif args.queue_action == 'clear':
        queue_manager.clear()
    elif args.queue_action == 'process':
        config = load_config()
        queue_manager.process_queue(config)
    else:
        print("Available queue actions: list, add, remove, clear, process")
    
    return 0

def handle_history_command(args):
    """Handle history commands"""
    if args.history_action == 'list':
        if not HISTORY_FILE.exists():
            print("No download history found")
            return 0
        
        try:
            with open(HISTORY_FILE, 'r') as f:
                history = json.load(f)
        except:
            print("Error reading history file")
            return 1
        
        if not history:
            print("Download history is empty")
            return 0
        
        print(f"\nDownload History ({len(history)} items):")
        print("-" * 80)
        for i, item in enumerate(history[-10:], 1):  # Show last 10
            timestamp = datetime.fromisoformat(item['timestamp']).strftime('%Y-%m-%d %H:%M')
            size = item.get('size', 0)
            size_str = f"{size/1024/1024:.1f}MB" if size > 0 else "Unknown"
            url = item['url'][:50] + '...' if len(item['url']) > 50 else item['url']
            print(f"{i:2d}. [{timestamp}] {size_str:>10s} {url}")
    
    elif args.history_action == 'clear':
        if HISTORY_FILE.exists():
            HISTORY_FILE.unlink()
        print("History cleared")
    
    return 0

def handle_config_command(args):
    """Handle configuration commands"""
    config = load_config()
    
    if args.config_action == 'show':
        print("Current Configuration:")
        print("-" * 30)
        for key, value in asdict(config).items():
            print(f"{key:25s}: {value}")
    
    elif args.config_action == 'set':
        if hasattr(config, args.key):
            # Try to convert value to appropriate type
            old_value = getattr(config, args.key)
            try:
                if isinstance(old_value, bool):
                    new_value = args.value.lower() in ('true', 'yes', '1')
                elif isinstance(old_value, int):
                    new_value = int(args.value)
                else:
                    new_value = args.value
                
                setattr(config, args.key, new_value)
                save_config(config)
                print(f"Set {args.key} = {new_value}")
            except ValueError:
                print(f"Invalid value for {args.key}: {args.value}")
                return 1
        else:
            print(f"Unknown config key: {args.key}")
            return 1
    
    return 0

def handle_file_command(args):
    """Handle file operations"""
    if args.file_action == 'hash':
        if not Path(args.file).exists():
            print(f"File not found: {args.file}")
            return 1
        
        hash_func = hashlib.new(args.algorithm)
        try:
            with open(args.file, 'rb') as f:
                for chunk in iter(lambda: f.read(8192), b""):
                    hash_func.update(chunk)
            
            print(f"{args.algorithm.upper()}: {hash_func.hexdigest()}")
        except Exception as e:
            print(f"Error calculating hash: {e}")
            return 1
    
    return 0

def handle_info_command(args):
    """Handle info command"""
    if args.system:
        print("System Information:")
        print(f"  Platform: {platform.platform()}")
        print(f"  Python: {sys.version.split()[0]}")
        print(f"  Architecture: {platform.architecture()[0]}")
        print(f"  Processor: {platform.processor()}")
        print(f"  Machine: {platform.machine()}")
    else:
        print("Cobradle - Advanced Multi-threaded Downloader")
        print(f"Version: 1.0.0")
        print(f"Config directory: {CONFIG_DIR}")
        print(f"Python version: {sys.version.split()[0]}")
    
    return 0

def main():
    """Main entry point"""
    parser = create_parser()
    
    if len(sys.argv) == 1:
        parser.print_help()
        return 0
    
    args = parser.parse_args()
    
    # Set up logging
    log_level = logging.WARNING
    if args.verbose == 1:
        log_level = logging.INFO
    elif args.verbose >= 2:
        log_level = logging.DEBUG
    elif args.quiet:
        log_level = logging.ERROR
    
    logging.basicConfig(
        level=log_level,
        format='%(asctime)s - %(levelname)s - %(message)s'
    )
    
    # Load configuration
    config = load_config()
    
    try:
        # Handle commands
        if args.command in ['download', 'dl', 'get']:
            return handle_download_command(args, config)
        elif args.command in ['batch', 'b']:
            return handle_batch_command(args, config)
        elif args.command in ['queue', 'q']:
            return handle_queue_command(args)
        elif args.command in ['history', 'h']:
            return handle_history_command(args)
        elif args.command == 'config':
            return handle_config_command(args)
        elif args.command == 'file':
            return handle_file_command(args)
        elif args.command == 'info':
            return handle_info_command(args)
        else:
            parser.print_help()
            return 0
            
    except KeyboardInterrupt:
        print("\nOperation cancelled by user.")
        return 130
    except Exception as e:
        print(f"Error: {e}")
        if args.verbose >= 2:
            import traceback
            traceback.print_exc()
        return 1

if __name__ == "__main__":
    sys.exit(main())
