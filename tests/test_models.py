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
