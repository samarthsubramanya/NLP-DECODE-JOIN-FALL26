import copy
import tomllib
from pathlib import Path

POC = Path(__file__).resolve().parent
DEFAULT = POC / "config.toml"


def load(path=DEFAULT, overrides=None):
    """Reads the TOML config and applies dotted overrides, e.g. {"joins.accept": 55}."""
    path = Path(path).resolve()
    cfg = tomllib.loads(path.read_text())
    for dotted, value in (overrides or {}).items():
        node = cfg
        *parents, leaf = dotted.split(".")
        for key in parents:
            node = node.setdefault(key, {})
        node[leaf] = value
    cfg["_dir"] = path.parent
    return cfg


def resolve(cfg, template, **fields):
    return (cfg["_dir"] / template.format(**fields)).resolve()


def parse_override(text):
    """'joins.accept=55' -> ("joins.accept", 55). Values are parsed as TOML."""
    key, raw = text.split("=", 1)
    return key.strip(), tomllib.loads(f"v = {raw.strip()}")["v"]


def run_dir(cfg, setting, run):
    return resolve(cfg, cfg["output"]["dir"]) / cfg["data"]["dataset"] / setting / run


def with_setting(cfg, setting):
    out = copy.copy(cfg)
    out["data"] = {**cfg["data"], "setting": setting}
    return out
