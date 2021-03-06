# -*- coding: utf-8 -*-
from binascii import hexlify
import io
import errno
import os
import select
import signal
import sys
import time
import structlog
from logging import StreamHandler, Formatter

from eth_utils import denoms, to_checksum_address
import gevent
from gevent.event import Event
from gevent import Greenlet
import IPython
from IPython.lib.inputhook import inputhook_manager, stdin_ready

from raiden.api.python import RaidenAPI
from raiden.utils import get_contract_path, safe_address_decode
from raiden.utils.solc import compile_files_cwd

ENTER_CONSOLE_TIMEOUT = 3
GUI_GEVENT = 'gevent'

# ansi escape code for typesetting
HEADER = '\033[95m'
OKBLUE = '\033[94m'
OKGREEN = '\033[92m'
WARNING = '\033[91m'
FAIL = '\033[91m'
ENDC = '\033[0m'
BOLD = '\033[1m'
UNDERLINE = '\033[4m'

# ipython needs to accept "--gui gevent" option
IPython.core.shellapp.InteractiveShellApp.gui.values += ('gevent',)


def print_usage():
    print("\t{}use `{}raiden{}` to interact with the raiden service.".format(
        OKBLUE, HEADER, OKBLUE))
    print("\tuse `{}chain{}` to interact with the blockchain.".format(HEADER, OKBLUE))
    print("\tuse `{}discovery{}` to find raiden nodes.".format(HEADER, OKBLUE))
    print("\tuse `{}tools{}` for convenience with tokens, channels, funding, ...".format(
        HEADER, OKBLUE))
    print("\tuse `{}denoms{}` for ether calculations".format(HEADER, OKBLUE))
    print("\tuse `{}lastlog(n){}` to see n lines of log-output. [default 10] ".format(
        HEADER, OKBLUE))
    print("\tuse `{}lasterr(n){}` to see n lines of stderr. [default 1]".format(
        HEADER, OKBLUE))
    print("\tuse `{}help(<topic>){}` for help on a specific topic.".format(HEADER, OKBLUE))
    print("\ttype `{}usage(){}` to see this help again.".format(HEADER, OKBLUE))
    print("\n" + ENDC)


def inputhook_gevent():
    while not stdin_ready():
        gevent.sleep(0.05)
    return 0


@inputhook_manager.register('gevent')
class GeventInputHook:

    def __init__(self, manager):
        self.manager = manager
        self._current_gui = GUI_GEVENT

    def enable(self, app=None):
        """ Enable event loop integration with gevent.

        Args:
            app: Ignored, it's only a placeholder to keep the call signature of all
                gui activation methods consistent, which simplifies the logic of
                supporting magics.

        Notes:
            This methods sets the PyOS_InputHook for gevent, which allows
            gevent greenlets to run in the background while interactively using
            IPython.
        """
        self.manager.set_inputhook(inputhook_gevent)
        self._current_gui = GUI_GEVENT
        return app

    def disable(self):
        """ Disable event loop integration with gevent.

        This merely sets PyOS_InputHook to NULL.
        """
        self.manager.clear_inputhook()


class SigINTHandler:

    def __init__(self, event):
        self.event = event
        self.installed = None
        self.installed_force = None
        self.install_handler()

    def install_handler(self):
        if self.installed_force:
            self.installed_force.cancel()
            self.installed_force = None
        self.installed = gevent.signal(signal.SIGINT, self.handle_int)

    def install_handler_force(self):
        if self.installed:
            self.installed.cancel()
            self.installed = None
        self.installed_force = gevent.signal(signal.SIGINT, self.handle_force)

    def handle_int(self):
        self.install_handler_force()

        gevent.spawn(self._confirm_enter_console)

    def handle_force(self):  # pylint: disable=no-self-use
        """ User pressed ^C a second time. Send SIGTERM to ourself. """
        os.kill(os.getpid(), signal.SIGTERM)

    def _confirm_enter_console(self):
        start = time.time()
        sys.stdout.write('\n')
        enter_console = False
        while time.time() - start < ENTER_CONSOLE_TIMEOUT:
            prompt = (
                '\r{}{}Hit [ENTER], to launch console; [Ctrl+C] again to quit! [{:1.0f}s]{}'
            ).format(
                OKGREEN,
                BOLD,
                ENTER_CONSOLE_TIMEOUT - (time.time() - start),
                ENDC,
            )

            sys.stdout.write(prompt)
            sys.stdout.flush()

            try:
                r, _, _ = select.select([sys.stdin], [], [], .5)
            except select.error as ex:
                sys.stdout.write('\n')
                # "Interrupted system call" means the user pressed ^C again
                if ex.args[0] == errno.EINTR:
                    self.handle_force()
                    return
                else:
                    raise
            if r:
                sys.stdin.readline()
                enter_console = True
                break
        if enter_console:
            sys.stdout.write('\n')
            self.installed_force.cancel()
            self.event.set()
        else:
            msg = '\n{}{}No answer after {}s. Resuming.{}\n'.format(
                WARNING,
                BOLD,
                ENTER_CONSOLE_TIMEOUT,
                ENDC,
            )

            sys.stdout.write(msg)
            sys.stdout.flush()
            # Restore regular handler
            self.install_handler()


class BaseService(Greenlet):
    def __init__(self, app):
        Greenlet.__init__(self)
        self.is_stopped = False
        self.app = app
        self.config = app.config

    def start(self):
        self.is_stopped = False
        Greenlet.start(self)

    def stop(self):
        self.is_stopped = True
        Greenlet.kill(self)


class Console(BaseService):
    """ A service starting an interactive ipython session when receiving the
    SIGSTP signal (e.g. via keyboard shortcut CTRL-Z).
    """

    def __init__(self, app):
        super().__init__(app)
        self.interrupt = Event()
        self.console_locals = {}
        if app.start_console:
            self.start()
            self.interrupt.set()
        else:
            SigINTHandler(self.interrupt)

    def start(self):
        # start console service
        super().start()

        class Raiden:
            def __init__(self, app):
                self.app = app

        self.console_locals = dict(
            _raiden=Raiden(self.app),
            raiden=self.app.raiden,
            chain=self.app.raiden.chain,
            discovery=self.app.discovery,
            tools=ConsoleTools(
                self.app.raiden,
                self.app.discovery,
                self.app.config['settle_timeout'],
                self.app.config['reveal_timeout'],
            ),
            denoms=denoms,
            true=True,
            false=False,
            usage=print_usage,
        )

    def _run(self):  # pylint: disable=method-hidden
        self.interrupt.wait()
        print('\n' * 2)
        print('Entering Console' + OKGREEN)
        print('Tip:' + OKBLUE)
        print_usage()

        # Remove handlers that log to stderr
        root = structlog.get_logger()
        for handler in root.handlers[:]:
            if isinstance(handler, StreamHandler) and handler.stream == sys.stderr:
                root.removeHandler(handler)

        stream = io.StringIO()
        handler = StreamHandler(stream=stream)
        handler.formatter = Formatter(u'%(levelname)s:%(name)s %(message)s')
        root.addHandler(handler)

        def lastlog(n=10, prefix=None, level=None):
            """ Print the last `n` log lines to stdout.
            Use `prefix='p2p'` to filter for a specific logger.
            Use `level=INFO` to filter for a specific level.
            Level- and prefix-filtering are applied before tailing the log.
            """
            lines = (stream.getvalue().strip().split('\n') or [])
            if prefix:
                lines = [
                    line
                    for line in lines
                    if line.split(':')[1].startswith(prefix)
                ]
            if level:
                lines = [
                    line
                    for line in lines
                    if line.split(':')[0] == level
                ]
            for line in lines[-n:]:
                print(line)

        self.console_locals['lastlog'] = lastlog

        err = io.StringIO()
        sys.stderr = err

        def lasterr(n=1):
            """ Print the last `n` entries of stderr to stdout. """
            for line in (err.getvalue().strip().split('\n') or [])[-n:]:
                print(line)

        self.console_locals['lasterr'] = lasterr

        IPython.start_ipython(argv=['--gui', 'gevent'], user_ns=self.console_locals)
        self.interrupt.clear()

        sys.exit(0)


class ConsoleTools:
    def __init__(self, raiden_service, discovery, settle_timeout, reveal_timeout):
        self._chain = raiden_service.chain
        self._raiden = raiden_service
        self._api = RaidenAPI(raiden_service)
        self._discovery = discovery
        self.settle_timeout = settle_timeout
        self.reveal_timeout = reveal_timeout

    def create_token(
            self,
            registry_address,
            initial_alloc=10 ** 6,
            name='raidentester',
            symbol='RDT',
            decimals=2,
            timeout=60,
            auto_register=True):
        """ Create a proxy for a new HumanStandardToken (ERC20), that is
        initialized with Args(below).
        Per default it will be registered with 'raiden'.

        Args:
            initial_alloc (int): amount of initial tokens.
            name (str): human readable token name.
            symbol (str): token shorthand symbol.
            decimals (int): decimal places.
            timeout (int): timeout in seconds for creation.
            auto_register (boolean): if True(default), automatically register
                the token with raiden.

        Returns:
            token_address_hex: the hex encoded address of the new token/token.
        """
        contract_path = get_contract_path('HumanStandardToken.sol')
        # Deploy a new ERC20 token
        token_proxy = self._chain.client.deploy_solidity_contract(
            'HumanStandardToken',
            compile_files_cwd([contract_path]),
            dict(),
            (initial_alloc, name, decimals, symbol),
            contract_path=contract_path,
            timeout=timeout,
        )
        token_address_hex = hexlify(token_proxy.contract_address)
        if auto_register:
            self.register_token(registry_address, token_address_hex)
        print("Successfully created {}the token '{}'.".format(
            'and registered ' if auto_register else ' ',
            name,
        ))
        return token_address_hex

    def register_token(self, registry_address_hex, token_address_hex):
        """ Register a token with the raiden token manager.

        Args:
            registry_address: registry address
            token_address_hex (string): a hex encoded token address.

        Returns:
            channel_manager: the channel_manager contract_proxy.
        """

        registry = self._raiden.chain.registry(registry_address_hex)

        # Add the ERC20 token to the raiden registry
        token_address = safe_address_decode(token_address_hex)
        registry.add_token(token_address)

        # Obtain the channel manager for the token
        channel_manager = registry.manager_by_token(token_address)

        # Register the channel manager with the raiden registry
        self._raiden.register_channel_manager(channel_manager.address)
        return channel_manager

    def open_channel_with_funding(
            self,
            registry_address_hex,
            token_address_hex,
            peer_address_hex,
            total_deposit,
            settle_timeout=None,
            reveal_timeout=None,
    ):
        """ Convenience method to open a channel.

        Args:
            registry_address_hex (str): hex encoded address of the registry for the channel.
            token_address_hex (str): hex encoded address of the token for the channel.
            peer_address_hex (str): hex encoded address of the channel peer.
            total_deposit (int): amount of total funding for the channel.
            settle_timeout (int): amount of blocks for the settle time (if None use app defaults).
            reveal_timeout (int): amount of blocks for the reveal time (if None use app defaults).

        Return:
            netting_channel: the (newly opened) netting channel object.
        """
        # Check, if peer is discoverable
        registry_address = safe_address_decode(registry_address_hex)
        peer_address = safe_address_decode(peer_address_hex)
        token_address = safe_address_decode(token_address_hex)
        try:
            self._discovery.get(peer_address)
        except KeyError:
            print('Error: peer {} not found in discovery'.format(peer_address_hex))
            return

        self._api.channel_open(
            registry_address,
            token_address,
            peer_address,
            settle_timeout=settle_timeout,
            reveal_timeout=reveal_timeout,
        )

        return self._api.set_total_channel_deposit(
            registry_address,
            token_address,
            peer_address,
            total_deposit,
        )

    def wait_for_contract(self, contract_address_hex, timeout=None):
        """ Wait until a contract is mined

        Args:
            contract_address_hex (string): hex encoded address of the contract
            timeout (int): time to wait for the contract to get mined

        Returns:
            True if the contract got mined, false otherwise
        """
        contract_address = safe_address_decode(contract_address_hex)
        start_time = time.time()
        result = self._raiden.chain.client.web3.eth.getCode(
            to_checksum_address(contract_address),
        )

        current_time = time.time()
        while len(result) == 0:
            if timeout and start_time + timeout > current_time:
                return False

            result = self._raiden.chain.client.web3.eth.getCode(
                to_checksum_address(contract_address),
            )
            gevent.sleep(0.5)

            current_time = time.time()

        return len(result) > 0
