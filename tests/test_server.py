from pasar.config import DEFAULT_ADDRESS, CloudTarget, Config
from pasar.server import build, split_bind, tick_and_housekeep, uvicorn_servers


def test_split_bind():
    assert split_bind("127.0.0.1:8750") == ("127.0.0.1", 8750)
    assert split_bind("[::1]:9000") == ("::1", 9000)


def test_build_creates_data_dir(tmp_path):
    cfg = Config(data_dir=str(tmp_path / "data"), mascot_dir=str(tmp_path / "m"))
    _daemon, app = build(cfg)
    assert (tmp_path / "data" / "pasar.db").exists()
    assert any(r.path == "/api/status" for r in app.routes)


def test_build_hands_the_daemon_its_providers(tmp_path, monkeypatch):
    """The single line that decides whether a configured cloud target can run anything at all."""
    monkeypatch.setattr("pasar.server.build_providers", lambda cfg, data: {"modal": object()})
    cloud = CloudTarget(name="modal", provider="modal", daily_budget=1.0, monthly_budget=10.0)
    cfg = Config(data_dir=str(tmp_path), mascot_dir=str(tmp_path / "m"), clouds={"modal": cloud})
    daemon, _app = build(cfg)
    assert "modal" in daemon.executors


def test_tick_and_housekeep_survives_a_tick_exception(caplog):
    # A tick that raises must not kill the scheduler loop; the next pass should still run.
    class FlakyDaemon:
        def __init__(self):
            self.ticks = 0
            self.housekept = 0

        def tick(self):
            self.ticks += 1
            if self.ticks == 1:
                raise RuntimeError("boom")

        def housekeep(self):
            self.housekept += 1

    daemon = FlakyDaemon()
    with caplog.at_level("ERROR"):
        last_housekeep = tick_and_housekeep(daemon, 0.0)
    assert daemon.ticks == 1 and daemon.housekept == 0  # housekeep skipped: tick raised first
    assert "tick failed" in caplog.text

    last_housekeep = tick_and_housekeep(daemon, last_housekeep)
    assert daemon.ticks == 2 and daemon.housekept == 1  # loop kept going on the next pass


def test_uvicorn_servers_bind_to_addresses_not_just_extra_bind(tmp_path):
    """cfg.bind holds only extra addresses; the server must still listen on
    DEFAULT_ADDRESS (via cfg.addresses()), plus any extra bind entries."""
    cfg = Config(data_dir=str(tmp_path / "data"), mascot_dir=str(tmp_path / "m"),
                 bind=["100.64.0.1:9001"])
    _daemon, app = build(cfg)
    servers = uvicorn_servers(app, cfg.addresses())
    hosts_ports = [(s.config.host, s.config.port) for s in servers]
    assert hosts_ports == [split_bind(DEFAULT_ADDRESS), ("100.64.0.1", 9001)]
