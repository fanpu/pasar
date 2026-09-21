import subprocess

import pytest

from pasar.probes import (
    Gpu,
    Probe,
    cgroup_memory,
    cgroup_pids,
    read_meminfo,
    read_psi_some_avg10,
    xid_errors_between,
)


def test_meminfo(tmp_path):
    p = tmp_path / "meminfo"
    p.write_text("MemTotal:       127600748 kB\nMemFree: 1 kB\nMemAvailable:   100157048 kB\n")
    assert read_meminfo(p) == (127600748 * 1024, 100157048 * 1024)


def test_psi(tmp_path):
    p = tmp_path / "memory"
    p.write_text("some avg10=12.50 avg60=3.00 avg300=1.00 total=48\n"
                 "full avg10=0.00 avg60=0.00 avg300=0.00 total=48\n")
    assert read_psi_some_avg10(p) == 12.5


def test_cgroup_readers(tmp_path):
    d = tmp_path / "unit"
    (d / "child").mkdir(parents=True)
    (d / "memory.current").write_text("4096\n")
    (d / "cgroup.procs").write_text("10\n11\n")
    (d / "child" / "cgroup.procs").write_text("12\n")
    assert cgroup_memory(d) == 4096
    assert sorted(cgroup_pids(d)) == [10, 11, 12]
    assert cgroup_memory(tmp_path / "missing") == 0 and cgroup_pids(tmp_path / "missing") == []


def test_probe_resolves_control_group_paths(tmp_path):
    d = tmp_path / "cg" / "user.slice" / "u.service"
    d.mkdir(parents=True)
    (d / "memory.current").write_text("7\n")
    probe = Probe(proc=tmp_path, cgroup_root=tmp_path / "cg", gpu=Gpu.__new__(Gpu))
    assert probe.cgroup_memory("/user.slice/u.service") == 7


def test_xid_counts_matching_lines():
    def fake_run(cmd, **kw):
        return subprocess.CompletedProcess(cmd, 0, "a NVRM: Xid (PCI:0000): 79\nother\nNVRM: Xid 13\n", "")
    assert xid_errors_between(0, 10, run=fake_run) == 2

    def broken(cmd, **kw):
        raise OSError("no journalctl")
    assert xid_errors_between(0, 10, run=broken) == 0


@pytest.mark.gpu
def test_nvml_sees_processes():
    assert Gpu().ok
