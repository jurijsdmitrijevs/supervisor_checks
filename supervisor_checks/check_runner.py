"""Instance of CheckRunner runs the set of pre-configured checks
against the process running under SupervisorD.
"""

__author__ = 'vovanec@gmail.com'


import concurrent.futures
import datetime
import os
import signal
import sys
import threading

from supervisor import childutils
from supervisor.compat import xmlrpclib
from supervisor.options import make_namespec
from supervisor.states import ProcessStates

# Process spec keys
STATE_KEY = 'state'
NAME_KEY = 'name'
GROUP_KEY = 'group'
EVENT_NAME_KEY = 'eventname'

MAX_THREADS = 16
TICK_EVENTS = {'TICK_5', 'TICK_60', 'TICK_3600'}


class CheckRunner(object):
    """SupervisorD checks runner.
    """

    def __init__(self, check_name, process_group, checks_config, env=None):
        """Constructor.

        :param str check_name: the name of check to display in log.
        :param str process_group: the name of the process group.
        :param list checks_config: the list of check module configurations
               in format [(check_class, check_configuration_dictionary)]
        :param dict env: environment.
        """

        self._environment = env or os.environ
        self._name = check_name
        self._checks_config = checks_config
        self._checks = self._init_checks()
        self._process_group = process_group
        self._group_check_name = '%s_check' % (self._process_group,)
        self._rpc_client = childutils.getRPCInterface(self._environment)
        self._stop_event = threading.Event()

    def run(self):
        """Run main check loop.
        """

        self._log('Starting the health check for %s process group. '
                  'Checks config: %s', self._process_group, self._checks_config)

        self._install_signal_handlers()

        while not self._stop_event.is_set():

            try:
                headers, _ = childutils.listener.wait(
                    sys.stdin, sys.stdout, waiter=self._stop_event)
            except childutils.WaitInterrupted:
                self._log(
                    'Health check for %s process group has been told to stop.',
                    self._process_group)

                break

            event_type = headers[EVENT_NAME_KEY]
            if event_type in TICK_EVENTS:
                self._check_processes()
            else:
                self._log('Received unsupported event type: %s', event_type)

            childutils.listener.ok(sys.stdout)

        self._log('Done.')

    def _check_processes(self):
        """Run single check loop for process group.
        """

        process_specs = self._get_process_spec_list(ProcessStates.RUNNING)
        if process_specs:
            if len(process_specs) == 1:
                self._check_and_restart(process_specs[0])
            else:
                # Query and restart in multiple threads simultaneously.
                with concurrent.futures.ThreadPoolExecutor(MAX_THREADS) as pool:
                    for process_spec in process_specs:
                        pool.submit(self._check_and_restart, process_spec)
        else:
            self._log(
                'No processes in state RUNNING found for process group %s',
                self._process_group)

    def _check_and_restart(self, process_spec):
        """Run checks for the process and restart if needed.
        """

        for check in self._checks:
            self._log('Performing %s check for process name %s',
                      check.NAME, process_spec['name'])

            try:
                if not check(process_spec):
                    self._log('%s check failed for process %s. Trying to '
                              'restart.', check.NAME, process_spec['name'])

                    return self._restart_process(process_spec)
                else:
                    self._log('%s check succeeded for process %s',
                              check.NAME, process_spec['name'])
            except Exception as exc:
                self._log('%s check raised error for process %s: %s',
                          check.NAME, process_spec['name'], exc)

    def _init_checks(self):
        """Init check instances.

        :rtype: list
        """

        checks = []
        for check_class, check_cfg in self._checks_config:
            checks.append(check_class(check_cfg, self._log))

        return checks

    def _get_process_spec_list(self, state=None):
        """Get the list of processes in a process group.
        """

        process_specs = []
        for process_spec in self._rpc_client.supervisor.getAllProcessInfo():
            if (process_spec[GROUP_KEY] == self._process_group and
                    (state is None or process_spec[STATE_KEY] == state)):
                process_specs.append(process_spec)

        return process_specs

    def _restart_process(self, process_spec):
        """Restart a process.
        """

        name_spec = make_namespec(
            process_spec[GROUP_KEY], process_spec[NAME_KEY])

        rpc_client = childutils.getRPCInterface(self._environment)

        process_spec = rpc_client.supervisor.getProcessInfo(name_spec)
        if process_spec[STATE_KEY] is ProcessStates.RUNNING:
            self._log('Trying to stop process %s', name_spec)

            try:
                rpc_client.supervisor.stopProcess(name_spec)
                self._log('Stopped process %s', name_spec)
            except xmlrpclib.Fault as exc:
                self._log('Failed to stop process %s: %s', name_spec, exc)

            try:
                self._log('Starting process %s', name_spec)
                rpc_client.supervisor.startProcess(name_spec, False)
            except xmlrpclib.Fault as exc:
                self._log('Failed to start process %s: %s', name_spec, exc)

        else:
            self._log('%s not in RUNNING state, cannot restart', name_spec)

    def _log(self, msg, *args):
        """Write message to STDERR.

        :param str msg: string message.
        """

        curr_dt = datetime.datetime.now().strftime('%Y/%M/%d %H:%M:%S')

        sys.stderr.write(
            '%s [%s] %s\n' % (curr_dt, self._name, msg % args,))

        sys.stderr.flush()

    def _install_signal_handlers(self):
        """Install signal handlers.
        """

        self._log('Installing signal handlers.')

        for sig in (signal.SIGINT, signal.SIGUSR1, signal.SIGHUP,
                    signal.SIGTERM, signal.SIGQUIT):
            signal.signal(sig, self._signal_handler)

    def _signal_handler(self, signum, _):
        """Signal handler.
        """

        self._log('Got signal %s', signum)

        self._stop_event.set()
