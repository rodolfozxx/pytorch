#!/usr/bin/env python3

# Copyright (c) Facebook, Inc. and its affiliates.
# All rights reserved.
#
# This source code is licensed under the BSD-style license found in the
# LICENSE file in the root directory of this source tree.

"""
Library that launches and manages ``n`` copies of worker subprocesses either specified by a function or a binary.

For functions, it uses ``torch.multiprocessing`` (and therefore python
``multiprocessing``) to spawn/fork worker processes. For binaries it uses python
``subprocessing.Popen`` to create worker processes.


Usage 1: Launching two trainers as a function

::

 from torch.distributed.elastic.multiprocessing import Std, start_processes

 def trainer(a, b, c):
     pass # train


 # runs two trainers
 # LOCAL_RANK=0 trainer(1,2,3)
 # LOCAL_RANK=1 trainer(4,5,6)
 ctx = start_processes(
         name="trainer",
         entrypoint=trainer,
         args={0: (1,2,3), 1: (4,5,6)},
         envs={0: {"LOCAL_RANK": 0}, 1: {"LOCAL_RANK": 1}},
         log_dir="/tmp/foobar",
         redirects=Std.ALL, # write all worker stdout/stderr to a log file
         tee={0: Std.ERR}, # tee only local rank 0's stderr to console
       )

 # waits for all copies of trainer to finish
 ctx.wait()

Usage 2: Launching 2 echo workers as a binary

::

 # same as invoking
 # echo hello
 # echo world > stdout.log
 ctx = start_processes(
         name="echo"
         entrypoint="echo",
         log_dir="/tmp/foobar",
         args={0: "hello", 1: "world"},
         redirects={1: Std.OUT},
        )

Just like ``torch.multiprocessing``, the return value of the function
:func:`start_processes` is a process context (:class:`api.PContext`). If a function
was launched, a :class:`api.MultiprocessContext` is returned and if a binary
was launched a :class:`api.SubprocessContext` is returned. Both are specific
implementations of the parent :class:`api.PContext` class.
"""

import os
from typing import Callable, Dict, Optional, Tuple, Union, Set

from torch.distributed.elastic.multiprocessing.api import (  # noqa: F401
    _validate_full_rank,
    MultiprocessContext,
    PContext,
    ProcessFailure,
    RunProcsResult,
    SignalException,
    Std,
    SubprocessContext,
    to_map,
)
from torch.distributed.elastic.utils.logging import get_logger

__all__ = [
    "start_processes",
    "MultiprocessContext",
    "PContext",
    "ProcessFailure",
    "RunProcsResult",
    "SignalException",
    "Std",
    "SubprocessContext",
    "to_map",
]

log = get_logger(__name__)


def start_processes(
    name: str,
    entrypoint: Union[Callable, str],
    args: Dict[int, Tuple],
    envs: Dict[int, Dict[str, str]],
    log_dir: str,
    log_line_prefixes: Optional[Dict[int, str]] = None,
    start_method: str = "spawn",
    redirects: Union[Std, Dict[int, Std]] = Std.NONE,
    tee: Union[Std, Dict[int, Std]] = Std.NONE,
    local_ranks_filter: Optional[Set[int]] = None,
    log_for_fb_tupperware: Optional[Tuple[int, int]] = None,  # rdzv_run_id, restart_count
) -> PContext:
    """
    Start ``n`` copies of ``entrypoint`` processes with the provided options.

    ``entrypoint`` is either a ``Callable`` (function) or a ``str`` (binary).
    The number of copies is determined by the number of entries for ``args`` and
    ``envs`` arguments, which need to have the same key set.

    ``args`` and ``env`` parameters are the arguments and environment variables
    to pass down to the entrypoint mapped by the replica index (local rank).
    All local ranks must be accounted for.
    That is, the keyset should be ``{0,1,...,(nprocs-1)}``.

    .. note:: When the ``entrypoint`` is a binary (``str``), ``args`` can only be strings.
              If any other type is given, then it is casted to a string representation
              (e.g. ``str(arg1)``). Furthermore, a binary failure will only write
              an ``error.json`` error file if the main function is annotated with
              ``torch.distributed.elastic.multiprocessing.errors.record``. For function launches,
              this is done by default and there is no need to manually annotate
              with the ``@record`` annotation.

    ``redirects`` and ``tee`` are bitmasks specifying which std stream(s) to redirect
    to a log file in the ``log_dir``. Valid mask values are defined in ``Std``.
    To redirect/tee only certain local ranks, pass ``redirects`` as a map with the key as
    the local rank to specify the redirect behavior for.
    Any missing local ranks will default to ``Std.NONE``.

    ``tee`` acts like the unix "tee" command in that it redirects + prints to console.
    To avoid worker stdout/stderr from printing to console, use the ``redirects`` parameter.

    For each process, the ``log_dir`` will contain:

    #. ``{local_rank}/error.json``: if the process failed, a file with the error info
    #. ``{local_rank}/stdout.json``: if ``redirect & STDOUT == STDOUT``
    #. ``{local_rank}/stderr.json``: if ``redirect & STDERR == STDERR``

    .. note:: It is expected that the ``log_dir`` exists, is empty, and is a directory.

    Example:
    ::

     log_dir = "/tmp/test"

     # ok; two copies of foo: foo("bar0"), foo("bar1")
     start_processes(
        name="trainer",
        entrypoint=foo,
        args:{0:("bar0",), 1:("bar1",),
        envs:{0:{}, 1:{}},
        log_dir=log_dir
     )

     # invalid; envs missing for local rank 1
     start_processes(
        name="trainer",
        entrypoint=foo,
        args:{0:("bar0",), 1:("bar1",),
        envs:{0:{}},
        log_dir=log_dir
     )

     # ok; two copies of /usr/bin/touch: touch file1, touch file2
     start_processes(
        name="trainer",
        entrypoint="/usr/bin/touch",
        args:{0:("file1",), 1:("file2",),
        envs:{0:{}, 1:{}},
        log_dir=log_dir
      )

     # caution; arguments casted to string, runs:
     # echo "1" "2" "3" and echo "[1, 2, 3]"
     start_processes(
        name="trainer",
        entrypoint="/usr/bin/echo",
        args:{0:(1,2,3), 1:([1,2,3],),
        envs:{0:{}, 1:{}},
        log_dir=log_dir
      )

    Args:
        name: a human readable short name that describes what the processes are
              (used as header when tee'ing stdout/stderr outputs)
        entrypoint: either a ``Callable`` (function) or ``cmd`` (binary)
        args: arguments to each replica
        envs: env vars to each replica
        log_dir: directory used to write log files
        start_method: multiprocessing start method (spawn, fork, forkserver)
                      ignored for binaries
        redirects: which std streams to redirect to a log file
        tee: which std streams to redirect + print to console
        local_ranks_filter: which ranks' logs to print to console

    """
    # listdir raises FileNotFound or NotADirectoryError so no need to check manually
    if log_for_fb_tupperware is None and log_dir != os.devnull and os.listdir(log_dir):
        raise RuntimeError(
            f"log_dir: {log_dir} is not empty, please provide an empty log_dir"
        )

    nprocs = len(args)
    _validate_full_rank(args, nprocs, "args")
    _validate_full_rank(envs, nprocs, "envs")

    # create subdirs for each local rank in the logs_dir
    # logs_dir
    #       |- 0
    #          |- error.json
    #          |- stdout.log
    #          |- stderr.log
    #       |- ...
    #       |- (nprocs-1)
    redirs = to_map(redirects, nprocs)
    ts = to_map(tee, nprocs)

    # to tee stdout/stderr we first redirect into a file
    # then tail -f stdout.log/stderr.log so add tee settings to redirects
    for local_rank, tee_std in ts.items():
        redirect_std = redirs[local_rank]
        redirs[local_rank] = redirect_std | tee_std

    SYS_STREAM = ""  # special case to indicate to output to console
    stdouts = dict.fromkeys(range(nprocs), SYS_STREAM)
    stderrs = dict.fromkeys(range(nprocs), SYS_STREAM)
    tee_stdouts: Dict[int, str] = {}
    tee_stderrs: Dict[int, str] = {}
    error_files = {}

    for local_rank in range(nprocs):
        if log_dir == os.devnull:
            tee_stdouts[local_rank] = os.devnull
            tee_stderrs[local_rank] = os.devnull
            error_files[local_rank] = os.devnull
            envs[local_rank]["TORCHELASTIC_ERROR_FILE"] = ""
        else:
            if log_for_fb_tupperware:
                clogdir = log_dir
            else:
                clogdir = os.path.join(log_dir, str(local_rank))
                os.mkdir(clogdir)

            if log_for_fb_tupperware is not None:
                log_filename_prefix = (
                    f"dedicated_log_elastic_{log_for_fb_tupperware[0]}_"
                    f"attempt_{log_for_fb_tupperware[1]}_rank_{local_rank}_"
                )
            else:
                log_filename_prefix = ""

            rd = redirs[local_rank]
            if (rd & Std.OUT) == Std.OUT:
                stdouts[local_rank] = os.path.join(clogdir, f"{log_filename_prefix}stdout.log")
            if (rd & Std.ERR) == Std.ERR:
                stderrs[local_rank] = os.path.join(clogdir, f"{log_filename_prefix}stderr.log")

            t = ts[local_rank]
            if t & Std.OUT == Std.OUT:
                tee_stdouts[local_rank] = stdouts[local_rank]
            if t & Std.ERR == Std.ERR:
                tee_stderrs[local_rank] = stderrs[local_rank]

            if local_ranks_filter and local_rank not in local_ranks_filter:
                # If stream is tee'd, only write to file, but don't tail
                if local_rank in tee_stdouts:
                    tee_stdouts.pop(local_rank, None)
                if local_rank in tee_stderrs:
                    tee_stderrs.pop(local_rank, None)

                # If stream is not redirected, don't print
                if stdouts[local_rank] == SYS_STREAM:
                    stdouts[local_rank] = os.devnull
                if stderrs[local_rank] == SYS_STREAM:
                    stderrs[local_rank] = os.devnull

            error_file = os.path.join(clogdir, f"{log_filename_prefix}error.json")
            error_files[local_rank] = error_file
            log.info("Setting worker%s reply file to: %s", local_rank, error_file)
            envs[local_rank]["TORCHELASTIC_ERROR_FILE"] = error_file

    context: PContext
    if isinstance(entrypoint, str):
        context = SubprocessContext(
            name=name,
            entrypoint=entrypoint,
            args=args,
            envs=envs,
            stdouts=stdouts,
            stderrs=stderrs,
            tee_stdouts=tee_stdouts,
            tee_stderrs=tee_stderrs,
            error_files=error_files,
            log_line_prefixes=log_line_prefixes,
        )
    else:
        context = MultiprocessContext(
            name=name,
            entrypoint=entrypoint,
            args=args,
            envs=envs,
            stdouts=stdouts,
            stderrs=stderrs,
            tee_stdouts=tee_stdouts,
            tee_stderrs=tee_stderrs,
            error_files=error_files,
            log_line_prefixes=log_line_prefixes,
            start_method=start_method,
        )

    try:
        context.start()
        return context
    except Exception:
        context.close()
        raise
