#!/usr/bin/env python3
"""
Cobradle - Advanced Multi-threaded Downloader
A powerful CLI downloader with extensive command options and interactive TUI.
"""

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
    required_packages = ['requests', 'aiohttp', 'aiofiles', 'tqdm', 'rich', 'textual']
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
from rich.console import Console
from rich.progress import Progress, TaskID, SpinnerColumn, TextColumn, BarColumn, TimeRemainingColumn, TransferSpeedColumn, DownloadColumn
from rich.table import Table
from rich.panel import Panel
from rich.layout import Layout
from rich.live import Live
from rich.text import Text
from rich import print as rprint

# Textual imports for TUI
from textual.app import App, ComposeResult
from textual.containers import Container, Horizontal, Vertical, ScrollableContainer
from textual.widgets import (
    Header,
    Footer,
    Static,
    Input,
    Button,
    Select,
    ContentSwitcher,
    Label,
    DataTable,
    Tree,
    Markdown,
    TextArea
)
from textual.screen import Screen, ModalScreen
from textual.binding import Binding
from textual import on, work
from textual.reactive import reactive
from textual.message import Message

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
    ui_mode: str = "auto"  # "cli", "tui", "auto"
    theme: str = "dark"
    auto_organize: bool = False
    notification_enabled: bool = True

class DownloadItem:
    """Represents a download item with status tracking"""
    
    def __init__(self, url: str, filename: str = None, output_dir: str = None):
        self.url = url
        self.filename = filename or self._extract_filename(url)
        self.output_dir = output_dir or "downloads"
        self.size = 0
        self.downloaded = 0
        self.speed = 0
        self.eta = 0
        self.status = "pending"  # pending, downloading, completed, failed, paused
        self.error = None
        self.progress = 0.0
        self.start_time = None
        self.end_time = None
        
    def _extract_filename(self, url: str) -> str:
        """Extract filename from URL"""
        parsed_url = urlparse(url)
        filename = unquote(parsed_url.path.split('/')[-1])
        if not filename or '.' not in filename:
            filename = f"download_{int(time.time())}"
        return filename
    
    def update_progress(self, downloaded: int, size: int, speed: float = 0):
        """Update download progress"""
        self.downloaded = downloaded
        self.size = size
        self.speed = speed
        self.progress = (downloaded / size * 100) if size > 0 else 0
        if speed > 0 and size > downloaded:
            self.eta = (size - downloaded) / speed

class FileDownloader:
    """Handles individual file downloads with threading and resume capability"""
    
    def __init__(self, config: DownloadConfig):
        self.config = config
        self.session = self._create_session()
        self.active_downloads = {}
        
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
    
    def download_file(self, download_item: DownloadItem, progress_callback=None) -> bool:
        """Download a single file with progress tracking"""
        try:
            download_item.status = "downloading"
            download_item.start_time = time.time()
            
            file_info = self.get_file_info(download_item.url)
            download_item.filename = file_info['filename']
            download_item.size = file_info['size']
            
            output_path = Path(download_item.output_dir) / download_item.filename
            output_path.parent.mkdir(parents=True, exist_ok=True)
            
            # Check if file already exists and supports resume
            resume_pos = 0
            mode = 'wb'
            if self.config.resume and output_path.exists():
                resume_pos = output_path.stat().st_size
                if resume_pos < download_item.size:
                    mode = 'ab'
                    logging.info(f"Resuming download from byte {resume_pos}")
                else:
                    logging.info(f"File already complete: {download_item.filename}")
                    download_item.status = "completed"
                    download_item.progress = 100.0
                    return True
            
            headers = {}
            if resume_pos > 0:
                headers['Range'] = f'bytes={resume_pos}-'
            
            # Start download
            response = self.session.get(
                download_item.url, 
                headers=headers, 
                stream=True, 
                timeout=self.config.timeout,
                verify=self.config.verify_ssl
            )
            response.raise_for_status()
            
            total_size = download_item.size if resume_pos == 0 else download_item.size - resume_pos
            downloaded = resume_pos
            last_update = time.time()
            
            with open(output_path, mode) as f:
                for chunk in response.iter_content(chunk_size=self.config.chunk_size):
                    if chunk and download_item.status == "downloading":
                        f.write(chunk)
                        downloaded += len(chunk)
                        
                        # Update progress periodically
                        current_time = time.time()
                        if current_time - last_update >= 0.1:  # Update every 100ms
                            speed = len(chunk) / (current_time - last_update) if current_time > last_update else 0
                            download_item.update_progress(downloaded, download_item.size, speed)
                            
                            if progress_callback:
                                progress_callback(download_item)
                            
                            last_update = current_time
                        
                        # Apply bandwidth limiting
                        if self.config.bandwidth_limit:
                            time.sleep(len(chunk) / (self.config.bandwidth_limit * 1024))
                    
                    elif download_item.status == "paused":
                        # Pause handling
                        while download_item.status == "paused":
                            time.sleep(0.1)
                    
                    elif download_item.status in ["cancelled", "failed"]:
                        break
            
            if download_item.status == "downloading":
                download_item.status = "completed"
                download_item.end_time = time.time()
                download_item.progress = 100.0
                logging.info(f"Successfully downloaded: {download_item.filename}")
                self._save_to_history(download_item)
                return True
            
            return False
            
        except Exception as e:
            download_item.status = "failed"
            download_item.error = str(e)
            logging.error(f"Error downloading {download_item.url}: {e}")
            return False
    
    def _save_to_history(self, download_item: DownloadItem):
        """Save download to history"""
        history = []
        if HISTORY_FILE.exists():
            try:
                with open(HISTORY_FILE, 'r') as f:
                    history = json.load(f)
            except:
                pass
        
        history.append({
            'url': download_item.url,
            'filename': download_item.filename,
            'output_dir': download_item.output_dir,
            'size': download_item.size,
            'timestamp': datetime.now().isoformat(),
            'duration': download_item.end_time - download_item.start_time if download_item.end_time and download_item.start_time else 0,
            'status': download_item.status
        })
        
        with open(HISTORY_FILE, 'w') as f:
            json.dump(history[-1000:], f, indent=2)  # Keep last 1000 entries

class BatchDownloader:
    """Handles batch downloads with parallel processing"""
    
    def __init__(self, config: DownloadConfig):
        self.config = config
        self.downloader = FileDownloader(config)
        self.download_items = []
        
    def add_downloads(self, urls: List[str], output_dir: str = None):
        """Add URLs to download list"""
        for url in urls:
            item = DownloadItem(url, output_dir=output_dir or self.config.output_dir)
            self.download_items.append(item)
    
    def download_batch(self, progress_callback=None) -> dict:
        """Download multiple URLs with parallel processing"""
        results = {'success': [], 'failed': []}
        
        with ThreadPoolExecutor(max_workers=self.config.max_concurrent_downloads) as executor:
            future_to_item = {
                executor.submit(self.downloader.download_file, item, progress_callback): item 
                for item in self.download_items
            }
            
            for future in as_completed(future_to_item):
                item = future_to_item[future]
                try:
                    success = future.result()
                    if success:
                        results['success'].append(item.url)
                    else:
                        results['failed'].append(item.url)
                except Exception as e:
                    item.status = "failed"
                    item.error = str(e)
                    results['failed'].append(item.url)
        
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
    
    def get_queue(self) -> list:
        """Get current queue"""
        return self.queue
    
    def remove(self, index: int):
        """Remove item from queue"""
        try:
            self.queue.pop(index)
            self._save_queue()
            return True
        except IndexError:
            return False
    
    def clear(self):
        """Clear entire queue"""
        self.queue.clear()
        self._save_queue()

# TUI Screens and Components
class AddDownloadModal(ModalScreen):
    """Modal for adding new downloads"""
    
    def compose(self) -> ComposeResult:
        yield Container(
            Static("Add New Download", classes="modal-title"),
            Input(placeholder="Enter URL(s), one per line", id="url-input"),
            Input(placeholder="Output directory (optional)", id="output-input"),
            Horizontal(
                Button("Add", variant="primary", id="add-btn"),
                Button("Cancel", variant="default", id="cancel-btn"),
                classes="modal-buttons"
            ),
            classes="modal-container"
        )
    
    @on(Button.Pressed, "#add-btn")
    def add_download(self):
        url_input = self.query_one("#url-input", Input)
        output_input = self.query_one("#output-input", Input)
        
        urls = [url.strip() for url in url_input.value.split('\n') if url.strip()]
        output_dir = output_input.value.strip() or None
        
        if urls:
            self.app.add_downloads(urls, output_dir)
        self.dismiss()
    
    @on(Button.Pressed, "#cancel-btn")
    def cancel(self):
        self.dismiss()

class SettingsModal(ModalScreen):
    """Modal for application settings"""
    
    def compose(self) -> ComposeResult:
        config = self.app.config
        yield Container(
            Static("Settings", classes="modal-title"),
            ScrollableContainer(
                Static("Download Settings", classes="section-title"),
                Horizontal(
                    Static("Max Concurrent Downloads:", classes="setting-label"),
                    Slider(1, 20, config.max_concurrent_downloads, id="max-downloads-slider"),
                ),
                Horizontal(
                    Static("Chunk Size (KB):", classes="setting-label"),
                    Slider(1, 128, config.chunk_size // 1024, id="chunk-size-slider"),
                ),
                Horizontal(
                    Static("Connection Timeout:", classes="setting-label"),
                    Slider(10, 300, config.timeout, id="timeout-slider"),
                ),
                Horizontal(
                    Static("Resume Downloads:", classes="setting-label"),
                    Switch(config.resume, id="resume-switch"),
                ),
                Horizontal(
                    Static("SSL Verification:", classes="setting-label"),
                    Switch(config.verify_ssl, id="ssl-switch"),
                ),
                Static("UI Settings", classes="section-title"),
                Horizontal(
                    Static("UI Mode:", classes="setting-label"),
                    Select(
                        [("Auto", "auto"), ("CLI", "cli"), ("TUI", "tui")],
                        value=config.ui_mode,
                        id="ui-mode-select"
                    ),
                ),
                Horizontal(
                    Static("Theme:", classes="setting-label"),
                    Select(
                        [("Dark", "dark"), ("Light", "light")],
                        value=config.theme,
                        id="theme-select"
                    ),
                ),
                Input(placeholder="User Agent", value=config.user_agent, id="user-agent-input"),
                Input(placeholder="Output Directory", value=config.output_dir, id="output-dir-input"),
                classes="settings-container"
            ),
            Horizontal(
                Button("Save", variant="primary", id="save-btn"),
                Button("Cancel", variant="default", id="cancel-btn"),
                classes="modal-buttons"
            ),
            classes="modal-container"
        )
    
    @on(Button.Pressed, "#save-btn")
    def save_settings(self):
        config = self.app.config
        
        # Update config from form
        config.max_concurrent_downloads = self.query_one("#max-downloads-slider", Slider).value
        config.chunk_size = self.query_one("#chunk-size-slider", Slider).value * 1024
        config.timeout = self.query_one("#timeout-slider", Slider).value
        config.resume = self.query_one("#resume-switch", Switch).value
        config.verify_ssl = self.query_one("#ssl-switch", Switch).value
        config.ui_mode = self.query_one("#ui-mode-select", Select).value
        config.theme = self.query_one("#theme-select", Select).value
        config.user_agent = self.query_one("#user-agent-input", Input).value
        config.output_dir = self.query_one("#output-dir-input", Input).value
        
        # Save config
        save_config(config)
        self.app.show_notification("Settings saved successfully!")
        self.dismiss()
    
    @on(Button.Pressed, "#cancel-btn")
    def cancel(self):
        self.dismiss()

class CobradelTUI(App):
    """Main TUI application"""
    
    CSS = """
    .modal-container {
        background: $surface;
        border: thick $primary;
        width: 80%;
        height: auto;
        margin: 2 4;
        padding: 1;
    }
    
    .modal-title {
        text-align: center;
        text-style: bold;
        color: $primary;
        margin-bottom: 1;
    }
    
    .modal-buttons {
        dock: bottom;
        height: 3;
        margin-top: 1;
    }
    
    .section-title {
        text-style: bold;
        color: $accent;
        margin: 1 0;
    }
    
    .setting-label {
        width: 25%;
        padding-right: 2;
    }
    
    .settings-container {
        height: 25;
    }
    
    #downloads-table {
        height: 1fr;
    }
    
    #queue-table {
        height: 1fr;
    }
    
    #history-table {
        height: 1fr;
    }
    
    #log-view {
        height: 10;
        border: solid $accent;
    }
    
    .status-pending { color: yellow; }
    .status-downloading { color: blue; }
    .status-completed { color: green; }
    .status-failed { color: red; }
    .status-paused { color: orange; }
    """
    
    BINDINGS = [
        Binding("ctrl+q", "quit", "Quit"),
        Binding("ctrl+n", "add_download", "Add Download"),
        Binding("ctrl+s", "settings", "Settings"),
        Binding("ctrl+p", "pause_resume", "Pause/Resume"),
        Binding("ctrl+c", "cancel_selected", "Cancel Selected"),
        Binding("f5", "refresh", "Refresh"),
    ]
    
    def __init__(self, config: DownloadConfig):
        super().__init__()
        self.config = config
        self.batch_downloader = BatchDownloader(config)
        self.queue_manager = QueueManager()
        self.download_items = []
        self.selected_download = None
        
    def compose(self) -> ComposeResult:
        """Create the TUI layout"""
        yield Header()
        
        with Container():
            with Tabs():
                with TabPane("Downloads", id="downloads-tab"):
                    yield Container(
                        Horizontal(
                            Button("Add Download", variant="primary", id="add-download-btn"),
                            Button("Pause/Resume", id="pause-resume-btn"),
                            Button("Cancel", id="cancel-btn"),
                            Button("Clear Completed", id="clear-completed-btn"),
                            classes="button-bar"
                        ),
                        DataTable(id="downloads-table"),
                        classes="tab-content"
                    )
                
                with TabPane("Queue", id="queue-tab"):
                    yield Container(
                        Horizontal(
                            Button("Process Queue", variant="primary", id="process-queue-btn"),
                            Button("Clear Queue", id="clear-queue-btn"),
                            Button("Add to Queue", id="add-to-queue-btn"),
                            classes="button-bar"
                        ),
                        DataTable(id="queue-table"),
                        classes="tab-content"
                    )
                
                with TabPane("History", id="history-tab"):
                    yield Container(
                        Horizontal(
                            Button("Refresh", id="refresh-history-btn"),
                            Button("Clear History", id="clear-history-btn"),
                            Button("Export", id="export-history-btn"),
                            classes="button-bar"
                        ),
                        DataTable(id="history-table"),
                        classes="tab-content"
                    )
                
                with TabPane("Settings", id="settings-tab"):
                    yield Container(
                        Button("Open Settings", variant="primary", id="settings-btn"),
                        Static("Quick Settings", classes="section-title"),
                        Horizontal(
                            Static("Max Downloads:"),
                            Slider(1, 20, self.config.max_concurrent_downloads, id="quick-max-downloads"),
                        ),
                        Horizontal(
                            Static("Auto-organize:"),
                            Switch(self.config.auto_organize, id="quick-auto-organize"),
                        ),
                        classes="tab-content"
                    )
        
        with Container(classes="log-container"):
            yield Log(id="log-view")
        
        yield Footer()
    
    def on_mount(self) -> None:
        """Initialize the application"""
        self.setup_tables()
        self.refresh_all_data()
        self.log("Cobradle TUI started successfully!")
    
    def setup_tables(self):
        """Setup data tables"""
        # Downloads table
        downloads_table = self.query_one("#downloads-table", DataTable)
        downloads_table.add_columns("Status", "Filename", "Progress", "Speed", "ETA", "Size")
        
        # Queue table
        queue_table = self.query_one("#queue-table", DataTable)
        queue_table.add_columns("Status", "URL", "Added", "Options")
        
        # History table
        history_table = self.query_one("#history-table", DataTable)
        history_table.add_columns("Filename", "Size", "Duration", "Date", "Status")
    
    def refresh_all_data(self):
        """Refresh all data displays"""
        self.refresh_downloads_table()
        self.refresh_queue_table()
        self.refresh_history_table()
    
    def refresh_downloads_table(self):
        """Update downloads table"""
        table = self.query_one("#downloads-table", DataTable)
        table.clear()
        
        for item in self.download_items:
            status_class = f"status-{item.status}"
            size_str = f"{item.size / 1024 / 1024:.1f}MB" if item.size > 0 else "Unknown"
            progress_str = f"{item.progress:.1f}%" if item.progress > 0 else "0%"
            speed_str = f"{item.speed / 1024:.1f} KB/s" if item.speed > 0 else "0 KB/s"
            eta_str = f"{int(item.eta)}s" if item.eta > 0 else "∞"
            
            table.add_row(
                Text(item.status.title(), style=status_class),
                item.filename[:30] + "..." if len(item.filename) > 30 else item.filename,
                progress_str,
                speed_str,
                eta_str,
                size_str
            )
    
    def refresh_queue_table(self):
        """Update queue table"""
        table = self.query_one("#queue-tle", DataTable)
        table.clear()
        
        for item in self.queue_manager.get_queue():
            url_short = item['url'][:50] + "..." if len(item['url']) > 50 else item['url']
            added_date = datetime.fromisoformat(item['added']).strftime('%Y-%m-%d %H:%M')
            options_str = str(len(item.get('options', {}))) + " options" if item.get('options') else "None"
            
            table.add_row(
                item['status'].title(),
                url_short,
                added_date,
                options_str
            )
    
    def refresh_history_table(self):
        """Update history table"""
        table = self.query_one("#history-table", DataTable)
        table.clear()
        
        if HISTORY_FILE.exists():
            try:
                with open(HISTORY_FILE, 'r') as f:
                    history = json.load(f)
                
                for item in history[-50:]:  # Show last 50 items
                    size_str = f"{item.get('size', 0) / 1024 / 1024:.1f}MB" if item.get('size', 0) > 0 else "Unknown"
                    duration_str = f"{item.get('duration', 0):.1f}s" if item.get('duration', 0) > 0 else "Unknown"
                    date_str = datetime.fromisoformat(item['timestamp']).strftime('%Y-%m-%d %H:%M')
                    
                    table.add_row(
                        item.get('filename', 'Unknown')[:30],
                        size_str,
                        duration_str,
                        date_str,
                        item.get('status', 'Unknown').title()
                    )
            except:
                pass
    
    def log(self, message: str):
        """Add message to log"""
        log_view = self.query_one("#log-view", Log)
        timestamp = datetime.now().strftime("%H:%M:%S")
        log_view.write_line(f"[{timestamp}] {message}")
    
    def show_notification(self, message: str):
        """Show notification message"""
        self.log(f"✓ {message}")
    
    def add_downloads(self, urls: List[re], output_dir: str = None):
        """Add new downloads"""
        for url in urls:
            item = DownloadItem(url, output_dir=output_dir or self.config.output_dir)
            self.download_items.append(item)
        
        self.batch_downloader.add_downloads(urls, output_dir)
        self.refresh_downloads_table()
        self.log(f"Added {len(urls)} download(s)")
        
        # Start downloads
        self.start_downloads()
    
    @work(exclusive=True)
    async def start_downloads(self):
        """Start downloading items"""
        def progress_callback(item: DownloadItem):
            self.call_from_thread(self.refresh_downloads_table)
        
        await asyncio.to_thread(
            self.batch_downloader.download_batch,
            progress_callback
        )
        
        self.call_from_thread(self.refresh_downloads_table)
        self.call_from_thread(self.log, "All downloads completed!")
    
    # Event handlers
    @on(Button.Pressed, "#add-download-btn")
    def action_add_download(self):
        self.push_screen(AddDownloadModal())
    
    @on(Button.Pressed, "#settings-btn")
    def action_settings(self):
        self.push_screen(SettingsModal())
    
    @on(Button.Pressed, "#process-queue-btn")
    def process_queue(self):
        queue = self.queue_manager.get_queue()
        pending_urls = [item['url'] for item in queue if item['status'] == 'pending']
        
        if pending_urls:
            self.add_downloads(pending_urls)
            self.log(f"Processing {len(pending_urls)} items from queue")
        else:
            self.log("No pending items in queue")
    
    @on(Button.Pressed, "#clear-queue-btn")
    def clear_queue(self):
        self.queue_manager.clear()
        self.refresh_queue_table()
        self.log("Queue cleared")
    
    @on(Button.Pressed, "#clear-history-btn")
    def clear_history(self):
        if HISTORY_FILE.exists():
            HISTORY_FILE.unlink()
        self.refresh_history_table()
        self.log("History cleared")
    
    @on(Button.Pressed, "#refresh-history-btn")
    def refresh_history(self):
        self.refresh_history_table()
        self.log("History refreshed")
    
    @on(Button.Pressed, "#clear-completed-btn")
    def clear_completed(self):
        self.download_items = [item for item in self.download_items if item.status not in ["completed", "failed"]]
        self.refresh_downloads_table()
        self.log("Cleared completed downloads")
    
    # Keyboard shortcuts
    def action_quit(self):
        """Quit the application"""
        self.exit()
    
    def action_add_download(self):
        """Add new download"""
        self.push_screen(AddDownloadModal())
    
    def action_settings(self):
        """Open settings"""
        self.push_screen(SettingsModal())
    
    def action_refresh(self):
        """Refresh all data"""
        self.refresh_all_data()
        self.log("Data refreshed")

# CLI Functions (existing functionality)
def create_parser():
    """Create argument parser with CLI and TUI options"""
    parser = argparse.ArgumentParser(
        prog='cobradle',
        description='Cobradle - Advanced Multi-threaded Downloader with CLI and TUI interfaces',
        formatter_class=argparse.RawDescriptionHelpFormatter
    )
    
    # UI Mode selection
    parser.add_argument('--cli', action='store_true', help='Force CLI mode')
    parser.add_argument('--tui', action='store_true', help='Force TUI mode')
    parser.add_argument('--ui-mode', choices=['cli', 'tui', 'auto'], help='Set UI mode')
    
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
    download_parser.add_argument('--limit-rate', help='Limit download rate (e.g., 500K, 2M)')
    
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
    history_subparsers.add_parser('search', help='Search history').add_argument('query', help='Search query')
    
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
    
    # TUI command
    tui_parser = subparsers.add_parser('tui', help='Launch TUI interface')
    
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

def should_use_tui(args, config: DownloadConfig) -> bool:
    """Determine whether to use TUI or CLI"""
    # Command line overrides
    if args.tui or args.ui_mode == 'tui':
        return True
    if args.cli or args.ui_mode == 'cli':
        return False
    
    # Config setting
    if config.ui_mode == 'tui':
        return True
    elif config.ui_mode == 'cli':
        return False
    
    # Auto detection: use TUI if no command specified and terminal is interactive
    if not args.command and sys.stdin.isatty() and sys.stdout.isatty():
        return True
    
    return False

def parse_bandwidth_limit(limit_str: str) -> Optional[int]:
    """Parse bandwidth limit string (e.g., '2M', '500K') to KB/s"""
    if not limit_str:
        return None
    
    limit_str = limit_str.upper().strip()
    
    if limit_str.endswith('K'):
        return int(limit_str[:-1])
    elif limit_str.endswith('M'):
        return int(limit_str[:-1]) * 1024
    elif limit_str.endswith('G'):
        return int(limit_str[:-1]) * 1024 * 1024
    else:
        # Assume KB/s
        return int(limit_str)

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
    if hasattr(args, 'limit_rate') and args.limit_rate:
        config.bandwidth_limit = parse_bandwidth_limit(args.limit_rate)
    
    # Ensure output directory exists
    Path(config.output_dir).mkdir(parents=True, exist_ok=True)
    
    if len(args.urls) == 1:
        # Single file download
        downloader = FileDownloader(config)
        download_item = DownloadItem(args.urls[0], output_dir=config.output_dir)
        
        def progress_callback(item):
            # Simple progress display for CLI
            if item.size > 0:
                percent = (item.downloaded / item.size) * 100
                print(f"\rProgress: {percent:.1f}% ({item.downloaded}/{item.size} bytes)", end='')
        
        success = downloader.download_file(download_item, progress_callback)
        print()  # New line after progress
        
        if success:
            print(f"✓ Download completed successfully: {download_item.filename}")
        else:
            print(f"✗ Download failed: {download_item.error}")
            return 1
    else:
        # Batch download with Rich progress bars
        console = Console()
        
        with Progress(
        SpinnerColumn(),
            TextColumn("[bold blue]{task.fields[filename]}"),
            BarColumn(),
            "[progress.percentage]{task.percentage:>3.1f}%",
            "•",
            DownloadColumn(),
            "•",
            TransferSpeedColumn(),
            "•",
            TimeRemainingColumn(),
            console=console,
            transient=True,
        ) as progress:
            
            batch_downloader = BatchDownloader(config)
            tasks = {}
                   # Create progress tasks
            for url in args.urls:
                filename = DownloadItem(url)._extract_filename(url)
                task_id = progress.add_task("download", filename=filename, start=False)
                tasks[url] = task_id
            
            def progress_callback(item):
                if item.url in tasks:
                    progress.update(
                        tasks[item.url],
                        completed=item.downloaded,
                        total=item.size if item.size > 0 else None
                    )
                    if not progress.tasks[tasks[item.url]].started:
                        progress.start_task(tasks[item.url])
            
            batch_downloader.add_downloads(args.urls, config.output_dir)
            results = batch_downloader.download_batch(progress_callback)
        
        console.print(f"\n[bold]Download Summary:[/bold]")
        console.print(f"  [green]Success: {len(results['success'])}[/green]")
        console.print(f"  [red]Failed: {len(results['failed'])}[/red]")
        
        if results['failed']:
            console.print(f"\n[red]Failed downloads:[/red]")
            for url in results['failed']:
                console.print(f"  - {url}")
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
    
    # Use the download command handler for consistency
    class BatchArgs:
        def __init__(self, urls, output, parallel):
            self.urls = urls
            self.output = output
            self.parallel = parallel
            self.chunk_size = None
            self.timeout = None
            self.retries = None
            self.user_agent = None
            self.no_ssl_verify = False
            self.resume = False
    
    batch_args = BatchArgs(urls, args.output, args.parallel)
    return handle_download_command(batch_args, config)

def handle_queue_command(args):
    """Handle queue management commands"""
    queue_manager = QueueManager()
    
    if args.queue_action == 'list':
        queue = queue_manager.get_queue()
        if not queue:
            print("Queue is empty")
            return 0
        
        print(f"\nDownload Queue ({len(queue)} items):")
        print("-" * 80)
        for i, item in enumerate(queue, 1):
            status = item.get('status', 'pending')
            url = item['url'][:60] + '...' if len(item['url']) > 60 else item['url']
            added = datetime.fromisoformat(item['added']).strftime('%Y-%m-%d %H:%M')
            print(f"{i:2d}. [{status:8s}] {url} (added: {added})")
    
    elif args.queue_action == 'add':
        queue_manager.add(args.url)
        print(f"Added to queue: {args.url}")
    
    elif args.queue_action == 'remove':
        if queue_manager.remove(args.index - 1):
            print(f"Removed item {args.index} from queue")
        else:
            print(f"Invalid queue index: {args.index}")
            return 1
    
    elif args.queue_action == 'clear':
        queue_manager.clear()
        print("Queue cleared")
    
    elif args.queue_action == 'process':
        config = load_config()
        queue = queue_manager.get_queue()
        pending_urls = [item['url'] for item in queue if item.get('status') == 'pending']
        
        if not pending_urls:
            print("No pending items in queue")
            return 0
        
        print(f"Processing {len(pending_urls)} items from queue...")
        
        # Use download handler
        class QueueArgs:
            def __init__(self, urls):
                self.urls = urls
                self.output = None
                self.parallel = None
                self.chunk_size = None
                self.timeout = None
                self.retries = None
                self.user_agent = None
                self.no_ssl_verify = False
                self.resume = False
        
        queue_args = QueueArgs(pending_urls)
        result = handle_download_command(queue_args, config)
        
        # Update queue status based on results
        # (This is simplified - in production you'd track individual results)
        if result == 0:
            print("Queue processing completed successfully")
        
        return result
    
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
        
        console = Console()
        
        table = Table(title="Download History")
        table.add_column("Date", style="cyan")
        table.add_column("Filename", style="magenta")
        table.add_column("Size", style="green")
        table.add_column("Duration", style="yellow")
        table.add_column("Status", style="red")
        
        for item in history[-20:]:  # Show last 20 items
            timestamp = datetime.fromisoformat(item['timestamp']).strftime('%Y-%m-%d %H:%M')
            size = item.get('size', 0)
            size_str = f"{size/1024/1024:.1f}MB" if size > 0 else "Unknown"
            duration = item.get('duration', 0)
            duration_str = f"{duration:.1f}s" if duration > 0 else "Unknown"
            status = item.get('status', 'Unknown').title()
            filename = item.get('filename', 'Unknown')[:30]
            
            table.add_row(timestamp, filename, size_str, duration_str, status)
        
        console.print(table)
    
    elif args.history_action == 'clear':
        if HISTORY_FILE.exists():
            HISTORY_FILE.unlink()
        print("History cleared")
    
    elif args.history_action == 'search':
        if not HISTORY_FILE.exists():
            print("No download history found")
            return 0
        
        try:
            with open(HISTORY_FILE, 'r') as f:
                history = json.load(f)
        except:
            print("Error reading history file")
            return 1
        
        query = args.query.lower()
        matches = [
            item for item in history 
            if query in item.get('filename', '').lower() or query in item.get('url', '').lower()
        ]
        
        if matches:
            print(f"Found {len(matches)} matches for '{args.query}':")
            for item in matches[-10:]:  # Show last 10 matches
                timestamp = datetime.fromisoformat(item['timestamp']).strftime('%Y-%m-%d %H:%M')
                filename = item.get('filename', 'Unknown')
                print(f"  [{timestamp}] {filename}")
        else:
            print(f"No matches found for '{args.query}'")
    
    return 0

def handle_config_command(args):
    """Handle configuration commands"""
    config = load_config()
    
    if args.config_action == 'show':
        console = Console()
        
        table = Table(title="Cobradle Configuration")
        table.add_column("Setting", style="cyan")
        table.add_column("Value", style="magenta")
        
        for key, value in asdict(config).items():
            table.add_row(key.replace('_', ' ').title(), str(value))
        
        console.print(table)
    
    elif args.config_action == 'set':
        if hasattr(config, args.key):
            # Try to convert value to appropriate type
            old_value = getattr(config, args.key)
            try:
                if isinstance(old_value, bool):
                    new_value = args.value.lower() in ('true', 'yes', '1', 'on')
                elif isinstance(old_value, int):
                    new_value = int(args.value)
                else:
                    new_value = args.value
                
                setattr(config, args.key, new_value)
                save_config(config)
                print(f"✓ Set {args.key} = {new_value}")
            except ValueError:
                print(f"✗ Invalid value for {args.key}: {args.value}")
                return 1
        else:
            print(f"✗ Unknown config key: {args.key}")
            available_keys = list(asdict(config).keys())
            print(f"Available keys: {', '.join(available_keys)}")
            return 1
    
    return 0

def handle_file_command(args):
    """Handle file operations"""
    if args.file_action == 'hash':
        if not Path(args.file).exists():
            print(f"File not found: {args.file}")
            return 1
        
        console = Console()
        
        with console.status(f"Calculating {args.algorithm.upper()} hash..."):
            hash_func = hashlib.new(args.algorithm)
            try:
                with open(args.file, 'rb') as f:
                    for chunk in iter(lambda: f.read(8192), b""):
                        hash_func.update(chunk)
                
                hash_value = hash_func.hexdigest()
                
                table = Table(title=f"File Hash - {Path(args.file).name}")
                table.add_column("Algorithm", style="cyan")
                table.add_column("Hash", style="magenta")
                table.add_row(args.algorithm.upper(), hash_value)
                
                console.print(table)
                
            except Exception as e:
                print(f"Error calculating hash: {e}")
                return 1
    
    return 0

def handle_info_command(args):
    """Handle info command"""
    console = Console()
    
    if args.system:
        table = Table(title="System Information")
        table.add_column("Property", style="cyan")
        table.add_column("Value", style="magenta")
        
        table.add_row("Platform", platform.platform())
        table.add_row("Python Version", sys.version.split()[0])
        table.add_row("Architecture", platform.architecture()[0])
        table.add_row("Processor", platform.processor() or "Unknown")
        table.add_row("Machine", platform.machine())
        table.add_row("Config Directory", str(CONFIG_DIR))
        
        console.print(table)
    else:
        console.print(Panel.fit(
            "[bold blue]Cobradle[/bold blue]\n"
            "[cyan]Advanced Multi-threaded Downloader[/cyan]\n\n"
            f"Version: [green]1.0.0[/green]\n"
            f"Config Directory: [yellow]{CONFIG_DIR}[/yellow]\n"
            f"Python: [magenta]{sys.version.split()[0]}[/magenta]\n\n"
            "[dim]Use --help for available commands[/dim]",
            title="About Cobradle"
        ))
    
    return 0

def main():
    """Main entry point with UI mode detection"""
    parser = create_parser()
    
    # Handle no arguments case
    if len(sys.argv) == 1:
        config = load_config()
        if should_use_tui(argparse.Namespace(tui=False, cli=False, ui_mode=None, command=None), config):
            # Launch TUI
            try:
                app = CobradelTUI(config)
                app.run()
                return 0
            except Exception as e:
                print(f"Error launching TUI: {e}")
                print("Falling back to CLI mode...")
        
        parser.print_help()
        return 0
    
    args = parser.parse_args()
    
    # Load configuration
    config = load_config()
    
    # Handle TUI launch
    if args.command == 'tui' or should_use_tui(args, config):
        try:
            app = CobradelTUI(config)
            app.run()
            return 0
        except Exception as e:
            print(f"Error launching TUI: {e}")
            if not args.command:
                return 1
            print("Continuing with CLI mode...")
    
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
    
    try:
        # Handle CLI commands
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
