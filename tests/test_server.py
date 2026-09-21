from pasar.config import DEFAULT_ADDRESS, Config
from pasar.server import build, split_bind, uvicorn_servers


def test_split_bind():
    assert split_bind("127.0.0.1:8750") == ("127.0.0.1", 8750)
    assert split_bind("[::1]:9000") == ("::1", 9000)


def test_build_creates_data_dir(tmp_path):
    cfg = Config(data_dir=str(tmp_path / "data"), mascot_dir=str(tmp_path / "m"))
    _daemon, app = build(cfg)
    assert (tmp_path / "data" / "pasar.db").exists()
    assert any(r.path == "/api/status" for r in app.routes)


def test_uvicorn_servers_bind_to_addresses_not_just_extra_bind(tmp_path):
    """cfg.bind holds only extra addresses; the server must still listen on
    DEFAULT_ADDRESS (via cfg.addresses()), plus any extra bind entries."""
    cfg = Config(data_dir=str(tmp_path / "data"), mascot_dir=str(tmp_path / "m"),
                 bind=["100.64.0.1:9001"])
    _daemon, app = build(cfg)
    servers = uvicorn_servers(app, cfg.addresses())
    hosts_ports = [(s.config.host, s.config.port) for s in servers]
    assert hosts_ports == [split_bind(DEFAULT_ADDRESS), ("100.64.0.1", 9001)]
