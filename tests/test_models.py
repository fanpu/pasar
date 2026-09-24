from pasar.models import TERMINAL, JobSpec, State, default_name


def test_default_name():
    assert default_name(".venv/bin/python train.py --lr 3e-5") == "train"
    assert default_name("bash scripts/eval.sh") == "eval"
    assert default_name("CUDA_VISIBLE_DEVICES=0 uv run python -m foo.bar") == "foo.bar"
    assert default_name("./run_all") == "run_all"
    assert default_name("") == "job"


def test_spec_json_round_trip_drops_env():
    spec = JobSpec(command="python a.py", est_runtime=60, cwd="/tmp", env={"SECRET": "x"}, tags=["a"])
    text = spec.to_json()
    assert "SECRET" not in text
    back = JobSpec.from_json(text)
    assert back.command == "python a.py" and back.tags == ["a"] and back.env is None


def test_terminal_states():
    assert TERMINAL == {State.COMPLETED, State.FAILED, State.CANCELLED}


def test_spec_roundtrip_keeps_cloud_fields():
    spec = JobSpec(command="train.py", est_runtime=3600, cwd="/tmp", target="modal",
                   gpu="H100:2", env_keys=["WANDB_API_KEY"], data=["/tmp/d"], max_cost=12.5)
    assert JobSpec.from_json(spec.to_json()) == spec


def test_spec_defaults_to_local():
    assert JobSpec(command="x", est_runtime=1, cwd="/tmp").target == "local"


def test_awaiting_is_neither_active_nor_terminal():
    from pasar.daemon import ACTIVE
    assert State.AWAITING not in ACTIVE and State.AWAITING not in TERMINAL


def test_spec_roundtrip_keeps_the_group():
    spec = JobSpec(command="x", est_runtime=1, cwd="/tmp", target="modal-a", group="modal")
    assert JobSpec.from_json(spec.to_json()).group == "modal"


def test_a_spec_stored_before_groups_existed_still_loads():
    old = ('{"command": "x", "est_runtime": 1, "cwd": "/tmp", "target": "modal", '
           '"gpu": "H100"}')
    spec = JobSpec.from_json(old)
    assert spec.target == "modal" and spec.group == ""
