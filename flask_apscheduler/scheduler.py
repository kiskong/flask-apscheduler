# Copyright 2015 Vinicius Chiele. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""APScheduler implementation."""

import functools
import logging
import socket

from apscheduler.events import EVENT_ALL
from apscheduler.schedulers.background import BackgroundScheduler
from flask import make_response
from . import api
from .utils import fix_job_def, pop_trigger

LOGGER = logging.getLogger('flask_apscheduler')


class APScheduler(object):
    """Provides a scheduler integrated to Flask."""

    def __init__(self, scheduler=None, app=None):
        self._scheduler = scheduler or BackgroundScheduler()
        self._host_name = socket.gethostname().lower()
        self._authentication_callback = None

        self.allowed_hosts = ['*']
        self.auth = None
        self.api_enabled = False
        self.app = None

        if app:
            self.init_app(app)

    @property
    def host_name(self):
        """Get the host name."""
        return self._host_name

    @property
    def running(self):
        """Get true whether the scheduler is running."""
        return self._scheduler.running

    @property
    def scheduler(self):
        """Get the base scheduler."""
        return self._scheduler

    def init_app(self, app):
        """Initialize the APScheduler with a Flask application instance."""

        self.app = app
        self.app.apscheduler = self

        self._load_config()
        self._load_jobs()

        if self.api_enabled:
            self._load_api()

    def start(self):
        """
        Start the scheduler.
        """
        if self.host_name not in self.allowed_hosts and '*' not in self.allowed_hosts:
            LOGGER.debug('Host name %s is not allowed to start the APScheduler. Servers allowed: %s' %
                         (self.host_name, ','.join(self.allowed_hosts)))
            return

        self._scheduler.start()

    def shutdown(self, wait=True):
        """
        Shut down the scheduler. Does not interrupt any currently running jobs.

        :param bool wait: ``True`` to wait until all currently executing jobs have finished
        :raises SchedulerNotRunningError: if the scheduler has not been started yet
        """

        self._scheduler.shutdown(wait)

    def pause(self):
        """
        Pause job processing in the scheduler.

        This will prevent the scheduler from waking up to do job processing until :meth:`resume`
        is called. It will not however stop any already running job processing.
        """
        self._scheduler.pause()

    def resume(self):
        """
        Resume job processing in the scheduler.
        """
        self._scheduler.resume()

    def add_listener(self, callback, mask=EVENT_ALL):
        """
        Add a listener for scheduler events.

        When a matching event  occurs, ``callback`` is executed with the event object as its
        sole argument. If the ``mask`` parameter is not provided, the callback will receive events
        of all types.

        For further info: https://apscheduler.readthedocs.io/en/latest/userguide.html#scheduler-events

        :param callback: any callable that takes one argument
        :param int mask: bitmask that indicates which events should be listened to
        """
        self._scheduler.add_listener(callback, mask)

    def remove_listener(self, callback):
        """
        Remove a previously added event listener.
        """
        self._scheduler.remove_listener(callback)

    def add_job(self, id, func, **kwargs):
        """
        Add the given job to the job list and wakes up the scheduler if it's already running.

        :param str id: explicit identifier for the job (for modifying it later)
        :param func: callable (or a textual reference to one) to run at the given time
        """

        job_def = dict(kwargs)
        job_def['id'] = id
        job_def['func'] = func
        job_def['name'] = job_def.get('name') or id

        fix_job_def(job_def)

        return self._scheduler.add_job(**job_def)

    def delete_job(self, id, jobstore=None):
        """
        Remove a job, preventing it from being run any more.

        :param str id: the identifier of the job
        :param str jobstore: alias of the job store that contains the job
        """

        self._scheduler.remove_job(id, jobstore)

    def delete_all_jobs(self, jobstore=None):
        """
        Remove all jobs from the specified job store, or all job stores if none is given.
        
        :param str|unicode jobstore: alias of the job store
        """

        self._scheduler.remove_all_jobs(jobstore)

    def get_job(self, id, jobstore=None):
        """
        Return the Job that matches the given ``id``.

        :param str id: the identifier of the job
        :param str jobstore: alias of the job store that most likely contains the job
        :return: the Job by the given ID, or ``None`` if it wasn't found
        :rtype: Job
        """

        return self._scheduler.get_job(id, jobstore)

    def get_jobs(self, jobstore=None):
        """
        Return a list of pending jobs (if the scheduler hasn't been started yet) and scheduled jobs, either from a
        specific job store or from all of them.

        :param str jobstore: alias of the job store
        :rtype: list[Job]
        """

        return self._scheduler.get_jobs(jobstore)

    def modify_job(self, id, jobstore=None, **changes):
        """
        Modify the properties of a single job. Modifications are passed to this method as extra keyword arguments.

        :param str id: the identifier of the job
        :param str jobstore: alias of the job store that contains the job
        """

        fix_job_def(changes)

        if 'trigger' in changes:
            trigger, trigger_args = pop_trigger(changes)
            self._scheduler.reschedule_job(id, jobstore, trigger, **trigger_args)

        return self._scheduler.modify_job(id, jobstore, **changes)

    def pause_job(self, id, jobstore=None):
        """
        Pause the given job until it is explicitly resumed.

        :param str id: the identifier of the job
        :param str jobstore: alias of the job store that contains the job
        """
        self._scheduler.pause_job(id, jobstore)

    def resume_job(self, id, jobstore=None):
        """
        Resume the schedule of the given job, or removes the job if its schedule is finished.

        :param str id: the identifier of the job
        :param str jobstore: alias of the job store that contains the job
        """
        self._scheduler.resume_job(id, jobstore)

    def run_job(self, id, jobstore=None):
        """
        Run the given job without scheduling it.
        :param id: the identifier of the job.
        :param str jobstore: alias of the job store that contains the job
        :return:
        """
        job = self._scheduler.get_job(id, jobstore)

        if not job:
            raise LookupError(id)

        job.func(*job.args, **job.kwargs)

    def authenticate(self, func):
        """
        A decorator that is used to register a function to authenticate a user.
        :param func: The callback to authenticate.
        """
        self._authentication_callback = func
        return func

    def _load_config(self):
        """
        Load the configuration from the Flask configuration.
        """
        options = dict()

        job_stores = self.app.config.get('SCHEDULER_JOBSTORES')
        if job_stores:
            options['jobstores'] = job_stores

        executors = self.app.config.get('SCHEDULER_EXECUTORS')
        if executors:
            options['executors'] = executors

        job_defaults = self.app.config.get('SCHEDULER_JOB_DEFAULTS')
        if job_defaults:
            options['job_defaults'] = job_defaults

        timezone = self.app.config.get('SCHEDULER_TIMEZONE')
        if timezone:
            options['timezone'] = timezone

        self._scheduler.configure(**options)

        self.auth = self.app.config.get('SCHEDULER_AUTH', self.auth)
        self.api_enabled = self.app.config.get('SCHEDULER_VIEWS_ENABLED', self.api_enabled)  # for compatibility reason
        self.api_enabled = self.app.config.get('SCHEDULER_API_ENABLED', self.api_enabled)
        self.allowed_hosts = self.app.config.get('SCHEDULER_ALLOWED_HOSTS', self.allowed_hosts)

    def _load_jobs(self):
        """
        Load the job definitions from the Flask configuration.
        """
        jobs = self.app.config.get('SCHEDULER_JOBS')

        if not jobs:
            jobs = self.app.config.get('JOBS')

        if jobs:
            for job in jobs:
                self.add_job(**job)

    def _load_api(self):
        """
        Add the routes for the scheduler API.
        """
        self.app.add_url_rule('/scheduler', 'get_scheduler_info', self._apply_auth(api.get_scheduler_info))
        self.app.add_url_rule('/scheduler/jobs', 'add_job', self._apply_auth(api.add_job), methods=['POST'])
        self.app.add_url_rule('/scheduler/jobs', 'get_jobs', self._apply_auth(api.get_jobs))
        self.app.add_url_rule('/scheduler/jobs/<job_id>', 'get_job', self._apply_auth(api.get_job))
        self.app.add_url_rule('/scheduler/jobs/<job_id>', 'delete_job', self._apply_auth(api.delete_job), methods=['DELETE'])
        self.app.add_url_rule('/scheduler/jobs/<job_id>', 'update_job', self._apply_auth(api.update_job), methods=['PATCH'])
        self.app.add_url_rule('/scheduler/jobs/<job_id>/pause', 'pause_job', self._apply_auth(api.pause_job), methods=['POST'])
        self.app.add_url_rule('/scheduler/jobs/<job_id>/resume', 'resume_job', self._apply_auth(api.resume_job), methods=['POST'])
        self.app.add_url_rule('/scheduler/jobs/<job_id>/run', 'run_job', self._apply_auth(api.run_job), methods=['POST'])

    def _apply_auth(self, view_func):
        """
        Apply decorator to authenticate the user who is making the request.
        :param view_func: The flask view func.
        """
        @functools.wraps(view_func)
        def decorated(*args, **kwargs):
            if not self.auth:
                return view_func(*args, **kwargs)

            auth_data = self.auth.get_authorization()

            if auth_data is None:
                return self._handle_authentication_error()

            if not self._authentication_callback or not self._authentication_callback(auth_data):
                return self._handle_authentication_error()

            return view_func(*args, **kwargs)

        return decorated

    def _handle_authentication_error(self):
        """
        Return an authentication error.
        """
        response = make_response('Access Denied')
        response.headers['WWW-Authenticate'] = self.auth.get_authenticate_header()
        response.status_code = 401
        return response
