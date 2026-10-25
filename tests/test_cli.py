"""The command line as a whole: it starts with the fireaid it is pinned to, and options keep what was typed."""

import signal

import pytest

import mvconnect.cli as cli
from mvconnect.masterbus import ENDING


@pytest.fixture(autouse=True)
def signals_as_they_were():
    """main() sets handlers for SIGTERM and SIGHUP, where there is one; the test process gets its own back."""
    before = {s: signal.getsignal(s) for s in ENDING}
    yield
    for s, handler in before.items():
        signal.signal(s, handler)


def test_ctrl_c_ends_a_command_with_130_not_a_traceback(monkeypatch, caplog):
    import logging

    import pytest

    from expect import logged

    def interrupted(component, **options):
        raise KeyboardInterrupt
    monkeypatch.setattr(cli.fire, "Fire", interrupted)
    with caplog.at_level(logging.INFO, logger="mvconnect"), pytest.raises(SystemExit) as e:
        cli.main()
    assert e.value.code == 130
    assert logged(caplog, "WARNING", "Ctrl-C", "wound down", 130) and not logged(caplog, "ERROR", "interrupted")


def test_a_kill_ends_a_command_through_its_cleanups(monkeypatch, caplog):
    import logging

    from expect import logged
    caplog.set_level(logging.INFO, logger="mvconnect")
    cleaned = []

    def killed(component, **options):
        try:
            signal.raise_signal(signal.SIGTERM)
        finally:
            cleaned.append(True)  # a flash-mode exit, say
    monkeypatch.setattr(cli.fire, "Fire", killed)
    with pytest.raises(SystemExit) as e:
        cli.main()
    assert e.value.code == 128 + signal.SIGTERM and cleaned
    assert logged(caplog, "WARNING", "SIGTERM", "wound down", 128 + signal.SIGTERM)


def test_main_starts_its_remote_with_a_setup(monkeypatch):
    called = {}
    monkeypatch.setattr(cli.fire, "Fire", lambda component, **options: called.update(options, component=component))
    cli.main()  # an older fireaid refuses Remote(setup=) before anything runs
    assert isinstance(called["component"], cli.Tool) and called["remote"] is not None


def test_a_server_open_to_other_computers_runs_and_leaves_the_warning_to_fireaid(monkeypatch, caplog):
    """The server is for a network of one's own: open or not, it starts. fireaid's server warns of an open one as
    it starts, so the setup adds no warning of its own: one per thing to know, not three for one."""
    import logging
    import sys

    from expect import logged
    from mvconnect import state
    started, at_exit = [], []

    class Server:
        def shutdown(self):
            pass
    monkeypatch.setattr(state, "start_in_thread", lambda: started.append(True) or Server())
    monkeypatch.setattr(cli.atexit, "register", at_exit.append)
    monkeypatch.setattr(cli, "setup_logging", lambda debug=False: None)  # keeps pytest's log capture in place
    monkeypatch.delenv("MVCONNECT_SERVER_PASSWORD", raising=False)
    servers = (["server"], ["server", "--bind", "0.0.0.0"], ["server", "--bind=::"], ["server", "--bind", "127.0.0.1"],
               ["server", "--password", "x"], ["server", "-b", "127.0.0.1"])
    for argv in servers:
        caplog.clear()
        monkeypatch.setattr(sys, "argv", ["mvconnect", *argv])
        with caplog.at_level(logging.WARNING, logger="mvconnect"):
            cli.serve_setup(False)
        ours = [r.getMessage() for r in caplog.records if r.name == "mvconnect"]
        assert ours == [cli.EXPERIMENTAL], argv  # the warning every command gives, and nothing about passwords
    assert not logged(caplog, "ERROR", "bus")
    assert len(started) == len(at_exit) == len(servers)  # each one started, and is shut down at exit


def test_a_server_that_cannot_open_the_link_does_not_start(monkeypatch, capsys):
    """Every command it ran would fail as it did, so the server ends with status 1 and the reason, in one line."""
    import sys

    from mvconnect import state
    from mvconnect.hidlink import LinkError

    def unopenable():
        raise LinkError("the bus state did not start: cannot open the MasterBus USB Link at 2-1:1.0: open failed")
    monkeypatch.setattr(state, "start_in_thread", unopenable)
    monkeypatch.setattr(sys, "argv", ["mvconnect", "server"])
    with pytest.raises(SystemExit) as e:
        cli.serve_setup(False)
    err = capsys.readouterr().err
    assert e.value.code == 1 and "cannot open" in err and "does not start" in err
    lines = err.strip().splitlines()
    assert len(lines) == 2 and cli.EXPERIMENTAL in lines[0] and "udev" not in err  # the warning, one line, no remedy


def test_every_command_begins_with_the_experimental_warning(capsys):
    """The first line of every command, before anything else it logs or checks."""
    cli.start(cli.Tool.data)
    first = capsys.readouterr().err.splitlines()[0]
    assert "WARNING" in first and cli.EXPERIMENTAL in first


def test_a_relayed_exit_status_is_no_signal_here(monkeypatch, caplog):
    """A remote command's status 130 or 143, relayed by fireaid's client, ends the command without a wind-down line."""
    import logging

    def relayed(component, **options):
        raise SystemExit(128 + signal.SIGTERM)
    monkeypatch.setattr(cli.fire, "Fire", relayed)
    with caplog.at_level(logging.INFO, logger="mvconnect"), pytest.raises(SystemExit) as e:
        cli.main()
    assert e.value.code == 128 + signal.SIGTERM and not caplog.records
