import importlib

from pylinkirc.launcher import main

import pylinkirc.utils
import pylinkirc.selectdriver

def _get_protocol_module(name):
    """
    Imports and returns the protocol module requested.
    """
    try:
        return importlib.import_module(pylinkirc.utils.PROTOCOL_PREFIX + name)
    except ModuleNotFoundError:
        return importlib.import_module('protocols.' + name)
pylinkirc.utils._get_protocol_module = _get_protocol_module

def _process_conns():
    """"Fix to prevent error on Windows"""

    from pylinkirc import world
    from pylinkirc.selectdriver import selector, selectors, SELECT_TIMEOUT, log
    while not world.shutting_down.is_set():
        try:
            for socketkey, mask in selector.select(timeout=SELECT_TIMEOUT):
                irc = socketkey.data
                try:
                    if mask & selectors.EVENT_READ and not irc._aborted.is_set():
                        irc._run_irc()
                except:
                    log.exception('Error in select driver loop:')
                    continue
        except OSError:
            continue

pylinkirc.selectdriver._process_conns = _process_conns

if __name__ == '__main__':
    import os
    try:
        os.remove("pylink.pid")
    except FileNotFoundError:
        pass
    main()
