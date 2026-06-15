from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.jobstores.memory import MemoryJobStore
from apscheduler.executors.pool import ThreadPoolExecutor
from typing import List, Optional
import logging
from .ping_service import PingService


logger = logging.getLogger(__name__)

# Default scheduler worker-pool size. Monitoring a handful of hosts needs only a
# small pool; callers inject Config.SCHEDULER_MAX_WORKERS to override.
DEFAULT_SCHEDULER_MAX_WORKERS = 4


def _run_ping_job(app, ping_service: "PingService", host: str):
    """Module-level job target executed by an APScheduler worker thread.

    Two reasons this lives at module scope instead of being a bound method:

    * **No scheduler in the closure.** A bound ``self._ping_job`` drags the whole
      ``SchedulerService`` (and its ``BackgroundScheduler``) along as ``self``.
      A persistent jobstore would then try to pickle it and APScheduler raises
      "Schedulers cannot be serialized". Keeping the target instance-independent
      means it stays safe even if a persistent store is reintroduced later.
    * **App context.** Jobs run in a worker thread, and Flask/flask_sqlalchemy
      contexts are thread-local, so the main thread's ``app.app_context()`` does
      not reach here. Without an explicit context, ``ping_and_save`` fails with
      "Working outside of application context". We push one per invocation.
    """
    try:
        if app is not None:
            with app.app_context():
                result = ping_service.ping_and_save(host)
        else:
            result = ping_service.ping_and_save(host)
        logger.debug(
            f"Ping result for {host}: {result.response_time}ms, "
            f"packet_loss: {result.packet_loss}"
        )
    except Exception as e:
        logger.error(f"Error pinging {host}: {e}")


class SchedulerService:
    def __init__(
        self,
        app=None,
        timeout: Optional[float] = None,
        max_workers: Optional[int] = None,
    ):
        # MemoryJobStore (not SQLAlchemyJobStore): ``start-monitoring`` re-registers
        # every job on each launch via ``add_ping_job``, so persisting jobs across
        # runs buys nothing -- and an in-memory store never pickles the job func,
        # which is what broke bound-method jobs ("Schedulers cannot be serialized").
        # Dropping the persistent store also removes the old hardcoded
        # ``sqlite:///network_check_tool.db`` jobstore URL that ignored DATABASE_URL.
        jobstores = {"default": MemoryJobStore()}

        self.max_workers = (
            max_workers if max_workers is not None else DEFAULT_SCHEDULER_MAX_WORKERS
        )
        # Guard a misconfigured SCHEDULER_MAX_WORKERS up front: ThreadPoolExecutor
        # otherwise raises a cryptic "max_workers must be greater than 0" from deep
        # inside concurrent.futures when the scheduler is built.
        if self.max_workers <= 0:
            raise ValueError(
                f"max_workers must be a positive integer, got {self.max_workers}"
            )
        executors = {"default": ThreadPoolExecutor(self.max_workers)}

        job_defaults = {"coalesce": False, "max_instances": 3}

        self.scheduler = BackgroundScheduler(
            jobstores=jobstores, executors=executors, job_defaults=job_defaults
        )

        # Flask app whose context each job pushes before touching the DB session.
        self.app = app
        # Forward the caller-provided timeout (e.g. Config.PING_TIMEOUT, injected at
        # the CLI edge) to PingService; fall back to its built-in default when unset
        # so the library layer never has to read global configuration itself (M2).
        self.ping_service = (
            PingService(timeout=timeout) if timeout is not None else PingService()
        )
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
            func=_run_ping_job,
            trigger="interval",
            seconds=interval,
            id=job_id,
            args=[self.app, self.ping_service, host],
            replace_existing=True,
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

    def get_job_status(self, job_id: str):
        job = self.scheduler.get_job(job_id)
        if job:
            return {
                "id": job.id,
                "name": str(job.func),
                "next_run_time": (
                    job.next_run_time.isoformat() if job.next_run_time else None
                ),
                "trigger": str(job.trigger),
            }
        return None
