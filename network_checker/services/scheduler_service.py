from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.executors.pool import ThreadPoolExecutor
from typing import List, Optional
import logging
from .ping_service import PingService


class SchedulerService:
    def __init__(self, database_url: str = 'sqlite:///network_checker.db'):
        jobstores = {
            'default': SQLAlchemyJobStore(url=database_url)
        }
        
        executors = {
            'default': ThreadPoolExecutor(20)
        }
        
        job_defaults = {
            'coalesce': False,
            'max_instances': 3
        }
        
        self.scheduler = BackgroundScheduler(
            jobstores=jobstores,
            executors=executors,
            job_defaults=job_defaults
        )
        
        self.ping_service = PingService()
        self.is_running = False
        
        # Configure logging
        logging.basicConfig(level=logging.INFO)
        self.logger = logging.getLogger(__name__)
    
    def start(self):
        if not self.is_running:
            self.scheduler.start()
            self.is_running = True
            self.logger.info("Scheduler started")
    
    def shutdown(self):
        if self.is_running:
            self.scheduler.shutdown()
            self.is_running = False
            self.logger.info("Scheduler stopped")
    
    def add_ping_job(self, host: str, interval: int = 60, job_id: Optional[str] = None):
        if job_id is None:
            job_id = f"ping_{host}"
        
        self.scheduler.add_job(
            func=self._ping_job,
            trigger='interval',
            seconds=interval,
            id=job_id,
            args=[host],
            replace_existing=True
        )
        
        self.logger.info(f"Added ping job for {host} with {interval}s interval")
    
    def remove_ping_job(self, job_id: str):
        try:
            self.scheduler.remove_job(job_id)
            self.logger.info(f"Removed ping job: {job_id}")
        except Exception as e:
            self.logger.error(f"Failed to remove job {job_id}: {e}")
    
    def add_multiple_ping_jobs(self, hosts: List[str], interval: int = 60):
        for host in hosts:
            self.add_ping_job(host, interval)
    
    def list_jobs(self):
        return self.scheduler.get_jobs()
    
    def _ping_job(self, host: str):
        try:
            result = self.ping_service.ping_and_save(host)
            self.logger.debug(f"Ping result for {host}: {result.response_time}ms, packet_loss: {result.packet_loss}")
        except Exception as e:
            self.logger.error(f"Error pinging {host}: {e}")
    
    def get_job_status(self, job_id: str):
        job = self.scheduler.get_job(job_id)
        if job:
            return {
                'id': job.id,
                'name': str(job.func),
                'next_run_time': job.next_run_time.isoformat() if job.next_run_time else None,
                'trigger': str(job.trigger)
            }
        return None