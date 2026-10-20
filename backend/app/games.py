import json
from pathlib import Path


CONFIG_DIR = Path(__file__).resolve().parents[1] / "config" / "games"


def games():
    return {path.stem: json.loads(path.read_text(encoding="utf-8")) for path in sorted(CONFIG_DIR.glob("*.yaml"))}


def game(code):
    try:
        return games()[code]
    except KeyError as exc:
        raise ValueError("Unknown game") from exc
