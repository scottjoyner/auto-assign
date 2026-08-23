#!/usr/bin/env python3
"""Minimal fleet worker - polls assistx-api for READY tasks and executes them."""

import argparse
import json
import logging
import os
import subprocess
import sys
import time
from datetime import datetime, timezone

try:
    import requests
except ImportError:
    print("ERROR: requests library required. Install with: pip install requests")
    sys.exit(1)


class MinimalWorker:
    def __init__(self, assistx_url, auth_user, auth_pass, node_id):
        self.assistx_url = assistx_url.rstrip("/")
        self.auth_user = auth_user
        self.auth_pass = auth_pass
        self.node_id = node_id
        self.session = requests.Session()
        self.session.auth = (auth_user, auth_pass)
        logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")

    def poll_ready_tasks(self, limit=10):
        """Get READY tasks from assistx-api."""
        url = f"{self.assistx_url}/api/tasks"
        params = {"status": "READY", "limit": limit}
        
        try:
            resp = self.session.get(url, params=params)
            if resp.status_code == 200:
                data = resp.json()
                return data.get("items", [])
            else:
                logging.error(f"Failed to fetch tasks: {resp.status_code} - {resp.text}")
                return []
        except Exception as e:
            logging.error(f"Error polling tasks: {e}")
            return []

    def claim_task(self, task_id):
        """Claim a task for this worker."""
        url = f"{self.assistx_url}/api/tasks/{task_id}/claim"
        payload = {
            "worker_id": self.node_id,
            "node_id": self.node_id,
            "lease_seconds": 300
        }
        
        try:
            resp = self.session.post(url, json=payload)
            if resp.status_code == 200:
                return True
            else:
                logging.error(f"Failed to claim task {task_id}: {resp.status_code} - {resp.text}")
                return False
        except Exception as e:
            logging.error(f"Error claiming task: {e}")
            return False

    def execute_task(self, task):
        """Execute a task's command."""
        task_id = task["id"]
        command = task.get("command", "")
        
        if not command:
            logging.warning(f"Task {task_id} has no command to execute")
            return False

        logging.info(f"Executing task {task_id}: {command}")
        
        try:
            result = subprocess.run(
                command,
                shell=True,
                capture_output=True,
                text=True,
                timeout=300  # 5 minute timeout
            )
            
            success = result.returncode == 0
            output = result.stdout + result.stderr
            
            logging.info(f"Task {task_id} completed: {'SUCCESS' if success else 'FAILED'}")
            return success
        except subprocess.TimeoutExpired:
            logging.error(f"Task {task_id} timed out after 5 minutes")
            return False
        except Exception as e:
            logging.error(f"Error executing task {task_id}: {e}")
            return False

    def complete_task(self, task_id, success=True):
        """Mark a task as completed."""
        url = f"{self.assistx_url}/api/tasks/{task_id}/complete"
        payload = {
            "worker_id": self.node_id,
            "success": success
        }
        
        try:
            resp = self.session.post(url, json=payload)
            if resp.status_code == 200:
                logging.info(f"Task {task_id} marked as {'completed' if success else 'failed'}")
                return True
            else:
                logging.error(f"Failed to complete task {task_id}: {resp.status_code} - {resp.text}")
                return False
        except Exception as e:
            logging.error(f"Error completing task: {e}")
            return False

    def run_once(self):
        """Run one iteration of the worker loop."""
        tasks = self.poll_ready_tasks(limit=5)
        
        if not tasks:
            logging.info("No READY tasks found")
            return 0
        
        processed = 0
        for task in tasks:
            task_id = task["id"]
            
            # Claim the task
            if not self.claim_task(task_id):
                continue
            
            # Execute the task
            success = self.execute_task(task)
            
            # Mark as complete
            self.complete_task(task_id, success=success)
            processed += 1
        
        return processed

    def run_loop(self, interval=30):
        """Run continuously with polling interval."""
        logging.info(f"Starting worker loop (interval={interval}s)")
        
        while True:
            try:
                self.run_once()
            except Exception as e:
                logging.error(f"Error in worker loop: {e}")
            
            time.sleep(interval)


def main():
    parser = argparse.ArgumentParser(description="Minimal fleet worker")
    parser.add_argument("--assistx-url", default=os.getenv("ASSISTX_URL", "http://localhost:8000"))
    parser.add_argument("--auth-user", default=os.getenv("AUTH_USER", "admin"))
    parser.add_argument("--auth-pass", default=os.getenv("AUTH_PASS", ""))
    parser.add_argument("--node-id", default=os.getenv("NODE_ID", "worker-1"))
    parser.add_argument("--interval", type=int, default=30)
    parser.add_argument("--once", action="store_true", help="Run once and exit")
    
    args = parser.parse_args()
    
    worker = MinimalWorker(
        assistx_url=args.assistx_url,
        auth_user=args.auth_user,
        auth_pass=args.auth_pass,
        node_id=args.node_id
    )
    
    if args.once:
        processed = worker.run_once()
        logging.info(f"Processed {processed} tasks")
    else:
        worker.run_loop(interval=args.interval)


if __name__ == "__main__":
    main()
