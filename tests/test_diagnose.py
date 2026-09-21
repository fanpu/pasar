from pasar.diagnose import diagnose

TRACEBACK = """step 10 loss 1.2
Traceback (most recent call last):
  File "train.py", line 3, in <module>
    main()
ValueError: shapes (3,4) and (5,) not aligned
"""


def test_kernel_oom():
    d = diagnose(exit_code=None, signal="SIGKILL", result="oom-kill", log_tail="")
    assert d.reason == "kernel_oom"


def test_gpu_oom_from_log():
    log = "torch.OutOfMemoryError: CUDA out of memory. Tried to allocate 2.00 GiB\n"
    d = diagnose(exit_code=1, signal=None, result="exit-code", log_tail=log)
    assert d.reason == "gpu_oom" and d.summary.startswith("torch.OutOfMemoryError")


def test_xid():
    d = diagnose(exit_code=None, signal="SIGABRT", result="signal", log_tail="", xid_errors=2)
    assert d.reason == "gpu_xid"


def test_signal():
    d = diagnose(exit_code=None, signal="SIGSEGV", result="core-dump", log_tail="")
    assert d.reason == "signal" and d.summary == "killed by SIGSEGV"


def test_python_exception_summary():
    d = diagnose(exit_code=1, signal=None, result="exit-code", log_tail=TRACEBACK)
    assert d.reason == "exit" and d.summary == "ValueError: shapes (3,4) and (5,) not aligned"


def test_nccl_and_plain_exit():
    log = "NCCL WARN something\nNCCL error: unhandled system error\n"
    assert diagnose(exit_code=1, signal=None, result="exit-code", log_tail=log).summary == (
        "NCCL error: unhandled system error"
    )
    d = diagnose(exit_code=3, signal=None, result="exit-code", log_tail="bye\n")
    assert d.summary == "exited with code 3"
