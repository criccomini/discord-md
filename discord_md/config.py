import re
import tomllib
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Source:
    server_id: int
    path: Path


@dataclass(frozen=True)
class Settings:
    output_dir: Path
    backfill_days: int
    reconcile_days: int
    sources: dict[int, Source]

    @classmethod
    def load(cls, path: Path) -> "Settings":
        with path.open("rb") as file:
            config = tomllib.load(file)

        channels = config.get("channels")
        if not isinstance(channels, list) or not channels:
            raise ValueError("Add at least one [[channels]] entry")

        sources = {}
        paths = set()
        for entry in channels:
            if not isinstance(entry, dict):
                raise ValueError("Use a [[channels]] table for each channel")
            server_id = read_id(entry, "server_id")
            channel_id = read_id(entry, "channel_id")
            name = entry.get("path")
            if not isinstance(name, str) or not re.fullmatch(r"[a-z0-9][a-z0-9_-]*/[a-z0-9][a-z0-9_-]*", name):
                raise ValueError("Use a path such as slatedb/dev for each channel")
            folder = Path(name)
            if channel_id in sources or folder in paths:
                raise ValueError("Use a distinct channel ID and path for each entry")
            sources[channel_id] = Source(server_id, folder)
            paths.add(folder)

        output = config.get("output_dir", "~/.perch/knowledge/discord")
        if not isinstance(output, str) or not Path(output).expanduser().is_absolute():
            raise ValueError("output_dir must be an absolute path")

        backfill = config.get("backfill_days", 30)
        reconcile = config.get("reconcile_days", 7)
        if any(type(days) is not int or days < 0 for days in (backfill, reconcile)):
            raise ValueError("backfill_days and reconcile_days must be nonnegative integers")
        return cls(Path(output).expanduser(), backfill, reconcile, sources)


def read_id(entry: dict, name: str) -> int:
    value = entry.get(name)
    if isinstance(value, str) and re.fullmatch(r"[1-9][0-9]{0,18}", value):
        snowflake = int(value)
        if snowflake < 2**63:
            return snowflake
    raise ValueError(f"{name} must be a quoted Discord ID")
