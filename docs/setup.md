# Set up discord-md

## Configure Discord

1. Create an application and bot in the [Discord Developer Portal](https://discord.com/developers/applications). On the **Bot** page, enable **Message Content Intent**.
2. Install the bot in each server with the `bot` scope. Give it **View Channel** and **Read Message History** in the channels you want to export.
3. In Discord, turn on **Developer Mode** under **User Settings → Advanced**. Right-click each server and channel to copy its ID.

## Install and configure the bot

On the machine where the Perch engine runs, use Python 3.11 or later. Run these commands from the discord-md checkout.

```sh
python3 -m venv ~/.local/share/discord-md/venv
~/.local/share/discord-md/venv/bin/pip install .

config_dir="${XDG_CONFIG_HOME:-$HOME/.config}"
install -d -m 700 "$config_dir/discord-md"
install -m 600 config.example.toml "$config_dir/discord-md/config.toml"
(umask 077 && touch "$config_dir/discord-md/token.env")
```

Edit `config.toml` and add a `[[channels]]` entry for each text or forum channel to export. Use two lowercase path segments such as `slatedb/dev` and a distinct path for each channel. discord-md uses the IDs to follow each source and the path to place it in the export.

Put `DISCORD_TOKEN=your-bot-token` in `token.env`. Keep the file readable only by your user.

## Run the service

```sh
config_dir="${XDG_CONFIG_HOME:-$HOME/.config}"
install -Dm644 systemd/discord-md.service "$config_dir/systemd/user/discord-md.service"
systemctl --user daemon-reload
systemctl --user enable --now discord-md.service
journalctl --user -u discord-md.service -f
```

The service keeps the bot connected and restarts it after a failure. To keep the user service running after logout and start it at boot, enable lingering for your Linux account with `loginctl enable-linger "$USER"`.

For a foreground run, set `DISCORD_TOKEN` in the environment and run `~/.local/share/discord-md/venv/bin/discord-md`. Use `--config PATH` to select another TOML file.

## Add the export to Perch

In **Settings → Knowledge**, add `discord/slatedb` and select the repositories that use those conversations. Use this entry in Perch's `config.toml` for the same result.

```toml
[[knowledge]]
dir = "discord/slatedb"
repos = ["slatedb/slatedb", "slatedb/slatedb-go"]
```

Start a new Perch agent session to pick up the directory. discord-md writes to `~/.perch/knowledge/discord` by default. Set `output_dir` to the corresponding directory if the engine uses a different Perch home. Each project has an `INDEX.md` that links to daily channel transcripts and files for public threads and forum posts. Messages include their author, UTC time, and Discord link. The export includes reply links and attachment names. Keep these paths free for the generated files.

## History and recovery

discord-md imports 30 days of message history and follows readable public threads in configured channels. It applies new messages, edits, and deletions as Discord sends them. On each sync it checks active threads, recently archived threads, and threads it already tracks.

The bot keeps its sync position and message data in `$XDG_STATE_HOME/discord-md/archive.sqlite3` or `~/.local/state/discord-md/archive.sqlite3`. After reconnecting, it fetches messages newer than the saved position in each tracked channel and thread. It also rechecks the last seven days for missed edits and deletes. Offline changes to older messages need a wider recheck. Set `backfill_days` and `reconcile_days` in `config.toml` to change these windows. For threads the bot has yet to track, it looks back by the longer of the two windows to find archived threads.
